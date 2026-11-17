import CryptoKit
import Foundation

struct TimelinePlacement: Hashable, Sendable {
    var id: String?
    var insert: JSONValue?
    var track: String
    var atFrame: Int

    static func moving(id: String, track: String, atFrame: Int) -> TimelinePlacement {
        TimelinePlacement(id: id, insert: nil, track: track, atFrame: atFrame)
    }

    static func inserting(_ value: JSONValue, track: String, atFrame: Int) -> TimelinePlacement {
        TimelinePlacement(id: nil, insert: value, track: track, atFrame: atFrame)
    }

    var wireValue: JSONValue {
        var object: [String: JSONValue] = [
            "track": .string(track),
            "atFrame": .number(Double(max(0, atFrame))),
        ]
        if let id { object["id"] = .string(id) }
        if let insert { object["insert"] = insert }
        return .object(object)
    }
}

struct TimelineDropResolution: Hashable, Sendable {
    var placement: TimelinePlacement?
    var rejection: String?

    static func accepted(_ placement: TimelinePlacement) -> TimelineDropResolution {
        TimelineDropResolution(placement: placement, rejection: nil)
    }

    static func rejected(_ reason: String) -> TimelineDropResolution {
        TimelineDropResolution(placement: nil, rejection: reason)
    }
}

struct TimelineOverwriteRegion: Hashable, Sendable {
    var nodeID: String
    var track: String
    var startFrame: Int
    var endFrame: Int
}

/// Local predictions of what a timeline edit will do, shown the instant the mouse is
/// released. The service's snapshot replaces them as soon as the edit is saved, so
/// these only need to put clips where the user dropped them, not be frame-exact.
enum OptimisticTimeline {
    typealias Change = ([EditorNode]) -> [EditorNode]
    private typealias OverwritePart = (
        node: EditorNode, oldStart: Int, oldEnd: Int, newStart: Int, ownerStart: Int, ownerEnd: Int
    )

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
    /// Removes primary segments, optionally closing the gap in the primary sequence.
    /// Child nodes are removed with their parent; free overlays stay in place.
    static func remove(ids: Set<String>, ripple: Bool) -> Change {
        { nodes in
            let removed = Set(nodes.filter { ids.contains($0.id) && $0.kind.lowercased() == "segment" }
                .flatMap { subtree($0.id, in: nodes) })
            guard !removed.isEmpty else { return nodes }
            let removedSegments = segments(nodes).filter { removed.contains($0.id) }
            let surviving = nodes.filter { !removed.contains($0.id) }
            guard ripple, !removedSegments.isEmpty else { return surviving }
            let ordered = segments(nodes)
            var starts: [String: Int] = [:]
            for segment in ordered where !removed.contains(segment.id) {
                let shift = removedSegments.reduce(into: 0) { total, deleted in
                    if deleted.startFrame < segment.startFrame {
                        total += max(0, deleted.endFrame - deleted.startFrame)
                    }
                }
                if shift > 0 { starts[segment.id] = max(0, segment.startFrame - shift) }
            }
            return relayout(surviving, newStarts: starts)
        }
    }

    /// Shared Python/Swift ID contract for overwrite fragments and insertions.
    static func overwriteID(editID: String, origin: String, start: Int, end: Int) -> String {
        let message = "reels-overwrite-v1\0\(editID.lowercased())\0\(origin)\0\(start)\0\(end)"
        let digest = SHA256.hash(data: Data(message.utf8))
        return digest.map { String(format: "%02x", $0) }.joined()
    }

