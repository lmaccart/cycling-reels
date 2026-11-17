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

@MainActor
private final class DirtyChanges {
    var canvas = 0
    var playhead = 0
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
        let window = NSWindow(contentRect: view.frame, styleMask: .borderless, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        defer { window.close() }
        window.contentView = view
        XCTAssertGreaterThan(view.visibleRect.width, 132)
        let rulerFont = try XCTUnwrap(EditorType.rulerAttributes[.font] as? NSFont)
        XCTAssertTrue(rulerFont === EditorType.nsRuler)
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
        XCTAssertTrue(rulerFont === (EditorType.rulerAttributes[.font] as? NSFont))
        var hasRulerMark = false
        for y in 1..<29 {
            for x in 136..<180 {
                let color = try XCTUnwrap(bitmap.colorAt(x: x, y: y)).usingColorSpace(.deviceRGB)
                if let color, color.redComponent > 0.3 {
                    hasRulerMark = true
                    break
                }
            }
            if hasRulerMark { break }
        }
        XCTAssertTrue(hasRulerMark, "the ruler must draw a tick or label, not only its background")
    }

    @MainActor
    func testCornerOnlyRedrawDoesNotPaintRuler() throws {
        let view = try canvas()
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
        view.draw(NSRect(x: 0, y: 125, width: 132, height: 35))
        XCTAssertEqual(try XCTUnwrap(bitmap.colorAt(x: 300, y: 10)).alphaComponent, 0)
        view.draw(NSRect(x: 132, y: 125, width: 508, height: 35))
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
        window.simulatedOcclusion = .visible
        window.display()
        window.simulatedOcclusion = []
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

        // Offscreen NSViews can report needsDisplay == false even immediately after
        // setting it. Observe the actual invalidation requests instead.
        view.needsDisplay = false
        overlay.needsDisplay = false
        let changes = DirtyChanges()
        let canvasObservation = view.observe(\.needsDisplay, options: [.new]) { _, _ in
            MainActor.assumeIsolated { changes.canvas += 1 }
        }
        let overlayObservation = overlay.observe(\.needsDisplay, options: [.new]) { _, _ in
            MainActor.assumeIsolated { changes.playhead += 1 }
        }
        defer {
            canvasObservation.invalidate()
            overlayObservation.invalidate()
        }
        NotificationCenter.default.post(name: NSWindow.didChangeOcclusionStateNotification, object: window)
        XCTAssertEqual(changes.canvas, 0)
        XCTAssertEqual(changes.playhead, 0)
        window.simulatedOcclusion = .visible
        NotificationCenter.default.post(name: NSWindow.didChangeOcclusionStateNotification, object: window)
        XCTAssertGreaterThan(changes.canvas, 0)
        XCTAssertGreaterThan(changes.playhead, 0)
        view.draw(view.bounds)
        XCTAssertGreaterThan(try XCTUnwrap(bitmap.colorAt(x: 300, y: 10)).alphaComponent, 0)
    }
}
