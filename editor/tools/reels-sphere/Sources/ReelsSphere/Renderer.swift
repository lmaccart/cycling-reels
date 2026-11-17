import AVFoundation
import CoreVideo
import Foundation
import Metal
import VideoToolbox
import simd

private struct Uniforms {
    var matrix: simd_float3x3
    var tanHalf: Float
    var aspect: Float
    var eac: Int32
    var planet: Int32
    var fullRange: Int32
}

/// One retained current sample + one lookahead per track; one GPU frame in flight.
private final class TrackFrames {
    let output: AVAssetReaderTrackOutput
    let reader: AVAssetReader
    let request: RenderRequest
    var current: CMSampleBuffer?
    var next: CMSampleBuffer?
    var firstPTS: Double?
    var previousPTS = -Double.infinity

    init(track: AVAssetTrack, reader: AVAssetReader, request: RenderRequest) throws {
        self.reader = reader
        self.request = request
        output = AVAssetReaderTrackOutput(track: track, outputSettings: [
            kCVPixelBufferPixelFormatTypeKey as String: request.fullRange ? kCVPixelFormatType_420YpCbCr8BiPlanarFullRange : kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            kCVPixelBufferMetalCompatibilityKey as String: true,
            kCVPixelBufferIOSurfacePropertiesKey as String: [:],
        ])
        output.alwaysCopiesSampleData = false
        guard reader.canAdd(output) else { throw SphereFailure.unsupported("AVAssetReader cannot decode track \(track.trackID)") }
        reader.add(output)
    }

    private func read() throws -> CMSampleBuffer? {
        while let sample = output.copyNextSampleBuffer() {
            let pts = sample.presentationTimeStamp.seconds
            guard pts.isFinite, pts > previousPTS else { throw SphereFailure.runtime("non-monotonic source PTS") }
            previousPTS = pts
            if pts + 1e-9 < request.sourceIn { continue }
            if pts >= request.sourceOut - 1e-9 { return nil }
            guard CMSampleBufferGetImageBuffer(sample) != nil else { throw SphereFailure.runtime("decoded sample has no image") }
            return sample
        }
        if reader.status == .failed { throw SphereFailure.runtime("decode: \(String(describing: reader.error))") }
        return nil
    }

    func sample(frame: Int) throws -> CMSampleBuffer {
        if current == nil {
            current = try read()
            guard let current else { throw SphereFailure.runtime("no frames in source interval") }
            firstPTS = current.presentationTimeStamp.seconds
            next = try read()
        }
        while let n = next, fpsTick(pts: n.presentationTimeStamp.seconds, first: firstPTS!, speed: request.speed, fps: request.fps) <= frame {
            current = n
            next = try read()
        }
        return current!
    }
}

final class OfflineRenderer {
    let request: RenderRequest
    let asset: AVURLAsset
    let tracks: [AVAssetTrack]
    let device: MTLDevice
    let queue: MTLCommandQueue
    let assemble: MTLComputePipelineState
    let reproject: MTLComputePipelineState
    let cache: CVMetalTextureCache

