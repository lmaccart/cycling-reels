import AppKit
import Foundation

struct BackendError: Error, LocalizedError, Sendable {
    let message: String
    var errorDescription: String? { message }
}

/// Runs `./reels flibrary <sub>` from the repo root and decodes its JSON. Streaming jobs (export) belong here too.
struct Backend: Sendable {
    let root: URL

    var environment: [String: String] {
        var env = ProcessInfo.processInfo.environment
        let extra = ["/opt/homebrew/bin", "/usr/local/bin", NSHomeDirectory() + "/.local/bin"]
        let have = (env["PATH"] ?? "/usr/bin:/bin").split(separator: ":").map(String.init)
        env["PATH"] = (extra + have).joined(separator: ":")
        return env
    }

    func makeProcess(_ args: [String]) -> Process {
        let p = Process()
        p.executableURL = root.appending(path: "reels")
        p.arguments = ["flibrary"] + args
        p.currentDirectoryURL = root
        p.environment = environment
        return p
    }

    /// Runs to completion and returns stdout. A `{"error": ...}` body or non-zero exit becomes a BackendError.
    func run(_ args: [String]) async throws -> Data {
        let process = makeProcess(args)
        return try await Task.detached(priority: .userInitiated) {
            let out = Pipe(), err = Pipe()
            process.standardOutput = out
            process.standardError = err
            try process.run()
            // Drain stderr concurrently so a chatty backend cannot block on a full pipe.
            let errTask = Task.detached { err.fileHandleForReading.readDataToEndOfFile() }
            let data = out.fileHandleForReading.readDataToEndOfFile()
            let errData = await errTask.value
            process.waitUntilExit()
            if let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any], let msg = obj["error"] as? String {
                throw BackendError(message: msg)
            }
            guard process.terminationStatus == 0 else {
                let text = String(decoding: errData.suffix(600), as: UTF8.self).trimmingCharacters(in: .whitespacesAndNewlines)
                throw BackendError(message: text.isEmpty ? "reels flibrary exited with status \(process.terminationStatus)" : text)
            }
            return data
        }.value
    }

    func list() async throws -> LibraryList {
        let data = try await run(["list"])
        do { return try JSONDecoder().decode(LibraryList.self, from: data) }
        catch { throw BackendError(message: "Unreadable library list: \(error.localizedDescription)") }
    }

    /// The levelling rotation (v360 angles; fov unused) the backend applies to this clip.
    func level(id: String) async throws -> SphereView {
        let data = try await run(["level", id])
        guard let o = try JSONSerialization.jsonObject(with: data) as? [String: Any], let y = o["yaw"] as? Double,
              let p = o["pitch"] as? Double, let r = o["roll"] as? Double else { throw BackendError(message: "level returned no rotation") }
        return SphereView(yaw: y, pitch: p, fov: 90, roll: r)
    }

    /// Levelled full-sphere equirect still at `t` seconds, or with `view` the flat reframe exactly as the flat export renders it (cached).
    func frame(id: String, t: Double, width: Int = 2048, view: SphereView? = nil, w: Int = 0, h: Int = 0) async throws -> URL {
        // `--view=...`: a negative yaw must not be read as an option
        var args = ["frame", id, "--t", String(format: "%.2f", t), "--width", String(width)]
        if let v = view { args += [String(format: "--view=%.3f,%.3f,%.3f,%.3f", v.yaw, v.pitch, v.roll, v.fov), "--w", String(w), "--h", String(h)] }
        let data = try await run(args)
        guard let obj = try JSONSerialization.jsonObject(with: data) as? [String: Any], let path = obj["path"] as? String else {
            throw BackendError(message: "frame returned no path")
        }
        return URL(fileURLWithPath: path)
    }
}

// MARK: - Repository discovery

enum RepositoryLocator {
    static let defaultsKey = "flibrary.repositoryRoot"

    /// The cycling repo has an executable `reels` launcher and a `library/` folder.
    static func validate(_ url: URL) -> URL? {
        let root = url.standardizedFileURL.resolvingSymlinksInPath()
        var isDir: ObjCBool = false
        guard FileManager.default.isExecutableFile(atPath: root.appending(path: "reels").path),
              FileManager.default.fileExists(atPath: root.appending(path: "library").path, isDirectory: &isDir), isDir.boolValue
        else { return nil }
        return root
    }

    /// Precedence: `--repo`, FLIBRARY_REPO_ROOT, saved default.
    static func candidate(arguments: [String], environment: [String: String], saved: String?) -> String? {
        if let i = arguments.firstIndex(of: "--repo"), arguments.indices.contains(i + 1) { return arguments[i + 1] }
        if let v = environment["FLIBRARY_REPO_ROOT"], !v.isEmpty { return v }
        return saved?.isEmpty == false ? saved : nil
    }

    @MainActor static func resolve() -> URL? {
        let path = candidate(arguments: CommandLine.arguments, environment: ProcessInfo.processInfo.environment,
                             saved: UserDefaults.standard.string(forKey: defaultsKey))
        guard let path, let root = validate(URL(fileURLWithPath: path)) else { return nil }
        return root
    }

    @MainActor static func pick() -> URL? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.message = "Choose your cycling folder (the one with ./reels and library/)."
        while panel.runModal() == .OK, let url = panel.url {
            if let root = validate(url) { UserDefaults.standard.set(root.path, forKey: defaultsKey); return root }
            panel.message = "That folder has no executable reels file and library/. Choose your cycling folder."
        }
        return nil
    }
}
