import AppKit
import AVFoundation
import Foundation
import Observation
import UniformTypeIdentifiers

@MainActor
@Observable
final class EditorStore {
    static let shared = EditorStore()

    let service = EditorService()
    let importer = ImportCoordinator()
    let music = MusicLibraryModel()
    var planSummaries: [PlanSummary] = []
    var plansLoaded = false
    var showPicker = true
    var startupFailure: StartupFailure?
    var toast: String?
    var isNamingNewReel = false
    var newReelTitle = ""
    private(set) var repositoryRoot: URL?
    private var toastTask: Task<Void, Never>?
    var serviceState: EditorService.ServiceState = .stopped
    var snapshot: EditorSnapshot?
    /// Last server-authoritative document state; `snapshot` may include predictions.
    @ObservationIgnored var authoritativeSnapshot: EditorSnapshot?
    /// Timeline edits shown on screen but not saved yet, oldest first.
    @ObservationIgnored var pendingPredictions: [(id: UUID, change: OptimisticTimeline.Change)] = []
    /// The last queued edit; the next one waits for it.
    @ObservationIgnored var editTail: Task<Void, Never>?
    var plans: [JSONValue] = []
    var library: [JSONValue] = []
    var searchHits: [SearchHit] = []
    var selectedIDs: Set<String> = []
    var recentlyChangedIDs: Set<String> = []
    var selectedAssetID: String?
    var selectedAsset: JSONValue?
    var timelineSourceURLs: [String: URL] = [:]
    var sourceStates: [String: String] = [:]
    var playheadFrame = 0
    var seekGeneration = 0
    private var isScrubbing = false
    var sourceIn: Double?
    var sourceOut: Double?
    var searchText = ""
    var semanticSearchPending = false
    var notice: String? {
        didSet {
            noticeTask?.cancel()
            guard let current = notice else { return }
            noticeTask = Task { [weak self] in
                try? await Task.sleep(for: .seconds(4))
                guard !Task.isCancelled, let self, self.notice == current else { return }
                self.notice = nil
            }
        }
    }
    @ObservationIgnored private var noticeTask: Task<Void, Never>?
    /// Repo-relative path of the open plan, exactly as given to `document.open`.
    var openPlanPath: String?
    var errorMessage: String?
    /// Set when exporting would overwrite a scheduled/posted reel; the view shows a confirmation.
    var workspaceInfo: JSONValue = .object([:])
    var inspectorSchema: JSONValue = .object([:])
    var previewManifest: JSONValue?
    var currentPreviewMode = "interactive"
    var playbackRate = 1.0
    var isPreparingPreview = false
    var displayedPreviewRevision: String?
    var previewFailureMessage: String?
    var pendingPermissions: [JSONValue] = []
    var currentGrants: [JSONValue] = []
    var isPermissionPromptPresented = false
    var activeTab = "source"
    var filters: [String: JSONValue] = [:]

    private var searchTask: Task<Void, Never>?
    private var semanticGeneration = 0
    private var libraryLoadGeneration = 0
    private var expectedSearchFilterHash: String?
    private var launchPlanPath: String?
    private var capturePreviewAttempted = false
    private var connectGeneration = 0

    struct StartupFailure: Equatable {
        var title: String
        var detail: String
        var logLines: [String]
    }

    init() {
        let args = ProcessInfo.processInfo.arguments
        launchPlanPath = Self.argument("--plan", in: args)
        showPicker = launchPlanPath == nil
        let saved = UserDefaults.standard.string(forKey: RepositoryLocator.defaultsKey)
        if let candidate = RepositoryLocator.candidatePath(
            arguments: args, environment: ProcessInfo.processInfo.environment, saved: saved) {
            do {
                repositoryRoot = try RepositoryLocator.validate(URL(fileURLWithPath: candidate))
            } catch {
                startupFailure = StartupFailure(
                    title: "Can't use that folder", detail: error.localizedDescription, logLines: [])
            }
        }
        wireCallbacks()
    }

    private func wireCallbacks() {
        service.onState = { [weak self] state in
            guard let self else { return }
            self.serviceState = state
            if case .failed(let message) = state, self.startupFailure == nil {
                self.startupFailure = StartupFailure(
                    title: "The editing service stopped", detail: message, logLines: self.service.logTail())
            }
        }
        service.onEvent = { [weak self] event in self?.receive(event) }
        AudioVisualCache.shared.configure(service: service)
        importer.call = { [weak self] method, params in
            guard let self else { throw ServiceFailure(code: "gone", message: "The app is closing.", details: nil) }
            return try await self.service.call(method, params: params)
        }
        music.call = { [weak self] method, params in
            guard let self else { throw ServiceFailure(code: "gone", message: "The app is closing.", details: nil) }
            return try await self.service.call(method, params: params)
        }
        music.onToast = { [weak self] message in self?.showToast(message) }
        importer.onToast = { [weak self] message in self?.showToast(message) }
        importer.onLibraryNeedsReload = { [weak self] in
            self?.loadLibrary()
            Task { await self?.refreshPlans() }
        }
    }

    var needsRepositoryChoice: Bool { repositoryRoot == nil }

    var hasDocument: Bool { snapshot != nil }
    var selectedNode: EditorNode? { snapshot?.nodes.first { selectedIDs.contains($0.id) } }
    var selectedCutaway: EditorNode? {
        selectedNode?.kind.lowercased() == "cutaway" ? selectedNode : nil
    }
    var selectedPrimaryForBroll: EditorNode? {
        guard let node = selectedNode, let snapshot else { return nil }
        if node.kind.lowercased() == "segment" { return node }
        guard let parentID = node.parentId else { return nil }
        return snapshot.nodes.first { $0.id == parentID && $0.kind.lowercased() == "segment" }
    }
    var fps: Double { max(snapshot?.fps ?? 30, 1) }
    var previewURL: URL? {
        let raw = previewManifest?["url"]?.stringValue
            ?? previewManifest?["staticURL"]?.stringValue
            ?? previewManifest?["programURL"]?.stringValue
            ?? previewManifest?["capabilityURL"]?.stringValue
            ?? snapshot?.preview["url"]?.stringValue
            ?? snapshot?.preview["staticURL"]?.stringValue
            ?? snapshot?.preview["programURL"]?.stringValue
            ?? snapshot?.preview["capabilityURL"]?.stringValue
        guard let raw else { return nil }
        return URL(string: raw)
    }
    var previewOrigin: URL? {
        guard let raw = workspaceInfo["previewOrigin"]?.stringValue else { return nil }
        return URL(string: raw)
    }
    var previewRevision: String? {
        previewManifest?["revision"]?.stringValue
            ?? previewManifest?["revisions"]?["document"]?.stringValue
            ?? previewManifest?["documentRevision"]?.stringValue
            ?? snapshot?.revisions.document
    }
    var isExactPreview: Bool {
        (previewManifest?["mode"]?.stringValue ?? snapshot?.preview["mode"]?.stringValue) == "exact"
    }
    var exactMovieURL: URL? {
        guard isExactPreview else { return nil }
        let raw = previewManifest?["movieURL"]?.stringValue
            ?? previewManifest?["previewVideoURL"]?.stringValue
            ?? previewManifest?["reelURL"]?.stringValue
            ?? previewManifest?["url"]?.stringValue
            ?? snapshot?.preview["movieURL"]?.stringValue
            ?? snapshot?.preview["previewVideoURL"]?.stringValue
            ?? snapshot?.preview["url"]?.stringValue
        guard let raw, let url = URL(string: raw), let previewOrigin,
              Self.sameOrigin(url, previewOrigin) else { return nil }
        return url
    }
    var selectedSourceStatus: String? {
        guard let selectedAssetID else { return nil }
        return sourceStates[selectedAssetID]
    }
    var sourceURL: URL? {
        if let id = selectedAssetID, let prepared = timelineSourceURLs[id] { return prepared }
        let kind = (selectedAsset?["type"]?.stringValue ?? selectedAsset?["projection"]?.stringValue ?? "").lowercased()
        guard !kind.contains("360"), selectedAsset?["is360"]?.boolValue != true else { return nil }
        let raw = selectedAsset?["sourceURL"]?.stringValue ?? selectedAsset?["previewURL"]?.stringValue
        guard let raw else { return nil }
        if raw.hasPrefix("/") { return URL(fileURLWithPath: raw) }
        return URL(string: raw)
    }
    var selectedAssetIsPhoto: Bool {
        guard let selectedAsset else { return false }
        let path = selectedAsset["media"]?.stringValue ?? selectedAsset["filename"]?.stringValue
            ?? selectedAsset["name"]?.stringValue ?? selectedAsset["sourceURL"]?.stringValue
        return NativeMediaKind.isPhoto(
            kind: selectedAsset["type"]?.stringValue ?? selectedAsset["kind"]?.stringValue,
            path: path
        )
    }
    var activePermission: JSONValue? { pendingPermissions.first }
    var permissionPrompt: String {
        guard let request = activePermission else { return "" }
        let reason = request["reason"]?.stringValue ?? "This edit requires explicit permission."
        let scope = request["scope"]?.jsonData(pretty: true).flatMap { String(data: $0, encoding: .utf8) }
        return [reason, scope].compactMap { $0 }.joined(separator: "\n\nScope:\n")
    }