    static func placementDuration(_ placement: TimelinePlacement, nodes: [EditorNode], fps: Int) -> Int? {
        guard fps > 0 else { return nil }
        if let id = placement.id, let node = nodes.first(where: { $0.id == id }) {
            return max(1, node.endFrame - node.startFrame)
        }
        guard let insert = placement.insert,
              let asset = insert["asset"]?.stringValue,
              !asset.isEmpty else { return nil }
        if let duration = insert["duration"]?.numberValue {
            return max(1, Int((duration * Double(fps)).rounded(.toNearestOrEven)))
        }
        let sourceIn = insert["in"]?.numberValue ?? 0
        guard let sourceOut = insert["out"]?.numberValue else { return nil }
        let speed = insert["speed"]?.numberValue ?? 1
        guard sourceOut > sourceIn, speed.isFinite, speed > 0, !asset.contains("\0") else { return nil }
        return max(1, Int((((sourceOut - sourceIn) / speed) * Double(fps)).rounded(.toNearestOrEven)))
    }

    static func hasSameTrackPlacementOverlap(_ placements: [TimelinePlacement],
                                             nodes: [EditorNode], fps: Int) -> Bool {
        let timed = placements.compactMap { placement -> (String, Int, Int)? in
            guard let duration = placementDuration(placement, nodes: nodes, fps: fps) else { return nil }
            let start = max(0, placement.atFrame)
            return (placement.track, start, start + duration)
        }
        for group in Dictionary(grouping: timed, by: { $0.0 }).values {
            let ordered = group.sorted { $0.1 == $1.1 ? $0.2 < $1.2 : $0.1 < $1.1 }
            for pair in zip(ordered, ordered.dropFirst()) where pair.1.1 < pair.0.2 {
                return true
            }
        }
        return false
    }

    /// Intervals on destination tracks that the overwrite will replace.
    static func overwriteRegions(
        placements: [TimelinePlacement],
        nodes: [EditorNode],
        fps: Int
    ) -> [TimelineOverwriteRegion] {
        let movingIDs = Set(placements.compactMap(\.id))
        var regions: [TimelineOverwriteRegion] = []
        for placement in placements {
            guard let duration = placementDuration(placement, nodes: nodes, fps: fps) else { continue }
            let start = max(0, placement.atFrame)
            let end = start + duration
            for node in nodes where !movingIDs.contains(node.id)
                && isOverwriteRoot(node, movingIDs: movingIDs)
                && (node.track ?? defaultTrackID(for: node)) == placement.track
                && node.startFrame < end && node.endFrame > start {
                regions.append(TimelineOverwriteRegion(
                    nodeID: node.id, track: placement.track,
                    startFrame: max(start, node.startFrame), endFrame: min(end, node.endFrame)
                ))
            }
        }
        return regions
    }

