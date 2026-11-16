import Foundation
import XCTest
@testable import ReelsEditor

final class EditorModelTests: XCTestCase {
    func testRepositoryValidationNeedsExecutableLauncherAndPlansFolder() throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        XCTAssertThrowsError(try RepositoryLocator.validate(root))
        let launcher = root.appending(path: "reels")
        try "#!/bin/sh\nexit 0\n".write(to: launcher, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o644], ofItemAtPath: launcher.path)
        XCTAssertThrowsError(try RepositoryLocator.validate(root))
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: launcher.path)
        XCTAssertThrowsError(try RepositoryLocator.validate(root), "plans/ is still missing")
        try FileManager.default.createDirectory(at: root.appending(path: "plans"), withIntermediateDirectories: true)
        XCTAssertEqual(try RepositoryLocator.validate(root).lastPathComponent, root.lastPathComponent)
    }

    func testRepositoryPrecedenceIsArgumentThenEnvironmentThenSaved() {
        XCTAssertEqual(RepositoryLocator.candidatePath(
            arguments: ["app", "--repo", "/a"], environment: ["REELS_EDITOR_REPO_ROOT": "/b"], saved: "/c"), "/a")
        XCTAssertEqual(RepositoryLocator.candidatePath(
            arguments: ["app"], environment: ["REELS_EDITOR_REPO_ROOT": "/b"], saved: "/c"), "/b")
        XCTAssertEqual(RepositoryLocator.candidatePath(arguments: ["app"], environment: [:], saved: "/c"), "/c")
        XCTAssertNil(RepositoryLocator.candidatePath(arguments: ["app", "--repo"], environment: [:], saved: nil))
    }

    func testPathMergeKeepsLoginPathFirstAndDropsDuplicates() {
        let merged = AppEnvironment.mergedPath(
            loginPath: "/Users/x/.nvm/bin:/opt/homebrew/bin:/usr/bin",
            basePath: "/usr/bin:/bin:/usr/sbin:/sbin", home: "/Users/x")
        XCTAssertEqual(merged.split(separator: ":").map(String.init), [
            "/Users/x/.nvm/bin", "/opt/homebrew/bin", "/usr/bin", "/usr/local/bin",
            "/Users/x/.local/bin", "/bin", "/usr/sbin", "/sbin"
        ])
        XCTAssertTrue(AppEnvironment.mergedPath(loginPath: nil, basePath: nil, home: "/h").hasPrefix("/opt/homebrew/bin"))
        let environment = AppEnvironment.serviceEnvironment(base: ["PATH": "/usr/bin"], loginPath: nil, home: "/h")
        XCTAssertEqual(environment["PYTHONUNBUFFERED"], "1")
        XCTAssertTrue(environment["PATH"]?.contains("/h/.local/bin") == true)
    }

    func testLoginPathParsingIgnoresShellNoise() {
        XCTAssertEqual(AppEnvironment.parseMarkedPath("welcome\n__REELS_PATH__/a:/b__REELS_PATH__"), "/a:/b")
        XCTAssertNil(AppEnvironment.parseMarkedPath("no markers"))
    }

    func testLogRingKeepsLastLinesAndPartialOutput() {
        let ring = LogRing(capacity: 3)
        ring.append("one\ntwo\nthr")
        ring.append("ee\nfour\nfi")
        XCTAssertEqual(ring.tail(3), ["three", "four", "fi"])
        XCTAssertEqual(ring.tail(10).count, 4, "capacity 3 plus the partial line")
    }

    func testClaudeCodeCommandQuotesTheRepoPath() {
        let repo = URL(fileURLWithPath: "/Users/x/My Rides/it's")
        XCTAssertEqual(ClaudeCodeLauncher.shellCommand(repo: repo), "cd '/Users/x/My Rides/it'\\''s' && claude")
        XCTAssertTrue(ClaudeCodeLauncher.appleScript(repo: repo).contains("do script"))
    }

    func testPlansListDecodesNewFieldsAndToleratesMissingOnes() {
        let result = JSONValue.object(["plans": .array([
            .object([
                "path": .string("plans/2026-11-15-little-gray.json"), "title": .string("little gray"),
                "postDate": .string("2026-11-15"), "format": .string("talking"), "segmentCount": .number(3),
                "durationSeconds": .number(8.4), "status": .string("approved"),
                "thumbnailURL": .string("http://127.0.0.1:1/t.jpg"), "modified": .number(1_700_000_000)
            ]),
            .object(["path": .string("plans/old-one.json")]),
            .object(["title": .string("no path")])
        ])])
        let plans = PlanSummary.parse(result)
        XCTAssertEqual(plans.count, 2)
        XCTAssertEqual(plans[0].segmentCount, 3)
        XCTAssertEqual(plans[0].durationText, "0:08")
        XCTAssertEqual(plans[0].thumbnailURL?.host, "127.0.0.1")
        XCTAssertEqual(plans[1].title, "old-one")
        XCTAssertNil(plans[1].status)
        XCTAssertTrue(plans[0].matches("GRAY"))
        XCTAssertFalse(plans[0].matches("zzz"))
        XCTAssertTrue(PlanSummary.parse(.null).isEmpty)
    }

    @MainActor
    func testIngestJobEventsDriveTheImportPhase() {
        let importer = ImportCoordinator()
        importer.handleJob(.object(["kind": .string("ingest"), "state": .string("running"), "log": .string("copying a.mp4")]))
        XCTAssertEqual(importer.logLines, ["copying a.mp4"])
        XCTAssertEqual(importer.phase, .idle, "events outside an import session do not start one")
    }

    func testPlaybackProbeNeedsActualAdvancingFrames() {
        var probe = PreviewPlaybackProbe()
        XCTAssertFalse(probe.observe(100))
        probe.begin(at: 30)
        XCTAssertFalse(probe.observe(30))
        XCTAssertFalse(probe.observe(20))
        XCTAssertFalse(probe.observe(44))
        XCTAssertTrue(probe.observe(45))
        XCTAssertFalse(probe.observe(46))
        XCTAssertEqual(probe.firstFrame, 30)
        XCTAssertEqual(probe.lastFrame, 45)
    }

    @MainActor
    func testPermissionsRestoreFromTheServiceRecordArray() {
        let store = EditorStore()
        store.syncPermissions(from: .object(["permissions": .array([
            .object(["id": .string("pending"), "state": .string("pending")]),
            .object(["id": .string("granted"), "state": .string("granted")]),
            .object(["id": .string("denied"), "state": .string("denied")])
        ])]))
        XCTAssertEqual(store.pendingPermissions.count, 1)
        XCTAssertEqual(store.currentGrants.count, 1)
        XCTAssertTrue(store.isPermissionPromptPresented)
    }

    func testJSONValueRoundTripsNestedPlanData() throws {
        let source = """
        {"enabled":true,"render":{"fps":29.97},"segments":[{"asset":"ride.mov","in":1.25}]}
        """
        let value = try JSONDecoder().decode(JSONValue.self, from: Data(source.utf8))
        let roundTrip = try JSONDecoder().decode(JSONValue.self, from: try XCTUnwrap(value.jsonData()))
        XCTAssertEqual(roundTrip, value)
        XCTAssertEqual(value.value(at: ["render", "fps"])?.numberValue, 29.97)
    }

    func testSettingJSONPathPreservesSiblingFieldsAndArrays() {
        let original: JSONValue = .object([
            "render": .object(["fps": .number(30), "width": .number(1080)]),
            "segments": .array([.object(["asset": .string("ride.mov"), "in": .number(2)])])
        ])
        let changed = original.setting(.number(60), at: ["segments", "0", "in"])
        XCTAssertEqual(changed.value(at: ["segments", "0", "in"])?.numberValue, 60)
        XCTAssertEqual(changed.value(at: ["segments", "0", "asset"])?.stringValue, "ride.mov")
        XCTAssertEqual(changed.value(at: ["render", "fps"])?.numberValue, 30)
    }

    func testSnapshotDecodesServiceRevisionsAndStableNodeIDs() throws {
        let json = """
        {
          "documentId":"ride-1",
          "revisions":{"document":"d1","library":"l2","renderer":"r3"},
          "plan":{"slug":"ride"},
          "nodes":[{"id":"clip-1","kind":"segment","lane":"video","parentId":null,
                    "startFrame":0,"endFrame":90,"frames":90,"raw":{"asset":"ride.mov"},
                    "assetId":"asset-1","sourceIn":1.0,"sourceOut":4.0,
                    "assetKind":"video","generated":false,"readOnly":false}],
          "fps":30,"durationFrames":90,"warnings":[],"preview":{}
        }
        """
        let value = try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
        let snapshot = try XCTUnwrap(EditorSnapshot.decode(value))
        XCTAssertEqual(snapshot.revisions, Revisions(document: "d1", library: "l2", renderer: "r3"))
        XCTAssertEqual(snapshot.nodes.first?.id, "clip-1")
        XCTAssertEqual(snapshot.nodes.first?.sourceOut, 4)
        XCTAssertEqual(snapshot.nodes.first?.assetKind, "video")
        XCTAssertEqual(snapshot.nodes.first?.generated, false)
        XCTAssertEqual(snapshot.nodes.first?.isReadOnly, false)
    }

    func testSnapshotDeltaMergesPlanNodesRevisionsAndTopLevelStateSafely() throws {
        func node(_ id: String, _ asset: String, start: Int = 0) -> JSONValue {
            .object([
                "id": .string(id), "kind": .string("segment"), "lane": .string("video"),
                "parentId": .null, "startFrame": .number(Double(start)),
                "endFrame": .number(Double(start + 10)), "frames": .number(10),
                "raw": .object(["asset": .string(asset)]), "assetId": .string(asset),
                "generated": .bool(false), "readOnly": .bool(false),
            ])
        }
        let base = try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("doc"),
            "snapshotToken": .string("snapshot-1"),
            "revisions": .object([
                "document": .string("d1"), "library": .string("l1"), "renderer": .string("r1")
            ]),
            "plan": .object([
                "segments": .array([
                    .object(["asset": .string("a")]), .object(["asset": .string("b")])
                ]),
                "text": .array([.object(["text": .string("old")])]),
            ]),
            "nodes": .array([node("a", "a"), node("b", "b", start: 10)]),
            "fps": .number(30), "durationFrames": .number(20),
            "warnings": .array([]), "preview": .object(["state": .string("ready")]),
            "change": .object(["origin": .string("manual")]),
            "selection": .object(["ids": .array([])]),
            "config": .object(["render": .object(["fps": .number(30)])]),
        ])))
        let delta: JSONValue = .object([
            "type": .string("document.delta"), "documentId": .string("doc"),
            "baseSnapshotToken": .string("snapshot-1"), "snapshotToken": .string("snapshot-2"),
            "baseRevisions": .object([
                "document": .string("d1"), "library": .string("l1"), "renderer": .string("r1")
            ]),
            "revisions": .object([
                "document": .string("d2"), "library": .string("l1"), "renderer": .string("r1")
            ]),
            "planPatch": .array([
                .object([
                    "op": .string("add"), "path": .string("/segments/0/volume"), "value": .number(0.5)
                ]),
                .object(["op": .string("remove"), "path": .string("/text/0")]),
            ]),
            "changedNodes": .array([node("a", "a", start: 1), node("c", "c", start: 20)]),
            "removedIds": .array([.string("b")]),
            "nodeOrder": .array([.string("a"), .string("c")]),
            "fields": .object([
                "revisions": .object([
                    "document": .string("d2"), "library": .string("l1"), "renderer": .string("r1")
                ]),
                "fps": .number(24), "durationFrames": .number(30),
                "warnings": .array([.string("updated")]),
                "preview": .object(["state": .string("stale")]),
                "change": .object(["origin": .string("manual"), "affectedIds": .array([.string("a")])]),
                "selection": .object(["ids": .array([.string("a")])]),
            ]),
            "removedFields": .array([.string("config")]),
        ])

        let next = try XCTUnwrap(EditorSnapshot.applyingDelta(delta, to: base))
        XCTAssertEqual(next.revisions.document, "d2")
        XCTAssertEqual(next.snapshotToken, "snapshot-2")
        XCTAssertEqual(next.durationFrames, 30)
        XCTAssertEqual(next.fps, 24)
        XCTAssertEqual(next.warnings, ["updated"])
        XCTAssertEqual(next.preview["state"]?.stringValue, "stale")
        XCTAssertEqual(next.nodes.map(\.id), ["a", "c"])
        XCTAssertEqual(next.nodes[0].startFrame, 1)
        XCTAssertEqual(next.plan.value(at: ["segments", "0", "volume"])?.numberValue, 0.5)
        XCTAssertEqual(next.plan.value(at: ["text"])?.arrayValue?.count, 0)
        XCTAssertEqual(next.extraFields["change"]?["origin"]?.stringValue, "manual")
        XCTAssertNil(next.extraFields["config"])
        XCTAssertEqual(next.extraFields["selection"]?["ids"]?.arrayValue?.first?.stringValue, "a")
        XCTAssertNil(EditorSnapshot.applyingDelta(delta, to: next), "a delta cannot be replayed on a newer base")
        let wrongBase = delta.setting(.string("snapshot-other"), at: ["baseSnapshotToken"])
        XCTAssertNil(EditorSnapshot.applyingDelta(wrongBase, to: base), "same revisions are insufficient without exact snapshot identity")
    }

    @MainActor
    func testDeltaRebasesQueuedPredictionExactlyOnceOnAuthoritativeNodes() throws {
        func node(_ id: String, start: Int, end: Int, volume: Double? = nil) -> JSONValue {
            var raw: [String: JSONValue] = ["asset": .string(id)]
            if let volume { raw["volume"] = .number(volume) }
            return .object([
                "id": .string(id), "kind": .string("segment"), "lane": .string("video"),
                "parentId": .null, "startFrame": .number(Double(start)),
                "endFrame": .number(Double(end)), "frames": .number(Double(end - start)),
                "raw": .object(raw), "assetId": .string(id), "generated": .bool(false),
                "readOnly": .bool(false),
            ])
        }
        let revisions = Revisions(document: "d1", library: "l1", renderer: "r1")
        let base = try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("doc"), "snapshotToken": .string("snapshot-1"),
            "revisions": .object([
                "document": .string("d1"), "library": .string("l1"), "renderer": .string("r1")
            ]),
            "plan": .object([
                "segments": .array([
                    .object(["asset": .string("a")]),
                    .object(["asset": .string("b")]),
                    .object(["asset": .string("c")]),
                ])
            ]),
            "nodes": .array([node("a", start: 0, end: 30),
                             node("b", start: 30, end: 60),
                             node("c", start: 60, end: 90)]),
            "fps": .number(30), "durationFrames": .number(90),
            "warnings": .array([]), "preview": .object(["state": .string("empty")]),
        ])))
        let delta: JSONValue = .object([
            "type": .string("document.delta"), "documentId": .string("doc"),
            "baseSnapshotToken": .string("snapshot-1"), "snapshotToken": .string("snapshot-2"),
            "baseRevisions": .object([
                "document": .string("d1"), "library": .string("l1"), "renderer": .string("r1")
            ]),
            "revisions": .object([
                "document": .string("d2"), "library": .string("l1"), "renderer": .string("r1")
            ]),
            "planPatch": .array([
                .object(["op": .string("add"), "path": .string("/segments/1/volume"),
                         "value": .number(0.5)])
            ]),
            "changedNodes": .array([node("b", start: 30, end: 60, volume: 0.5)]),
            "removedIds": .array([]),
            "nodeOrder": .array([.string("a"), .string("b"), .string("c")]),
            "fields": .object(["revisions": .object([
                "document": .string("d2"), "library": .string("l1"), "renderer": .string("r1")
            ])]),
            "removedFields": .array([]),
        ])

        let store = EditorStore()
        let queuedTrim = OptimisticTimeline.trimSegment(id: "b", length: 20)
        store.authoritativeSnapshot = base
        store.pendingPredictions = [(id: UUID(), change: queuedTrim)]
        store.snapshot = OptimisticTimeline.apply(queuedTrim, to: base)

        XCTAssertEqual(store.snapshot?.nodes.first(where: { $0.id == "c" })?.startFrame, 50)
        XCTAssertTrue(store.reconcileApplyResponse(delta, baseDocumentId: "doc", baseRevisions: revisions))
        XCTAssertEqual(store.authoritativeSnapshot?.nodes.first(where: { $0.id == "c" })?.startFrame, 60)
        XCTAssertEqual(store.snapshot?.nodes.first(where: { $0.id == "b" })?.endFrame, 50)
        XCTAssertEqual(store.snapshot?.nodes.first(where: { $0.id == "c" })?.startFrame, 50)
    }

    func testSearchHitCarriesSourceSelection() {
        let hit = SearchHit(
            id: "asset-1-4", title: "Cornering", evidence: "rider exits a bend",
            assetId: "asset-1", sourceIn: 4.2, sourceOut: 7.8, related: true,
            raw: .object(["moment": .string("cornering")])
        )
        XCTAssertEqual(hit.sourceOut - hit.sourceIn, 3.6, accuracy: 0.0001)
        XCTAssertEqual(hit.raw["moment"]?.stringValue, "cornering")
    }

    func testGeneratedCaptionTimingIsReadOnlyButGeneratedSFXCanMove() {
        let caption = EditorNode(
            id: "caption-1", kind: "caption", lane: "overlay", parentId: "segment-1",
            startFrame: 0, endFrame: 30, frames: 30, raw: .object([:]),
            assetId: nil, sourceIn: nil, sourceOut: nil,
            assetKind: nil, generated: true, readOnly: false
        )
        let sfx = EditorNode(
            id: "sfx-1", kind: "generated_sfx", lane: "audio", parentId: nil,
            startFrame: 0, endFrame: 10, frames: 10, raw: .object([:]),
            assetId: nil, sourceIn: nil, sourceOut: nil,
            assetKind: nil, generated: true, readOnly: false
        )
        XCTAssertTrue(caption.isReadOnly)
        XCTAssertFalse(sfx.isReadOnly)
        XCTAssertTrue(NativeMediaKind.isPhoto(kind: "image/heic", path: "ride.heic"))
        XCTAssertFalse(NativeMediaKind.isPhoto(kind: "video/mp4", path: "ride.mp4"))
    }

    func testParentedCaptionsAreNotMistakenForBRoll() {
        var page = EditorNode(
            id: "caption-page", kind: "caption_page", lane: "captions", parentId: "voice",
            startFrame: 0, endFrame: 90, frames: 90,
            raw: .object(["words": .array([.object(["text": .string("easy ride")])])]),
            assetId: "original", sourceIn: nil, sourceOut: nil,
            assetKind: nil, generated: true, readOnly: true)
        XCTAssertEqual(TimelineLaneClassifier.category(for: page), "Captions")
        XCTAssertEqual(page.title, "easy ride")
        XCTAssertTrue(TimelineLaneClassifier.visibleInTimeline(page))
        page.kind = "caption_word"
        XCTAssertFalse(TimelineLaneClassifier.visibleInTimeline(page))
        page.kind = "cutaway"
        page.lane = "cutaway"
        XCTAssertEqual(TimelineLaneClassifier.category(for: page), "B-roll")
    }

    func testSemanticEnrichmentKeepsRegexIdentityAndAddsSourceRangeChildren() throws {
        let regexRow: JSONValue = .object([
            "id": .string("regex-row-1"),
            "assetId": .string("asset-1"),
            "title": .string("Laundry"),
            "sourceIn": .number(2),
            "sourceOut": .number(4),
            "evidence": .string("washing the kit"),
            "highlights": .array([.object(["start": .number(0), "end": .number(7)])])
        ])
        let relatedRange: JSONValue = .object([
            "assetId": .string("asset-1"),
            "sourceIn": .number(5),
            "sourceOut": .number(7),
            "evidence": .string("clothes on the line"),
            "reason": .string("The laundry follows the wash."),
            "highlights": .array([.object(["start": .number(0), "end": .number(7)])])
        ])
        let enrichedRow: JSONValue = .object([
            "id": .string("regex-row-1"),
            "assetId": .string("asset-1"),
            "title": .string("Laundry"),
            "sourceIn": .number(2),
            "sourceOut": .number(4),
            "evidence": .string("washing the kit"),
            "related": .object(["reason": .string("The laundry follows the wash.")]),
            "sourceRanges": .array([relatedRange])
        ])
        let base = EditorStore.hits(
            from: .object(["regexRows": .array([regexRow])]),
            related: false,
            keys: ["regexRows"]
        )
        let semantic: JSONValue = .object([
            "relatedRows": .array([]),
            "enrichedRows": .array([enrichedRow])
        ])
        let semanticHits = EditorStore.hits(
            from: semantic, related: true, keys: ["relatedRows", "enrichedRows"]
        )
        let merged = EditorStore.merge(base, related: semanticHits)

        let row = try XCTUnwrap(merged.first)
        XCTAssertEqual(merged.count, 1)
        XCTAssertEqual(row.id, "regex-row-1")
        XCTAssertEqual(row.sourceIn, 2)
        XCTAssertFalse(row.related)
        XCTAssertEqual(row.relatedReason, "The laundry follows the wash.")
        XCTAssertEqual(row.sourceRanges.count, 1)
        XCTAssertEqual(row.sourceRanges[0].sourceIn, 5)
        XCTAssertEqual(row.raw["highlights"]?.arrayValue?.count, 1)
    }

    func testUnicodeCodepointHighlightOffsetsMapToUTF16Safely() throws {
        let text = "👩‍🚴 café"
        let range = try XCTUnwrap(UnicodeCodepointOffsets.utf16Range(in: text, start: 4, end: 8))
        XCTAssertEqual(range, NSRange(location: 6, length: 4))
        XCTAssertNil(UnicodeCodepointOffsets.stringRange(in: text, start: -1, end: 2))
        XCTAssertNil(UnicodeCodepointOffsets.stringRange(in: text, start: 8, end: 4))
    }

    func testParentedVoiceTrackUsesAudioLaneInsteadOfBRoll() {
        let voice = EditorNode(
            id: "voice-1", kind: "audio", lane: "voice-over", parentId: "segment-1",
            startFrame: 0, endFrame: 90, frames: 90, raw: .object([:]),
            assetId: "asset-1", sourceIn: 0, sourceOut: 3,
            assetKind: "audio", generated: false, readOnly: false
        )
        let cutaway = EditorNode(
            id: "cutaway-1", kind: "cutaway", lane: "b-roll", parentId: "segment-1",
            startFrame: 0, endFrame: 90, frames: 90, raw: .object([:]),
            assetId: "asset-2", sourceIn: 0, sourceOut: 3,
            assetKind: "video", generated: false, readOnly: false
        )

        XCTAssertEqual(TimelineLaneClassifier.category(for: voice), "Audio")
        XCTAssertEqual(TimelineLaneClassifier.category(for: cutaway), "B-roll")
    }
    @MainActor
    func testTimelineScrubSeeksBeforeReleaseAndIgnoresPreviewFeedback() throws {
        let store = EditorStore()
        store.snapshot = try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("ride"),
            "revisions": .object([
                "document": .string("d1"), "library": .string("l1"), "renderer": .string("r1")
            ]),
            "plan": .object([:]), "nodes": .array([]),
            "fps": .number(30), "durationFrames": .number(90),
            "warnings": .array([]), "preview": .object([:])
        ])))
        let initialGeneration = store.seekGeneration

        store.setPlayhead(20, final: false)
        XCTAssertEqual(store.playheadFrame, 20)
        XCTAssertEqual(store.seekGeneration, initialGeneration + 1)
        store.setPlayhead(60, final: false)
        XCTAssertEqual(store.playheadFrame, 60)
        XCTAssertEqual(store.seekGeneration, initialGeneration + 2)

        // A slower decoder must not pull the playhead away from the pointer.
        store.updatePlayheadFromPreview(20, revision: "d1")
        XCTAssertEqual(store.playheadFrame, 60)
        store.setPlayhead(30, final: false)
        XCTAssertEqual(store.playheadFrame, 30)
        XCTAssertEqual(store.seekGeneration, initialGeneration + 3)
        store.setPlayhead(-10, final: false)
        XCTAssertEqual(store.playheadFrame, 0)
        store.setPlayhead(100, final: false)
        XCTAssertEqual(store.playheadFrame, 90)

        store.setPlayhead(45)
        XCTAssertEqual(store.playheadFrame, 45)
        XCTAssertEqual(store.seekGeneration, initialGeneration + 6)
        store.updatePlayheadFromPreview(46, revision: "d1")
        XCTAssertEqual(store.playheadFrame, 46, "Playback resumes driving the playhead after release")
        XCTAssertEqual(store.seekGeneration, initialGeneration + 6, "Playback feedback must not trigger a seek loop")
    }
}
