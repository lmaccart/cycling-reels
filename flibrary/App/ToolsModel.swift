import Foundation
import SwiftUI

/// What the backend's export/estimate read from the job file (see CONTRACT.md).
struct JobSpec: Codable, Equatable, Sendable {
    var id: String
    var kind: String                 // "equirect" | "flat"
    var stabilize = false
    var `in`: Double?, out: Double?
    var width: Int?, height: Int?, fps: Int?
    var keys: [ReframeKey]?
}

/// A background job (export) shown in the jobs strip.
struct Job: Identifiable, Sendable {
    enum State: Sendable { case queued, running, done, failed }
    let id: UUID
    var title: String
    var progress: Double
    var state: State
    var detail: String?
    var spec: JobSpec? = nil
    var output: String? = nil
}

enum JobLine: Equatable, Sendable {
    case progress(Double), done(String), error(String)

    /// One stdout line of `export`: `{"progress": 0.4}`, `{"done": true, "path": ...}` or `{"error": ...}`.
    static func parse(_ line: String) -> JobLine? {
        guard let o = try? JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any] else { return nil }
        if let e = o["error"] as? String { return .error(e) }
        if o["done"] as? Bool == true { return .done(o["path"] as? String ?? "") }
        if let p = o["progress"] as? Double { return .progress(min(1, max(0, p))) }
        return nil
    }
}

struct Estimate: Decodable, Equatable, Sendable {
    let bytes: Int, freeBytes: Int, ok: Bool
    let reason: String?

    var summary: String {
        let f = { (n: Int) in ByteCountFormatter.string(fromByteCount: Int64(n), countStyle: .file) }
        return "about \(f(bytes)), \(f(freeBytes)) free"
    }
    /// nil when the export may start; otherwise the text to show instead.
    var refusal: String? { ok ? nil : (reason ?? "Not enough free disk space") + " (\(summary))" }
}

/// Owns the jobs strip and the equirect prompt. Jobs run one at a time: estimate, then export with streamed progress.
@MainActor @Observable
final class ToolsModel {
    var jobs: [Job] = []
    /// 360 clips the user tried to drag that have no equirect export yet; non-empty shows the prompt.
    var equirectPrompt: [Asset] = []
    var message: String?
    @ObservationIgnored weak var store: LibraryStore?
    @ObservationIgnored private var current: Process?
    @ObservationIgnored private var running = false

    func promptForEquirect(_ assets: [Asset]) { equirectPrompt = assets }

    /// Queues an unstabilized equirect export (the drag prompt).
    func requestEquirect(assetID: String) { enqueue(JobSpec(id: assetID, kind: "equirect"), title: "Equirect \(assetID)") }

    func confirmEquirectPrompt() {
        let ids = equirectPrompt.map(\.id)
        equirectPrompt = []
        ids.forEach(requestEquirect(assetID:))
    }

    func enqueue(_ spec: JobSpec, title: String) {
        jobs.append(Job(id: UUID(), title: title, progress: 0, state: .queued, detail: "Queued", spec: spec))
        if !running { Task { await drain() } }
    }

    func cancel(_ id: UUID) {
        guard let i = jobs.firstIndex(where: { $0.id == id }) else { return }
        if jobs[i].state == .queued { jobs[i].state = .failed; jobs[i].detail = "Cancelled" }
        else if jobs[i].state == .running { current?.terminate(); jobs[i].detail = "Cancelling…" }
    }

    func dismiss(_ id: UUID) { jobs.removeAll { $0.id == id } }

    private func drain() async {
        running = true
        defer { running = false }
        while let i = jobs.firstIndex(where: { $0.state == .queued }) { await run(jobs[i].id) }
    }

    private func update(_ id: UUID, _ f: (inout Job) -> Void) { if let i = jobs.firstIndex(where: { $0.id == id }) { f(&jobs[i]) } }

    private func run(_ id: UUID) async {
        guard let store, let spec = jobs.first(where: { $0.id == id })?.spec else { return update(id) { $0.state = .failed; $0.detail = "No library" } }
        update(id) { $0.state = .running; $0.detail = "Estimating…" }
        let dir = store.root.appending(path: ".cache/flibrary/jobs")
        let file = dir.appending(path: "\(id.uuidString).json")
        defer { try? FileManager.default.removeItem(at: file) }
        do {
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            try JSONEncoder().encode(spec).write(to: file)
            let est = try JSONDecoder().decode(Estimate.self, from: await store.backend.run(["estimate", file.path]))
            if let why = est.refusal { return update(id) { $0.state = .failed; $0.detail = why } }
            update(id) { $0.detail = est.summary }
            let p = store.backend.makeProcess(["export", file.path])
            let out = Pipe()
            p.standardOutput = out; p.standardError = FileHandle.nullDevice
            try p.run()
            current = p
            var result: JobLine?
            for try await line in out.fileHandleForReading.bytes.lines {
                guard let l = JobLine.parse(line) else { continue }
                if case .progress(let v) = l { update(id) { $0.progress = v } } else { result = l }
            }
            current = nil
            switch result {
            case .done(let path):
                update(id) { $0.state = .done; $0.progress = 1; $0.output = path; $0.detail = "Done" }
                await store.refresh()
            case .error(let e): update(id) { $0.state = .failed; $0.detail = e }
            default: update(id) { $0.state = .failed; $0.detail = "Export ended without a result" }
            }
        } catch { update(id) { $0.state = .failed; $0.detail = error.localizedDescription } }
    }
}

struct JobsStrip: View {
    @Environment(ToolsModel.self) private var tools
    var body: some View {
        if !tools.jobs.isEmpty {
            VStack(alignment: .leading, spacing: 4) {
                ForEach(tools.jobs) { job in
                    HStack {
                        Text(job.title).lineLimit(1)
                        if job.state == .running { ProgressView(value: job.progress).frame(width: 140) }
                        Text(job.detail ?? "").foregroundStyle(job.state == .failed ? .red : .secondary).lineLimit(2)
                        Spacer()
                        if job.state == .running || job.state == .queued {
                            Button("Cancel") { tools.cancel(job.id) }.buttonStyle(.link)
                        } else {
                            Button("Dismiss") { tools.dismiss(job.id) }.buttonStyle(.link)
                        }
                    }.font(.caption)
                }
            }
            .padding(8).background(.bar)
        }
    }
}

/// Inspector section: equirect export (with stabilize) and the clip's existing exports.
struct Tools360Panel: View {
    let asset: Asset
    @Environment(ToolsModel.self) private var tools
    @State private var stabilize = false

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("360 tools").font(.headline)
            Toggle("Stabilize (camera orientation)", isOn: $stabilize)
            if stabilize {
                Text("Levels the horizon with the camera's gyro on every frame (about real time on Metal).")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Button("Make equirect for Premiere") {
                tools.enqueue(JobSpec(id: asset.id, kind: "equirect", stabilize: stabilize), title: "Equirect\(stabilize ? " (stab)" : "") \(asset.id)")
            }
            let eq = (asset.exports ?? []).filter { $0.kind == "equirect" }
            if !eq.isEmpty {
                Text("Existing: " + eq.map { $0.label ?? $0.kind }.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
            }
        }
    }
}
