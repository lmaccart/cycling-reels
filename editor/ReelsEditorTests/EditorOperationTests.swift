import AVFoundation
import XCTest
@testable import ReelsEditor

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

    func testEmptyPreviewReleasesExactPlayerAndDeactivatesTransport() {
        let item = AVPlayerItem(url: URL(fileURLWithPath: "/tmp/empty-preview-test.mp4"))
        let player = AVPlayer(playerItem: item)
        let model = ExactProgramPlayerModel()
        model.player = player
        model.setActive(true)

        model.clear()
        NotificationCenter.default.post(name: .reelsTogglePlayback, object: nil)

        XCTAssertFalse(model.isActive)
        XCTAssertNil(model.player)
        XCTAssertNil(player.currentItem)
        XCTAssertEqual(player.rate, 0)
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
                "probe": {"duration": 12, "fps": 30, "width": 1280, "height": 720, "hasAudio": False}})
        write_json(project.plans_dir / "timeline.json", {"slug": "timeline", "autoSfx": False,
            "segments": [{"asset": "a", "in": 0, "out": 4},
                         {"asset": "b", "in": 4, "out": 12},
                         {"asset": "c", "in": 0, "out": 2}]})
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

        store.performShortcut(.split)
        current = try await settle(after: current.revisions.document)
        var pieces = current.nodes.filter { $0.kind == "segment" && $0.assetId == "b" }
            .sorted { $0.startFrame < $1.startFrame }
        XCTAssertEqual(pieces.count, 2)
        XCTAssertEqual(pieces.map { ($0.sourceIn ?? -1, $0.sourceOut ?? -1) }.map { "\($0.0)-\($0.1)" },
                       ["4.0-7.0", "7.0-12.0"])
        XCTAssertTrue(pieces.allSatisfy { $0.raw["enabled"]?.boolValue == false })

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
        store.insertDroppedItem("flag:\(flagged.id)", atFrame: current.editingDurationFrames, trackID: "V1")
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
    }
}
