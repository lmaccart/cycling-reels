import XCTest
import simd
@testable import ReelsEditor

final class SphereMathTests: XCTestCase {
    private func assertMatrix(_ m: simd_double3x3, _ rows: [[Double]], file: StaticString = #filePath, line: UInt = #line) {
        for r in 0..<3 { for c in 0..<3 {
            XCTAssertEqual(m[c][r], rows[r][c], accuracy: 1e-6, "m[\(r)][\(c)]", file: file, line: line)
        } }
    }
    private func assertView(_ v: SphereView, _ y: Double, _ p: Double, _ f: Double, _ r: Double,
                            file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(v.yaw, y, accuracy: 1e-6, file: file, line: line)
        XCTAssertEqual(v.pitch, p, accuracy: 1e-6, file: file, line: line)
        XCTAssertEqual(v.fov, f, accuracy: 1e-6, file: file, line: line)
        XCTAssertEqual(v.roll, r, accuracy: 1e-6, file: file, line: line)
    }
    private func assertVec(_ a: simd_double3, _ b: simd_double3, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(simd_length(a - b), 0, accuracy: 1e-6, "\(a) vs \(b)", file: file, line: line)
    }

    func testV360MatrixReferences() {
        assertMatrix(SphereMath.v360Matrix(yaw: 30, pitch: 20, roll: 10),
                     [[0.882564119, 0.018028311, 0.46984631], [0.163175911, 0.925416578, -0.342020143], [-0.440969611, 0.378522306, 0.813797681]])
        assertMatrix(SphereMath.v360Matrix(yaw: 160, pitch: 25, roll: 90),
                     [[0.144543958, 0.939692621, 0.309975519], [0.906307787, 0.0, -0.422618262], [-0.397131262, 0.342020143, -0.85165074]])
    }

    func testQuaternionMatrix() {
        assertMatrix(SphereMath.matrix(quaternion: 0.9, x: 0.1, y: -0.3, z: 0.2),
                     [[0.726315789, -0.442105263, -0.526315789], [0.315789474, 0.894736842, -0.315789474], [0.610526316, 0.063157895, 0.789473684]])
    }

    func testAnglesRoundTrip() {
        for (y, p, r) in [(30.0, 20.0, 10.0), (-120, -45, 170), (0, 0, 0), (179, 60, -30)] {
            let a = SphereMath.v360Angles(SphereMath.v360Matrix(yaw: y, pitch: p, roll: r))
            XCTAssertEqual(a.yaw, y, accuracy: 1e-6); XCTAssertEqual(a.pitch, p, accuracy: 1e-6)
            XCTAssertEqual(a.roll, r, accuracy: 1e-6)
        }
        for p in [90.0, -90.0] {
            let m = SphereMath.v360Matrix(yaw: 40, pitch: p, roll: 0)
            let a = SphereMath.v360Angles(m)
            XCTAssertEqual(a.pitch, p, accuracy: 1e-4)
            let m2 = SphereMath.v360Matrix(yaw: a.yaw, pitch: a.pitch, roll: a.roll)
            XCTAssertEqual(simd_length(m2.columns.0 - m.columns.0) + simd_length(m2.columns.1 - m.columns.1)
                           + simd_length(m2.columns.2 - m.columns.2), 0, accuracy: 1e-6)
        }
    }

    func testWrapAndEase() {
        XCTAssertEqual(SphereMath.wrapYaw(180), -180)
        XCTAssertEqual(SphereMath.wrapYaw(-190), 170, accuracy: 1e-9)
        XCTAssertEqual(SphereMath.wrapYaw(540), -180)
        XCTAssertEqual(SphereMath.ease(-1), 0); XCTAssertEqual(SphereMath.ease(2), 1)
        XCTAssertEqual(SphereMath.ease(0.5), 0.5, accuracy: 1e-12)
    }

    func testPathValue() {
        let keys = [
            SphereKey(at: 0, view: SphereView(yaw: 170, pitch: 0, fov: 90, roll: 0), cut: false),
            SphereKey(at: 2, view: SphereView(yaw: -170, pitch: 20, fov: 110, roll: 10), cut: false),
            SphereKey(at: 3, view: SphereView(yaw: 0, pitch: 0, fov: 90, roll: 0), cut: true),
        ]
        assertView(SphereMath.pathValue(keys, at: -1), 170, 0, 90, 0)
        assertView(SphereMath.pathValue(keys, at: 0), 170, 0, 90, 0)
        let mid = SphereMath.pathValue(keys, at: 1)
        XCTAssertEqual(abs(SphereMath.wrapYaw(mid.yaw - 180)), 0, accuracy: 3)  // across ±180, not through 0
        XCTAssertEqual(mid.fov, 100, accuracy: 1e-9)  // fov eases linearly
        assertView(SphereMath.pathValue(keys, at: 2), -170, 20, 110, 10)
        assertView(SphereMath.pathValue(keys, at: 2.5), -170, 20, 110, 10)
        assertView(SphereMath.pathValue(keys, at: 2.999), -170, 20, 110, 10)
        assertView(SphereMath.pathValue(keys, at: 3), 0, 0, 90, 0)
        assertView(SphereMath.pathValue(keys, at: 4), 0, 0, 90, 0)
    }

