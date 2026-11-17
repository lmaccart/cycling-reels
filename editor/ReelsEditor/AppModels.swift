import Foundation

enum JSONValue: Codable, Hashable, Sendable {
    case null
    case bool(Bool)
    case number(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() { self = .null }
        else if let value = try? container.decode(Bool.self) { self = .bool(value) }
        else if let value = try? container.decode(Double.self) { self = .number(value) }
        else if let value = try? container.decode(String.self) { self = .string(value) }
        else if let value = try? container.decode([JSONValue].self) { self = .array(value) }
        else { self = .object(try container.decode([String: JSONValue].self)) }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null: try container.encodeNil()
        case .bool(let value): try container.encode(value)
        case .number(let value): try container.encode(value)
        case .string(let value): try container.encode(value)
        case .array(let value): try container.encode(value)
        case .object(let value): try container.encode(value)
        }
    }

    subscript(_ key: String) -> JSONValue? {
        guard case .object(let values) = self else { return nil }
        return values[key]
    }

    var objectValue: [String: JSONValue]? {
        guard case .object(let value) = self else { return nil }
        return value
    }

    var arrayValue: [JSONValue]? {
        guard case .array(let value) = self else { return nil }
        return value
    }

    var stringValue: String? {
        guard case .string(let value) = self else { return nil }
        return value
    }

    var numberValue: Double? {
        guard case .number(let value) = self else { return nil }
        return value
    }

    var boolValue: Bool? {
        guard case .bool(let value) = self else { return nil }
        return value
    }

    var displayValue: String {
        switch self {
        case .null: "—"
        case .bool(let value): value ? "On" : "Off"
        case .number(let value): value.formatted(.number.precision(.fractionLength(0...3)))
        case .string(let value): value
        case .array(let value): "\(value.count) items"
        case .object(let value): "\(value.count) fields"
        }
    }

    func jsonData(pretty: Bool = false) -> Data? {
        let encoder = JSONEncoder()
        if pretty { encoder.outputFormatting = [.prettyPrinted, .sortedKeys] }
        return try? encoder.encode(self)
    }
}

struct Revisions: Codable, Hashable, Sendable {
    var document: String
    var library: String
    var renderer: String

    static let empty = Revisions(document: "", library: "", renderer: "")
}

struct EditorNode: Codable, Hashable, Identifiable, Sendable {
    var id: String
    var kind: String
    var lane: String
    var parentId: String?
    var startFrame: Int
    var endFrame: Int
    var frames: Int
    var raw: JSONValue
    var assetId: String?
    var sourceIn: Double?
    var sourceOut: Double?
    var sourceDuration: Double?
    var speed: Double?
    var assetKind: String?
    var mediaKind: String?
    var generated: Bool?
    var readOnly: Bool?
    /// Stored track assignment ("V2", "A3", ...). nil = default mapping (see EditorStore.trackID(for:)).
    var track: String?

    var title: String {
        if let text = raw["text"]?.stringValue, !text.isEmpty { return text }
        if kind.lowercased().contains("caption"), let words = raw["words"]?.arrayValue {
            let text = words.compactMap { $0["text"]?.stringValue ?? $0["w"]?.stringValue }.joined(separator: " ")
            if !text.isEmpty { return text }
        }
        if let asset = raw["asset"]?.stringValue { return asset }
        if let assetId, !assetId.isEmpty { return assetId }
        if let label = raw["label"]?.stringValue { return label }
        return kind.capitalized
    }

    var isReadOnly: Bool {
        if readOnly == true { return true }
        let kindText = kind.lowercased()
        return generated == true && (kindText.contains("caption") || kindText.contains("transcript"))
    }

    var isPhoto: Bool {
        NativeMediaKind.isPhoto(kind: assetKind, path: raw["media"]?.stringValue ?? raw["asset"]?.stringValue)
    }
}

enum NativeMediaKind {
    private static let stillExtensions: Set<String> = [
        "jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "webp", "gif"
    ]

    static func isPhoto(kind: String?, path: String?) -> Bool {
        let value = (kind ?? "").lowercased()
        if value.contains("photo") || value.contains("image") || value.contains("still") { return true }
        return stillExtensions.contains(URL(fileURLWithPath: path ?? "").pathExtension.lowercased())
    }
}

struct EditorTrack: Codable, Identifiable, Hashable, Sendable {
    var id: String        // "V1", "A2", ...
    var canonicalId: String? = nil
    var kind: String      // "video" | "audio"
    var name: String
    var index: Int
    var locked: Bool
    var userAdded: Bool

