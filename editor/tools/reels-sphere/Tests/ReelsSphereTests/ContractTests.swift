import CoreMedia
import simd
import Foundation
import XCTest
@testable import ReelsSphere

final class ContractTests: XCTestCase {
    private func request(_ changes: [String: Any] = [:]) throws -> RenderRequest {
        var values: [String: Any] = [
            "version": 1, "source": #filePath, "destination": "/tmp/sphere-\(UUID()).mp4",
            "sourceProjection": "equirect", "trackIDs": [1], "sourceIn": 10.125, "sourceOut": 11.125,
            "speed": 1.25, "width": 320, "height": 568, "fps": 30, "frames": 24,
            "projection": "flat", "keys": [["at": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 75]],
            "fullRange": true, "codec": "h264",
        ]
        values.merge(changes) { _, new in new }
        return try JSONDecoder().decode(RenderRequest.self, from: JSONSerialization.data(withJSONObject: values))
    }

    private func proxy(_ changes: [String: Any] = [:]) throws -> RenderRequest {
        var values: [String: Any] = [
            "preset": "proxy", "projection": "equirect", "width": 2048, "height": 1024,
            "speed": 1, "frames": 30, "keys": [], "maxOutputBytes": 20_000_000,
        ]
        values.merge(changes) { _, new in new }
        return try request(values)
    }

    func testLegacyRenderPresetRemainsOptional() throws {
        let old = try request()
        try old.validate()
        XCTAssertFalse(old.isProxy)
        XCTAssertEqual(old.presetName, "render")
        try request(["preset": "render"]).validate()
        XCTAssertThrowsError(try request(["preset": "unknown"]).validate())
        XCTAssertThrowsError(try request(["views": []]).validate())
        XCTAssertThrowsError(try request(["maxOutputBytes": 1]).validate())
    }

    func testWholeSphereProxyContractAndIdentity() throws {
        let r = try proxy()
        try r.validate()
        XCTAssertTrue(r.isProxy)
        for frame in [0, 29] { for column in 0..<3 {
            XCTAssertEqual(r.matrix(frame: frame, keys: []).0[column], matrix_identity_double3x3[column])
        } }
        try proxy(["sourceOut": 190.125, "frames": 5400]).validate()
        for change: [String: Any] in [
            ["speed": 1.25], ["fps": 60], ["width": 2046], ["height": 1026],
            ["maxOutputBytes": 0], ["maxOutputBytes": -1], ["maxOutputBytes": NSNull()],
            ["sourceOut": 190.126], ["views": []], ["projection": "flat"],
            ["corrections": ["sourceStart": 10.125, "sourceStep": 1.0/30,
                             "quaternions": Array(repeating: [1.0,0,0,0], count: 30)]],
        ] { XCTAssertThrowsError(try proxy(change).validate()) }
    }

    func testProofReadbackIsProxyOnlyBoundedAndCannotOverwrite() throws {
        let r = try proxy(["proofSampleFrames": [0, 29]])
        try r.validate()
        XCTAssertEqual(r.proofSampleURL(frame: 29).path, r.destination + ".proof-29.nv12")
        for frames in [[], [-1], [30], [1, 1], [2, 1], Array(0..<9)] {
            XCTAssertThrowsError(try proxy(["proofSampleFrames": frames]).validate())
        }
        XCTAssertThrowsError(try request(["proofSampleFrames": [0]]).validate())
        let existing = r.proofSampleURL(frame: 0)
        try Data([0]).write(to: existing)
        defer { try? FileManager.default.removeItem(at: existing) }
        XCTAssertThrowsError(try r.validate())
        XCTAssertEqual(try Data(contentsOf: existing), Data([0]))
    }

    func testFrontBackUsesEachResolvedAimAndPortraitAspect() throws {
        let views: [[String: Any]] = [
            ["at": 0, "yaw": 37, "pitch": -13, "roll": 9, "fov": 82],
            ["at": 0, "yaw": -145, "pitch": 12, "roll": -8, "fov": 91],
        ]
        let changes: [String: Any] = ["projection": "front-back", "width": 1440, "height": 1280, "views": views]
        let r = try proxy(changes)
        try r.validate()
        XCTAssertEqual(r.viewWidth, 720)
        for view in r.views! {
            let portrait = try request(["width": 720, "height": 1280, "keys": [
                ["at": 0, "yaw": view.yaw, "pitch": view.pitch, "roll": view.roll, "fov": view.fov],
            ]])
            let expected = portrait.matrix(frame: 0, keys: portrait.keys.map(\.key))
            let actual = r.cameraMatrix(view: view.key.view)
            for column in 0..<3 { XCTAssertEqual(actual.0[column], expected.0[column]) }
            XCTAssertEqual(actual.1, expected.1, accuracy: 1e-14)
        }
        for badViews: Any in [[], [views[0]], views + [views[0]], [
            views[0].merging(["at": 1]) { _, new in new }, views[1],
        ], [views[0].merging(["pitch": 91]) { _, new in new }, views[1]],
            [views[0].merging(["cut": true]) { _, new in new }, views[1]]] {
            var bad = changes
            bad["views"] = badViews
            XCTAssertThrowsError(try proxy(bad).validate())
        }
        var packedAspect = changes
        packedAspect["width"] = 720
        XCTAssertThrowsError(try proxy(packedAspect).validate())
    }

