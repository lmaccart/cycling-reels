import AVKit
import SwiftUI
import WebKit
import Vision
import XCTest
@testable import ReelsEditor

// Unordered windows still exercise SwiftUI's visible-view update scheduling.
// No window is placed on the user's screen or made key.
@MainActor
private final class HostedTestWindow: NSWindow {
    override var occlusionState: NSWindow.OcclusionState { .visible }
}

@MainActor
private final class DeleteRedrawObservation {
    var inputTime: TimeInterval?
    var redrawTime: TimeInterval?

    func recordRedraw() {
        guard inputTime != nil, redrawTime == nil else { return }
        redrawTime = ProcessInfo.processInfo.systemUptime
    }
}

private struct DeleteLatencyMeasurement {
    let scenario: String
    let nodeCount: Int
    let milliseconds: Double
}

@MainActor
private final class FlagRowMouseInput: NSObject {
    let events: [NSEvent]
    private var index = 0
    private(set) var released = false
    init(_ events: [NSEvent]) { self.events = events }
    @objc func post(_ timer: Timer) {
        NSApp.postEvent(events[index], atStart: false)
        released = events[index].type == .leftMouseUp
        index = min(index + 1, events.count - 1)
    }
}

@MainActor
final class EditorOperationTests: XCTestCase {
    func testBrollInsertUsesPrimaryStableIDAndLocalTime() {
        let operation = BrollOperationBuilder.insert(
            parentID: "primary-7", assetID: "asset-bike",
            sourceIn: 1.25, sourceOut: 4.5, at: 2.1, isPhoto: false
        )
        XCTAssertEqual(operation["op"]?.stringValue, "insert_overlay")
        XCTAssertEqual(operation["parentId"]?.stringValue, "primary-7")
        XCTAssertEqual(operation["collection"]?.stringValue, "cutaways")
        XCTAssertEqual(operation["value"]?["asset"]?.stringValue, "asset-bike")
        XCTAssertEqual(operation["value"]?["in"]?.numberValue, 1.25)
        XCTAssertEqual(operation["value"]?["out"]?.numberValue, 4.5)
        XCTAssertEqual(operation["value"]?["at"]?.numberValue, 2.1)
    }

    func testCutawayReplacementOnlySetsSourceFieldsByStableID() {
        let operations = BrollOperationBuilder.replace(
            cutawayID: "cutaway-uuid", assetID: "new-asset",
            sourceIn: 3, sourceOut: 8, isPhoto: false
        )
        XCTAssertEqual(operations.count, 3)
        XCTAssertEqual(operations.map { $0["op"]?.stringValue }, ["set_field", "set_field", "set_field"])
        XCTAssertEqual(operations.map { $0["id"]?.stringValue }, Array(repeating: "cutaway-uuid", count: 3))
        XCTAssertEqual(operations.map { $0["path"]?.stringValue }, ["/asset", "/in", "/out"])
        XCTAssertEqual(operations[0]["value"]?.stringValue, "new-asset")
        XCTAssertEqual(operations[1]["value"]?.numberValue, 3)
        XCTAssertEqual(operations[2]["value"]?.numberValue, 8)
    }

    func testCutawayTrimKeepsStableIdentityAndEnforcesPositiveRange() {
        let operation = BrollOperationBuilder.trim(
            cutawayID: "cutaway-uuid", sourceIn: 2, sourceOut: 2.01, minimumDuration: 0.1
        )
        XCTAssertEqual(operation["op"]?.stringValue, "trim_cutaway")
        XCTAssertEqual(operation["id"]?.stringValue, "cutaway-uuid")
        XCTAssertEqual(operation["sourceIn"]?.numberValue, 2)
        XCTAssertEqual(try XCTUnwrap(operation["sourceOut"]?.numberValue), 2.1, accuracy: 0.0001)
    }

    func testPhotoCutawayUsesDurationRatherThanSourceInOut() {
        let insert = BrollOperationBuilder.insert(
            parentID: "primary-7", assetID: "still",
            sourceIn: 0, sourceOut: 4, at: 1.25, isPhoto: true
        )
        XCTAssertEqual(insert["value"]?["duration"]?.numberValue, 4)
        XCTAssertNil(insert["value"]?["in"])
        XCTAssertNil(insert["value"]?["out"])
        let replace = BrollOperationBuilder.replace(
            cutawayID: "cutaway-uuid", assetID: "still",
            sourceIn: 0, sourceOut: 5, isPhoto: true
        )
        XCTAssertEqual(replace.map { $0["path"]?.stringValue }, ["/asset", "/duration"])
        XCTAssertEqual(replace[1]["value"]?.numberValue, 5)
    }

    func testTimelinePhotoDropUsesDefaultDurationWithoutVideoBounds() throws {
        let photo: JSONValue = .object(["kind": .string("photo"), "filename": .string("still.jpg")])
        let value = try XCTUnwrap(EditorStore.timelineInsertSpec(
            assetID: "still", metadata: photo, sourceIn: nil, sourceOut: nil
        ))
        XCTAssertEqual(value["duration"]?.numberValue, 2.5)
        XCTAssertNil(value["in"])
        XCTAssertNil(value["out"])
        let marked = try XCTUnwrap(EditorStore.timelineInsertSpec(
            assetID: "still", metadata: photo, sourceIn: 1, sourceOut: 4
        ))
        XCTAssertEqual(marked["duration"]?.numberValue, 3)
        XCTAssertNil(EditorStore.timelineInsertSpec(
            assetID: "video", metadata: .object(["kind": .string("video")]),
            sourceIn: nil, sourceOut: nil
        ))
    }

    func testLockedAudioDetectsAudibleInsertOnEmptyVideoTrack() {
        let insert: JSONValue = .object([
            "asset": .string("source"), "in": .number(0), "out": .number(2)
        ])
        let placements = [TimelinePlacement.inserting(insert, track: "V2", atFrame: 90)]
        let audible: JSONValue = .object([
            "kind": .string("video"), "probe": .object(["hasAudio": .bool(true)])
        ])
        let silent: JSONValue = .object([
            "kind": .string("video"), "probe": .object(["hasAudio": .bool(false)])
        ])
        XCTAssertTrue(EditorStore.overwriteChangesLockedAudio(
            placements: placements, regions: [], nodes: [], assets: ["source": audible]
        ))
        XCTAssertFalse(EditorStore.overwriteChangesLockedAudio(
            placements: placements, regions: [], nodes: [], assets: ["source": silent]
        ))
        XCTAssertTrue(EditorStore.overwriteChangesLockedAudio(
            placements: placements, regions: [], nodes: []
        ))
        XCTAssertFalse(EditorStore.overwriteChangesLockedAudio(
            placements: [.inserting(.object(["asset": .string("still"), "duration": .number(2.5)]),
                                     track: "V2", atFrame: 90)],
            regions: [], nodes: [], assets: ["still": .object(["kind": .string("photo")])]
        ))
        XCTAssertFalse(EditorStore.insertionHasAudio(
            .object(["asset": .string("source"), "volume": .number(0)]), metadata: audible
        ) ?? true)
    }
    func testSplitTargetsSelectedClipsOrEveryClipUnderPlayhead() {
        let nodes = [
            EditorNode(id: "a", kind: "segment", lane: "video", parentId: nil, startFrame: 0, endFrame: 40,
                       frames: 40, raw: .object([:]), assetId: nil, sourceIn: nil, sourceOut: nil,
                       assetKind: nil, generated: nil, readOnly: nil, track: nil),
            EditorNode(id: "b", kind: "segment", lane: "video", parentId: nil, startFrame: 20, endFrame: 60,
                       frames: 40, raw: .object([:]), assetId: nil, sourceIn: nil, sourceOut: nil,
                       assetKind: nil, generated: nil, readOnly: nil, track: nil),
            EditorNode(id: "c", kind: "segment", lane: "video", parentId: nil, startFrame: 60, endFrame: 80,
                       frames: 20, raw: .object([:]), assetId: nil, sourceIn: nil, sourceOut: nil,
                       assetKind: nil, generated: nil, readOnly: nil, track: nil),
            EditorNode(id: "readonly", kind: "segment", lane: "video", parentId: nil, startFrame: 10, endFrame: 50,
                       frames: 40, raw: .object([:]), assetId: nil, sourceIn: nil, sourceOut: nil,
                       assetKind: nil, generated: nil, readOnly: true, track: nil)
        ]
        XCTAssertEqual(TimelineEditingTargets.split(nodes: nodes, selectedIDs: [], at: 30), ["a", "b"])
        XCTAssertEqual(TimelineEditingTargets.split(nodes: nodes, selectedIDs: ["b"], at: 30), ["b"])
        XCTAssertEqual(TimelineEditingTargets.split(nodes: nodes, selectedIDs: ["c"], at: 30), [])
    }
    func testNewTimelineEditingOperationsEncodeContractFields() {
        let ids = ["a", "b"]
        let duplicate = TimelineEditingOperation.duplicate(ids: ids)
        XCTAssertEqual(duplicate["op"]?.stringValue, "duplicate_segments")
        XCTAssertEqual(duplicate["ids"]?.arrayValue?.compactMap(\.stringValue), ids)

        let remove = TimelineEditingOperation.rippleRemove(ids: ids)
        XCTAssertEqual(remove["op"]?.stringValue, "ripple_remove")
        XCTAssertEqual(remove["ids"]?.arrayValue?.compactMap(\.stringValue), ids)

        let raw = JSONValue.object(["asset": .string("source"), "in": .number(1), "out": .number(4)])
        let paste = TimelineEditingOperation.paste(values: [raw], atFrame: 120, track: "V2")
        XCTAssertEqual(paste["op"]?.stringValue, "paste_segments")
        XCTAssertEqual(paste["values"]?.arrayValue, [raw])
        XCTAssertEqual(paste["atFrame"]?.numberValue, 120)
        XCTAssertEqual(paste["track"]?.stringValue, "V2")
        XCTAssertEqual(TimelineEditingOperation.paste(values: [raw], atFrame: 0, track: nil)["track"], .null)

        let enabled = TimelineEditingOperation.setEnabled(ids: ids, enabled: false)
        XCTAssertEqual(enabled["op"]?.stringValue, "set_enabled")
        XCTAssertEqual(enabled["ids"]?.arrayValue?.compactMap(\.stringValue), ids)
        XCTAssertEqual(enabled["enabled"]?.boolValue, false)
    }
    func testFlaggedDropResolvesFlagIdentityToAssetMetadataAndRange() throws {
        let flag = try XCTUnwrap(ClipFlag(.object([
            "id": .string("flag-1"), "asset": .string("asset-2"),
            "in": .number(2.5), "out": .number(8)
        ])))
        let metadata = JSONValue.object(["id": .string("asset-2"), "name": .string("ridge")])
        let resolved = try XCTUnwrap(TimelineEditingTargets.flaggedDrop(
            id: "flag:flag-1", flags: [flag], library: [metadata]
        ))
        XCTAssertEqual(resolved.assetID, "asset-2")
        XCTAssertEqual(resolved.metadata["name"]?.stringValue, "ridge")
        XCTAssertEqual(resolved.sourceIn, 2.5)
        XCTAssertEqual(resolved.sourceOut, 8)
        XCTAssertNil(TimelineEditingTargets.flaggedDrop(id: "flag:gone", flags: [flag], library: [metadata]))
    }

