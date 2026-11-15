import Foundation

enum InspectorSchemaSupport {
    static let projectConfigTargets = [
        "config.render", "config.audio", "config.brand", "config.sfx", "config.captions"
    ]

    static func targetNames(
        node: EditorNode?,
        isProject: Bool,
        assetKind: String?,
        available: JSONValue
    ) -> [String] {
        let keys: [String]
        if isProject || node == nil {
            keys = [
                "document", "post", "hook", "endCard", "titleCard"
            ] + projectConfigTargets
        } else if let node {
            let kind = node.kind.lowercased().replacingOccurrences(of: "_", with: "")
            let photo = isPhoto(node: node, assetKind: assetKind)
            if kind == "segment" {
                keys = ["segment.\(photo ? "photo" : "video")"]
            } else if kind == "cutaway" {
                keys = ["cutaway.\(photo ? "photo" : "video")"]
            } else {
                switch kind {
                case "text", "caption": keys = [node.parentId == nil ? "textBeat.reel" : "textBeat.segment"]
                case "stats", "stat": keys = ["stat"]
                case "exhibits", "exhibit": keys = [node.parentId == nil ? "exhibit.reel" : "exhibit.segment"]
                case "punch": keys = ["punch.manual"]
                case "blur": keys = ["blur.patch"]
                case "bleep": keys = ["bleep.window"]
                case "musicdrop": keys = ["musicDrop"]
                case "sfx", "generatedsfx": keys = [node.parentId == nil ? "sfxCue.reel" : "sfxCue.segment"]
                case "path", "route": keys = ["route"]
                default: keys = []
                }
            }
        } else {
            keys = []
        }
        let targets = available["targets"]?.objectValue ?? [:]
        return keys.filter { targets[$0] != nil }
    }

    static func isPhoto(node: EditorNode, assetKind: String?) -> Bool {
        NativeMediaKind.isPhoto(
            kind: node.assetKind ?? assetKind ?? node.raw["type"]?.stringValue ?? node.raw["kind"]?.stringValue,
            path: node.raw["media"]?.stringValue ?? node.raw["asset"]?.stringValue
        )
    }

    static func components(_ path: String) -> [String] {
        let value = path.hasPrefix("/") ? String(path.dropFirst()) : path
        guard !value.isEmpty else { return [] }
        return value.split(separator: "/").map {
            String($0).replacingOccurrences(of: "~1", with: "/").replacingOccurrences(of: "~0", with: "~")
        }
    }

    static func relativeComponents(path: String, scope: String) -> [String] {
        let pathParts = components(path)
        let scopeParts = components(scope)
        guard !scopeParts.isEmpty, pathParts.count >= scopeParts.count else { return pathParts }
        for (actual, expected) in zip(pathParts, scopeParts) {
            if expected.hasPrefix("{") && expected.hasSuffix("}") { continue }
            if actual != expected { return pathParts }
        }
        return Array(pathParts.dropFirst(scopeParts.count))
    }

    static func isVisible(_ descriptor: JSONValue, fields: JSONValue, scope: String, scoped: Bool) -> Bool {
        guard let condition = descriptor["visibleWhen"] else { return true }
        guard let conditionPath = condition["path"]?.stringValue else { return true }
        let components = scoped
            ? relativeComponents(path: conditionPath, scope: scope)
            : components(conditionPath)
        let current = fields.value(at: components)
        if let exists = condition["exists"]?.boolValue {
            let isSet = current != nil && current != .null
            return exists ? isSet : !isSet
        }
        if let expected = condition["equals"] { return current == expected }
        return true
    }

    static func defaultValue(for descriptor: JSONValue) -> JSONValue {
        if let value = descriptor["default"] { return value }
        switch descriptor["type"]?.stringValue {
        case "boolean": return .bool(false)
        case "number", "timing":
            return .number(descriptor["min"]?.numberValue ?? 0)
        case "enum":
            return .string(descriptor["choices"]?.arrayValue?.first?.stringValue ?? "")
        case "collection", "keyframe": return .array([])
        case "group":
            var values: [String: JSONValue] = [:]
            for field in descriptor["fields"]?.arrayValue ?? [] {
                guard let last = components(field["path"]?.stringValue ?? "").last else { continue }
                values[last] = defaultValue(for: field)
            }
            return .object(values)
        default: return .string("")
        }
    }

    static func defaultObject(for target: JSONValue) -> (JSONValue, Bool) {
        defaultObject(for: target["groups"]?.arrayValue?.flatMap { $0["fields"]?.arrayValue ?? [] } ?? [],
                      scope: target["scopePath"]?.stringValue ?? "")
    }

    static func defaultObject(for fields: [JSONValue], scope: String) -> (JSONValue, Bool) {
        var object: JSONValue = .object([:])
        var valid = true
        for field in fields {
            let path = field["path"]?.stringValue ?? ""
            let relative = relativeComponents(path: path, scope: scope)
                .filter { !($0.hasPrefix("{") && $0.hasSuffix("}")) }
            guard !relative.isEmpty else { continue }
            if field["required"]?.boolValue == true,
               field["default"] == nil,
               !(field["id"]?.stringValue == "text" && field["type"]?.stringValue == "text") {
                valid = false
            }
            var value = defaultValue(for: field)
            if field["id"]?.stringValue == "text", field["type"]?.stringValue == "text",
               field["required"]?.boolValue == true, field["default"] == nil {
                value = .string("New text")
            } else if field["id"]?.stringValue == "for", field["default"] == nil {
                value = .number(max(0.1, field["min"]?.numberValue ?? 1))
            }
            object = object.setting(value, at: relative)
        }
        return (object, valid)
    }

    static func defaultCollectionItem(for descriptor: JSONValue) -> JSONValue {
        let scope = descriptor["path"]?.stringValue ?? ""
        let fields = descriptor["itemFields"]?.arrayValue ?? []
        guard !fields.isEmpty else { return .object([:]) }
        var entries: [([String], JSONValue)] = []
        for field in fields {
            let suffix = relativeComponents(path: field["path"]?.stringValue ?? "", scope: scope)
                .filter { !($0.hasPrefix("{") && $0.hasSuffix("}")) }
            entries.append((suffix, defaultValue(for: field)))
        }
        if entries.allSatisfy({ $0.0.count == 1 && Int($0.0[0]) != nil }) {
            let indexes = entries.compactMap { Int($0.0[0]) }
            var values = Array(repeating: JSONValue.null, count: (indexes.max() ?? -1) + 1)
            for (path, value) in entries {
                if let index = Int(path[0]), values.indices.contains(index) { values[index] = value }
            }
            return .array(values)
        }
        if entries.allSatisfy({ $0.0.isEmpty }) { return entries[0].1 }
        var value: JSONValue = .object([:])
        for (path, item) in entries where !path.isEmpty {
            value = value.setting(item, at: path)
        }
        return value
    }
}