    /// Launch/retry entry point. Resolves the repo (asking once if needed), then starts the service.
    func connect() {
        if repositoryRoot == nil {
            guard startupFailure == nil else { return }
            chooseRepository()
            return
        }
        guard let repositoryRoot else { return }
        startupFailure = nil
        errorMessage = nil
        connectGeneration += 1
        let generation = connectGeneration
        Task {
            let loginPath = await Self.cachedLoginPath()
            guard generation == connectGeneration else { return }
            let environment = AppEnvironment.serviceEnvironment(
                base: ProcessInfo.processInfo.environment, loginPath: loginPath)
            do {
                try service.start(repositoryRoot: repositoryRoot, environment: environment)
            } catch {
                serviceState = .failed(error.localizedDescription)
                startupFailure = StartupFailure(
                    title: "Could not launch the editing service",
                    detail: error.localizedDescription, logLines: service.logTail())
                return
            }
            do {
                workspaceInfo = try await service.call("workspace.info")
                syncPermissions(from: workspaceInfo)
                loadLibrary()
                await refreshPlans()
                importer.refreshInbox()
                if let launchPlanPath, !launchPlanPath.isEmpty {
                    await openPlan(launchPlanPath)
                    self.launchPlanPath = nil
                }
            } catch {
                if case .failed = serviceState {
                    // The failure screen already shows the service's own last lines.
                } else {
                    startupFailure = StartupFailure(
                        title: "The editing service didn't start",
                        detail: error.localizedDescription, logLines: service.logTail())
                }
            }
        }
    }

    private static let loginPathBox = LoginPathBox()
    private static func cachedLoginPath() async -> String? { await loginPathBox.value() }

    /// First-run / failure recovery: ask for the cycling folder, validate it, remember it, connect.
    func chooseRepository() {
        let panel = NSOpenPanel()
        panel.title = "Choose your cycling folder"
        panel.message = "Pick the folder that contains the reels launcher and your plans folder."
        panel.prompt = "Choose"
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        if let repositoryRoot { panel.directoryURL = repositoryRoot }
        guard panel.runModal() == .OK, let url = panel.url else {
            if repositoryRoot == nil && startupFailure == nil {
                startupFailure = StartupFailure(
                    title: "Choose your cycling folder",
                    detail: "Reels Editor needs the folder that holds your reels launcher and plans.", logLines: [])
            }
            return
        }
        do {
            let valid = try RepositoryLocator.validate(url)
            RepositoryLocator.save(valid)
            service.stopSynchronously(grace: 1)
            repositoryRoot = valid
            snapshot = nil
            authoritativeSnapshot = nil
            plans = []
            planSummaries = []
            plansLoaded = false
            showPicker = true
            startupFailure = nil
            connect()
        } catch {
            startupFailure = StartupFailure(
                title: "Can't use that folder", detail: error.localizedDescription, logLines: [])
        }
    }

    func retryConnection() {
        service.stopSynchronously(grace: 1)
        startupFailure = nil
        serviceState = .stopped
        connect()
    }

    func showLog() {
        let url = LogRing.defaultFileURL
        if FileManager.default.fileExists(atPath: url.path) { NSWorkspace.shared.open(url) }
        else { NSWorkspace.shared.activateFileViewerSelecting([url.deletingLastPathComponent()]) }
    }

    func openInClaudeCode() {
        guard let repositoryRoot else { return }
        if !ClaudeCodeLauncher.open(repo: repositoryRoot) {
            errorMessage = "Could not open Terminal. Run claude in \(repositoryRoot.path) yourself."
        }
    }

    func showToast(_ message: String) {
        toast = message
        toastTask?.cancel()
        toastTask = Task {
            try? await Task.sleep(for: .seconds(3.5))
            guard !Task.isCancelled else { return }
            toast = nil
        }
    }

    // MARK: Projects

    func refreshPlans() async {
        do {
            let result = try await service.call("plans.list")
            plans = Self.items(from: result, keys: ["plans", "items"])
            planSummaries = PlanSummary.parse(result)
            plansLoaded = true
        } catch {
            plansLoaded = true
            if case .connected = serviceState { errorMessage = error.localizedDescription }
        }
    }

    func createProject(title: String) async {
        let trimmed = title.trimmingCharacters(in: .whitespacesAndNewlines)
        var params: [String: JSONValue] = [:]
        if !trimmed.isEmpty { params["title"] = .string(trimmed) }
        do {
            let result = try await service.call("projects.create", params: .object(params))
            guard let path = result["path"]?.stringValue else {
                errorMessage = "The service created a reel but did not say where."
                return
            }
            await refreshPlans()
            await openPlan(path)
        } catch { errorMessage = error.localizedDescription }
    }

    func beginNewReel() {
        newReelTitle = ""
        showPicker = true
        isNamingNewReel = true
    }

    func revealPlan(_ path: String) {
        guard let repositoryRoot else { return }
        NSWorkspace.shared.activateFileViewerSelecting([repositoryRoot.appending(path: path)])
    }

    func openPlan(_ path: String) async {
        do {
            previewManifest = nil
            displayedPreviewRevision = nil
            _ = try await service.call("document.open", params: .object(["path": .string(path)]))
            authoritativeSnapshot = nil
            await refreshSnapshot()
            if snapshot != nil { showPicker = false; openPlanPath = path }
            notice = "Project opened"
        } catch { errorMessage = error.localizedDescription }
    }

    func refreshSnapshot() async {
        do {
            let value = try await service.call("document.snapshot")
            syncPermissions(from: value)
            if let next = EditorSnapshot.decode(value) {
                authoritativeSnapshot = next
                snapshot = withPendingPredictions(next)
                inspectorSchema = next.inspectorSchema ?? inspectorSchema
                if next.preview["state"]?.stringValue == "ready" {
                    previewManifest = next.preview
                    NotificationCenter.default.post(name: .reelsEditorPreviewReady, object: next.preview)
                }
                requestTimelineMedia(next.nodes.compactMap(\.assetId))
                displayedPreviewRevision = next.preview["documentRevision"]?.stringValue
                    ?? next.preview["revision"]?.stringValue
                playheadFrame = min(playheadFrame, next.durationFrames)
                selectedIDs.formIntersection(Set(next.nodes.map(\.id)))
                if selectedIDs.isEmpty,
                   let firstPrimary = next.nodes.first(where: { $0.kind.lowercased() == "segment" }) {
                    selectNode(firstPrimary.id)
                    if let assetID = firstPrimary.assetId {
                        let asset = library.first(where: { $0["id"]?.stringValue == assetID })
                            ?? .object(["id": .string(assetID)])
                        selectAsset(asset)
                    }
                }
                await loadInspectorSchema(for: selectedNode?.id ?? "project")
                NotificationCenter.default.post(name: .reelsEditorDocumentReady, object: nil)
                if ProcessInfo.processInfo.arguments.contains("--capture-window"),
                   !capturePreviewAttempted,
                   !["ready", "preparing"].contains(next.preview["state"]?.stringValue ?? "") {
                    capturePreviewAttempted = true
                    preparePreview(mode: "interactive")
                }
            } else {
                errorMessage = "The service returned a snapshot the app could not read."
            }
        } catch { errorMessage = error.localizedDescription }
    }

    func selectNode(_ id: String, additive: Bool = false) {
        if additive {
            if selectedIDs.contains(id) { selectedIDs.remove(id) } else { selectedIDs.insert(id) }
        } else {
            selectedIDs = [id]
        }
        Task {
            _ = try? await service.call("selection.set", params: .object([
                "ids": .array(selectedIDs.sorted().map(JSONValue.string)),
                "playhead": .number(Double(playheadFrame))
            ]))
            await persistState()
            await loadInspectorSchema(for: id)
        }
    }

    func setPlayhead(_ frame: Int, final: Bool = true) {
        isScrubbing = !final
        playheadFrame = max(0, min(snapshot?.durationFrames ?? frame, frame))
        seekGeneration += 1
        guard final else { return }
        Task {
            _ = try? await service.call("selection.set", params: .object([
                "ids": .array(selectedIDs.sorted().map(JSONValue.string)),
                "playhead": .number(Double(playheadFrame))
            ]))
            await persistState()
        }
    }

    func updatePlayheadFromPreview(_ frame: Int, revision: String?) {
        guard !isScrubbing, let snapshot,
              revision == nil || revision == snapshot.revisions.document || revision == displayedPreviewRevision else { return }
        playheadFrame = max(0, min(snapshot.durationFrames, frame))
    }

    func hit(for id: String) -> SearchHit? { searchHits.first { $0.id == id } }

    func loadInspectorSchema(for id: String) async {
        if let schema = try? await service.call("inspector.schema", params: .object(["id": .string(id)])) {
            inspectorSchema = schema
        }
    }

