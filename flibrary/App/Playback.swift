import AVFoundation
import CoreGraphics
import Metal
import simd

// Live playback for the aim viewer: frames come from the .360's two EAC tracks (or a full-length equirect export)
// via AVAssetReader, are reprojected on the GPU (Playback.metal) into the flat preview, with the audio on an AVPlayer clock.
// No proxy or intermediate file is ever written.

/// Pure time and layout maths (unit tested).
enum PlaybackMath {
    /// Playing from the out point (or past it) starts again at `in`: playback stops at out, it does not loop.
    static func startTime(_ t: Double, in start: Double, out: Double) -> Double { t >= out - 0.05 ? start : max(0, t) }
    static func isFinished(_ t: Double, out: Double) -> Bool { t >= out }
    /// Exports made with a custom in/out start mid-clip; only a full-length one maps 1:1 onto the clip's time.
    static func coversClip(exportDuration: Double, clipDuration: Double) -> Bool { abs(exportDuration - clipDuration) < 0.5 }

    /// GoPro EAC face (0..5: top row L/F/R, bottom D/B/U) and face-local u, v in 0...1 for a ray in the camera sphere (x right, y down, z forward).
    static func eacFace(_ d: simd_double3) -> (face: Int, u: Double, v: Double) {
        let a = simd_abs(d)
        let face: Int, s: SIMD2<Double>
        if a.y > max(a.x, a.z) { face = d.y > 0 ? 3 : 5; s = SIMD2(-d.z / d.y, (d.y > 0 ? -d.x : d.x) / d.y) }
        else if a.x > a.z { face = d.x > 0 ? 2 : 0; s = SIMD2(-d.z / d.x, (d.x > 0 ? d.y : -d.y) / d.x) }
        else if d.z > 0 { face = 1; s = SIMD2(d.x / d.z, d.y / d.z) }
        else { face = 4; s = SIMD2(d.y / d.z, d.x / d.z) }
        return (face, 2 / .pi * atan(s.x) + 0.5, 2 / .pi * atan(s.y) + 0.5)
    }

    /// Pixel index inside the face-sized (h x h) track picture, with v360's 2 px EAC border.
    static func eacPixel(u: Double, v: Double, face: Int, h: Int) -> (x: Double, y: Double) {
        (((u + Double(face % 3)) * Double(3 * h - 4) / 3 + 1.5) - Double(face % 3 * h), v * Double(h - 4) + 1.5)
    }

    /// Raw track column of a side or middle face pixel ignoring the seam blend (`o` = overlap band width).
    static func rawColumn(col: Int, x: Double, h: Int, overlap o: Int) -> Double {
        let g = o / 2, cs = h - g, p = (cs - o) / 2
        if col == 1 { return Double(h + g) + x }
        let q = (x + 0.5) * Double(cs) / Double(h) - 0.5
        let dx = col == 0 ? 0 : 2 * h + g
        return Double(dx) + q + (q >= Double(p + o) ? Double(o) : 0)
    }
}

private struct PlayUniforms {
    var m: simd_float3x3
    var tanHalf: Float, aspect: Float
    var eac: Int32, face: Int32, overlap: Int32
}

