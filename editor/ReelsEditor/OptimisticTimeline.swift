import Foundation

/// Local predictions of what a timeline edit will do, shown the instant the mouse is
/// released. The service's snapshot replaces them as soon as the edit is saved, so
/// these only need to put clips where the user dropped them, not be frame-exact.
enum OptimisticTimeline {
    typealias Change = ([EditorNode]) -> [EditorNode]

    struct DropSlot: Equatable {
        /// Segment the dropped one goes in front of; nil = the end of the reel.
        let beforeID: String?
    }

    /// Primary segments in timeline order.
    static func segments(_ nodes: [EditorNode]) -> [EditorNode] {
        nodes.filter { $0.parentId == nil && $0.kind.lowercased() == "segment" }
            .sorted { $0.startFrame < $1.startFrame }
    }

    /// IDs of `rootID` and everything nested under it.
    static func subtree(_ rootID: String, in nodes: [EditorNode]) -> Set<String> {
        var ids: Set<String> = [rootID]
        var grew = true
        while grew {
            grew = false
            for node in nodes where !ids.contains(node.id) {
                if let parent = node.parentId, ids.contains(parent) {
                    ids.insert(node.id)
                    grew = true
                }
            }
        }
        return ids
    }

    /// Shifts each segment (with its children) to the new start frames.
    private static func relayout(_ nodes: [EditorNode], newStarts: [String: Int]) -> [EditorNode] {
        var delta: [String: Int] = [:]
        for (id, start) in newStarts {
            guard let node = nodes.first(where: { $0.id == id }), start != node.startFrame else { continue }
            for member in subtree(id, in: nodes) { delta[member] = start - node.startFrame }
        }
        guard !delta.isEmpty else { return nodes }
        return nodes.map { node in
            guard let shift = delta[node.id] else { return node }
            var moved = node
            moved.startFrame += shift
            moved.endFrame += shift
            return moved
        }
    }

    /// `reorder_segments`: move one segment before `beforeID` (or to the end).
    static func reorderSegment(id: String, beforeID: String?) -> Change {
        { nodes in
            let ordered = segments(nodes)
            guard let moving = ordered.first(where: { $0.id == id }) else { return nodes }
            var rest = ordered.filter { $0.id != id }
            let index = beforeID.flatMap { target in rest.firstIndex(where: { $0.id == target }) } ?? rest.count
            rest.insert(moving, at: index)
            var cursor = ordered.first?.startFrame ?? 0
            var starts: [String: Int] = [:]
            for segment in rest {
                starts[segment.id] = cursor
                cursor += max(1, segment.endFrame - segment.startFrame)
            }
            return relayout(nodes, newStarts: starts)
        }
    }

    /// Where a dragged segment lands: before the first other segment whose middle is
    /// right of the dropped clip's middle (nil = at the end). Returns nil when that is
    /// the slot it already had, so dropping a clip back in place does nothing.
    static func dropSlot(moving: EditorNode, landingStart: Int, segments ordered: [EditorNode]) -> DropSlot? {
        let length = moving.endFrame - moving.startFrame
        let middle = Double(landingStart) + Double(length) / 2
        let others = ordered.filter { $0.id != moving.id }
        let before = others.first { Double($0.startFrame + $0.endFrame) / 2 > middle }
        guard let currentIndex = ordered.firstIndex(where: { $0.id == moving.id }) else { return nil }
        let currentNext = ordered.indices.contains(currentIndex + 1) ? ordered[currentIndex + 1].id : nil
        if before?.id == currentNext { return nil }
        return DropSlot(beforeID: before?.id)
    }

    /// `move_overlay` and other free placements: put the item (and its children) at `startFrame`.
    static func move(id: String, toFrame startFrame: Int) -> Change {
        { nodes in relayout(nodes, newStarts: [id: max(0, startFrame)]) }
    }

    /// Overlay/cutaway resize: the item spans exactly `start..<end`.
    static func resize(id: String, start: Int, end: Int) -> Change {
        { nodes in
            nodes.map { node in
                guard node.id == id else { return node }
                var sized = node
                sized.startFrame = max(0, start)
                sized.endFrame = max(sized.startFrame + 1, end)
                sized.frames = sized.endFrame - sized.startFrame
                return sized
            }
        }
    }

    /// Segment trim: the segment keeps its place, takes `length` frames, and later
    /// segments ripple to close (or open) the gap.
    static func trimSegment(id: String, length: Int) -> Change {
        { nodes in
            let ordered = segments(nodes)
            guard let index = ordered.firstIndex(where: { $0.id == id }) else { return nodes }
            let target = ordered[index]
            let newLength = max(1, length)
            let delta = newLength - (target.endFrame - target.startFrame)
            guard delta != 0 else { return nodes }
            var starts: [String: Int] = [:]
            for later in ordered[(index + 1)...] { starts[later.id] = later.startFrame + delta }
            let resized = nodes.map { node -> EditorNode in
                guard node.id == id else { return node }
                var trimmed = node
                trimmed.endFrame = trimmed.startFrame + newLength
                trimmed.frames = newLength
                return trimmed
            }
            return relayout(resized, newStarts: starts)
        }
    }

    /// `set_node_track`.
    static func moveToTrack(id: String, track: String) -> Change {
        { nodes in
            nodes.map { node in
                guard node.id == id else { return node }
                var moved = node
                moved.track = track
                return moved
            }
        }
    }

    /// Applies a prediction to a snapshot, keeping its duration in step.
    static func apply(_ change: Change, to snapshot: EditorSnapshot) -> EditorSnapshot {
        var next = snapshot
        next.nodes = change(snapshot.nodes)
        let end = segments(next.nodes).map(\.endFrame).max() ?? next.durationFrames
        next.durationFrames = max(end, 0)
        return next
    }
}
