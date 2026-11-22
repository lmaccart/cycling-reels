import AppKit
import CoreGraphics
import CoreVideo
import Metal
import MetalKit
import SwiftUI
import os
import simd

// The aim viewer's GPU path. The decoded video frame(s) (or an uploaded still) are Metal textures; an MTKView draws the flat view
// with a fragment pass (Playback.metal) reading the CURRENT view at draw time. Mouse events only store a value and ask for a redraw:
// no Task per event, no CGImage, no SwiftUI image swap. A CGImage exists only for "Check frame" and tests (`readback`).

/// Source textures for one moment: two EAC track pictures (or one levelled equirect twice) plus how to read them.
final class FrameTextures: @unchecked Sendable {
    let a: MTLTexture, b: MTLTexture
    let eac: (face: Int, overlap: Int)?
    let level: simd_double3x3
    let time: Double
    private let keep: [AnyObject]   // CVMetalTextures / pixel buffers the textures alias

    init(a: MTLTexture, b: MTLTexture, eac: (face: Int, overlap: Int)?, level: simd_double3x3, time: Double, keep: [AnyObject] = []) {
        self.a = a; self.b = b; self.eac = eac; self.level = level; self.time = time; self.keep = keep
    }

    /// Wraps decoded pixel buffers without copying (CVMetalTextureCache).
    static func make(buffers: [CVPixelBuffer], cache: CVMetalTextureCache, eac: (face: Int, overlap: Int)?, level: simd_double3x3, time: Double) -> FrameTextures? {
        var tex: [MTLTexture] = [], refs: [AnyObject] = []
        for buf in buffers {
            var ref: CVMetalTexture?
            CVMetalTextureCacheCreateTextureFromImage(nil, cache, buf, nil, .bgra8Unorm, CVPixelBufferGetWidth(buf), CVPixelBufferGetHeight(buf), 0, &ref)
            guard let ref, let t = CVMetalTextureGetTexture(ref) else { return nil }
            tex.append(t); refs.append(ref); refs.append(buf)
        }
        guard let first = tex.first, let last = tex.last else { return nil }
        return FrameTextures(a: first, b: last, eac: eac, level: level, time: time, keep: refs)
    }

    /// A levelled equirect still (backend frame) uploaded once; sampled in equirect mode.
    static func still(_ image: CGImage, device: MTLDevice, time: Double) -> FrameTextures? {
        let w = image.width, h = image.height
        var px = [UInt8](repeating: 0, count: w * h * 4)
        let ok = px.withUnsafeMutableBytes { p -> Bool in
            guard let ctx = CGContext(data: p.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                      bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue) else { return false }
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
            return true
        }
        let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: w, height: h, mipmapped: false)
        guard ok, let t = device.makeTexture(descriptor: d) else { return nil }
        t.replace(region: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0, withBytes: px, bytesPerRow: w * 4)
        return FrameTextures(a: t, b: t, eac: nil, level: matrix_identity_double3x3, time: time)
    }
}

/// Counters for the perf test and for confirming that work never piles up.
struct ViewerStats: Sendable {
    var draws = 0, skipped = 0, inFlight = 0, maxInFlight = 0
    var gpuSeconds = 0.0
}

private struct PlayUniforms {
    var m: simd_float3x3
    var tanHalf: Float, aspect: Float
    var eac: Int32, face: Int32, overlap: Int32
}

/// Metal state shared by the MTKView and the readback; at most `maxFrames` command buffers are ever in flight.
final class ViewerRenderer: @unchecked Sendable {
    static let maxFrames = 2
    static let shared: ViewerRenderer? = ViewerRenderer()

    let device: MTLDevice
    private let queue: MTLCommandQueue
    private let pipeline: MTLRenderPipelineState
    private let lock = OSAllocatedUnfairLock(initialState: ViewerStats())

    init?() {
        guard let d = MTLCreateSystemDefaultDevice(), let q = d.makeCommandQueue(), let lib = d.makeDefaultLibrary(),
              let v = lib.makeFunction(name: "viewVert"), let f = lib.makeFunction(name: "viewFrag") else { return nil }
        let desc = MTLRenderPipelineDescriptor()
        desc.vertexFunction = v; desc.fragmentFunction = f
        desc.colorAttachments[0].pixelFormat = .bgra8Unorm
        guard let p = try? d.makeRenderPipelineState(descriptor: desc) else { return nil }
        device = d; queue = q; pipeline = p
    }

    var stats: ViewerStats { lock.withLock { $0 } }
    func resetStats() { lock.withLock { $0 = ViewerStats() } }