    init(_ request: RenderRequest) async throws {
        self.request = request
        try request.validate()
        asset = AVURLAsset(url: URL(fileURLWithPath: request.source), options: [AVURLAssetOverrideMIMETypeKey: "video/mp4"])
        let all: [AVAssetTrack]
        do { all = try await asset.loadTracks(withMediaType: .video) }
        catch { throw SphereFailure.unsupported("AVFoundation cannot open source: \(error)") }
        var selected: [AVAssetTrack] = []
        for id in request.trackIDs {
            guard let track = all.first(where: { $0.trackID == id }) else { throw SphereFailure.unsupported("MP4 video track ID \(id) missing") }
            let formats = try await track.load(.formatDescriptions)
            guard formats.count == 1 else { throw SphereFailure.unsupported("changing format descriptions") }
            let f = formats[0]
            let codec = CMFormatDescriptionGetMediaSubType(f)
            guard [kCMVideoCodecType_HEVC, kCMVideoCodecType_H264].contains(codec), VTIsHardwareDecodeSupported(codec)
            else { throw SphereFailure.unsupported("source codec lacks VideoToolbox hardware decode") }
            let ext = (CMFormatDescriptionGetExtensions(f) as NSDictionary?) ?? [:]
            if let bits = ext["BitsPerComponent"] as? Int, bits != 8 { throw SphereFailure.unsupported("only 8-bit planar SDR sources") }
            if let full = ext[kCMFormatDescriptionExtension_FullRangeVideo] as? NSNumber,
               full.boolValue != request.fullRange {
                throw SphereFailure.invalid("fullRange disagrees with source track metadata")
            }
            for (key, expected) in [(kCMFormatDescriptionExtension_ColorPrimaries, "ITU_R_709_2"),
                                    (kCMFormatDescriptionExtension_TransferFunction, "ITU_R_709_2"),
                                    (kCMFormatDescriptionExtension_YCbCrMatrix, "ITU_R_709_2")] {
                if let value = ext[key] as? String, value != expected { throw SphereFailure.unsupported("non-BT.709 source \(key)=\(value)") }
            }
            let transform = try await track.load(.preferredTransform)
            let range = try await track.load(.timeRange)
            guard transform.isIdentity, abs(range.start.seconds) < 1e-9,
                  request.sourceIn < range.end.seconds, request.sourceOut <= range.end.seconds + .init(1.0/Double(request.fps))
            else { throw SphereFailure.unsupported("track transform, nonzero track origin or interval beyond source") }
            selected.append(track)
        }
        tracks = selected
        if request.sourceProjection == "gopro-eac" {
            let sizes = try await selected.asyncDimensions()
            let w = Int(sizes[0].width), h = Int(sizes[0].height), o = w - 3*h
            // Chroma crops in _track_chain must be integral and identical across tracks.
            guard sizes[0] == sizes[1], o > 0, o <= h/4, o % 8 == 0, h % 4 == 0
            else { throw SphereFailure.unsupported("GoPro seam crops require matching dimensions and chroma-aligned overlap") }
        }
        guard let device = MTLCreateSystemDefaultDevice(), let queue = device.makeCommandQueue() else { throw SphereFailure.unsupported("Metal device unavailable") }
        self.device = device
        self.queue = queue
        let executable = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
        let library = try device.makeLibrary(URL: executable.deletingLastPathComponent().appendingPathComponent("sphere.metallib"))
        guard let a = library.makeFunction(name: "assemble"), let r = library.makeFunction(name: "reproject") else { throw SphereFailure.runtime("incompatible sphere.metallib") }
        assemble = try await device.makeComputePipelineState(function: a)
        reproject = try await device.makeComputePipelineState(function: r)
        var cache: CVMetalTextureCache?
        guard CVMetalTextureCacheCreate(nil, nil, device, nil, &cache) == kCVReturnSuccess, let cache else { throw SphereFailure.runtime("CVMetalTextureCache creation") }
        self.cache = cache
    }

    private func texture(_ buffer: CVPixelBuffer, plane: Int) throws -> (CVMetalTexture, MTLTexture) {
        var cv: CVMetalTexture?
        let status = CVMetalTextureCacheCreateTextureFromImage(nil, cache, buffer, nil,
            plane == 0 ? .r8Unorm : .rg8Unorm, CVPixelBufferGetWidthOfPlane(buffer, plane),
            CVPixelBufferGetHeightOfPlane(buffer, plane), plane, &cv)
        guard status == kCVReturnSuccess, let cv, let tex = CVMetalTextureGetTexture(cv) else { throw SphereFailure.runtime("IOSurface plane \(plane): \(status)") }
        return (cv, tex)
    }

    private func dispatch(_ encoder: MTLComputeCommandEncoder, _ pipeline: MTLComputePipelineState, _ texture: MTLTexture) {
        encoder.setComputePipelineState(pipeline)
        let w = pipeline.threadExecutionWidth
        encoder.dispatchThreads(MTLSize(width: texture.width, height: texture.height, depth: 1),
                                threadsPerThreadgroup: MTLSize(width: w, height: min(8, pipeline.maxTotalThreadsPerThreadgroup / w), depth: 1))
    }

