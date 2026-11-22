import AVFoundation
import AppKit
import SwiftUI

/// State for the aim viewer: scrub, aim, add key. The picture is drawn by the GPU viewer (Viewer.swift) from the decoded frame; aim changes
/// only update `viewer.view`. A backend still is the fallback source when the clip cannot be decoded here.
@MainActor @Observable
final class ReframeModel {
    let asset: Asset
    let store: LibraryStore
    var settings: ReframeSettings
    var t = 0.0
    var aim = ReframeSettings.straight { didSet { viewer.view = aim } }
    let viewer = ViewerState()
    var check: CGImage?
    var checkNote: String?
    var error: String?
    var playing = false
    @ObservationIgnored private var engine: PlaybackEngine?
    @ObservationIgnored private var audio: AVPlayer?
    @ObservationIgnored private var playTask: Task<Void, Never>?
    @ObservationIgnored private var state: ReframeState
    @ObservationIgnored private var engineFailed = false
    @ObservationIgnored private var sourceTask: Task<Void, Never>?

    init(asset: Asset, store: LibraryStore) {
        self.asset = asset; self.store = store
        state = ReframeState.load(store.root)
        var s = state.reframe[asset.id] ?? ReframeSettings()
        if s.out <= s.in { s.in = 0; s.out = asset.duration ?? 0 }
        settings = s
        viewer.aspect = Double(settings.height) / Double(settings.width)
        viewer.view = aim
        loadSource()
    }

    var duration: Double { asset.duration ?? 1 }
    var relative: Double { max(0, t - settings.in) }
    var previewSize: (w: Int, h: Int) {
        let k = 480.0 / Double(max(settings.width, settings.height))
        return (Int(Double(settings.width) * k), Int(Double(settings.height) * k))
    }

    func persist() { state.reframe[asset.id] = settings; state.save(store.root) }

    func seek(_ time: Double) {
        pause()
        t = min(duration, max(0, time))
        if let v = settings.view(at: relative) { aim = v }
        loadSource()
    }

    /// Play/pause. Plays from the playhead (from `in` if it is at the out point), stops at `out` (no loop). The preview follows
    /// the keyed path if there are keys, else the current aim; audio is the clock.
    func togglePlay() { if playing { pause() } else { play() } }

    func pause() {
        guard playing else { return }
        playing = false; playTask?.cancel(); audio?.pause()
    }

    private func play() {
        sourceTask?.cancel(); playing = true
        t = PlaybackMath.startTime(t, in: settings.in, out: settings.out)
        playTask = Task { [weak self] in
            guard let self else { return }
            let engine = await self.ensureEngine()
            guard let engine, !Task.isCancelled else { self.playing = false; self.error = self.error ?? "Playback unavailable for this clip"; return }
            self.error = nil
            let start = self.t
            let player = await self.audioPlayer()
            await player?.seek(to: CMTime(seconds: start, preferredTimescale: 600), toleranceBefore: .zero, toleranceAfter: .zero)
            player?.play()
            self.viewer.continuous = true   // the MTKView redraws at display rate from here on, reading the newest frame and view
            defer { self.viewer.continuous = false }
            let began = Date()
            var tick = 0
            while !Task.isCancelled {
                let now = player.map { $0.currentTime().seconds } ?? start + Date().timeIntervalSince(began)
                if PlaybackMath.isFinished(now, out: self.settings.out) || (player.map { $0.rate == 0 && now > start } ?? false) { self.t = self.settings.out; self.pause(); break }
                let keyed = self.settings.view(at: max(0, now - self.settings.in))
                if let keyed { self.viewer.view = keyed }
                // the SwiftUI readouts follow at ~30 Hz; the picture does not wait for them
                if tick % 4 == 0 { self.t = now; if let keyed { self.aim = keyed } }
                tick += 1
                do { if let f = try await engine.frame(at: now), f !== self.viewer.frame { self.viewer.frame = f } }
                catch is CancellationError { break }
                catch { self.error = error.localizedDescription; self.pause(); break }
                try? await Task.sleep(for: .milliseconds(4))
            }
        }
    }

    /// The decoder for this clip (made once; nil when the clip can only be shown from backend stills).
    private func ensureEngine() async -> PlaybackEngine? {
        if let engine { return engine }
        if engineFailed { return nil }
        let level = try? await store.backend.level(id: asset.id)
        let ex = asset.equirectExport.map { URL(fileURLWithPath: $0.path) }
        engine = await PlaybackEngine.make(asset: asset, export: ex, level: level)
        engineFailed = engine == nil
        return engine
    }

