import AppKit
import SwiftUI

/// Pure decision of what a drag carries. Originals for flat clips/photos; equirect exports for 360 clips.
enum DragPlanner {
    struct Plan: Equatable {
        var urls: [URL] = []
        var needsEquirect: [Asset] = []
    }

    static func url(for asset: Asset, exists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }) -> URL? {
        if asset.is360 {
            guard let e = asset.equirectExport, exists(e.path) else { return nil }
            return URL(fileURLWithPath: e.path)
        }
        return exists(asset.media) ? asset.mediaURL : nil
    }

    static func plan(_ assets: [Asset], exists: (String) -> Bool = { FileManager.default.fileExists(atPath: $0) }) -> Plan {
        var plan = Plan()
        for a in assets {
            if let u = url(for: a, exists: exists) { plan.urls.append(u) } else if a.is360 { plan.needsEquirect.append(a) }
        }
        return plan
    }

    /// Writers for the drag: NSURL puts a real `public.file-url` on the pasteboard.
    static func writers(_ urls: [URL]) -> [NSPasteboardWriting] { urls.map { $0 as NSURL } }
}

struct MenuAction { let title: String; let run: @MainActor () -> Void }

/// One AppKit view that owns clicks, hover position and the multi-file drag session for a tile or row.
/// SwiftUI's `.onDrag` carries a single provider, so real multi-item file drags need NSDraggingSource.
struct DragSurface: NSViewRepresentable {
    var mouseDown: (NSEvent.ModifierFlags, Int) -> Void = { _, _ in }
    var click: (NSEvent.ModifierFlags) -> Void = { _ in }
    /// File URLs to drag; empty means no drag starts.
    var dragURLs: () -> [URL] = { [] }
    var dragImage: () -> NSImage? = { nil }
    var dragEnded: () -> Void = {}
    var hover: (Double?) -> Void = { _ in }
    var menu: () -> [MenuAction] = { [] }

    func makeNSView(context: Context) -> SurfaceView { let v = SurfaceView(); v.spec = self; return v }
    func updateNSView(_ v: SurfaceView, context: Context) { v.spec = self }

    final class SurfaceView: NSView, NSDraggingSource {
        var spec = DragSurface()
        private var downEvent: NSEvent?
        private var dragged = false
        private var tracking: NSTrackingArea?

        override var isFlipped: Bool { true }
        override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

        override func updateTrackingAreas() {
            super.updateTrackingAreas()
            if let tracking { removeTrackingArea(tracking) }
            let t = NSTrackingArea(rect: .zero, options: [.mouseMoved, .mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect], owner: self)
            addTrackingArea(t); tracking = t
        }

        private func fraction(_ e: NSEvent) -> Double {
            let x = convert(e.locationInWindow, from: nil).x
            return min(1, max(0, Double(x / max(1, bounds.width))))
        }
        override func mouseEntered(with e: NSEvent) { spec.hover(fraction(e)) }
        override func mouseMoved(with e: NSEvent) { spec.hover(fraction(e)) }
        override func mouseExited(with e: NSEvent) { spec.hover(nil) }

        override func mouseDown(with e: NSEvent) {
            downEvent = e; dragged = false
            spec.mouseDown(e.modifierFlags, e.clickCount)
        }

        override func mouseUp(with e: NSEvent) {
            if !dragged, e.clickCount < 2 { spec.click(e.modifierFlags) }
        }

        override func mouseDragged(with e: NSEvent) {
            guard !dragged, let down = downEvent else { return }
            let a = down.locationInWindow, b = e.locationInWindow
            guard hypot(a.x - b.x, a.y - b.y) > 4 else { return }
            dragged = true
            let urls = spec.dragURLs()
            guard !urls.isEmpty else { spec.dragEnded(); return }
            let image = spec.dragImage() ?? NSWorkspace.shared.icon(for: .data)
            let size = NSSize(width: 120, height: 120 * image.size.height / max(1, image.size.width))
            let origin = convert(down.locationInWindow, from: nil)
            let items = DragPlanner.writers(urls).enumerated().map { i, writer in
                let item = NSDraggingItem(pasteboardWriter: writer)
                let off = CGFloat(min(i, 4)) * 6
                item.setDraggingFrame(NSRect(x: origin.x - size.width / 2 + off, y: origin.y - size.height / 2 + off,
                                             width: size.width, height: size.height), contents: image)
                return item
            }
            beginDraggingSession(with: items, event: down, source: self)
        }

        func draggingSession(_ session: NSDraggingSession, sourceOperationMaskFor context: NSDraggingContext) -> NSDragOperation { .copy }
        func ignoreModifierKeys(for session: NSDraggingSession) -> Bool { true }
        func draggingSession(_ session: NSDraggingSession, endedAt screenPoint: NSPoint, operation: NSDragOperation) { spec.dragEnded() }

        override func menu(for event: NSEvent) -> NSMenu? {
            spec.mouseDown(event.modifierFlags, 1)
            let actions = spec.menu()
            guard !actions.isEmpty else { return nil }
            let m = NSMenu()
            for action in actions {
                let item = NSMenuItem(title: action.title, action: #selector(runItem(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = MenuBox(action.run)
                m.addItem(item)
            }
            return m
        }

        @objc private func runItem(_ sender: NSMenuItem) { (sender.representedObject as? MenuBox)?.run() }
    }

    final class MenuBox: NSObject {
        let run: @MainActor () -> Void
        init(_ run: @escaping @MainActor () -> Void) { self.run = run }
    }
}
