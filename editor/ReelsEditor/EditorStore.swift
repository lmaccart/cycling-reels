import AppKit
import AVFoundation
import Foundation
import Observation
import UniformTypeIdentifiers

@MainActor
@Observable
final class EditorStore {
    let service = EditorService()
    var serviceState: EditorService.ServiceState = .stopped
    var snapshot: EditorSnapshot?
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
    var sourceIn: Double?
    var sourceOut: Double?
    var searchText = ""
    var semanticSearchPending = false
    var chatEntries: [ChatEntry] = []
    var chatDraft = ""
    var isChatWorking = false
    var notice: String?
    var errorMessage: String?
    var workspaceInfo: JSONValue = .object([:])
    var inspectorSchema: JSONValue = .object([:])
    var protectedRoots: [String] = []
    var previewManifest: JSONValue?
    var currentPreviewMode = "interactive"
    var playbackRate = 1.0
    var isPreparingPreview = false
    var displayedPreviewRevision: String?
    var previewFailureMessage: String?
    var exportJobID: String?
    var pendingExportReceipt: JSONValue?
    var isSavingExport = false
    var pendingPermissions: [JSONValue] = []
    var currentGrants: [JSONValue] = []
    var isPermissionPromptPresented = false
    var activeTab = "source"
    var filters: [String: JSONValue] = [:]

    private var searchTask: Task<Void, Never>?
    private var semanticGeneration = 0
    private var libraryLoadGeneration = 0
    private var expectedSearchFilterHash: String?
    private var chatOutputStartIndex: Int?
    private var repositoryRoot: URL?
    private var workspaceRoot: URL?
    private var launchPlanPath: String?
    private var capturePreviewAttempted = false
    var configurationError: String?
    var hasLaunchConfiguration: Bool { repositoryRoot != nil && workspaceRoot != nil }

    init() {
        let env = ProcessInfo.processInfo.environment
        let args = ProcessInfo.processInfo.arguments
        let argumentRepo = Self.argument("--repo", in: args) ?? env["REELS_EDITOR_REPO_ROOT"]
        let argumentWorkspace = Self.argument("--workspace", in: args) ?? env["REELS_EDITOR_WORKSPACE"]
        launchPlanPath = Self.argument("--plan", in: args)
        if let argumentRepo, let argumentWorkspace {
            repositoryRoot = URL(fileURLWithPath: argumentRepo).standardizedFileURL.resolvingSymlinksInPath()
            workspaceRoot = URL(fileURLWithPath: argumentWorkspace).standardizedFileURL.resolvingSymlinksInPath()
        } else if argumentRepo != nil || argumentWorkspace != nil {
            configurationError = "Pass both --repo <isolated-repo> and --workspace <workspace>; partial paths are not used."
        } else if let savedRepo = UserDefaults.standard.string(forKey: "reelsEditor.repositoryRoot"),
                  let savedWorkspace = UserDefaults.standard.string(forKey: "reelsEditor.workspaceRoot") {
            repositoryRoot = URL(fileURLWithPath: savedRepo).standardizedFileURL.resolvingSymlinksInPath()
            workspaceRoot = URL(fileURLWithPath: savedWorkspace).standardizedFileURL.resolvingSymlinksInPath()
        }
        service.onState = { [weak self] state in self?.serviceState = state }
        service.onEvent = { [weak self] event in self?.receive(event) }
    }

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