    // MARK: - Golden vectors shared with tests/test_editor_sphere.py

    private struct GoldenCase {
        var name: String
        var keys: [SphereKey]
        var samples: [[Double]]
        var arcs: [(from: Int, to: Int, degrees: Double)]
    }

    private func golden() throws -> (tolerance: Double, cases: [GoldenCase]) {
        let url = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appending(path: "tests/fixtures/sphere_path_golden.json")
        let root = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: Any])
        let cases = try XCTUnwrap(root["cases"] as? [[String: Any]]).map { item -> GoldenCase in
            let keys = (item["keys"] as? [[String: Any]] ?? []).map { k in
                SphereKey(at: k["at"] as? Double ?? 0,
                          view: SphereView(yaw: k["yaw"] as? Double ?? 0, pitch: k["pitch"] as? Double ?? 0,
                                           fov: k["fov"] as? Double ?? 0, roll: k["roll"] as? Double ?? 0),
                          cut: k["cut"] as? Bool ?? false)
            }
            let arcs = (item["arcs"] as? [[String: Any]] ?? []).map {
                (from: $0["from"] as? Int ?? 0, to: $0["to"] as? Int ?? 0, degrees: $0["degrees"] as? Double ?? 0)
            }
            return GoldenCase(name: item["name"] as? String ?? "", keys: keys,
                              samples: item["samples"] as? [[Double]] ?? [], arcs: arcs)
        }
        XCTAssertGreaterThanOrEqual(cases.count, 5)
        return (try XCTUnwrap(root["tolerance"] as? Double), cases)
    }

    private func wrapped(_ a: Double, _ b: Double) -> Double { abs(SphereMath.wrapYaw(a - b)) }

    /// Degrees of rotation between two rotation matrices.
    private func turn(_ a: simd_double3x3, _ b: simd_double3x3) -> Double {
        let m = a.transpose * b
        let trace = m[0][0] + m[1][1] + m[2][2]
        return acos(min(1, max(-1, (trace - 1) / 2))) * 180 / .pi
    }

    func testPathValueMatchesGoldenVectors() throws {
        let (tolerance, cases) = try golden()
        var checked = 0
        for c in cases {
            for s in c.samples {
                let v = SphereMath.pathValue(c.keys, at: s[0])
                XCTAssertLessThan(wrapped(v.yaw, s[1]), tolerance, "\(c.name) yaw at \(s[0])")
                XCTAssertLessThan(abs(v.pitch - s[2]), tolerance, "\(c.name) pitch at \(s[0])")
                XCTAssertLessThan(abs(v.fov - s[3]), tolerance, "\(c.name) fov at \(s[0])")
                XCTAssertLessThan(wrapped(v.roll, s[4]), tolerance, "\(c.name) roll at \(s[0])")
                checked += 1
            }
        }
        XCTAssertGreaterThan(checked, 200)
    }

    func testEveryMoveTakesTheShortWay() throws {
        for c in try golden().cases {
            for arc in c.arcs {
                let a = c.keys[arc.from], b = c.keys[arc.to]
                let shortest = turn(SphereMath.matrix(for: a.view), SphereMath.matrix(for: b.view))
                XCTAssertEqual(shortest, arc.degrees, accuracy: 1e-6, c.name)
                XCTAssertLessThanOrEqual(shortest, 180)
                let mats = (0...400).map { i in
                    SphereMath.matrix(for: SphereMath.pathValue(c.keys, at: a.at + (b.at - a.at) * Double(i) / 400))
                }
                let travelled = zip(mats, mats.dropFirst()).reduce(0) { $0 + turn($1.0, $1.1) }
                XCTAssertEqual(travelled, shortest, accuracy: 1e-4, "\(c.name) arc \(arc.from)")
            }
        }
    }

    func testRaysAndEquirect() {
        let c = SphereMath.ray(u: 0.5, v: 0.5, fov: 90, aspect: 16.0 / 9, projection: .flat)
        assertVec(c, simd_double3(0, 0, 1))
        let e = SphereMath.ray(u: 1, v: 0.5, fov: 90, aspect: 16.0 / 9, projection: .flat)
        assertVec(e, simd_normalize(simd_double3(1, 0, 1)))
        assertVec(SphereMath.ray(u: 0.5, v: 0.5, fov: 90, aspect: 16.0 / 9, projection: .tinyPlanet), simd_double3(0, 0, 1))
        let tp = SphereMath.ray(u: 1, v: 0.5, fov: 90, aspect: 16.0 / 9, projection: .tinyPlanet)
        XCTAssertEqual(simd_length(tp), 1, accuracy: 1e-9)
        XCTAssertEqual(tp.z, cos(2 * atan(1.25)), accuracy: 1e-9)
        let a = SphereMath.equirectUV(simd_double3(0, 0, 1)); XCTAssertEqual(a.u, 0.5, accuracy: 1e-9); XCTAssertEqual(a.v, 0.5, accuracy: 1e-9)
        let b = SphereMath.equirectUV(simd_double3(1, 0, 0)); XCTAssertEqual(b.u, 0.75, accuracy: 1e-9); XCTAssertEqual(b.v, 0.5, accuracy: 1e-9)
        XCTAssertEqual(SphereMath.equirectUV(simd_double3(0, -1, 0)).v, 0, accuracy: 1e-9)
    }

    func testEffectiveView() {
        let v = SphereView(yaw: 10, pitch: 30, fov: 90, roll: 5)
        XCTAssertEqual(SphereMath.effectiveView(v, projection: .tinyPlanet).pitch, -90)
        XCTAssertEqual(SphereMath.effectiveView(v, projection: .flat), v)
    }

    func testTurnSanity() {
        let id = SphereView(yaw: 0, pitch: 0, fov: 90, roll: 0)
        let r = SphereMath.turn(id, right: 90, up: 0, upAxis: simd_double3(0, -1, 0))
        XCTAssertEqual(r.yaw, 90, accuracy: 1e-6); XCTAssertEqual(r.pitch, 0, accuracy: 1e-6)
        let u = SphereMath.turn(id, right: 0, up: 10, upAxis: simd_double3(0, -1, 0))
        XCTAssertEqual(u.pitch, 10, accuracy: 1e-6)
        let n = SphereMath.turn(id, right: 0, up: 10, upAxis: nil)
        XCTAssertEqual(n.pitch, 10, accuracy: 1e-6)
        XCTAssertEqual(n.fov, 90)
    }

    func testTurnRolledKeepsRightDotUp() {
        let up = simd_double3(0, -1, 0)
        let v = SphereView(yaw: 20, pitch: 10, fov: 90, roll: 90)
        let before = simd_dot(SphereMath.matrix(for: v) * simd_double3(1, 0, 0), up)
        let t = SphereMath.turn(v, right: 25, up: 0, upAxis: up)
        let after = simd_dot(SphereMath.matrix(for: t) * simd_double3(1, 0, 0), up)
        XCTAssertEqual(before, after, accuracy: 1e-6)
    }

    func testLevel() {
        let up = simd_double3(0, -1, 0)
        let l = SphereMath.level(SphereView(yaw: 15, pitch: 10, fov: 90, roll: 30), upAxis: up)
        XCTAssertEqual(l.roll, 0, accuracy: 1e-6)
        XCTAssertEqual(l.yaw, 15, accuracy: 1e-6)
        let flipped = SphereMath.level(SphereView(yaw: 0, pitch: 0, fov: 90, roll: 170), upAxis: up)
        let m = SphereMath.matrix(for: flipped)
        XCTAssertEqual(simd_dot(m * simd_double3(1, 0, 0), up), 0, accuracy: 1e-6)
        XCTAssertLessThan(simd_dot(m * simd_double3(0, 1, 0), up), 0)
    }

    func testDegreesPerPoint() {
        XCTAssertEqual(SphereMath.degreesPerPoint(fov: 90, viewWidthPoints: 300), 0.3, accuracy: 1e-12)
    }

    func testShaderMatchesMath() {
        XCTAssertTrue(SphereShader.source.contains("1.25"))
        XCTAssertTrue(SphereShader.source.contains("2.0 * atan(r)"))
        XCTAssertTrue(SphereShader.source.contains("address::repeat"))
        let st = SphereRenderState(matrix: SphereMath.v360Matrix(yaw: 0, pitch: 0, roll: 0), fov: 90, projection: .flat)
        let uv = sphereCPUMapping(st, u: 0.5, v: 0.5)
        XCTAssertEqual(uv.u, 0.5, accuracy: 1e-9); XCTAssertEqual(uv.v, 0.5, accuracy: 1e-9)
    }
}
