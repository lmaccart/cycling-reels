import AppKit
import XCTest
@testable import ReelsEditor

final class UXPresentationTests: XCTestCase {
    @MainActor
    func testXCTestHostCannotActivateOrRestoreEditorWindows() throws {
        XCTAssertNotEqual(NSApp.activationPolicy(), .regular)
        XCTAssertEqual(NSApp.activationPolicy(), .prohibited)
        XCTAssertFalse(NSApp.isActive)
        let delegate = try XCTUnwrap(NSApp.delegate as? EditorAppDelegate)
        XCTAssertFalse(delegate.applicationShouldRestoreApplicationState(NSApp))
        XCTAssertFalse(delegate.applicationShouldSaveApplicationState(NSApp))
        XCTAssertFalse(delegate.applicationShouldHandleReopen(NSApp, hasVisibleWindows: false))
        let visibleWindows = NSApp.windows.filter(\.isVisible)
        XCTAssertTrue(visibleWindows.isEmpty, "Unexpected visible windows: \(visibleWindows.map { "\(type(of: $0)): \($0.title) \($0.frame)" })")
    }

    private func json(_ text: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: Data(text.utf8))
    }

    private var tagged: String {
        """
        {"id":"a1","kind":"video","originalName":"34cbbfd4d7f35b467a3bd21f679f594f.mov","created":"2026-02-07T12:10:51",
         "probe":{"duration":11.6},"transcript":"library/transcripts/a1.json",
         "tags":{"type":"talking","shot":"selfie","summary":"Selfie between intervals",
         "moments":[{"id":"m-1","in":0.0,"out":3.0,"label":"two done"},{"id":"m-2","in":7.8,"out":11.58}]}}
        """
    }

    func testTitleUsesSummaryThenNameThenHashFallback() throws {
        XCTAssertEqual(AssetPresentation.title(try json(tagged)), "Selfie between intervals")
        XCTAssertEqual(AssetPresentation.title(try json(#"{"originalName":"IMG_1593.mov","tags":null}"#)), "IMG_1593.mov")
        XCTAssertEqual(AssetPresentation.title(try json(#"{"originalName":"34cbbfd4d7f35b467a3bd21f679f594f.mov"}"#)), "Untagged clip")
    }

    func testDetailParts() throws {
        let item = try json(tagged)
        XCTAssertEqual(AssetPresentation.detailParts(item, currentYear: 2026), ["Feb 7", "0:12", "selfie", "talking"])
        XCTAssertEqual(AssetPresentation.dateLabel(item, currentYear: 2027), "Feb 7, 2026")
        XCTAssertTrue(AssetPresentation.hasTranscript(item))
    }

    func testSearchMatchesSummaryTagsAndMoments() throws {
        let item = try json(tagged)
        XCTAssertTrue(AssetPresentation.matches(item, query: "intervals selfie"))
        XCTAssertTrue(AssetPresentation.matches(item, query: "TWO done"))
        XCTAssertFalse(AssetPresentation.matches(item, query: "sunset"))
        XCTAssertTrue(AssetPresentation.matches(item, query: "  "))
    }

    func testNoResultsOverlayOnlyWhenClipsAndHitsEmpty() {
        XCTAssertTrue(AssetPresentation.showsNoResults(clipCount: 0, hitCount: 0))
        XCTAssertFalse(AssetPresentation.showsNoResults(clipCount: 3, hitCount: 0))
        XCTAssertFalse(AssetPresentation.showsNoResults(clipCount: 0, hitCount: 1))
        XCTAssertFalse(AssetPresentation.showsNoResults(clipCount: 0, hitCount: 0, searchInFlight: true))
    }

    func testMoments() throws {
        let moments = AssetPresentation.moments(try json(tagged))
        XCTAssertEqual(moments.map(\.id), ["m-1", "m-2"])
        XCTAssertEqual(moments[0].label, "two done  (0:00–0:03)")
        XCTAssertEqual(moments[1].title, "Moment 2")
        XCTAssertEqual(AssetPresentation.selectedMomentID(.string("m-2"), moments: moments), "m-2")
        XCTAssertEqual(AssetPresentation.selectedMomentID(.number(0), moments: moments), "m-1")
        XCTAssertNil(AssetPresentation.selectedMomentID(.string(""), moments: moments))
        XCTAssertTrue(AssetPresentation.moments(try json(#"{"tags":null}"#)).isEmpty)
    }

    func testJobNoticesAreHumanOrSilent() throws {
        XCTAssertNil(EventNotice.jobMessage(try json(#"{"kind":"preview","state":"queued"}"#)))
        XCTAssertNil(EventNotice.jobMessage(try json(#"{"kind":"ingest","state":"running"}"#)))
        XCTAssertEqual(EventNotice.jobMessage(try json(#"{"kind":"preview","state":"completed"}"#)), "Preview ready")
        XCTAssertEqual(EventNotice.jobMessage(try json(#"{"kind":"export","state":"running","progress":0.4}"#)), "Rendering… 40%")
        XCTAssertEqual(EventNotice.jobMessage(try json(#"{"kind":"export","state":"failed"}"#)), "Render failed")
    }
}
