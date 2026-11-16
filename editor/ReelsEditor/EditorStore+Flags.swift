import Foundation

/// A flagged clip from flags.json: a library asset plus an optional in/out range and a note.
struct ClipFlag: Identifiable, Hashable, Sendable {
    var id: String
    var asset: String
    var inPoint: Double?
    var outPoint: Double?
    var note: String?

    init?(_ value: JSONValue) {
        guard let id = value["id"]?.stringValue, let asset = value["asset"]?.stringValue else { return nil }
        self.id = id
        self.asset = asset
        inPoint = value["in"]?.numberValue
        outPoint = value["out"]?.numberValue
        note = value["note"]?.stringValue
    }

    static func list(_ value: JSONValue?) -> [ClipFlag] {
        (value?["flags"]?.arrayValue ?? []).compactMap(ClipFlag.init)
    }

    var range: String? {
        guard let inPoint, let outPoint else { return nil }
        return "\(TimeFormat.clock(inPoint))–\(TimeFormat.clock(outPoint))"
    }
}

extension EditorStore {
    func loadFlags() {
        Task {
            if let value = try? await service.call("flags.list") { flags = ClipFlag.list(value) }
        }
    }

    func receiveFlags(_ payload: JSONValue) { flags = ClipFlag.list(payload) }

    func isFlagged(assetID: String?) -> Bool {
        guard let assetID else { return false }
        return flags.contains { $0.asset == assetID }
    }

    /// The range a new flag on the Source clip would cover: In–Out, In + 5 s, or the whole clip.
    var sourceFlagRange: (in: Double?, out: Double?) {
        guard let sourceIn else { return (nil, nil) }
        if let sourceOut, sourceOut > sourceIn + 0.1 { return (sourceIn, sourceOut) }
        return (sourceIn, sourceIn + 5)
    }

    func flagSource(note: String) {
        guard let assetID = selectedAssetID else {
            notice = "Choose a clip in the Library first."
            return
        }
        var params: [String: JSONValue] = ["asset": .string(assetID), "note": .string(note)]
        if !selectedAssetIsPhoto {
            let range = sourceFlagRange
            if let start = range.in { params["in"] = .number(start) }
            if let end = range.out { params["out"] = .number(end) }
        }
        Task {
            do {
                let value = try await service.call("flags.add", params: .object(params))
                flags = ClipFlag.list(value)
                notice = "Flagged"
            } catch { errorMessage = error.localizedDescription }
        }
    }

    func removeFlag(_ flag: ClipFlag) {
        Task {
            do {
                let value = try await service.call("flags.remove", params: .object(["id": .string(flag.id)]))
                flags = ClipFlag.list(value)
            } catch { errorMessage = error.localizedDescription }
        }
    }

    func renameFlag(_ flag: ClipFlag, note: String) {
        Task {
            do {
                let value = try await service.call("flags.update", params: .object(["id": .string(flag.id), "note": .string(note)]))
                flags = ClipFlag.list(value)
            } catch { errorMessage = error.localizedDescription }
        }
    }

    /// Shows a flagged clip in the Source monitor with its range marked In/Out.
    func openFlag(_ flag: ClipFlag) {
        guard let item = library.first(where: { $0["id"]?.stringValue == flag.asset }) else {
            notice = "That clip isn't in the library any more."
            return
        }
        selectAsset(item)
        sourceIn = flag.inPoint
        sourceOut = flag.outPoint
    }
}
