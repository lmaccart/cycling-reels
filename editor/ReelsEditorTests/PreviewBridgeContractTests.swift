import XCTest
@testable import ReelsEditor

final class PreviewBridgeContractTests: XCTestCase {
    private let origin = URL(string: "http://127.0.0.1:43127")!
    private let capability = URL(string: "http://127.0.0.1:43127/previews/rev-1/interactive/index.html")!

    func testLoadRevisionContainsProtectedPropsAndStaticMapping() throws {
        let manifest: JSONValue = .object([
            "url": .string(capability.absoluteString),
            "staticBaseUrl": .string("http://127.0.0.1:43127/previews/rev-1/interactive/public/"),
            "props": .object(["fps": .number(30), "totalFrames": .number(90), "segments": .array([])])
        ])
        let command = try XCTUnwrap(PreviewBridgeContract.loadCommand(manifest: manifest, revision: "rev-1", startFrame: 18))
        XCTAssertEqual(command["type"]?.stringValue, "loadRevision")
        XCTAssertEqual(command["revision"]?.stringValue, "rev-1")
        XCTAssertEqual(command["startFrame"]?.numberValue, 18)
        XCTAssertEqual(command["props"]?["fps"]?.numberValue, 30)
        XCTAssertEqual(
            PreviewBridgeContract.envelope(command)["channel"]?.stringValue,
            "cycling-reel-preview"
        )
        XCTAssertEqual(PreviewBridgeContract.messageHandler, "reelsPreview")
    }

    func testNavigationRequiresExactLaunchedOriginAndCapabilityPath() {
        XCTAssertTrue(PreviewBridgeContract.allowsPage(capability, origin: origin, capability: capability))
        XCTAssertFalse(PreviewBridgeContract.allowsPage(
            URL(string: "http://localhost:43127/previews/rev-1/interactive/index.html")!,
            origin: origin, capability: capability
        ))
        XCTAssertFalse(PreviewBridgeContract.allowsPage(
            URL(string: "http://127.0.0.1:43128/previews/rev-1/interactive/index.html")!,
            origin: origin, capability: capability
        ))
        XCTAssertFalse(PreviewBridgeContract.allowsPage(
            URL(string: "http://127.0.0.1:43127/other/index.html")!,
            origin: origin, capability: capability
        ))
    }

    func testStaticBaseAndRevisionTaggedEventsAreValidated() {
        XCTAssertTrue(PreviewBridgeContract.allowsStaticBase(
            URL(string: "http://127.0.0.1:43127/previews/rev-1/public/")!, origin: origin
        ))
        XCTAssertFalse(PreviewBridgeContract.allowsStaticBase(
            URL(string: "http://127.0.0.1:43128/previews/rev-1/public/")!, origin: origin
        ))
        XCTAssertTrue(PreviewBridgeContract.eventMatches(revision: "rev-1", loadedRevision: "rev-1", type: "frame"))
        XCTAssertFalse(PreviewBridgeContract.eventMatches(revision: "rev-0", loadedRevision: "rev-1", type: "frame"))
        XCTAssertTrue(PreviewBridgeContract.eventMatches(revision: nil, loadedRevision: "rev-1", type: "error"))
    }
}
