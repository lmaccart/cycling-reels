import AppKit
import Foundation
import SwiftUI

// MARK: - Pure UX helpers (unit tested)

/// Plain-language names for the two preview modes the service understands.
enum PreviewModeLabel {
    static func title(_ mode: String) -> String {
        mode == "exact" ? "Final quality" : "Fast preview"
    }

    static func help(_ mode: String) -> String {
        mode == "exact"
            ? "Final quality: plays the real rendered movie, so it matches the export. Slower to prepare."
            : "Fast preview: plays the reel live in the editor. Opens quickly and follows your edits; small differences from the final render are possible."
    }
}

/// Timecode / duration formatting shared by every panel.
enum TimeFormat {
    /// `HH:MM:SS:FF` timecode for a frame number.
    static func timecode(_ frame: Int, fps: Double) -> String {
        let rate = max(Int(fps.rounded()), 1)
        let frame = max(0, frame)
        let seconds = frame / rate
        return String(format: "%02d:%02d:%02d:%02d", seconds / 3600, (seconds / 60) % 60, seconds % 60, frame % rate)
    }

    /// `m:ss.t` — one decimal, used for source marks and clip positions.
    static func precise(_ seconds: Double) -> String {
        guard seconds.isFinite else { return "0:00.0" }
        let tenths = max(0, Int((seconds * 10).rounded()))
        return String(format: "%d:%02d.%d", tenths / 600, (tenths / 10) % 60, tenths % 10)
    }

    /// Short `m:ss` or `h:mm:ss`, used for ruler labels and totals.
    static func clock(_ seconds: Double) -> String {
        guard seconds.isFinite else { return "0:00" }
        let total = max(0, Int(seconds.rounded()))
        if total >= 3600 { return String(format: "%d:%02d:%02d", total / 3600, (total / 60) % 60, total % 60) }
        return String(format: "%d:%02d", total / 60, total % 60)
    }
}

/// Zoom, ruler and snapping math for the timeline (pixels per frame = zoom).
enum TimelineMetrics {
    static let minZoom = 0.05
    static let maxZoom = 4.0
    static let defaultZoom = 0.56
    static let snapThresholdPixels = 8.0

    static func clamp(_ zoom: Double) -> Double { min(maxZoom, max(minZoom, zoom.isFinite ? zoom : defaultZoom)) }
    static func zoomIn(_ zoom: Double) -> Double { clamp(zoom * 1.35) }
    static func zoomOut(_ zoom: Double) -> Double { clamp(zoom / 1.35) }

    /// Zoom that makes the whole reel fit in `viewportWidth`, leaving the lane header and a small margin.
    static func fitZoom(durationFrames: Int, viewportWidth: Double, gutter: Double) -> Double {
        let usable = viewportWidth - gutter - 48
        guard durationFrames > 0, usable > 0 else { return defaultZoom }
        return clamp(usable / Double(durationFrames))
    }

    /// Seconds between ruler ticks so labels never crowd (>= ~64 px apart).
    static func rulerStepSeconds(pixelsPerSecond: Double) -> Int {
        let steps = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600]
        return steps.first { Double($0) * pixelsPerSecond >= 64 } ?? 600
    }

    /// The nearest candidate frame within the snap threshold, if any.
    static func snap(frame: Int, candidates: [Int], zoom: Double,
                     thresholdPixels: Double = snapThresholdPixels) -> Int? {
        guard zoom > 0 else { return nil }
        let threshold = thresholdPixels / zoom
        var best: (frame: Int, distance: Double)?
        for candidate in candidates {
            let distance = abs(Double(candidate - frame))
            if distance <= threshold, best == nil || distance < best!.distance {
                best = (candidate, distance)
            }
        }
        return best?.frame
    }

    /// Edge frames of every other clip plus the playhead and the reel start.
    static func snapCandidates(nodes: [EditorNode], excluding id: String?, playhead: Int) -> [Int] {
        var frames: Set<Int> = [0, playhead]
        for node in nodes where node.id != id {
            frames.insert(node.startFrame)
            frames.insert(node.endFrame)
        }
        return frames.sorted()
    }

    /// Edge hit zone in pixels: 7 px, but never more than a third of a narrow clip.
    static func edgeZone(clipWidth: Double) -> Double { max(2, min(7, clipWidth / 3)) }
}

/// Global editing shortcuts. Resolved from raw key events so the mapping can be tested.
enum EditorShortcut: Equatable {
    case togglePlayback, stepBack, stepForward, jumpBack, jumpForward, toStart, toEnd
    case markIn, markOut, insert, split, delete, zoomIn, zoomOut, zoomFit

    /// Frames moved by Shift-arrow, and seconds moved by J / L.
    static let bigStepSeconds = 1.0

    /// Keys a focused list (the Library) also uses, so the list keeps them.
    var isNavigation: Bool {
        [.togglePlayback, .stepBack, .stepForward, .jumpBack, .jumpForward, .toStart, .toEnd, .delete].contains(self)
    }

