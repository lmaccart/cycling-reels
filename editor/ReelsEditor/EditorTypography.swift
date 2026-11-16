import SwiftUI
import AppKit

/// Pro video-editor type scale (Premiere/Resolve feel): compact SF Pro for UI,
/// SF Mono for timecode and numbers, small caps-style tracked headers for panels.
/// Every panel uses these tokens instead of ad-hoc `.font(.system(size:))`.
enum EditorType {
    /// Panel title bars: "PROGRAM", "TIMELINE", "EFFECT CONTROLS".
    static let panelTitle = Font.system(size: 10.5, weight: .semibold).width(.condensed)
    static let panelTitleTracking: CGFloat = 0.8
    /// Section headers inside panels ("Motion", "Opacity", "Audio").
    static let section = Font.system(size: 11.5, weight: .semibold)
    /// Normal control labels and row text.
    static let body = Font.system(size: 11.5)
    static let bodyEmphasis = Font.system(size: 11.5, weight: .medium)
    /// Secondary/hint text.
    static let caption = Font.system(size: 10.5)
    /// Clip names on timeline clips.
    static let clipLabel = Font.system(size: 10.5, weight: .semibold).width(.condensed)
    /// Track header names: V1, V2, A1, A2.
    static let trackHeader = Font.system(size: 11, weight: .bold).width(.condensed)
    /// Big timecode readouts (program monitor, timeline header) — blue in the timeline.
    static let timecodeLarge = Font.system(size: 15, weight: .medium, design: .monospaced)
    /// Small timecode/number fields.
    static let timecode = Font.system(size: 10.5, weight: .regular, design: .monospaced)
    /// Ruler tick labels.
    static let ruler = Font.system(size: 9.5, weight: .regular, design: .monospaced)

    // AppKit equivalents for the NSView-drawn timeline.
    static var nsClipLabel: NSFont { NSFont.systemFont(ofSize: 10.5, weight: .semibold) }
    static var nsTrackHeader: NSFont { NSFont.systemFont(ofSize: 11, weight: .bold) }
    static var nsRuler: NSFont { NSFont.monospacedSystemFont(ofSize: 9.5, weight: .regular) }
    static var nsTimecode: NSFont { NSFont.monospacedSystemFont(ofSize: 10.5, weight: .medium) }
}

/// Uppercase tracked panel header, e.g. `PanelTitle("Effect Controls")`.
struct PanelTitle: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text.uppercased())
            .font(EditorType.panelTitle)
            .tracking(EditorType.panelTitleTracking)
            .foregroundStyle(.secondary)
    }
}

// Timecode strings: use the existing `TimeFormat.timecode(frame, fps:)` (EditorStore+UX.swift).