    func testClipContextMenuContentsMatchClipKindAndSphereProbe() {
        let sphere = EditorNode(id: "sphere-segment", kind: "segment", lane: "video", parentId: nil,
                                startFrame: 0, endFrame: 60, frames: 60,
                                raw: .object(["asset": .string("sphere-asset")]), assetId: "sphere-asset",
                                sourceIn: 0, sourceOut: 2, assetKind: "video", generated: false,
                                readOnly: false, track: "V1")
        let sphereAsset: JSONValue = .object([
            "id": .string("sphere-asset"), "kind": .string("video"),
            "probe": .object(["sphere": .object(["projection": .string("equirectangular")])])
        ])
        XCTAssertEqual(TimelineContextMenuContents.groups(node: sphere, library: [sphereAsset]), [
            ["Split here", "Split at playhead"],
            ["Delete", "Ripple delete", "Duplicate", "Copy", "Paste"],
            ["Speed", "Disable"],
            ["Flag source range", "Aim 360", "Reveal in Source"]
        ])
        let flat = JSONValue.object(["id": .string("sphere-asset"), "kind": .string("video"),
                                     "probe": .object(["sphere": .null])])
        XCTAssertFalse(TimelineContextMenuContents.groups(node: sphere, library: [flat])
            .flatMap { $0 }.contains("Aim 360"))
        let cutaway = EditorNode(id: "cutaway", kind: "cutaway", lane: "overlay", parentId: "sphere-segment",
                                 startFrame: 0, endFrame: 30, frames: 30, raw: .object([:]),
                                 assetId: nil, sourceIn: nil, sourceOut: nil, assetKind: nil,
                                 generated: false, readOnly: false, track: nil)
        XCTAssertEqual(TimelineContextMenuContents.groups(node: cutaway, library: [sphereAsset]), [
            ["Delete", "Paste"], ["Flag source range", "Reveal in Source"]
        ])
    }
    func testEditorServiceReturnsRecordedStubResponse() async throws {
        let service = EditorService()
        let recorded = JSONValue.object(["ok": .bool(true)])
        service.callStub = { method, params in
            XCTAssertEqual(method, "document.apply")
            XCTAssertEqual(params["reason"]?.stringValue, "test edit")
            return recorded
        }

        let response = try await service.call("document.apply", params: .object(["reason": .string("test edit")]))
        XCTAssertEqual(response, recorded)
    }
    func testTimelineToolKeySwitching() {
        XCTAssertEqual(TimelineTool.fromKeyCode(8), .razor)
        XCTAssertEqual(TimelineTool.fromKeyCode(9), .select)
        XCTAssertNil(TimelineTool.fromKeyCode(11))
    }

