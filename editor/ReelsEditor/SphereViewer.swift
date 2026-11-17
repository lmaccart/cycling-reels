import SwiftUI
import AppKit
import AVFoundation
import MetalKit
import simd

// MARK: - Player model

/// Owns the AVPlayer for an equirectangular proxy MP4 and hands BGRA frames to the renderer.
@MainActor
final class SpherePlayerModel: ObservableObject {
    let player = AVPlayer()
    @Published private(set) var isPlaying = false
    /// Proxy-clock seconds, updated ~30 Hz.
    @Published private(set) var currentSeconds: Double = 0
    private(set) var url: URL?
    private var output: AVPlayerItemVideoOutput?
    private var lastBuffer: CVPixelBuffer?
    /// Proxy seconds of the frame `latestPixelBuffer` last returned — the time the picture
    /// really shows, so per-frame stabilization lines up with it exactly.
    private(set) var displayedSeconds: Double = 0
    private var timeObserver: Any?
    private var rateObserver: NSKeyValueObservation?

    var rate: Float {
        get { player.rate }
        set { player.rate = newValue }
    }

    init() {
        player.actionAtItemEnd = .pause
        let interval = CMTime(value: 1, timescale: 30)
        timeObserver = player.addPeriodicTimeObserver(forInterval: interval, queue: .main) { [weak self] t in
            let s = t.seconds
            MainActor.assumeIsolated {
                guard let self, s.isFinite else { return }
                self.currentSeconds = s
            }
        }
        rateObserver = player.observe(\.timeControlStatus, options: [.initial, .new]) { [weak self] p, _ in
            let playing = p.timeControlStatus != .paused
            Task { @MainActor in self?.isPlaying = playing }
        }
    }

    deinit {
        // Observers are removed with the player; the model owns it.
        rateObserver?.invalidate()
    }

    /// Loads a local or http(s) proxy; does nothing if the URL is unchanged.
    func load(url: URL) {
        guard url != self.url else { return }
        self.url = url
        lastBuffer = nil
        let attrs: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA,
            kCVPixelBufferMetalCompatibilityKey as String: true,
            kCVPixelBufferIOSurfacePropertiesKey as String: [String: Any](),
        ]
        let out = AVPlayerItemVideoOutput(pixelBufferAttributes: attrs)
        output = out
        let item = AVPlayerItem(url: url)
        item.add(out)
        player.replaceCurrentItem(with: item)
        currentSeconds = 0
    }

    func seek(to seconds: Double, exact: Bool = true) {
        let t = CMTime(seconds: max(0, seconds), preferredTimescale: 600)
        let tol: CMTime = exact ? .zero : .positiveInfinity
        currentSeconds = max(0, seconds)
        player.seek(to: t, toleranceBefore: tol, toleranceAfter: tol) { [weak self] _ in
            Task { @MainActor in self?.refreshPausedFrame() }
        }
    }

    func play() { player.play() }
    func pause() { player.pause() }
    func togglePlayback() { isPlaying ? pause() : play() }

    private func refreshPausedFrame() {
        guard let output else { return }
        let t = player.currentTime()
        var shown = CMTime.invalid
        if let b = output.copyPixelBuffer(forItemTime: t, itemTimeForDisplay: &shown) {
            lastBuffer = b
            displayedSeconds = shown.isValid ? shown.seconds : t.seconds
        }
    }

    /// Newest decoded frame (kept so a paused frame stays drawn).
    func latestPixelBuffer(forHostTime host: CFTimeInterval) -> CVPixelBuffer? {
        guard let output else { return lastBuffer }
        let t = output.itemTime(forHostTime: host)
        var shown = CMTime.invalid
        if t.isValid, output.hasNewPixelBuffer(forItemTime: t),
           let b = output.copyPixelBuffer(forItemTime: t, itemTimeForDisplay: &shown) {
            lastBuffer = b
            displayedSeconds = shown.isValid ? shown.seconds : t.seconds
        } else if lastBuffer == nil {
            let now = player.currentTime()
            if let b = output.copyPixelBuffer(forItemTime: now, itemTimeForDisplay: &shown) {
                lastBuffer = b
                displayedSeconds = shown.isValid ? shown.seconds : now.seconds
            }
        }
        return lastBuffer
    }
}

// MARK: - Public types

enum SphereGesturePhase: Sendable { case began, changed, ended }

/// Everything the shader needs for one frame. `matrix` already includes stabilization and the view.
struct SphereRenderState {
    var matrix: simd_double3x3
    var fov: Double
    var projection: SphereProjection
}