    /// Audio-only player on the clip's first audio track (the video is decoded separately).
    private func audioPlayer() async -> AVPlayer? {
        if let audio { return audio }
        let src = AVURLAsset(url: asset.mediaURL, options: [AVURLAssetOverrideMIMETypeKey: "video/mp4"])
        guard let track = try? await src.loadTracks(withMediaType: .audio).first,
              let range = try? await track.load(.timeRange) else { return nil }
        let comp = AVMutableComposition()
        guard let ct = comp.addMutableTrack(withMediaType: .audio, preferredTrackID: kCMPersistentTrackID_Invalid),
              (try? ct.insertTimeRange(range, of: track, at: .zero)) != nil else { return nil }
        audio = AVPlayer(playerItem: AVPlayerItem(asset: comp))
        return audio
    }

    func setIn() { settings.in = min(t, settings.out); persist() }
    func setOut() { settings.out = max(t, settings.in); persist() }
    func addKey() { settings.setKey(at: relative, view: aim); persist() }
    func deleteKey() { settings.deleteKey(near: relative); persist() }
    func apply(_ view: SphereView) { aim = view }

    func drag(dx: Double, dy: Double) {
        let k = aim.fov / 500
        aim.yaw = SphereMath.wrapYaw(aim.yaw - dx * k)
        aim.pitch = min(90, max(-90, aim.pitch + dy * k))
    }

    func zoom(_ delta: Double) { aim.fov = min(150, max(10, aim.fov * (1 - delta))) }

    func setAspect(width: Int, height: Int) { settings.width = width; settings.height = height; persist(); viewer.aspect = Double(height) / Double(width) }

    /// Latest-wins: every call replaces the pending one. After a short settle the engine decodes the frame at the playhead and the viewer
    /// shows it (paused view always comes from the GPU path); the backend's levelled still is only the fallback.
    func loadSource() {
        sourceTask?.cancel()
        let time = t
        sourceTask = Task { [weak self] in
            try? await Task.sleep(for: .milliseconds(60))
            guard let self, !Task.isCancelled else { return }
            if let engine = await self.ensureEngine() {
                do {
                    guard !Task.isCancelled else { return }
                    if let f = try await engine.frame(at: time), !Task.isCancelled { self.viewer.frame = f; self.error = nil; return }
                } catch is CancellationError { return } catch { self.error = error.localizedDescription }
            }
            do {
                let image = try await self.fetchSource(at: (time * 10).rounded() / 10)
                guard !Task.isCancelled, let r = self.viewer.renderer, let f = FrameTextures.still(image, device: r.device, time: time) else { return }
                self.viewer.frame = f; self.error = nil
            } catch { if !Task.isCancelled { self.error = error.localizedDescription } }
        }
    }

    private func fetchSource(at time: Double) async throws -> CGImage {
        if let export = asset.equirectExport, FileManager.default.fileExists(atPath: export.path) {
            let gen = AVAssetImageGenerator(asset: AVURLAsset(url: URL(fileURLWithPath: export.path)))
            gen.maximumSize = CGSize(width: 2048, height: 1024)
            gen.requestedTimeToleranceBefore = CMTime(seconds: 0.1, preferredTimescale: 600)
            gen.requestedTimeToleranceAfter = CMTime(seconds: 0.1, preferredTimescale: 600)
            return try await gen.image(at: CMTime(seconds: time, preferredTimescale: 600)).image
        }
        let url = try await store.backend.frame(id: asset.id, t: time, width: 2048)
        guard let img = await Media.image(path: url.path, maxPixel: 2048) else { throw BackendError(message: "Could not read frame") }
        return img
    }

    /// Backend render of the same view at the same size, for comparing against the local preview.
    func checkFrame() async {
        let (w, h) = previewSize
        checkNote = "Rendering…"
        do {
            let url = try await store.backend.frame(id: asset.id, t: t, view: aim, w: w, h: h)
            check = await Media.image(path: url.path, maxPixel: 2000)
            if let c = check, let p = viewer.snapshot(width: w, height: h), let d = FlatRenderer.meanAbsDiff(p, c) { checkNote = String(format: "Mean pixel difference %.1f / 255", d) }
            else { checkNote = nil }
        } catch { checkNote = error.localizedDescription }
    }

    func exportFlat(tools: ToolsModel) {
        persist()
        let s = settings
        let spec = JobSpec(id: asset.id, kind: "flat", stabilize: false, in: s.in, out: s.out, width: s.width, height: s.height, fps: s.fps,
                           keys: s.keys.isEmpty ? [ReframeKey(at: 0, view: aim)] : s.keys)
        tools.enqueue(spec, title: "Flat \(s.width)x\(s.height) \(asset.id)")
    }
}

struct ReframeView: View {
    @State var model: ReframeModel
    @Environment(ToolsModel.self) private var tools

    init(store: LibraryStore, asset: Asset) { _model = State(initialValue: ReframeModel(asset: asset, store: store)) }