    /// Sends one edit batch. Edits run strictly one after another (each built on the
    /// revision the previous one produced), so quick back-to-back gestures never race.
    /// `predict` moves clips on screen immediately; the saved snapshot replaces it.
    func apply(_ operations: [JSONValue], reason: String, predict: OptimisticTimeline.Change? = nil) async {
        let token = UUID()
        if let predict, let current = snapshot {
            pendingPredictions.append((token, predict))
            snapshot = OptimisticTimeline.apply(predict, to: current)
        }
        let previous = editTail
        let task = Task { @MainActor in
            await previous?.value
            await self.send(operations, reason: reason, token: token)
        }
        editTail = task
        await task.value
    }

    private func send(_ operations: [JSONValue], reason: String, token: UUID) async {
        defer { pendingPredictions.removeAll { $0.id == token } }
        guard let visibleSnapshot = snapshot else { return }
        let baseSnapshot = authoritativeSnapshot ?? visibleSnapshot
        let baseRevisions = baseSnapshot.revisions
        var batch: [String: JSONValue] = [
            "expected": .object([
                "document": .string(baseRevisions.document),
                "library": .string(baseRevisions.library),
                "renderer": .string(baseRevisions.renderer)
            ]),
            "reason": .string(reason),
            "origin": .string("manual"),
            "operations": .array(operations)
        ]
        if let baseSnapshotToken = authoritativeSnapshot?.snapshotToken {
            batch["responseMode"] = .string("delta")
            batch["baseRevisions"] = .object([
                "document": .string(baseRevisions.document),
                "library": .string(baseRevisions.library),
                "renderer": .string(baseRevisions.renderer)
            ])
            batch["baseSnapshotToken"] = .string(baseSnapshotToken)
        }
        do {
            let value = try await service.call("document.apply", params: .object(["batch": .object(batch)]))
            pendingPredictions.removeAll { $0.id == token }
            if !reconcileApplyResponse(value, baseDocumentId: baseSnapshot.documentId,
                                       baseRevisions: baseRevisions) {
                await refreshSnapshot()
            }
            notice = reason
        } catch {
            pendingPredictions.removeAll { $0.id == token }
            errorMessage = error.localizedDescription
            await refreshSnapshot()
        }
    }

    @discardableResult
    func reconcileApplyResponse(_ value: JSONValue, baseDocumentId: String,
                                baseRevisions: Revisions) -> Bool {
        if let authoritativeSnapshot,
           let next = EditorSnapshot.applyingDelta(value, to: authoritativeSnapshot) {
            self.authoritativeSnapshot = next
            snapshot = withPendingPredictions(next)
            inspectorSchema = next.inspectorSchema ?? inspectorSchema
            return true
        }
        guard let next = EditorSnapshot.decode(value), next.documentId == baseDocumentId else {
            return false
        }
        // A full response is the compatibility/stale-base fallback. Do not roll
        // back a newer authoritative snapshot delivered by an intervening event.
        let currentRevision = authoritativeSnapshot?.revisions.document
        guard currentRevision == nil || currentRevision == baseRevisions.document
                || currentRevision == next.revisions.document else {
            return false
        }
        authoritativeSnapshot = next
        snapshot = withPendingPredictions(next)
        inspectorSchema = next.inspectorSchema ?? inspectorSchema
        return true
    }

    /// A saved snapshot plus the edits still on their way, so queued moves don't flick back.
    func withPendingPredictions(_ next: EditorSnapshot) -> EditorSnapshot {
        pendingPredictions.reduce(next) { OptimisticTimeline.apply($1.change, to: $0) }
    }

    func editField(nodeID: String, path: String, value: JSONValue) {
        if snapshot?.nodes.first(where: { $0.id == nodeID })?.isReadOnly == true {
            notice = "Generated caption timing is read-only; correct transcript words instead."
            return
        }
        Task {
            await apply([.object([
                "op": .string("set_field"),
                "id": .string(nodeID),
                "path": .string(path),
                "value": value
            ])], reason: "Adjust \(path.split(separator: "/").last ?? "property")")
        }
    }

    func insertInspectorOverlay(parentID: String, collection: String, value: JSONValue) {
        if snapshot?.nodes.first(where: { $0.id == parentID })?.isReadOnly == true {
            notice = "Generated caption timing is read-only."
            return
        }
        Task {
            await apply([.object([
                "op": .string("insert_overlay"),
                "parentId": .string(parentID),
                "collection": .string(collection),
                "value": value
            ])], reason: "Add \(collection)")
        }
    }

    func removeInspectorItem(_ id: String) {
        guard let node = snapshot?.nodes.first(where: { $0.id == id }) else { return }
        guard !node.isReadOnly else {
            notice = "Generated caption timing is read-only."
            return
        }
        let operation: JSONValue
        if node.kind.lowercased() == "segment" {
            operation = .object([
                "op": .string("remove_segments"),
                "ids": .array([.string(id)])
            ])
        } else {
            operation = .object([
                "op": .string("remove_overlay"),
                "id": .string(id)
            ])
        }
        Task { await apply([operation], reason: "Remove \(node.kind)") }
    }

    func setKeyframes(nodeID: String, path: String, value: JSONValue) async {
        guard snapshot?.nodes.first(where: { $0.id == nodeID })?.isReadOnly != true else {
            notice = "Generated caption timing is read-only."
            return
        }
        await apply([.object([
            "op": .string("set_keyframes"),
            "id": .string(nodeID),
            "path": .string(path),
            "value": value
        ])], reason: "Edit keyframes")
    }

    func undo() { Task { await history(method: "document.undo") } }
    func redo() { Task { await history(method: "document.redo") } }

    private func history(method: String) async {
        do {
            let value = try await service.call(method, params: .object([
                "expected": .object([
                    "document": .string(snapshot?.revisions.document ?? ""),
                    "library": .string(snapshot?.revisions.library ?? ""),
                    "renderer": .string(snapshot?.revisions.renderer ?? "")
                ])
            ]))
            if let next = EditorSnapshot.decode(value) {
                authoritativeSnapshot = next
                snapshot = withPendingPredictions(next)
            }
            else { await refreshSnapshot() }
        } catch { errorMessage = error.localizedDescription }
    }

    func splitAtPlayhead() {
        guard let node = selectedNode, !node.isReadOnly,
              node.kind.lowercased().contains("segment") || node.lane.lowercased().contains("video") else { return }
        Task {
            await apply([.object([
                "op": .string("split_segment"),
                "id": .string(node.id),
                "atFrame": .number(Double(playheadFrame))
            ])], reason: "Split clip")
        }
    }

    func deleteSelection() {
        guard !selectedIDs.isEmpty else { return }
        let nodes = snapshot?.nodes.filter { selectedIDs.contains($0.id) && !$0.isReadOnly } ?? []
        guard !nodes.isEmpty else {
            notice = "Generated caption timing is read-only."
            return
        }
        let segments = nodes.filter { $0.kind.lowercased() == "segment" }.map(\.id)
        let overlays = nodes.filter { $0.kind.lowercased() != "segment" }
        var operations: [JSONValue] = []
        if !segments.isEmpty {
            operations.append(.object([
                "op": .string("remove_segments"),
                "ids": .array(segments.map(JSONValue.string))
            ]))
        }
        operations.append(contentsOf: overlays.map { .object(["op": .string("remove_overlay"), "id": .string($0.id)]) })
        Task {
            await apply(operations, reason: "Delete selection")
        }
    }

    func acquireTimelineLease(owner: String) async -> String? {
        // The lease only keeps AI edits out during a drag; failing to get one never
        // blocks the user's own edit, so it isn't worth an error dialog.
        let result = try? await service.call("lease.acquire", params: .object(["owner": .string(owner)]))
        return result?["token"]?.stringValue
    }

    /// Gives back a gesture lease.
    func releaseTimelineLease(_ token: String) {
        Task { _ = try? await service.call("lease.release", params: .object(["token": .string(token)])) }
    }

    func reorder(ids: [String], beforeID: String?) {
        var op: [String: JSONValue] = [
            "op": .string("reorder_segments"),
            "ids": .array(ids.map(JSONValue.string))
        ]
        if let beforeID { op["beforeId"] = .string(beforeID) }
        let predict = ids.count == 1 ? OptimisticTimeline.reorderSegment(id: ids[0], beforeID: beforeID) : nil
        Task { await apply([.object(op)], reason: "Reorder clips", predict: predict) }
    }