/// CPU mirror of the shader's mapping, for tests: equirect (u, v) for an output pixel (u, v).
func sphereCPUMapping(_ state: SphereRenderState, u: Double, v: Double) -> (u: Double, v: Double) {
    let d = SphereMath.sampleDirection(matrix: state.matrix, u: u, v: v, fov: state.fov,
                                       aspect: SphereShader.frameAspect, projection: state.projection)
    return SphereMath.equirectUV(d)
}

enum SphereShader {
    static let frameAspect = 16.0 / 9.0

    static let source = """
    #include <metal_stdlib>
    using namespace metal;

    struct Uniforms {
        float3x3 m;
        float tanHalf;
        float aspect;
        int mode;
        int hasFrame;
        float2 viewSize;
        float2 pad;
    };
    struct VOut { float4 pos [[position]]; };

    vertex VOut sphere_vertex(uint vid [[vertex_id]]) {
        float2 p = float2((vid << 1) & 2, vid & 2);
        VOut o;
        o.pos = float4(p * 2.0 - 1.0, 0.0, 1.0);
        return o;
    }

    fragment float4 sphere_fragment(VOut in [[stage_in]],
                                    constant Uniforms &U [[buffer(0)]],
                                    texture2d<float> tex [[texture(0)]]) {
        if (U.hasFrame == 0) { return float4(0, 0, 0, 1); }
        // 9:16 frame (width / height = 1 / aspect), aspect-fit inside the view.
        float frameW = min(U.viewSize.x, U.viewSize.y / U.aspect);
        float frameH = frameW * U.aspect;
        float2 origin = (U.viewSize - float2(frameW, frameH)) * 0.5;
        float2 q = (in.pos.xy - origin) / float2(frameW, frameH);
        if (q.x < 0.0 || q.x > 1.0 || q.y < 0.0 || q.y > 1.0) { return float4(0, 0, 0, 1); }
        float3 ray;
        if (U.mode == 0) {
            float tx = U.tanHalf;
            ray = normalize(float3(tx * (2.0 * q.x - 1.0), tx * U.aspect * (2.0 * q.y - 1.0), 1.0));
        } else {
            float x = 1.25 * (2.0 * q.x - 1.0);
            float y = 1.25 * U.aspect * (2.0 * q.y - 1.0);
            float r = sqrt(x * x + y * y);
            if (r == 0.0) { ray = float3(0, 0, 1); }
            else {
                float th = 2.0 * atan(r);
                ray = float3(x / r * sin(th), y / r * sin(th), cos(th));
            }
        }
        float3 d = U.m * ray;
        float phi = atan2(d.x, d.z);
        float theta = asin(clamp(d.y, -1.0, 1.0));
        float2 uv = float2((phi / M_PI_F + 1.0) * 0.5, (theta / (M_PI_F * 0.5) + 1.0) * 0.5);
        constexpr sampler s(filter::linear, s_address::repeat, t_address::clamp_to_edge);
        return float4(tex.sample(s, uv).rgb, 1.0);
    }
    """
}

// MARK: - View

/// Live Metal viewer that shows exactly what v360 renders.
struct SphereViewerView: NSViewRepresentable {
    let model: SpherePlayerModel
    /// Render state for the given proxy seconds.
    let state: @MainActor (_ proxySeconds: Double) -> SphereRenderState
    /// dx > 0: mouse moved right. dy > 0: mouse moved UP on screen.
    var onDrag: (_ dxPoints: Double, _ dyPoints: Double, _ phase: SphereGesturePhase) -> Void = { _, _, _ in }
    var onScroll: (_ delta: Double) -> Void = { _ in }
    var onMagnify: (_ magnification: Double) -> Void = { _ in }
    var onDoubleClick: () -> Void = {}

    func makeCoordinator() -> SphereRenderer { SphereRenderer() }

    func makeNSView(context: Context) -> SphereMTKView {
        let view = SphereMTKView(frame: .zero, device: MTLCreateSystemDefaultDevice())
        view.isPaused = false
        view.enableSetNeedsDisplay = false
        view.preferredFramesPerSecond = 60
        view.colorPixelFormat = .bgra8Unorm
        view.clearColor = MTLClearColorMake(0, 0, 0, 1)
        context.coordinator.attach(to: view)
        view.delegate = context.coordinator
        return view
    }

    func updateNSView(_ view: SphereMTKView, context: Context) {
        context.coordinator.model = model
        context.coordinator.state = state
        view.onDrag = onDrag
        view.onScroll = onScroll
        view.onMagnify = onMagnify
        view.onDoubleClick = onDoubleClick
    }
}

final class SphereMTKView: MTKView {
    var onDrag: (Double, Double, SphereGesturePhase) -> Void = { _, _, _ in }
    var onScroll: (Double) -> Void = { _ in }
    var onMagnify: (Double) -> Void = { _ in }
    var onDoubleClick: () -> Void = {}
    private var dragStart: NSPoint?