    var isVideo: Bool { kind == "video" }

    static var defaults: [EditorTrack] {
        [("V1", "video", 1), ("V2", "video", 2), ("V3", "video", 3),
         ("A1", "audio", 1), ("A2", "audio", 2), ("A3", "audio", 3)].map {
            EditorTrack(id: $0.0, kind: $0.1, name: $0.0, index: $0.2, locked: false, userAdded: false)
        }
    }
}

struct TimelineCapabilities: Codable, Hashable, Sendable {
    var version: Int
    var overwrite: Bool
    var crossTrack: Bool
    var libraryInsert: Bool
    var multiMove: Bool
    var cutawayPromotion: Bool
    var insert: Bool
    var idAlgorithm: String
}

private struct AnyCodingKey: CodingKey {
    let stringValue: String
    let intValue: Int?

    init(_ string: String) {
        stringValue = string
        intValue = nil
    }

    init?(stringValue: String) {
        self.init(stringValue)
    }

    init?(intValue: Int) {
        stringValue = String(intValue)
        self.intValue = intValue
    }
}

struct EditorSnapshot: Codable, Hashable, Sendable {
    var documentId: String
    var revisions: Revisions
    var plan: JSONValue
    var nodes: [EditorNode]
    var fps: Double
    var durationFrames: Int
    var warnings: [String]
    var preview: JSONValue
    var projectConfig: JSONValue?
    var inspectorSchema: JSONValue?
    /// Organisational tracks (V1..., A1...). Absent in older services: derived default list.
    var tracks: [EditorTrack] = EditorTrack.defaults
    /// Unknown service fields survive delta merges and future protocol additions.
    var extraFields: [String: JSONValue] = [:]
    var editingDurationFrames: Int {
        max(durationFrames, Int(extraFields["editingDurationFrames"]?.numberValue ?? Double(durationFrames)))
    }
    var isPreviewEmpty: Bool { preview["state"]?.stringValue == "empty" }
    var snapshotToken: String? { extraFields["snapshotToken"]?.stringValue }
    var timelineCapabilities: TimelineCapabilities? {
        guard let data = extraFields["timelineCapabilities"]?.jsonData() else { return nil }
        return try? JSONDecoder().decode(TimelineCapabilities.self, from: data)
    }

