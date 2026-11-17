import Foundation

/// The open 360 aim panel follows the timeline: it rebinds to the selected 360 clip, or,
/// with nothing selected, to the 360 clip under the playhead (so after a cut it moves to
/// whichever piece the playhead is on).
extension EditorStore {
    /// A split retains the selected ID on the left. Move that selection to its new
    /// right piece before panel observers can seek back to the left.
    func followSphereAimAfterSplit(previous: [EditorNode], next: [EditorNode]) {
        guard sphereAim.isActive else { return }
        for old in previous where selectedIDs.contains(old.id) {
            guard let left = next.first(where: { $0.id == old.id }),
                  left.endFrame < old.endFrame,
                  let right = next.first(where: { candidate in
                      candidate.id != old.id && candidate.assetId == old.assetId && candidate.kind == old.kind
                          && candidate.parentId == old.parentId && candidate.startFrame == left.endFrame
                          && candidate.endFrame == old.endFrame
                          && !previous.contains(where: { $0.id == candidate.id })
                  }),
                  playheadFrame >= right.startFrame && playheadFrame < right.endFrame,
                  SphereAimModel.isSphereClip(right, library: library) else { continue }
            selectedIDs.remove(old.id)
            selectedIDs.insert(right.id)
        }
    }

    /// The clip the aim panel should show. `current` (the clip it shows now) wins while the
    /// playhead is still on it, so playing through a cutaway doesn't steal the panel.
    static func sphereAimTarget(nodes: [EditorNode], selectedIDs: Set<String>, playheadFrame: Int,
                                library: [JSONValue], current: String?) -> EditorNode? {
        if !selectedIDs.isEmpty {
            return nodes.first { selectedIDs.contains($0.id) && SphereAimModel.isSphereClip($0, library: library) }
        }
        let under = nodes.filter {
            playheadFrame >= $0.startFrame && playheadFrame < max($0.endFrame, $0.startFrame + 1)
                && SphereAimModel.isSphereClip($0, library: library)
        }
        if let current, let node = under.first(where: { $0.id == current }) { return node }
        // a cutaway sits over its segment, so it's the picture under the playhead
        return under.first { $0.kind.lowercased().contains("cutaway") } ?? under.first
    }

    /// Rebinds the open aim panel when the selection, the playhead or a cut changed which
    /// 360 clip it should show. Returns true when it rebinds.
    @discardableResult
    func followSphereAim() -> Bool {
        guard sphereAim.isActive, let nodes = snapshot?.nodes else { return false }
        let current = sphereAim.boundNodeID
        guard let target = Self.sphereAimTarget(nodes: nodes, selectedIDs: selectedIDs, playheadFrame: playheadFrame,
                                                library: library, current: current),
              target.id != current else { return false }
        if playheadFrame < target.startFrame || playheadFrame >= target.endFrame {
            setPlayhead(target.startFrame)
        }
        sphereAim.boundNodeID = target.id  // now, so the next change doesn't open it twice
        Task { await sphereAim.open(nodeID: target.id, store: self) }
        return true
    }
}