    /// A clip dropped after a horizontal drag. `toFrame` is where its start landed.
    func moveTimelineItem(_ node: EditorNode, toFrame: Int, beforeID: String?) {
        if node.isReadOnly || (node.generated == true && node.kind.lowercased() != "generated_sfx") {
            notice = "Generated caption timing is read-only."
        } else if node.kind.lowercased() == "generated_sfx" {
            Task {
                await apply([.object([
                    "op": .string("move_overlay"),
                    "id": .string(node.id),
                    "at": .number(Double(max(0, toFrame)) / fps)
                ])], reason: "Move generated sound cue", predict: OptimisticTimeline.move(id: node.id, toFrame: toFrame))
            }
        } else if node.kind.lowercased() == "cutaway" || node.parentId != nil || Self.isOverlay(node) {
            let parentStart = snapshot?.nodes.first(where: { $0.id == node.parentId })?.startFrame ?? 0
            let localFrame = max(0, toFrame - parentStart)
            Task {
                await apply([.object([
                    "op": .string("move_overlay"),
                    "id": .string(node.id),
                    "at": .number(Double(localFrame) / fps)
                ])], reason: "Move overlay",
                predict: OptimisticTimeline.move(id: node.id, toFrame: parentStart + localFrame))
            }
        } else if node.kind.lowercased().contains("segment")
                    || node.lane.lowercased().contains("video")
                    || node.lane.lowercased().contains("primary") {
            let ordered = OptimisticTimeline.segments(snapshot?.nodes ?? [])
            guard let slot = OptimisticTimeline.dropSlot(moving: node, landingStart: toFrame, segments: ordered) else {
                return // dropped back where it was
            }
            reorder(ids: [node.id], beforeID: slot.beforeID)
        }
    }

    @discardableResult
    func overwriteTimeline(_ placements: [TimelinePlacement], editID: String,
                           insertionAssets: [String: JSONValue] = [:]) -> Bool {
        // Placements may refer to deterministic survivor IDs from a queued
        // prediction, so validate node identity/geometry against the visible view.
        let state = snapshot ?? authoritativeSnapshot
        guard let state, let capabilities = (authoritativeSnapshot ?? state).timelineCapabilities,
              capabilities.version == 1, capabilities.idAlgorithm == "sha256-v1",
              capabilities.overwrite else {
            notice = "Overwrite editing is unavailable for this renderer."
            return false
        }
        let availableTracks = state.tracks
        guard (1...500).contains(placements.count), !editID.contains("\0") else {
            notice = "This overwrite selection is not valid."
            return false
        }
        guard let placements = Self.canonicalizeOverwritePlacements(placements, tracks: availableTracks),
              placements.allSatisfy({ $0.atFrame >= 0 && !$0.track.contains("\0") }) else {
            notice = "This overwrite placement is not valid."
            return false
        }
        if placements.count > 1 && !capabilities.multiMove {
            notice = "Multi-clip overwrite is unavailable for this renderer."
            return false
        }
        let nodesByID = Dictionary(uniqueKeysWithValues: state.nodes.map { ($0.id, $0) })
        for placement in placements {
            guard !placement.track.contains("\0"),
                  let target = availableTracks.first(where: {
                      ($0.canonicalId ?? $0.id) == placement.track || $0.id == placement.track
                  }),
                  target.isVideo, !target.locked else {
                notice = "The destination video track is locked or unavailable."
                return false
            }
            if let id = placement.id {
                guard !id.contains("\0"), let node = nodesByID[id],
                      ["segment", "cutaway"].contains(node.kind.lowercased()), !node.isReadOnly else {
                    notice = "A selected source clip cannot be moved."
                    return false
                }
                let sourceID = node.track ?? Self.defaultTrackID(kind: node.kind)
                guard let source = availableTracks.first(where: {
                    ($0.canonicalId ?? $0.id) == sourceID || $0.id == sourceID
                }), source.isVideo, !source.locked else {
                    notice = "A selected source track is locked or unavailable."
                    return false
                }
                if (source.canonicalId ?? source.id) != placement.track && !capabilities.crossTrack {
                    notice = "Cross-track overwrite is unavailable for this renderer."
                    return false
                }
                if node.kind.lowercased() == "cutaway",
                   placement.track == "V1", (source.canonicalId ?? source.id) != "V1",
                   !capabilities.cutawayPromotion {
                    notice = "Cutaway promotion is unavailable for this renderer."
                    return false
                }
            } else {
                guard let insert = placement.insert, capabilities.libraryInsert,
                      let asset = insert["asset"]?.stringValue, !asset.isEmpty, !asset.contains("\0") else {
                    notice = "Library insertion is unavailable or invalid for this renderer."
                    return false
                }
            }
        }
        let fps = max(1, Int(state.fps.rounded(.toNearestOrEven)))
        let regions = OptimisticTimeline.overwriteRegions(placements: placements, nodes: state.nodes, fps: fps)
        if regions.contains(where: { region in
            state.nodes.first(where: { $0.id == region.nodeID })?.isReadOnly != false
        }) {
            notice = "An affected destination clip cannot be overwritten."
            return false
        }
        var assets = Dictionary(library.compactMap { item -> (String, JSONValue)? in
            guard let id = item["id"]?.stringValue ?? item["assetId"]?.stringValue else { return nil }
            return (id, item)
        }, uniquingKeysWith: { _, newer in newer })
        if let selectedAssetID, let selectedAsset { assets[selectedAssetID] = selectedAsset }
        assets.merge(insertionAssets, uniquingKeysWith: { _, newer in newer })
        if availableTracks.first(where: { ($0.canonicalId ?? $0.id) == "A1" })?.locked == true,
           Self.overwriteChangesLockedAudio(
               placements: placements, regions: regions, nodes: state.nodes, assets: assets
           ) {
            notice = "This edit would change linked source audio on locked track A1."
            return false
        }
        guard !OptimisticTimeline.hasSameTrackPlacementOverlap(placements, nodes: state.nodes, fps: fps) else {
            notice = "Selected clips overlap each other on the destination track."
            return false
        }
        let stableEditID = editID.lowercased()
        let operation = Self.overwriteOperation(placements: placements, editID: stableEditID)
        Task {
            await apply([operation], reason: "Overwrite timeline clips",
                        predict: OptimisticTimeline.overwrite(placements: placements,
                                                              editID: stableEditID, fps: fps))
        }
        return true
    }

    static func canonicalizeOverwritePlacements(_ placements: [TimelinePlacement],
                                                tracks: [EditorTrack]) -> [TimelinePlacement]? {
        var result: [TimelinePlacement] = []
        result.reserveCapacity(placements.count)
        for placement in placements {
            guard let track = tracks.first(where: {
                ($0.canonicalId ?? $0.id) == placement.track || $0.id == placement.track
            }) else { return nil }
            var canonical = placement
            canonical.track = track.canonicalId ?? track.id
            result.append(canonical)
        }
        return result
    }

    static func overwriteOperation(placements: [TimelinePlacement], editID: String) -> JSONValue {
        .object([
            "op": .string("move_segments"),
            "editId": .string(editID.lowercased()),
            "mode": .string("overwrite"),
            "placements": .array(placements.map(\.wireValue)),
        ])
    }

    static func overwriteChangesLockedAudio(placements: [TimelinePlacement],
                                            regions: [TimelineOverwriteRegion],
                                            nodes: [EditorNode],
                                            assets: [String: JSONValue] = [:]) -> Bool {
        let audioOwners = Set(nodes.compactMap { node -> String? in
            guard node.kind.lowercased() == "source_audio", node.isReadOnly else { return nil }
            return node.parentId
        })
        let nodesByID = Dictionary(uniqueKeysWithValues: nodes.map { ($0.id, $0) })
        if placements.contains(where: { placement in
            if let insert = placement.insert {
                let metadata = insert["asset"]?.stringValue.flatMap { assets[$0] }
                return insertionHasAudio(insert, metadata: metadata) ?? true
            }
            guard let id = placement.id, audioOwners.contains(id), let owner = nodesByID[id] else { return false }
            return placement.atFrame != owner.startFrame
        }) {
            return true
        }
        return regions.contains { audioOwners.contains($0.nodeID) }
    }

    static func insertionHasAudio(_ insert: JSONValue, metadata: JSONValue?) -> Bool? {
        if insert["volume"]?.numberValue == 0 { return false }
        let path = metadata?["media"]?.stringValue ?? metadata?["filename"]?.stringValue
            ?? insert["asset"]?.stringValue
        let kind = metadata?["kind"]?.stringValue ?? metadata?["type"]?.stringValue
            ?? metadata?["mimeType"]?.stringValue
        if NativeMediaKind.isPhoto(kind: kind, path: path) { return false }
        if let probe = metadata?["probe"], case .object = probe {
            return probe["hasAudio"]?.boolValue ?? false
        }
        return metadata?["hasAudio"]?.boolValue
    }

