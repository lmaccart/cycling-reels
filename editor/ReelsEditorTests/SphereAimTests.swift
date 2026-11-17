import XCTest
import simd
@testable import ReelsEditor

@MainActor
final class SphereAimTests: XCTestCase {
    private func cameraJSON(path: [JSONValue] = [], stabilize: JSONValue = .null) -> JSONValue {
        .object([
            "assetId": .string("a360"), "in": .number(10), "out": .number(16), "speed": .number(1),
            "seconds": .number(6), "projection": .string("flat"),
            "view": .object(["yaw": .number(90), "pitch": .number(25), "fov": .number(100), "roll": .number(0)]),
            "path": .array(path), "stabilize": stabilize,
            "angles": .object(["side": .object(["yaw": .number(160), "pitch": .number(25), "fov": .number(100), "roll": .number(90)])]),
        ])
    }

    private func key(_ at: Double, _ yaw: Double, cut: Bool = false) -> JSONValue {
        var item: [String: JSONValue] = ["at": .number(at), "yaw": .number(yaw), "pitch": .number(0),
                                         "fov": .number(90), "roll": .number(0)]
        if cut { item["cut"] = .bool(true) }
        return .object(item)
    }

    private func model(_ json: JSONValue) -> SphereAimModel {
        let model = SphereAimModel()
        model.testingSetCamera(SphereAimModel.parseCamera(json, nodeID: "n1"))
        return model
    }

    private func ops(_ list: [JSONValue]) -> [String: JSONValue] {
        Dictionary(list.compactMap { op in op["path"]?.stringValue.map { ($0, op["value"] ?? .null) } },
                   uniquingKeysWith: { _, b in b })
    }

    func testParsesCameraKeysAnglesAndStabilize() throws {
        let camera = try XCTUnwrap(SphereAimModel.parseCamera(
            cameraJSON(path: [key(2, 10, cut: true), key(0, 0)], stabilize: .string("strong")), nodeID: "n1"))
        XCTAssertEqual(camera.keys.map(\.at), [0, 2])
        XCTAssertEqual(camera.keys.map(\.cut), [false, true])
        XCTAssertEqual(camera.angles.map(\.name), ["side"])
        XCTAssertEqual(camera.stabilize, "strong")
        XCTAssertEqual(SphereAimModel.stabilizeLabel(.bool(true)), "medium")
        XCTAssertEqual(SphereAimModel.stabilizeLabel(.number(1.2)), "custom")
        XCTAssertEqual(SphereAimModel.stabilizeLabel(nil), "off")
    }

    func testAimWithoutKeyframesSetsTheClipView() {
        let m = model(cameraJSON())
        let raw: JSONValue = .object(["asset": .string("a360"), "yaw": .number(5)])
        let result = ops(m.operationsForAim(SphereView(yaw: 200, pitch: 10, fov: 80, roll: 0), atLocal: 3, fps: 30, raw: raw))
        XCTAssertEqual(result["/yaw"], .null)  // top-level override cleared
        XCTAssertEqual(result["/view"]?["yaw"]?.numberValue, -160)  // wrapped
        XCTAssertNil(result["/path"])
    }

    func testFirstKeyframeStartsAPathFromTheClipView() {
        let m = model(cameraJSON())
        m.draft = SphereView(yaw: 0, pitch: 0, fov: 90, roll: 0)
        let path = ops(m.operationsForAddKey(atLocal: 2, fps: 30, raw: nil))["/path"]?.arrayValue ?? []
        XCTAssertEqual(path.compactMap { $0["at"]?.numberValue }, [0, 2])
        XCTAssertEqual(path.first?["yaw"]?.numberValue, 90)
        XCTAssertEqual(path.last?["yaw"]?.numberValue, 0)
    }

