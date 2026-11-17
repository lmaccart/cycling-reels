import AppKit
import Darwin

@MainActor
final class EditorAppDelegate: NSObject, NSApplicationDelegate {
    static var isTestHost: Bool {
        ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil
            || NSClassFromString("XCTestCase") != nil
    }

    func applicationShouldSaveApplicationState(_ app: NSApplication) -> Bool { !Self.isTestHost }
    func applicationShouldRestoreApplicationState(_ app: NSApplication) -> Bool { !Self.isTestHost }

    private var captureObservers: [NSObjectProtocol] = []
    private var captureContinuation: CheckedContinuation<Bool, Never>?
    private var captureDocumentReady = false
    private var previewContinuation: CheckedContinuation<Bool, Never>?
    private var previewOutcome: Bool?
    private var previewFailure: String?

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        guard !Self.isTestHost else { return false }
        if !flag, let window = sender.windows.first(where: { $0.contentView != nil && $0.canBecomeMain }) {
            window.makeKeyAndOrderFront(nil)
            return false
        }
        return !flag
    }

    func applicationWillTerminate(_ notification: Notification) {
        EditorStore.shared.service.stopSynchronously()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        guard !Self.isTestHost else { return }
        Task { await enforceMinimumWindowSize() }
        guard let destination = Self.argument("--capture-window", in: ProcessInfo.processInfo.arguments) else { return }
        if Self.argument("--plan", in: ProcessInfo.processInfo.arguments) != nil
            || ProcessInfo.processInfo.arguments.contains("--capture-require-document") {
            captureObservers.append(NotificationCenter.default.addObserver(
                forName: .reelsEditorDocumentReady, object: nil, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in self?.markCaptureDocumentReady() }
            })
            captureObservers.append(NotificationCenter.default.addObserver(
                forName: .reelsEditorProgramReady, object: nil, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in self?.markCapturePreviewReady() }
            })
            captureObservers.append(NotificationCenter.default.addObserver(
                forName: .reelsEditorPreviewFailed, object: nil, queue: .main
            ) { [weak self] notification in
                let reason = notification.object as? String ?? "Preview preparation failed"
                Task { @MainActor in self?.markCapturePreviewFailed(reason) }
            })
        }
        Task { await captureOwnWindow(to: URL(fileURLWithPath: destination)) }
    }

    private func enforceMinimumWindowSize() async {
        for _ in 0..<40 {
            if let window = NSApp.windows.first(where: { $0.contentView != nil }) {
                window.minSize = NSSize(width: 1280, height: 800)
                return
            }
            try? await Task.sleep(for: .milliseconds(250))
        }
    }

    private func captureOwnWindow(to destination: URL) async {
        for _ in 0..<180 {
            if let window = NSApp.windows.first(where: { $0.contentView != nil }) {
                let needsDocument = Self.argument("--plan", in: ProcessInfo.processInfo.arguments) != nil
                    || ProcessInfo.processInfo.arguments.contains("--capture-require-document")
                if needsDocument, !(await waitForDocumentReady()) {
                    NSLog("Reels Editor capture: no open document before the 45-second deadline; capturing the current screen.")
                    window.setContentSize(NSSize(width: 1280, height: 768))
                    window.displayIfNeeded()
                    try? await Task.sleep(for: .milliseconds(600))
                    capture(window: window, to: destination)
                    finishCaptureIfRequested()
                    return
                }
                if needsDocument, !(await waitForPreviewReady()) {
                    if previewFailure == nil {
                        NotificationCenter.default.post(name: .reelsEditorPreviewTimedOut, object: nil)
                    }
                    NSLog("Reels Editor capture continuing in preview error/pending state: %@",
                          previewFailure ?? "preview was not ready within 20 seconds.")
                }
                await Task.yield()
                if !needsDocument { try? await Task.sleep(for: .milliseconds(700)) }
                window.minSize = NSSize(width: 1280, height: 800)
                window.setContentSize(NSSize(width: 1280, height: 768))
                window.displayIfNeeded()
                try? await Task.sleep(for: .milliseconds(900))
                capture(window: window, to: destination)

                window.setContentSize(NSSize(width: 1520, height: 940))
                window.displayIfNeeded()
                try? await Task.sleep(for: .milliseconds(900))
                capture(window: window, to: Self.defaultSizePath(for: destination))
                finishCaptureIfRequested()
                return
            }
            try? await Task.sleep(for: .milliseconds(250))
        }
        NSLog("Reels Editor capture skipped: application window did not become available.")
        finishCaptureIfRequested()
    }

    private func waitForDocumentReady() async -> Bool {
        if captureDocumentReady { return true }
        return await withCheckedContinuation { continuation in
            captureContinuation = continuation
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(45))
                guard let pending = self.captureContinuation else { return }
                self.captureContinuation = nil
                pending.resume(returning: false)
            }
        }
    }

    private func markCaptureDocumentReady() {
        captureDocumentReady = true
        guard let continuation = captureContinuation else { return }
        captureContinuation = nil
        continuation.resume(returning: true)
    }

    private func waitForPreviewReady() async -> Bool {
        if let previewOutcome { return previewOutcome }
        return await withCheckedContinuation { continuation in
            previewContinuation = continuation
            Task { @MainActor in
                try? await Task.sleep(for: .seconds(20))
                guard let pending = self.previewContinuation else { return }
                self.previewContinuation = nil
                self.previewOutcome = false
                pending.resume(returning: false)
            }
        }
    }

    private func markCapturePreviewReady() {
        previewOutcome = true
        guard let continuation = previewContinuation else { return }
        previewContinuation = nil
        continuation.resume(returning: true)
    }

    private func markCapturePreviewFailed(_ reason: String) {
        previewFailure = reason
        previewOutcome = false
        guard let continuation = previewContinuation else { return }
        previewContinuation = nil
        continuation.resume(returning: false)
    }

    private func finishCaptureIfRequested() {
        captureObservers.forEach(NotificationCenter.default.removeObserver)
        captureObservers.removeAll()
        if ProcessInfo.processInfo.arguments.contains("--capture-and-quit") {
            NotificationCenter.default.post(name: .reelsEditorCaptureQuit, object: nil)
            Task { @MainActor in
                try? await Task.sleep(for: .milliseconds(250))
                NSApp.terminate(nil)
                try? await Task.sleep(for: .milliseconds(350))
                Darwin.exit(EXIT_SUCCESS)
            }
        }
    }

    private func capture(window: NSWindow, to destination: URL) {
        guard let view = window.contentView else { return }
        let rect = view.bounds
        guard let bitmap = NSBitmapImageRep(
            bitmapDataPlanes: nil,
            pixelsWide: max(1, Int(rect.width)),
            pixelsHigh: max(1, Int(rect.height)),
            bitsPerSample: 8,
            samplesPerPixel: 4,
            hasAlpha: true,
            isPlanar: false,
            colorSpaceName: .deviceRGB,
            bytesPerRow: 0,
            bitsPerPixel: 0
        ) else { return }
        bitmap.size = rect.size
        view.cacheDisplay(in: rect, to: bitmap)
        guard let data = bitmap.representation(using: .png, properties: [:]) else { return }
        do {
            try FileManager.default.createDirectory(at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
            try data.write(to: destination, options: .atomic)
            NSLog("Reels Editor captured its own content view to %@", destination.path)
        } catch {
            NSLog("Reels Editor capture failed: %@", error.localizedDescription)
        }
    }

    private static func defaultSizePath(for destination: URL) -> URL {
        let ext = destination.pathExtension.isEmpty ? "png" : destination.pathExtension
        let base = destination.deletingPathExtension()
        return base.appendingPathExtension("default.\(ext)")
    }

    private static func argument(_ key: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: key), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }
}