    func testProxyTimingUsesTrimClockAndExactHalfTicks() throws {
        XCTAssertEqual(proxyFPSTick(pts: CMTime(value: 601, timescale: 60), sourceIn: 10), 1)
        XCTAssertEqual(proxyFPSTick(pts: CMTime(value: 603, timescale: 60), sourceIn: 10), 2)
        XCTAssertEqual(proxyFPSTick(pts: CMTime(value: 1001, timescale: 60000), sourceIn: 0), 1)
        XCTAssertEqual(proxyFPSTick(pts: CMTime(value: 2002, timescale: 60000), sourceIn: 0), 1)
        let r = try proxy(["sourceIn": 10, "sourceOut": 11])
        try r.validateProxyTiming(firstPTS: CMTime(value: 600, timescale: 60))
        XCTAssertThrowsError(try r.validateProxyTiming(firstPTS: CMTime(value: 601, timescale: 60)))
        let fractional = try proxy()
        try fractional.validateProxyTiming(firstPTS: CMTime(value: 608, timescale: 60))
        XCTAssertThrowsError(try proxy(["frames": 29]).validateProxyTiming(firstPTS: CMTime(value: 608, timescale: 60)))
    }

    func testRejectsInvalidIntervalsDimensionsAndCameraKeys() throws {
        for change: [String: Any] in [["sourceOut": 10.0], ["speed": 0], ["width": 321], ["fps": 0],
                                      ["trackIDs": [1, 1]], ["projection": "guess"], ["keys": []],
                                      ["keys": [["at": 0, "yaw": 0, "pitch": 91, "roll": 0, "fov": 75]]]] {
            XCTAssertThrowsError(try request(change).validate())
        }
    }

    func testRejectsCorrectionClockShiftAndMissingFrames() throws {
        let quats = Array(repeating: [1.0, 0, 0, 0], count: 24)
        let correct: [String: Any] = ["sourceStart": 10.125, "sourceStep": 1.25/30, "quaternions": quats]
        try request(["corrections": correct]).validate()
        for change: [String: Any] in [["sourceStart": 10], ["sourceStep": 1.0/30],
                                      ["quaternions": Array(quats.dropLast())], ["quaternions": Array(repeating: [0.0,0,0,0], count: 24)]] {
            var bad = correct
            bad.merge(change) { _, new in new }
            XCTAssertThrowsError(try request(["corrections": bad]).validate())
        }
    }

    func testFpsRoundNearUsesTrimmedFirstPTSNotSourceIn() {
        let first = 10.126783333333
        XCTAssertEqual(fpsTick(pts: first, first: first, speed: 1, fps: 30), 0)
        XCTAssertEqual(fpsTick(pts: first+1001.0/60000, first: first, speed: 1, fps: 30), 1)
        XCTAssertEqual(fpsTick(pts: first+2002.0/60000, first: first, speed: 1, fps: 30), 1)
        XCTAssertEqual(fpsTick(pts: first+1001.0/60000, first: first, speed: 2, fps: 30), 0)
        XCTAssertEqual(fpsTick(pts: 1.0/60, first: 0, speed: 1, fps: 30), 1)
    }

    func testSharedGoldenVectorsIncludingExteriorWindowKeys() throws {
        struct Fixture: Decodable {
            struct Case: Decodable { let name: String; let keys: [CameraKey]; let samples: [[Double]]; let projection: String? }
            let tolerance: Double
            let cases: [Case]
        }
        var root = URL(fileURLWithPath: #filePath)
        for _ in 0..<6 { root.deleteLastPathComponent() }
        let fixture = try JSONDecoder().decode(Fixture.self, from: Data(contentsOf: root.appendingPathComponent("tests/fixtures/sphere_path_golden.json")))
        for c in fixture.cases { for s in c.samples {
            let v = SphereMath.pathValue(c.keys.map(\.key), at: s[0], projection: c.projection == "tiny-planet" ? .tinyPlanet : .flat)
            XCTAssertEqual(SphereMath.wrapYaw(v.yaw-s[1]), 0, accuracy: fixture.tolerance, c.name)
            XCTAssertEqual(v.pitch, s[2], accuracy: fixture.tolerance, c.name)
            XCTAssertEqual(v.fov, s[3], accuracy: fixture.tolerance, c.name)
            XCTAssertEqual(SphereMath.wrapYaw(v.roll-s[4]), 0, accuracy: fixture.tolerance, c.name)
        } }
    }
}
