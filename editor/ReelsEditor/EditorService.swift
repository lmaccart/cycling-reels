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

    private var process = Process()
    private var stdin: FileHandle?
    private var stdoutBuffer = Data()
    private var pending: [String: Pending] = [:]
    private var nextID = 1
    private(set) var state: ServiceState = .stopped
    private(set) var logRing = LogRing()
    var onEvent: ((JSONValue) -> Void)?
    var onState: ((ServiceState) -> Void)?
    var callStub: ((String, JSONValue) async throws -> JSONValue)?

    /// Last stderr lines from the service, for failure screens.
    func logTail(_ count: Int = 20) -> [String] { logRing.tail(count) }

    /// Live mode: `<repo>/reels editor serve` with cwd = repo.
    func start(repositoryRoot: URL, environment: [String: String]) throws {
        guard state == .stopped || isTerminated else { return }
        process = Process()
        stdoutBuffer = Data()
        state = .starting
        onState?(state)
        let ring = LogRing(fileURL: LogRing.defaultFileURL)
        logRing = ring
        let process = self.process
        process.executableURL = repositoryRoot.appending(path: "reels")
        process.currentDirectoryURL = repositoryRoot
        process.arguments = ["editor", "serve"]
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
            // Protocol stdout stays free of diagnostics; stderr goes to the ring buffer and log file.
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil } else { ring.append(data) }
        }
        process.terminationHandler = { [weak self] finished in
            let status = finished.terminationStatus
            // Give the stderr handler a beat to drain the final lines.
            Task { @MainActor [weak self] in
                try? await Task.sleep(for: .milliseconds(150))
                guard let self, self.process === finished else { return }
                var message = "The editing service stopped (exit \(status))."
                if let last = ring.tail(1).first { message += " " + last }
                self.failPending(message)
                if self.state != .stopped {
                    self.state = .failed(message)
                    self.onState?(self.state)
                }
            }
        }
        try process.run()
    }

    var isTerminated: Bool { !process.isRunning }

    func call(_ method: String, params: JSONValue = .object([:])) async throws -> JSONValue {
        if let callStub { return try await callStub(method, params) }
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
        state = .stopped
        Task { _ = try? await call("shutdown") }
        process.terminate()
        stdin?.closeFile()
        stdin = nil
    }

    /// For app quit: ask the service to shut down, wait briefly, then terminate. Never leaves a child behind.
    func stopSynchronously(grace: TimeInterval = 2) {
        guard process.isRunning else { return }
        state = .stopped
        let request = #"{"version":1,"id":"native-quit","method":"shutdown","params":{}}"# + "\n"
        try? stdin?.write(contentsOf: Data(request.utf8))
        try? stdin?.close()
        stdin = nil
        let deadline = Date().addingTimeInterval(grace)
        while process.isRunning, Date() < deadline { Thread.sleep(forTimeInterval: 0.05) }
        if process.isRunning { process.terminate() }
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