    func connect() {
        guard let repositoryRoot, let workspaceRoot else { return }
        do {
            protectedRoots = try Self.validate(root: repositoryRoot, workspace: workspaceRoot)
        } catch {
            configurationError = error.localizedDescription
            serviceState = .failed(error.localizedDescription)
            return
        }
        do {
            try service.start(repositoryRoot: repositoryRoot, workspace: workspaceRoot)
            Task {
                do {
                    workspaceInfo = try await service.call("workspace.info")
                    syncPermissions(from: workspaceInfo)
                    if let productionRoot = workspaceInfo["productionRoot"]?.stringValue
                        ?? workspaceInfo["production_root"]?.stringValue {
                        let resolved = URL(fileURLWithPath: productionRoot)
                            .standardizedFileURL.resolvingSymlinksInPath().path
                        protectedRoots = Array(Set(protectedRoots + [resolved])).sorted()
                    }
                    let result = try await service.call("plans.list")
                    plans = Self.items(from: result, keys: ["plans", "items"])
                    let history = try? await service.call("chat.history")
                    chatEntries = Self.chatHistory(from: history ?? .null)
                    loadLibrary()
                    if let recent = launchPlanPath
                        ?? workspaceInfo["plan"]?.stringValue
                        ?? workspaceInfo["state"]?["plan"]?.stringValue,
                       !recent.isEmpty {
                        await openPlan(recent)
                    }
                } catch {
                    self.errorMessage = error.localizedDescription
                }
            }
        } catch {
            serviceState = .failed(error.localizedDescription)
            errorMessage = "Could not launch the local editing service: \(error.localizedDescription)"
        }
    }

    func configure(repository: URL, workspace: URL) {
        repositoryRoot = repository.standardizedFileURL.resolvingSymlinksInPath()
        workspaceRoot = workspace.standardizedFileURL.resolvingSymlinksInPath()
        do {
            protectedRoots = try Self.validate(root: repositoryRoot!, workspace: workspaceRoot!)
            UserDefaults.standard.set(repositoryRoot!.path, forKey: "reelsEditor.repositoryRoot")
            UserDefaults.standard.set(workspaceRoot!.path, forKey: "reelsEditor.workspaceRoot")
            configurationError = nil
            connect()
        } catch {
            configurationError = error.localizedDescription
        }
    }

    func openPlan(_ path: String) async {
        do {
            previewManifest = nil
            displayedPreviewRevision = nil
            _ = try await service.call("document.open", params: .object(["path": .string(path)]))
            await refreshSnapshot()
            notice = "Project opened"
        } catch { errorMessage = error.localizedDescription }
    }

