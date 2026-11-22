import XCTest
import simd
@testable import flibrary

/// Interactive-path performance: many aim changes and draws, per-draw cost and no piled-up work.
final class ViewerTests: XCTestCase {
    func testLetterbox() {
        let v = ViewerRenderer.letterbox(drawable: 1000, 800, aspect: 16.0 / 9)   // wide drawable, 9:16 output: pillarboxed
        XCTAssertEqual(v.height, 800, accuracy: 1e-9); XCTAssertEqual(v.width, 800 * 9 / 16, accuracy: 1e-9)
        XCTAssertEqual(v.originX, (1000 - v.width) / 2, accuracy: 1e-9); XCTAssertEqual(v.originY, 0, accuracy: 1e-9)
        let w = ViewerRenderer.letterbox(drawable: 400, 800, aspect: 9.0 / 16)   // tall drawable, 16:9 output: letterboxed
        XCTAssertEqual(w.width, 400, accuracy: 1e-9); XCTAssertEqual(w.height, 225, accuracy: 1e-9)
    }

    /// Runs `count` aim changes; each is one draw request like a mouse event would make. Returns per-draw CPU encode ms and the stats.
    private func drive(_ r: ViewerRenderer, frame: () throws -> FrameTextures?, count: Int, label: String) throws {
        let (tex, pass) = try XCTUnwrap(r.offscreen(width: 1080, height: 1080 * 16 / 9 / 2))   // a retina-ish pane
        _ = tex
        r.resetStats()
        var cpu = 0.0, requested = 0
        let began = Date()
        for i in 0..<count {
            let f = try frame()
            let view = SphereView(yaw: SphereMath.wrapYaw(Double(i) * 1.7), pitch: sin(Double(i) / 20) * 40, fov: 60 + Double(i % 60), roll: 0)
            let t0 = Date()
            _ = r.draw(pass: pass, size: (tex.width, tex.height), drawable: nil, frame: f, view: view, aspect: 16.0 / 9)
            cpu += Date().timeIntervalSince(t0); requested += 1
            usleep(4000)   // ~ 250 events/s, faster than a display refresh
        }
        while r.stats.inFlight > 0 { usleep(500) }
        let s = r.stats, wall = Date().timeIntervalSince(began)
        print(String(format: "PERF %@: %d requests, %d drawn, %d skipped; CPU encode %.3f ms/draw, GPU %.3f ms/draw, max in flight %d, wall %.2f s",
                     label, requested, s.draws, s.skipped, cpu / Double(requested) * 1000, s.gpuSeconds / Double(max(1, s.draws)) * 1000, s.maxInFlight, wall))
        XCTAssertLessThanOrEqual(s.maxInFlight, ViewerRenderer.maxFrames)
        XCTAssertLessThan(cpu / Double(requested) * 1000, 4, "encoding a draw should take a few ms at most")
        XCTAssertLessThan(s.gpuSeconds / Double(max(1, s.draws)) * 1000, 8)
    }

    func testPausedAimChangesAreCheapAndNeverQueue() throws {
        let r = try XCTUnwrap(ViewerRenderer.shared)
        var rgba = [UInt8](repeating: 255, count: 2048 * 1024 * 4)
        for i in 0..<(2048 * 1024) { rgba[i * 4] = UInt8(i % 2048 / 8); rgba[i * 4 + 1] = UInt8(i / 2048 / 4) }
        let f = try XCTUnwrap(FrameTextures.still(try XCTUnwrap(ReframeTests.cgImage(rgba, 2048, 1024)), device: r.device, time: 0))
        try drive(r, frame: { f }, count: 300, label: "paused equirect still")
    }

    func testPlaybackDrawsFromDecodedFramesNeverQueue() async throws {
        let root = try XCTUnwrap(RepositoryLocator.validate(URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()))
        let backend = Backend(root: root)
        let list = try await backend.list()
        let a = try XCTUnwrap(list.assets.first { $0.id == "261121-gs010119-c867" && FileManager.default.fileExists(atPath: $0.media) })
        let level = try await backend.level(id: a.id)
        let made = await PlaybackEngine.make(asset: a, export: nil, level: level)
        let engine = try XCTUnwrap(made)
        let r = try XCTUnwrap(ViewerRenderer.shared)
        // paused on a decoded EAC frame
        let pausedFrame = try await engine.frame(at: 10)
        let paused = try XCTUnwrap(pausedFrame)
        try drive(r, frame: { paused }, count: 300, label: "paused decoded EAC frame")
        // playing: new decoded frame every ~16 ms while the view moves
        var t = 10.2
        var decode = 0.0
        try await driveAsync(r, count: 300) { i in
            let d0 = Date()
            t += 1.0 / 60
            let f = try await engine.frame(at: t)
            decode += Date().timeIntervalSince(d0)
            return f
        }
        print(String(format: "PERF playing: decode (engine.frame) %.2f ms/step incl. skipped steps", decode / 300 * 1000))
    }

    private func driveAsync(_ r: ViewerRenderer, count: Int, frame: (Int) async throws -> FrameTextures?) async throws {
        let (tex, pass) = try XCTUnwrap(r.offscreen(width: 1080, height: 1080 * 16 / 9 / 2))
        r.resetStats()
        var cpu = 0.0
        for i in 0..<count {
            let f = try await frame(i)
            let t0 = Date()
            _ = r.draw(pass: pass, size: (tex.width, tex.height), drawable: nil, frame: f, view: SphereView(yaw: Double(i), pitch: 0, fov: 90, roll: 0), aspect: 16.0 / 9)
            cpu += Date().timeIntervalSince(t0)
        }
        while r.stats.inFlight > 0 { usleep(500) }
        let s = r.stats
        print(String(format: "PERF playing: %d drawn, %d skipped; CPU encode %.3f ms/draw, GPU %.3f ms/draw, max in flight %d", s.draws, s.skipped, cpu / Double(count) * 1000, s.gpuSeconds / Double(max(1, s.draws)) * 1000, s.maxInFlight))
        XCTAssertLessThanOrEqual(s.maxInFlight, ViewerRenderer.maxFrames)
        XCTAssertLessThan(cpu / Double(count) * 1000, 4)
    }
}
