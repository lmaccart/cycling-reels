import AVFoundation
import CoreGraphics
import Metal
import simd

// Live playback for the aim viewer: frames come from the .360's two EAC tracks (or a full-length equirect export)
// via AVAssetReader, are wrapped as Metal textures (no copy) for the viewer's fragment pass (Viewer.swift, Playback.metal), with the audio on an AVPlayer clock.
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

/// Decodes the playback source in order and hands out the newest frame as textures. It never renders: drawing is the viewer's job.
actor PlaybackEngine {
    private let asset: AVURLAsset
    private let tracks: [AVAssetTrack]
    private let eac: (face: Int, overlap: Int)?
    private let level: simd_double3x3
    private let renderer: ViewerRenderer
    private var cache: CVMetalTextureCache?
    private var reader: AVAssetReader?
    private var outputs: [AVAssetReaderTrackOutput] = []
    private var held: [CVPixelBuffer] = []
    private var heldTime: Double?
    private var heldFrame: FrameTextures?
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
        guard let r = ViewerRenderer.shared else { return nil }
        self.asset = asset; self.tracks = tracks; self.eac = eac; self.level = level; renderer = r
        CVMetalTextureCacheCreate(nil, nil, r.device, nil, &cache)
    }

    private func start(at t: Double) throws {
        reader?.cancelReading()
        let r = try AVAssetReader(asset: asset)
        let settings: [String: Any] = [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
                                       kCVPixelBufferMetalCompatibilityKey as String: true]
        outputs = tracks.map { AVAssetReaderTrackOutput(track: $0, outputSettings: settings) }
        for o in outputs { o.alwaysCopiesSampleData = false; r.add(o) }
        r.timeRange = CMTimeRange(start: CMTime(seconds: t, preferredTimescale: 600), duration: .positiveInfinity)
        guard r.startReading() else { throw r.error ?? BackendError(message: "Could not start reading") }
        reader = r; held = []; heldTime = nil; heldFrame = nil; pending = nil; ended = false
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

    /// The newest frame at or before clip time `t` as textures, nil if there is none. Frames are consumed in order, so calling this with
    /// increasing `t` plays (late frames are skipped, never queued); a far-away `t` restarts the reader there. Throws CancellationError
    /// when the caller was cancelled while waiting its turn (scrubbing: only the latest seek decodes).
    func frame(at t: Double) throws -> FrameTextures? {
        try Task.checkCancellation()
        let jump = heldTime.map { t < $0 - 0.25 || (t > $0 + 1.0 && !ended) } ?? false
        if reader == nil || jump { try start(at: t) }
        var changed = false
        while !ended {
            if pending == nil { if let n = readNext() { pending = n } else { ended = true; break } }
            guard let p = pending else { break }
            if heldTime == nil || p.time <= t + 0.004 { held = p.bufs; heldTime = p.time; pending = nil; changed = true } else { break }
        }
        if changed, let cache, let time = heldTime {
            CVMetalTextureCacheFlush(cache, 0)
            heldFrame = FrameTextures.make(buffers: held, cache: cache, eac: eac, level: level, time: time)
        }
        return heldFrame
    }

    /// CPU readback of the view at clip time `t` (tests / Check frame style comparisons only).
    func render(at t: Double, view: SphereView, width w: Int, height h: Int) throws -> CGImage? {
        guard let f = try frame(at: t) else { return nil }
        return renderer.readback(f, view: view, width: w, height: h)
    }
}
