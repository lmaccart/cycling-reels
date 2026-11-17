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