    func testAimingBetweenKeyframesAddsOneAndOnTopUpdatesIt() {
        let m = model(cameraJSON(path: [key(0, 0), key(4, 40)]))
        var path = ops(m.operationsForAim(SphereView(yaw: 20, pitch: 5, fov: 90, roll: 0), atLocal: 2, fps: 30, raw: nil))["/path"]?.arrayValue ?? []
        XCTAssertEqual(path.compactMap { $0["at"]?.numberValue }, [0, 2, 4])
        path = ops(m.operationsForAim(SphereView(yaw: 25, pitch: 5, fov: 90, roll: 0), atLocal: 2.01, fps: 30, raw: nil))["/path"]?.arrayValue ?? []
        XCTAssertEqual(path.count, 3)
        XCTAssertEqual(path[1]["yaw"]?.numberValue, 25)
        // too close to an existing keyframe (renderer needs 0.1 s) and not on it: no edit
        XCTAssertTrue(m.operationsForAim(SphereView(yaw: 1, pitch: 0, fov: 90, roll: 0), atLocal: 2.06, fps: 30, raw: nil).isEmpty)
    }

    func testHardCutToggleAndDeleteBackToASingleView() {
        let m = model(cameraJSON(path: [key(0, 0), key(3, 90)]))
        let path = ops(m.operationsForToggleCut(atLocal: 3, fps: 30, raw: nil))["/path"]?.arrayValue ?? []
        XCTAssertEqual(path.last?["cut"], .bool(true))
        XCTAssertNil(path.first?["cut"])  // the first keyframe never cuts
        let deleted = ops(m.operationsForDeleteKey(atLocal: 3, fps: 30, raw: nil))
        XCTAssertEqual(deleted["/path"], .null)
        XCTAssertEqual(deleted["/view"]?["yaw"]?.numberValue, 0)
    }

    func testPlaybackViewFollowsKeyframesAndStabilization() {
        let m = model(cameraJSON(path: [key(0, 0), key(1, 90, cut: true)]))
        XCTAssertEqual(m.view(atLocal: 0.5).yaw, 0, accuracy: 1e-9)
        XCTAssertEqual(m.view(atLocal: 1.0).yaw, 90, accuracy: 1e-9)
        XCTAssertEqual(SphereMath.element(m.stabilization(atSource: 12), 0, 0), 1, accuracy: 1e-12)  // no proxy: identity
    }

    func testSplitPlaybackKeepsOriginalIntervalAndBoundaryCut() throws {
        for cut in [false, true] {
            let original = [key(0, 0), key(4, 90, cut: cut)]
            let full = try XCTUnwrap(SphereAimModel.parseCamera(cameraJSON(path: original), nodeID: "full"))
            let start = SphereMath.pathValue(full.keys, at: 1)
            let json = cameraJSON(path: [key(0, start.yaw), key(3, 90, cut: cut)])
                .setting(.array(original), at: ["playbackPath"])
                .setting(.number(1), at: ["playbackOffsetSeconds"])
                .setting(.number(3), at: ["seconds"])
            let m = model(json)
            for n in 0...90 {
                let t = Double(n) / 30
                XCTAssertEqual(m.view(atLocal: t).yaw, SphereMath.pathValue(full.keys, at: t + 1).yaw, accuracy: 1e-9)
            }
            _ = m.operationsForAim(SphereView(yaw: 50, pitch: 0, fov: 90, roll: 0), atLocal: 1, fps: 30, raw: nil)
            XCTAssertEqual(m.view(atLocal: 1).yaw, 50, accuracy: 1e-9)
        }
    }

    func testTinyPlanetPlaybackUsesProjectedPath() {
        let original: [JSONValue] = [
            .object(["at": .number(0), "yaw": .number(0), "pitch": .number(90), "roll": .number(0), "fov": .number(90)]),
            .object(["at": .number(2), "yaw": .number(90), "pitch": .number(90), "roll": .number(90), "fov": .number(90)]),
        ]
        let m = model(cameraJSON(path: original).setting(.string("tiny-planet"), at: ["projection"]))
        let expected = SphereMath.v360Matrix(yaw: 90, pitch: -90, roll: 0)
        let actual = SphereMath.matrix(for: m.view(atLocal: 1))
        for c in 0..<3 { XCTAssertEqual(simd_length(actual[c] - expected[c]), 0, accuracy: 1e-9) }
    }

