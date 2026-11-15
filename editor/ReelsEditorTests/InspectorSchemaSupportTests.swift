import XCTest
@testable import ReelsEditor

final class InspectorSchemaSupportTests: XCTestCase {
    func testTargetSelectionUsesStableNodeKindAndPhotoMetadata() {
        let schema: JSONValue = .object([
            "targets": .object([
                "segment.photo": .object([:]),
                "segment.video": .object([:]),
                "cutaway.photo": .object([:]),
                "cutaway.video": .object([:])
            ])
        ])
        let photo = EditorNode(
            id: "stable-segment", kind: "segment", lane: "primary", parentId: nil,
            startFrame: 0, endFrame: 60, frames: 60, raw: .object([:]),
            assetId: "still", sourceIn: nil, sourceOut: nil
        )
        let cutaway = EditorNode(
            id: "stable-cutaway", kind: "cutaway", lane: "cutaway", parentId: "stable-segment",
            startFrame: 10, endFrame: 30, frames: 20, raw: .object([:]),
            assetId: "ride", sourceIn: nil, sourceOut: nil
        )
        XCTAssertEqual(
            InspectorSchemaSupport.targetNames(node: photo, isProject: false, assetKind: "image/heic", available: schema),
            ["segment.photo"]
        )
        XCTAssertEqual(
            InspectorSchemaSupport.targetNames(node: cutaway, isProject: false, assetKind: "video/mp4", available: schema),
            ["cutaway.video"]
        )
    }

    func testRelativePathsStripOnlyTheTargetScope() {
        XCTAssertEqual(
            InspectorSchemaSupport.relativeComponents(
                path: "/segments/{index}/text/{item}/for",
                scope: "/segments/{index}/text/{item}"
            ),
            ["for"]
        )
        XCTAssertEqual(
            InspectorSchemaSupport.relativeComponents(path: "/render/fps", scope: "/render"),
            ["fps"]
        )
        XCTAssertEqual(
            InspectorSchemaSupport.relativeComponents(path: "/captions/maxCharsPerPage", scope: "/segments/{index}/punch"),
            ["captions", "maxCharsPerPage"]
        )
    }

    func testDescriptorDefaultsCreateNativeTypedValues() {
        XCTAssertEqual(
            InspectorSchemaSupport.defaultValue(for: .object(["type": .string("boolean")])),
            .bool(false)
        )
        XCTAssertEqual(
            InspectorSchemaSupport.defaultValue(for: .object([
                "type": .string("number"), "min": .number(2), "step": .number(0.5)
            ])),
            .number(2)
        )
        XCTAssertEqual(
            InspectorSchemaSupport.defaultValue(for: .object([
                "type": .string("enum"), "choices": .array([.string("top"), .string("lower")])
            ])),
            .string("top")
        )
    }

    func testTargetItemDefaultsBuildRelativeChildObjects() {
        let target: JSONValue = .object([
            "scopePath": .string("/segments/{index}/text/{item}"),
            "groups": .array([
                .object([
                    "label": .string("Text"),
                    "fields": .array([
                        .object([
                            "id": .string("text"), "label": .string("Text"),
                            "path": .string("/segments/{index}/text/{item}/text"),
                            "type": .string("text"), "required": .bool(true)
                        ]),
                        .object([
                            "id": .string("for"), "label": .string("Duration"),
                            "path": .string("/segments/{index}/text/{item}/for"),
                            "type": .string("timing"), "default": .number(2.2)
                        ])
                    ])
                ])
            ])
        ])
        let (value, valid) = InspectorSchemaSupport.defaultObject(for: target)
        XCTAssertTrue(valid)
        XCTAssertEqual(value["text"]?.stringValue, "New text")
        XCTAssertEqual(value["for"]?.numberValue, 2.2)
        XCTAssertNil(value.value(at: ["segments"]))
    }

    func testPrimitiveCollectionItemsStayIndependentAndTyped() {
        let points: JSONValue = .object([
            "path": .string("/exhibits/{item}/chart/points"),
            "itemFields": .array([
                .object(["path": .string("/exhibits/{item}/chart/points/{item}/0"), "type": .string("number")]),
                .object(["path": .string("/exhibits/{item}/chart/points/{item}/1"), "type": .string("number")])
            ])
        ])
        XCTAssertEqual(
            InspectorSchemaSupport.defaultCollectionItem(for: points),
            .array([.number(0), .number(0)])
        )
    }
}
