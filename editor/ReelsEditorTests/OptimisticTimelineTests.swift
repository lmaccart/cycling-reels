import XCTest
@testable import ReelsEditor

final class OptimisticTimelineTests: XCTestCase {
    private func node(_ id: String, _ start: Int, _ end: Int, kind: String = "segment", parent: String? = nil) -> EditorNode {
        EditorNode(id: id, kind: kind, lane: kind == "segment" ? "video" : "overlay", parentId: parent,
                   startFrame: start, endFrame: end, frames: end - start, raw: .object([:]),
                   assetId: nil, sourceIn: nil, sourceOut: nil, assetKind: nil, generated: nil,
                   readOnly: nil, track: nil)
    }

    private var reel: [EditorNode] {
        [node("a", 0, 30), node("b", 30, 90), node("c", 90, 100), node("t", 40, 50, kind: "text", parent: "b")]
    }

    private func starts(_ nodes: [EditorNode]) -> [String: Int] {
        Dictionary(uniqueKeysWithValues: nodes.map { ($0.id, $0.startFrame) })
    }

    func testReorderMovesSegmentAndCarriesItsChildren() {
        let moved = OptimisticTimeline.reorderSegment(id: "b", beforeID: "a")(reel)
        XCTAssertEqual(starts(moved), ["b": 0, "a": 60, "c": 90, "t": 10])
        XCTAssertEqual(OptimisticTimeline.segments(moved).map(\.id), ["b", "a", "c"])
    }

    func testReorderToEnd() {
        let moved = OptimisticTimeline.reorderSegment(id: "a", beforeID: nil)(reel)
        XCTAssertEqual(OptimisticTimeline.segments(moved).map(\.id), ["b", "c", "a"])
        XCTAssertEqual(starts(moved)["a"], 70)
    }

    func testTrimRipplesLaterSegments() {
        let trimmed = OptimisticTimeline.trimSegment(id: "a", length: 20)(reel)
        XCTAssertEqual(starts(trimmed), ["a": 0, "b": 20, "c": 80, "t": 30])
        XCTAssertEqual(trimmed.first { $0.id == "a" }?.endFrame, 20)
    }

    func testMoveAndResizeOverlay() {
        XCTAssertEqual(starts(OptimisticTimeline.move(id: "t", toFrame: 70)(reel))["t"], 70)
        let sized = OptimisticTimeline.resize(id: "t", start: 45, end: 80)(reel).first { $0.id == "t" }
        XCTAssertEqual(sized?.startFrame, 45)
        XCTAssertEqual(sized?.endFrame, 80)
    }

    func testDropSlotUsesLandingMiddleAndIgnoresDropInPlace() {
        let ordered = OptimisticTimeline.segments(reel)
        let a = ordered[0]
        // Barely moved: same slot, no edit.
        XCTAssertNil(OptimisticTimeline.dropSlot(moving: a, landingStart: 5, segments: ordered))
        // Dropped past the middle of b: goes before c.
        XCTAssertEqual(OptimisticTimeline.dropSlot(moving: a, landingStart: 60, segments: ordered),
                       .init(beforeID: "c"))
        // Dropped past everything: end of the reel.
        XCTAssertEqual(OptimisticTimeline.dropSlot(moving: a, landingStart: 120, segments: ordered),
                       .init(beforeID: nil))
    }
}