    enum CodingKeys: String, CodingKey {
        case documentId, revisions, plan, nodes, fps, durationFrames, warnings, preview
        case projectConfig, inspectorSchema, tracks
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        documentId = try c.decode(String.self, forKey: .documentId)
        revisions = try c.decode(Revisions.self, forKey: .revisions)
        plan = try c.decode(JSONValue.self, forKey: .plan)
        nodes = try c.decode([EditorNode].self, forKey: .nodes)
        fps = try c.decode(Double.self, forKey: .fps)
        durationFrames = try c.decode(Int.self, forKey: .durationFrames)
        warnings = try c.decode([String].self, forKey: .warnings)
        preview = try c.decode(JSONValue.self, forKey: .preview)
        projectConfig = try c.decodeIfPresent(JSONValue.self, forKey: .projectConfig)
        inspectorSchema = try c.decodeIfPresent(JSONValue.self, forKey: .inspectorSchema)
        if let list = try? c.decodeIfPresent([EditorTrack].self, forKey: .tracks), !list.isEmpty {
            tracks = list
        } else {
            tracks = EditorTrack.defaults
        }
        let all = try decoder.container(keyedBy: AnyCodingKey.self)
        let known = Set([
            "documentId", "revisions", "plan", "nodes", "fps", "durationFrames",
            "warnings", "preview", "projectConfig", "inspectorSchema", "tracks",
        ])
        extraFields = [:]
        for key in all.allKeys where !known.contains(key.stringValue) {
            extraFields[key.stringValue] = try all.decode(JSONValue.self, forKey: key)
        }
    }

    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: AnyCodingKey.self)
        try c.encode(documentId, forKey: AnyCodingKey("documentId"))
        try c.encode(revisions, forKey: AnyCodingKey("revisions"))
        try c.encode(plan, forKey: AnyCodingKey("plan"))
        try c.encode(nodes, forKey: AnyCodingKey("nodes"))
        try c.encode(fps, forKey: AnyCodingKey("fps"))
        try c.encode(durationFrames, forKey: AnyCodingKey("durationFrames"))
        try c.encode(warnings, forKey: AnyCodingKey("warnings"))
        try c.encode(preview, forKey: AnyCodingKey("preview"))
        try c.encodeIfPresent(projectConfig, forKey: AnyCodingKey("projectConfig"))
        try c.encodeIfPresent(inspectorSchema, forKey: AnyCodingKey("inspectorSchema"))
        try c.encode(tracks, forKey: AnyCodingKey("tracks"))
        let known = Set([
            "documentId", "revisions", "plan", "nodes", "fps", "durationFrames",
            "warnings", "preview", "projectConfig", "inspectorSchema", "tracks",
        ])
        for (key, value) in extraFields where !known.contains(key) {
            try c.encode(value, forKey: AnyCodingKey(key))
        }
    }

    static func decode(_ value: JSONValue) -> EditorSnapshot? {
        var normalized = value
        if normalized["projectConfig"] == nil, let config = normalized["config"] {
            normalized = normalized.setting(config, at: ["projectConfig"])
        }
        guard let data = normalized.jsonData() else { return nil }
        return try? JSONDecoder().decode(EditorSnapshot.self, from: data)
    }

    static func applyingDelta(_ delta: JSONValue, to base: EditorSnapshot) -> EditorSnapshot? {
        guard delta["type"]?.stringValue == "document.delta",
              delta["documentId"]?.stringValue == base.documentId,
              let baseSnapshotToken = base.snapshotToken,
              delta["baseSnapshotToken"]?.stringValue == baseSnapshotToken,
              let nextSnapshotToken = delta["snapshotToken"]?.stringValue,
              let revisionsData = delta["baseRevisions"]?.jsonData(),
              let baseRevisions = try? JSONDecoder().decode(Revisions.self, from: revisionsData),
              baseRevisions == base.revisions,
              let nextRevisionsData = delta["revisions"]?.jsonData(),
              let nextRevisions = try? JSONDecoder().decode(Revisions.self, from: nextRevisionsData),
              nextRevisions.document != base.revisions.document,
              Set(base.nodes.map(\.id)).count == base.nodes.count,
              let encoded = try? JSONEncoder().encode(base),
              var root = try? JSONDecoder().decode(JSONValue.self, from: encoded) else {
            return nil
        }

        guard let currentPlan = root["plan"],
              let patchedPlan = currentPlan.applyingJSONPatch(delta["planPatch"]?.arrayValue ?? []) else {
            return nil
        }
        root = root.setting(patchedPlan, at: ["plan"])

        var byID = Dictionary(uniqueKeysWithValues: base.nodes.map { ($0.id, $0) })
        for idValue in delta["removedIds"]?.arrayValue ?? [] {
            guard let id = idValue.stringValue else { return nil }
            byID.removeValue(forKey: id)
        }
        for value in delta["changedNodes"]?.arrayValue ?? [] {
            guard let data = value.jsonData(),
                  let node = try? JSONDecoder().decode(EditorNode.self, from: data) else {
                return nil
            }
            byID[node.id] = node
        }
        guard let orderValues = delta["nodeOrder"]?.arrayValue else { return nil }
        let order = orderValues.compactMap(\.stringValue)
        guard order.count == orderValues.count,
              Set(order).count == order.count,
              Set(order) == Set(byID.keys) else {
            return nil
        }
        let orderedNodes = order.compactMap { byID[$0] }
        guard orderedNodes.count == order.count,
              let nodesData = try? JSONEncoder().encode(orderedNodes),
              let nodesValue = try? JSONDecoder().decode(JSONValue.self, from: nodesData),
              var fields = root.objectValue else {
            return nil
        }
        fields["nodes"] = nodesValue
        for (key, value) in delta["fields"]?.objectValue ?? [:] {
            fields[key] = value
        }
        for keyValue in delta["removedFields"]?.arrayValue ?? [] {
            guard let key = keyValue.stringValue else { return nil }
            fields.removeValue(forKey: key)
        }
        fields["documentId"] = .string(base.documentId)
        fields["snapshotToken"] = .string(nextSnapshotToken)
        if let revisions = delta["revisions"] {
            fields["revisions"] = revisions
        }
        return decode(.object(fields))
    }
}

struct ServiceFailure: Error, LocalizedError {
    let code: String
    let message: String
    let details: JSONValue?

    var errorDescription: String? { message }
}

