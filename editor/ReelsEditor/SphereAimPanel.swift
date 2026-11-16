import SwiftUI
import simd

/// Program-monitor 360 aim mode: drag to aim, scroll/pinch to zoom, keyframes at the playhead.
struct SphereAimPanel: View {
    @Bindable var store: EditorStore
    @Bindable var aim: SphereAimModel
    @StateObject private var player = SpherePlayerModel()
    @State private var viewSize: CGSize = .zero

    private var local: Double? { aim.localSeconds(frame: store.playheadFrame, store: store) }
    private var raw: JSONValue? { aim.node(in: store)?.raw }

    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Color.black
                if let proxy = aim.proxy {
                    SphereViewerView(
                        model: player,
                        state: { seconds in aim.renderState(proxySeconds: seconds) },
                        onDrag: { dx, dy, phase in drag(dx: dx, dy: dy, phase: phase) },
                        onScroll: { delta in zoom(delta * 0.25) },
                        onMagnify: { m in zoom(m * 60) },
                        onDoubleClick: { addKey() }
                    )
                    .onAppear { load(proxy) }
                    .onChange(of: proxy.url) { _, _ in load(proxy) }
                }
                overlay
            }
            .background(GeometryReader { geo in
                Color.clear.onAppear { viewSize = geo.size }.onChange(of: geo.size) { _, size in viewSize = size }
            })
            KeyframeStrip(store: store, aim: aim)
            toolbar
        }
        .onChange(of: store.seekGeneration) { _, _ in syncToPlayhead() }
        .onChange(of: store.snapshot?.revisions.document) { _, _ in
            guard let id = aim.camera?.nodeID else { return }
            Task { await aim.reloadCamera(nodeID: id, store: store, prepare: false) }
        }
        .onReceive(player.$currentSeconds) { seconds in followPlayback(seconds) }
        .onReceive(NotificationCenter.default.publisher(for: .reelsTogglePlayback)) { _ in togglePlayback() }
        .onChange(of: store.playbackRate) { _, rate in if player.isPlaying { player.rate = Float(rate) } }
        .onDisappear { player.pause() }
    }

    // MARK: - Overlay

    @ViewBuilder
    private var overlay: some View {
        switch aim.status {
        case .loading where aim.proxy == nil:
            VStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text("Preparing the 360 view…").font(EditorType.body).foregroundStyle(.white.opacity(0.8))
            }
        case .failed(let message):
            VStack(spacing: 8) {
                Image(systemName: "exclamationmark.triangle").foregroundStyle(EditorPalette.warning)
                Text(message).font(EditorType.body).foregroundStyle(.white.opacity(0.85))
                    .multilineTextAlignment(.center).frame(maxWidth: 300)
            }
        default:
            if aim.proxy != nil {
                VStack {
                    HStack(alignment: .top) {
                        Text(statusLine)
                            .font(EditorType.timecode)
                            .padding(.horizontal, 6).padding(.vertical, 3)
                            .background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 4))
                            .foregroundStyle(.white)
                        Spacer()
                    }
                    Spacer()
                    if local == nil {
                        Button("Playhead is outside this clip — jump to it") { jumpToClip() }
                            .controlSize(.small)
                            .padding(.bottom, 8)
                    }
                }
                .padding(8)
                .allowsHitTesting(local == nil)
            }
        }
    }

    private var statusLine: String {
        guard let camera = aim.camera else { return "360°" }
        let view = aim.view(atLocal: local ?? 0)
        var parts = [String(format: "yaw %.0f°  pitch %.0f°  fov %.0f°", SphereMath.wrapYaw(view.yaw), view.pitch, view.fov)]
        if abs(view.roll) > 0.5 { parts.append(String(format: "roll %.0f°", view.roll)) }
        if camera.keys.count >= 2 { parts.append("\(camera.keys.count) keyframes") }
        if camera.stabilize != "off" {
            parts.append(aim.proxy?.hasOrientation == false ? "no stabilization data" : "stabilized")
        }
        return parts.joined(separator: " · ")
    }

    // MARK: - Toolbar

    private var toolbar: some View {
        let t = local
        let keyHere = t.flatMap { aim.keyIndex(atLocal: $0, fps: store.fps) }
        let hasKeys = (aim.camera?.keys.count ?? 0) >= 2
        return HStack(spacing: 8) {
            Button { stepKey(-1) } label: { Image(systemName: "chevron.left.to.line") }
                .help("Previous keyframe").disabled(!hasKeys)
            Button { addKey() } label: {
                Label(keyHere == nil ? "Add keyframe" : "Update keyframe", systemImage: keyHere == nil ? "diamond" : "diamond.fill")
            }
            .help("Set the camera angle at the playhead as a keyframe (or double-click the picture)")
            .disabled(t == nil)
            Button { stepKey(1) } label: { Image(systemName: "chevron.right.to.line") }
                .help("Next keyframe").disabled(!hasKeys)
            Toggle(isOn: Binding(
                get: { keyHere.flatMap { aim.camera?.keys[$0].cut } ?? false },
                set: { _ in toggleCut() }
            )) { Text("Hard cut") }
            .toggleStyle(.checkbox)
            .disabled(t == nil || !hasKeys)
            .help("Jump to this keyframe's angle instead of panning to it")
            Button { deleteKey() } label: { Image(systemName: "trash") }
                .help("Delete the keyframe at the playhead").disabled(keyHere == nil)
            Divider().frame(height: 16)
            Menu {
                ForEach(aim.camera?.angles ?? [], id: \.name) { angle in
                    Button(angle.name) { applyAngle(angle.view) }
                }
                if !(aim.camera?.angles.isEmpty ?? true) { Divider() }
                Button("Level horizon") { level() }.disabled(aim.upAxis == nil)
                if hasKeys { Button("Remove all keyframes") { clearKeys() } }
            } label: { Label("Angles", systemImage: "scope") }
            .menuStyle(.borderlessButton).fixedSize()
            .help("Tagged angles for this clip, and horizon levelling")
            Picker("Stabilize", selection: Binding(
                get: { aim.camera?.stabilize ?? "off" },
                set: { setStabilize($0) }
            )) {
                ForEach(SphereAimModel.stabilizeChoices, id: \.self) { Text($0.capitalized).tag($0) }
                if aim.camera?.stabilize == "custom" { Text("Custom").tag("custom") }
            }
            .pickerStyle(.menu).fixedSize()
            .help("Cancel camera shake with the camera's own orientation data. Lock holds the starting direction.")
            Picker("Projection", selection: Binding(
                get: { aim.camera?.projection ?? .flat },
                set: { setProjection($0) }
            )) {
                Text("Flat").tag(SphereProjection.flat)
                Text("Tiny planet").tag(SphereProjection.tinyPlanet)
            }
            .labelsHidden().pickerStyle(.menu).fixedSize()
            Spacer(minLength: 0)
            Button("Done") { aim.close() }
                .help("Back to the Program preview")
        }
        .font(EditorType.body)
        .buttonStyle(.borderless)
        .controlSize(.small)
        .lineLimit(1)
        .padding(.horizontal, 10)
        .frame(height: 34)
        .background(EditorPalette.panel)
        .overlay(alignment: .top) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
    }

    // MARK: - Actions

    private func load(_ proxy: SphereAimModel.Proxy) {
        player.load(url: proxy.url)
        syncToPlayhead()
    }

    private func proxySeconds(forLocal t: Double) -> Double? {
        guard let proxy = aim.proxy else { return nil }
        return aim.sourceSeconds(local: t) - proxy.start
    }

    private func syncToPlayhead() {
        guard !player.isPlaying, let t = local, let seconds = proxySeconds(forLocal: t) else { return }
        player.seek(to: seconds)
    }

    private func followPlayback(_ seconds: Double) {
        guard player.isPlaying, let proxy = aim.proxy, let camera = aim.camera else { return }
        let t = aim.local(fromSource: proxy.start + seconds)
        if t >= camera.seconds {
            player.pause()
            return
        }
        if let frame = aim.programFrame(local: max(0, t), store: store) {
            store.updatePlayheadFromPreview(frame, revision: nil)
        }
    }

    private func togglePlayback() {
        if player.isPlaying {
            player.pause()
            store.setPlayhead(store.playheadFrame)
            return
        }
        if local == nil { jumpToClip() }
        guard let t = local, let seconds = proxySeconds(forLocal: t) else { return }
        player.seek(to: seconds)
        player.play()
        player.rate = Float(store.playbackRate * (aim.camera?.speed ?? 1))
    }

    private func jumpToClip() {
        guard let node = aim.node(in: store) else { return }
        store.setPlayhead(node.startFrame)
    }

    private func stepKey(_ direction: Int) {
        guard let camera = aim.camera, let t = local ?? Optional(0) else { return }
        let eps = 0.5 / store.fps
        let target = direction > 0
            ? camera.keys.first { $0.at > t + eps }
            : camera.keys.last { $0.at < t - eps }
        if let target, let frame = aim.programFrame(local: target.at, store: store) { store.setPlayhead(frame) }
    }

    private func commit(_ operations: [JSONValue], _ reason: String) {
        aim.draft = nil
        guard !operations.isEmpty else { return }
        Task { await store.apply(operations, reason: reason) }
    }

    private func drag(dx: Double, dy: Double, phase: SphereGesturePhase) {
        guard let t = local else { return }
        let width = min(viewSize.width, viewSize.height * 9 / 16)
        if phase == .ended {
            aim.endDrag()
            if let draft = aim.draft {
                commit(aim.operationsForAim(draft, atLocal: t, fps: store.fps, raw: raw), "Aim 360 camera")
            }
            return
        }
        aim.dragChanged(dx: dx, dy: dy, phase: phase, local: t, viewWidth: width)
    }

    private func zoom(_ delta: Double) {
        guard let t = local else { return }
        aim.zoom(by: delta, local: t)
        aim.scheduleZoomCommit {
            guard let draft = aim.draft, let t = local else { return }
            commit(aim.operationsForAim(draft, atLocal: t, fps: store.fps, raw: raw), "Zoom 360 camera")
        }
    }

    private func addKey() {
        guard let t = local else { return }
        commit(aim.operationsForAddKey(atLocal: t, fps: store.fps, raw: raw), "Add 360 keyframe")
    }

    private func toggleCut() {
        guard let t = local else { return }
        commit(aim.operationsForToggleCut(atLocal: t, fps: store.fps, raw: raw), "Toggle 360 hard cut")
    }

    private func deleteKey() {
        guard let t = local else { return }
        commit(aim.operationsForDeleteKey(atLocal: t, fps: store.fps, raw: raw), "Delete 360 keyframe")
    }

    private func clearKeys() {
        commit(aim.operationsForClearKeys(atLocal: local ?? 0, raw: raw), "Remove 360 keyframes")
    }

    private func applyAngle(_ view: SphereView) {
        guard let t = local else { return }
        commit(aim.operationsForAim(view, atLocal: t, fps: store.fps, raw: raw), "Aim 360 camera")
    }

    private func level() {
        guard let t = local, let up = aim.upAxis else { return }
        let leveled = SphereMath.level(aim.view(atLocal: t), upAxis: up)
        commit(aim.operationsForAim(leveled, atLocal: t, fps: store.fps, raw: raw), "Level 360 horizon")
    }

    private func setStabilize(_ choice: String) {
        guard choice != "custom" else { return }
        let operations = aim.operationsForStabilize(choice)
        guard let id = aim.camera?.nodeID else { return }
        Task {
            await store.apply(operations, reason: "360 stabilize \(choice)")
            await aim.reloadCamera(nodeID: id, store: store, prepare: true)
        }
    }

    private func setProjection(_ projection: SphereProjection) {
        commit(aim.operationsForProjection(projection), "360 projection")
    }
}