    static func resolve(keyCode: UInt16, characters: String?, flags: NSEvent.ModifierFlags) -> EditorShortcut? {
        let mods = flags.intersection([.command, .shift, .option, .control])
        let key = characters?.lowercased() ?? ""
        if mods == [.command] {
            switch key {
            case "b": return .split
            case "=", "+": return .zoomIn
            case "-", "_": return .zoomOut
            case "0": return .zoomFit
            default: return nil
            }
        }
        if mods == [.command, .shift], key == "=" || key == "+" { return .zoomIn }
        guard mods.isEmpty || mods == [.shift] else { return nil }
        switch keyCode {
        case 49: return mods.isEmpty ? .togglePlayback : nil
        case 123: return mods.isEmpty ? .stepBack : .jumpBack
        case 124: return mods.isEmpty ? .stepForward : .jumpForward
        case 115: return .toStart
        case 119: return .toEnd
        case 51, 117: return mods.isEmpty ? .delete : nil
        default: break
        }
        guard mods.isEmpty else { return nil }
        switch key {
        case "k": return .togglePlayback
        case "j": return .jumpBack
        case "l": return .jumpForward
        case "i": return .markIn
        case "o": return .markOut
        case ",": return .insert
        default: return nil
        }
    }
}

extension Notification.Name {
    static let reelsMarkSourceIn = Notification.Name("ReelsEditor.MarkSourceIn")
    static let reelsMarkSourceOut = Notification.Name("ReelsEditor.MarkSourceOut")
}

// MARK: - Store helpers (computed / methods only)

extension EditorStore {
    /// A reel that is open but has no clips yet.
    var isReelEmpty: Bool {
        guard let snapshot else { return false }
        return !snapshot.nodes.contains { $0.kind.lowercased().contains("segment") }
    }

    /// `plans/<slug>.json`: the real file this reel saves to.
    var planFileLabel: String? {
        if let openPlanPath { return openPlanPath }
        guard let slug = snapshot?.plan["slug"]?.stringValue, !slug.isEmpty else { return nil }
        return "plans/\(slug).json"
    }

    var reelTitle: String {
        if let hook = snapshot?.plan["hook"]?["text"]?.stringValue,
           !hook.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return hook }
        return snapshot?.plan["slug"]?.stringValue ?? "Reels Editor"
    }

    func seek(bySeconds seconds: Double) {
        setPlayhead(playheadFrame + Int((seconds * fps).rounded()))
    }

    /// Insert from the Source monitor: the marked range when there is one, otherwise the whole selected clip.
    func insertFromSource() {
        guard hasDocument else { return }
        if sourceIn != nil, sourceOut != nil {
            insertSourceSelection()
        } else if let id = selectedAssetID {
            insertDroppedItem(id)
        } else {
            notice = "Select footage in the Library first."
        }
    }

    func performShortcut(_ shortcut: EditorShortcut) {
        switch shortcut {
        case .togglePlayback: NotificationCenter.default.post(name: .reelsTogglePlayback, object: nil)
        case .stepBack: setPlayhead(playheadFrame - 1)
        case .stepForward: setPlayhead(playheadFrame + 1)
        case .jumpBack: seek(bySeconds: -EditorShortcut.bigStepSeconds)
        case .jumpForward: seek(bySeconds: EditorShortcut.bigStepSeconds)
        case .toStart: setPlayhead(0)
        case .toEnd: setPlayhead(snapshot?.durationFrames ?? 0)
        case .markIn: NotificationCenter.default.post(name: .reelsMarkSourceIn, object: nil)
        case .markOut: NotificationCenter.default.post(name: .reelsMarkSourceOut, object: nil)
        case .insert: insertFromSource()
        case .split: splitAtPlayhead()
        case .delete: deleteSelection()
        case .zoomIn: NotificationCenter.default.post(name: .reelsTimelineZoom, object: "in")
        case .zoomOut: NotificationCenter.default.post(name: .reelsTimelineZoom, object: "out")
        case .zoomFit: NotificationCenter.default.post(name: .reelsTimelineZoom, object: "fit")
        }
    }
}

extension Notification.Name {
    static let reelsTimelineZoom = Notification.Name("ReelsEditor.TimelineZoom")
}

// MARK: - Global shortcut monitor

/// Installs one local key monitor for the window it lives in and removes it when the view goes away.
/// Keys are ignored while any text field or sheet is active so typing is never hijacked.
struct ShortcutMonitor: NSViewRepresentable {
    let handler: @MainActor (EditorShortcut) -> Void

    func makeCoordinator() -> Coordinator { Coordinator() }

    func makeNSView(context: Context) -> NSView {
        let view = NSView(frame: .zero)
        context.coordinator.install(for: view, handler: handler)
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        context.coordinator.handler = handler
    }

    static func dismantleNSView(_ view: NSView, coordinator: Coordinator) {
        coordinator.remove()
    }

    @MainActor
    final class Coordinator {
        var handler: (@MainActor (EditorShortcut) -> Void)?
        private var token: Any?

        func install(for view: NSView, handler: @escaping @MainActor (EditorShortcut) -> Void) {
            self.handler = handler
            guard token == nil else { return }
            token = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self, weak view] event in
                nonisolated(unsafe) let event = event
                let consumed = MainActor.assumeIsolated { () -> Bool in
                    guard let self, let view, let window = view.window,
                          event.window === window, window.attachedSheet == nil,
                          !(window.firstResponder is NSText) else { return false }
                    guard let shortcut = EditorShortcut.resolve(
                        keyCode: event.keyCode, characters: event.charactersIgnoringModifiers, flags: event.modifierFlags
                    ) else { return false }
                    if window.firstResponder is NSTableView, shortcut.isNavigation { return false }
                    self.handler?(shortcut)
                    return true
                }
                return consumed ? nil : event
            }
        }

        func remove() {
            if let token { NSEvent.removeMonitor(token) }
            token = nil
        }
    }
}
