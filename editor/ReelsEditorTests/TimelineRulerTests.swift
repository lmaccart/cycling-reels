import AppKit
import Darwin
import XCTest
@testable import ReelsEditor

@MainActor
private final class OcclusionWindow: NSWindow {
    var simulatedOcclusion: NSWindow.OcclusionState = []
    override var occlusionState: NSWindow.OcclusionState { simulatedOcclusion }
}

@MainActor
private final class RecordingPlayheadLayer: PlayheadLayer {
    var invalidated: [NSRect] = []

    override func setNeedsDisplay(_ invalidRect: NSRect) {
        invalidated.append(invalidRect)
        super.setNeedsDisplay(invalidRect)
    }
}

final class TimelineRulerTests: XCTestCase {
    @MainActor
    private func canvas(allowOccludedDrawing: Bool = true) throws -> NSView {
        let snapshot = try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("ruler-test"),
            "revisions": .object([
                "document": .string("1"), "library": .string("1"), "renderer": .string("1")
            ]),
            "plan": .object(["slug": .string("ruler-test"), "segments": .array([])]),
            "nodes": .array([]), "tracks": .array([]), "fps": .number(30),
            "durationFrames": .number(1800), "warnings": .array([]),
            "preview": .object(["state": .string("empty")]),
        ])))
        let view = makeTimelineCanvasCaptureView(snapshot: snapshot, tracks: EditorTrack.defaults,
                                                 trackMap: [:], zoom: 1,
                                                 allowOccludedDrawing: allowOccludedDrawing)
        view.frame = NSRect(x: 0, y: 0, width: 640, height: 160)
        return view
    }

    private func footprint() throws -> UInt64 {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: integer_t.self, capacity: Int(count)) {
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), $0, &count)
            }
        }
        guard result == KERN_SUCCESS else { throw NSError(domain: NSMachErrorDomain, code: Int(result)) }
        return info.phys_footprint
    }

    @MainActor
    func testRulerDrawsTenThousandTimesWithoutUnboundedGrowth() throws {
        let view = try canvas()
        let bitmap = try XCTUnwrap(NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: 640, pixelsHigh: 160, bitsPerSample: 8,
            samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0
        ))
        let context = try XCTUnwrap(NSGraphicsContext(bitmapImageRep: bitmap))
        NSGraphicsContext.saveGraphicsState()
        defer { NSGraphicsContext.restoreGraphicsState() }
        NSGraphicsContext.current = context
        let ruler = NSRect(x: 132, y: 125, width: 508, height: 35)
        for _ in 0..<200 { autoreleasepool { view.draw(ruler) } }
        for _ in 0..<5_000 { autoreleasepool { view.draw(ruler) } }
        let halfway = try footprint()
        for _ in 0..<5_000 { autoreleasepool { view.draw(ruler) } }
        let after = try footprint()
        XCTAssertLessThan(after > halfway ? after - halfway : 0, 96 * 1024 * 1024,
                          "ruler drawing should not retain memory on each tick")
        XCTAssertGreaterThan(try XCTUnwrap(bitmap.colorAt(x: 300, y: 10)).alphaComponent, 0)
    }

    @MainActor
    func testPlayheadInvalidatesOnlyOldAndNewColumns() throws {
        let overlay = RecordingPlayheadLayer(frame: NSRect(x: 0, y: 0, width: 640, height: 160))
        overlay.invalidated.removeAll()
        overlay.update(frame: 100, zoom: 1, labelWidth: 132, rulerHeight: 30, fps: 30, redraw: true)
        XCTAssertEqual(overlay.invalidated, [
            NSRect(x: 124, y: 0, width: 17, height: 160),
            NSRect(x: 224, y: 0, width: 17, height: 160)
        ])
        overlay.invalidated.removeAll()
        overlay.update(frame: 200, zoom: 1, labelWidth: 132, rulerHeight: 30, fps: 30, redraw: false)
        XCTAssertTrue(overlay.invalidated.isEmpty)
        overlay.update(frame: 201, zoom: 1, labelWidth: 132, rulerHeight: 30, fps: 30, redraw: true)
        XCTAssertEqual(overlay.invalidated, [
            NSRect(x: 324, y: 0, width: 17, height: 160),
            NSRect(x: 325, y: 0, width: 17, height: 160)
        ])
    }

    @MainActor
    func testOccludedCanvasSkipsDrawingAndRefreshesWhenVisible() throws {
        let view = try canvas(allowOccludedDrawing: false)
        let window = OcclusionWindow(contentRect: view.frame, styleMask: .borderless,
                                     backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        defer { window.close() }
        window.contentView = view
        let overlay = try XCTUnwrap(view.subviews.first as? PlayheadLayer)
        overlay.update(frame: 100, zoom: 1, labelWidth: 132, rulerHeight: 30, fps: 30, redraw: false)

        let bitmap = try XCTUnwrap(NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: 640, pixelsHigh: 160, bitsPerSample: 8,
            samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
            bytesPerRow: 0, bitsPerPixel: 0
        ))
        memset(try XCTUnwrap(bitmap.bitmapData), 0, bitmap.bytesPerRow * bitmap.pixelsHigh)
        let context = try XCTUnwrap(NSGraphicsContext(bitmapImageRep: bitmap))
        NSGraphicsContext.saveGraphicsState()
        defer { NSGraphicsContext.restoreGraphicsState() }
        NSGraphicsContext.current = context
        view.draw(view.bounds)
        XCTAssertEqual(try XCTUnwrap(bitmap.colorAt(x: 300, y: 10)).alphaComponent, 0)

        window.simulatedOcclusion = .visible
        NotificationCenter.default.post(name: NSWindow.didChangeOcclusionStateNotification, object: window)
        view.draw(view.bounds)
        XCTAssertGreaterThan(try XCTUnwrap(bitmap.colorAt(x: 300, y: 10)).alphaComponent, 0)
    }
}
