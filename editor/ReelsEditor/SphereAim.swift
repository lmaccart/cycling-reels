import Foundation
import Observation
import simd

/// The 360 aim mode of the Program monitor: a live view into the selected 360 clip
/// where dragging aims the virtual camera, and keyframes are set at the playhead.
///
/// Edits go to the plan exactly like Inspector edits (`set_field` on the clip): `/view`
/// for a clip with one angle, `/path` for keyframes (`cut: true` = hard cut),
/// `/stabilize` and `/projection`. The renderer reproduces the same math with ffmpeg.
@MainActor
@Observable
final class SphereAimModel {
    enum Status: Equatable { case idle, loading, ready, failed(String) }

    struct Camera: Equatable {
        var nodeID: String
        var assetID: String
        var sourceIn: Double
        var sourceOut: Double
        var speed: Double
        var seconds: Double
        var projection: SphereProjection
        var view: SphereView
        var keys: [SphereKey]
        /// Original interpolation intervals for playback; `keys` are bounded edit handles.
        var playbackKeys: [SphereKey]? = nil
        var playbackOffset: Double = 0
        var stabilize: String          // off | light | medium | strong | lock | custom
        var stabilizeSeconds: Double? = nil   // the plan's number when `stabilize` is custom
        var angles: [(name: String, view: SphereView)]

        static func == (lhs: Camera, rhs: Camera) -> Bool {
            lhs.nodeID == rhs.nodeID && lhs.assetID == rhs.assetID && lhs.sourceIn == rhs.sourceIn
                && lhs.sourceOut == rhs.sourceOut && lhs.speed == rhs.speed && lhs.projection == rhs.projection
                && lhs.view == rhs.view && lhs.keys == rhs.keys && lhs.stabilize == rhs.stabilize
                && lhs.stabilizeSeconds == rhs.stabilizeSeconds
                && lhs.playbackKeys == rhs.playbackKeys && lhs.playbackOffset == rhs.playbackOffset
                && lhs.angles.map(\.name) == rhs.angles.map(\.name)
        }
    }

    struct Proxy: Equatable {
        var url: URL
        var start: Double
        var end: Double
        var fps: Double
        var gravity: simd_double3?
        var hasOrientation: Bool
        var stabilization: [simd_double3x3]   // per proxy frame, empty = none
        var stabilizationStart: Double
    }

    var isActive = false
    private(set) var status: Status = .idle
    private(set) var camera: Camera?
    private(set) var proxy: Proxy?
    /// The angle being dragged/zoomed right now (shown live, written on release).
    var draft: SphereView?
    private var requestID = ""
    /// The clip the panel is showing or loading (set before its camera arrives).
    var boundNodeID: String?
    @ObservationIgnored private var dragStart: SphereView?
    @ObservationIgnored private var zoomCommit: Task<Void, Never>?

    static let stabilizeChoices = ["off", "light", "medium", "strong", "lock"]

    // MARK: - Opening

    /// Whether a timeline node is 360 video (its library asset has a sphere probe).
    static func isSphereClip(_ node: EditorNode?, library: [JSONValue]) -> Bool {
        guard let node, let assetID = node.assetId, !node.isPhoto else { return false }
        let kind = node.kind.lowercased()
        guard kind == "segment" || kind.contains("cutaway") else { return false }
        guard let asset = library.first(where: { $0["id"]?.stringValue == assetID }) else { return false }
        return asset["probe"]?["sphere"] != nil && asset["probe"]?["sphere"] != .null
            && (asset["kind"]?.stringValue ?? "video") == "video"
    }

    func open(nodeID: String, store: EditorStore) async {
        isActive = true
        draft = nil
        boundNodeID = nodeID
        if camera?.nodeID != nodeID {
            proxy = nil
            camera = nil  // nothing edits the previous clip while this one loads
        }
        status = .loading
        await reloadCamera(nodeID: nodeID, store: store, prepare: true)
    }

    func close() {
        isActive = false
        draft = nil
        zoomCommit?.cancel()
    }