/// Decodes the playback source in order and renders flat views of the held frame.
actor PlaybackEngine {
    private let asset: AVURLAsset
    private let tracks: [AVAssetTrack]
    private let eac: (face: Int, overlap: Int)?
    private let level: simd_double3x3
    private let device = MTLCreateSystemDefaultDevice()!
    private let queue: MTLCommandQueue
    private let pipeline: MTLComputePipelineState
    private var cache: CVMetalTextureCache?
    private var reader: AVAssetReader?
    private var outputs: [AVAssetReaderTrackOutput] = []
    private var held: [CVPixelBuffer] = []
    private var heldTime: Double?
    private var pending: (time: Double, bufs: [CVPixelBuffer])?
    private var ended = false

    /// `export`: a full-length levelled equirect (used when its duration matches); otherwise the .360 with `sphere` (tracks/face/overlap).
    /// Returns nil when neither can be opened.
    static func make(asset a: Asset, export: URL?, level: SphereView?) async -> PlaybackEngine? {
        func open(_ url: URL) -> AVURLAsset { AVURLAsset(url: url, options: [AVURLAssetOverrideMIMETypeKey: "video/mp4"]) }
        if let export, let ex = Optional(open(export)), let d = try? await ex.load(.duration).seconds,
           PlaybackMath.coversClip(exportDuration: d, clipDuration: a.duration ?? 0),
           let v = try? await ex.loadTracks(withMediaType: .video).first {
            return PlaybackEngine(asset: ex, tracks: [v], eac: nil, level: matrix_identity_double3x3)
        }
        let src = open(a.mediaURL)
        guard a.projection == "gopro-eac", let v = try? await src.loadTracks(withMediaType: .video), v.count >= 2,
              let size = try? await v[0].load(.naturalSize) else { return nil }
        let face = Int(size.height), overlap = Int(size.width) - 3 * face
        let l = level.map { SphereMath.v360Matrix(yaw: $0.yaw, pitch: $0.pitch, roll: $0.roll) } ?? matrix_identity_double3x3
        return PlaybackEngine(asset: src, tracks: Array(v.prefix(2)), eac: (face, overlap), level: l)
    }

    private init?(asset: AVURLAsset, tracks: [AVAssetTrack], eac: (face: Int, overlap: Int)?, level: simd_double3x3) {
        self.asset = asset; self.tracks = tracks; self.eac = eac; self.level = level
        guard let q = device.makeCommandQueue(), let lib = device.makeDefaultLibrary(), let f = lib.makeFunction(name: "playView"),
              let p = try? device.makeComputePipelineState(function: f) else { return nil }
        queue = q; pipeline = p
        CVMetalTextureCacheCreate(nil, nil, device, nil, &cache)
    }

    /// Whether the held frame is the one for time `t` (paused aim changes can redraw it without decoding).
    func holds(_ t: Double) -> Bool { heldTime.map { abs($0 - t) < 0.1 } ?? false }

    private func start(at t: Double) throws {
        reader?.cancelReading()
        let r = try AVAssetReader(asset: asset)
        let settings: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                                       kCVPixelBufferMetalCompatibilityKey as String: true]
        outputs = tracks.map { AVAssetReaderTrackOutput(track: $0, outputSettings: settings) }
        for o in outputs { o.alwaysCopiesSampleData = false; r.add(o) }
        r.timeRange = CMTimeRange(start: CMTime(seconds: t, preferredTimescale: 600), duration: .positiveInfinity)
        guard r.startReading() else { throw r.error ?? BackendError(message: "Could not start reading") }
        reader = r; held = []; heldTime = nil; pending = nil; ended = false
    }

    private func readNext() -> (Double, [CVPixelBuffer])? {
        var bufs: [CVPixelBuffer] = [], time = 0.0
        for o in outputs {
            guard let s = o.copyNextSampleBuffer(), let b = CMSampleBufferGetImageBuffer(s) else { return nil }
            if bufs.isEmpty { time = CMSampleBufferGetPresentationTimeStamp(s).seconds }
            bufs.append(b)
        }
        return (time, bufs)
    }

    /// Advances (or restarts the reader for a jump) to the frame for clip time `t`; nil if there is none.
    private func advance(to t: Double) throws {
        let jump = heldTime.map { t < $0 - 0.25 || (t > $0 + 1.0 && !ended) } ?? false
        if reader == nil || jump { try start(at: t) }
        while !ended {
            if pending == nil { if let n = readNext() { pending = n } else { ended = true; break } }
            guard let p = pending else { break }
            if heldTime == nil || p.time <= t + 0.004 { held = p.bufs; heldTime = p.time; pending = nil } else { break }
        }
    }

    /// The flat view of the frame at clip time `t` as a w x h image. Frames are consumed in order, so calling this with
    /// increasing `t` plays; a far-away `t` restarts the reader there.
    func render(at t: Double, view: SphereView, width w: Int, height h: Int) throws -> CGImage? {
        try advance(to: t)
        guard !held.isEmpty, let cache else { return nil }
        func texture(_ b: CVPixelBuffer) -> MTLTexture? {
            var ref: CVMetalTexture?
            CVMetalTextureCacheCreateTextureFromImage(nil, cache, b, nil, .bgra8Unorm, CVPixelBufferGetWidth(b), CVPixelBufferGetHeight(b), 0, &ref)
            return ref.flatMap(CVMetalTextureGetTexture)
        }
        let tex = held.compactMap(texture)
        let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: w, height: h, mipmapped: false)
        d.usage = [.shaderWrite]; d.storageMode = .shared
        guard tex.count == held.count, let out = device.makeTexture(descriptor: d), let cmd = queue.makeCommandBuffer(), let enc = cmd.makeComputeCommandEncoder() else { return nil }
        let m = level * SphereMath.matrix(for: view)
        var u = PlayUniforms(m: simd_float3x3(SIMD3<Float>(m[0]), SIMD3<Float>(m[1]), SIMD3<Float>(m[2])), tanHalf: Float(tan(view.fov * .pi / 360)), aspect: Float(h) / Float(w),
                             eac: eac == nil ? 0 : 1, face: Int32(eac?.face ?? 0), overlap: Int32(eac?.overlap ?? 0))
        enc.setComputePipelineState(pipeline)
        enc.setTexture(tex[0], index: 0); enc.setTexture(tex.last!, index: 1); enc.setTexture(out, index: 2)
        enc.setBytes(&u, length: MemoryLayout<PlayUniforms>.stride, index: 0)
        enc.dispatchThreads(MTLSize(width: w, height: h, depth: 1), threadsPerThreadgroup: MTLSize(width: 16, height: 16, depth: 1))
        enc.endEncoding(); cmd.commit(); cmd.waitUntilCompleted()
        var px = [UInt8](repeating: 0, count: w * h * 4)
        out.getBytes(&px, bytesPerRow: w * 4, from: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0)
        guard let provider = CGDataProvider(data: Data(px) as CFData) else { return nil }
        return CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
    }
}
