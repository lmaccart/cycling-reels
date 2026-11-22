import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
import XCTest
@testable import flibrary

final class ReframeTests: XCTestCase {
    func view(_ y: Double, _ p: Double = 0, _ r: Double = 0, _ f: Double = 90) -> SphereView { SphereView(yaw: y, pitch: p, fov: f, roll: r) }

    func testKeyInsertReplaceDelete() {
        var s = ReframeSettings()
        s.setKey(at: 4, view: view(40)); s.setKey(at: 1, view: view(10)); s.setKey(at: 2, view: view(20))
        XCTAssertEqual(s.keys.map(\.at), [1, 2, 4])
        s.setKey(at: 2.03, view: view(99))            // within tolerance: replaces, keeps its time
        XCTAssertEqual(s.keys.map(\.at), [1, 2, 4]); XCTAssertEqual(s.keys[1].yaw, 99)
        s.setKey(at: -3, view: view(5))               // clamps to 0 and sorts first
        XCTAssertEqual(s.keys.map(\.at), [0, 1, 2, 4])
        s.deleteKey(near: 1.01); XCTAssertEqual(s.keys.map(\.at), [0, 2, 4])
        s.deleteKey(near: 3); XCTAssertEqual(s.keys.count, 3)
    }

    func testInterpolationMatchesBackendGolden() {
        // Values from scripts/reels/sphere.py path_value for the same keys.
        var s = ReframeSettings()
        s.setKey(at: 0, view: view(0, 0, 0, 90)); s.setKey(at: 2, view: view(120, 30, 10, 70)); s.setKey(at: 4, view: view(-170, -10, 0, 100))
        let golden: [(Double, [Double])] = [(0.5, [11.729663, 3.139262, -2.285489, 87.929688]), (1, [56.772402, 17.522699, -4.248188, 80]),
            (1.5, [106.138788, 28.745108, 5.783613, 72.070312]), (3, [155.140227, 8.435204, 11.305205, 85]), (3.5, [-177.579158, -6.810442, 3.262498, 96.894531])]
        for (t, g) in golden {
            let v = s.view(at: t)!
            for (a, b) in zip([v.yaw, v.pitch, v.roll, v.fov], g) { XCTAssertEqual(a, b, accuracy: 1e-4, "t=\(t)") }
        }
        XCTAssertEqual(SphereMath.ease(0.25), 0.103515625, accuracy: 1e-12)
        XCTAssertNil(ReframeSettings().view(at: 1))
        XCTAssertEqual(s.view(at: 99)?.fov, 100)
    }

