import AppKit
import XCTest
@testable import ReelsEditor

@MainActor
private final class CursorRecordingCanvas: TimelineCanvas {
    var registeredCursors: [NSCursor] = []
    override func addCursorRect(_ rect: NSRect, cursor: NSCursor) {
        registeredCursors.append(cursor)
        super.addCursorRect(rect, cursor: cursor)
    }
}

final class EditorUXHelpersTests: XCTestCase {
    func testCaptionWordCorrectionsUseTranscriptIndex() {
        let words: [JSONValue] = [
            .object(["text": .string("of"), "index": .number(40), "w": .string("of"), "s": .number(195.99)]),
            .object(["text": .string("Zicheng"), "index": .number(41), "w": .string("Zicheng"), "s": .number(212.49)]),
        ]
        let out = TranscriptWordEditor.corrections(words: words, drafts: ["of", "Zhicheng"])
        XCTAssertEqual(out, [.object(["index": .number(41), "word": .string("Zhicheng")])])
        let plain: [JSONValue] = [.object(["w": .string("a")]), .object(["w": .string("b")])]
        XCTAssertEqual(TranscriptWordEditor.corrections(words: plain, drafts: ["a", "c"]),
                       [.object(["index": .number(1), "word": .string("c")])])
    }

    @MainActor
    func testCanvasToolKeysRegisterAndRemoveTheRazorCursor() throws {
        let canvas = CursorRecordingCanvas(frame: NSRect(x: 0, y: 0, width: 500, height: 300))
        let window = NSWindow(contentRect: canvas.frame, styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = canvas
        defer { window.close() }
        func press(_ code: UInt16, _ characters: String) throws {
            canvas.keyDown(with: try XCTUnwrap(NSEvent.keyEvent(with: .keyDown, location: .zero,
                modifierFlags: [], timestamp: 0, windowNumber: window.windowNumber, context: nil,
                characters: characters, charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code)))
            canvas.registeredCursors.removeAll()
            canvas.resetCursorRects()
        }
        try press(8, "c")
        XCTAssertEqual(canvas.registeredCursors, [.crosshair])
        try press(9, "v")
        XCTAssertFalse(canvas.registeredCursors.contains(.crosshair))
    }

    func testTimecodeAndPreciseTime() {
        XCTAssertEqual(TimeFormat.timecode(0, fps: 30), "00:00:00:00")
        XCTAssertEqual(TimeFormat.timecode(95, fps: 30), "00:00:03:05")
        XCTAssertEqual(TimeFormat.timecode(-4, fps: 30), "00:00:00:00")
        XCTAssertEqual(TimeFormat.precise(3.26), "0:03.3")
        XCTAssertEqual(TimeFormat.precise(75.04), "1:15.0")
        XCTAssertEqual(TimeFormat.precise(.nan), "0:00.0")
        XCTAssertEqual(TimeFormat.clock(65), "1:05")
        XCTAssertEqual(TimeFormat.clock(3725), "1:02:05")
    }

    func testZoomClampsAndSteps() {
        XCTAssertEqual(TimelineMetrics.clamp(100), TimelineMetrics.maxZoom)
        XCTAssertEqual(TimelineMetrics.clamp(0), TimelineMetrics.minZoom)
        XCTAssertEqual(TimelineMetrics.clamp(.nan), TimelineMetrics.defaultZoom)
        XCTAssertGreaterThan(TimelineMetrics.zoomIn(1), 1)
        XCTAssertLessThan(TimelineMetrics.zoomOut(1), 1)
        XCTAssertEqual(TimelineMetrics.zoomIn(TimelineMetrics.maxZoom), TimelineMetrics.maxZoom)
    }

    @MainActor
    func testZoomSliderBindingRoundTripsLogScale() {
        let control = TimelineZoomControl()
        let slider = control.sliderPosition
        for zoom in [TimelineMetrics.minZoom, 0.1, TimelineMetrics.defaultZoom, 1, TimelineMetrics.maxZoom] {
            control.zoom = zoom
            let position = slider.wrappedValue
            XCTAssertEqual(position, log(zoom), accuracy: 0.0001)
            control.zoom = 0.25
            slider.wrappedValue = position
            XCTAssertEqual(control.zoom, zoom, accuracy: 0.0001)
        }
    }

    @MainActor
    func testZoomSliderBindingClampsAndPreservesNonfiniteFallback() {
        let control = TimelineZoomControl()
        let slider = control.sliderPosition
        for (position, expected) in [
            (log(TimelineMetrics.minZoom) - 1, TimelineMetrics.minZoom),
            (log(TimelineMetrics.maxZoom) + 1, TimelineMetrics.maxZoom),
            (-1000, TimelineMetrics.minZoom),
            (Double.nan, TimelineMetrics.defaultZoom),
            (Double.infinity, TimelineMetrics.defaultZoom)
        ] {
            slider.wrappedValue = position
            XCTAssertEqual(control.zoom, expected, accuracy: 0.0001)
        }
    }

    @MainActor
    func testZoomSliderBindingAgreesWithButtonsAndFit() {
        let control = TimelineZoomControl()
        let slider = control.sliderPosition
        slider.wrappedValue = log(1)
        control.zoomIn()
        XCTAssertEqual(slider.wrappedValue, log(1.35), accuracy: 0.0001)
        control.zoomOut()
        XCTAssertEqual(slider.wrappedValue, log(1), accuracy: 0.0001)
        control.fit(durationFrames: 600, viewportWidth: 1000, gutter: 112)
        XCTAssertEqual(control.zoom, 1.4, accuracy: 0.0001)
        XCTAssertEqual(slider.wrappedValue, log(1.4), accuracy: 0.0001)
        slider.wrappedValue = log(1.4)
        XCTAssertEqual(control.zoom, 1.4, accuracy: 0.0001)
        control.zoom = TimelineMetrics.maxZoom
        control.zoomIn()
        XCTAssertEqual(slider.wrappedValue, TimelineZoomControl.sliderRange.upperBound, accuracy: 0.0001)
        control.zoom = TimelineMetrics.minZoom
        control.zoomOut()
        XCTAssertEqual(slider.wrappedValue, TimelineZoomControl.sliderRange.lowerBound, accuracy: 0.0001)
    }

    func testFitZoomLeavesGutterAndMargin() {
        let zoom = TimelineMetrics.fitZoom(durationFrames: 300, viewportWidth: 1000, gutter: 112)
        XCTAssertEqual(zoom, (1000 - 112 - 48) / 300, accuracy: 0.0001)
        XCTAssertEqual(TimelineMetrics.fitZoom(durationFrames: 0, viewportWidth: 1000, gutter: 112), TimelineMetrics.defaultZoom)
    }

    func testVisibleFramesAndZoomRoundTrip() {
        let zoom = TimelineMetrics.zoom(forVisibleFrames: 600, viewportWidth: 1000, gutter: 100)
        XCTAssertEqual(zoom, 1.5, accuracy: 0.0001)
        XCTAssertEqual(TimelineMetrics.visibleFrames(zoom: zoom, viewportWidth: 1000, gutter: 100), 600, accuracy: 0.001)
        XCTAssertEqual(TimelineMetrics.zoom(forVisibleFrames: 0, viewportWidth: 1000, gutter: 100), TimelineMetrics.defaultZoom)
        XCTAssertEqual(TimelineMetrics.zoom(forVisibleFrames: 1, viewportWidth: 1000, gutter: 100), TimelineMetrics.maxZoom)
    }

    func testRulerStepKeepsLabelsApart() {
        XCTAssertEqual(TimelineMetrics.rulerStepSeconds(pixelsPerSecond: 100), 1)
        XCTAssertEqual(TimelineMetrics.rulerStepSeconds(pixelsPerSecond: 20), 5)
        XCTAssertEqual(TimelineMetrics.rulerStepSeconds(pixelsPerSecond: 0.1), 600)
    }

    func testSnapPicksNearestWithinPixelThreshold() {
        // zoom 0.5 px/frame: 8 px = 16 frames
        XCTAssertEqual(TimelineMetrics.snap(frame: 95, candidates: [0, 100, 110], zoom: 0.5), 100)
        XCTAssertNil(TimelineMetrics.snap(frame: 60, candidates: [0, 100], zoom: 0.5))
        XCTAssertNil(TimelineMetrics.snap(frame: 5, candidates: [], zoom: 0.5))
        // zoomed in: 8 px = 2 frames
        XCTAssertNil(TimelineMetrics.snap(frame: 95, candidates: [100], zoom: 4))
    }

    func testEdgeZoneShrinksOnNarrowClips() {
        XCTAssertEqual(TimelineMetrics.edgeZone(clipWidth: 200), 7)
        XCTAssertEqual(TimelineMetrics.edgeZone(clipWidth: 12), 4)
        XCTAssertEqual(TimelineMetrics.edgeZone(clipWidth: 1), 2)
    }

    func testShortcutResolution() {
        func r(_ code: UInt16, _ ch: String, _ f: NSEvent.ModifierFlags = []) -> EditorShortcut? {
            EditorShortcut.resolve(keyCode: code, characters: ch, flags: f)
        }
        XCTAssertEqual(r(49, " "), .togglePlayback)
        XCTAssertEqual(r(40, "k"), .togglePlayback)
        XCTAssertEqual(r(38, "j"), .jumpBack)
        XCTAssertEqual(r(37, "l"), .jumpForward)
        XCTAssertEqual(r(34, "i"), .markIn)
        XCTAssertEqual(r(31, "o"), .markOut)
        XCTAssertEqual(r(43, ","), .insert)
        XCTAssertEqual(r(123, ""), .stepBack)
        XCTAssertEqual(r(124, "", .shift), .jumpForward)
        XCTAssertEqual(r(51, ""), .delete)
        XCTAssertEqual(r(40, "k", .command), .split)
        XCTAssertNil(r(11, "b", .command))
        XCTAssertNil(r(11, "b"))
        XCTAssertNil(r(34, "i", .command))
        XCTAssertNil(r(40, "k", .option))
    }

    func testPreviewModeLabelsAvoidJargon() {
        XCTAssertEqual(PreviewModeLabel.title("interactive"), "Fast preview")
        XCTAssertEqual(PreviewModeLabel.title("exact"), "Final quality")
    }
}
