import XCTest
@testable import ReelsEditor

@MainActor
final class EditorOperationTests: XCTestCase {
    func testBrollInsertUsesPrimaryStableIDAndLocalTime() {
        let operation = BrollOperationBuilder.insert(
            parentID: "primary-7", assetID: "asset-bike",
            sourceIn: 1.25, sourceOut: 4.5, at: 2.1, isPhoto: false
        )
        XCTAssertEqual(operation["op"]?.stringValue, "insert_overlay")
        XCTAssertEqual(operation["parentId"]?.stringValue, "primary-7")
        XCTAssertEqual(operation["collection"]?.stringValue, "cutaways")
        XCTAssertEqual(operation["value"]?["asset"]?.stringValue, "asset-bike")
        XCTAssertEqual(operation["value"]?["in"]?.numberValue, 1.25)
        XCTAssertEqual(operation["value"]?["out"]?.numberValue, 4.5)
        XCTAssertEqual(operation["value"]?["at"]?.numberValue, 2.1)
    }

    func testCutawayReplacementOnlySetsSourceFieldsByStableID() {
        let operations = BrollOperationBuilder.replace(
            cutawayID: "cutaway-uuid", assetID: "new-asset",
            sourceIn: 3, sourceOut: 8, isPhoto: false
        )
        XCTAssertEqual(operations.count, 3)
        XCTAssertEqual(operations.map { $0["op"]?.stringValue }, ["set_field", "set_field", "set_field"])
        XCTAssertEqual(operations.map { $0["id"]?.stringValue }, Array(repeating: "cutaway-uuid", count: 3))
        XCTAssertEqual(operations.map { $0["path"]?.stringValue }, ["/asset", "/in", "/out"])
        XCTAssertEqual(operations[0]["value"]?.stringValue, "new-asset")
        XCTAssertEqual(operations[1]["value"]?.numberValue, 3)
        XCTAssertEqual(operations[2]["value"]?.numberValue, 8)
    }

    func testCutawayTrimKeepsStableIdentityAndEnforcesPositiveRange() {
        let operation = BrollOperationBuilder.trim(
            cutawayID: "cutaway-uuid", sourceIn: 2, sourceOut: 2.01, minimumDuration: 0.1
        )
        XCTAssertEqual(operation["op"]?.stringValue, "trim_cutaway")
        XCTAssertEqual(operation["id"]?.stringValue, "cutaway-uuid")
        XCTAssertEqual(operation["sourceIn"]?.numberValue, 2)
        XCTAssertEqual(try XCTUnwrap(operation["sourceOut"]?.numberValue), 2.1, accuracy: 0.0001)
    }

    func testPhotoCutawayUsesDurationRatherThanSourceInOut() {
        let insert = BrollOperationBuilder.insert(
            parentID: "primary-7", assetID: "still",
            sourceIn: 0, sourceOut: 4, at: 1.25, isPhoto: true
        )
        XCTAssertEqual(insert["value"]?["duration"]?.numberValue, 4)
        XCTAssertNil(insert["value"]?["in"])
        XCTAssertNil(insert["value"]?["out"])
        let replace = BrollOperationBuilder.replace(
            cutawayID: "cutaway-uuid", assetID: "still",
            sourceIn: 0, sourceOut: 5, isPhoto: true
        )
        XCTAssertEqual(replace.map { $0["path"]?.stringValue }, ["/asset", "/duration"])
        XCTAssertEqual(replace[1]["value"]?.numberValue, 5)
    }

    func testTimelinePhotoDropUsesDefaultDurationWithoutVideoBounds() throws {
        let photo: JSONValue = .object(["kind": .string("photo"), "filename": .string("still.jpg")])
        let value = try XCTUnwrap(EditorStore.timelineInsertSpec(
            assetID: "still", metadata: photo, sourceIn: nil, sourceOut: nil
        ))
        XCTAssertEqual(value["duration"]?.numberValue, 2.5)
        XCTAssertNil(value["in"])
        XCTAssertNil(value["out"])
        let marked = try XCTUnwrap(EditorStore.timelineInsertSpec(
            assetID: "still", metadata: photo, sourceIn: 1, sourceOut: 4
        ))
        XCTAssertEqual(marked["duration"]?.numberValue, 3)
        XCTAssertNil(EditorStore.timelineInsertSpec(
            assetID: "video", metadata: .object(["kind": .string("video")]),
            sourceIn: nil, sourceOut: nil
        ))
    }

    func testLockedAudioDetectsAudibleInsertOnEmptyVideoTrack() {
        let insert: JSONValue = .object([
            "asset": .string("source"), "in": .number(0), "out": .number(2)
        ])
        let placements = [TimelinePlacement.inserting(insert, track: "V2", atFrame: 90)]
        let audible: JSONValue = .object([
            "kind": .string("video"), "probe": .object(["hasAudio": .bool(true)])
        ])
        let silent: JSONValue = .object([
            "kind": .string("video"), "probe": .object(["hasAudio": .bool(false)])
        ])
        XCTAssertTrue(EditorStore.overwriteChangesLockedAudio(
            placements: placements, regions: [], nodes: [], assets: ["source": audible]
        ))
        XCTAssertFalse(EditorStore.overwriteChangesLockedAudio(
            placements: placements, regions: [], nodes: [], assets: ["source": silent]
        ))
        XCTAssertTrue(EditorStore.overwriteChangesLockedAudio(
            placements: placements, regions: [], nodes: []
        ))
        XCTAssertFalse(EditorStore.overwriteChangesLockedAudio(
            placements: [.inserting(.object(["asset": .string("still"), "duration": .number(2.5)]),
                                     track: "V2", atFrame: 90)],
            regions: [], nodes: [], assets: ["still": .object(["kind": .string("photo")])]
        ))
        XCTAssertFalse(EditorStore.insertionHasAudio(
            .object(["asset": .string("source"), "volume": .number(0)]), metadata: audible
        ) ?? true)
    }
}