    override var acceptsFirstResponder: Bool { true }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { onDoubleClick(); return }
        dragStart = event.locationInWindow
        onDrag(0, 0, .began)
    }
    override func mouseDragged(with event: NSEvent) {
        guard let s = dragStart else { return }
        // AppKit window coordinates have y up, so dy > 0 is mouse moved up.
        onDrag(Double(event.locationInWindow.x - s.x), Double(event.locationInWindow.y - s.y), .changed)
    }
    override func mouseUp(with event: NSEvent) {
        guard let s = dragStart else { return }
        dragStart = nil
        onDrag(Double(event.locationInWindow.x - s.x), Double(event.locationInWindow.y - s.y), .ended)
    }
    override func scrollWheel(with event: NSEvent) { onScroll(Double(event.scrollingDeltaY)) }
    override func magnify(with event: NSEvent) { onMagnify(Double(event.magnification)) }
}

// MARK: - Renderer

/// Fragment-shader uniforms (layout matches `Uniforms` in SphereShader.source).
struct SphereUniforms {
    var m: simd_float3x3
    var tanHalf: Float
    var aspect: Float
    var mode: Int32
    var hasFrame: Int32
    var viewSize: SIMD2<Float>
    var pad: SIMD2<Float> = .zero

    init(state st: SphereRenderState, drawableSize size: CGSize) {
        m = simd_float3x3(columns: (SIMD3<Float>(st.matrix.columns.0),
                                    SIMD3<Float>(st.matrix.columns.1),
                                    SIMD3<Float>(st.matrix.columns.2)))
        tanHalf = Float(tan(st.fov * .pi / 360))
        aspect = Float(SphereShader.frameAspect)
        mode = st.projection == .flat ? 0 : 1
        hasFrame = 1
        viewSize = SIMD2<Float>(Float(size.width), Float(size.height))
    }
}

@MainActor
final class SphereRenderer: NSObject, MTKViewDelegate {
    var model: SpherePlayerModel?
    var state: (@MainActor (Double) -> SphereRenderState)?
    private var queue: MTLCommandQueue?
    private var pipeline: MTLRenderPipelineState?
    private var cache: CVMetalTextureCache?

    func attach(to view: MTKView) {
        guard let device = view.device else { return }
        queue = device.makeCommandQueue()
        CVMetalTextureCacheCreate(nil, nil, device, nil, &cache)
        do {
            let lib = try device.makeLibrary(source: SphereShader.source, options: nil)
            let d = MTLRenderPipelineDescriptor()
            d.vertexFunction = lib.makeFunction(name: "sphere_vertex")
            d.fragmentFunction = lib.makeFunction(name: "sphere_fragment")
            d.colorAttachments[0].pixelFormat = view.colorPixelFormat
            pipeline = try device.makeRenderPipelineState(descriptor: d)
        } catch {
            NSLog("SphereViewer shader failed: \(error)")
        }
    }

    nonisolated func mtkView(_ view: MTKView, drawableSizeWillChange size: CGSize) {}

    nonisolated func draw(in view: MTKView) {
        MainActor.assumeIsolated { render(view) }
    }

    private func render(_ view: MTKView) {
        guard let queue, let pipeline,
              let pass = view.currentRenderPassDescriptor,
              let drawable = view.currentDrawable,
              let cmd = queue.makeCommandBuffer() else { return }
        var texture: MTLTexture?
        var cvTex: CVMetalTexture?
        if let model, let buf = model.latestPixelBuffer(forHostTime: CACurrentMediaTime()), let cache {
            let w = CVPixelBufferGetWidth(buf), h = CVPixelBufferGetHeight(buf)
            if CVMetalTextureCacheCreateTextureFromImage(nil, cache, buf, nil, .bgra8Unorm,
                                                         w, h, 0, &cvTex) == kCVReturnSuccess,
               let t = cvTex { texture = CVMetalTextureGetTexture(t) }
        }
        let st = state?(model?.displayedSeconds ?? 0)
        guard let enc = cmd.makeRenderCommandEncoder(descriptor: pass) else { return }
        if let texture, let st {
            var u = SphereUniforms(state: st, drawableSize: view.drawableSize)
            enc.setRenderPipelineState(pipeline)
            enc.setFragmentBytes(&u, length: MemoryLayout<SphereUniforms>.stride, index: 0)
            enc.setFragmentTexture(texture, index: 0)
            enc.drawPrimitives(type: .triangle, vertexStart: 0, vertexCount: 3)
        }
        enc.endEncoding()
        // Keep the texture alive until the GPU is done.
        cmd.addCompletedHandler { _ in withExtendedLifetime(cvTex) {} }
        cmd.present(drawable)
        cmd.commit()
    }
}
