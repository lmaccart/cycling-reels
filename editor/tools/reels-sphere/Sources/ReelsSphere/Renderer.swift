import AVFoundation
import CoreVideo
import Darwin
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
    var secondMatrix: simd_float3x3
    var secondTanHalf: Float
    var proxy: Int32
    var layout: Int32
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
    var lastSampleEnd = CMTime.invalid

    init(track: AVAssetTrack, reader: AVAssetReader, request: RenderRequest) throws {
        self.reader = reader
        self.request = request
        // Decoded AVAssetReader samples commonly omit duration. The asset's
        // validated track range covers sourceOut, which is the reader's EOF.
        lastSampleEnd = CMTime(seconds: request.sourceOut, preferredTimescale: 600_000_000)
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
            if request.isProxy && sample.duration.isNumeric && sample.duration > .zero {
                lastSampleEnd = sample.presentationTimeStamp + sample.duration
            }
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
            if request.isProxy { try request.validateProxyTiming(firstPTS: current.presentationTimeStamp) }
            next = try read()
        }
        func tick(_ sample: CMSampleBuffer) -> Int {
            request.isProxy ? proxyFPSTick(pts: sample.presentationTimeStamp, sourceIn: request.sourceIn) :
                fpsTick(pts: sample.presentationTimeStamp.seconds, first: firstPTS!, speed: request.speed, rate: request.rate)
        }
        while let n = next, tick(n) <= frame {
            current = n
            next = try read()
        }
        if request.isProxy && next == nil {
            let end = CMTimeMinimum(lastSampleEnd, CMTime(seconds: request.sourceOut, preferredTimescale: 600_000_000))
            guard request.frames == proxyFPSTick(pts: end, sourceIn: request.sourceIn)
            else { throw SphereFailure.unsupported("proxy source EOF does not match requested fps frame count; use ffmpeg") }
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
            let dimensions = CMVideoFormatDescriptionGetDimensions(f)
            if ext[kCMFormatDescriptionExtension_YCbCrMatrix] == nil && Int(dimensions.width) * Int(dimensions.height) <= 720 * 576 {
                throw SphereFailure.unsupported("untagged SD uses staging's BT.601 colour interpretation")
            }
            let transform = try await track.load(.preferredTransform)
            let range = try await track.load(.timeRange)
            guard transform.isIdentity, abs(range.start.seconds) < 1e-9,
                  request.sourceIn < range.end.seconds, request.sourceOut <= range.end.seconds + .init(1.0/Double(request.fps))
            else { throw SphereFailure.unsupported("track transform, nonzero track origin or interval beyond source") }
            if request.isProxy && request.sourceOut > range.end.seconds + 1e-9 {
                throw SphereFailure.unsupported("proxy trim exceeds track EOF; use ffmpeg")
            }
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

    /// No CPU lock, allocation or copy on the normal production path.
    private func writeProofSample(_ buffer: CVPixelBuffer, frame: Int) throws {
        guard CVPixelBufferLockBaseAddress(buffer, .readOnly) == kCVReturnSuccess
        else { throw SphereFailure.runtime("proof readback lock") }
        defer { CVPixelBufferUnlockBaseAddress(buffer, .readOnly) }
        let width = request.width, height = request.height
        var bytes = Data(count: width * height * 3 / 2)
        try bytes.withUnsafeMutableBytes { (target: UnsafeMutableRawBufferPointer) in
            for plane in 0...1 {
                guard let source = CVPixelBufferGetBaseAddressOfPlane(buffer, plane)
                else { throw SphereFailure.runtime("proof readback plane") }
                let stride = CVPixelBufferGetBytesPerRowOfPlane(buffer, plane)
                let rows = plane == 0 ? height : height / 2
                let offset = plane == 0 ? 0 : width * height
                for row in 0..<rows {
                    memcpy(target.baseAddress!.advanced(by: offset + row * width),
                           source.advanced(by: row * stride), width)
                }
            }
        }
        try bytes.write(to: request.proofSampleURL(frame: frame), options: .withoutOverwriting)
    }

    func render() throws -> [String: Any] {
        let r = request, started = Date()
        let destination = URL(fileURLWithPath: r.destination)
        // Match the owning proxy's cleanup namespace; interrupted native source
        // writers must not leave aim-prefixed files outside source cleanup.
        let prefix = r.isProxy && r.projection == "front-back" ? ".source-" : ".sphere-"
        let partial = destination.deletingLastPathComponent().appendingPathComponent("\(prefix)\(UUID().uuidString).mp4")
        let partialPath = partial.path
        defer { try? FileManager.default.removeItem(at: partial) }
        let reader = try AVAssetReader(asset: asset)
        // Reader clips its first sample PTS to timeRange.start, even when that
        // image precedes the trim. Seek earlier and discard by ORIGINAL PTS so
        // vf_fps round=near uses the same first retained frame as ffmpeg -ss.
        reader.timeRange = CMTimeRange(start: CMTime(seconds: max(0, r.sourceIn - 1), preferredTimescale: 600_000_000),
                                      end: CMTime(seconds: r.sourceOut, preferredTimescale: 600_000_000))
        let decoders = try tracks.map { try TrackFrames(track: $0, reader: reader, request: r) }
        let writer = try AVAssetWriter(outputURL: partial, fileType: .mp4)
        var compression: [String: Any] = [
            AVVideoAverageBitRateKey: r.isProxy ? 8_000_000 : (r.isEquirectRender ? Int(Double(r.width*r.height)*r.rate*0.15) : max(20_000_000, r.width*r.height*r.fps/2)),
            AVVideoMaxKeyFrameIntervalKey: r.isProxy ? 10 : r.fps,
            AVVideoAllowFrameReorderingKey: false, AVVideoExpectedSourceFrameRateKey: r.fps,
        ]
        if r.isProxy { compression[kVTCompressionPropertyKey_DataRateLimits as String] = [1_500_000, 1] }
        let settings: [String: Any] = [
            AVVideoCodecKey: r.codec == "hevc" ? AVVideoCodecType.hevc : AVVideoCodecType.h264,
            AVVideoWidthKey: r.width, AVVideoHeightKey: r.height,
            AVVideoEncoderSpecificationKey: [kVTVideoEncoderSpecification_RequireHardwareAcceleratedVideoEncoder as String: true],
            AVVideoCompressionPropertiesKey: compression,
            AVVideoColorPropertiesKey: [AVVideoColorPrimariesKey: AVVideoColorPrimaries_ITU_R_709_2,
                AVVideoTransferFunctionKey: AVVideoTransferFunction_ITU_R_709_2, AVVideoYCbCrMatrixKey: AVVideoYCbCrMatrix_ITU_R_709_2],
        ]
        guard writer.canApply(outputSettings: settings, forMediaType: .video) else { throw SphereFailure.unsupported("hardware encoder settings") }
        let input = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        input.expectsMediaDataInRealTime = false
        // The writer would otherwise snap NTSC frame times to its default 1/600 s track timescale.
        if r.fpsDivisor != nil { input.mediaTimeScale = Int32(r.fps * 1000) }
        let adaptor = AVAssetWriterInputPixelBufferAdaptor(assetWriterInput: input, sourcePixelBufferAttributes: [
            kCVPixelBufferPixelFormatTypeKey as String: r.isProxy && r.fullRange ?
                kCVPixelFormatType_420YpCbCr8BiPlanarFullRange : kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange,
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
        func floatMatrix(_ m: simd_double3x3) -> simd_float3x3 {
            simd_float3x3(columns: (SIMD3<Float>(m.columns.0), SIMD3<Float>(m.columns.1), SIMD3<Float>(m.columns.2)))
        }
        func uniforms(frame: Int) -> Uniforms {
            let first = r.matrix(frame: frame, keys: keys)
            let second = r.projection == "front-back" ? r.cameraMatrix(view: r.views![1].key.view) : first
            return Uniforms(matrix: floatMatrix(first.0), tanHalf: Float(first.1),
                            aspect: Float(r.height)/Float(r.viewWidth),
                            eac: r.sourceProjection == "gopro-eac" ? 1 : 0,
                            planet: r.mode == .tinyPlanet ? 1 : 0, fullRange: r.fullRange ? 1 : 0,
                            secondMatrix: floatMatrix(second.0), secondTanHalf: Float(second.1),
                            proxy: r.isProxy ? 1 : 0,
                            layout: r.projection == "equirect" ? 1 : (r.projection == "front-back" ? 2 : 0))
        }
        // Proxy aims never move: keep the W4 matrix/FOV conversion out of the frame loop.
        let proxyUniforms = r.isProxy ? uniforms(frame: 0) : nil
        func outputBytes() throws -> Int64 {
            var info = stat()
            guard lstat(partialPath, &info) == 0 else { throw SphereFailure.runtime("cannot stat encoded output: errno \(errno)") }
            return Int64(info.st_size)
        }
        func checkOutputLimit() throws {
            if let limit = r.maxOutputBytes, try outputBytes() > limit {
                throw SphereFailure.runtime("proxy exceeded maxOutputBytes=\(limit); refusing publication")
            }
        }
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
                var frameUniforms = proxyUniforms ?? uniforms(frame: n)
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
                    enc.setBytes(&frameUniforms, length: MemoryLayout<Uniforms>.stride, index: 0)
                    var chroma = Int32(plane)
                    enc.setBytes(&chroma, length: MemoryLayout<Int32>.size, index: 1)
                    dispatch(enc, reproject, dst.1)
                }
                assembledPTS = pts
                enc.endEncoding(); command.commit(); command.waitUntilCompleted()
                withExtendedLifetime(retained) {}
                guard command.status == .completed else { throw SphereFailure.runtime("Metal: \(String(describing: command.error))") }
                if let samples = r.proofSampleFrames, samples.contains(n) {
                    try writeProofSample(output, frame: n)
                }
                CVBufferSetAttachment(output, kCVImageBufferColorPrimariesKey, kCVImageBufferColorPrimaries_ITU_R_709_2, .shouldPropagate)
                CVBufferSetAttachment(output, kCVImageBufferTransferFunctionKey, kCVImageBufferTransferFunction_ITU_R_709_2, .shouldPropagate)
                CVBufferSetAttachment(output, kCVImageBufferYCbCrMatrixKey, kCVImageBufferYCbCrMatrix_ITU_R_709_2, .shouldPropagate)
                let deadline = Date().addingTimeInterval(60)
                while !input.isReadyForMoreMediaData && writer.status == .writing && Date() < deadline { Thread.sleep(forTimeInterval: 0.001) }
                guard input.isReadyForMoreMediaData, adaptor.append(output, withPresentationTime: r.outputTime(n)) else { throw SphereFailure.runtime("encode frame \(n): \(String(describing: writer.error))") }
                if r.isProxy { try checkOutputLimit() }
            }
        }
        input.markAsFinished()
        writer.endSession(atSourceTime: r.outputTime(r.frames))
        let done = DispatchSemaphore(value: 0)
        writer.finishWriting { done.signal() }
        guard done.wait(timeout: .now()+120) == .success, writer.status == .completed else { throw SphereFailure.runtime("finish encode: \(String(describing: writer.error))") }
        let bytes = try outputBytes()
        try checkOutputLimit()
        // No replace: a racing consumer's existing output is never overwritten.
        try FileManager.default.moveItem(at: partial, to: destination)
        return ["renderer": "metal", "encoder": "\(r.codec)_videotoolbox", "frames": r.frames,
                "fps": r.fps, "width": r.width, "height": r.height,
                "preset": r.presetName, "projection": r.projection, "bytes": bytes, "outputBytes": bytes,
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
