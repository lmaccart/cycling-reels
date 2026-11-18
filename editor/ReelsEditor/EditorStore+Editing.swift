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
        let segments = OptimisticTimeline.segments(nodes).filter {
            frame > $0.startFrame && frame < $0.endFrame && canSplit(node: $0)
        }
        // Nothing selected: as in Premiere, ⌘K cuts every clip under the playhead.
        guard !selectedIDs.isEmpty else { return segments.map(\.id) }
        // Anything selected: only the selected clips are cut, never the other tracks. A clip's
        // linked ride audio stands for the clip; a selected caption, sound or text, or an id
        // that no longer exists, cuts nothing (the caller shows a notice) instead of everything.
        let clips = selectedClipIDs(nodes: nodes, selectedIDs: selectedIDs)
        return segments.filter { clips.contains($0.id) }.map(\.id)
    }

    /// The clips a selection stands for: selected segments, and the owners of selected linked audio.
    static func selectedClipIDs(nodes: [EditorNode], selectedIDs: Set<String>) -> Set<String> {
        var ids = Set<String>()
        for node in nodes where selectedIDs.contains(node.id) {
            switch node.kind.lowercased() {
            case "segment": ids.insert(node.id)
            case "source_audio": if let owner = node.parentId { ids.insert(owner) }
            default: break
            }
        }
        return ids
    }

    static func flaggedDrop(id: String, flags: [ClipFlag], library: [JSONValue])
        -> (assetID: String, metadata: JSONValue, sourceIn: Double?, sourceOut: Double?)? {
        guard id.hasPrefix("flag:"),
              let flag = flags.first(where: { $0.id == String(id.dropFirst(5)) }),
              let metadata = library.first(where: { $0["id"]?.stringValue == flag.asset }) else { return nil }
        return (flag.asset, metadata, flag.inPoint, flag.outPoint)
    }
    static func isPositioned(_ plan: JSONValue) -> Bool {
        plan["timeline"]?["version"]?.numberValue == 1
    }

    static func canSplit(node: EditorNode) -> Bool {
        node.kind.lowercased() == "segment" && !node.isReadOnly
    }
}

extension EditorStore {
    func deleteSegments(_ ids: [String], ripple: Bool) {
        guard !ids.isEmpty else { return }
        let positioned = TimelineEditingTargets.isPositioned(snapshot?.plan ?? .object([:]))
        let operation = ripple ? TimelineEditingOperation.rippleRemove(ids: ids) : .object([
            "op": .string("remove_segments"), "ids": .array(ids.map(JSONValue.string))
        ])
        let effectiveRipple = ripple || !positioned
        let canPredict = OptimisticTimeline.canRemove(ids: Set(ids), ripple: effectiveRipple,
                                                       positioned: positioned, nodes: snapshot?.nodes ?? [])
        let prediction = canPredict
            ? OptimisticTimeline.remove(ids: Set(ids), ripple: effectiveRipple, positioned: positioned)
            : nil
        enqueue([operation], reason: ripple ? "Ripple delete" : "Delete", predict: prediction)
    }

    func duplicateSegments(_ ids: [String]) {
        guard !ids.isEmpty else { return }
        Task { await apply([TimelineEditingOperation.duplicate(ids: ids)], reason: "Duplicate clips") }
    }

    func copySegments(_ ids: [String]) {
        guard let state = snapshot else { copiedSegments = []; return }
        copiedSegments = OptimisticTimeline.segments(state.nodes)
            .filter { ids.contains($0.id) }.map { $0.raw }
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

    var splitTargetsAtPlayhead: [String] {
        let ids = TimelineEditingTargets.split(nodes: snapshot?.nodes ?? [],
                                               selectedIDs: selectedIDs, at: playheadFrame)
        return eligibleSplitIDs(ids)
    }

    private func eligibleSplitIDs(_ ids: [String]) -> [String] {
        let nodes = snapshot?.nodes ?? []
        return ids.filter { id in
            guard let node = nodes.first(where: { $0.id == id }),
                  TimelineEditingTargets.canSplit(node: node) else { return false }
            let linkedAudioLocked = tracks.first(where: { $0.id == "A1" })?.locked == true
                && !node.isPhoto
                && nodes.contains { $0.kind.lowercased() == "source_audio" && $0.parentId == id }
            return !isLocked(node: node) && !linkedAudioLocked
        }
    }

    func splitSegments(_ ids: [String], at frame: Int) {
        let validIDs = eligibleSplitIDs(ids)
        guard !validIDs.isEmpty else {
            notice = "Nothing to split here: move the playhead inside the selected clip (and unlock its track)."
            return
        }
        let operations = validIDs.map { id in JSONValue.object([
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
            if presentSpeedDialog(alert) == .alertFirstButtonReturn, let speed = Double(field.stringValue),
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