    func render() throws -> [String: Any] {
        let r = request, started = Date()
        let destination = URL(fileURLWithPath: r.destination)
        let partial = destination.deletingLastPathComponent().appendingPathComponent(".sphere-\(UUID().uuidString).mp4")
        defer { try? FileManager.default.removeItem(at: partial) }
        let reader = try AVAssetReader(asset: asset)
        // Reader clips its first sample PTS to timeRange.start, even when that
        // image precedes the trim. Seek earlier and discard by ORIGINAL PTS so
        // vf_fps round=near uses the same first retained frame as ffmpeg -ss.
        reader.timeRange = CMTimeRange(start: CMTime(seconds: max(0, r.sourceIn - 1), preferredTimescale: 600_000_000),
                                      end: CMTime(seconds: r.sourceOut, preferredTimescale: 600_000_000))
        let decoders = try tracks.map { try TrackFrames(track: $0, reader: reader, request: r) }
        let writer = try AVAssetWriter(outputURL: partial, fileType: .mp4)
        let settings: [String: Any] = [
            AVVideoCodecKey: r.codec == "hevc" ? AVVideoCodecType.hevc : AVVideoCodecType.h264,
            AVVideoWidthKey: r.width, AVVideoHeightKey: r.height,
            AVVideoEncoderSpecificationKey: [kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder as String: true],
            AVVideoCompressionPropertiesKey: [AVVideoAverageBitRateKey: max(20_000_000, r.width*r.height*r.fps/2),
                AVVideoMaxKeyFrameIntervalKey: r.fps, AVVideoAllowFrameReorderingKey: false,
                AVVideoExpectedSourceFrameRateKey: r.fps],
            AVVideoColorPropertiesKey: [AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2, AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2],
        ]
        guard writer.canApply(outputSettings: settings, forMediaType: .video) else { throw SphereFailure.unsupported("hardware encoder settings") }
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        input.expectsMediaDataInRealTime = false
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
            kCVPixelBufferWidthKey as String: r.width, kCVPixelBufferHeightKey as String: r.height,
            kCVPixelBufferMetalCompatibilityKey as String: true, kCVPixelBufferIOSurfacePropertiesKey as String: [:],
        ])
        guard writer.canAdd(input) else { throw SphereFailure.runtime("writer input refused") }
        writer.add(input)
        guard reader.startReading(), writer.startWriting() else { throw SphereFailure.runtime("start reader/writer: \(String(describing: reader.error)) \(String(describing: writer.error))") }
        defer { reader.cancelReading(); if writer.status == .writing { writer.cancelWriting() } }
        writer.startSession(atSourceTime: .zero)
        guard let pool = adaptor.pixelBufferPool else { throw SphereFailure.runtime("writer IOSurface pool unavailable") }
        let keys = r.keys.map(\.key)
        var atlas: [MTLTexture] = []
        var firstPTS = 0.0, lastPTS = 0.0
        let poolOptions = [kCVPixelBufferPoolAllocationThresholdKey as String: 8] as CFDictionary
        var retained: [CVMetalTexture] = []
        retained.reserveCapacity(6)
        var assembledPTS = CMTime.invalid
        for n in 0..<r.frames {
            try autoreleasepool {
                let sample = try decoders[0].sample(frame: n)
                let otherSample = decoders.count == 2 ? try decoders[1].sample(frame: n) : nil
                let pts = sample.presentationTimeStamp
                if let otherSample, abs((otherSample.presentationTimeStamp - pts).seconds) >= 1e-8 {
                    throw SphereFailure.runtime("GoPro track PTS mismatch")
                }
                if n == 0 { firstPTS = pts.seconds }
                lastPTS = pts.seconds
                let buffer = CMSampleBufferGetImageBuffer(sample)!
                let otherBuffer = otherSample.flatMap { CMSampleBufferGetImageBuffer($0) }
                func isSurface(_ b: CVPixelBuffer) -> Bool { CVPixelBufferGetPlaneCount(b) == 2 && CVPixelBufferGetIOSurface(b) != nil }
                guard isSurface(buffer), otherBuffer.map(isSurface) ?? true else { throw SphereFailure.runtime("decoder did not return two-plane IOSurfaces") }
                defer { retained.removeAll(keepingCapacity: true) }
                var output: CVPixelBuffer?
                let allocationDeadline = Date().addingTimeInterval(30)
                var status: CVReturn
                repeat {
                    status = CVPixelBufferPoolCreatePixelBufferWithAuxAttributes(nil, pool, poolOptions, &output)
                    if status == kCVReturnWouldExceedAllocationThreshold { Thread.sleep(forTimeInterval: 0.001) }
                } while status == kCVReturnWouldExceedAllocationThreshold && writer.status == .writing && Date() < allocationDeadline
                guard status == kCVReturnSuccess, let output else { throw SphereFailure.runtime("bounded writer pool at frame \(n): \(status), \(String(describing: writer.error))") }
                guard let command = queue.makeCommandBuffer(), let enc = command.makeComputeCommandEncoder() else { throw SphereFailure.runtime("Metal command creation") }
                let (matrix, tanHalf) = r.matrix(frame: n, keys: keys)
                let floatMatrix = simd_float3x3(columns: (SIMD3<Float>(matrix.columns.0), SIMD3<Float>(matrix.columns.1), SIMD3<Float>(matrix.columns.2)))
                var uniforms = Uniforms(matrix: floatMatrix, tanHalf: Float(tanHalf), aspect: Float(r.height)/Float(r.width),
                                        eac: r.sourceProjection == "gopro-eac" ? 1 : 0, planet: r.mode == .tinyPlanet ? 1 : 0, fullRange: r.fullRange ? 1 : 0)
                for plane in 0...1 {
                    let src = try texture(buffer, plane: plane)
                    let dst = try texture(output, plane: plane)
                    retained.append(src.0); retained.append(dst.0)
                    var sourceTexture = src.1
                    if r.sourceProjection == "gopro-eac" {
                        let other = try texture(otherBuffer!, plane: plane)
                        retained.append(other.0)
                        if atlas.count <= plane {
                            let desc = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: src.1.pixelFormat, width: src.1.height*3, height: src.1.height*2, mipmapped: false)
                            desc.storageMode = .private; desc.usage = [.shaderRead, .shaderWrite]
                            guard let tex = device.makeTexture(descriptor: desc) else { throw SphereFailure.runtime("atlas allocation") }
                            atlas.append(tex)
                        }
                        sourceTexture = atlas[plane]
                        if assembledPTS != pts {
                            enc.setTexture(src.1, index: 0); enc.setTexture(other.1, index: 1); enc.setTexture(sourceTexture, index: 2)
                            dispatch(enc, assemble, sourceTexture)
                            enc.memoryBarrier(scope: .textures)
                        }
                    }
                    enc.setTexture(sourceTexture, index: 0); enc.setTexture(dst.1, index: 1)
                    enc.setBytes(&uniforms, length: MemoryLayout<Uniforms>.stride, index: 0)
                    var chroma = Int32(plane)
                    enc.setBytes(&chroma, length: MemoryLayout<Int32>.size, index: 1)
                    dispatch(enc, reproject, dst.1)
                }
                assembledPTS = pts
                enc.endEncoding(); command.commit(); command.waitUntilCompleted()
                withExtendedLifetime(retained) {}
                guard command.status == .completed else { throw SphereFailure.runtime("Metal: \(String(describing: command.error))") }
                CVBufferSetAttachment(output, kCVImageBufferColorPrimariesKey, kCVImageBufferColorPrimaries_ITU_R_709_2, .shouldPropagate)
                CVBufferSetAttachment(output, kCVImageBufferTransferFunctionKey, kCVImageBufferTransferFunction_ITU_R_709_2, .shouldPropagate)
                CVBufferSetAttachment(output, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
                let deadline = Date().addingTimeInterval(60)
                while !input.isReadyForMoreMediaData && writer.status == .writing && Date() < deadline { Thread.sleep(forTimeInterval: 0.001) }
                guard input.isReadyForMoreMediaData, adaptor.append(output, withPresentationTime: CMTime(value: Int64(n), timescale: Int32(r.fps))) else { throw SphereFailure.runtime("encode frame \(n): \(String(describing: writer.error))") }
            }
        }
        input.markAsFinished()
        writer.endSession(atSourceTime: CMTime(value: Int64(r.frames), timescale: Int32(r.fps)))
        let done = DispatchSemaphore(value: 0)
        writer.finishWriting { done.signal() }
        guard done.wait(timeout: .now()+120) == .success, writer.status == .completed else { throw SphereFailure.runtime("finish encode: \(String(describing: writer.error))") }
        // No replace: a racing consumer's existing output is never overwritten.
        try FileManager.default.moveItem(at: partial, to: destination)
        return ["renderer": "metal", "encoder": "\(r.codec)_videotoolbox", "frames": r.frames,
                "fps": r.fps, "width": r.width, "height": r.height,
                "firstSourcePTS": firstPTS, "lastSourcePTS": lastPTS, "wallSeconds": Date().timeIntervalSince(started)]
    }
}

private extension Array where Element == AVAssetTrack {
    func asyncDimensions() async throws -> [CGSize] {
        var result: [CGSize] = []
        for track in self { result.append(try await track.load(.naturalSize)) }
        return result
    }
}
