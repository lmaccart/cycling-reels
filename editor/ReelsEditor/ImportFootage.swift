import AppKit
import Observation
import SwiftUI
import UniformTypeIdentifiers

/// Drives `library.import` / `library.ingest` and the progress sheet. Files are copied into inbox/;
/// originals stay where they are.
@MainActor
@Observable
final class ImportCoordinator {
    enum Phase: Equatable {
        case idle
        case copying
        case ingesting
        case finished(String)
        case failed(String)
    }

    var phase: Phase = .idle
    var isPresented = false
    var logLines: [String] = []
    var inboxCount = 0
    var inboxFiles: [String] = []
    var skippedCount = 0

    @ObservationIgnored var call: ((String, JSONValue) async throws -> JSONValue)?
    @ObservationIgnored var onToast: ((String) -> Void)?
    @ObservationIgnored var onLibraryNeedsReload: (() -> Void)?
    private var jobID: String?
    private var copied = 0
    private var cancelRequested = false

    var isBusy: Bool { phase == .copying || phase == .ingesting }

    static var supportedTypes: [UTType] {
        var types: [UTType] = [.movie, .image, .folder]
        if let raw = UTType(filenameExtension: "360") { types.append(raw) }
        return types
    }

    func chooseFiles() {
        let panel = NSOpenPanel()
        panel.title = "Import Footage"
        panel.message = "Pick clips, photos or folders. Your files are copied; originals stay where they are."
        panel.prompt = "Import"
        panel.allowsMultipleSelection = true
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowedContentTypes = Self.supportedTypes
        guard panel.runModal() == .OK else { return }
        start(paths: panel.urls)
    }

    func start(paths urls: [URL]) {
        guard !urls.isEmpty, !isBusy else { return }
        beginSession(.copying)
        Task {
            do {
                let result = try await requireCall("library.import", .object([
                    "paths": .array(urls.map { .string($0.path) }),
                    "ingest": .bool(true)
                ]))
                copied = Int(result["copied"]?.numberValue ?? 0)
                skippedCount = result["skipped"]?.arrayValue?.count ?? 0
                if let id = result["jobId"]?.stringValue {
                    jobID = jobID ?? id
                    if phase == .copying { phase = .ingesting }
                } else if !phase.isTerminal {
                    finish(copied > 0 ? "Copied \(copied) \(Self.clips(copied))" : "Nothing to import",
                           reload: copied > 0)
                }
            } catch {
                fail(error.localizedDescription)
            }
        }
    }

    func ingestInbox() {
        guard !isBusy else { return }
        copied = inboxCount
        beginSession(.ingesting)
        Task {
            do {
                let result = try await requireCall("library.ingest", .object([:]))
                jobID = jobID ?? result["jobId"]?.stringValue ?? result["id"]?.stringValue
                if jobID == nil, !phase.isTerminal { finish("Library is up to date", reload: true) }
            } catch {
                fail(error.localizedDescription)
            }
        }
    }

    func refreshInbox() {
        Task {
            guard let result = try? await call?("library.inbox", .object([:])) else {
                inboxCount = 0
                inboxFiles = []
                return
            }
            inboxCount = Int(result["count"]?.numberValue ?? 0)
            inboxFiles = result["files"]?.arrayValue?.compactMap(\.stringValue) ?? []
        }
    }

    func cancel() {
        guard isBusy else { return }
        cancelRequested = true
        guard let jobID else {
            isPresented = false
            return
        }
        Task { _ = try? await call?("job.cancel", .object(["id": .string(jobID)])) }
    }

    func dismiss() {
        isPresented = false
        if !isBusy { phase = .idle }
    }

    /// `job.changed` events with kind "ingest": `payload.state`, `payload.log`.
    func handleJob(_ payload: JSONValue) {
        guard payload["kind"]?.stringValue == "ingest" else { return }
        if jobID == nil, let id = payload["id"]?.stringValue { jobID = id }
        if let log = payload["log"]?.stringValue, !log.isEmpty, logLines.last != log {
            logLines.append(log)
            if logLines.count > 400 { logLines.removeFirst(logLines.count - 400) }
        }
        guard isBusy else { return }
        switch payload["state"]?.stringValue {
        case "running", "queued": phase = .ingesting
        case "completed":
            finish(copied > 0 ? "Imported \(copied) \(Self.clips(copied))" : "Library updated", reload: true)
        case "cancelled":
            phase = .failed("Cancelled. Files already copied stay in your inbox.")
            onLibraryNeedsReload?()
            refreshInbox()
        case "failed", "interrupted":
            let message = payload["error"]?["message"]?.stringValue ?? "Ingest did not finish."
            fail(message)
        default: break
        }
    }