    /// Predict overwrite placement and deterministic survivor IDs without changing
    /// plan authority. The server snapshot replaces this result after validation.
    static func overwrite(placements: [TimelinePlacement], editID: String, fps: Int) -> Change {
        { original in
            guard !editID.contains("\0"), fps > 0, !placements.isEmpty else { return original }
            let byID = Dictionary(uniqueKeysWithValues: original.map { ($0.id, $0) })
            let movingIDs = placements.compactMap(\.id)
            guard original.allSatisfy({ !$0.id.contains("\0") }),
                  Set(movingIDs).count == movingIDs.count,
                  movingIDs.allSatisfy({ byID[$0].map(isOverwriteClip) == true }),
                  placements.allSatisfy({ ($0.id != nil) != ($0.insert != nil) }),
                   placements.allSatisfy({
                       !$0.track.contains("\0") && $0.atFrame >= 0
                           && ($0.insert?["asset"]?.stringValue?.contains("\0") != true)
                   }),
                   !hasSameTrackPlacementOverlap(placements, nodes: original, fps: fps) else {
                 return original
             }

            let regions = overwriteRegions(placements: placements, nodes: original, fps: fps)
            let destinations = Dictionary(grouping: regions, by: \.track)
            var partsByID: [String: [OverwritePart]] = [:]
            var output: [EditorNode] = []
            let movingIDSet = Set(movingIDs)
            let roots = original.filter { isOverwriteRoot($0, movingIDs: movingIDSet) }
            let rootIDs = Set(roots.map(\.id))

            for root in roots {
                if let placement = placements.first(where: { $0.id == root.id }) {
                    let duration = max(1, root.endFrame - root.startFrame)
                    var moved = root
                    let newStart = max(0, placement.atFrame)
                    let promotesCutaway = root.kind.lowercased() == "cutaway" && placement.track == "V1"
                    if promotesCutaway {
                        moved.kind = "segment"
                        moved.lane = "primary"
                        moved.parentId = nil
                    }
                    moved.track = placement.track
                    moved.startFrame = newStart
                    moved.endFrame = newStart + duration
                    moved.frames = duration
                    var raw = moved.raw.objectValue ?? [:]
                    if promotesCutaway {
                        raw.removeValue(forKey: "parentId")
                        raw.removeValue(forKey: "at")
                        raw.removeValue(forKey: "for")
                        raw["lane"] = .string("primary")
                    }
                    if promotesCutaway || raw["timeline"] != nil {
                        var timeline = raw["timeline"]?.objectValue ?? [:]
                        timeline["track"] = .string(placement.track)
                        timeline["startFrame"] = .number(Double(newStart))
                        timeline["durationFrames"] = .number(Double(duration))
                        raw["timeline"] = .object(timeline)
                        moved.raw = .object(raw)
                    }
                    partsByID[root.id] = [(
                        node: moved, oldStart: root.startFrame, oldEnd: root.endFrame,
                        newStart: newStart, ownerStart: root.startFrame, ownerEnd: root.endFrame
                    )]
                    continue
                }

                let cuts = mergeIntervals((destinations[root.track ?? defaultTrackID(for: root)] ?? [])
                    .map { ($0.startFrame, $0.endFrame) })
                let survivors = subtract(root.startFrame..<root.endFrame, cuts: cuts)
                partsByID[root.id] = survivors.enumerated().map { index, interval -> OverwritePart in
                    var fragment = root
                    fragment.startFrame = interval.lowerBound
                    fragment.endFrame = interval.upperBound
                    fragment.frames = max(0, interval.upperBound - interval.lowerBound)
                    if index > 0 {
                        fragment.id = "frag-" + overwriteID(
                            editID: editID, origin: root.id,
                            start: interval.lowerBound, end: interval.upperBound
                        )
                    }
                    retimeFragment(&fragment, from: root, interval: interval, fps: fps)
                    return (
                        node: fragment, oldStart: interval.lowerBound, oldEnd: interval.upperBound,
                        newStart: interval.lowerBound, ownerStart: interval.lowerBound,
                        ownerEnd: interval.upperBound
                    )
                }
            }

            // Preserve all untouched nodes, replace overwritten roots with their
            // surviving fragments, and reparent editable children deterministically.
            let indexByID = Dictionary(uniqueKeysWithValues: original.enumerated().map { ($0.element.id, $0.offset) })
            let orderedNodes = original.sorted {
                let leftDepth = nodeDepth($0, byID: byID)
                let rightDepth = nodeDepth($1, byID: byID)
                return leftDepth == rightDepth
                    ? (indexByID[$0.id] ?? 0) < (indexByID[$1.id] ?? 0)
                    : leftDepth < rightDepth
            }
            for node in orderedNodes {
                if rootIDs.contains(node.id) {
                    output.append(contentsOf: partsByID[node.id]?.map(\.node) ?? [])
                    continue
                }
                guard !movingIDs.contains(node.id) else {
                    partsByID[node.id] = []
                    continue
                }
                guard let parentID = node.parentId, let parents = partsByID[parentID] else {
                    output.append(node)
                    partsByID[node.id] = [identityPart(node)]
                    continue
                }
                let copies = childParts(node, parentParts: parents, editID: editID, fps: fps)
                partsByID[node.id] = copies
                output.append(contentsOf: copies.map(\.node))
            }

            for (index, placement) in placements.enumerated() {
                if placement.id != nil {
                    continue // moved clips were inserted at their original authority position
                }
                if let insert = placement.insert,
                   let added = insertedNode(insert, placement: placement, index: index,
                                            editID: editID, fps: fps) {
                    output.append(added)
                }
            }

            // Keep layout ordering deterministic, while retaining same-time order.
            return output.enumerated().sorted {
                if $0.element.startFrame != $1.element.startFrame {
                    return $0.element.startFrame < $1.element.startFrame
                }
                return $0.offset < $1.offset
            }.map(\.element)
        }
    }

