import Foundation

@MainActor
final class EditorService {
    enum ServiceState: Equatable {
        case stopped
        case starting
        case connected
        case failed(String)
    }

    private struct Pending {
        let continuation: CheckedContinuation<JSONValue, Error>
    }

    private let process = Process()
    private var stdin: FileHandle?
    private var stdoutBuffer = Data()
    private var pending: [String: Pending] = [:]
    private var nextID = 1
    private(set) var state: ServiceState = .stopped
    var onEvent: ((JSONValue) -> Void)?
    var onState: ((ServiceState) -> Void)?

    func start(repositoryRoot: URL, workspace: URL) throws {
        guard state == .stopped || isTerminated else { return }
        state = .starting
        onState?(state)
        process.executableURL = repositoryRoot.appending(path: "reels")
        process.currentDirectoryURL = repositoryRoot
        process.arguments = ["--editor-workspace", workspace.path, "editor", "serve"]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment
        let input = Pipe()
        let output = Pipe()
        let errors = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors
        stdin = input.fileHandleForWriting
        output.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else {
                handle.readabilityHandler = nil
                return
            }
            Task { @MainActor [weak self] in self?.receive(data) }
        }
        errors.fileHandleForReading.readabilityHandler = { handle in
            // Protocol stdout is intentionally kept free of diagnostics.
            if handle.availableData.isEmpty { handle.readabilityHandler = nil }
        }
        process.terminationHandler = { [weak self] process in
            Task { @MainActor [weak self] in
                guard let self else { return }
                let message = "Editing service stopped (exit \(process.terminationStatus))."
                self.failPending(message)
                self.state = .failed(message)
                self.onState?(self.state)
            }
        }
        try process.run()
    }

    var isTerminated: Bool { !process.isRunning }

    func call(_ method: String, params: JSONValue = .object([:])) async throws -> JSONValue {
        guard process.isRunning, let stdin else {
            throw ServiceFailure(code: "service_unavailable", message: "The editing service is not running.", details: nil)
        }
        let id = "native-\(nextID)"
        nextID += 1
        let request = JSONValue.object([
            "version": .number(1),
            "id": .string(id),
            "method": .string(method),
            "params": params
        ])
        guard var data = request.jsonData() else {
            throw ServiceFailure(code: "encode_failed", message: "Could not encode the service request.", details: nil)
        }
        data.append(0x0A)
        return try await withCheckedThrowingContinuation { continuation in
            pending[id] = Pending(continuation: continuation)
            do {
                try stdin.write(contentsOf: data)
            } catch {
                pending.removeValue(forKey: id)
                continuation.resume(throwing: error)
            }
        }
    }

    func stop() {
        guard process.isRunning else { return }
        Task { _ = try? await call("shutdown") }
        process.terminate()
        stdin?.closeFile()
        stdin = nil
    }

    private func receive(_ data: Data) {
        stdoutBuffer.append(data)
        while let newline = stdoutBuffer.firstIndex(of: 0x0A) {
            let line = stdoutBuffer.prefix(upTo: newline)
            stdoutBuffer.removeSubrange(...newline)
            guard !line.isEmpty,
                  let value = try? JSONDecoder().decode(JSONValue.self, from: Data(line)) else { continue }
            handle(value)
        }
    }

    private func handle(_ message: JSONValue) {
        if let event = message["event"]?.stringValue {
            onEvent?(message)
            if event == "service.ready" || event == "workspace.ready" {
                state = .connected
                onState?(state)
            }
            return
        }
        guard let id = message["id"]?.stringValue,
              let item = pending.removeValue(forKey: id) else { return }
        if let failure = message["error"] {
            let error = ServiceFailure(
                code: failure["code"]?.stringValue ?? "service_error",
                message: failure["message"]?.stringValue ?? "The editing service rejected the request.",
                details: failure["details"]
            )
            item.continuation.resume(throwing: error)
        } else {
            if state == .starting {
                state = .connected
                onState?(state)
            }
            item.continuation.resume(returning: message["result"] ?? .null)
        }
    }

    private func failPending(_ message: String) {
        let waiting = pending.values
        pending.removeAll()
        for item in waiting {
            item.continuation.resume(throwing: ServiceFailure(code: "service_stopped", message: message, details: nil))
        }
    }
}