    /// The largest rect of aspect h/w that fits in a drawable, centred (the rest stays black).
    static func letterbox(drawable dw: Double, _ dh: Double, aspect: Double) -> MTLViewport {
        let fitsWidth = dw * aspect <= dh
        let vw = fitsWidth ? dw : dh / aspect, vh = fitsWidth ? dw * aspect : dh
        return MTLViewport(originX: (dw - vw) / 2, originY: (dh - vh) / 2, width: vw, height: vh, znear: 0, zfar: 1)
    }

    private func uniforms(_ f: FrameTextures, _ view: SphereView, aspect: Double) -> PlayUniforms {
        let m = f.level * SphereMath.matrix(for: view)
        return PlayUniforms(m: simd_float3x3(SIMD3<Float>(m[0]), SIMD3<Float>(m[1]), SIMD3<Float>(m[2])), tanHalf: Float(tan(view.fov * .pi / 360)), aspect: Float(aspect),
                            eac: f.eac == nil ? 0 : 1, face: Int32(f.eac?.face ?? 0), overlap: Int32(f.eac?.overlap ?? 0))
    }

    /// Encodes and commits one draw into `pass` (the viewport is letterboxed for `aspect` = output h/w). Returns false, drawing nothing, while
    /// `maxFrames` earlier draws are still on the GPU; `done` runs when this one finishes (also false-return: not called).
    @discardableResult
    func draw(pass: MTLRenderPassDescriptor, size: (w: Int, h: Int), drawable: MTLDrawable?, frame: FrameTextures?, view: SphereView, aspect: Double,
              done: (@Sendable () -> Void)? = nil) -> Bool {
        let admitted = lock.withLock { s -> Bool in
            if s.inFlight >= Self.maxFrames { s.skipped += 1; return false }
            s.inFlight += 1; s.draws += 1; s.maxInFlight = max(s.maxInFlight, s.inFlight); return true
        }
        guard admitted else { return false }
        guard let cmd = queue.makeCommandBuffer(), let enc = cmd.makeRenderCommandEncoder(descriptor: pass) else {
            lock.withLock { $0.inFlight -= 1 }; return false
        }
        if let frame {
            enc.setViewport(Self.letterbox(drawable: Double(size.w), Double(size.h), aspect: aspect))
            enc.setRenderPipelineState(pipeline)
            enc.setFragmentTexture(frame.a, index: 0); enc.setFragmentTexture(frame.b, index: 1)
            var u = uniforms(frame, view, aspect: aspect)
            enc.setFragmentBytes(&u, length: MemoryLayout<PlayUniforms>.stride, index: 0)
            enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
        }
        enc.endEncoding()
        if let drawable { cmd.present(drawable) }
        cmd.addCompletedHandler { [lock] cb in
            lock.withLock { $0.inFlight -= 1; $0.gpuSeconds += max(0, cb.gpuEndTime - cb.gpuStartTime) }
            _ = frame   // the textures live until the GPU is done with them
            done?()
        }
        cmd.commit()
        return true
    }

    func offscreen(width w: Int, height h: Int) -> (MTLTexture, MTLRenderPassDescriptor)? {
        let d = MTLTextureDescriptor.texture2DDescriptor(pixelFormat: .bgra8Unorm, width: w, height: h, mipmapped: false)
        d.usage = [.renderTarget]; d.storageMode = .shared
        guard let t = device.makeTexture(descriptor: d) else { return nil }
        let pass = MTLRenderPassDescriptor()
        pass.colorAttachments[0].texture = t; pass.colorAttachments[0].loadAction = .clear; pass.colorAttachments[0].storeAction = .store
        pass.colorAttachments[0].clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 1)
        return (t, pass)
    }

    /// One-off CPU copy of a view at exactly w x h (Check frame and tests only; the interactive path never does this).
    func readback(_ frame: FrameTextures, view: SphereView, width w: Int, height h: Int) -> CGImage? {
        guard let (tex, pass) = offscreen(width: w, height: h) else { return nil }
        let sem = DispatchSemaphore(value: 0)
        while !draw(pass: pass, size: (w, h), drawable: nil, frame: frame, view: view, aspect: Double(h) / Double(w), done: { sem.signal() }) { usleep(500) }
        sem.wait()
        var px = [UInt8](repeating: 0, count: w * h * 4)
        tex.getBytes(&px, bytesPerRow: w * 4, from: MTLRegionMake2D(0, 0, w, h), mipmapLevel: 0)
        guard let provider = CGDataProvider(data: Data(px) as CFData) else { return nil }
        return CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                       bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
                       provider: provider, decode: nil, shouldInterpolate: true, intent: .defaultIntent)
    }
}