    func trim(node: EditorNode, sourceIn: Double, sourceOut: Double, ghost: TimelineGhost) {
        let operation: JSONValue
        let reason: String
        var predict: OptimisticTimeline.Change?
        if node.isReadOnly || (node.generated == true && node.kind.lowercased() != "generated_sfx") {
            notice = "Generated caption timing is read-only."
            return
        } else if node.kind.lowercased() == "cutaway", node.isPhoto {
            let parentStart = snapshot?.nodes.first(where: { $0.id == node.parentId })?.startFrame ?? 0
            operation = .object([
                "op": .string("resize_overlay"),
                "id": .string(node.id),
                "at": .number(max(0, sourceIn - Double(parentStart) / fps)),
                "for": .number(min(12, max(0.8, sourceOut)))
            ])
            reason = "Resize photo cutaway"
            predict = OptimisticTimeline.resize(id: node.id, start: ghost.start, end: ghost.end)
        } else if node.kind.lowercased() == "cutaway", sourceOut > sourceIn {
            operation = BrollOperationBuilder.trim(
                cutawayID: node.id, sourceIn: sourceIn, sourceOut: sourceOut,
                minimumDuration: 1.0 / fps
            )
            reason = "Trim B-roll source"
            predict = OptimisticTimeline.resize(id: node.id, start: ghost.start, end: ghost.end)
        } else if node.kind.lowercased() == "segment", node.isPhoto {
            operation = .object([
                "op": .string("set_field"),
                "id": .string(node.id),
                "path": .string("/duration"),
                "value": .number(min(12, max(0.8, sourceOut)))
            ])
            reason = "Adjust photo duration"
            predict = OptimisticTimeline.trimSegment(id: node.id, length: ghost.end - ghost.start)
        } else if node.kind.lowercased() == "generated_sfx" {
            notice = "Generated sound cues can be moved but not edge-trimmed."
            return
        } else if Self.isOverlay(node) {
            operation = .object([
                "op": .string("resize_overlay"),
                "id": .string(node.id),
                "at": .number(max(0, sourceIn)),
                "for": .number(max(1 / fps, sourceOut))
            ])
            reason = "Resize overlay"
            predict = OptimisticTimeline.resize(id: node.id, start: ghost.start, end: ghost.end)
        } else if sourceOut > sourceIn {
            operation = .object([
                "op": .string("trim_segment"),
                "id": .string(node.id),
                "sourceIn": .number(max(0, sourceIn)),
                "sourceOut": .number(max(sourceIn + 1.0 / fps, sourceOut))
            ])
            reason = "Trim clip"
            predict = node.parentId == nil
                ? OptimisticTimeline.trimSegment(id: node.id, length: ghost.end - ghost.start)
                : OptimisticTimeline.resize(id: node.id, start: ghost.start, end: ghost.end)
        } else {
            return
        }
        Task { await apply([operation], reason: reason, predict: predict) }
    }

    private static func isOverlay(_ node: EditorNode) -> Bool {
        let kind = "\(node.kind) \(node.lane)".lowercased()
        return node.parentId != nil || kind.contains("overlay")
            || kind.contains("caption") || kind.contains("text")
            || kind.contains("cutaway") || kind.contains("b-roll") || kind.contains("broll")
    }

    func insertSearchHit(_ hit: SearchHit, beforeID: String? = nil) {
        _ = beforeID // Kept for source compatibility; placement is now explicit, never selection-relative.
        _ = insertDroppedItem(hit.id, atFrame: playheadFrame, trackID: "V1",
                              sourceIn: hit.sourceIn, sourceOut: hit.sourceOut)
    }

    func previewTimelineDrop(_ id: String, atFrame: Int, trackID: String,
                             commandInsert: Bool) -> TimelineDropResolution {
        guard let state = authoritativeSnapshot ?? snapshot,
              let capabilities = state.timelineCapabilities,
              capabilities.version == 1, capabilities.idAlgorithm == "sha256-v1",
              capabilities.overwrite else {
            return .rejected("Timeline overwrite is unavailable for this renderer.")
        }
        if commandInsert {
            return .rejected(capabilities.insert
                ? "Insert mode is not supported by this editor yet."
                : "Command-drag insert is unavailable; drag normally to overwrite.")
        }
        guard capabilities.libraryInsert else {
            return .rejected("Library footage drops are unavailable for this renderer.")
        }
        guard let target = state.tracks.first(where: {
            ($0.canonicalId ?? $0.id) == trackID || $0.id == trackID
        }), target.isVideo, !target.locked else {
            return .rejected("Drop footage on an unlocked video track.")
        }

        let value: JSONValue?
        if let hit = hit(for: id) {
            value = Self.timelineInsertSpec(
                assetID: hit.assetId, metadata: hit.raw,
                sourceIn: hit.sourceIn, sourceOut: hit.sourceOut, moment: hit.raw["moment"]
            )
        } else if let metadata = (id == selectedAssetID ? selectedAsset : nil)
                    ?? library.first(where: { $0["id"]?.stringValue == id }) {
            value = Self.timelineInsertSpec(
                assetID: id, metadata: metadata,
                sourceIn: id == selectedAssetID ? sourceIn : nil,
                sourceOut: id == selectedAssetID ? sourceOut : nil
            )
        } else {
            return .rejected("That footage is no longer in the library.")
        }
        guard let value else { return .rejected("The selected source range is invalid.") }
        return .accepted(.inserting(value, track: target.canonicalId ?? target.id, atFrame: max(0, atFrame)))
    }

    func addSearchHitAsBroll(_ hit: SearchHit) {
        let photo = isPhotoAsset(hit.assetId)
            || NativeMediaKind.isPhoto(
                kind: hit.raw["type"]?.stringValue ?? hit.raw["assetKind"]?.stringValue,
                path: hit.raw["media"]?.stringValue ?? hit.raw["filename"]?.stringValue
            )
        insertBroll(assetID: hit.assetId, sourceIn: hit.sourceIn, sourceOut: hit.sourceOut, isPhoto: photo)
    }

    func addSelectedAssetAsBroll() {
        guard let selectedAssetID else {
            errorMessage = "Choose a source asset before adding B-roll."
            return
        }
        let duration = selectedAsset?["duration"]?.numberValue
            ?? selectedAsset?["seconds"]?.numberValue
            ?? selectedAsset?["durationSeconds"]?.numberValue
            ?? 10
        let start = max(0, sourceIn ?? 0)
        let end = max(start + 0.3, sourceOut ?? duration)
        insertBroll(assetID: selectedAssetID, sourceIn: start, sourceOut: end, isPhoto: selectedAssetIsPhoto)
    }

    func replaceSelectedCutaway(with hit: SearchHit) {
        let photo = isPhotoAsset(hit.assetId)
            || NativeMediaKind.isPhoto(
                kind: hit.raw["type"]?.stringValue ?? hit.raw["assetKind"]?.stringValue,
                path: hit.raw["media"]?.stringValue ?? hit.raw["filename"]?.stringValue
            )
        replaceSelectedCutaway(
            with: hit.assetId, sourceIn: hit.sourceIn, sourceOut: hit.sourceOut, isPhoto: photo
        )
    }

    func replaceSelectedCutaway(with assetID: String, sourceIn: Double, sourceOut: Double, isPhoto: Bool? = nil) {
        guard let cutaway = selectedCutaway else {
            errorMessage = "Select a B-roll cutaway on the timeline to replace its source."
            return
        }
        guard sourceOut > sourceIn else {
            errorMessage = "Mark a valid source range before replacing this cutaway."
            return
        }
        Task {
            await apply(
                BrollOperationBuilder.replace(
                    cutawayID: cutaway.id, assetID: assetID,
                    sourceIn: sourceIn, sourceOut: sourceOut,
                    isPhoto: isPhoto ?? isPhotoAsset(assetID)
                ),
                reason: "Replace B-roll source"
            )
        }
    }

    func replaceSelectedCutawayFromSource() {
        guard let selectedAssetID else {
            errorMessage = "Choose a source asset before replacing this cutaway."
            return
        }
        let duration = selectedAsset?["duration"]?.numberValue
            ?? selectedAsset?["seconds"]?.numberValue
            ?? selectedAsset?["durationSeconds"]?.numberValue
            ?? 10
        let start = max(0, sourceIn ?? 0)
        let end = max(start + 0.3, sourceOut ?? duration)
        replaceSelectedCutaway(with: selectedAssetID, sourceIn: start, sourceOut: end)
    }

    private func insertBroll(assetID: String, sourceIn: Double, sourceOut: Double, isPhoto: Bool) {
        guard let parent = selectedPrimaryForBroll else {
            errorMessage = "Select a primary video or one of its B-roll items first."
            return
        }
        guard sourceOut > sourceIn else {
            errorMessage = "The B-roll source range must have positive duration."
            return
        }
        let localAt = max(0, Double(playheadFrame - parent.startFrame) / fps)
        Task {
            await apply([
                BrollOperationBuilder.insert(
                    parentID: parent.id, assetID: assetID,
                    sourceIn: sourceIn, sourceOut: sourceOut, at: localAt, isPhoto: isPhoto
                )
            ], reason: "Add B-roll")
        }
    }

    private func isPhotoAsset(_ id: String) -> Bool {
        if id == selectedAssetID, selectedAssetIsPhoto { return true }
        let asset = library.first(where: { $0["id"]?.stringValue == id })
        return NativeMediaKind.isPhoto(
            kind: asset?["type"]?.stringValue ?? asset?["mimeType"]?.stringValue,
            path: asset?["media"]?.stringValue ?? asset?["filename"]?.stringValue
        )
    }

