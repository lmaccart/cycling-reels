import XCTest
import simd
@testable import flibrary

final class PlaybackTests: XCTestCase {
    func testStartTimeStopsAtOutAndRestartsFromIn() {
        XCTAssertEqual(PlaybackMath.startTime(12, in: 5, out: 20), 12)
        XCTAssertEqual(PlaybackMath.startTime(20, in: 5, out: 20), 5)   // at out: play again from in
        XCTAssertEqual(PlaybackMath.startTime(25, in: 5, out: 20), 5)
        XCTAssertEqual(PlaybackMath.startTime(-1, in: 0, out: 20), 0)
        XCTAssertTrue(PlaybackMath.isFinished(20, out: 20)); XCTAssertFalse(PlaybackMath.isFinished(19.9, out: 20))
        XCTAssertTrue(PlaybackMath.coversClip(exportDuration: 185.4, clipDuration: 185.49))
        XCTAssertFalse(PlaybackMath.coversClip(exportDuration: 2, clipDuration: 185.49))
    }

    func testEacFaces() {
        let axes: [(simd_double3, Int)] = [([0, 0, 1], 1), ([1, 0, 0], 2), ([-1, 0, 0], 0), ([0, 1, 0], 3), ([0, 0, -1], 4), ([0, -1, 0], 5)]
        for (d, face) in axes {
            let f = PlaybackMath.eacFace(d)
            XCTAssertEqual(f.face, face); XCTAssertEqual(f.u, 0.5, accuracy: 1e-9); XCTAssertEqual(f.v, 0.5, accuracy: 1e-9)
        }
        let edge = PlaybackMath.eacFace(simd_normalize([0.5, 0.0, 1.0]))   // 26.6 degrees right of centre on the front face
        XCTAssertEqual(edge.face, 1); XCTAssertEqual(edge.u, 2 / .pi * atan(0.5) + 0.5, accuracy: 1e-9)
        let corner = PlaybackMath.eacFace(simd_normalize([1, 1, 1.0001]))  // 45 degrees: face edge
        XCTAssertEqual(corner.u, 1.0, accuracy: 1e-3)
    }

    func testEacPixelAndTrackColumn() {
        let h = 736, o = 64
        let c = PlaybackMath.eacPixel(u: 0.5, v: 0.5, face: 1, h: h)
        XCTAssertEqual(c.x, 367.5, accuracy: 1e-9); XCTAssertEqual(c.y, 367.5, accuracy: 1e-9)   // centre of a 736 px face
        let l = PlaybackMath.eacPixel(u: 0, v: 0, face: 0, h: h)
        XCTAssertEqual(l.x, 1.5, accuracy: 1e-9)   // 2 px border
        XCTAssertEqual(PlaybackMath.rawColumn(col: 1, x: 367.5, h: h, overlap: o), 1135.5, accuracy: 1e-9)   // middle face is raw columns 768..1503
        XCTAssertEqual(PlaybackMath.rawColumn(col: 0, x: 0, h: h, overlap: o), 0, accuracy: 0.05)
        // right face starts at 2h + g; its last pixel is the strip's last raw column (2272 - 1)
        XCTAssertEqual(PlaybackMath.rawColumn(col: 2, x: Double(h - 1), h: h, overlap: o), 2271, accuracy: 1.0)
    }

    // MARK: live (real repo, skipped when absent)

    private func repo() throws -> URL {
        try XCTUnwrap(RepositoryLocator.validate(URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()))
    }

    private func asset(_ backend: Backend) async throws -> Asset {
        let list = try await backend.list()
        return try XCTUnwrap(list.assets.first { $0.id == "261121-gs010119-c867" && FileManager.default.fileExists(atPath: $0.media) })
    }

    private let views = [SphereView(yaw: 0, pitch: 0, fov: 90, roll: 0), SphereView(yaw: 90, pitch: 10, fov: 90, roll: 0),
                         SphereView(yaw: 180, pitch: 0, fov: 100, roll: 0), SphereView(yaw: -40, pitch: -60, fov: 120, roll: 15),
                         SphereView(yaw: 30, pitch: 45, fov: 70, roll: 0)]

    private func compare(_ engine: PlaybackEngine, backend: Backend, id: String, t: Double, label: String) async throws {
        for v in views {
            let img = try await engine.render(at: t, view: v, width: 270, height: 480)
            let ref = try await backend.frame(id: id, t: t, view: v, w: 270, h: 480)
            let refLoaded = await Media.image(path: ref.path, maxPixel: 2000)
            let refImg = try XCTUnwrap(refLoaded)
            let d = try XCTUnwrap(FlatRenderer.meanAbsDiff(try XCTUnwrap(img), refImg))
            print(String(format: "LIVE playback %@ yaw %.0f pitch %.0f fov %.0f: mean abs diff %.2f / 255", label, v.yaw, v.pitch, v.fov, d))
            XCTAssertLessThan(d, 6, "\(label) \(v)")
        }
    }

    func testLiveSourcePathMatchesBackendFrame() async throws {
        let backend = Backend(root: try repo())
        let a = try await asset(backend)
        let level = try await backend.level(id: a.id)
        let made = await PlaybackEngine.make(asset: a, export: nil, level: level)
        let engine = try XCTUnwrap(made)
        try await compare(engine, backend: backend, id: a.id, t: 10.0, label: ".360 t=10")
        // play on: 3 s of consecutive frames at 30 fps, then compare the frame we land on
        let began = Date()
        var t = 10.5
        while t < 13.5 { _ = try await engine.render(at: t, view: views[0], width: 270, height: 480); t += 1.0 / 30 }
        print(String(format: "LIVE playback decode+render 90 frames in %.2f s", Date().timeIntervalSince(began)))
        try await compare(engine, backend: backend, id: a.id, t: 15.0, label: ".360 jump t=15")
    }

    func testLiveExportPathMatchesBackendFrame() async throws {
        let backend = Backend(root: try repo())
        let a = try await asset(backend)
        let out = try repo().appending(path: "flibrary-exports/\(a.id)-equirect.mp4")
        try XCTSkipIf(FileManager.default.fileExists(atPath: out.path), "an equirect export already exists")
        defer { try? FileManager.default.removeItem(at: out) }
        let job = try repo().appending(path: ".cache/flibrary/test-playback-job.json")
        try FileManager.default.createDirectory(at: job.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data(#"{"id":"\#(a.id)","kind":"equirect","stabilize":false,"in":0,"out":2}"#.utf8).write(to: job)
        defer { try? FileManager.default.removeItem(at: job) }
        _ = try await backend.run(["export", job.path])
        // a 2 s export only covers a 2 s "clip": pretend the clip is that long so it is accepted
        var json = try XCTUnwrap(JSONSerialization.jsonObject(with: JSONEncoderBridge.encode(a)) as? [String: Any])
        json["duration"] = 2.0
        let short = try JSONDecoder().decode(Asset.self, from: JSONSerialization.data(withJSONObject: json))
        let made = await PlaybackEngine.make(asset: short, export: out, level: nil)
        let engine = try XCTUnwrap(made)
        try await compare(engine, backend: backend, id: a.id, t: 1.0, label: "export t=1")
    }
}

/// Asset is Decodable only; rebuild its JSON from the fields the test needs.
enum JSONEncoderBridge {
    static func encode(_ a: Asset) throws -> Data {
        try JSONSerialization.data(withJSONObject: ["id": a.id, "kind": a.kind, "media": a.media, "is360": a.is360, "projection": a.projection as Any, "duration": a.duration ?? 0])
    }
}
