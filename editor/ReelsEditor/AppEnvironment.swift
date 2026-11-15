import AppKit
import Foundation

// MARK: - Repository discovery

enum RepositoryLocator {
    static let defaultsKey = "reelsEditor.repositoryRoot"

    /// A folder is the cycling repo when it has an executable `reels` launcher and a `plans/` directory.
    static func validate(_ url: URL) throws -> URL {
        let root = url.standardizedFileURL.resolvingSymlinksInPath()
        guard FileManager.default.isExecutableFile(atPath: root.appending(path: "reels").path) else {
            throw ServiceFailure(
                code: "invalid_repo",
                message: "\(root.path) has no executable \"reels\" file. Choose your cycling folder.",
                details: nil
            )
        }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: root.appending(path: "plans").path, isDirectory: &isDirectory),
              isDirectory.boolValue else {
            throw ServiceFailure(
                code: "invalid_repo",
                message: "\(root.path) has no plans folder. Choose your cycling folder.",
                details: nil
            )
        }
        return root
    }

    /// Order of precedence: `--repo` argument, `REELS_EDITOR_REPO_ROOT`, then the saved folder.
    static func candidatePath(arguments: [String], environment: [String: String], saved: String?) -> String? {
        if let index = arguments.firstIndex(of: "--repo"), arguments.indices.contains(index + 1) {
            return arguments[index + 1]
        }
        if let value = environment["REELS_EDITOR_REPO_ROOT"], !value.isEmpty { return value }
        if let saved, !saved.isEmpty { return saved }
        return nil
    }

    static func save(_ url: URL, defaults: UserDefaults = .standard) {
        defaults.set(url.path, forKey: defaultsKey)
    }
}

// MARK: - Child process environment

enum AppEnvironment {
    static func extraPathEntries(home: String) -> [String] {
        ["/opt/homebrew/bin", "/usr/local/bin", "\(home)/.local/bin"]
    }

    /// Login-shell entries first, then the Homebrew/user extras, then whatever the app already had. Duplicates and empties dropped.
    static func mergedPath(loginPath: String?, basePath: String?, home: String) -> String {
        var seen = Set<String>()
        var entries: [String] = []
        let sources: [[String]] = [
            (loginPath ?? "").split(separator: ":").map(String.init),
            extraPathEntries(home: home),
            (basePath ?? "").split(separator: ":").map(String.init)
        ]
        for entry in sources.joined() where !entry.isEmpty && seen.insert(entry).inserted {
            entries.append(entry)
        }
        return entries.joined(separator: ":")
    }

    static func serviceEnvironment(
        base: [String: String], loginPath: String?, home: String = NSHomeDirectory()
    ) -> [String: String] {
        var environment = base
        environment["PATH"] = mergedPath(loginPath: loginPath, basePath: base["PATH"], home: home)
        environment["PYTHONUNBUFFERED"] = "1"
        environment["HOME"] = base["HOME"] ?? home
        return environment
    }

    /// Extracts the PATH printed between markers so rc-file noise cannot corrupt it.
    static func parseMarkedPath(_ output: String) -> String? {
        let marker = "__REELS_PATH__"
        let parts = output.components(separatedBy: marker)
        guard parts.count >= 3 else { return nil }
        let value = parts[1].trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? nil : value
    }

    /// Blocking; call off the main thread. Returns nil on timeout or failure.
    nonisolated static func readLoginShellPath(timeout: TimeInterval = 3) -> String? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-lic", "printf '__REELS_PATH__%s__REELS_PATH__' \"$PATH\""]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        let finished = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in finished.signal() }
        do { try process.run() } catch { return nil }
        if finished.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            return nil
        }
        let data = output.fileHandleForReading.readDataToEndOfFile()
        return parseMarkedPath(String(decoding: data, as: UTF8.self))
    }

    /// Captured once per launch, off the main thread.
    static func loginShellPath() async -> String? {
        await Task.detached(priority: .userInitiated) { readLoginShellPath() }.value
    }
}

// MARK: - Service log (stderr ring buffer)

/// Keeps the last lines the service wrote to stderr; safe to feed from a background queue.
final class LogRing: @unchecked Sendable {
    private let lock = NSLock()
    private var lines: [String] = []
    private var partial = ""
    private let capacity: Int
    private let fileHandle: FileHandle?

    init(capacity: Int = 200, fileURL: URL? = nil) {
        self.capacity = capacity
        if let fileURL {
            try? FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
            FileManager.default.createFile(atPath: fileURL.path, contents: nil)
            fileHandle = try? FileHandle(forWritingTo: fileURL)
        } else {
            fileHandle = nil
        }
    }

