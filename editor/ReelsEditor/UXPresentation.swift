import Foundation

/// Pure helpers that turn raw library records and service events into human wording.
enum AssetPresentation {
    struct Moment: Equatable, Identifiable, Sendable {
        var id: String
        var title: String
        var range: String
        var label: String { range.isEmpty ? title : "\(title)  (\(range))" }
    }

    static func tags(_ item: JSONValue) -> JSONValue? {
        guard let tags = item["tags"], tags.objectValue != nil else { return nil }
        return tags
    }

    static func summary(_ item: JSONValue) -> String? {
        clean(tags(item)?["summary"]?.stringValue)
    }

    /// A 24+ character run of hex digits, with or without a leading date: how camera clones name files.
    static func looksLikeHash(_ name: String) -> Bool {
        let stem = (name as NSString).deletingPathExtension
        let compact = stem.replacingOccurrences(of: "-", with: "")
        return compact.count >= 24 && compact.allSatisfy { $0.isHexDigit }
    }

    static func title(_ item: JSONValue) -> String {
        if let summary = summary(item) { return summary }
        if let name = clean(item["originalName"]?.stringValue ?? item["name"]?.stringValue) {
            if !looksLikeHash(name) { return name }
            return item["kind"]?.stringValue == "photo" ? "Untagged photo" : "Untagged clip"
        }
        return "Untitled clip"
    }

    static func duration(_ item: JSONValue) -> Double? {
        item["probe"]?["duration"]?.numberValue ?? item["duration"]?.numberValue
    }

    static func hasTranscript(_ item: JSONValue) -> Bool {
        if let path = item["transcript"]?.stringValue, !path.isEmpty { return true }
        return (item["audio"]?["speechSeconds"]?.numberValue ?? 0) > 0
    }

    /// "Feb 7" (adds the year when it is not `currentYear`) from an ISO timestamp.
    static func dateLabel(_ item: JSONValue, currentYear: Int) -> String? {
        guard let created = item["created"]?.stringValue, created.count >= 10 else { return nil }
        let parts = created.prefix(10).split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3, (1...12).contains(parts[1]) else { return nil }
        let months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        let base = "\(months[parts[1] - 1]) \(parts[2])"
        return parts[0] == currentYear ? base : "\(base), \(parts[0])"
    }

    static func clock(_ seconds: Double) -> String {
        let total = Int(seconds.rounded())
        return "\(total / 60):" + String(format: "%02d", total % 60)
    }

    /// Second line of a library row, without the speech icon (the view adds that).
    static func detailParts(_ item: JSONValue, currentYear: Int) -> [String] {
        var parts: [String] = []
        if let date = dateLabel(item, currentYear: currentYear) { parts.append(date) }
        if let seconds = duration(item), seconds > 0 { parts.append(clock(seconds)) }
        let tags = tags(item)
        let shot = clean(tags?["shot"]?.stringValue)
        let type = clean(tags?["type"]?.stringValue)
        if let shot { parts.append(shot) }
        if let type, type != shot { parts.append(type) }
        if shot == nil, type == nil, item["kind"]?.stringValue == "photo" { parts.append("photo") }
        return parts
    }

    static func searchText(_ item: JSONValue) -> String {
        var pieces: [String?] = [
            summary(item), item["originalName"]?.stringValue, item["id"]?.stringValue,
            tags(item)?["type"]?.stringValue, tags(item)?["shot"]?.stringValue, tags(item)?["notes"]?.stringValue,
            tags(item)?["people"]?.stringValue
        ]
        for moment in tags(item)?["moments"]?.arrayValue ?? [] { pieces.append(moment["label"]?.stringValue) }
        for hook in tags(item)?["hooks"]?.arrayValue ?? [] { pieces.append(hook.stringValue) }
        return pieces.compactMap { $0 }.joined(separator: " ")
    }

    static func matches(_ item: JSONValue, query: String) -> Bool {
        let words = query.lowercased().split(whereSeparator: \.isWhitespace)
        guard !words.isEmpty else { return true }
        let haystack = searchText(item).lowercased()
        return words.allSatisfy { haystack.contains($0) }
    }

    /// The empty-results overlay: only when neither local clips nor search hits exist and no query is in flight.
    static func showsNoResults(clipCount: Int, hitCount: Int, searchInFlight: Bool = false) -> Bool {
        clipCount == 0 && hitCount == 0 && !searchInFlight
    }

    static func moments(_ item: JSONValue?) -> [Moment] {
        guard let item, let list = tags(item)?["moments"]?.arrayValue else { return [] }
        return list.enumerated().compactMap { index, moment in
            guard let id = moment["id"]?.stringValue, !id.isEmpty else { return nil }
            let label = clean(moment["label"]?.stringValue) ?? "Moment \(index + 1)"
            var range = ""
            if let start = moment["in"]?.numberValue, let end = moment["out"]?.numberValue {
                range = "\(clock(start))–\(clock(end))"
            }
            return Moment(id: id, title: label, range: range)
        }
    }

    /// The moment a plan value refers to: an ID, or a legacy index into the moments list.
    static func selectedMomentID(_ value: JSONValue, moments: [Moment]) -> String? {
        if let id = value.stringValue, moments.contains(where: { $0.id == id }) { return id }
        if let index = value.numberValue.map(Int.init), moments.indices.contains(index) { return moments[index].id }
        return nil
    }

    private static func clean(_ value: String?) -> String? {
        guard let value else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? nil : trimmed
    }
}

/// Which service events deserve a status-bar message. Nil means stay quiet.
enum EventNotice {
    static func jobMessage(_ payload: JSONValue) -> String? {
        let kind = payload["kind"]?.stringValue ?? ""
        let state = payload["state"]?.stringValue ?? ""
        let noun: String
        switch kind {
        case "preview": noun = "Preview"
        case "export": noun = "Render"
        default: return nil
        }
        switch state {
        case "running":
            guard var progress = payload["progress"]?.numberValue else { return nil }
            if progress <= 1 { progress *= 100 }
            let percent = Int(min(100, max(0, progress)).rounded())
            return "\(kind == "export" ? "Rendering" : "Preparing preview")… \(percent)%"
        case "completed": return kind == "preview" ? "Preview ready" : nil
        case "failed": return "\(noun) failed"
        default: return nil
        }
    }
}