    /// Re-reads the clip's camera from the plan (after any edit, ours or the terminal's).
    func reloadCamera(nodeID: String, store: EditorStore, prepare: Bool) async {
        do {
            let value = try await store.service.call("sphere.camera", params: .object(["id": .string(nodeID)]))
            guard boundNodeID == nil || boundNodeID == nodeID else { return }  // rebound while this was loading
            guard let parsed = Self.parseCamera(value, nodeID: nodeID) else {
                status = .failed("This clip's 360 camera couldn't be read.")
                return
            }
            let previous = camera
            camera = parsed
            let rangeChanged = previous == nil || previous?.assetID != parsed.assetID
                || previous?.sourceIn != parsed.sourceIn || previous?.sourceOut != parsed.sourceOut
            let stabChanged = previous?.stabilize != parsed.stabilize || previous?.stabilizeSeconds != parsed.stabilizeSeconds
            if prepare || rangeChanged || stabChanged || proxy == nil {
                if proxy == nil || rangeChanged { status = .loading }
                await requestProxy(store: store)
            }
        } catch {
            guard boundNodeID == nil || boundNodeID == nodeID else { return }
            status = .failed(error.localizedDescription)
        }
    }

    private func requestProxy(store: EditorStore) async {
        guard let camera else { return }
        requestID = UUID().uuidString
        var params: [String: JSONValue] = [
            "id": .string(camera.assetID),
            "start": .number(camera.sourceIn),
            "end": .number(camera.sourceOut),
            "requestId": .string(requestID),
        ]
        if let value = Self.stabilizeParam(camera) { params["stabilize"] = value }
        do {
            _ = try await store.service.call("sphere.prepare", params: .object(params))
        } catch {
            status = .failed(error.localizedDescription)
        }
    }

    /// What the live view stabilizes with: the plan's own value, so a custom number of
    /// seconds smooths exactly as much as the render does.
    static func stabilizeParam(_ camera: Camera) -> JSONValue? {
        if camera.stabilize == "off" { return nil }
        if camera.stabilize == "custom", let seconds = camera.stabilizeSeconds { return .number(seconds) }
        return .string(camera.stabilize)
    }

    /// `sphere.ready` / `sphere.unavailable` events from the service.
    func receive(name: String, payload: JSONValue) {
        guard payload["requestId"]?.stringValue == requestID else { return }
        if name == "sphere.unavailable" {
            status = .failed(payload["reason"]?.stringValue ?? "The 360 view couldn't be prepared.")
            return
        }
        guard let raw = payload["url"]?.stringValue, let url = URL(string: raw),
              let start = payload["start"]?.numberValue, let end = payload["end"]?.numberValue else {
            status = .failed("The 360 view came back incomplete.")
            return
        }
        var gravity: simd_double3?
        if let g = payload["gravity"]?.arrayValue?.compactMap(\.numberValue), g.count == 3 {
            gravity = simd_normalize(simd_double3(g[0], g[1], g[2]))
        }
        var matrices: [simd_double3x3] = []
        var stabStart = start
        if let stab = payload["stabilization"], let quats = stab["quats"]?.arrayValue {
            stabStart = stab["start"]?.numberValue ?? start
            matrices = quats.compactMap { q in
                guard let c = q.arrayValue?.compactMap(\.numberValue), c.count == 4 else { return nil }
                return SphereMath.matrix(quaternion: c[0], x: c[1], y: c[2], z: c[3])
            }
        }
        proxy = Proxy(url: url, start: start, end: end, fps: payload["fps"]?.numberValue ?? 30,
                      gravity: gravity, hasOrientation: payload["hasOrientation"]?.boolValue ?? false,
                      stabilization: matrices, stabilizationStart: stabStart)
        status = .ready
    }