    @discardableResult
    func insertDroppedItem(_ id: String, atFrame: Int, trackID: String,
                           sourceIn: Double? = nil, sourceOut: Double? = nil,
                           commandInsert: Bool = false) -> Bool {
        guard let state = authoritativeSnapshot ?? snapshot,
              let capabilities = state.timelineCapabilities,
              capabilities.version == 1, capabilities.overwrite else {
            notice = "Timeline overwrite is unavailable for this renderer."
            return false
        }
        if commandInsert {
            notice = capabilities.insert
                ? "Insert mode is not supported by this editor yet."
                : "Command-drag insert is unavailable; drag normally to overwrite."
            return false
        }
        guard capabilities.libraryInsert else {
            notice = "Library footage drops are unavailable for this renderer."
            return false
        }
        guard let target = state.tracks.first(where: {
            ($0.canonicalId ?? $0.id) == trackID || $0.id == trackID
        }), target.isVideo, !target.locked else {
            notice = "Drop footage on an unlocked video track."
            return false
        }
        let startFrame = max(0, atFrame)
        if let hit = hit(for: id) {
            guard let value = Self.timelineInsertSpec(
                assetID: hit.assetId, metadata: hit.raw,
                sourceIn: sourceIn ?? hit.sourceIn, sourceOut: sourceOut ?? hit.sourceOut,
                moment: hit.raw["moment"]
            ) else {
                notice = "The selected source range is invalid."
                return false
            }
            return submitTimelineInsert(
                value, assetID: hit.assetId, metadata: hit.raw,
                trackID: target.canonicalId ?? target.id, atFrame: startFrame
            )
        }
        guard library.contains(where: { $0["id"]?.stringValue == id }) || selectedAssetID == id else {
            notice = "That footage is no longer in the library."
            return false
        }
        let markedIn: Double? = id == selectedAssetID
            ? (sourceIn != nil ? sourceIn : self.sourceIn) : sourceIn
        let markedOut: Double? = id == selectedAssetID
            ? (sourceOut != nil ? sourceOut : self.sourceOut) : sourceOut
        let documentID = state.documentId
        Task {
            do {
                let asset = try await service.call("library.asset", params: .object(["id": .string(id)]))
                guard (snapshot ?? authoritativeSnapshot)?.documentId == documentID else { return }
                guard let value = Self.timelineInsertSpec(
                    assetID: id, metadata: asset, sourceIn: markedIn, sourceOut: markedOut
                ) else {
                    notice = "The selected source range is invalid."
                    return
                }
                _ = overwriteTimeline(
                    [.inserting(value, track: target.canonicalId ?? target.id, atFrame: startFrame)],
                    editID: UUID().uuidString.lowercased(), insertionAssets: [id: asset]
                )
            } catch {
                errorMessage = error.localizedDescription
            }
        }
        return true
    }

    private func submitTimelineInsert(_ value: JSONValue, assetID: String, metadata: JSONValue,
                                      trackID: String, atFrame: Int) -> Bool {
        let placements = [TimelinePlacement.inserting(value, track: trackID, atFrame: atFrame)]
        let editID = UUID().uuidString.lowercased()
        let state = snapshot ?? authoritativeSnapshot
        let audioLocked = state?.tracks.contains { ($0.canonicalId ?? $0.id) == "A1" && $0.locked } == true
        if audioLocked, Self.insertionHasAudio(value, metadata: metadata) == nil {
            let documentID = state?.documentId
            Task {
                do {
                    let asset = try await service.call("library.asset", params: .object(["id": .string(assetID)]))
                    guard (snapshot ?? authoritativeSnapshot)?.documentId == documentID else { return }
                    _ = overwriteTimeline(placements, editID: editID, insertionAssets: [assetID: asset])
                } catch {
                    errorMessage = error.localizedDescription
                }
            }
            return true
        }
        return overwriteTimeline(placements, editID: editID, insertionAssets: [assetID: metadata])
    }

    static func timelineInsertSpec(assetID: String, metadata: JSONValue,
                                   sourceIn: Double?, sourceOut: Double?,
                                   moment: JSONValue? = nil) -> JSONValue? {
        let mediaPath = metadata["media"]?.stringValue ?? metadata["filename"]?.stringValue ?? assetID
        let mediaType = metadata["kind"]?.stringValue ?? metadata["type"]?.stringValue
            ?? metadata["mimeType"]?.stringValue
        let isPhoto = NativeMediaKind.isPhoto(kind: mediaType, path: mediaPath)
        let assetDuration = metadata["probe"]?["duration"]?.numberValue
            ?? metadata["sourceDuration"]?.numberValue
            ?? metadata["duration"]?.numberValue
            ?? metadata["seconds"]?.numberValue
            ?? metadata["durationSeconds"]?.numberValue
        let boundedDuration = assetDuration.flatMap { $0.isFinite && $0 > 0 ? $0 : nil }
        let requestedStart = sourceIn ?? metadata["in"]?.numberValue ?? 0
        guard requestedStart.isFinite else { return nil }
        let start = max(0, requestedStart)
        var value: [String: JSONValue] = ["asset": .string(assetID)]
        if isPhoto {
            if let requestedEnd = sourceOut ?? metadata["out"]?.numberValue {
                guard requestedEnd.isFinite else { return nil }
                let end = boundedDuration.map { min(requestedEnd, $0) } ?? requestedEnd
                guard end > start else { return nil }
                value["duration"] = .number(end - start)
            } else {
                value["duration"] = .number(boundedDuration ?? 2.5)
            }
        } else {
            guard let requestedEnd = sourceOut ?? metadata["out"]?.numberValue ?? boundedDuration,
                  requestedEnd.isFinite else { return nil }
            let end = boundedDuration.map { min(requestedEnd, $0) } ?? requestedEnd
            guard end > start else { return nil }
            value["in"] = .number(start)
            value["out"] = .number(end)
            if let speed = metadata["speed"]?.numberValue, speed.isFinite, speed > 0 {
                value["speed"] = .number(speed)
            }
        }
        if let moment, moment.stringValue != nil { value["moment"] = moment }
        return .object(value)
    }

    func loadLibrary() {
        libraryLoadGeneration += 1
        let generation = libraryLoadGeneration
        Task {
            do {
                var allAssets: [JSONValue] = []
                var offset = 0
                while true {
                    let result = try await service.call("library.list", params: .object([
                        "offset": .number(Double(offset)),
                        "limit": .number(200)
                    ]))
                    let page = Self.items(from: result, keys: ["assets", "items", "library"])
                    allAssets.append(contentsOf: page)
                    guard let nextValue = result["next"]?.numberValue else { break }
                    let next = Int(nextValue)
                    guard next > offset, !page.isEmpty else { break }
                    offset = next
                }
                guard generation == libraryLoadGeneration else { return }
                var seen = Set<String>()
                library = allAssets.filter { item in
                    guard let id = item["id"]?.stringValue ?? item["assetId"]?.stringValue else { return true }
                    return seen.insert(id).inserted
                }
            } catch {
                if generation == libraryLoadGeneration { errorMessage = error.localizedDescription }
            }
        }
    }

    func search(_ query: String) {
        searchText = query
        searchTask?.cancel()
        semanticGeneration += 1
        expectedSearchFilterHash = nil
        let generation = semanticGeneration
        Task {
            _ = try? await service.call("search.cancel", params: .object(["generation": .number(Double(generation - 1))]))
        }
        guard !query.isEmpty else {
            searchHits = []
            semanticSearchPending = false
            loadLibrary()
            return
        }
        semanticSearchPending = false
        searchTask = Task {
            do {
                let result = try await service.call("search.query", params: .object([
                    "query": .string(query),
                    "filters": .object(filters),
                    "generation": .number(Double(generation))
                ]))
                guard !Task.isCancelled, generation == semanticGeneration else { return }
                let responseGeneration = Int(result["generation"]?.numberValue ?? Double(generation))
                guard responseGeneration == generation else { return }
                expectedSearchFilterHash = result["filterHash"]?.stringValue
                searchHits = Self.hits(from: result, related: false, keys: ["matches", "regexRows", "results"])
            } catch {
                guard generation == semanticGeneration else { return }
                errorMessage = error.localizedDescription
            }
            do {
                try await Task.sleep(for: .milliseconds(600))
                guard !Task.isCancelled, generation == semanticGeneration else { return }
                semanticSearchPending = true
            } catch { return }
        }
    }

