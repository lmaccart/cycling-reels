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
    var assetKind: String?
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
    }

    static func decode(_ value: JSONValue) -> EditorSnapshot? {
        var normalized = value
        if normalized["projectConfig"] == nil, let config = normalized["config"] {
            normalized = normalized.setting(config, at: ["projectConfig"])
        }
        guard let data = normalized.jsonData() else { return nil }
        return try? JSONDecoder().decode(EditorSnapshot.self, from: data)
    }
}

struct ServiceFailure: Error, LocalizedError {
    let code: String
    let message: String
    let details: JSONValue?

    var errorDescription: String? { message }
}

struct ChatEntry: Identifiable, Hashable {
    let id = UUID()
    var role: String
    var text: String
    var state: String = "complete"
    var timestamp = Date()
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
}