    static func parseCamera(_ value: JSONValue, nodeID: String) -> Camera? {
        guard let assetID = value["assetId"]?.stringValue,
              let sourceIn = value["in"]?.numberValue, let sourceOut = value["out"]?.numberValue,
              let view = parseView(value["view"]) else { return nil }
        let speed = value["speed"]?.numberValue ?? 1
        func parseKeys(_ raw: JSONValue?) -> [SphereKey] {
            (raw?.arrayValue ?? []).compactMap { item in
                guard let at = item["at"]?.numberValue, let v = parseView(item) else { return nil }
                return SphereKey(at: at, view: v, cut: item["cut"]?.boolValue ?? false)
            }.sorted { $0.at < $1.at }
        }
        let keys = parseKeys(value["path"])
        let angles = (value["angles"]?.objectValue ?? [:]).compactMap { name, raw -> (String, SphereView)? in
            guard let v = parseView(raw, fallback: view) else { return nil }
            return (name, v)
        }.sorted { $0.0 < $1.0 }
        return Camera(
            nodeID: nodeID, assetID: assetID, sourceIn: sourceIn, sourceOut: sourceOut, speed: speed,
            seconds: value["seconds"]?.numberValue ?? (sourceOut - sourceIn) / max(speed, 0.01),
            projection: value["projection"]?.stringValue == "tiny-planet" ? .tinyPlanet : .flat,
            view: view, keys: keys,
            playbackKeys: value["playbackPath"].map { parseKeys($0) },
            playbackOffset: value["playbackOffsetSeconds"]?.numberValue ?? 0,
            stabilize: stabilizeLabel(value["stabilize"]),
            stabilizeSeconds: value["stabilize"]?.numberValue,
            angles: angles.map { (name: $0.0, view: $0.1) }
        )
    }

    private static func parseView(_ value: JSONValue?, fallback: SphereView? = nil) -> SphereView? {
        guard let value else { return fallback }
        if let yaw = value.numberValue { return fallback.map { SphereView(yaw: yaw, pitch: $0.pitch, fov: $0.fov, roll: $0.roll) } }
        guard let yaw = value["yaw"]?.numberValue ?? fallback?.yaw else { return nil }
        return SphereView(
            yaw: yaw,
            pitch: value["pitch"]?.numberValue ?? fallback?.pitch ?? 0,
            fov: value["fov"]?.numberValue ?? fallback?.fov ?? 75,
            roll: value["roll"]?.numberValue ?? fallback?.roll ?? 0
        )
    }

    static func stabilizeLabel(_ value: JSONValue?) -> String {
        switch value {
        case nil, .null, .bool(false): return "off"
        case .bool(true): return "medium"
        case .string(let s): return stabilizeChoices.contains(s.lowercased()) ? s.lowercased() : "off"
        case .number: return "custom"
        default: return "off"
        }
    }

    /// Unit tests set a parsed camera directly (no service round trip).
    func testingSetCamera(_ value: Camera?) { camera = value }

    // MARK: - Time

    func node(in store: EditorStore) -> EditorNode? {
        guard let id = camera?.nodeID else { return nil }
        return store.snapshot?.nodes.first { $0.id == id }
    }

    /// Seconds from the clip's start (after speed) for a program frame, or nil outside the clip.
    func localSeconds(frame: Int, store: EditorStore) -> Double? {
        guard let node = node(in: store) else { return nil }
        guard frame >= node.startFrame, frame < max(node.endFrame, node.startFrame + 1) else { return nil }
        return Double(frame - node.startFrame) / store.fps
    }

    func sourceSeconds(local: Double) -> Double {
        guard let camera else { return 0 }
        return camera.sourceIn + local * camera.speed
    }

    func local(fromSource source: Double) -> Double {
        guard let camera else { return 0 }
        return (source - camera.sourceIn) / max(camera.speed, 0.01)
    }

    func programFrame(local: Double, store: EditorStore) -> Int? {
        guard let node = node(in: store) else { return nil }
        return node.startFrame + Int((local * store.fps).rounded())
    }

    // MARK: - Views

    func view(atLocal t: Double) -> SphereView {
        if let draft { return draft }
        guard let camera else { return SphereView(yaw: 0, pitch: 0, fov: 75, roll: 0) }
        let keys = camera.playbackKeys ?? camera.keys
        let time = t + (camera.playbackKeys == nil ? 0 : camera.playbackOffset)
        return keys.count >= 2 ? SphereMath.pathValue(keys, at: time, projection: camera.projection)
            : SphereMath.effectiveView(camera.view, projection: camera.projection)
    }

    func stabilization(atSource source: Double) -> simd_double3x3 {
        guard let proxy, !proxy.stabilization.isEmpty else { return matrix_identity_double3x3 }
        let index = Int(((source - proxy.stabilizationStart) * proxy.fps).rounded())
        return proxy.stabilization[max(0, min(proxy.stabilization.count - 1, index))]
    }

