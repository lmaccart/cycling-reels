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
    func testSplitTargetsSelectedClipsOrEveryClipUnderPlayhead() {
        let nodes = [
            EditorNode(id: "a", kind: "segment", lane: "video", parentId: nil, startFrame: 0, endFrame: 40,
                       frames: 40, raw: .object([:]), assetId: nil, sourceIn: nil, sourceOut: nil,
                       assetKind: nil, generated: nil, readOnly: nil, track: nil),
            EditorNode(id: "b", kind: "segment", lane: "video", parentId: nil, startFrame: 20, endFrame: 60,
                       frames: 40, raw: .object([:]), assetId: nil, sourceIn: nil, sourceOut: nil,
                       assetKind: nil, generated: nil, readOnly: nil, track: nil),
            EditorNode(id: "c", kind: "segment", lane: "video", parentId: nil, startFrame: 60, endFrame: 80,
                       frames: 20, raw: .object([:]), assetId: nil, sourceIn: nil, sourceOut: nil,
                       assetKind: nil, generated: nil, readOnly: nil, track: nil),
            EditorNode(id: "readonly", kind: "segment", lane: "video", parentId: nil, startFrame: 10, endFrame: 50,
                       frames: 40, raw: .object([:]), assetId: nil, sourceIn: nil, sourceOut: nil,
                       assetKind: nil, generated: nil, readOnly: true, track: nil)
        ]
        XCTAssertEqual(TimelineEditingTargets.split(nodes: nodes, selectedIDs: [], at: 30), ["a", "b"])
        XCTAssertEqual(TimelineEditingTargets.split(nodes: nodes, selectedIDs: ["b"], at: 30), ["b"])
        XCTAssertEqual(TimelineEditingTargets.split(nodes: nodes, selectedIDs: ["c"], at: 30), [])
    }
    func testNewTimelineEditingOperationsEncodeContractFields() {
        let ids = ["a", "b"]
        let duplicate = TimelineEditingOperation.duplicate(ids: ids)
        XCTAssertEqual(duplicate["op"]?.stringValue, "duplicate_segments")
        XCTAssertEqual(duplicate["ids"]?.arrayValue?.compactMap(\.stringValue), ids)

        let remove = TimelineEditingOperation.rippleRemove(ids: ids)
        XCTAssertEqual(remove["op"]?.stringValue, "ripple_remove")
        XCTAssertEqual(remove["ids"]?.arrayValue?.compactMap(\.stringValue), ids)

        let raw = JSONValue.object(["asset": .string("source"), "in": .number(1), "out": .number(4)])
        let paste = TimelineEditingOperation.paste(values: [raw], atFrame: 120, track: "V2")
        XCTAssertEqual(paste["op"]?.stringValue, "paste_segments")
        XCTAssertEqual(paste["values"]?.arrayValue, [raw])
        XCTAssertEqual(paste["atFrame"]?.numberValue, 120)
        XCTAssertEqual(paste["track"]?.stringValue, "V2")
        XCTAssertEqual(TimelineEditingOperation.paste(values: [raw], atFrame: 0, track: nil)["track"], .null)

        let enabled = TimelineEditingOperation.setEnabled(ids: ids, enabled: false)
        XCTAssertEqual(enabled["op"]?.stringValue, "set_enabled")
        XCTAssertEqual(enabled["ids"]?.arrayValue?.compactMap(\.stringValue), ids)
        XCTAssertEqual(enabled["enabled"]?.boolValue, false)
    }
    func testFlaggedDropResolvesFlagIdentityToAssetMetadataAndRange() throws {
        let flag = try XCTUnwrap(ClipFlag(.object([
            "id": .string("flag-1"), "asset": .string("asset-2"),
            "in": .number(2.5), "out": .number(8)
        ])))
        let metadata = JSONValue.object(["id": .string("asset-2"), "name": .string("ridge")])
        let resolved = try XCTUnwrap(TimelineEditingTargets.flaggedDrop(
            id: "flag:flag-1", flags: [flag], library: [metadata]
        ))
        XCTAssertEqual(resolved.assetID, "asset-2")
        XCTAssertEqual(resolved.metadata["name"]?.stringValue, "ridge")
        XCTAssertEqual(resolved.sourceIn, 2.5)
        XCTAssertEqual(resolved.sourceOut, 8)
        XCTAssertNil(TimelineEditingTargets.flaggedDrop(id: "flag:gone", flags: [flag], library: [metadata]))
    }

    func testClipContextMenuContentsMatchClipKindAndSphereProbe() {
        let sphere = EditorNode(id: "sphere-segment", kind: "segment", lane: "video", parentId: nil,
                                startFrame: 0, endFrame: 60, frames: 60,
                                raw: .object(["asset": .string("sphere-asset")]), assetId: "sphere-asset",
                                sourceIn: 0, sourceOut: 2, assetKind: "video", generated: false,
                                readOnly: false, track: "V1")
        let sphereAsset: JSONValue = .object([
            "id": .string("sphere-asset"), "kind": .string("video"),
            "probe": .object(["sphere": .object(["projection": .string("equirectangular")])])
        ])
        XCTAssertEqual(TimelineContextMenuContents.groups(node: sphere, library: [sphereAsset]), [
            ["Split here", "Split at playhead"],
            ["Delete", "Ripple delete", "Duplicate", "Copy", "Paste"],
            ["Speed", "Disable"],
            ["Flag source range", "Aim 360", "Reveal in Source"]
        ])
        let flat = JSONValue.object(["id": .string("sphere-asset"), "kind": .string("video"),
                                     "probe": .object(["sphere": .null])])
        XCTAssertFalse(TimelineContextMenuContents.groups(node: sphere, library: [flat])
            .flatMap { $0 }.contains("Aim 360"))
        let cutaway = EditorNode(id: "cutaway", kind: "cutaway", lane: "overlay", parentId: "sphere-segment",
                                 startFrame: 0, endFrame: 30, frames: 30, raw: .object([:]),
                                 assetId: nil, sourceIn: nil, sourceOut: nil, assetKind: nil,
                                 generated: false, readOnly: false, track: nil)
        XCTAssertEqual(TimelineContextMenuContents.groups(node: cutaway, library: [sphereAsset]), [
            ["Delete", "Paste"], ["Flag source range", "Reveal in Source"]
        ])
    }
    func testEditorServiceReturnsRecordedStubResponse() async throws {
        let service = EditorService()
        let recorded = JSONValue.object(["ok": .bool(true)])
        service.callStub = { method, params in
            XCTAssertEqual(method, "document.apply")
            XCTAssertEqual(params["reason"]?.stringValue, "test edit")
            return recorded
        }

        let response = try await service.call("document.apply", params: .object(["reason": .string("test edit")]))
        XCTAssertEqual(response, recorded)
    }
    func testTimelineToolKeySwitching() {
        XCTAssertEqual(TimelineTool.fromKeyCode(8), .razor)
        XCTAssertEqual(TimelineTool.fromKeyCode(9), .select)
        XCTAssertNil(TimelineTool.fromKeyCode(11))
    }
}
