import SwiftUI
import AppKit

extension NSPasteboard.PasteboardType {
    /// Drag payload for a library track: the track slug as a string.
    static let reelsMusicTrack = NSPasteboard.PasteboardType("com.cycling.reels.music-track")
}

extension EditorStore {
    /// The music node in the current plan, if any.
    var musicNode: EditorNode? {
        snapshot?.nodes.first { $0.kind == "music" || $0.kind == "generated_music" }
    }

    /// Sets the reel's music track through the normal transaction path (undoable).
    /// Other music fields (volume, cutOnBeat, ...) are kept; `start` belongs to the old song, so it is
    /// replaced by `startSeconds`, or left unset (the renderer picks the song's loudest stretch) when nil.
    func setMusic(track slug: String, startSeconds: Double?) {
        guard snapshot != nil else { return }
        var object: [String: JSONValue] = [:]
        if let raw = musicNode?.raw.objectValue {
            for key in ["volume", "cutOnBeat", "instagram", "audioVolume", "videoVolume"] {
                if let value = raw[key] { object[key] = value }
            }
        }
        object["track"] = .string(slug)
        if let startSeconds { object["start"] = .number((startSeconds * 100).rounded() / 100) }
        Task {
            await apply([.object([
                "op": .string("set_field"),
                "id": .string("document"),
                "path": .string("/music"),
                "value": .object(object)
            ])], reason: "Set music to \(slug)")
        }
    }

    func removeMusic() {
        guard snapshot != nil, musicNode != nil else { return }
        Task {
            await apply([.object([
                "op": .string("set_field"),
                "id": .string("document"),
                "path": .string("/music"),
                "value": .null
            ])], reason: "Remove music")
        }
    }
}