    deinit { try? fileHandle?.close() }

    func append(_ data: Data) {
        try? fileHandle?.write(contentsOf: data)
        append(String(decoding: data, as: UTF8.self))
    }

    func append(_ text: String) {
        lock.lock(); defer { lock.unlock() }
        partial += text
        while let newline = partial.firstIndex(of: "\n") {
            let line = String(partial[..<newline]).trimmingCharacters(in: .controlCharacters)
            partial = String(partial[partial.index(after: newline)...])
            if !line.isEmpty { push(line) }
        }
    }

    private func push(_ line: String) {
        lines.append(line)
        if lines.count > capacity { lines.removeFirst(lines.count - capacity) }
    }

    func tail(_ count: Int = 20) -> [String] {
        lock.lock(); defer { lock.unlock() }
        var result = lines
        let rest = partial.trimmingCharacters(in: .whitespacesAndNewlines)
        if !rest.isEmpty { result.append(rest) }
        return Array(result.suffix(count))
    }

    static var defaultFileURL: URL {
        FileManager.default.homeDirectoryForCurrentUser
            .appending(path: "Library/Logs/Reels Editor/service.log")
    }
}

// MARK: - Claude Code in Terminal

enum ClaudeCodeLauncher {
    static func shellQuote(_ value: String) -> String {
        "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }

    static func shellCommand(repo: URL) -> String {
        "cd \(shellQuote(repo.path)) && claude"
    }

    static func appleScript(repo: URL) -> String {
        let command = shellCommand(repo: repo)
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "\"", with: "\\\"")
        return "tell application \"Terminal\"\nactivate\ndo script \"\(command)\"\nend tell"
    }

    @MainActor
    static func open(repo: URL) -> Bool {
        var failure: NSDictionary?
        if let script = NSAppleScript(source: appleScript(repo: repo)) {
            script.executeAndReturnError(&failure)
            if failure == nil { return true }
        }
        guard let terminal = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.apple.Terminal") else { return false }
        NSWorkspace.shared.open([repo], withApplicationAt: terminal, configuration: NSWorkspace.OpenConfiguration())
        return true
    }
}

// MARK: - plans.list rows

struct PlanSummary: Identifiable, Hashable, Sendable {
    var path: String
    var title: String
    var postDate: String?
    var format: String?
    var segmentCount: Int?
    var durationSeconds: Double?
    var status: String?
    var thumbnailURL: URL?
    var modified: Double?

    var id: String { path }

    init?(_ value: JSONValue) {
        guard let path = value["path"]?.stringValue, !path.isEmpty else { return nil }
        self.path = path
        let stem = URL(fileURLWithPath: path).deletingPathExtension().lastPathComponent
        let rawTitle = value["title"]?.stringValue ?? value["slug"]?.stringValue ?? value["name"]?.stringValue
        let trimmed = rawTitle?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        title = trimmed.isEmpty ? stem : trimmed
        postDate = value["postDate"]?.stringValue
        format = value["format"]?.stringValue
        segmentCount = value["segmentCount"]?.numberValue.map { Int($0) }
        durationSeconds = value["durationSeconds"]?.numberValue
        status = value["status"]?.stringValue
        thumbnailURL = value["thumbnailURL"]?.stringValue.flatMap { URL(string: $0) }
        modified = value["modified"]?.numberValue
    }

    static func parse(_ result: JSONValue) -> [PlanSummary] {
        let rows = result["plans"]?.arrayValue ?? result["items"]?.arrayValue ?? result.arrayValue ?? []
        return rows.compactMap(PlanSummary.init)
    }

    var durationText: String? {
        guard let durationSeconds, durationSeconds > 0 else { return nil }
        let total = Int(durationSeconds.rounded())
        return "\(total / 60):" + String(format: "%02d", total % 60)
    }

    var dateText: String? {
        guard let postDate else { return nil }
        let parser = DateFormatter()
        parser.locale = Locale(identifier: "en_US_POSIX")
        parser.dateFormat = "yyyy-MM-dd"
        guard let date = parser.date(from: postDate) else { return postDate }
        return date.formatted(.dateTime.weekday(.abbreviated).month(.abbreviated).day())
    }

    func matches(_ query: String) -> Bool {
        let needle = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !needle.isEmpty else { return true }
        return [title, path, postDate, format, status].compactMap { $0?.lowercased() }
            .contains { $0.contains(needle) }
    }
}