    private static func isOverwriteClip(_ node: EditorNode) -> Bool {
        ["segment", "cutaway"].contains(node.kind.lowercased())
    }

    private static func isOverwriteRoot(_ node: EditorNode, movingIDs: Set<String>) -> Bool {
        let kind = node.kind.lowercased()
        guard isOverwriteClip(node) else { return false }
        return kind != "cutaway" || node.parentId == nil || movingIDs.contains(node.id)
    }

    private static func defaultTrackID(for node: EditorNode) -> String {
        if let track = node.track, !track.isEmpty { return track }
        return switch node.kind.lowercased() {
        case "segment": "V1"
        case "cutaway", "generated_cutaway": "V2"
        default: "V1"
        }
    }

    private static func identityPart(_ node: EditorNode) -> OverwritePart {
        (node: node, oldStart: node.startFrame, oldEnd: node.endFrame,
         newStart: node.startFrame, ownerStart: node.startFrame, ownerEnd: node.endFrame)
    }

    private static func mergeIntervals(_ intervals: [(Int, Int)]) -> [Range<Int>] {
        let ordered = intervals.filter { $0.1 > $0.0 }.sorted {
            $0.0 == $1.0 ? $0.1 < $1.1 : $0.0 < $1.0
        }
        var merged: [Range<Int>] = []
        for (start, end) in ordered {
            if let last = merged.last, start <= last.upperBound {
                merged[merged.count - 1] = last.lowerBound..<max(last.upperBound, end)
            } else {
                merged.append(start..<end)
            }
        }
        return merged
    }

    private static func subtract(_ source: Range<Int>, cuts: [Range<Int>]) -> [Range<Int>] {
        guard source.upperBound > source.lowerBound else { return [] }
        var cursor = source.lowerBound
        var out: [Range<Int>] = []
        for cut in cuts where cut.upperBound > cursor && cut.lowerBound < source.upperBound {
            if cut.lowerBound > cursor { out.append(cursor..<min(cut.lowerBound, source.upperBound)) }
            cursor = max(cursor, cut.upperBound)
            if cursor >= source.upperBound { break }
        }
        if cursor < source.upperBound { out.append(cursor..<source.upperBound) }
        return out
    }

    private static func retimeFragment(_ fragment: inout EditorNode, from source: EditorNode,
                                      interval: Range<Int>, fps: Int) {
        let deltaStart = interval.lowerBound - source.startFrame
        let length = interval.upperBound - interval.lowerBound
        let speed = source.speed ?? source.raw["speed"]?.numberValue ?? 1
        if source.isPhoto {
            let duration = Double(length) / Double(fps)
            fragment.raw = fragment.raw.setting(.number(duration), at: ["duration"])
            return
        }
        if let sourceIn = source.sourceIn ?? source.raw["in"]?.numberValue {
            let originalOut = source.sourceOut ?? source.raw["out"]?.numberValue
            let limitOut = max(
                sourceIn,
                originalOut ?? sourceIn
                    + Double(source.endFrame - source.startFrame) * speed / Double(fps)
            )
            let candidateIn = sourceIn + Double(deltaStart) * speed / Double(fps)
            let candidateOut = sourceIn
                + Double(interval.upperBound - source.startFrame) * speed / Double(fps)
            let nextIn = interval.lowerBound == source.startFrame
                ? sourceIn : min(max(candidateIn, sourceIn), limitOut)
            let nextOut: Double
            if interval.upperBound == source.endFrame, let originalOut {
                nextOut = originalOut
            } else {
                nextOut = min(max(candidateOut, nextIn), limitOut)
            }
            fragment.sourceIn = nextIn
            fragment.sourceOut = nextOut
            fragment.raw = fragment.raw.setting(.number(nextIn), at: ["in"])
            fragment.raw = fragment.raw.setting(.number(nextOut), at: ["out"])
        }
    }