/// What the MTKView draws. Plain values: the model writes them, `draw` reads them.
@MainActor
final class ViewerState {
    let renderer: ViewerRenderer?
    var frame: FrameTextures? { didSet { setNeedsDraw() } }
    var view = ReframeSettings.straight { didSet { setNeedsDraw() } }
    var aspect = 16.0 / 9 { didSet { setNeedsDraw() } }
    /// Display-link paced redraws (while playing); otherwise a draw happens only after a change.
    var continuous = false { didSet { apply() } }
    private weak var mtk: MTKView?
    private var wantsDraw = false

    init(renderer: ViewerRenderer? = .shared) { self.renderer = renderer }

    func attach(_ v: MTKView) { mtk = v; apply() }
    private func apply() { mtk?.isPaused = !continuous; mtk?.enableSetNeedsDisplay = !continuous; setNeedsDraw() }

    func setNeedsDraw() { if !continuous { mtk?.setNeedsDisplay(mtk?.bounds ?? .zero) } }

    /// MTKView delegate entry: draws the newest state (skipped while the GPU is two frames behind, then retried when it catches up).
    func draw(in v: MTKView) {
        guard let renderer, let pass = v.currentRenderPassDescriptor, let drawable = v.currentDrawable else { return }
        pass.colorAttachments[0].clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 1)
        let size = (w: Int(v.drawableSize.width), h: Int(v.drawableSize.height))
        guard size.w > 0, size.h > 0 else { return }
        let ok = renderer.draw(pass: pass, size: size, drawable: drawable, frame: frame, view: view, aspect: aspect,
                               done: { [weak self] in DispatchQueue.main.async { MainActor.assumeIsolated { self?.retryIfBehind() } } })
        if !ok { wantsDraw = true }
    }

    private func retryIfBehind() { if wantsDraw { wantsDraw = false; setNeedsDraw() } }

    /// The preview as a CGImage, for Check frame.
    func snapshot(width: Int, height: Int) -> CGImage? {
        guard let frame, let renderer else { return nil }
        return renderer.readback(frame, view: view, width: width, height: height)
    }
}

/// The preview pane: an MTKView that also turns mouse drag, scroll and pinch into aim changes.
struct ViewerSurface: NSViewRepresentable {
    let state: ViewerState
    var onDrag: (Double, Double) -> Void
    var onZoom: (Double) -> Void

    func makeNSView(context: Context) -> ViewerMTKView {
        let v = ViewerMTKView(frame: .zero, device: state.renderer?.device)
        v.state = state
        v.delegate = context.coordinator
        v.colorPixelFormat = .bgra8Unorm
        v.framebufferOnly = true
        v.autoResizeDrawable = false
        v.clearColor = MTLClearColor(red: 0, green: 0, blue: 0, alpha: 1)
        v.preferredFramesPerSecond = 120
        state.attach(v)
        return v
    }

    func updateNSView(_ v: ViewerMTKView, context: Context) { v.onDrag = onDrag; v.onZoom = onZoom; context.coordinator.state = state }
    func makeCoordinator() -> Coordinator { Coordinator(state: state) }

    @MainActor final class Coordinator: NSObject, MTKViewDelegate {
        var state: ViewerState
        init(state: ViewerState) { self.state = state }
        nonisolated func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}
        nonisolated func draw(in view: MTKView) { MainActor.assumeIsolated { state.draw(in: view) } }
    }

    final class ViewerMTKView: MTKView {
        var state: ViewerState?
        var onDrag: (Double, Double) -> Void = { _, _ in }
        var onZoom: (Double) -> Void = { _ in }
        static let maxSide: CGFloat = 1600   // backing-size cap: plenty for a preview, keeps the fragment pass cheap

        override func layout() {
            super.layout()
            let scale = window?.backingScaleFactor ?? 2
            var w = bounds.width * scale, h = bounds.height * scale
            let k = min(1, Self.maxSide / max(1, max(w, h)))
            w = max(1, (w * k).rounded()); h = max(1, (h * k).rounded())
            if drawableSize.width != w || drawableSize.height != h { drawableSize = CGSize(width: w, height: h); state?.setNeedsDraw() }
        }

        override func mouseDragged(with e: NSEvent) { onDrag(e.deltaX, e.deltaY) }
        override func scrollWheel(with e: NSEvent) { onZoom(e.scrollingDeltaY * 0.004) }
        override func magnify(with e: NSEvent) { onZoom(e.magnification) }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }
    }
}
