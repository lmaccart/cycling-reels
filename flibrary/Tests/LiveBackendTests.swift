import XCTest
@testable import flibrary

/// Runs against the real repo when present (skipped otherwise): list, thumbnails, hover-scrub, 360 frame.
final class LiveBackendTests: XCTestCase {
    func testLiveListThumbsScrub() async throws {
        let root = try XCTUnwrap(RepositoryLocator.validate(URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()))
        let backend = Backend(root: root)
        let list = try await backend.list()
        XCTAssertFalse(list.assets.isEmpty)
        let flat = try XCTUnwrap(list.assets.first { !$0.is360 && $0.kind == "video" && FileManager.default.fileExists(atPath: $0.media) })
        let sphere = try XCTUnwrap(list.assets.first { $0.is360 && $0.sheetGrid != nil })
        let t1 = await Media.thumbnail(for: flat), t2 = await Media.thumbnail(for: sphere)
        XCTAssertNotNil(t1); XCTAssertNotNil(t2)
        let s1 = await Media.scrubFrame(for: flat, fraction: 0.6), s2 = await Media.scrubFrame(for: sphere, fraction: 0.6)
        XCTAssertNotNil(s1, "flat scrub"); XCTAssertNotNil(s2, "360 scrub")
        print("LIVE flat \(flat.id) thumb \(t1?.width ?? 0)x\(t1?.height ?? 0) scrub \(s1?.width ?? 0)x\(s1?.height ?? 0); 360 \(sphere.id) tile \(s2?.width ?? 0)x\(s2?.height ?? 0)")
        let frame = try await backend.frame(id: sphere.id, t: 1.0, width: 1024)
        XCTAssertTrue(FileManager.default.fileExists(atPath: frame.path))
        print("LIVE frame \(frame.path)")
    }
}
