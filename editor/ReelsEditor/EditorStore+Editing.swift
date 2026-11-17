import AppKit

/// Wire values for the editor operations that mutate primary segments.
enum TimelineEditingOperation {
    static func duplicate(ids: [String]) -> JSONValue {
        .object(["op": .string("duplicate_segments"), "ids": .array(ids.map(JSONValue.string))])
    }

    static func rippleRemove(ids: [String]) -> JSONValue {
        .object(["op": .string("ripple_remove"), "ids": .array(ids.map(JSONValue.string))])
    }

    static func paste(values: [JSONValue], atFrame: Int, track: String?) -> JSONValue {
        .object([
            "op": .string("paste_segments"),
            "values": .array(values),
            "atFrame": .number(Double(atFrame)),
            "track": track.map(JSONValue.string) ?? .null
        ])
    }

    static func setEnabled(ids: [String], enabled: Bool) -> JSONValue {
        .object(["op": .string("set_enabled"), "ids": .array(ids.map(JSONValue.string)), "enabled": .bool(enabled)])
    }
}

enum TimelineEditingTargets {
    static func split(nodes: [EditorNode], selectedIDs: Set<String>, at frame: Int) -> [String] {
        let segments = OptimisticTimeline.segments(nodes).filter { frame > $0.startFrame && frame < $0.endFrame }
        let selected = segments.filter { selectedIDs.contains($0.id) }
        return (selectedIDs.isEmpty ? segments : selected).map(\.id)
    }

    static func flaggedDrop(id: String, flags: [ClipFlag], library: [JSONValue])
        -> (assetID: String, metadata: JSONValue, sourceIn: Double?, sourceOut: Double?)? {
        guard id.hasPrefix("flag:"),
              let flag = flags.first(where: { $0.id == String(id.dropFirst(5)) }),
              let metadata = library.first(where: { $0["id"]?.stringValue == flag.asset }) else { return nil }
        return (flag.asset, metadata, flag.inPoint, flag.outPoint)
    }
}

extension EditorStore {
    func deleteSegments(_ ids: [String], ripple: Bool) {
        guard !ids.isEmpty else { return }
        let operation = ripple ? TimelineEditingOperation.rippleRemove(ids: ids) : .object([
            "op": .string("remove_segments"), "ids": .array(ids.map(JSONValue.string))
        ])
        Task {
            await apply([operation], reason: ripple ? "Ripple delete" : "Delete",
                        predict: OptimisticTimeline.remove(ids: Set(ids), ripple: ripple))
        }
    }

    func duplicateSegments(_ ids: [String]) {
        guard !ids.isEmpty else { return }
        Task { await apply([TimelineEditingOperation.duplicate(ids: ids)], reason: "Duplicate clips") }
    }

    func copySegments(_ ids: [String]) {
        guard let state = snapshot else { return }
        let copied = OptimisticTimeline.segments(state.nodes).filter { ids.contains($0.id) }.map { $0.raw }
        guard !copied.isEmpty else { return }
        copiedSegments = copied
    }

    func pasteSegments() {
        guard !copiedSegments.isEmpty else { return }
        Task {
            await apply([TimelineEditingOperation.paste(values: copiedSegments, atFrame: playheadFrame, track: nil)],
                        reason: "Paste clips")
        }
    }

    func setSegmentsEnabled(_ ids: [String], enabled: Bool) {
        guard !ids.isEmpty else { return }
        Task { await apply([TimelineEditingOperation.setEnabled(ids: ids, enabled: enabled)], reason: enabled ? "Enable clips" : "Disable clips") }
    }

    func splitSegments(_ ids: [String], at frame: Int) {
        guard !ids.isEmpty else { return }
        let operations = ids.map { id in JSONValue.object([
            "op": .string("split_segment"), "id": .string(id), "atFrame": .number(Double(frame))
        ]) }
        Task { await apply(operations, reason: "Split clips") }
    }

    func flaggedDrop(_ dragID: String) -> (assetID: String, metadata: JSONValue, sourceIn: Double?, sourceOut: Double?)? {
        TimelineEditingTargets.flaggedDrop(id: dragID, flags: flags, library: library)
    }
    func performTimelineContextAction(_ action: String, nodeID: String, atFrame: Int) {
        let primaryIDs = OptimisticTimeline.segments(snapshot?.nodes ?? []).map(\.id)
        let selected = primaryIDs.filter(selectedIDs.contains)
        let ids = selected.isEmpty ? [nodeID] : selected
        if action == "Paste" {
            pasteSegments()
            return
        }
        guard let node = snapshot?.nodes.first(where: { $0.id == nodeID }) else { return }
        switch action {
        case "Split here":
            splitSegments([nodeID], at: atFrame)
        case "Split at playhead":
            splitAtPlayhead()
        case "Delete":
            if node.kind.lowercased() == "segment" { deleteSegments(ids, ripple: false) }
            else { deleteSelection() }
        case "Ripple delete":
            if node.kind.lowercased() == "segment" { deleteSegments(ids, ripple: true) }
        case "Duplicate":
            duplicateSegments(ids)
        case "Copy":
            copySegments(ids)
        case "Paste":
            pasteSegments()
        case "Disable":
            setSegmentsEnabled(ids, enabled: false)
        case "Enable":
            setSegmentsEnabled(ids, enabled: true)
        case "0.25×", "0.5×", "1×", "2×":
            let speed = Double(action.dropLast()) ?? 1
            setSegmentSpeed(ids, speed: speed)
        case "Custom…":
            let alert = NSAlert()
            alert.messageText = "Custom clip speed"
            alert.informativeText = "Enter a speed from 0.25× to 2×."
            let field = NSTextField(string: "1")
            field.frame = NSRect(x: 0, y: 0, width: 180, height: 24)
            alert.accessoryView = field
            alert.addButton(withTitle: "Apply")
            alert.addButton(withTitle: "Cancel")
            if alert.runModal() == .alertFirstButtonReturn, let speed = Double(field.stringValue),
               (0.25...2).contains(speed) {
                setSegmentSpeed(ids, speed: speed)
            }
        case "Flag source range":
            guard let asset = node.assetId ?? node.raw["asset"]?.stringValue else { return }
            var params: [String: JSONValue] = ["asset": .string(asset), "note": .string("Timeline clip")]
            if let start = node.sourceIn { params["in"] = .number(start) }
            if let end = node.sourceOut { params["out"] = .number(end) }
            Task {
                do { flags = ClipFlag.list(try await service.call("flags.add", params: .object(params))) }
                catch { errorMessage = error.localizedDescription }
            }
        case "Aim 360":
            selectNode(nodeID, additive: false)
            openSphereAim()
        case "Reveal in Source":
            guard let assetID = node.assetId ?? node.raw["asset"]?.stringValue,
                  let asset = library.first(where: { $0["id"]?.stringValue == assetID }) else { return }
            selectAsset(asset)
            sourceIn = node.sourceIn
            sourceOut = node.sourceOut
        default:
            break
        }
    }

    private func setSegmentSpeed(_ ids: [String], speed: Double) {
        guard speed.isFinite, (0.25...2).contains(speed), !ids.isEmpty else { return }
        let operations = ids.map { id in JSONValue.object([
            "op": .string("set_field"), "id": .string(id),
            "path": .string("/speed"), "value": .number(speed)
        ]) }
        Task { await apply(operations, reason: "Set clip speed") }
    }
    private static var copiedSegmentValues: [JSONValue] = []
    var copiedSegments: [JSONValue] {
        get { Self.copiedSegmentValues }
        set { Self.copiedSegmentValues = newValue }
    }
}