    private func previewSnapshot(revision: String, movie: URL, disabled: Set<Int> = [1],
                                 empty: Bool = false, mode: String = "exact") throws -> EditorSnapshot {
        let enabled = (0..<3).filter { !disabled.contains($0) }
        let manifest: JSONValue = empty
            ? .object(["state": .string("empty"), "mode": .string(mode), "revision": .string(revision)])
            : .object(["state": .string("ready"), "mode": .string(mode), "revision": .string(revision),
                       "movieURL": .string(movie.absoluteString)])
        let nodes: [JSONValue] = (0..<3).map { index in
            var node: [String: JSONValue] = [
                "id": .string("clip-\(index)"), "kind": .string("segment"), "lane": .string("primary"),
                "startFrame": .number(Double(index * 60)), "endFrame": .number(Double((index + 1) * 60)),
                "frames": .number(60), "raw": .object(disabled.contains(index) ? ["enabled": .bool(false)] : [:])
            ]
            if let renderedIndex = enabled.firstIndex(of: index) {
                node["playbackStartFrame"] = .number(Double(renderedIndex * 60))
                node["playbackEndFrame"] = .number(Double((renderedIndex + 1) * 60))
            }
            return .object(node)
        }
        return try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("preview-host"), "revisions": .object([
                "document": .string(revision), "library": .string("l"), "renderer": .string("r")
            ]),
            "plan": .object([:]), "nodes": .array(nodes), "fps": .number(30),
            "durationFrames": .number(Double(enabled.count * 60)), "editingDurationFrames": .number(180),
            "warnings": .array([]), "preview": manifest
        ])))
    }

    private func eventually(_ message: String, _ predicate: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(10)
        while !predicate(), Date() < deadline { try await Task.sleep(for: .milliseconds(10)) }
        XCTAssertTrue(predicate(), message)
    }

    private func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }

    private enum FlagRowGesture { case click, drag }

    /// Ask the production List's native drag source for its payload, not the store resolver.
    private func flagRowPasteboard(store: EditorStore, flag: ClipFlag,
                                   interaction: FlagRowGesture? = nil) async throws -> NSPasteboard {
        let host = NSHostingView(rootView: List(selection: Binding(
            get: { store.selectedAssetID }, set: { _ in }
        )) {
            Section("Flagged") {
                ForEach([flag]) { FlagRow(store: store, flag: $0) }
            }
        }.listStyle(.sidebar))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 300, height: 180),
                              styleMask: .titled, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.makeKeyAndOrderFront(nil)
        defer { window.close() }
        host.layoutSubtreeIfNeeded()
        try await eventually("Flagged List installs its native rows") {
            self.descendants(host).compactMap { $0 as? NSTableView }.contains { $0.numberOfRows > 1 }
        }
        let table = try XCTUnwrap(descendants(host).first { $0 is NSTableView } as? NSTableView)
        let dataSource = try XCTUnwrap(table.dataSource)
        for row in 0..<table.numberOfRows { _ = table.view(atColumn: 0, row: row, makeIfNecessary: true) }
        if let interaction {
            // In-process events exercise drag initiation and click, never the installed app.
            let rect = table.rect(ofRow: 1)
            let point = table.convert(NSPoint(x: rect.midX, y: rect.midY), to: nil)
            var eventNumber = 0
            let startTime = ProcessInfo.processInfo.systemUptime
            func event(_ type: NSEvent.EventType, offset: CGFloat = 0) throws -> NSEvent {
                eventNumber += 1
                return try XCTUnwrap(NSEvent.mouseEvent(with: type,
                    location: NSPoint(x: point.x + offset, y: point.y), modifierFlags: [],
                    timestamp: startTime + Double(eventNumber) * 0.05, windowNumber: window.windowNumber,
                    context: nil, eventNumber: eventNumber, clickCount: 1, pressure: 1))
            }
            func gesture(_ steps: [(NSEvent.EventType, CGFloat)], until predicate: () -> Bool) async throws {
                let down = try event(.leftMouseDown)
                // SwiftUI can defer mouseDown into NSTableView's nested tracking loop.
                let source = FlagRowMouseInput(try steps.map { try event($0.0, offset: $0.1) })
                let input = Timer(timeInterval: 0.05, target: source,
                                  selector: #selector(FlagRowMouseInput.post(_:)), userInfo: nil, repeats: true)
                RunLoop.main.add(input, forMode: .common)
                RunLoop.main.add(input, forMode: .eventTracking)
                defer { input.invalidate() }
                window.sendEvent(down)
                try await eventually("Native row gesture completes") { source.released && predicate() }
            }
            switch interaction {
            case .drag:
                let dragPasteboard = NSPasteboard(name: .drag)
                let beforeDrag = dragPasteboard.changeCount
                try await gesture([
                    (.leftMouseDragged, 10), (.leftMouseDragged, 25),
                    (.leftMouseDragged, 40), (.leftMouseUp, 40)
                ]) { dragPasteboard.changeCount != beforeDrag }
                XCTAssertEqual(dragPasteboard.string(forType: .string), "flag:\(flag.id)")
                XCTAssertNil(store.selectedAssetID, "Dragging must not open the source as a click")
            case .click:
                try await gesture([(.leftMouseUp, 0)]) {
                    store.selectedAssetID == flag.asset && store.sourceIn == flag.inPoint && store.sourceOut == flag.outPoint
                }
            }
        }
        let pasteboard = NSPasteboard.withUniqueName()
        for row in 0..<table.numberOfRows {
            let rect = table.rect(ofRow: row)
            _ = table.view(atColumn: 0, row: row, makeIfNecessary: true)
            guard table.canDragRows(with: IndexSet(integer: row),
                                    at: NSPoint(x: rect.midX, y: rect.midY)) else { continue }
            let writer: (any NSPasteboardWriting)?
            if let outline = table as? NSOutlineView, let item = outline.item(atRow: row) {
                writer = outline.dataSource?.outlineView?(outline, pasteboardWriterForItem: item)
            } else {
                writer = dataSource.tableView?(table, pasteboardWriterForRow: row)
            }
            if let writer {
                pasteboard.writeObjects([writer])
                if pasteboard.string(forType: .string) != nil { return pasteboard }
            }
        }
        pasteboard.releaseGlobally()
        XCTFail("A flagged row must supply native text before the timeline can receive a drag")
        throw NSError(domain: "FlagRowDrag", code: 1)
    }

    func testNativeFlagRowDragExportsAcceptedTextWithExactRange() async throws {
        let store = EditorStore()
        let flag = try XCTUnwrap(ClipFlag(.object([
            "id": .string("f-native"), "asset": .string("source"),
            "in": .number(0), "out": .number(55), "note": .string("flagged range")
        ])))
        store.flags = [flag]
        store.library = [.object(["id": .string("source"), "kind": .string("video"),
                                 "probe": .object(["duration": .number(120)])])]
        let metadata = try XCTUnwrap(store.library.first)
        store.service.callStub = { _, _ in metadata }
        let pasteboard = try await flagRowPasteboard(store: store, flag: flag, interaction: .drag)
        defer { pasteboard.releaseGlobally() }
        let payload = try XCTUnwrap(pasteboard.string(forType: .string))
        XCTAssertEqual(payload, "flag:f-native")
        let clickPasteboard = try await flagRowPasteboard(store: store, flag: flag, interaction: .click)
        defer { clickPasteboard.releaseGlobally() }
        XCTAssertEqual(store.selectedAssetID, "source")
        XCTAssertEqual(store.sourceIn, 0)
        XCTAssertEqual(store.sourceOut, 55)
        let resolved = try XCTUnwrap(TimelineEditingTargets.flaggedDrop(
            id: payload, flags: store.flags, library: store.library
        ))
        XCTAssertEqual(resolved.assetID, "source")
        XCTAssertEqual(resolved.sourceIn, 0)
        XCTAssertEqual(resolved.sourceOut, 55)
    }

    private func renderedText(_ view: NSView) throws -> String {
        view.layoutSubtreeIfNeeded()
        let bitmap = try XCTUnwrap(view.bitmapImageRepForCachingDisplay(in: view.bounds))
        view.cacheDisplay(in: view.bounds, to: bitmap)
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        try VNImageRequestHandler(cgImage: XCTUnwrap(bitmap.cgImage)).perform([request])
        return (request.results ?? []).compactMap { $0.topCandidates(1).first?.string }.joined(separator: "\n")
    }

    private func capture(_ host: NSView, name: String) throws {
        host.layoutSubtreeIfNeeded()
        let bitmap = try XCTUnwrap(host.bitmapImageRepForCachingDisplay(in: host.bounds))
        host.cacheDisplay(in: host.bounds, to: bitmap)
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: ".cache/editor/timeline-captures")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try XCTUnwrap(bitmap.representation(using: .png, properties: [:]))
            .write(to: root.appending(path: name))
    }

    func testProgramMonitorLoadedPlayingEmptyTransportAndReenable() async throws {
        // Existing user footage, read-only: no generated media and no installed-app launch.
        let movie = URL(fileURLWithPath: ProcessInfo.processInfo.environment["REELS_TEST_MEDIA"]
            ?? "/Users/leif/rice/cycling/library/media/261115-img-3672-2875.mov")
        let asset = AVURLAsset(url: movie)
        let audio = try await asset.loadTracks(withMediaType: .audio)
        XCTAssertFalse(audio.isEmpty, "The loaded lifecycle regression must include real audio")
        let store = EditorStore()
        store.workspaceInfo = .object(["previewOrigin": .string("file:///")])
        var serverSnapshot = try previewSnapshot(revision: "ready", movie: movie)
        store.service.callStub = { method, _ in
            if method == "document.snapshot" {
                return try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(serverSnapshot))
            }
            return .object([:])
        }
        store.snapshot = serverSnapshot
        store.authoritativeSnapshot = serverSnapshot
        let model = ExactProgramPlayerModel()
        let host = NSHostingView(rootView: ProgramMonitor(store: store, exactPlayer: model))
        let window = HostedTestWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 420),
                              styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        try await eventually("ProgramMonitor loads a ready exact player") {
            model.player?.currentItem?.status == .readyToPlay
        }
        XCTAssertEqual(descendants(host).filter { $0 is AVPlayerView }.count, 1)
        XCTAssertFalse(try renderedText(host).contains("Final-quality render isn't ready"))
        try capture(host, name: "program-ready.png")
        let oldPlayer = try XCTUnwrap(model.player)
        await MainActor.run { store.performShortcut(.togglePlayback) }
        try await eventually("Ready transport actually advances loaded footage") {
            oldPlayer.rate > 0 && oldPlayer.currentTime().seconds > 0.1
        }

        serverSnapshot = try previewSnapshot(revision: "empty", movie: movie, disabled: [0, 1, 2], empty: true)
        store.service.onEvent?(.object(["event": .string("preview.ready"),
                                      "payload": .object(["manifest": serverSnapshot.preview])]))
        try await eventually("Authoritative empty preview tears down the hosted player") {
            store.snapshot?.revisions.document == "empty" && model.player == nil
                && self.descendants(host).allSatisfy { !($0 is AVPlayerView) }
        }
        XCTAssertTrue(try renderedText(host).contains("Nothing to preview"))
        try capture(host, name: "program-empty.png")
        // This command is awaited on the main actor; the notification observer completes synchronously.
        await MainActor.run { store.performShortcut(.togglePlayback) }
        XCTAssertNil(oldPlayer.currentItem)
        XCTAssertEqual(oldPlayer.rate, 0)
        XCTAssertFalse(model.isActive)
        XCTAssertNil(store.exactMovieURL)

        serverSnapshot = try previewSnapshot(revision: "reenabled", movie: movie)
        store.service.onEvent?(.object(["event": .string("preview.ready"),
                                      "payload": .object(["manifest": serverSnapshot.preview])]))
        try await eventually("Re-enable creates fresh loaded media, paused") {
            store.snapshot?.revisions.document == "reenabled"
                && model.player?.currentItem?.status == .readyToPlay
        }
        let restored = try XCTUnwrap(model.player)
        XCTAssertFalse(restored === oldPlayer)
        XCTAssertEqual(restored.rate, 0)
        XCTAssertNil(oldPlayer.currentItem)
        XCTAssertEqual(oldPlayer.rate, 0)
        await MainActor.run { store.performShortcut(.togglePlayback) }
        try await eventually("Fresh media remains playable after re-enable") { restored.rate > 0 }
        model.clear()
    }

    func testProgramMonitorPausedSeeksPreserveEditingGapsTailsAndActivePlaybackMapsBack() async throws {
        let movie = URL(fileURLWithPath: ProcessInfo.processInfo.environment["REELS_TEST_MEDIA"]
            ?? "/Users/leif/rice/cycling/library/media/261115-img-3672-2875.mov")
        let store = EditorStore()
        store.service.callStub = { _, _ in .object([:]) }
        store.workspaceInfo = .object(["previewOrigin": .string("file:///")])
        store.snapshot = try previewSnapshot(revision: "mixed", movie: movie)
        store.authoritativeSnapshot = store.snapshot
        let model = ExactProgramPlayerModel()
        let host = NSHostingView(rootView: ProgramMonitor(store: store, exactPlayer: model))
        let window = HostedTestWindow(contentRect: NSRect(x: 0, y: 0, width: 540, height: 420),
                              styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { model.clear(); window.close() }
        try await eventually("Exact media loads") { model.player?.currentItem?.status == .readyToPlay }
        for (editing, playback) in [(150, 90), (90, 60)] {
            store.setPlayhead(editing)
            try await eventually("Seek uses the authoritative compact clock") {
                abs((model.player?.currentTime().seconds ?? -1) * 30 - Double(playback)) < 1
            }
            try await Task.sleep(for: .milliseconds(250))
            XCTAssertEqual(store.playheadFrame, editing, "Paused periodic/seek feedback must preserve editing navigation")
        }
        store.setPlayhead(150)
        try await eventually("Seek lands in the last enabled segment") {
            abs((model.player?.currentTime().seconds ?? -1) * 30 - 90) < 1
        }
        await MainActor.run { store.performShortcut(.togglePlayback) }
        try await eventually("Active callbacks translate playback frames back into the last editing segment") {
            store.playheadFrame > 150 && store.playheadFrame < 180
        }
        await MainActor.run { store.performShortcut(.togglePlayback) }
        store.snapshot = try previewSnapshot(revision: "tail", movie: movie, disabled: [1, 2])
        store.authoritativeSnapshot = store.snapshot
        store.setPlayhead(150)
        try await eventually("Disabled tail seeks only the preview boundary") {
            abs((model.player?.currentTime().seconds ?? -1) * 30 - 60) < 1
        }
        try await Task.sleep(for: .milliseconds(250))
        XCTAssertEqual(store.playheadFrame, 150)
        store.snapshot = try previewSnapshot(revision: "all", movie: movie, disabled: [0, 1, 2], empty: true)
        store.authoritativeSnapshot = store.snapshot
        store.setPlayhead(175)
        try await eventually("All-disabled retains editing navigation with no player") { model.player == nil }
        XCTAssertEqual(store.playheadFrame, 175)
    }

    func testInteractiveCoordinatorMapsCommandsAndActiveFramesWithoutPausedSeekFeedback() throws {
        let movie = URL(string: "http://127.0.0.1:43127/preview.mp4")!
        let store = EditorStore()
        store.snapshot = try previewSnapshot(revision: "mixed", movie: movie, mode: "interactive")
        store.authoritativeSnapshot = store.snapshot
        let coordinator = ProgramWebView.Coordinator(store: store, origin: movie)
        coordinator.load(movie, manifest: .object([:]), revision: "mixed", frame: 150, rate: 1, origin: movie)
        XCTAssertEqual(coordinator.lastFrame, 90, "loadRevision receives rendered coordinates")
        store.playheadFrame = 150
        coordinator.receivePlaybackFrame(90, revision: "mixed", playing: false)
        XCTAssertEqual(store.playheadFrame, 150)
        coordinator.togglePlayback()
        coordinator.receivePlaybackFrame(95, revision: "mixed", playing: true)
        XCTAssertEqual(store.playheadFrame, 155)
        coordinator.receivePlaybackFrame(60, revision: "obsolete", playing: true)
        XCTAssertEqual(store.playheadFrame, 155)
        coordinator.togglePlayback()
        store.playheadFrame = 90
        coordinator.seekIfNeeded(frame: 90, generation: 1)
        XCTAssertEqual(coordinator.lastFrame, 60, "Seeking a disabled gap targets the next rendered boundary")
        coordinator.receivePlaybackFrame(60, revision: "mixed", playing: false)
        XCTAssertEqual(store.playheadFrame, 90)
        store.snapshot = try previewSnapshot(revision: "tail", movie: movie, disabled: [1, 2], mode: "interactive")
        store.authoritativeSnapshot = store.snapshot
        store.playheadFrame = 150
        coordinator.load(movie, manifest: .object([:]), revision: "tail", frame: 150, rate: 1, origin: movie)
        XCTAssertEqual(coordinator.lastFrame, 60)
        coordinator.receivePlaybackFrame(60, revision: "tail", playing: false)
        XCTAssertEqual(store.playheadFrame, 150)
        store.snapshot = try previewSnapshot(revision: "all", movie: movie, disabled: [0, 1, 2],
                                             empty: true, mode: "interactive")
        store.authoritativeSnapshot = store.snapshot
        store.playheadFrame = 175
        coordinator.togglePlayback()
        coordinator.receivePlaybackFrame(60, revision: "tail", playing: true)
        XCTAssertEqual(store.playheadFrame, 175)
        coordinator.deactivate()
    }

    func testContextAimAndUnsupportedCopyUseConsumerSelection() async throws {
        let movie = URL(string: "http://127.0.0.1/preview.mp4")!
        let store = EditorStore()
        var snapshot = try previewSnapshot(revision: "aim", movie: movie, disabled: [])
        snapshot.nodes[0].assetId = "sphere"
        snapshot.nodes[0].sourceIn = 0
        snapshot.nodes[0].sourceOut = 2
        snapshot.nodes[0].raw = .object(["asset": .string("sphere"), "in": .number(0), "out": .number(2)])
        snapshot.nodes.append(EditorNode(id: "text", kind: "text", lane: "text", parentId: "clip-0",
            startFrame: 0, endFrame: 30, frames: 30, raw: .object(["text": .string("caption")]),
            assetId: nil, sourceIn: nil, sourceOut: nil, assetKind: nil, generated: nil, readOnly: nil))
        store.snapshot = snapshot
        store.authoritativeSnapshot = snapshot
        store.library = [.object(["id": .string("sphere"), "kind": .string("video"),
                                  "probe": .object(["sphere": .object([:])])])]
        let cameraRead = expectation(description: "Context Aim opens the selected node's camera")
        store.service.callStub = { method, params in
            if method == "sphere.camera" {
                XCTAssertEqual(params["id"]?.stringValue, "clip-0")
                cameraRead.fulfill()
                return .object([
                    "assetId": .string("sphere"), "in": .number(0), "out": .number(2),
                    "speed": .number(1), "seconds": .number(2), "projection": .string("flat"),
                    "view": .object(["yaw": .number(25), "pitch": .number(0), "fov": .number(90)]),
                    "path": .array([]), "angles": .object([:])
                ])
            }
            return .object([:])
        }
        store.performTimelineContextAction("Aim 360", nodeID: "clip-0", atFrame: 0)
        await fulfillment(of: [cameraRead], timeout: 5)
        XCTAssertTrue(store.sphereAim.isActive)
        XCTAssertEqual(store.sphereAim.boundNodeID, "clip-0")
        XCTAssertEqual(store.sphereAim.camera?.assetID, "sphere")
        store.sphereAim.close()
        store.selectedIDs = ["clip-0"]
        store.performTimelineContextAction("Copy", nodeID: "clip-0", atFrame: 0)
        XCTAssertEqual(store.copiedSegments.first?["asset"]?.stringValue, "sphere")
        store.selectedIDs = ["text"]
        store.performTimelineContextAction("Copy", nodeID: "text", atFrame: 0)
        XCTAssertTrue(store.copiedSegments.isEmpty, "Unsupported Copy must not paste unrelated prior footage")
    }

    func testHostedDeleteBeforeRealServiceAcknowledgementAndRollback() async throws {
        _ = try await hostedDeleteScenarios()
    }

    func testReleaseHostedDeleteInputToRedrawLatency() async throws {
        #if DEBUG
        throw XCTSkip("The 50 ms latency gate requires an explicitly Release-configured test pass; Debug runs deterministic ordering/rollback checks.")
        #else
        for measurement in try await hostedDeleteScenarios() {
            print("DELETE-LATENCY Release \(measurement.scenario) nodes=\(measurement.nodeCount): input-to-redraw=\(measurement.milliseconds)ms; backend apply gated")
            XCTAssertLessThan(measurement.milliseconds, 50,
                              "Release input-to-redraw budget with 570 camera keyframes: \(measurement.scenario)")
        }
        #endif
    }

    private func hostedDeleteScenarios() async throws -> [DeleteLatencyMeasurement] {
        var measurements: [DeleteLatencyMeasurement] = []
        let code = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let python = ProcessInfo.processInfo.environment["REELS_TEST_PYTHON"]
            ?? "/Users/leif/rice/cycling/.venv/bin/python"
        XCTAssertTrue(FileManager.default.isExecutableFile(atPath: python))
        for scenario in ["sequential", "positioned", "ripple", "rejected"] {
            let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
            try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
            defer { try? FileManager.default.removeItem(at: root) }
            // The real broker/compiler/persistence/protocol run unchanged. Only hold
            // apply before dispatch, and disable unrelated automatic media rendering.
            let server = """
            import os, time
            from pathlib import Path
            from reels.config import Project
            from reels.library import save_asset
            from reels.util import quick_hash, write_json
            from reels.editor.workspace import Workspace
            from reels.editor import service
            root = Path.cwd()
            project = Project(root)
            project.ensure_dirs()
            (root / "remotion").mkdir()
            media = project.media_dir / "ride.mp4"
            media.write_bytes(b"fingerprint-only test asset")
            save_asset(project, {"id": "ride", "kind": "video", "media": "library/media/ride.mp4",
                "hash": quick_hash(media), "status": "tagged", "tags": {},
                "probe": {"duration": 60, "fps": 30, "width": 1280, "height": 720, "hasAudio": True}})
            segments = [{"asset": "ride", "in": 9.133333333333333, "out": 19.133333333333333},
                        {"asset": "ride", "in": 19.999666666666666, "out": 50.733333333333334}]
            for segment in segments:
                duration = segment["out"] - segment["in"]
                segment["path"] = [{"at": i * duration / 285, "yaw": i * 0.1, "pitch": 0, "fov": 90}
                                   for i in range(285)]
            plan = {"slug": "delete", "autoSfx": False, "segments": segments}
            if os.environ["REELS_DELETE_SCENARIO"] != "sequential":
                plan["timeline"] = {"version": 1, "fps": 30, "durationFrames": 1248}
                for segment, start, length in zip(segments, (26, 326), (300, 922)):
                    segment["timeline"] = {"track": "V1", "startFrame": start,
                        "durationFrames": length, "transitionInFrames": 0}
            write_json(project.plans_dir / "delete.json", plan)
            class GatedService(service.EditorService):
                def _changed(self, snapshot, **kwargs):
                    return super()._changed(snapshot, **{**kwargs, "prepare_preview": False})
                def dispatch(self, method, params, source="native"):
                    if method == "document.apply" and params.get("batch", {}).get("reason") != "External edit":
                        (root / "apply.waiting").touch()
                        deadline = time.monotonic() + 20
                        while not (root / "apply.release").exists():
                            if time.monotonic() > deadline:
                                raise RuntimeError("Test did not release document.apply")
                            time.sleep(0.005)
                    return super().dispatch(method, params, source)
            service.EditorService = GatedService
            service.serve(Workspace.live(root, code_root=Path(os.environ["REELS_TEST_CODE_ROOT"])))
            """
            try server.write(to: root.appending(path: "serve.py"), atomically: true, encoding: .utf8)
            let launcher = root.appending(path: "reels")
            try "#!/bin/sh\nexec \"$REELS_TEST_PYTHON\" serve.py\n".write(to: launcher, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: launcher.path)
            var env = ProcessInfo.processInfo.environment
            env["PYTHONPATH"] = code.appending(path: "scripts").path
            env["REELS_TEST_PYTHON"] = python
            env["REELS_TEST_CODE_ROOT"] = code.path
            env["REELS_DELETE_SCENARIO"] = scenario
            env["REELS_HOME"] = root.path
            let store = EditorStore()
            try store.service.start(repositoryRoot: root, environment: env)
            defer {
                FileManager.default.createFile(atPath: root.appending(path: "apply.release").path, contents: nil)
                store.service.stopSynchronously()
            }
            let opened = try await store.service.call("document.open", params: .object(["path": .string("plans/delete.json")]))
            let initial = try XCTUnwrap(EditorSnapshot.decode(opened))
            store.authoritativeSnapshot = initial
            store.snapshot = initial
            store.openPlanPath = "plans/delete.json"
            // No source decoding/proxy preparation is involved in this geometry test.
            store.sourceStates["ride"] = "unavailable"
            let ordered = OptimisticTimeline.segments(initial.nodes)
            XCTAssertEqual(ordered.count, 2)
            let target = ordered[scenario == "sequential" ? 1 : 0]
            let survivor = ordered[scenario == "sequential" ? 0 : 1]
            store.selectedIDs = [target.id]
            await store.loadInspectorSchema(for: target.id)
            // Include the production nested NSHostingView / NativeSplitView boundary,
            // not a canvas manually fed the predicted snapshot.
            let host = NSHostingView(rootView: ContentView(store: store))
            let window = HostedTestWindow(contentRect: NSRect(x: 0, y: 0, width: 1520, height: 940),
                                  styleMask: .titled, backing: .buffered, defer: false)
            window.isReleasedWhenClosed = false
            window.contentView = host
            defer { window.close() }
            try await eventually("Integrated timeline is hosted: \(scenario)") {
                self.descendants(host).contains { $0 is TimelineCanvas }
            }
            let canvas = try XCTUnwrap(descendants(host).compactMap { $0 as? TimelineCanvas }.first)
            func clip(_ node: EditorNode) -> NSAccessibilityElement? {
                (canvas.accessibilityChildren() ?? []).compactMap { $0 as? NSAccessibilityElement }.first {
                    $0.accessibilityLabel()?.contains(", V1, Video,") == true
                        && $0.accessibilityLabel()?.contains(", \(node.frames) frames") == true
                }
            }
            host.layoutSubtreeIfNeeded()
            XCTAssertFalse(window.isVisible, "Hosted verification must never order a window")
            XCTAssertFalse(window.isKeyWindow)
            XCTAssertTrue(canvas.window === window)
            store.setPlayhead(300)
            try await eventually("Both clips reach the integrated canvas: \(scenario)") {
                (clip(target)?.accessibilityFrame().width ?? 0) > 0
                    && (clip(survivor)?.accessibilityFrame().width ?? 0) > 0
            }
            let oldTargetFrame = try XCTUnwrap(clip(target)).accessibilityFrame()
            let scale = oldTargetFrame.width / CGFloat(target.frames)
            let timelineOriginX = oldTargetFrame.minX - CGFloat(target.startFrame) * scale
            try capture(host, name: "delete-\(scenario)-before.png")
            let redraw = DeleteRedrawObservation()
            // Offscreen NSView reports needsDisplay == false even when invalidated;
            // observe the setter request, as in the occlusion regression. No
            // production timing callback or ordered window is needed.
            let observation = canvas.observe(\.needsDisplay, options: [.new]) { _, _ in
                MainActor.assumeIsolated { redraw.recordRedraw() }
            }
            defer { observation.invalidate() }
            redraw.inputTime = ProcessInfo.processInfo.systemUptime
            if scenario == "ripple" {
                store.performTimelineContextAction("Ripple delete", nodeID: target.id, atFrame: target.startFrame)
            } else if scenario == "sequential" {
                canvas.keyDown(with: try XCTUnwrap(NSEvent.keyEvent(
                    with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                    windowNumber: window.windowNumber, context: nil, characters: "\u{7f}",
                    charactersIgnoringModifiers: "\u{7f}", isARepeat: false, keyCode: 51)))
            } else {
                store.deleteSelection()
            }
            XCTAssertFalse(store.snapshot?.nodes.contains { $0.id == target.id } ?? true,
                           "Prediction must publish on the input handler's turn, before any Task hop")
            XCTAssertEqual(store.pendingPredictions.count, 1)
            // The broker cannot save or acknowledge until this test releases it.
            try await eventually("Hosted clip disappears before backend acknowledgement: \(scenario)") {
                clip(target) == nil
            }
            XCTAssertNil(clip(target), "The actual hosted canvas must remove the clip before any server save: \(scenario)")
            XCTAssertNotNil(redraw.redrawTime, "Prediction must invalidate drawing, not only accessibility: \(scenario)")
            let expectedStart = scenario == "ripple" ? survivor.startFrame - target.frames : survivor.startFrame
            let predictedFrame = try XCTUnwrap(clip(survivor)).accessibilityFrame()
            let predictedScale = predictedFrame.width / CGFloat(survivor.frames)
            XCTAssertEqual(predictedFrame.minX, timelineOriginX + CGFloat(expectedStart) * predictedScale,
                           accuracy: 1, scenario)
            measurements.append(DeleteLatencyMeasurement(
                scenario: scenario, nodeCount: initial.nodes.count,
                milliseconds: (try XCTUnwrap(redraw.redrawTime) - XCTUnwrap(redraw.inputTime)) * 1000
            ))
            try capture(host, name: "delete-\(scenario)-optimistic.png")
            try await eventually("Backend receives apply and remains gated: \(scenario)") {
                FileManager.default.fileExists(atPath: root.appending(path: "apply.waiting").path)
            }
            XCTAssertFalse(store.snapshot?.nodes.contains { $0.id == target.id } ?? true, scenario)
            let planURL = root.appending(path: "plans/delete.json")
            let unsavedPlan = try JSONDecoder().decode(JSONValue.self, from: Data(contentsOf: planURL))
            XCTAssertEqual(unsavedPlan["segments"]?.arrayValue?.count, 2, "Backend has not saved: \(scenario)")
            XCTAssertEqual(store.authoritativeSnapshot?.revisions, initial.revisions, scenario)
            // An intervening refresh must keep the prediction on screen.
            await store.refreshSnapshot()
            XCTAssertNil(clip(target), scenario)
            if scenario == "rejected" {
                // A genuine revision conflict, not a fabricated error response.
                _ = try await store.service.call("document.apply", params: .object(["batch": .object([
                    "reason": .string("External edit"), "expected": try XCTUnwrap(opened["revisions"]),
                    "operations": .array([.object(["op": .string("set_field"), "id": .string(survivor.id),
                        "path": .string("/volume"), "value": .number(0.5)])])
                ])]))
            }
            try Data().write(to: root.appending(path: "apply.release"))
            await store.editTail?.value
            try await eventually("Hosted reconciliation completes: \(scenario)") {
                scenario == "rejected" ? clip(target) != nil : clip(target) == nil
            }
            let saved = try JSONDecoder().decode(JSONValue.self, from: Data(contentsOf: planURL))
            XCTAssertEqual(saved["segments"]?.arrayValue?.count, scenario == "rejected" ? 2 : 1, scenario)
            XCTAssertTrue(store.pendingPredictions.isEmpty, scenario)
            if scenario == "rejected" {
                XCTAssertNotNil(store.errorMessage)
                let restored = try XCTUnwrap(clip(target)).accessibilityFrame()
                XCTAssertEqual(restored.minX, timelineOriginX
                    + CGFloat(target.startFrame) * restored.width / CGFloat(target.frames), accuracy: 1)
                XCTAssertEqual(store.snapshot?.nodes.first { $0.id == survivor.id }?.raw["volume"]?.numberValue, 0.5)
                // Do not let the expected error alert interfere with the capture.
                store.errorMessage = nil
            } else {
                XCTAssertNil(store.errorMessage, scenario)
                XCTAssertFalse(store.authoritativeSnapshot?.nodes.contains { $0.id == target.id } ?? true, scenario)
                XCTAssertEqual(store.snapshot?.nodes.first { $0.id == survivor.id }?.startFrame, expectedStart, scenario)
            }
            try capture(host, name: "delete-\(scenario)-reconciled.png")
        }
        return measurements
    }

    func testHostedZoomSliderBindingUpdatesClipGeometry() async throws {
        let store = EditorStore()
        store.snapshot = try previewSnapshot(revision: "zoom", movie: URL(string: "http://127.0.0.1/preview.mp4")!)
        let zoomControl = TimelineZoomControl()
        let slider = zoomControl.sliderPosition
        let host = NSHostingView(rootView: TimelinePanel(store: store, zoomControl: zoomControl))
        let window = HostedTestWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 450),
                                      styleMask: .titled, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        try await eventually("Production timeline canvas is hosted offscreen") {
            self.descendants(host).contains { $0 is TimelineCanvas }
        }
        let canvas = try XCTUnwrap(descendants(host).compactMap { $0 as? TimelineCanvas }.first)
        host.layoutSubtreeIfNeeded()
        func clipWidth() -> CGFloat {
            (canvas.accessibilityChildren() ?? []).compactMap { $0 as? NSAccessibilityElement }
                .first { $0.accessibilityLabel()?.contains(", V1, Video,") == true }?
                .accessibilityFrame().width ?? 0
        }
        // Drive the exact binding used by the production Slider. Changing zoom
        // after attachment also publishes geometry without a playhead refresh.
        func setSlider(zoom: Double) async throws {
            slider.wrappedValue = log(zoom)
            XCTAssertEqual(zoomControl.zoom, zoom, accuracy: 0.0001)
            try await assertClipWidth(zoom: zoom)
        }
        func assertClipWidth(zoom: Double) async throws {
            let expectedWidth = max(4, 60 * zoom)
            try await eventually("The production canvas reads zoom \(zoom)") {
                abs(clipWidth() - expectedWidth) < 0.01
            }
            XCTAssertEqual(clipWidth(), expectedWidth, accuracy: 0.01)
        }
        try await setSlider(zoom: 1)
        let originalWidth = clipWidth()
        try await setSlider(zoom: 0.25)
        XCTAssertLessThan(clipWidth(), originalWidth)
        try await setSlider(zoom: 2)
        XCTAssertGreaterThan(clipWidth(), originalWidth)
        try await setSlider(zoom: 1)
        XCTAssertEqual(clipWidth(), originalWidth, accuracy: 0.01)

        // Buttons and keyboard commands share these actions. Their changes must
        // flow back through the same Slider getter and into the hosted canvas.
        zoomControl.zoomOut()
        XCTAssertEqual(slider.wrappedValue, log(1 / 1.35), accuracy: 0.0001)
        try await assertClipWidth(zoom: 1 / 1.35)
        zoomControl.zoomIn()
        XCTAssertEqual(slider.wrappedValue, log(1), accuracy: 0.0001)
        try await assertClipWidth(zoom: 1)
        NotificationCenter.default.post(name: .reelsTimelineZoom, object: "in")
        XCTAssertEqual(slider.wrappedValue, log(1.35), accuracy: 0.0001)
        try await assertClipWidth(zoom: 1.35)
        NotificationCenter.default.post(name: .reelsTimelineZoom, object: "fit")
        try await assertClipWidth(zoom: TimelineMetrics.maxZoom)
        XCTAssertEqual(slider.wrappedValue, TimelineZoomControl.sliderRange.upperBound, accuracy: 0.0001)

        slider.wrappedValue = TimelineZoomControl.sliderRange.lowerBound - 1
        XCTAssertEqual(zoomControl.zoom, TimelineMetrics.minZoom, accuracy: 0.0001)
        try await assertClipWidth(zoom: TimelineMetrics.minZoom)
        slider.wrappedValue = TimelineZoomControl.sliderRange.upperBound + 1
        XCTAssertEqual(zoomControl.zoom, TimelineMetrics.maxZoom, accuracy: 0.0001)
        try await assertClipWidth(zoom: TimelineMetrics.maxZoom)
        XCTAssertFalse(window.isVisible)
        XCTAssertFalse(window.isKeyWindow)
        XCTAssertFalse(NSApp.isActive)
    }

    func testRealServiceDisabledSplitSpeedTrimPreviewUndoRedoRoundTrip() async throws {
        let code = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let python = ProcessInfo.processInfo.environment["REELS_TEST_PYTHON"]
            ?? "/Users/leif/rice/cycling/.venv/bin/python"
        guard FileManager.default.isExecutableFile(atPath: python) else {
            throw XCTSkip("Needs the read-only main checkout Python environment")
        }
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: root.appending(path: "plans"), withIntermediateDirectories: true)
        try FileManager.default.createDirectory(at: root.appending(path: "remotion"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let server = """
        import os
        from pathlib import Path
        from reels.config import Project
        from reels.library import save_asset
        from reels.util import quick_hash, write_json
        from reels.editor.workspace import Workspace
        from reels.editor.service import serve
        root = Path.cwd()
        project = Project(root)
        project.ensure_dirs()
        for aid in ("a", "b", "c"):
            media = project.media_dir / f"{aid}.mp4"
            media.write_bytes((aid * 128).encode())
            save_asset(project, {"id": aid, "kind": "video", "media": f"library/media/{aid}.mp4",
                "hash": quick_hash(media), "status": "tagged", "tags": {},
                "probe": {"duration": 12, "fps": 30, "width": 1280, "height": 720, "hasAudio": True}})
        write_json(project.plans_dir / "timeline.json", {"slug": "timeline", "autoSfx": False,
            "segments": [{"asset": "a", "in": 0, "out": 4},
                         {"asset": "b", "in": 4, "out": 12},
                         {"asset": "c", "in": 0, "out": 2}]})
        write_json(project.plans_dir / "split-locks.json", {"slug": "split-locks", "autoSfx": False,
            "timeline": {"version": 1, "fps": 30, "durationFrames": 120},
            "segments": [{"asset": aid, "in": 0, "out": 4,
                "timeline": {"track": track, "startFrame": 0, "durationFrames": 120, "transitionInFrames": 0}}
                for aid, track in (("a", "V1"), ("b", "V2"))]})
        serve(Workspace.live(root, code_root=Path(os.environ["REELS_TEST_CODE_ROOT"])))
        """
        try server.write(to: root.appending(path: "serve.py"), atomically: true, encoding: .utf8)
        let launcher = root.appending(path: "reels")
        try "#!/bin/sh\nexec \"$REELS_TEST_PYTHON\" serve.py\n".write(to: launcher, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: launcher.path)
        var env = ProcessInfo.processInfo.environment
        env["PYTHONPATH"] = code.appending(path: "scripts").path
        env["REELS_TEST_PYTHON"] = python
        env["REELS_TEST_CODE_ROOT"] = code.path
        env["REELS_HOME"] = root.path
        let store = EditorStore()
        try store.service.start(repositoryRoot: root, environment: env)
        defer { store.service.stopSynchronously() }
        let opened = try await store.service.call("document.open", params: .object(["path": .string("plans/timeline.json")]))
        let initial = try XCTUnwrap(EditorSnapshot.decode(opened))
        store.authoritativeSnapshot = initial
        store.snapshot = initial

        func settle(after revision: String) async throws -> EditorSnapshot {
            let deadline = Date().addingTimeInterval(8)
            while Date() < deadline {
                if let current = store.snapshot, current.revisions.document != revision { return current }
                try await Task.sleep(for: .milliseconds(10))
            }
            throw NSError(domain: "TimelineEditingIntegration", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: store.errorMessage ?? "Service edit did not reach the consumer snapshot"])
        }
        var current = initial
        let middle = try XCTUnwrap(current.nodes.first { $0.kind == "segment" && $0.assetId == "b" })
        store.selectedIDs = [middle.id]
        store.playheadFrame = 210
        store.performTimelineContextAction("Disable", nodeID: middle.id, atFrame: 210)
        current = try await settle(after: current.revisions.document)
        XCTAssertEqual(current.nodes.first { $0.id == middle.id }?.raw["enabled"]?.boolValue, false)
        XCTAssertEqual(current.nodes.first { $0.id == middle.id }?.startFrame, 120)
        XCTAssertEqual(current.editingDurationFrames, 420)
        XCTAssertEqual(current.durationFrames, 180)
        XCTAssertEqual(store.playheadFrame, 210)

        let timelineHost = NSHostingView(rootView: TimelinePanel(store: store))
        let timelineWindow = HostedTestWindow(contentRect: NSRect(x: 0, y: 0, width: 1000, height: 450),
                                      styleMask: .titled, backing: .buffered, defer: false)
        timelineWindow.isReleasedWhenClosed = false
        timelineWindow.contentView = timelineHost
        defer { timelineWindow.close() }
        try await eventually("Production timeline canvas is hosted") {
            self.descendants(timelineHost).contains { NSStringFromClass(type(of: $0)).hasSuffix(".TimelineCanvas") }
        }
        let canvas = try XCTUnwrap(descendants(timelineHost).first {
            NSStringFromClass(type(of: $0)).hasSuffix(".TimelineCanvas")
        })
        timelineHost.layoutSubtreeIfNeeded()
        store.setPlayhead(211)
        func clipWidth() -> CGFloat {
            (canvas.accessibilityChildren() ?? []).compactMap { $0 as? NSAccessibilityElement }
                .first { $0.accessibilityLabel()?.hasPrefix("b, V1,") == true }?
                .accessibilityFrame().width ?? 0
        }
        try await eventually("Hosted clip geometry is available") { clipWidth() > 0 }
        func key(_ code: UInt16, _ characters: String) throws -> NSEvent {
            try XCTUnwrap(NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [],
                timestamp: 0, windowNumber: timelineWindow.windowNumber, context: nil,
                characters: characters, charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code))
        }
        func clipEvent(_ type: NSEvent.EventType) throws -> NSEvent {
            let element = try XCTUnwrap((canvas.accessibilityChildren() ?? [])
                .compactMap { $0 as? NSAccessibilityElement }
                .first { $0.accessibilityLabel()?.hasPrefix("b, V1,") == true })
            let rect = timelineWindow.convertFromScreen(element.accessibilityFrame())
            let point = NSPoint(x: rect.minX + rect.width * 0.375, y: rect.midY)
            return try XCTUnwrap(NSEvent.mouseEvent(with: type, location: point, modifierFlags: [],
                timestamp: 0, windowNumber: timelineWindow.windowNumber, context: nil,
                eventNumber: 0, clickCount: 1, pressure: 1))
        }
        for entry in ["context menu", "razor"] {
            store.selectedIDs = [middle.id]
            store.setPlayhead(210)
            await Task.yield()
            if entry == "context menu" {
                let menu = try XCTUnwrap(canvas.menu(for: clipEvent(.rightMouseDown)))
                let split = try XCTUnwrap(menu.item(withTitle: "Split here"))
                XCTAssertTrue(NSApp.sendAction(try XCTUnwrap(split.action), to: split.target, from: split))
            } else {
                canvas.keyDown(with: try key(8, "c"))
                canvas.resetCursorRects()
                canvas.mouseDown(with: try clipEvent(.leftMouseDown))
            }
            current = try await settle(after: current.revisions.document)
            let splitPieces = current.nodes.filter { $0.kind == "segment" && $0.assetId == "b" }
                .sorted { $0.startFrame < $1.startFrame }
            XCTAssertEqual(splitPieces.map(\.sourceIn), [4, 7], entry)
            XCTAssertEqual(splitPieces.map(\.sourceOut), [7, 12], entry)
            XCTAssertTrue(splitPieces.allSatisfy { $0.raw["enabled"]?.boolValue == false }, entry)
            let splitRevision = current.revisions.document
            store.undo()
            current = try await settle(after: splitRevision)
            XCTAssertEqual(current.nodes.filter { $0.kind == "segment" && $0.assetId == "b" }.count, 1)
            canvas.keyDown(with: try key(9, "v"))
            canvas.resetCursorRects()
            await Task.yield()
        }
        let beforeSelect = current.revisions.document
        canvas.mouseDown(with: try clipEvent(.leftMouseDown))
        canvas.mouseUp(with: try clipEvent(.leftMouseUp))
        XCTAssertEqual(store.selectedIDs, [middle.id], "V restores selection instead of cutting")
        XCTAssertEqual(store.snapshot?.revisions.document, beforeSelect)

        store.performShortcut(.split)
        current = try await settle(after: current.revisions.document)
        var pieces = current.nodes.filter { $0.kind == "segment" && $0.assetId == "b" }
            .sorted { $0.startFrame < $1.startFrame }
        XCTAssertEqual(pieces.count, 2)
        XCTAssertEqual(pieces.map { ($0.sourceIn ?? -1, $0.sourceOut ?? -1) }.map { "\($0.0)-\($0.1)" },
                       ["4.0-7.0", "7.0-12.0"])
        XCTAssertTrue(pieces.allSatisfy { $0.raw["enabled"]?.boolValue == false })

        store.presentSpeedDialog = { alert in
            guard let field = alert.accessoryView as? NSTextField else {
                XCTFail("Custom speed must provide its editable input")
                return .alertSecondButtonReturn
            }
            field.stringValue = "1.5"
            return .alertFirstButtonReturn
        }
        store.performTimelineContextAction("Custom…", nodeID: pieces[0].id, atFrame: 210)
        current = try await settle(after: current.revisions.document)
        XCTAssertEqual(current.nodes.first { $0.id == pieces[0].id }?.raw["speed"]?.numberValue, 1.5)
        store.performTimelineContextAction("2×", nodeID: pieces[0].id, atFrame: 210)
        current = try await settle(after: current.revisions.document)
        pieces = current.nodes.filter { $0.kind == "segment" && $0.assetId == "b" }
            .sorted { $0.startFrame < $1.startFrame }
        XCTAssertEqual(pieces[0].raw["speed"]?.numberValue, 2)
        XCTAssertEqual(pieces[0].raw["enabled"]?.boolValue, false)

        let trimmed = pieces[0]
        store.trim(node: trimmed, sourceIn: 5, sourceOut: 7,
                   ghost: TimelineGhost(start: trimmed.startFrame, end: trimmed.endFrame))
        current = try await settle(after: current.revisions.document)
        let trimmedNode = try XCTUnwrap(current.nodes.first { $0.id == trimmed.id })
        XCTAssertEqual(trimmedNode.sourceIn, 5)
        XCTAssertEqual(trimmedNode.sourceOut, 7)
        XCTAssertEqual(trimmedNode.raw["enabled"]?.boolValue, false)
        let flagResult = try await store.service.call("flags.add", params: .object([
            "asset": .string("b"), "in": .number(1), "out": .number(3), "note": .string("range test")
        ]))
        let flagged = try XCTUnwrap(ClipFlag.list(flagResult).first)
        store.flags = [flagged]
        store.library = [.object([
            "id": .string("b"), "kind": .string("video"),
            "probe": .object(["duration": .number(12), "fps": .number(30)])
        ])]
        store.performTimelineContextAction("Flag source range", nodeID: trimmed.id, atFrame: trimmed.startFrame)
        try await eventually("Timeline Flag reaches the actual service and flags consumer") {
            store.flags.contains { $0.inPoint == 5 && $0.outPoint == 7 }
        }
        store.performTimelineContextAction("Reveal in Source", nodeID: trimmed.id, atFrame: trimmed.startFrame)
        try await eventually("Reveal opens the source selection and preserves its range") {
            store.selectedAssetID == "b" && store.sourceIn == 5 && store.sourceOut == 7
        }
        let flagPasteboard = try await flagRowPasteboard(store: store, flag: flagged)
        defer { flagPasteboard.releaseGlobally() }
        let flagPayload = try XCTUnwrap(flagPasteboard.string(forType: .string))
        let proposal = store.previewTimelineDrop(flagPayload, atFrame: current.editingDurationFrames,
                                                trackID: "V1", commandInsert: false)
        XCTAssertNil(proposal.rejection)
        XCTAssertEqual(proposal.placement?.insert?["in"]?.numberValue, 1)
        XCTAssertEqual(proposal.placement?.insert?["out"]?.numberValue, 3)
        XCTAssertEqual(proposal.placement?.insert?["asset"]?.stringValue, "b")
        store.insertDroppedItem(flagPayload, atFrame: current.editingDurationFrames, trackID: "V1")
        current = try await settle(after: current.revisions.document)
        let droppedFlag = try XCTUnwrap(current.nodes.first {
            $0.kind == "segment" && $0.assetId == "b" && $0.sourceIn == 1 && $0.sourceOut == 3
        })
        XCTAssertEqual(droppedFlag.sourceIn, 1)
        XCTAssertEqual(droppedFlag.sourceOut, 3)
        store.selectedIDs = [droppedFlag.id]
        store.performTimelineContextAction("Duplicate", nodeID: droppedFlag.id, atFrame: droppedFlag.startFrame)
        current = try await settle(after: current.revisions.document)
        let duplicate = try XCTUnwrap(current.nodes.first {
            $0.kind == "segment" && $0.assetId == "b" && $0.sourceIn == 1
                && $0.sourceOut == 3 && $0.id != droppedFlag.id
        })
        store.selectedIDs = [duplicate.id]
        store.performTimelineContextAction("Copy", nodeID: duplicate.id, atFrame: duplicate.startFrame)
        store.setPlayhead(current.editingDurationFrames)
        store.performTimelineContextAction("Paste", nodeID: duplicate.id, atFrame: store.playheadFrame)
        current = try await settle(after: current.revisions.document)
        let pasted = try XCTUnwrap(current.nodes.first {
            $0.kind == "segment" && $0.assetId == "b" && $0.sourceIn == 1
                && $0.sourceOut == 3 && $0.startFrame >= duplicate.endFrame && $0.id != duplicate.id
        })
        store.selectedIDs = [pasted.id]
        store.performTimelineContextAction("Ripple delete", nodeID: pasted.id, atFrame: pasted.startFrame)
        current = try await settle(after: current.revisions.document)
        XCTAssertFalse(current.nodes.contains { $0.id == pasted.id })

        let primaryIDs = OptimisticTimeline.segments(current.nodes).map(\.id)
        store.setSegmentsEnabled(primaryIDs, enabled: false)
        current = try await settle(after: current.revisions.document)
        XCTAssertTrue(OptimisticTimeline.segments(current.nodes).allSatisfy {
            $0.raw["enabled"]?.boolValue == false
        })
        XCTAssertEqual(current.durationFrames, 0)
        XCTAssertGreaterThan(current.editingDurationFrames, 0)
        let empty = try await store.service.call("preview.prepare", params: .object(["mode": .string("exact")]))
        XCTAssertEqual(empty["state"]?.stringValue, "empty")
        XCTAssertNil(empty["movieURL"])
        XCTAssertNil(empty["props"])
        XCTAssertEqual(store.previewManifest?["state"]?.stringValue, "empty")

        let beforeEnable = current.revisions.document
        store.setSegmentsEnabled(primaryIDs, enabled: true)
        current = try await settle(after: beforeEnable)
        XCTAssertTrue(OptimisticTimeline.segments(current.nodes).allSatisfy {
            $0.raw["enabled"]?.boolValue != false
        })
        let enabledRevision = current.revisions.document
        store.undo()
        current = try await settle(after: enabledRevision)
        XCTAssertTrue(OptimisticTimeline.segments(current.nodes).allSatisfy {
            $0.raw["enabled"]?.boolValue == false
        })
        let undoneRevision = current.revisions.document
        store.redo()
        current = try await settle(after: undoneRevision)
        XCTAssertTrue(OptimisticTimeline.segments(current.nodes).allSatisfy {
            $0.raw["enabled"]?.boolValue != false
        })
        XCTAssertGreaterThan(current.durationFrames, 0)

        let lockFixture = try await store.service.call("document.open", params: .object([
            "path": .string("plans/split-locks.json")
        ]))
        current = try XCTUnwrap(EditorSnapshot.decode(lockFixture))
        store.acceptRefreshedSnapshot(current)
        for (lockedTracks, expectedAssets) in [
            (Set<String>(), Set(["a", "b"])), (Set(["V1"]), Set(["b"])),
            (Set(["V2"]), Set(["a"])), (Set(["V1", "V2"]), Set<String>()),
            (Set(["A1"]), Set<String>()), (Set(["V1", "A1"]), Set<String>())
        ] {
            for track in ["V1", "V2", "A1"] {
                let locked = lockedTracks.contains(track)
                if store.tracks.first(where: { $0.id == track })?.locked != locked {
                    store.setTrackLocked(id: track, locked: locked)
                    current = try await settle(after: current.revisions.document)
                }
            }
            store.selectedIDs = []
            store.setPlayhead(60)
            let targets = Set(store.splitTargetsAtPlayhead)
            XCTAssertEqual(Set(current.nodes.filter { targets.contains($0.id) }.compactMap(\.assetId)), expectedAssets)
            if expectedAssets.isEmpty {
                XCTAssertEqual(current.nodes.filter { $0.kind == "segment" }.count, 2)
                continue
            }
            store.performShortcut(.split)
            current = try await settle(after: current.revisions.document)
            for aid in ["a", "b"] {
                let clips = current.nodes.filter { $0.kind == "segment" && $0.assetId == aid }
                    .sorted { $0.startFrame < $1.startFrame }
                XCTAssertEqual(clips.count, expectedAssets.contains(aid) ? 2 : 1)
                XCTAssertEqual(clips.first?.sourceOut, expectedAssets.contains(aid) ? 2 : 4)
                if expectedAssets.contains(aid) { XCTAssertEqual(clips.last?.sourceIn, 2) }
            }
            store.undo()
            current = try await settle(after: current.revisions.document)
            XCTAssertEqual(current.nodes.filter { $0.kind == "segment" }.count, 2)
        }
    }
}
