import Foundation

struct ExportFile: Decodable, Hashable, Sendable {
    let kind: String          // "equirect" | "flat"
    let path: String
    let bytes: Int?
    let label: String?
}

struct SheetGrid: Decodable, Hashable, Sendable {
    let cols: Int
    let rows: Int
    let times: [Double]
    // Exact tile geometry when the backend knows it (pixels in the sheet image).
    let perSheet: Int?, tileW: Double?, tileH: Double?, gutter: Double?, header: Double?, sheetWidth: Double?
}

struct Angle: Decodable, Hashable, Sendable {
    let yaw: Double?, pitch: Double?, fov: Double?, roll: Double?
}

struct Tags: Decodable, Hashable, Sendable {
    let type: String?
    let shot: String?
    let quality: Int?
    let people: String?
    let summary: String?
    let hooks: [String]?
    let location: String?
    let notes: String?
}

struct Asset: Decodable, Hashable, Identifiable, Sendable {
    let id: String
    let kind: String          // "video" | "photo"
    let media: String
    let originalName: String?
    let created: String?
    let duration: Double?
    let width: Int?
    let height: Int?
    let fps: Double?
    let is360: Bool
    let projection: String?
    let thumb: String?
    let sheets: [String]?
    let sheetGrid: SheetGrid?
    let tags: Tags?
    let angles: [String: Angle]?
    let exports: [ExportFile]?

    var isPhoto: Bool { kind == "photo" }
    var mediaURL: URL { URL(fileURLWithPath: media) }
    var summary: String { tags?.summary ?? "" }
    /// Local-time capture date parsed from "yyyy-MM-ddTHH:mm:ss" (no formatter, so it is Sendable-safe).
    var date: Date? { Asset.parse(created) }
    var shotLabel: String { [tags?.type, tags?.shot].compactMap { $0 }.joined(separator: " / ") }
    var equirectExport: ExportFile? { (exports ?? []).first { $0.kind == "equirect" } }

    static func parse(_ s: String?) -> Date? {
        guard let s, s.count >= 19 else { return nil }
        let n = s.prefix(19).split(whereSeparator: { "-T:".contains($0) }).compactMap { Int($0) }
        guard n.count == 6 else { return nil }
        return Calendar.current.date(from: DateComponents(year: n[0], month: n[1], day: n[2], hour: n[3], minute: n[4], second: n[5]))
    }

    /// Text the search field matches against.
    var haystack: String {
        ([id, originalName ?? "", summary, tags?.location ?? "", tags?.notes ?? ""] + (tags?.hooks ?? []))
            .joined(separator: "\n").lowercased()
    }
}

struct LibraryList: Decodable, Sendable {
    let root: String?
    let exportsDir: String?
    let freeBytes: Int?
    let assets: [Asset]
}

enum KindFilter: String, CaseIterable, Identifiable, Sendable {
    case all = "All", threeSixty = "360", flat = "Flat"
    var id: String { rawValue }
}

enum DateFilter: String, CaseIterable, Identifiable, Sendable {
    case any = "Any date", week = "Last 7 days", month = "Last 30 days", day = "One day…"
    var id: String { rawValue }
}

struct LibraryFilter: Sendable {
    var query = ""
    var kind: KindFilter = .all
    var type: String? = nil
    var minQuality = 0
    var date: DateFilter = .any
    var day = Date()

    func apply(_ assets: [Asset], now: Date = Date()) -> [Asset] {
        let terms = query.lowercased().split(separator: " ").map(String.init)
        let cal = Calendar.current
        return assets.filter { a in
            switch kind {
            case .all: break
            case .threeSixty: if !a.is360 { return false }
            case .flat: if a.is360 { return false }
            }
            if let type, a.tags?.type != type { return false }
            if minQuality > 0, (a.tags?.quality ?? 0) < minQuality { return false }
            switch date {
            case .any: break
            case .week, .month:
                let days = date == .week ? 7 : 30
                guard let d = a.date, let cutoff = cal.date(byAdding: .day, value: -days, to: cal.startOfDay(for: now)), d >= cutoff else { return false }
            case .day:
                guard let d = a.date, cal.isDate(d, inSameDayAs: day) else { return false }
            }
            if !terms.isEmpty { let h = a.haystack; if !terms.allSatisfy({ h.contains($0) }) { return false } }
            return true
        }
        .sorted { ($0.date ?? .distantPast) > ($1.date ?? .distantPast) }
    }
}

func formatDuration(_ s: Double?) -> String {
    guard let s else { return "" }
    let t = Int(s.rounded())
    return t >= 3600 ? String(format: "%d:%02d:%02d", t / 3600, t / 60 % 60, t % 60) : String(format: "%d:%02d", t / 60, t % 60)
}
