import SwiftUI

/// Organisational tracks (V1, V2, A1, ...). They never change what the renderer outputs.
extension EditorStore {
    var tracks: [EditorTrack] { snapshot?.tracks ?? EditorTrack.defaults }

    func addTrack(kind: String) {
        runTrackOp(["op": .string("add_track"), "kind": .string(kind)], reason: "Add \(kind) track")
    }

    func removeTrack(id: String) {
        guard let track = tracks.first(where: { $0.id == id }), track.userAdded else {
            notice = "Only tracks you added can be removed."
            return
        }
        if snapshot?.nodes.contains(where: { trackID(for: $0) == id }) == true {
            notice = "Move the clips off \(id) before removing it."
            return
        }
        runTrackOp(["op": .string("remove_track"), "id": .string(id)], reason: "Remove track \(id)")
    }

    func renameTrack(id: String, name: String) {
        runTrackOp(["op": .string("rename_track"), "id": .string(id), "name": .string(name)],
                   reason: "Rename track \(id)")
    }

    func setTrackLocked(id: String, locked: Bool) {
        runTrackOp(["op": .string("set_track_locked"), "id": .string(id), "locked": .bool(locked)],
                   reason: locked ? "Lock track \(id)" : "Unlock track \(id)")
    }

    /// Timeline drag between tracks: shown on the new track at once, saved in the background.
    func moveNode(_ node: EditorNode, toTrack trackID: String) {
        guard let target = tracks.first(where: { $0.id == trackID }), !target.locked else {
            notice = "Track \(trackID) is locked."
            return
        }
        Task {
            await apply([.object(["op": .string("set_node_track"), "id": .string(node.id), "track": .string(trackID)])],
                        reason: "Move to \(trackID)",
                        predict: OptimisticTimeline.moveToTrack(id: node.id, track: trackID))
        }
    }

    /// Stored assignment, else the default mapping.
    func trackID(for node: EditorNode) -> String {
        if let stored = node.track, !stored.isEmpty { return stored }
        return Self.defaultTrackID(kind: node.kind)
    }

    func isLocked(node: EditorNode) -> Bool {
        let id = trackID(for: node)
        return tracks.first(where: { $0.id == id })?.locked == true
    }

    static func defaultTrackID(kind: String) -> String {
        switch kind {
        case "segment": return "V1"
        case "cutaway", "generated_cutaway": return "V2"
        case "music", "music_drop": return "A2"
        case "sfx", "generated_sfx", "resolved_sfx", "nat", "bleep", "bleep_window", "generated_bleep": return "A3"
        default: return "V3"
        }
    }

    private func runTrackOp(_ op: [String: JSONValue], reason: String) {
        Task { await apply([.object(op)], reason: reason) }
    }
}