    func refreshSnapshot() async {
        do {
            let value = try await service.call("document.snapshot")
            syncPermissions(from: value)
            if let next = EditorSnapshot.decode(value) {
                snapshot = next
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

    func setPlayhead(_ frame: Int) {
        playheadFrame = max(0, min(snapshot?.durationFrames ?? frame, frame))
        seekGeneration += 1
        Task {
            _ = try? await service.call("selection.set", params: .object([
                "ids": .array(selectedIDs.sorted().map(JSONValue.string)),
                "playhead": .number(Double(playheadFrame))
            ]))
            await persistState()
        }
    }

    func updatePlayheadFromPreview(_ frame: Int, revision: String?) {
        guard let snapshot,
              revision == nil || revision == snapshot.revisions.document || revision == displayedPreviewRevision else { return }
        playheadFrame = max(0, min(snapshot.durationFrames, frame))
    }

    func hit(for id: String) -> SearchHit? { searchHits.first { $0.id == id } }

    func loadInspectorSchema(for id: String) async {
        if let schema = try? await service.call("inspector.schema", params: .object(["id": .string(id)])) {
            inspectorSchema = schema
        }
    }

    func apply(_ operations: [JSONValue], reason: String, lease: String? = nil) async {
        guard let snapshot else { return }
        var batch: [String: JSONValue] = [
            "expected": .object([
                "document": .string(snapshot.revisions.document),
                "library": .string(snapshot.revisions.library),
                "renderer": .string(snapshot.revisions.renderer)
            ]),
            "reason": .string(reason),
            "origin": .string("manual"),
            "operations": .array(operations)
        ]
        if let lease { batch["lease"] = .string(lease) }
        do {
            let value = try await service.call("document.apply", params: .object(["batch": .object(batch)]))
            if let next = EditorSnapshot.decode(value) { self.snapshot = next }
            else { await refreshSnapshot() }
            notice = reason
        } catch {
            errorMessage = error.localizedDescription
            await refreshSnapshot()
        }
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
            if let next = EditorSnapshot.decode(value) { snapshot = next }
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
        do {
            let result = try await service.call("lease.acquire", params: .object(["owner": .string(owner)]))
            return result["token"]?.stringValue
        } catch {
            errorMessage = error.localizedDescription
            return nil
        }
    }

    func reorder(ids: [String], beforeID: String?, lease: String?) {
        Task {
            guard let lease else { return }
            var op: [String: JSONValue] = [
                "op": .string("reorder_segments"),
                "ids": .array(ids.map(JSONValue.string))
            ]
            if let beforeID { op["beforeId"] = .string(beforeID) }
            await apply([.object(op)], reason: "Reorder clips", lease: lease)
            _ = try? await service.call("lease.release", params: .object(["token": .string(lease)]))
        }
    }

    func moveTimelineItem(_ node: EditorNode, toFrame: Int, beforeID: String?, lease: String?) {
        Task {
            guard let lease else { return }
            if node.isReadOnly || (node.generated == true && node.kind.lowercased() != "generated_sfx") {
                notice = "Generated caption timing is read-only."
            } else if node.kind.lowercased() == "generated_sfx" {
                await apply([.object([
                    "op": .string("move_overlay"),
                    "id": .string(node.id),
                    "at": .number(Double(max(0, toFrame)) / fps)
                ])], reason: "Move generated sound cue", lease: lease)
            } else if node.kind.lowercased() == "cutaway" || node.parentId != nil || Self.isOverlay(node) {
                let parentStart = snapshot?.nodes.first(where: { $0.id == node.parentId })?.startFrame ?? 0
                let localFrame = max(0, toFrame - parentStart)
                await apply([.object([
                    "op": .string("move_overlay"),
                    "id": .string(node.id),
                    "at": .number(Double(localFrame) / fps)
                ])], reason: "Move overlay", lease: lease)
            } else if node.kind.lowercased().contains("segment")
                        || node.lane.lowercased().contains("video")
                        || node.lane.lowercased().contains("primary") {
                var operation: [String: JSONValue] = [
                    "op": .string("reorder_segments"),
                    "ids": .array([.string(node.id)])
                ]
                if let beforeID { operation["beforeId"] = .string(beforeID) }
                await apply([.object(operation)], reason: "Reorder clips", lease: lease)
            }
            _ = try? await service.call("lease.release", params: .object(["token": .string(lease)]))
        }
    }

    func trim(node: EditorNode, sourceIn: Double, sourceOut: Double, lease: String?) {
        Task {
            guard let lease else { return }
            if node.isReadOnly || (node.generated == true && node.kind.lowercased() != "generated_sfx") {
                notice = "Generated caption timing is read-only."
            } else if node.kind.lowercased() == "cutaway", node.isPhoto {
                let parentStart = snapshot?.nodes.first(where: { $0.id == node.parentId })?.startFrame ?? 0
                await apply([.object([
                    "op": .string("resize_overlay"),
                    "id": .string(node.id),
                    "at": .number(max(0, sourceIn - Double(parentStart) / fps)),
                    "for": .number(min(12, max(0.8, sourceOut)))
                ])], reason: "Resize photo cutaway", lease: lease)
            } else if node.kind.lowercased() == "cutaway", sourceOut > sourceIn {
                await apply([
                    BrollOperationBuilder.trim(
                        cutawayID: node.id, sourceIn: sourceIn, sourceOut: sourceOut,
                        minimumDuration: 1.0 / fps
                    )
                ], reason: "Trim B-roll source", lease: lease)
            } else if node.kind.lowercased() == "segment", node.isPhoto {
                await apply([.object([
                    "op": .string("set_field"),
                    "id": .string(node.id),
                    "path": .string("/duration"),
                    "value": .number(min(12, max(0.8, sourceOut)))
                ])], reason: "Adjust photo duration", lease: lease)
            } else if node.kind.lowercased() == "generated_sfx" {
                notice = "Generated sound cues can be moved but not edge-trimmed."
            } else if Self.isOverlay(node) {
                await apply([.object([
                    "op": .string("resize_overlay"),
                    "id": .string(node.id),
                    "at": .number(max(0, sourceIn)),
                    "for": .number(max(1 / fps, sourceOut))
                ])], reason: "Resize overlay", lease: lease)
            } else if sourceOut > sourceIn {
                await apply([.object([
                    "op": .string("trim_segment"),
                    "id": .string(node.id),
                    "sourceIn": .number(max(0, sourceIn)),
                    "sourceOut": .number(max(sourceIn + 1.0 / fps, sourceOut))
                ])], reason: "Trim clip", lease: lease)
            }
            _ = try? await service.call("lease.release", params: .object(["token": .string(lease)]))
        }
    }

    private static func isOverlay(_ node: EditorNode) -> Bool {
        let kind = "\(node.kind) \(node.lane)".lowercased()
        return node.parentId != nil || kind.contains("overlay")
            || kind.contains("caption") || kind.contains("text")
            || kind.contains("cutaway") || kind.contains("b-roll") || kind.contains("broll")
    }

    func insertSearchHit(_ hit: SearchHit, beforeID: String? = nil) {
        var value: [String: JSONValue] = [
            "asset": .string(hit.assetId),
            "in": .number(hit.sourceIn),
            "out": .number(hit.sourceOut)
        ]
        if let moment = hit.raw["moment"]?.stringValue { value["moment"] = .string(moment) }
        Task {
            var op: [String: JSONValue] = ["op": .string("insert_segment"), "value": .object(value)]
            if let beforeID { op["beforeId"] = .string(beforeID) }
            await apply([.object(op)], reason: "Insert source range")
        }
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

    func insertDroppedItem(_ id: String) {
        if let hit = hit(for: id) {
            insertSearchHit(hit, beforeID: selectedNode?.id)
            return
        }
        guard library.contains(where: { $0["id"]?.stringValue == id }) || selectedAssetID == id else { return }
        Task {
            do {
                let asset = try await service.call("library.asset", params: .object(["id": .string(id)]))
                let duration = asset["duration"]?.numberValue ?? asset["seconds"]?.numberValue ?? asset["durationSeconds"]?.numberValue ?? 10
                let hit = SearchHit(id: id, title: asset["name"]?.stringValue ?? id, evidence: "Library source",
                                   assetId: id, sourceIn: 0, sourceOut: max(0.1, duration),
                                   related: false, raw: asset)
                insertSearchHit(hit, beforeID: selectedNode?.id)
            } catch { errorMessage = error.localizedDescription }
        }
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
        let hit = SearchHit(
            id: selectedAssetID, title: selectedAssetID, evidence: "Source In / Out",
            assetId: selectedAssetID, sourceIn: sourceIn, sourceOut: sourceOut,
            related: false, raw: selectedAsset ?? .object([:])
        )
        insertSearchHit(hit, beforeID: selectedNode?.id)
    }

    func preparePreview(mode: String) {
        currentPreviewMode = mode
        isPreparingPreview = true
        Task {
            do {
                _ = try await service.call("preview.prepare", params: .object(["mode": .string(mode)]))
                notice = "\(mode.capitalized) preview requested"
            } catch { errorMessage = error.localizedDescription }
            isPreparingPreview = false
        }
    }

    func requestExactPreview(reason: String) {
        guard currentPreviewMode != "exact" else {
            errorMessage = "Exact preview also failed: \(reason)"
            return
        }
        notice = "Interactive preview issue: \(reason). Preparing Exact."
        preparePreview(mode: "exact")
    }

    func sendChat() {
        let text = chatDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, !isChatWorking else { return }
        chatEntries.append(ChatEntry(role: "you", text: text))
        chatOutputStartIndex = chatEntries.count
        chatDraft = ""
        isChatWorking = true
        Task {
            do {
                _ = try await service.call("chat.send", params: .object(["text": .string(text)]))
            } catch {
                chatEntries.append(ChatEntry(role: "service", text: error.localizedDescription, state: "error"))
                isChatWorking = false
            }
        }
    }

    func stopChat() {
        Task {
            _ = try? await service.call("chat.stop")
            isChatWorking = false
        }
    }

    func stopServiceForCapture() {
        service.stop()
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

    func export() {
        Task {
            do {
                exportJobID = nil
                pendingExportReceipt = nil
                let result = try await service.call("export.start")
                exportJobID = result["id"]?.stringValue
                notice = "Export started for the current revision"
                if let receipt = pendingExportReceipt {
                    pendingExportReceipt = nil
                    presentExportSave(for: receipt)
                }
            } catch { errorMessage = error.localizedDescription }
        }
    }

    private func presentExportSave(for receipt: JSONValue) {
        guard let exportJobID else {
            let path = receipt["reelPath"]?.stringValue
                ?? receipt["previewVideoPath"]?.stringValue
            if let path { saveExportCopy(path) }
            else { notice = "Export ready in the private editor workspace" }
            return
        }
        let panel = NSSavePanel()
        panel.allowedContentTypes = [.mpeg4Movie, .quickTimeMovie]
        panel.canCreateDirectories = true
        panel.nameFieldStringValue = URL(fileURLWithPath: receipt["reelPath"]?.stringValue ?? "reel.mp4").lastPathComponent
        panel.message = "Save an export artifact outside the production checkout."
        let selector = NSPopUpButton(frame: NSRect(x: 0, y: 0, width: 180, height: 24), pullsDown: false)
        selector.addItems(withTitles: ["Reel", "Review copy"])
        panel.accessoryView = selector
        panel.begin { [weak self] response in
            guard response == .OK, let destination = panel.url, let self else { return }
            let target = destination.standardizedFileURL.resolvingSymlinksInPath()
            guard self.isAllowedExportDestination(target) else {
                self.errorMessage = "That location is inside a protected project or production checkout. Choose another destination."
                return
            }
            let artifact = selector.indexOfSelectedItem == 1 ? "review" : "reel"
            let overwrite = FileManager.default.fileExists(atPath: target.path)
            self.isSavingExport = true
            Task {
                do {
                    let result = try await self.service.call("export.save", params: .object([
                        "id": .string(exportJobID),
                        "destination": .string(target.path),
                        "artifact": .string(artifact),
                        "overwrite": .bool(overwrite)
                    ]))
                    self.notice = "Saved \(artifact == "review" ? "review copy" : "reel") to \(target.lastPathComponent)"
                    self.errorMessage = nil
                    _ = result
                } catch {
                    self.errorMessage = "Could not save export: \(error.localizedDescription)"
                }
                self.isSavingExport = false
            }
        }
    }

    func saveExportCopy(_ sourcePath: String) {
        guard let workspaceRoot else { return }
        let source = URL(fileURLWithPath: sourcePath).standardizedFileURL.resolvingSymlinksInPath()
        guard source.path.hasPrefix(workspaceRoot.path + "/"),
              FileManager.default.fileExists(atPath: source.path) else {
            errorMessage = "The export is not a file inside the private editor workspace."
            return
        }
        let panel = NSSavePanel()
        panel.nameFieldStringValue = source.lastPathComponent
        panel.allowedContentTypes = [.mpeg4Movie, .quickTimeMovie]
        panel.canCreateDirectories = true
        panel.message = "Save a copy outside the production checkout. The private editor export remains unchanged."
        panel.begin { [weak self] response in
            guard response == .OK, let destination = panel.url, let self else { return }
            let target = destination.standardizedFileURL.resolvingSymlinksInPath()
            guard self.isAllowedExportDestination(target) else {
                self.errorMessage = "That location is inside a protected project or production checkout. Choose another destination."
                return
            }
            do {
                if FileManager.default.fileExists(atPath: target.path) { try FileManager.default.removeItem(at: target) }
                try FileManager.default.copyItem(at: source, to: target)
                self.notice = "Saved a copy to \(target.lastPathComponent)"
            } catch {
                self.errorMessage = "Could not save export copy: \(error.localizedDescription)"
            }
        }
    }

    private func isAllowedExportDestination(_ target: URL) -> Bool {
        !protectedRoots.contains(where: {
            target.path == $0 || target.path.hasPrefix($0 + "/")
        })
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
        if name == "chat.record" {
            Self.appendChatRecord(payload, to: &chatEntries)
        } else if name == "chat.delta" {
            let role = Self.chatRole(payload["role"]?.stringValue ?? "assistant")
            let text = payload["text"]?.stringValue ?? ""
            guard role == "Claude", !text.isEmpty else { return }
            if let index = streamingAssistantIndex(in: chatEntries) {
                chatEntries[index].text += text
            } else {
                chatEntries.append(ChatEntry(role: "Claude", text: text, state: "streaming"))
            }
        } else if name == "chat.done" {
            isChatWorking = false
            let resultText = payload["result"]?["text"]?.stringValue
                ?? payload["result"]?.stringValue
                ?? payload["text"]?.stringValue
                ?? ""
            let responseStart = min(chatOutputStartIndex ?? chatEntries.count, chatEntries.count)
            let hasAssistantResponse = chatEntries.dropFirst(responseStart)
                .contains { $0.role == "Claude" && !$0.text.isEmpty }
            if !resultText.isEmpty && !hasAssistantResponse {
                chatEntries.append(ChatEntry(role: "Claude", text: resultText))
            }
            if let index = streamingAssistantIndex(in: chatEntries) {
                chatEntries[index].state = "complete"
            }
            chatOutputStartIndex = nil
        } else if name == "chat.error" || name == "chat.failed" {
            isChatWorking = false
            chatEntries.append(ChatEntry(
                role: "service",
                text: Self.errorMessage(from: payload, fallback: "Chat request failed."),
                state: "error"
            ))
            chatOutputStartIndex = nil
        } else if name == "search.results" {
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
            exportJobID = payload["jobId"]?.stringValue ?? payload["id"]?.stringValue ?? exportJobID
            pendingExportReceipt = payload
            if exportJobID != nil {
                pendingExportReceipt = nil
                presentExportSave(for: payload)
            } else {
                notice = "Export completed; preparing save options"
            }
        } else if name.hasPrefix("job.") {
            notice = payload["message"]?.stringValue ?? name.replacingOccurrences(of: ".", with: " ").capitalized
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

    nonisolated static func chatHistory(from value: JSONValue) -> [ChatEntry] {
        let records = value["records"]?.arrayValue
            ?? items(from: value, keys: ["messages", "history", "items"])
        return records.compactMap { item in
            guard let rawRole = item["role"]?.stringValue,
                  let text = item["text"]?.stringValue ?? item["content"]?.stringValue,
                  !text.isEmpty,
                  let role = displayChatRole(rawRole),
                  !isReasoningRecord(role: rawRole, state: item["state"]?.stringValue) else { return nil }
            return ChatEntry(role: role, text: text, state: item["state"]?.stringValue ?? "complete")
        }
    }

    nonisolated static func displayChatRole(_ role: String) -> String? {
        switch role.lowercased() {
        case "user", "you": "you"
        case "assistant", "claude": "Claude"
        case "tool": "Tool"
        case "system", "reasoning", "analysis", "thinking": nil
        default: nil
        }
    }

    nonisolated private static func chatRole(_ role: String) -> String {
        displayChatRole(role) ?? ""
    }

    nonisolated private static func isReasoningRecord(role: String, state: String?) -> Bool {
        let value = role.lowercased()
        let status = state?.lowercased() ?? ""
        return ["reasoning", "analysis", "thinking"].contains(value)
            || ["reasoning", "analysis", "thinking"].contains(status)
    }

    nonisolated private static func appendChatRecord(_ record: JSONValue, to entries: inout [ChatEntry]) {
        let rawRole = record["role"]?.stringValue ?? ""
        guard let role = displayChatRole(rawRole),
              !isReasoningRecord(role: rawRole, state: record["state"]?.stringValue),
              let text = record["text"]?.stringValue ?? record["content"]?.stringValue,
              !text.isEmpty else { return }

        if role == "you", entries.last?.role == "you", entries.last?.text == text { return }
        if role == "Claude",
           let index = entries.indices.reversed().first(where: { entries[$0].role == "Claude" && entries[$0].state == "streaming" }) {
            entries[index].text = text
            entries[index].state = record["state"]?.stringValue ?? "complete"
        } else {
            entries.append(ChatEntry(role: role, text: text, state: record["state"]?.stringValue ?? "complete"))
        }
    }

    private func streamingAssistantIndex(in entries: [ChatEntry]) -> Int? {
        let start = min(chatOutputStartIndex ?? 0, entries.count)
        return entries.indices.reversed().first {
            $0 >= start && entries[$0].role == "Claude" && entries[$0].state == "streaming"
        }
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

    static func validate(root: URL, workspace: URL) throws -> [String] {
        guard FileManager.default.isExecutableFile(atPath: root.appending(path: "reels").path) else {
            throw ServiceFailure(code: "invalid_repo", message: "The selected --repo does not contain an executable reels launcher.", details: nil)
        }
        let rootPath = root.standardizedFileURL.resolvingSymlinksInPath().path
        let workspacePath = workspace.standardizedFileURL.resolvingSymlinksInPath().path
        guard workspacePath != rootPath, !(rootPath.hasPrefix(workspacePath + "/")) else {
            throw ServiceFailure(code: "invalid_workspace", message: "Choose a separate private --workspace directory; the repository root itself cannot be used.", details: nil)
        }
        let marker = root.appending(path: ".reels-editor-runtime.json")
        if FileManager.default.fileExists(atPath: marker.path) {
            let metadata = try marker.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
            guard metadata.isRegularFile == true, metadata.isSymbolicLink != true,
                  let record = try JSONSerialization.jsonObject(with: Data(contentsOf: marker)) as? [String: Any],
                  record["version"] as? Int == 1,
                  record["kind"] as? String == "cycling-reels-editor-runtime",
                  record["status"] as? String == "ready",
                  let digest = record["codeDigest"] as? String,
                  digest.count == 64, digest.allSatisfy({ $0.isHexDigit }),
                  let source = record["sourceRoot"] as? String, source.hasPrefix("/") else {
                throw ServiceFailure(code: "invalid_runtime", message: "Invalid installed editor runtime. Run editor/reels-editor setup again.", details: nil)
            }
            let production = URL(fileURLWithPath: source).standardizedFileURL.resolvingSymlinksInPath().path
            guard rootPath != production, !rootPath.hasPrefix(production + "/"),
                  !production.hasPrefix(rootPath + "/") else {
                throw ServiceFailure(code: "production_repo", message: "The installed runtime must be outside the production checkout.", details: nil)
            }
            guard workspacePath != production, !workspacePath.hasPrefix(production + "/"),
                  !production.hasPrefix(workspacePath + "/"), !workspacePath.hasPrefix(rootPath + "/") else {
                throw ServiceFailure(code: "production_workspace", message: "Choose a private workspace outside the production checkout and runtime.", details: nil)
            }
            for relative in [".venv", "remotion/node_modules"] {
                let dependency = root.appending(path: relative)
                let values = try dependency.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
                guard values.isDirectory == true, values.isSymbolicLink != true else {
                    throw ServiceFailure(code: "shared_dependencies", message: "Editor dependencies must be independent directories. Run editor/reels-editor setup.", details: nil)
                }
            }
            return [production, rootPath].sorted()
        }
        let git = Process()
        let pipe = Pipe()
        git.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        git.arguments = ["-C", rootPath, "worktree", "list", "--porcelain"]
        git.standardOutput = pipe
        git.standardError = Pipe()
        try git.run()
        let output = pipe.fileHandleForReading.readDataToEndOfFile()
        git.waitUntilExit()
        guard git.terminationStatus == 0, let text = String(data: output, encoding: .utf8) else {
            throw ServiceFailure(code: "unverified_repo", message: "Could not verify the selected repository as an isolated Git worktree.", details: nil)
        }
        let worktrees = text.components(separatedBy: "\n")
            .filter { $0.hasPrefix("worktree ") }
            .map { String($0.dropFirst("worktree ".count)) }
            .map { URL(fileURLWithPath: $0).standardizedFileURL.resolvingSymlinksInPath().path }
        guard worktrees.count > 1, worktrees.first != rootPath else {
            throw ServiceFailure(code: "production_repo", message: "The main checkout is not an editor target. Select the isolated editor worktree and a private workspace.", details: nil)
        }
        if let productionRoot = worktrees.first,
           workspacePath == productionRoot || workspacePath.hasPrefix(productionRoot + "/") {
            throw ServiceFailure(code: "production_workspace", message: "The workspace is inside the production checkout; choose a private editor workspace instead.", details: nil)
        }
        return Array(Set(worktrees + [rootPath])).sorted()
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