    func selectAsset(_ item: JSONValue) {
        selectedAsset = item
        selectedAssetID = item["id"]?.stringValue ?? item["assetId"]?.stringValue
        sourceIn = item["sourceIn"]?.numberValue ?? item["in"]?.numberValue
        sourceOut = nil
        if let id = selectedAssetID {
            sourceStates[id] = "loading"
            Task {
                do {
                    let asset = try await service.call("library.asset", params: .object(["id": .string(id)]))
                    guard selectedAssetID == id else { return }
                    selectedAsset = asset
                    sourceIn = asset["sourceIn"]?.numberValue ?? asset["in"]?.numberValue ?? sourceIn
                    let kind = (selectedAsset?["type"]?.stringValue ?? selectedAsset?["projection"]?.stringValue ?? "").lowercased()
                    if kind.contains("360") || selectedAsset?["is360"]?.boolValue == true {
                        sourceStates[id] = "pending"
                        _ = try await service.call("source.prepare", params: .object(["id": .string(id)]))
                    } else if let url = Self.registeredURL(from: selectedAsset) {
                        timelineSourceURLs[id] = url
                        sourceStates[id] = "ready"
                    } else {
                        sourceStates[id] = "pending"
                        let result = try await service.call("source.prepare", params: .object(["id": .string(id)]))
                        if let url = Self.registeredURL(from: result) {
                            timelineSourceURLs[id] = url
                            sourceStates[id] = "ready"
                        }
                    }
                }
                catch { errorMessage = error.localizedDescription }
            }
        }
    }

    func requestTimelineMedia(_ assetIDs: [String]) {
        for id in Set(assetIDs) where timelineSourceURLs[id] == nil && sourceStates[id] == nil {
            sourceStates[id] = "loading"
            Task {
                do {
                    let asset = try await service.call("library.asset", params: .object(["id": .string(id)]))
                    let type = (asset["type"]?.stringValue ?? asset["projection"]?.stringValue ?? "").lowercased()
                    if type.contains("360") || asset["is360"]?.boolValue == true {
                        sourceStates[id] = "pending"
                        let result = try await service.call("source.prepare", params: .object(["id": .string(id)]))
                        if let url = Self.registeredURL(from: result) {
                            timelineSourceURLs[id] = url
                            sourceStates[id] = "ready"
                        }
                    } else if let url = Self.registeredURL(from: asset) {
                        timelineSourceURLs[id] = url
                        sourceStates[id] = "ready"
                    } else {
                        sourceStates[id] = "pending"
                        let result = try await service.call("source.prepare", params: .object(["id": .string(id)]))
                        if let url = Self.registeredURL(from: result) {
                            timelineSourceURLs[id] = url
                            sourceStates[id] = "ready"
                        }
                    }
                } catch {
                    sourceStates[id] = "error"
                }
            }
        }
    }

    func setSourceIn(_ seconds: Double) { sourceIn = seconds }
    func setSourceOut(_ seconds: Double) { sourceOut = seconds }

    func insertSourceSelection() {
        guard let selectedAssetID,
              let sourceIn,
              let sourceOut,
              sourceOut > sourceIn else {
            errorMessage = "Mark a source In and Out range before inserting."
            return
        }
        _ = insertDroppedItem(selectedAssetID, atFrame: playheadFrame, trackID: "V1",
                              sourceIn: sourceIn, sourceOut: sourceOut)
    }

    func preparePreview(mode: String) {
        currentPreviewMode = mode
        isPreparingPreview = true
        Task {
            do {
                _ = try await service.call("preview.prepare", params: .object(["mode": .string(mode)]))
                notice = mode == "exact" ? "Final-quality preview requested" : "Fast preview requested"
            } catch { errorMessage = error.localizedDescription }
            isPreparingPreview = false
        }
    }

    func requestExactPreview(reason: String) {
        guard currentPreviewMode != "exact" else {
            errorMessage = "Final-quality preview also failed: \(reason)"
            return
        }
        notice = "Fast preview issue: \(reason). Preparing final quality."
        preparePreview(mode: "exact")
    }

    func stopServiceForCapture() {
        service.stopSynchronously(grace: 1)
    }

    func respondToPermission(granted: Bool) {
        guard let request = activePermission, let id = request["id"]?.stringValue else {
            errorMessage = "There is no pending permission request to answer."
            return
        }
        Task {
            do {
                let result = try await service.call("permission.respond", params: .object([
                    "id": .string(id),
                    "granted": .bool(granted)
                ]))
                pendingPermissions.removeAll { $0["id"]?.stringValue == id }
                if granted {
                    if !currentGrants.contains(where: { $0["id"]?.stringValue == id }) {
                        currentGrants.append(result)
                    }
                    notice = "Permission granted for \(request["kind"]?.stringValue ?? "this action")"
                } else {
                    notice = "Permission denied for \(request["kind"]?.stringValue ?? "this action")"
                }
                isPermissionPromptPresented = !pendingPermissions.isEmpty
            } catch {
                errorMessage = "Could not record the permission decision: \(error.localizedDescription)"
            }
        }
    }

    private func persistState() async {
        _ = try? await service.call("state.save", params: .object([
            "selection": .array(selectedIDs.sorted().map(JSONValue.string)),
            "playhead": .number(Double(playheadFrame))
        ]))
    }

    private func receive(_ event: JSONValue) {
        let name = event["event"]?.stringValue ?? ""
        let payload = event["payload"] ?? .object([:])
        if name == "search.results" {
            let generation = Int(payload["generation"]?.numberValue ?? -1)
            guard generation == semanticGeneration else { return }
            let eventLibraryRevision = payload["libraryRevision"]?.stringValue
                ?? event["revisions"]?["library"]?.stringValue
            if let eventLibraryRevision, let current = snapshot?.revisions.library,
               eventLibraryRevision != current { return }
            if let eventFilterHash = payload["filterHash"]?.stringValue,
               let expectedSearchFilterHash, eventFilterHash != expectedSearchFilterHash { return }
            switch payload["type"]?.stringValue {
            case "regex":
                searchHits = Self.hits(from: payload, related: false, keys: ["matches", "regexRows"])
            case "semantic":
                let regex = Self.hits(from: payload, related: false, keys: ["regexRows"])
                if searchHits.isEmpty { searchHits = regex }
                let related = Self.hits(from: payload, related: true, keys: ["relatedRows"])
                let enriched = Self.hits(from: payload, related: true, keys: ["enrichedRows"])
                searchHits = Self.merge(Self.merge(searchHits, related: related), related: enriched)
                semanticSearchPending = false
            case "semanticError":
                semanticSearchPending = false
                notice = Self.errorMessage(from: payload, fallback: "Related search unavailable; local matches remain.")
            default:
                break
            }
        } else if name.hasPrefix("search.") {
            if name.contains("error") {
                semanticSearchPending = false
                notice = Self.errorMessage(from: payload, fallback: "Search failed.")
            }
        } else if name.hasPrefix("preview.") {
            if name.contains("pending") {
                isPreparingPreview = true
            } else if name.contains("ready") {
                let manifest = payload["manifest"] ?? payload["preview"] ?? payload
                previewManifest = manifest
                displayedPreviewRevision = manifest["revision"]?.stringValue
                    ?? manifest["revisions"]?["document"]?.stringValue
                    ?? manifest["documentRevision"]?.stringValue
                    ?? displayedPreviewRevision
                isPreparingPreview = false
                previewFailureMessage = nil
                NotificationCenter.default.post(name: .reelsEditorPreviewReady, object: manifest)
                Task { await refreshSnapshot() }
            } else if name.contains("error") {
                isPreparingPreview = false
                errorMessage = payload["message"]?.stringValue ?? "Preview preparation failed."
                previewFailureMessage = errorMessage
                NotificationCenter.default.post(name: .reelsEditorPreviewFailed, object: errorMessage)
            }
        } else if name.hasPrefix("document.") {
            if name.contains("changed") || name.contains("applied") {
                if let ids = payload["affectedIDs"]?.arrayValue {
                    recentlyChangedIDs = Set(ids.compactMap(\.stringValue))
                }
                Task { await refreshSnapshot() }
            }
        } else if name == "source.ready" {
            let assetID = payload["assetId"]?.stringValue ?? payload["id"]?.stringValue
            let url = Self.registeredURL(from: payload["asset"] ?? payload)
            if let assetID {
                if let url { timelineSourceURLs[assetID] = url; sourceStates[assetID] = "ready" }
                else { sourceStates[assetID] = "error" }
                if assetID == selectedAssetID, let asset = payload["asset"] { selectedAsset = asset }
            }
        } else if name == "source.error" || name == "source.unavailable" {
            let assetID = payload["assetId"]?.stringValue ?? payload["id"]?.stringValue
            if let assetID { sourceStates[assetID] = "error" }
            notice = payload["message"]?.stringValue ?? payload["reason"]?.stringValue ?? "Source proxy is unavailable"
        } else if name == "permission.requested" {
            if !pendingPermissions.contains(where: { $0["id"]?.stringValue == payload["id"]?.stringValue }) {
                pendingPermissions.append(payload)
            }
            isPermissionPromptPresented = true
            notice = "Permission requested: \(payload["kind"]?.stringValue ?? "review required")"
        } else if name == "export.ready" {
            notice = "Render finished"
            Task { await refreshPlans() }
        } else if name == "music.changed" {
            Task { await music.reload() }
        } else if name == "music.searchResults" {
            music.handleSearchResults(payload)
        } else if name == "plans.changed" {
            Task { await refreshPlans() }
        } else if name == "library.changed" {
            loadLibrary()
            Task { await refreshPlans() }
            importer.refreshInbox()
        } else if name.hasPrefix("job.") {
            if payload["kind"]?.stringValue == "ingest" {
                importer.handleJob(payload)
            } else if payload["kind"]?.stringValue == "schedule" {
                schedule.handleJob(payload)
            } else if payload["kind"]?.stringValue == "music" {
                music.handleJob(payload)
            } else {
                notice = EventNotice.jobMessage(payload)
            }
        }
    }