struct SearchHit: Identifiable, Hashable {
    var id: String
    var title: String
    var evidence: String
    var assetId: String
    var sourceIn: Double
    var sourceOut: Double
    var related: Bool
    var raw: JSONValue
    var relatedReason: String? = nil
    var sourceRanges: [SearchRangeEvidence] = []
}

struct SearchRangeEvidence: Identifiable, Hashable {
    var id: String { "\(sourceIn):\(sourceOut)" }
    var sourceIn: Double
    var sourceOut: Double
    var evidence: String
    var reason: String
    var highlights: [JSONValue]
}

enum UnicodeCodepointOffsets {
    static func stringRange(in text: String, start: Int, end: Int) -> Range<String.Index>? {
        let scalars = text.unicodeScalars
        guard start >= 0, end > start, end <= scalars.count else { return nil }
        let lowerScalar = scalars.index(scalars.startIndex, offsetBy: start)
        let upperScalar = scalars.index(scalars.startIndex, offsetBy: end)
        guard let lower = String.Index(lowerScalar, within: text),
              let upper = String.Index(upperScalar, within: text) else { return nil }
        return lower..<upper
    }

    static func utf16Range(in text: String, start: Int, end: Int) -> NSRange? {
        guard let range = stringRange(in: text, start: start, end: end) else { return nil }
        let lower = range.lowerBound.utf16Offset(in: text)
        let upper = range.upperBound.utf16Offset(in: text)
        return NSRange(location: lower, length: upper - lower)
    }
}

extension JSONValue {
    func value(at components: [String]) -> JSONValue? {
        guard let first = components.first else { return self }
        if case .array(let values) = self, let index = Int(first), values.indices.contains(index) {
            return values[index].value(at: Array(components.dropFirst()))
        }
        return self[first]?.value(at: Array(components.dropFirst()))
    }

    func setting(_ value: JSONValue, at components: [String]) -> JSONValue {
        guard let first = components.first else { return value }
        if case .array(var values) = self, let index = Int(first), values.indices.contains(index) {
            values[index] = values[index].setting(value, at: Array(components.dropFirst()))
            return .array(values)
        }
        var values = objectValue ?? [:]
        values[first] = (values[first] ?? .object([:])).setting(value, at: Array(components.dropFirst()))
        return .object(values)
    }

    func applyingJSONPatch(_ patch: [JSONValue]) -> JSONValue? {
        var result = self
        for operation in patch {
            guard let name = operation["op"]?.stringValue,
                  let pointer = operation["path"]?.stringValue,
                  ["add", "replace", "remove"].contains(name) else {
                return nil
            }
            let components = pointer.split(separator: "/", omittingEmptySubsequences: false)
                .dropFirst()
                .map {
                    String($0)
                        .replacingOccurrences(of: "~1", with: "/")
                        .replacingOccurrences(of: "~0", with: "~")
                }
            guard let updated = result.replacing(
                components: ArraySlice(components), operation: name, value: operation["value"]
            ) else {
                return nil
            }
            result = updated
        }
        return result
    }

    private func replacing(components: ArraySlice<String>, operation: String,
                           value: JSONValue?) -> JSONValue? {
        guard let first = components.first else {
            return operation == "remove" ? nil : value
        }
        let rest = components.dropFirst()
        switch self {
        case .object(var values):
            if rest.isEmpty {
                if operation == "remove" {
                    guard values.removeValue(forKey: first) != nil else { return nil }
                } else {
                    guard let value else { return nil }
                    if operation == "replace" && values[first] == nil { return nil }
                    values[first] = value
                }
            } else {
                guard let child = values[first],
                      let replacement = child.replacing(components: rest, operation: operation, value: value) else {
                    return nil
                }
                values[first] = replacement
            }
            return .object(values)
        case .array(var values):
            if rest.isEmpty {
                if operation == "add" {
                    let index = first == "-" ? values.count : Int(first)
                    guard let index, index >= 0, index <= values.count, let value else { return nil }
                    values.insert(value, at: index)
                } else {
                    guard let index = Int(first), values.indices.contains(index) else { return nil }
                    if operation == "remove" {
                        values.remove(at: index)
                    } else {
                        guard let value else { return nil }
                        values[index] = value
                    }
                }
            } else {
                guard let index = Int(first), values.indices.contains(index),
                      let replacement = values[index].replacing(
                        components: rest, operation: operation, value: value
                      ) else {
                    return nil
                }
                values[index] = replacement
            }
            return .array(values)
        default:
            return nil
        }
    }
}