/// The clip's keyframes under the 360 view: diamonds (filled = hard cut), the playhead, click to jump.
private struct KeyframeStrip: View {
    @Bindable var store: EditorStore
    @Bindable var aim: SphereAimModel

    var body: some View {
        GeometryReader { geo in
            let seconds = max(aim.camera?.seconds ?? 1, 0.01)
            let width = geo.size.width - 20
            ZStack(alignment: .leading) {
                Rectangle().fill(EditorPalette.separator).frame(width: width, height: 2).offset(x: 10)
                ForEach(Array((aim.camera?.keys ?? []).enumerated()), id: \.offset) { _, key in
                    Image(systemName: key.cut ? "diamond.fill" : "diamond")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(EditorPalette.accent)
                        .position(x: 10 + width * min(1, max(0, key.at / seconds)), y: geo.size.height / 2)
                        .onTapGesture {
                            if let frame = aim.programFrame(local: key.at, store: store) { store.setPlayhead(frame) }
                        }
                        .help(String(format: "%.2f s%@", key.at, key.cut ? " · hard cut" : ""))
                }
                if let t = aim.localSeconds(frame: store.playheadFrame, store: store) {
                    Rectangle().fill(EditorPalette.accent).frame(width: 1, height: geo.size.height - 4)
                        .position(x: 10 + width * min(1, max(0, t / seconds)), y: geo.size.height / 2)
                }
            }
        }
        .frame(height: 18)
        .background(EditorPalette.panel)
        .accessibilityLabel("360 keyframes")
    }
}