    nonisolated private static func items(from value: JSONValue, keys: [String]) -> [JSONValue] {
        if let values = value.arrayValue { return values }
        var emptyArray: [JSONValue]?
        for key in keys {
            if let values = value[key]?.arrayValue {
                if !values.isEmpty { return values }
                emptyArray = values
            }
        }
        return emptyArray ?? []
    }

    func syncPermissions(from value: JSONValue) {
        let permissionObject = value["permissions"] ?? value
        if let records = permissionObject.arrayValue {
            let scoped = records.filter {
                $0["documentId"]?.stringValue == nil || snapshot == nil
                    || $0["documentId"]?.stringValue == snapshot?.documentId
            }
            pendingPermissions = scoped.filter { $0["state"]?.stringValue == "pending" }
            currentGrants = scoped.filter { $0["state"]?.stringValue == "granted" }
            isPermissionPromptPresented = !pendingPermissions.isEmpty
            return
        }
        let pending = permissionObject["pendingPermissions"]?.arrayValue
            ?? permissionObject["pending"]?.arrayValue
        if let pending {
            pendingPermissions = pending
            isPermissionPromptPresented = !pending.isEmpty
        }
        let grants = permissionObject["currentGrants"] ?? permissionObject["grants"]
        if let array = grants?.arrayValue {
            currentGrants = array
        } else if let object = grants?.objectValue {
            currentGrants = object.keys.sorted().map { key in
                .object(["kind": .string(key), "scope": object[key] ?? .null])
            }
        }
    }

    nonisolated static func hits(from value: JSONValue, related: Bool,
                                 keys: [String] = ["results", "matches", "items", "hits"]) -> [SearchHit] {
        let values = items(from: value, keys: keys)
        return values.compactMap { item in
            let passage = item["passage"]
            let assetID = item["assetId"]?.stringValue ?? item["asset"]?.stringValue
                ?? passage?["assetId"]?.stringValue ?? passage?["asset"]?.stringValue
            guard let assetID else { return nil }
            let range = item["range"] ?? passage?["range"]
            let start = item["sourceIn"]?.numberValue ?? item["in"]?.numberValue
                ?? passage?["sourceIn"]?.numberValue ?? passage?["in"]?.numberValue
                ?? range?["start"]?.numberValue ?? range?["in"]?.numberValue ?? 0
            let end = item["sourceOut"]?.numberValue ?? item["out"]?.numberValue
                ?? passage?["sourceOut"]?.numberValue ?? passage?["out"]?.numberValue
                ?? range?["end"]?.numberValue ?? range?["out"]?.numberValue ?? start + 1
            let evidence = item["evidence"]?.stringValue ?? item["text"]?.stringValue
                ?? passage?["evidence"]?.stringValue ?? passage?["text"]?.stringValue
                ?? item["reason"]?.stringValue ?? ""
            let relatedObject = item["related"]?.objectValue
            let sourceRanges = (item["sourceRanges"]?.arrayValue ?? [])
                .compactMap(Self.searchRangeEvidence)
            return SearchHit(
                id: item["id"]?.stringValue ?? "\(assetID)-\(start)",
                title: item["title"]?.stringValue ?? item["name"]?.stringValue
                    ?? item["filename"]?.stringValue ?? passage?["title"]?.stringValue ?? assetID,
                evidence: evidence,
                assetId: assetID,
                sourceIn: start,
                sourceOut: max(end, start + 0.1),
                related: related || item["related"]?.boolValue == true,
                raw: item,
                relatedReason: relatedObject?["reason"]?.stringValue
                    ?? item["relatedReason"]?.stringValue
                    ?? (related ? item["reason"]?.stringValue : nil),
                sourceRanges: sourceRanges
            )
        }
    }

    nonisolated static func merge(_ existing: [SearchHit], related: [SearchHit]) -> [SearchHit] {
        var output = existing
        for hit in related {
            let index = output.firstIndex(where: {
                $0.assetId == hit.assetId && abs($0.sourceIn - hit.sourceIn) < 0.25
                    && abs($0.sourceOut - hit.sourceOut) < 0.5
            }) ?? output.firstIndex(where: { $0.assetId == hit.assetId })
            if let index {
                let old = output[index]
                var ranges = old.sourceRanges
                for range in hit.sourceRanges where !ranges.contains(where: {
                    abs($0.sourceIn - range.sourceIn) < 0.001 && abs($0.sourceOut - range.sourceOut) < 0.001
                }) {
                    ranges.append(range)
                }
                if ranges.isEmpty, hit.related {
                    ranges.append(SearchRangeEvidence(
                        sourceIn: hit.sourceIn, sourceOut: hit.sourceOut, evidence: hit.evidence,
                        reason: hit.relatedReason ?? "", highlights: hit.raw["highlights"]?.arrayValue ?? []
                    ))
                }
                var enriched = old
                enriched.relatedReason = hit.relatedReason ?? old.relatedReason
                enriched.sourceRanges = ranges
                let semantic = hit.raw["related"] ?? hit.raw
                enriched.raw = old.raw.setting(semantic, at: ["semantic"])
                output[index] = enriched
            } else { output.append(hit) }
        }
        return output
    }

    nonisolated private static func searchRangeEvidence(_ value: JSONValue) -> SearchRangeEvidence? {
        let range = value["range"]
        let start = value["sourceIn"]?.numberValue ?? value["in"]?.numberValue
            ?? range?["start"]?.numberValue ?? range?["in"]?.numberValue
        let end = value["sourceOut"]?.numberValue ?? value["out"]?.numberValue
            ?? range?["end"]?.numberValue ?? range?["out"]?.numberValue
        guard let start, let end, end > start else { return nil }
        return SearchRangeEvidence(
            sourceIn: start, sourceOut: end,
            evidence: value["evidence"]?.stringValue ?? value["text"]?.stringValue ?? "",
            reason: value["reason"]?.stringValue ?? "",
            highlights: value["highlights"]?.arrayValue ?? []
        )
    }

    nonisolated private static func errorMessage(from payload: JSONValue, fallback: String) -> String {
        let nested = payload["error"]
        let message = nested?["message"]?.stringValue
            ?? payload["message"]?.stringValue
            ?? nested?.stringValue
            ?? fallback
        let code = nested?["code"]?.stringValue ?? payload["code"]?.stringValue
        guard let code, !code.isEmpty, !message.contains(code) else { return message }
        return "\(message) (\(code))"
    }

    private static func registeredURL(from value: JSONValue?) -> URL? {
        guard let value else { return nil }
        let raw = value["sourceURL"]?.stringValue
            ?? value["proxyURL"]?.stringValue
            ?? value["preparedURL"]?.stringValue
            ?? value["previewURL"]?.stringValue
            ?? value["url"]?.stringValue
            ?? value["path"]?.stringValue
            ?? value["mediaPath"]?.stringValue
        guard let raw else { return nil }
        if raw.hasPrefix("/") { return URL(fileURLWithPath: raw) }
        guard let url = URL(string: raw), ["http", "https", "file"].contains(url.scheme?.lowercased() ?? "") else { return nil }
        return url
    }

    private static func argument(_ name: String, in arguments: [String]) -> String? {
        guard let index = arguments.firstIndex(of: name), arguments.indices.contains(index + 1) else { return nil }
        return arguments[index + 1]
    }

    private static func sameOrigin(_ lhs: URL, _ rhs: URL) -> Bool {
        guard let left = URLComponents(url: lhs, resolvingAgainstBaseURL: false),
              let right = URLComponents(url: rhs, resolvingAgainstBaseURL: false) else { return false }
        return left.scheme?.lowercased() == right.scheme?.lowercased()
            && left.host?.lowercased() == right.host?.lowercased()
            && left.port == right.port
    }
}

extension Notification.Name {
    static let reelsEditorDocumentReady = Notification.Name("ReelsEditor.DocumentReady")
    static let reelsEditorPreviewReady = Notification.Name("ReelsEditor.PreviewReady")
    static let reelsEditorProgramReady = Notification.Name("ReelsEditor.ProgramReady")
    static let reelsEditorPreviewFailed = Notification.Name("ReelsEditor.PreviewFailed")
    static let reelsEditorPreviewTimedOut = Notification.Name("ReelsEditor.PreviewTimedOut")
    static let reelsEditorCaptureQuit = Notification.Name("ReelsEditor.CaptureQuit")
}

/// Runs the login-shell PATH lookup once per launch.
actor LoginPathBox {
    private var task: Task<String?, Never>?
    func value() async -> String? {
        if let task { return await task.value }
        let created = Task { await AppEnvironment.loginShellPath() }
        task = created
        return await created.value
    }
}