    func renderState(proxySeconds: Double) -> SphereRenderState {
        let projection = camera?.projection ?? .flat
        guard let proxy else {
            return SphereRenderState(matrix: matrix_identity_double3x3, fov: 75, projection: projection)
        }
        let source = proxy.start + proxySeconds
        let view = SphereMath.effectiveView(self.view(atLocal: local(fromSource: source)), projection: projection)
        let matrix = stabilization(atSource: source) * SphereMath.matrix(for: view)
        return SphereRenderState(matrix: matrix, fov: view.fov, projection: projection)
    }

    /// Keyframe within half a frame of a local time.
    func keyIndex(atLocal t: Double, fps: Double) -> Int? {
        camera?.keys.firstIndex { abs($0.at - t) <= 0.5 / max(fps, 1) + 1e-6 }
    }

    var upAxis: simd_double3? { proxy?.gravity.map { -$0 } }

    // MARK: - Gestures (live)

    func dragChanged(dx: Double, dy: Double, phase: SphereGesturePhase, local t: Double, viewWidth: Double) {
        if phase == .began || dragStart == nil { dragStart = view(atLocal: t) }
        guard let start = dragStart else { return }
        let dpp = SphereMath.degreesPerPoint(fov: start.fov, viewWidthPoints: max(viewWidth, 1))
        // the picture follows the pointer: dragging right turns the camera left
        draft = SphereMath.turn(start, right: -dx * dpp, up: -dy * dpp, upAxis: upAxis)
    }

    func endDrag() { dragStart = nil }

    func zoom(by delta: Double, local t: Double) {
        let current = view(atLocal: t)
        var next = current
        next.fov = min(150, max(20, current.fov - delta))
        draft = next
    }

    // MARK: - Edits (JSON operations for EditorStore.apply)

    private static func viewJSON(_ v: SphereView) -> [String: JSONValue] {
        func r(_ x: Double) -> JSONValue { .number((x * 100).rounded() / 100) }
        return ["yaw": r(SphereMath.wrapYaw(v.yaw)), "pitch": r(v.pitch), "fov": r(v.fov), "roll": r(v.roll)]
    }

    /// Keys as plan JSON; `at` stays inside the clip (0…seconds) so `plan_camera` accepts them.
    private static func pathJSON(_ keys: [SphereKey], seconds: Double) -> JSONValue {
        .array(keys.sorted { $0.at < $1.at }.enumerated().map { index, key in
            var item = viewJSON(key.view)
            item["at"] = .number((max(0, min(seconds, key.at)) * 1000).rounded() / 1000)
            if key.cut && index > 0 { item["cut"] = .bool(true) }
            return .object(item)
        })
    }

    private func set(_ path: String, _ value: JSONValue) -> JSONValue {
        .object(["op": .string("set_field"), "id": .string(camera?.nodeID ?? ""),
                 "path": .string(path), "value": value])
    }

    /// Clears top-level yaw/pitch/fov/roll overrides so /view and /path are what renders.
    private func clearOverrides(raw: JSONValue?) -> [JSONValue] {
        ["yaw", "pitch", "fov", "roll"].compactMap { key in
            guard let raw, raw[key] != nil, raw[key] != .null else { return nil }
            return set("/\(key)", .null)
        }
    }

    /// Applies `view` at local time t: the whole clip's angle when it has no keyframes,
    /// otherwise the keyframe at t (updated, or a new one).
    func operationsForAim(_ view: SphereView, atLocal t: Double, fps: Double, raw: JSONValue?) -> [JSONValue] {
        guard var camera else { return [] }
        if camera.keys.count < 2 {
            camera.view = view
            camera.playbackKeys = nil
            self.camera = camera
            return clearOverrides(raw: raw) + [set("/view", .object(Self.viewJSON(view)))]
        }
        return writeKeys(setting: view, atLocal: t, fps: fps, cut: nil, raw: raw)
    }