    func testStabilizeAndProjectionOperations() {
        let m = model(cameraJSON())
        XCTAssertEqual(ops(m.operationsForStabilize("lock"))["/stabilize"], .string("lock"))
        XCTAssertEqual(ops(m.operationsForStabilize("off"))["/stabilize"], .null)
        XCTAssertEqual(ops(m.operationsForProjection(.tinyPlanet))["/projection"], .string("tiny-planet"))
    }

    func testCustomStabilizeSendsThePlansOwnNumber() throws {
        let custom = try XCTUnwrap(SphereAimModel.parseCamera(cameraJSON(stabilize: .number(1.2)), nodeID: "n1"))
        XCTAssertEqual(custom.stabilize, "custom")
        XCTAssertEqual(SphereAimModel.stabilizeParam(custom), .number(1.2))  // not "medium"
        let strong = try XCTUnwrap(SphereAimModel.parseCamera(cameraJSON(stabilize: .string("strong")), nodeID: "n1"))
        XCTAssertEqual(SphereAimModel.stabilizeParam(strong), .string("strong"))
        XCTAssertNil(SphereAimModel.stabilizeParam(try XCTUnwrap(SphereAimModel.parseCamera(cameraJSON(), nodeID: "n1"))))
    }

    func testWrittenKeysStayInsideTheClip() {
        // keys a split piece used to get from its camera window, before and after its 6 s
        let m = model(cameraJSON(path: [key(-1, 0), key(7, 80)]))
        let path = ops(m.operationsForAim(SphereView(yaw: 30, pitch: 0, fov: 90, roll: 0), atLocal: 3, fps: 30, raw: nil))["/path"]?.arrayValue ?? []
        XCTAssertEqual(path.compactMap { $0["at"]?.numberValue }, [0, 3, 6])
    }

    private func node(_ id: String, _ kind: String, _ asset: String, _ start: Int, _ end: Int) -> EditorNode {
        EditorNode(id: id, kind: kind, lane: kind == "cutaway" ? "b-roll" : "primary", parentId: nil,
                   startFrame: start, endFrame: end, frames: end - start, raw: .object([:]),
                   assetId: asset, sourceIn: 0, sourceOut: Double(end - start) / 30,
                   assetKind: "video", generated: false, readOnly: false)
    }

    func testAimFollowsTheSelectionElseThePlayhead() {
        let library: [JSONValue] = [
            .object(["id": .string("a360"), "kind": .string("video"), "probe": .object(["sphere": .object([:])])]),
            .object(["id": .string("flat"), "kind": .string("video"), "probe": .object([:])]),
        ]
        // left and right pieces of a cut 360 clip, a flat clip, and a 360 cutaway over the right piece
        let nodes = [node("left", "segment", "a360", 0, 90), node("right", "segment", "a360", 90, 180),
                     node("flat", "segment", "flat", 180, 270), node("over", "cutaway", "a360", 100, 130)]
        func target(_ selected: Set<String>, _ frame: Int, _ current: String? = nil) -> String? {
            EditorStore.sphereAimTarget(nodes: nodes, selectedIDs: selected, playheadFrame: frame,
                                        library: library, current: current)?.id
        }
        XCTAssertEqual(target(["left"], 150), "left")           // the selected 360 clip, wherever the playhead is
        XCTAssertNil(target(["flat"], 150))                     // a non-360 selection leaves the panel alone
        XCTAssertEqual(target([], 90, "left"), "right")         // after a cut: the piece under the playhead
        XCTAssertEqual(target([], 110), "over")                 // the cutaway is the picture there
        XCTAssertEqual(target([], 110, "right"), "right")       // but playing through it keeps the current clip
        XCTAssertNil(target([], 200))
    }