    private static func nodeDepth(_ node: EditorNode, byID: [String: EditorNode]) -> Int {
        var depth = 0
        var parentID = node.parentId
        var visited: Set<String> = [node.id]
        while let id = parentID, let parent = byID[id], visited.insert(id).inserted {
            depth += 1
            parentID = parent.parentId
        }
        return depth
    }

    private static func childParts(_ child: EditorNode, parentParts: [OverwritePart],
                                   editID: String, fps: Int) -> [OverwritePart] {
        var out: [OverwritePart] = []
        for parent in parentParts {
            let isPoint = child.startFrame == child.endFrame
            let intersectionStart = max(child.startFrame, parent.oldStart)
            let intersectionEnd = min(child.endFrame, parent.oldEnd)
            guard (isPoint && child.startFrame >= parent.oldStart && child.startFrame < parent.oldEnd)
                    || intersectionEnd > intersectionStart else { continue }
            var copy = child
            copy.parentId = parent.node.id
            let oldStart = isPoint ? child.startFrame : intersectionStart
            let oldEnd = isPoint ? child.endFrame : intersectionEnd
            copy.startFrame = parent.newStart + oldStart - parent.oldStart
            copy.endFrame = parent.newStart + oldEnd - parent.oldStart
            copy.frames = copy.endFrame - copy.startFrame
            if !isPoint {
                retimeFragment(&copy, from: child, interval: oldStart..<oldEnd, fps: fps)
            }
            if !out.isEmpty && !child.isReadOnly {
                copy.id = "node-" + overwriteID(
                    editID: editID, origin: child.id,
                    start: parent.ownerStart, end: parent.ownerEnd
                )
            }
            out.append((
                node: copy, oldStart: oldStart, oldEnd: oldEnd, newStart: copy.startFrame,
                ownerStart: parent.ownerStart, ownerEnd: parent.ownerEnd
            ))
            if child.isReadOnly { break }
        }
        return out
    }

    private static func insertedNode(_ insert: JSONValue, placement: TimelinePlacement,
                                     index: Int, editID: String, fps: Int) -> EditorNode? {
        guard let asset = insert["asset"]?.stringValue,
              !asset.isEmpty, !asset.contains("\0"),
              let length = placementDuration(placement, nodes: [], fps: fps) else { return nil }
        let start = max(0, placement.atFrame)
        let end = start + length
        let speed = insert["speed"]?.numberValue ?? 1
        let photo = insert["duration"] != nil || insert["assetKind"]?.stringValue == "photo"
        return EditorNode(
            id: "clip-" + overwriteID(editID: editID, origin: "insert:\(index)", start: start, end: end),
            kind: "segment", lane: "primary", parentId: nil,
            startFrame: start, endFrame: end, frames: length, raw: insert,
            assetId: asset, sourceIn: insert["in"]?.numberValue,
            sourceOut: insert["out"]?.numberValue,
            sourceDuration: insert["sourceDuration"]?.numberValue,
            speed: speed, assetKind: photo ? "photo" : "video",
            mediaKind: photo ? "photo" : "video", generated: false, readOnly: false,
            track: placement.track
        )
    }

    /// Applies a prediction to a snapshot, keeping its duration in step.
    static func apply(_ change: Change, to snapshot: EditorSnapshot) -> EditorSnapshot {
        var next = snapshot
        next.nodes = change(snapshot.nodes)
        let end = segments(next.nodes).map(\.endFrame).max() ?? next.durationFrames
        next.durationFrames = max(snapshot.durationFrames, max(end, 0))
        return next
    }
}