    /// Adds (or updates) a keyframe at t with the current view.
    func operationsForAddKey(atLocal t: Double, fps: Double, raw: JSONValue?) -> [JSONValue] {
        guard var camera else { return [] }
        let current = view(atLocal: t)
        let clamped = max(0, min(camera.seconds, t))
        if camera.keys.count < 2 {
            if clamped < 0.1 {
                camera.keys = [SphereKey(at: 0, view: current, cut: false),
                               SphereKey(at: max(0.1, camera.seconds), view: current, cut: false)]
            } else {
                camera.keys = [SphereKey(at: 0, view: camera.view, cut: false),
                               SphereKey(at: clamped, view: current, cut: false)]
            }
            camera.playbackKeys = nil
            self.camera = camera
            return clearOverrides(raw: raw) + [set("/path", Self.pathJSON(camera.keys, seconds: camera.seconds))]
        }
        return writeKeys(setting: current, atLocal: t, fps: fps, cut: nil, raw: raw)
    }

    func operationsForToggleCut(atLocal t: Double, fps: Double, raw: JSONValue?) -> [JSONValue] {
        guard let camera, camera.keys.count >= 2 else { return [] }
        let index = keyIndex(atLocal: t, fps: fps)
        let cut = !(index.map { camera.keys[$0].cut } ?? false)
        return writeKeys(setting: view(atLocal: t), atLocal: t, fps: fps, cut: cut, raw: raw)
    }

    func operationsForDeleteKey(atLocal t: Double, fps: Double, raw: JSONValue?) -> [JSONValue] {
        guard var camera, let index = keyIndex(atLocal: t, fps: fps) else { return [] }
        camera.keys.remove(at: index)
        camera.playbackKeys = nil
        if camera.keys.count < 2 {
            let remaining = camera.keys.first?.view ?? camera.view
            camera.view = remaining
            camera.keys = []
            self.camera = camera
            return clearOverrides(raw: raw) + [set("/path", .null), set("/view", .object(Self.viewJSON(remaining)))]
        }
        self.camera = camera
        return [set("/path", Self.pathJSON(camera.keys, seconds: camera.seconds))]
    }

    func operationsForClearKeys(atLocal t: Double, raw: JSONValue?) -> [JSONValue] {
        guard var camera, !camera.keys.isEmpty else { return [] }
        let held = view(atLocal: t)
        camera.view = held
        camera.keys = []
        camera.playbackKeys = nil
        self.camera = camera
        return clearOverrides(raw: raw) + [set("/path", .null), set("/view", .object(Self.viewJSON(held)))]
    }

    private func writeKeys(setting view: SphereView, atLocal t: Double, fps: Double, cut: Bool?, raw: JSONValue?) -> [JSONValue] {
        guard var camera else { return [] }
        let at = max(0, min(camera.seconds, t))
        if let index = keyIndex(atLocal: at, fps: fps) {
            camera.keys[index].view = view
            if let cut { camera.keys[index].cut = cut }
        } else {
            let tooClose = camera.keys.contains { abs($0.at - at) < 0.1 }
            guard !tooClose else { return [] }  // the renderer needs keyframes 0.1 s apart
            camera.keys.append(SphereKey(at: at, view: view, cut: cut ?? false))
            camera.keys.sort { $0.at < $1.at }
        }
        camera.playbackKeys = nil
        self.camera = camera
        return clearOverrides(raw: raw) + [set("/path", Self.pathJSON(camera.keys, seconds: camera.seconds))]
    }

    func operationsForStabilize(_ choice: String) -> [JSONValue] {
        guard var camera else { return [] }
        camera.stabilize = choice
        self.camera = camera
        return [set("/stabilize", choice == "off" ? .null : .string(choice))]
    }

    func operationsForProjection(_ projection: SphereProjection) -> [JSONValue] {
        guard var camera else { return [] }
        camera.projection = projection
        self.camera = camera
        return [set("/projection", .string(projection == .tinyPlanet ? "tiny-planet" : "flat"))]
    }

    func scheduleZoomCommit(_ action: @escaping @MainActor () -> Void) {
        zoomCommit?.cancel()
        zoomCommit = Task { @MainActor in
            try? await Task.sleep(for: .milliseconds(450))
            guard !Task.isCancelled else { return }
            action()
        }
    }
}
