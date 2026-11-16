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

    private func clip(_ id: String, _ start: Int, _ end: Int, track: String = "V1",
                      sourceIn: Double = 0, speed: Double = 1,
                      sourceOutOverride: Double? = nil) -> EditorNode {
        let sourceOut = sourceOutOverride ?? sourceIn + Double(end - start) * speed / 30
        return EditorNode(
            id: id, kind: "segment", lane: "primary", parentId: nil,
            startFrame: start, endFrame: end, frames: end - start,
            raw: .object(["asset": .string(id), "in": .number(sourceIn),
                          "out": .number(sourceOut), "speed": .number(speed)]),
            assetId: id, sourceIn: sourceIn, sourceOut: sourceOut,
            sourceDuration: 100, speed: speed, assetKind: "video", mediaKind: "video",
            generated: false, readOnly: false, track: track
        )
    }

    private func insert(_ start: Int, _ length: Int, track: String = "V1") -> TimelinePlacement {
        .inserting(
            .object(["asset": .string("incoming"), "duration": .number(Double(length) / 30)]),
            track: track, atFrame: start
        )
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

    func testOverwriteIDsMatchSharedPythonUTF8Vectors() {
        let editID = "A0000000-0000-4000-8000-000000000001"
        XCTAssertEqual(
            OptimisticTimeline.overwriteID(editID: editID, origin: "segment-victim", start: 180, end: 240),
            "ef0b2abac24c30522a26f0ffc5bb7e3f544b58ebd80d4c8deaef496d27806876"
        )
        XCTAssertEqual(
            OptimisticTimeline.overwriteID(editID: editID, origin: "insert:0", start: 90, end: 180),
            "f376c9832eba1e75ada37f5cf94f4e25aa9f1276967c17426652af5922d9e9a6"
        )
        XCTAssertEqual(
            OptimisticTimeline.overwriteID(editID: editID, origin: "caption-café", start: 180, end: 240),
            "84a5c489ae125a1439d2a0fe33d2f33d5b91f83d655278cf09fe55f58c7dd5ce"
        )
    }

    func testOverwriteFourIntersectionFormsAndUsesSpeedForSurvivorSourceFrames() {
        let victim = clip("victim", 30, 90, sourceIn: 4, speed: 1.5)
        let editID = "a0000000-0000-4000-8000-000000000001"

        let left = OptimisticTimeline.overwrite(placements: [insert(10, 40)], editID: editID, fps: 30)([victim])
        let leftSurvivor = try! XCTUnwrap(left.first { $0.id == "victim" })
        XCTAssertEqual(leftSurvivor.startFrame, 50)
        XCTAssertEqual(leftSurvivor.endFrame, 90)
        XCTAssertEqual(try! XCTUnwrap(leftSurvivor.sourceIn), 5, accuracy: 0.000_001)
        XCTAssertEqual(try! XCTUnwrap(leftSurvivor.sourceOut), 7, accuracy: 0.000_001)

        let right = OptimisticTimeline.overwrite(placements: [insert(70, 40)], editID: editID, fps: 30)([victim])
        let rightSurvivor = try! XCTUnwrap(right.first { $0.id == "victim" })
        XCTAssertEqual(rightSurvivor.startFrame, 30)
        XCTAssertEqual(rightSurvivor.endFrame, 70)
        XCTAssertEqual(try! XCTUnwrap(rightSurvivor.sourceIn), 4, accuracy: 0.000_001)
        XCTAssertEqual(try! XCTUnwrap(rightSurvivor.sourceOut), 6, accuracy: 0.000_001)

        let covered = OptimisticTimeline.overwrite(placements: [insert(20, 80)], editID: editID, fps: 30)([victim])
        XCTAssertFalse(covered.contains { $0.id == "victim" })

        let middle = OptimisticTimeline.overwrite(placements: [insert(50, 20)], editID: editID, fps: 30)([victim])
        XCTAssertEqual(middle.first { $0.id == "victim" }?.startFrame, 30)
        let tailID = "frag-" + OptimisticTimeline.overwriteID(
            editID: editID, origin: "victim", start: 70, end: 90
        )
        XCTAssertEqual(middle.first { $0.id == tailID }?.startFrame, 70)
        XCTAssertEqual(middle.first { $0.id == tailID }?.endFrame, 90)
        XCTAssertEqual(try! XCTUnwrap(middle.first { $0.id == tailID }?.sourceIn), 6, accuracy: 0.000_001)
    }

    func testOverwriteMultiVictimSelfExclusionAndSourceGap() {
        let source = clip("source", 0, 30)
        let firstVictim = clip("first", 90, 120)
        let secondVictim = clip("second", 120, 170)
        let moved = OptimisticTimeline.overwrite(
            placements: [.moving(id: "source", track: "V1", atFrame: 101)],
            editID: "multi-victim", fps: 30
        )([source, firstVictim, secondVictim])

        XCTAssertTrue(moved.contains { $0.id == "source" && $0.startFrame == 101 && $0.endFrame == 131 })
        XCTAssertFalse(moved.contains { $0.track == "V1" && $0.startFrame < 30 && $0.endFrame > 0 },
                       "moving a clip must leave a non-ripple source gap")
        XCTAssertTrue(moved.contains { $0.id == "first" && $0.startFrame == 90 && $0.endFrame == 101 })
        XCTAssertTrue(moved.contains { $0.id == "second" && $0.startFrame == 131 && $0.endFrame == 170 },
                      "the earliest surviving fragment retains its original ID")

        let selfMoved = OptimisticTimeline.overwrite(
            placements: [.moving(id: "source", track: "V1", atFrame: 10)],
            editID: "self-move", fps: 30
        )([source])
        XCTAssertEqual(selfMoved.filter { $0.id == "source" }.map(\.startFrame), [10],
                       "a same-track move must not overwrite itself")
    }

    func testOverwriteQueueCanMovePredictedRightFragmentAndUsesSharedInsertID() {
        let editID = "a0000000-0000-4000-8000-000000000001"
        let victim = clip("victim", 30, 90, sourceIn: 4, speed: 1.5)
        let firstPrediction = OptimisticTimeline.overwrite(
            placements: [insert(50, 20)], editID: editID, fps: 30
        )([victim])
        let tailID = "frag-" + OptimisticTimeline.overwriteID(
            editID: editID, origin: "victim", start: 70, end: 90
        )
        let queuedPrediction = OptimisticTimeline.overwrite(
            placements: [.moving(id: tailID, track: "V2", atFrame: 101)],
            editID: "queued-edit", fps: 30
        )(firstPrediction)
        XCTAssertTrue(queuedPrediction.contains {
            $0.id == tailID && $0.track == "V2" && $0.startFrame == 101 && $0.endFrame == 121
        })
        XCTAssertEqual(try! XCTUnwrap(queuedPrediction.first { $0.id == tailID }?.sourceIn),
                       6, accuracy: 0.000_001)

        let inserted = OptimisticTimeline.overwrite(
            placements: [insert(90, 90)], editID: editID, fps: 30
        )([])
        XCTAssertEqual(inserted.first?.id,
                       "clip-f376c9832eba1e75ada37f5cf94f4e25aa9f1276967c17426652af5922d9e9a6",
                       "inserted node identity must match the shared Python vector")
        XCTAssertEqual(OptimisticTimeline.placementDuration(
            .inserting(.object(["asset": .string("tie"), "duration": .number(0.05)]),
                       track: "V1", atFrame: 0),
            nodes: [], fps: 30
        ), 2, "1.5 frames rounds to the nearest even frame")
        XCTAssertEqual(OptimisticTimeline.placementDuration(
            .inserting(.object(["asset": .string("tie"), "duration": .number(0.15)]),
                       track: "V1", atFrame: 0),
            nodes: [], fps: 30
        ), 4, "4.5 frames rounds to the nearest even frame")
    }

    func testOverwriteKeepsOwnedCutawaysWithParentAndQueuedNodeIdentity() {
        let owner = clip("owner", 0, 90, track: "V1")
        let cutaway = EditorNode(
            id: "owned-broll", kind: "cutaway", lane: "cutaway", parentId: owner.id,
            startFrame: 10, endFrame: 80, frames: 70,
            raw: .object([
                "asset": .string("broll"), "in": .number(2),
                "out": .number(2 + 70.0 / 30.0), "speed": .number(1),
            ]),
            assetId: "broll", sourceIn: 2, sourceOut: 2 + 70.0 / 30.0,
            sourceDuration: 20, speed: 1, assetKind: "video", mediaKind: "video",
            generated: false, readOnly: false, track: "V2"
        )
        let unrelatedV2 = OptimisticTimeline.overwrite(
            placements: [insert(30, 10, track: "V2")], editID: "unrelated-v2", fps: 30
        )([owner, cutaway])
        XCTAssertEqual(unrelatedV2.first { $0.id == cutaway.id }, cutaway,
                       "an unrelated V2 overwrite must not slice a parent-owned cutaway")
        XCTAssertFalse(unrelatedV2.contains { $0.id.hasPrefix("frag-") || $0.id.hasPrefix("node-") })
        XCTAssertFalse(OptimisticTimeline.overwriteRegions(
            placements: [insert(30, 10, track: "V2")], nodes: [owner, cutaway], fps: 30
        ).contains { $0.nodeID == cutaway.id })
        let explicitlyMoved = OptimisticTimeline.overwrite(
            placements: [.moving(id: cutaway.id, track: "V3", atFrame: 100)],
            editID: "explicit-cutaway-move", fps: 30
        )([owner, cutaway])
        XCTAssertTrue(explicitlyMoved.contains {
            $0.id == cutaway.id && $0.track == "V3" && $0.startFrame == 100 && $0.endFrame == 170
        }, "only an explicit cutaway move promotes it to an independent overwrite root")

        let editID = "cutaway-parent-edit"
        let firstPlacements = [insert(30, 20, track: "V1")]
        let first = OptimisticTimeline.overwrite(placements: firstPlacements, editID: editID, fps: 30)
        let split = first([owner, cutaway])
        let parentTailID = "frag-" + OptimisticTimeline.overwriteID(
            editID: editID, origin: owner.id, start: 50, end: 90
        )
        let childTailID = "node-" + OptimisticTimeline.overwriteID(
            editID: editID, origin: cutaway.id, start: 50, end: 90
        )
        XCTAssertTrue(split.contains { $0.id == cutaway.id && $0.parentId == owner.id
            && $0.startFrame == 10 && $0.endFrame == 30 })
        XCTAssertTrue(split.contains { $0.id == childTailID && $0.parentId == parentTailID
            && $0.track == "V2" && $0.startFrame == 50 && $0.endFrame == 80 })

        let queuedPlacement = TimelinePlacement.moving(id: parentTailID, track: "V2", atFrame: 120)
        let queued = OptimisticTimeline.overwrite(
            placements: [queuedPlacement], editID: "queued-parent-move", fps: 30
        )(split)
        let queuedAgain = OptimisticTimeline.overwrite(
            placements: [queuedPlacement], editID: "queued-parent-move", fps: 30
        )(first([owner, cutaway]))
        XCTAssertEqual(queued, queuedAgain, "authoritative replay must preserve queued child identities")
        XCTAssertTrue(queued.contains { $0.id == childTailID && $0.parentId == parentTailID
            && $0.startFrame == 120 && $0.endFrame == 150 })
    }

    func testCutawayPromotionBecomesRootSegmentAndExtendsDurationForQueuedMove() throws {
        let owner = clip("owner", 0, 90)
        let cutaway = EditorNode(
            id: "promoted-broll", kind: "cutaway", lane: "cutaway", parentId: owner.id,
            startFrame: 10, endFrame: 40, frames: 30,
            raw: .object([
                "asset": .string("silent-broll"), "in": .number(2), "out": .number(3),
                "at": .number(10), "for": .number(1), "lane": .string("cutaway"),
                "parentId": .string("owner"),
            ]),
            assetId: "silent-broll", sourceIn: 2, sourceOut: 3,
            sourceDuration: 10, speed: 1, assetKind: "video", mediaKind: "video",
            generated: false, readOnly: false, track: "V2"
        )
        let nodeValues = try [owner, cutaway].map {
            try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode($0))
        }
        let snapshot = try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("silent-promotion"),
            "revisions": .object([
                "document": .string("d1"), "library": .string("l1"), "renderer": .string("r1"),
            ]),
            "plan": .object([:]), "nodes": .array(nodeValues),
            "fps": .number(30), "durationFrames": .number(90),
            "warnings": .array([]), "preview": .object([:]),
        ])))

        let promotedSnapshot = OptimisticTimeline.apply(
            OptimisticTimeline.overwrite(
                placements: [.moving(id: cutaway.id, track: "V1", atFrame: 100)],
                editID: "promote-edit", fps: 30
            ),
            to: snapshot
        )
        let promoted = try XCTUnwrap(promotedSnapshot.nodes.first { $0.id == cutaway.id })
        XCTAssertEqual(promoted.kind, "segment")
        XCTAssertEqual(promoted.lane, "primary")
        XCTAssertNil(promoted.parentId)
        XCTAssertEqual(promoted.track, "V1")
        XCTAssertEqual(promoted.startFrame, 100)
        XCTAssertEqual(promoted.endFrame, 130)
        XCTAssertTrue(OptimisticTimeline.segments(promotedSnapshot.nodes).contains { $0.id == cutaway.id },
                      "silent promotion keeps the original cutaway identity as a primary root")
        XCTAssertEqual(promoted.raw["lane"]?.stringValue, "primary")
        XCTAssertNil(promoted.raw["parentId"])
        XCTAssertNil(promoted.raw["at"])
        XCTAssertNil(promoted.raw["for"])
        XCTAssertEqual(promoted.raw["timeline"]?["track"]?.stringValue, "V1")
        XCTAssertEqual(promoted.raw["timeline"]?["startFrame"]?.numberValue, 100)
        XCTAssertEqual(promoted.raw["timeline"]?["durationFrames"]?.numberValue, 30)
        XCTAssertEqual(promotedSnapshot.durationFrames, 130)

        let queuedSnapshot = OptimisticTimeline.apply(
            OptimisticTimeline.overwrite(
                placements: [.moving(id: cutaway.id, track: "V1", atFrame: 150)],
                editID: "queued-promoted-move", fps: 30
            ),
            to: promotedSnapshot
        )
        let queued = try XCTUnwrap(queuedSnapshot.nodes.first { $0.id == cutaway.id })
        XCTAssertEqual(queued.kind, "segment")
        XCTAssertNil(queued.parentId)
        XCTAssertEqual(queued.startFrame, 150)
        XCTAssertEqual(queued.endFrame, 180)
        XCTAssertEqual(queued.raw["timeline"]?["startFrame"]?.numberValue, 150)
        XCTAssertEqual(queuedSnapshot.durationFrames, 180)
    }

    func testOverwritePreservesPinnedDurationAndExactSourceBoundaries() throws {
        var fractional = clip(
            "fractional", 0, 30, sourceIn: 1.01, sourceOutOverride: 2.02
        )
        fractional.raw = fractional.raw.setting(.number(2.02), at: ["out"])
        let untouched = OptimisticTimeline.overwrite(
            placements: [insert(90, 10)], editID: "fractional-untouched", fps: 30
        )([fractional])
        XCTAssertEqual(untouched.first { $0.id == "fractional" }?.sourceIn, 1.01)
        XCTAssertEqual(untouched.first { $0.id == "fractional" }?.sourceOut, 2.02,
                       "an unchanged right boundary keeps the service's exact source Out")

        let cut = OptimisticTimeline.overwrite(
            placements: [insert(10, 10)], editID: "fractional-cut", fps: 30
        )([fractional])
        let tailID = "frag-" + OptimisticTimeline.overwriteID(
            editID: "fractional-cut", origin: "fractional", start: 20, end: 30
        )
        XCTAssertEqual(try XCTUnwrap(cut.first { $0.id == tailID }?.sourceIn),
                       1.01 + 20.0 / 30.0, accuracy: 0.000_001)
        XCTAssertEqual(cut.first { $0.id == tailID }?.sourceOut, 2.02)

        let quantized = clip(
            "quantized", 0, 20, sourceIn: 4, speed: 1.5, sourceOutOverride: 4.99
        )
        let oneFrameTail = OptimisticTimeline.overwrite(
            placements: [insert(0, 19)], editID: "quantized-tail", fps: 30
        )([quantized])
        let tail = try XCTUnwrap(oneFrameTail.first { $0.id == "quantized" })
        XCTAssertEqual(tail.startFrame, 19)
        XCTAssertEqual(try XCTUnwrap(tail.sourceIn), 4.95, accuracy: 0.000_001)
        XCTAssertEqual(try XCTUnwrap(tail.sourceOut), 4.99, accuracy: 0.000_001)
        XCTAssertLessThanOrEqual(tail.sourceOut ?? .infinity, 4.99)

        let last = clip("last", 120, 180)
        let snapshot = try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("pinned-duration"),
            "revisions": .object([
                "document": .string("d1"), "library": .string("l1"), "renderer": .string("r1"),
            ]),
            "plan": .object([:]),
            "nodes": .array([try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(last))]),
            "fps": .number(30), "durationFrames": .number(180),
            "warnings": .array([]), "preview": .object([:]),
        ])))
        let movedEarlier = OptimisticTimeline.apply(
            OptimisticTimeline.overwrite(
                placements: [.moving(id: "last", track: "V1", atFrame: 60)],
                editID: "move-last-earlier", fps: 30
            ),
            to: snapshot
        )
        XCTAssertEqual(movedEarlier.nodes.first { $0.id == "last" }?.endFrame, 120)
        XCTAssertEqual(movedEarlier.durationFrames, 180,
                       "moving the last clip earlier preserves the pinned trailing gap")
    }

    func testOverwriteRejectsOverlappingIncomingPlacementsOnSameTrack() {
        let original = [clip("existing", 0, 30)]
        let predicted = OptimisticTimeline.overwrite(
            placements: [insert(90, 30), insert(119, 30)], editID: "overlap", fps: 30
        )(original)
        XCTAssertEqual(predicted, original)
    }

    func testOverwriteLeavesSourceGapLayersHigherTrackAndSplitsEditableChildren() {
        let source = clip("source", 0, 30, track: "V1")
        let lower = clip("lower", 90, 180, track: "V1")
        let upper = clip("upper", 90, 180, track: "V2")
        let placement = TimelinePlacement.moving(id: "source", track: "V2", atFrame: 90)
        let moved = OptimisticTimeline.overwrite(placements: [placement], editID: "edit-1", fps: 30)(
            [source, lower, upper]
        )
        XCTAssertFalse(moved.contains { $0.id == "source" && $0.track == "V1" })
        XCTAssertTrue(moved.contains { $0.id == "source" && $0.track == "V2" && $0.startFrame == 90 })
        XCTAssertTrue(moved.contains { $0.id == "lower" && $0.startFrame == 90 },
                      "overwriting V2 must not cut the lower V1 layer")
        XCTAssertFalse(moved.contains { $0.id == "upper" && $0.startFrame == 90 })

        let owner = clip("segment-victim", 180, 240)
        let caption = EditorNode(
            id: "caption-café", kind: "text", lane: "text", parentId: owner.id,
            startFrame: 180, endFrame: 240, frames: 60, raw: .object(["text": .string("caption")]),
            assetId: nil, sourceIn: nil, sourceOut: nil, sourceDuration: nil, speed: nil,
            assetKind: nil, mediaKind: nil, generated: false, readOnly: false, track: "V3"
        )
        let secondOwner = clip("second-owner", 260, 320)
        let secondChild = EditorNode(
            id: "second-child", kind: "text", lane: "text", parentId: secondOwner.id,
            startFrame: 260, endFrame: 320, frames: 60, raw: .object([:]),
            assetId: nil, sourceIn: nil, sourceOut: nil, sourceDuration: nil, speed: nil,
            assetKind: nil, mediaKind: nil, generated: false, readOnly: false, track: "V3"
        )
        let split = OptimisticTimeline.overwrite(
            placements: [insert(210, 10), insert(290, 10)],
            editID: "a0000000-0000-4000-8000-000000000001", fps: 30
        )([owner, caption, secondOwner, secondChild])
        let duplicateID = "node-" + OptimisticTimeline.overwriteID(
            editID: "a0000000-0000-4000-8000-000000000001",
            origin: "caption-café", start: 220, end: 240
        )
        let secondDuplicateID = "node-" + OptimisticTimeline.overwriteID(
            editID: "a0000000-0000-4000-8000-000000000001",
            origin: "second-child", start: 300, end: 320
        )
        XCTAssertEqual(split.first { $0.id == "caption-café" }?.parentId, owner.id)
        XCTAssertEqual(split.first { $0.id == duplicateID }?.parentId,
                       split.first { $0.startFrame == 220 && $0.kind == "segment" }?.id)
        XCTAssertEqual(split.first { $0.id == "second-child" }?.parentId, secondOwner.id)
        XCTAssertEqual(split.first { $0.id == secondDuplicateID }?.parentId,
                       split.first { $0.startFrame == 300 && $0.kind == "segment" }?.id)
    }

    func testSameTimeCrossTrackMoveLeavesGeneratedReadOnlyAudioOnA1Unchanged() {
        let source = clip("source", 0, 30, track: "V1")
        var audio = node("source-audio", 0, 30, kind: "source_audio", parent: source.id)
        audio.lane = "audio"
        audio.track = "A1"
        audio.generated = true
        audio.readOnly = true
        audio.assetId = "audio-asset"
        audio.raw = .object([
            "asset": .string("audio-asset"), "in": .number(1), "out": .number(2),
            "speed": .number(1), "volume": .number(0.8)
        ])

        let moved = OptimisticTimeline.overwrite(
            placements: [.moving(id: source.id, track: "V2", atFrame: 0)],
            editID: "audio-move", fps: 30
        )([source, audio])
        let audioChildren = moved.filter { $0.id == "source-audio" }
        XCTAssertEqual(audioChildren.count, 1)
        XCTAssertEqual(audioChildren.first?.parentId, source.id)
        XCTAssertEqual(audioChildren.first?.track, "A1")
        XCTAssertEqual(audioChildren.first?.startFrame, 0)
        XCTAssertEqual(audioChildren.first?.endFrame, 30)
        XCTAssertEqual(audioChildren.first?.raw, audio.raw)
        XCTAssertTrue(moved.contains { $0.id == source.id && $0.track == "V2" && $0.startFrame == 0 })
        XCTAssertFalse(TimelineLaneClassifier.shouldDrawLinkedAudio(for: source, nodes: [source, audio]))
        XCTAssertEqual(TimelineLaneClassifier.interactionNode(for: audio, nodes: [source, audio])?.id,
                       source.id)
        XCTAssertTrue(TimelineLaneClassifier.shouldDrawLinkedAudio(for: source, nodes: [source]),
                      "legacy snapshots without generated source audio keep the linked A1 visual")
        XCTAssertNil(TimelineLaneClassifier.interactionNode(for: audio, nodes: [audio]),
                     "orphaned read-only source audio cannot become a standalone interaction")
    }

    func testBinDropGeometryHitsCanonicalVideoTrackAndEvenFrame() {
        let tracks = [
            EditorTrack(id: "video-two", canonicalId: "V2", kind: "video", name: "B-roll", index: 2,
                        locked: false, userAdded: false),
            EditorTrack(id: "V1", kind: "video", name: "V1", index: 1, locked: false, userAdded: false),
            EditorTrack(id: "A1", kind: "audio", name: "A1", index: 1, locked: false, userAdded: false),
        ]
        let normalTarget = TimelineDropLocation.resolveAppKit(
            CGPoint(x: 132 + 90.5 * 0.5, y: 320 - (35 + 23)),
            canvasSize: CGSize(width: 620, height: 320), zoom: 0.5, tracks: tracks
        )
        XCTAssertEqual(normalTarget?.track, "V2", "renamed display labels must not become operation IDs")
        XCTAssertEqual(normalTarget?.atFrame, 90, "exact half-frame ties round to even")

        // The AppKit-converted x is document-space, including the horizontal scroll.
        let zoom = 0.5
        let visibleWidth: CGFloat = 320
        let scrollX: CGFloat = 220
        let visibleStartFrame = scrollX / zoom
        let scrolledFrame = 520
        let localPointerX = 132 + CGFloat(scrolledFrame - Int(visibleStartFrame)) * zoom
        let convertedDocumentX = localPointerX + scrollX
        XCTAssertLessThan(localPointerX, visibleWidth, "test point is within a narrow scrolled viewport")
        let scrolledTarget = TimelineDropLocation.resolveAppKit(
            CGPoint(x: convertedDocumentX, y: 320 - (35 + 23)),
            canvasSize: CGSize(width: 1600, height: 320), zoom: zoom, tracks: tracks
        )
        XCTAssertEqual(scrolledTarget?.track, "V2")
        XCTAssertEqual(scrolledTarget?.atFrame, scrolledFrame)

        let audioCenterFromTop: CGFloat = 35 + 2 * 46 + 8 + 20
        XCTAssertNil(TimelineDropLocation.resolveAppKit(
            CGPoint(x: 132 + 20, y: 320 - audioCenterFromTop),
            canvasSize: CGSize(width: 620, height: 320), zoom: 0.5, tracks: tracks
        ), "audio-row drops must not fall through to V1 or a selection target")
        XCTAssertNil(TimelineDropLocation.resolveAppKit(
            CGPoint(x: 131, y: 320 - (35 + 23)),
            canvasSize: CGSize(width: 620, height: 320), zoom: 0.5, tracks: tracks
        ), "the pinned label gutter is outside the timeline")
        XCTAssertNil(TimelineDropLocation.resolveAppKit(
            CGPoint(x: 620, y: 320 - (35 + 23)),
            canvasSize: CGSize(width: 620, height: 320), zoom: 0.5, tracks: tracks
        ), "the canvas maximum edge is out of bounds")
        let lockedTracks = tracks.map { track in
            var track = track
            if track.canonicalId == "V2" { track.locked = true }
            return track
        }
        XCTAssertNil(TimelineDropLocation.resolveAppKit(
            CGPoint(x: 132 + 20, y: 320 - (35 + 23)),
            canvasSize: CGSize(width: 620, height: 320), zoom: 0.5, tracks: lockedTracks
        ), "locked destination rows are not drop targets")

        let vertical = TimelineDropLocation.overwriteFrame(
            at: CGPoint(x: convertedDocumentX, y: 190),
            downPoint: CGPoint(x: convertedDocumentX - 20, y: 230),
            anchorFrame: scrolledFrame, grabOffset: 8, zoom: zoom, snapping: true
        )
        XCTAssertTrue(vertical.verticalSameTime)
        XCTAssertEqual(vertical.frame, scrolledFrame, "vertical track moves preserve exact source time")
        let horizontal = TimelineDropLocation.overwriteFrame(
            at: CGPoint(x: convertedDocumentX, y: 190),
            downPoint: CGPoint(x: convertedDocumentX - 30, y: 190),
            anchorFrame: scrolledFrame, grabOffset: 8, zoom: zoom, snapping: true
        )
        XCTAssertFalse(horizontal.verticalSameTime)
        XCTAssertEqual(horizontal.frame, scrolledFrame - 8)
    }
}