    private func snapshot(_ nodes: [EditorNode], revision: String = "d") throws -> EditorSnapshot {
        let rawNodes = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(nodes))
        return try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("split-doc"),
            "revisions": .object(["document": .string(revision), "library": .string("l"), "renderer": .string("r")]),
            "plan": .object([:]), "nodes": rawNodes, "fps": .number(30),
            "durationFrames": .number(Double(nodes.map(\.endFrame).max() ?? 0)),
            "warnings": .array([]), "preview": .object([:]),
        ])))
    }

    func testAimEditRefreshKeepsEmptySelectionAndLaterClip() throws {
        let store = EditorStore()
        store.library = [.object(["id": .string("a360"), "kind": .string("video"),
                                   "probe": .object(["sphere": .object([:])])])]
        let first = node("first", "segment", "a360", 0, 90)
        var later = node("later", "segment", "a360", 90, 270)
        store.acceptRefreshedSnapshot(try snapshot([first, later]))
        XCTAssertEqual(store.selectedIDs, ["first"])  // initial document selection remains
        store.selectedIDs = []
        store.playheadFrame = 135
        store.sphereAim.isActive = true
        store.sphereAim.boundNodeID = "later"
        store.sphereAim.testingSetCamera(try XCTUnwrap(SphereAimModel.parseCamera(cameraJSON(), nodeID: "later")))
        let edits = store.sphereAim.operationsForAim(SphereView(yaw: 35, pitch: 10, fov: 80, roll: 0),
                                                   atLocal: 1.5, fps: 30, raw: later.raw)
        XCTAssertEqual(edits.last?["id"]?.stringValue, "later")
        later.raw = later.raw.setting(try XCTUnwrap(ops(edits)["/view"]), at: ["view"])
        store.acceptRefreshedSnapshot(try snapshot([first, later], revision: "aim-edit"))
        XCTAssertTrue(store.selectedIDs.isEmpty)
        XCTAssertFalse(store.followSphereAim())
        XCTAssertEqual(store.sphereAim.boundNodeID, "later")
        XCTAssertEqual(store.sphereAim.node(in: store)?.raw["view"]?["yaw"]?.numberValue, 35)
        XCTAssertEqual(store.playheadFrame, 135)
        store.sphereAim.close()
    }

    func testSelectedClipSplitMovesSelectionAndAimToRightPiece() throws {
        let store = EditorStore()
        store.library = [.object(["id": .string("a360"), "kind": .string("video"),
                                   "probe": .object(["sphere": .object([:])])])]
        let merged = try snapshot([node("earlier", "segment", "a360", 0, 90),
                                   node("selected", "segment", "a360", 90, 270)])
        let split = try snapshot([node("earlier", "segment", "a360", 0, 90),
                                  node("selected", "segment", "a360", 90, 180),
                                  node("new-right", "segment", "a360", 180, 270)], revision: "split")
        store.snapshot = merged
        store.selectedIDs = ["selected"]
        store.playheadFrame = 180
        store.sphereAim.isActive = true
        store.sphereAim.boundNodeID = "selected"
        // The ordinary split response keeps the selected ID on the left.
        store.snapshot = split
        XCTAssertEqual(store.selectedIDs, ["new-right"])
        XCTAssertEqual(store.sphereAim.boundNodeID, "new-right")
        XCTAssertEqual(store.playheadFrame, 180)
        // Undo removes the right ID and restores the merged clip, not the first clip.
        store.snapshot = merged
        XCTAssertEqual(store.selectedIDs, ["selected"])
        XCTAssertEqual(store.sphereAim.boundNodeID, "selected")
        XCTAssertEqual(store.playheadFrame, 180)
        // A refresh after undo must not substitute a default selection.
        store.acceptRefreshedSnapshot(merged)
        XCTAssertEqual(store.selectedIDs, ["selected"])
        // Redo retains the left ID again; the selected piece follows the playhead.
        store.snapshot = split
        XCTAssertEqual(store.selectedIDs, ["new-right"])
        XCTAssertEqual(store.sphereAim.boundNodeID, "new-right")
        XCTAssertEqual(store.playheadFrame, 180)
        store.sphereAim.close()
    }

    func testSplitUndoRedoSelectionReachesServiceAndTerminalContext() async throws {
        let code = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        let python = ProcessInfo.processInfo.environment["REELS_TEST_PYTHON"]
            ?? code.appending(path: ".venv/bin/python").path
        guard FileManager.default.isExecutableFile(atPath: python) else {
            throw XCTSkip("Needs the repository Python environment")
        }
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        // Real Python service and compiler; placeholder media is fingerprinted, never decoded.
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
        (root / "remotion").mkdir(exist_ok=True)
        media = project.media_dir / "pano.mp4"
        media.write_bytes(b"selection-test" * 64)
        save_asset(project, {"id": "pano", "kind": "video", "media": "library/media/pano.mp4",
            "hash": quick_hash(media), "status": "tagged", "tags": {},
            "probe": {"duration": 6, "fps": 30, "width": 512, "height": 256,
                      "hasAudio": False, "sphere": {"projection": "equirect", "width": 512, "height": 256}}})
        write_json(project.plans_dir / "selection.json",
                   {"slug": "selection", "autoSfx": False, "segments": [{"asset": "pano", "in": 0, "out": 6}]})
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
        defer { store.sphereAim.close(); store.service.stopSynchronously() }
        let opened = try await store.service.call("document.open", params: .object(["path": .string("plans/selection.json")]))
        store.snapshot = try XCTUnwrap(EditorSnapshot.decode(opened))
        store.library = [.object(["id": .string("pano"), "kind": .string("video"),
                                   "probe": .object(["sphere": .object([:])])])]
        let leftID = try XCTUnwrap(store.snapshot?.nodes.first(where: { $0.kind == "segment" })?.id)
        store.playheadFrame = 90
        store.setSelection([leftID])

        func assertServiceSelection(_ id: String) async throws {
            // Wait for the normal asynchronous selection/inspector pathway, with
            // an observable service-state condition rather than a fixed sleep.
            var selection: JSONValue = .null
            var context: JSONValue = .null
            let deadline = Date().addingTimeInterval(5)
            repeat {
                await Task.yield()
                selection = try await store.service.call("selection.get")
                let data = try Data(contentsOf: root.appending(path: ".cache/editor/live/context.json"))
                context = try JSONDecoder().decode(JSONValue.self, from: data)
                if selection["ids"] == .array([.string(id)]),
                   context["selection"]?.arrayValue?.first?["id"]?.stringValue == id { break }
                try await Task.sleep(for: .milliseconds(10))
            } while Date() < deadline
            XCTAssertEqual(selection["ids"], .array([.string(id)]))
            XCTAssertEqual(selection["playhead"]?.numberValue, 90)
            XCTAssertEqual(context["selection"]?.arrayValue?.first?["id"]?.stringValue, id)
            XCTAssertEqual(context["playheadSeconds"]?.numberValue, 3)
        }
        try await assertServiceSelection(leftID)
        var result = try await store.service.call("document.apply", params: .object([
            "expected": opened["revisions"]!, "origin": .string("manual"), "reason": .string("split"),
            "operations": .array([.object(["op": .string("split_segment"), "id": .string(leftID), "atFrame": .number(90)])])
        ]))
        var rightID: String?
        for action in ["split", "document.undo", "document.redo"] {
            if action != "split" {
                result = try await store.service.call(action, params: .object(["expected": result["revisions"]!]))
            }
            store.sphereAim.isActive = true
            store.sphereAim.boundNodeID = store.selectedIDs.first
            store.acceptRefreshedSnapshot(try XCTUnwrap(EditorSnapshot.decode(result)))
            if action == "split" {
                rightID = store.snapshot?.nodes.first(where: { $0.kind == "segment" && $0.startFrame == 90 })?.id
            }
            let expected = action == "document.undo" ? leftID : try XCTUnwrap(rightID)
            XCTAssertEqual(store.selectedIDs, [expected])
            XCTAssertEqual(store.sphereAim.boundNodeID, expected)
            XCTAssertEqual(store.playheadFrame, 90)
            store.sphereAim.close() // no proxy job: this regression exercises selection, not decoding
            try await assertServiceSelection(expected)
        }
    }
}