    private func beginSession(_ phase: Phase) {
        self.phase = phase
        logLines = []
        jobID = nil
        copied = 0
        skippedCount = 0
        cancelRequested = false
        isPresented = true
    }

    private func finish(_ message: String, reload: Bool) {
        phase = .finished(message)
        isPresented = false
        onToast?(message)
        if reload { onLibraryNeedsReload?() }
        refreshInbox()
    }

    private func fail(_ message: String) {
        phase = .failed(message)
        refreshInbox()
    }

    private func requireCall(_ method: String, _ params: JSONValue) async throws -> JSONValue {
        guard let call else {
            throw ServiceFailure(code: "service_unavailable", message: "The editing service is not running.", details: nil)
        }
        return try await call(method, params)
    }

    static func clips(_ count: Int) -> String { count == 1 ? "clip" : "clips" }
}

extension ImportCoordinator.Phase {
    var isTerminal: Bool {
        switch self {
        case .finished, .failed: true
        default: false
        }
    }
}

// MARK: - Drag and drop

struct FootageDropModifier: ViewModifier {
    let importer: ImportCoordinator
    @State private var targeted = false

    func body(content: Content) -> some View {
        content
            .dropDestination(for: URL.self) { urls, _ in
                let files = urls.filter(\.isFileURL)
                guard !files.isEmpty else { return false }
                importer.start(paths: files)
                return true
            } isTargeted: { targeted = $0 }
            .overlay {
                if targeted {
                    ZStack {
                        RoundedRectangle(cornerRadius: 14)
                            .strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 2, dash: [8, 6]))
                            .background(Color.accentColor.opacity(0.10), in: RoundedRectangle(cornerRadius: 14))
                        VStack(spacing: 8) {
                            Image(systemName: "square.and.arrow.down").font(.system(size: 34, weight: .light))
                            Text("Drop to import footage").font(.title3.weight(.semibold))
                            Text("Files are copied. Originals stay where they are.")
                                .font(.callout).foregroundStyle(.secondary)
                        }
                    }
                    .padding(12)
                    .allowsHitTesting(false)
                    .transition(.opacity)
                }
            }
            .animation(.easeOut(duration: 0.12), value: targeted)
    }
}

extension View {
    func footageDropTarget(_ importer: ImportCoordinator) -> some View {
        modifier(FootageDropModifier(importer: importer))
    }
}

// MARK: - Progress sheet

struct ImportProgressSheet: View {
    let importer: ImportCoordinator

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 12) {
                Image(systemName: icon)
                    .font(.system(size: 26, weight: .light))
                    .foregroundStyle(iconColor)
                    .frame(width: 34)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.headline)
                    Text("Your files are copied; originals stay where they are.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
            if importer.isBusy { ProgressView().progressViewStyle(.linear) }
            ScrollViewReader { proxy in
                ScrollView {
                    VStack(alignment: .leading, spacing: 2) {
                        if importer.logLines.isEmpty {
                            Text(importer.isBusy ? "Waiting for the first message…" : "No output.")
                                .foregroundStyle(.tertiary)
                        }
                        ForEach(Array(importer.logLines.enumerated()), id: \.offset) { index, line in
                            Text(line).id(index)
                        }
                    }
                    .font(.system(size: 11, design: .monospaced))
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                    .textSelection(.enabled)
                }
                .frame(height: 170)
                .background(Color.black.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                .onChange(of: importer.logLines.count) { _, count in
                    if count > 0 { proxy.scrollTo(count - 1, anchor: .bottom) }
                }
            }
            if case .failed(let message) = importer.phase {
                Text(message).font(.callout).foregroundStyle(.red).fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                if importer.isBusy {
                    Button("Cancel", role: .cancel) { importer.cancel() }
                        .keyboardShortcut(.cancelAction)
                } else {
                    Button("Done") { importer.dismiss() }
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(22)
        .frame(width: 520)
        .interactiveDismissDisabled(importer.isBusy)
    }

    private var title: String {
        switch importer.phase {
        case .copying: "Copying footage…"
        case .ingesting: "Adding footage to your library…"
        case .finished(let message): message
        case .failed: "Import stopped"
        case .idle: "Import"
        }
    }

    private var icon: String {
        switch importer.phase {
        case .finished: "checkmark.circle"
        case .failed: "exclamationmark.triangle"
        default: "square.and.arrow.down"
        }
    }

    private var iconColor: Color {
        switch importer.phase {
        case .finished: .green
        case .failed: .orange
        default: .accentColor
        }
    }
}