    func testStateRoundTrip() throws {
        let dir = FileManager.default.temporaryDirectory.appending(path: "flib-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        var st = ReframeState()
        var s = ReframeSettings(); s.in = 1; s.out = 30; s.fps = 60; s.stabilize = true
        s.setKey(at: 0, view: view(0)); s.setKey(at: 2.5, view: view(90, 10, -5, 80))
        st.reframe["a1"] = s
        st.save(dir)
        XCTAssertEqual(ReframeState.load(dir), st)
        let raw = try JSONSerialization.jsonObject(with: Data(contentsOf: ReframeState.url(dir))) as? [String: Any]
        let a1 = ((raw?["reframe"] as? [String: Any])?["a1"]) as? [String: Any]
        XCTAssertEqual(a1?["in"] as? Double, 1); XCTAssertEqual((a1?["keys"] as? [[String: Any]])?[1]["roll"] as? Double, -5)
        XCTAssertEqual(ReframeState.load(dir.appending(path: "none")), ReframeState())
    }

    func testJobLineParsing() {
        XCTAssertEqual(JobLine.parse(#"{"progress": 0.42}"#), .progress(0.42))
        XCTAssertEqual(JobLine.parse(#"{"progress": 7}"#), .progress(1))
        XCTAssertEqual(JobLine.parse(#"{"done": true, "path": "/x/y.mp4"}"#), .done("/x/y.mp4"))
        XCTAssertEqual(JobLine.parse(#"{"error": "cancelled"}"#), .error("cancelled"))
        XCTAssertNil(JobLine.parse("ffmpeg noise")); XCTAssertNil(JobLine.parse(#"{"other": 1}"#))
    }

    func testEstimateRefusal() throws {
        let no = try JSONDecoder().decode(Estimate.self, from: Data(#"{"bytes":5000000000,"freeBytes":12000000000,"ok":false,"reason":"would leave under 10 GB free"}"#.utf8))
        XCTAssertTrue(try XCTUnwrap(no.refusal).hasPrefix("would leave under 10 GB free"))
        let yes = try JSONDecoder().decode(Estimate.self, from: Data(#"{"bytes":1,"freeBytes":2,"ok":true}"#.utf8))
        XCTAssertNil(yes.refusal)
    }

    /// Runs the queue against a fake `reels`: the refused job never exports; the next job streams progress and refreshes.
    @MainActor func testJobRunnerRefusalAndStreaming() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: "flib-fake-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root.appending(path: "library"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let script = """
        #!/bin/bash
        case "$2" in
          list) echo '{"assets":[]}' ;;
          estimate) if grep -q '"id":"refuse"' "$3"; then echo '{"bytes":9,"freeBytes":1,"ok":false,"reason":"disk full"}'; else echo '{"bytes":9,"freeBytes":99,"ok":true}'; fi ;;
          export) echo "touch $3.ran" >/dev/null; touch "$3.ran"; echo '{"progress": 0.5}'; echo '{"done": true, "path": "/x.mp4"}' ;;
        esac
        """
        let url = root.appending(path: "reels")
        try script.write(to: url, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: url.path)
        let store = LibraryStore(root: try XCTUnwrap(RepositoryLocator.validate(root)))
        let tools = ToolsModel(); tools.store = store
        tools.enqueue(JobSpec(id: "refuse", kind: "equirect"), title: "a")
        tools.requestEquirect(assetID: "ok")
        for _ in 0..<100 where tools.jobs.contains(where: { $0.state == .queued || $0.state == .running }) { try await Task.sleep(for: .milliseconds(100)) }
        XCTAssertEqual(tools.jobs.map(\.state), [.failed, .done])
        XCTAssertTrue(tools.jobs[0].detail?.hasPrefix("disk full") ?? false)
        XCTAssertEqual(tools.jobs[1].output, "/x.mp4")
        // The runner deletes the job file in a `defer` just after marking the job done; give it a moment.
        let jobsDir = root.appending(path: ".cache/flibrary/jobs").path
        var ran = try FileManager.default.contentsOfDirectory(atPath: jobsDir)
        for _ in 0..<50 where ran.contains(where: { $0.hasSuffix(".json") }) {
            try await Task.sleep(for: .milliseconds(20))
            ran = try FileManager.default.contentsOfDirectory(atPath: jobsDir)
        }
        XCTAssertTrue(ran.filter { $0.hasSuffix(".json") }.isEmpty, "job files are cleaned up (\(ran))")
    }

    /// RGBA bytes (alpha ignored) as a CGImage.
    static func cgImage(_ rgba: [UInt8], _ w: Int, _ h: Int) -> CGImage? {
        CGImage(width: w, height: h, bitsPerComponent: 8, bitsPerPixel: 32, bytesPerRow: w * 4, space: CGColorSpace(name: CGColorSpace.sRGB)!,
                bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.noneSkipLast.rawValue), provider: CGDataProvider(data: Data(rgba) as CFData)!, decode: nil,
                shouldInterpolate: false, intent: .defaultIntent)
    }

    func testRendererStraightAheadSamplesCentre() throws {
        // A 2:1 equirect with a left half red, right half blue: straight ahead (centre) straddles the seam.
        var buf = [UInt8](repeating: 255, count: 64 * 32 * 4)
        for y in 0..<32 { for x in 0..<64 { let i = (y * 64 + x) * 4; buf[i] = x < 32 ? 255 : 0; buf[i + 1] = 0; buf[i + 2] = x < 32 ? 0 : 255 } }
        let r = try XCTUnwrap(ViewerRenderer.shared)
        let tex = try XCTUnwrap(FrameTextures.still(try XCTUnwrap(Self.cgImage(buf, 64, 32)), device: r.device, time: 0))
        let img = try XCTUnwrap(r.readback(tex, view: view(0), width: 40, height: 20))
        let d = try XCTUnwrap(EquirectPixels(img, maxWidth: 40)).data
        XCTAssertGreaterThan(d[(10 * 40 + 2) * 4], 200)        // left = red
        XCTAssertGreaterThan(d[(10 * 40 + 37) * 4 + 2], 200)   // right = blue
        let right = try XCTUnwrap(r.readback(tex, view: view(60), width: 40, height: 20))   // yaw right: all blue
        XCTAssertGreaterThan(try XCTUnwrap(EquirectPixels(right, maxWidth: 40)).data[(10 * 40 + 5) * 4 + 2], 200)
    }

    /// Local preview vs `./reels flibrary frame --view` on a real clip (skipped when the repo or clip is missing).
    func testLivePreviewMatchesBackend() async throws {
        let root = try XCTUnwrap(RepositoryLocator.validate(URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()))
        let id = "261121-gs010119-c867"
        try XCTSkipUnless(FileManager.default.fileExists(atPath: root.appending(path: "library/media/\(id).360").path))
        let backend = Backend(root: root)
        let t = 20.0
        let still = try await backend.frame(id: id, t: t, width: 2048)
        let stillImage = await Media.image(path: still.path, maxPixel: 2048)
        let r = try XCTUnwrap(ViewerRenderer.shared)
        let src = try XCTUnwrap(FrameTextures.still(try XCTUnwrap(stillImage), device: r.device, time: t))
        let out = ProcessInfo.processInfo.environment["FLIBRARY_PREVIEW_OUT"]
        var report = ""
        for (v, w, h) in [(view(0), 270, 480), (view(75, 10, 0, 80), 270, 480), (view(180, -15, 12, 100), 480, 270)] {
            let local = try XCTUnwrap(r.readback(src, view: v, width: w, height: h))
            let rurl = try await backend.frame(id: id, t: t, view: v, w: w, h: h)
            let rimg = await Media.image(path: rurl.path, maxPixel: 2000)
            let remote = try XCTUnwrap(rimg)
            let diff = try XCTUnwrap(FlatRenderer.meanAbsDiff(local, remote))
            report += String(format: "view y%.0f p%.0f r%.0f f%.0f %dx%d: mean abs diff %.2f/255\n", v.yaw, v.pitch, v.roll, v.fov, w, h, diff)
            if let out {
                for (name, img) in [("local", local), ("backend", remote)] {
                    let u = URL(fileURLWithPath: out).appending(path: "\(name)-\(Int(v.yaw))-\(w).jpg")
                    let dest = try XCTUnwrap(CGImageDestinationCreateWithURL(u as CFURL, UTType.jpeg.identifier as CFString, 1, nil))
                    CGImageDestinationAddImage(dest, img, nil); CGImageDestinationFinalize(dest)
                }
            }
            XCTAssertLessThan(diff, 12, "preview should track the backend render")
        }
        print("PREVIEW-MATCH\n" + report)
    }
}
