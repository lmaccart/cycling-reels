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

    func testStabilizeAndProjectionOperations() {
        let m = model(cameraJSON())
        XCTAssertEqual(ops(m.operationsForStabilize("lock"))["/stabilize"], .string("lock"))
        XCTAssertEqual(ops(m.operationsForStabilize("off"))["/stabilize"], .null)
        XCTAssertEqual(ops(m.operationsForProjection(.tinyPlanet))["/projection"], .string("tiny-planet"))
    }
}