    var body: some View {
        @Bindable var m = model
        VStack(spacing: 8) {
            HStack(spacing: 12) {
                VStack {
                    ViewerSurface(state: model.viewer, onDrag: model.drag, onZoom: model.zoom).frame(maxWidth: .infinity, maxHeight: .infinity)
                    Text("Local preview (drag to aim, scroll to zoom)").font(.caption).foregroundStyle(.secondary)
                }
                if model.check != nil { pane(model.check, caption: model.checkNote ?? "Backend frame") }
            }.frame(maxHeight: .infinity)
            if let e = model.error { Text(e).font(.caption).foregroundStyle(.red) }
            HStack {
                Picker("Aspect", selection: Binding(get: { "\(m.settings.width)x\(m.settings.height)" },
                                                    set: { s in if let a = ReframeSettings.aspects.first(where: { "\($0.1)x\($0.2)" == s }) { m.setAspect(width: a.1, height: a.2) } })) {
                    ForEach(ReframeSettings.aspects, id: \.0) { Text($0.0).tag("\($0.1)x\($0.2)") }
                }.pickerStyle(.segmented).frame(width: 170).labelsHidden()
                Picker("FPS", selection: $m.settings.fps) { Text("30 fps").tag(30); Text("60 fps").tag(60) }
                    .pickerStyle(.segmented).frame(width: 130).labelsHidden().onChange(of: m.settings.fps) { m.persist() }
                Button("Straight ahead") { m.apply(ReframeSettings.straight) }
                Button("Behind (rider)") { m.apply(ReframeSettings.behind) }
                Spacer()
                Text(String(format: "yaw %.0f  pitch %.0f  fov %.0f", m.aim.yaw, m.aim.pitch, m.aim.fov)).font(.caption.monospacedDigit())
            }
            HStack {
                Text("Roll").font(.caption)
                Slider(value: Binding(get: { m.aim.roll }, set: { m.aim.roll = $0 }), in: -180...180)
                Text(String(format: "%.0f", m.aim.roll)).font(.caption.monospacedDigit()).frame(width: 36)
                Button("Check frame") { Task { await m.checkFrame() } }
                Button("Export flat") { m.exportFlat(tools: tools) }.buttonStyle(.borderedProminent)
            }
            Timeline(model: model)
            HStack {
                Button { m.togglePlay() } label: { Label(m.playing ? "Pause" : "Play", systemImage: m.playing ? "pause.fill" : "play.fill") }
                    .keyboardShortcut(.space, modifiers: [])
                Button("Set in (I)") { m.setIn() }.keyboardShortcut("i", modifiers: [])
                Button("Set out (O)") { m.setOut() }.keyboardShortcut("o", modifiers: [])
                Button("Add / update key (K)") { m.addKey() }.keyboardShortcut("k", modifiers: [])
                Button("Delete key") { m.deleteKey() }.keyboardShortcut(.delete, modifiers: [])
                Spacer()
                Text("\(formatDuration(m.t)) · in \(String(format: "%.1f", m.settings.in)) out \(String(format: "%.1f", m.settings.out)) · \(m.settings.keys.count) keys")
                    .font(.caption).foregroundStyle(.secondary)
            }.font(.caption)
        }
        .padding(10)
        .onAppear { m.viewer.setNeedsDraw() }
        .onDisappear { m.pause() }
    }

    private func pane(_ image: CGImage?, caption: String) -> some View {
        VStack {
            Group { if let image { Image(decorative: image, scale: 1).resizable().scaledToFit() } else { ProgressView() } }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            Text(caption).font(.caption).foregroundStyle(.secondary)
        }
    }
}

/// Scrub bar: drag to move the playhead, diamonds are keys (click to jump), shaded range is in...out.
struct Timeline: View {
    let model: ReframeModel
    var body: some View {
        GeometryReader { geo in
            let w = geo.size.width, d = max(0.1, model.duration)
            let x = { (s: Double) in CGFloat(s / d) * w }
            ZStack(alignment: .leading) {
                Rectangle().fill(.quaternary).frame(height: 8)
                Rectangle().fill(.blue.opacity(0.35)).frame(width: max(0, x(model.settings.out) - x(model.settings.in)), height: 8)
                    .offset(x: x(model.settings.in))
                Rectangle().fill(.white).frame(width: 2, height: 24).offset(x: x(model.t) - 1)
                ForEach(Array(model.settings.keys.enumerated()), id: \.offset) { _, k in
                    Image(systemName: "diamond.fill").foregroundStyle(.yellow).font(.system(size: 11))
                        .position(x: x(model.settings.in + k.at), y: 12)
                        .onTapGesture { model.seek(model.settings.in + k.at) }
                }
            }
            .frame(height: 24).contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0).onChanged { model.seek(Double($0.location.x / w) * d) })
        }.frame(height: 24)
    }
}
