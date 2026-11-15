import AppKit
@preconcurrency import AVFoundation
import SwiftUI

enum TimelineLaneClassifier {
    static func category(for node: EditorNode) -> String {
        let text = [
            node.kind, node.lane,
            node.raw["type"]?.stringValue ?? "",
            node.raw["kind"]?.stringValue ?? "",
            node.raw["assetKind"]?.stringValue ?? "",
            node.raw["role"]?.stringValue ?? ""
        ].joined(separator: " ").lowercased()
        if ["audio", "sound", "music", "bleep", "sfx", "voice", "dialog", "narrat", "speech"]
            .contains(where: text.contains) { return "Audio" }
        if text.contains("cutaway") || text.contains("broll") || text.contains("b-roll") { return "B-roll" }
        if text.contains("caption") { return "Captions" }
        if ["text", "overlay", "hook", "exhibit", "effect", "punch", "blur", "camera", "route", "progress", "stats", "card"]
            .contains(where: text.contains) { return "Text & graphics" }
        if text.contains("segment") || text.contains("video") || text.contains("photo") || text.contains("image") || text.contains("primary") { return "Video" }
        return "Other"
    }

    static func visibleInTimeline(_ node: EditorNode) -> Bool {
        // Pages are the timeline unit; their words/settings remain available
        // in the Inspector without drawing overlapping duplicate clip strips.
        !["caption_word", "caption_settings"].contains(node.kind.lowercased())
    }
}

struct TimelinePanel: View {
    @Bindable var store: EditorStore
    @State private var zoom = TimelineMetrics.defaultZoom
    @State private var snapping = true
    @State private var viewportWidth: CGFloat = 0

    private static let gutter = 126.0

    private var contentWidth: CGFloat {
        max(650, CGFloat(Self.gutter) + CGFloat(store.snapshot?.durationFrames ?? 0) * zoom + 120)
    }
    private var trackHeight: CGFloat {
        max(164, 28 + CGFloat(TimelineCanvas.lanes(for: store.snapshot).count) * 38)
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text("Timeline")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(.secondary)
                Rectangle().fill(EditorPalette.separator).frame(width: 1, height: 18)
                Button { store.setPlayhead(store.playheadFrame - 1) } label: { Image(systemName: "backward.frame") }
                    .help("Previous frame  ←")
                    .accessibilityLabel("Previous frame")
                Button {
                    NotificationCenter.default.post(name: .reelsTogglePlayback, object: nil)
                } label: { Image(systemName: "playpause.fill") }
                    .help("Play / pause  Space or K")
                    .accessibilityLabel("Play or pause")
                Button { store.setPlayhead(store.playheadFrame + 1) } label: { Image(systemName: "forward.frame") }
                    .help("Next frame  →")
                    .accessibilityLabel("Next frame")
                Text(TimeFormat.timecode(store.playheadFrame, fps: store.fps))
                    .font(.system(size: 12, design: .monospaced))
                    .monospacedDigit()
                    .frame(minWidth: 92, alignment: .leading)
                    .help("Playhead timecode (hours:minutes:seconds:frames)")
                Button { store.splitAtPlayhead() } label: { Image(systemName: "scissors") }
                    .help("Split the selected clip at the playhead  ⌘B")
                    .accessibilityLabel("Split at playhead")
                    .disabled(store.selectedNode == nil)
                Button { store.deleteSelection() } label: { Image(systemName: "trash") }
                    .help("Delete the selected clip  ⌫")
                    .accessibilityLabel("Delete selection")
                    .disabled(store.selectedIDs.isEmpty)
                Spacer(minLength: 8)
                if let node = store.selectedNode {
                    Text(node.title)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .frame(maxWidth: 200, alignment: .trailing)
                }
                Toggle(isOn: $snapping) { Image(systemName: "magnet") }
                    .toggleStyle(.button)
                    .help("Snap edges to the playhead and neighboring clips")
                    .accessibilityLabel("Snapping")
                Button { zoom = TimelineMetrics.zoomOut(zoom) } label: { Image(systemName: "minus.magnifyingglass") }
                    .help("Zoom out  ⌘−")
                    .accessibilityLabel("Zoom out")
                Slider(value: Binding(
                    get: { log(zoom) },
                    set: { zoom = TimelineMetrics.clamp(exp($0)) }
                ), in: log(TimelineMetrics.minZoom)...log(TimelineMetrics.maxZoom))
                    .frame(width: 90)
                    .accessibilityLabel("Timeline zoom")
                    .help("Timeline zoom")
                Button { zoom = TimelineMetrics.zoomIn(zoom) } label: { Image(systemName: "plus.magnifyingglass") }
                    .help("Zoom in  ⌘+")
                    .accessibilityLabel("Zoom in")
                Button { fit() } label: { Image(systemName: "arrow.left.and.right.square") }
                    .help("Fit the whole reel in view  ⌘0")
                    .accessibilityLabel("Fit reel to window")
                    .disabled(!store.hasDocument)
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
            .padding(.horizontal, 10)
            .frame(height: 38)
            .background(EditorPalette.panel)
            Divider()

            GeometryReader { proxy in
                ScrollView(.horizontal) {
                    TimelineCanvasRepresentable(
                        snapshot: store.snapshot,
                        selectedIDs: store.selectedIDs,
                        changedIDs: store.recentlyChangedIDs,
                        sourceURLs: store.timelineSourceURLs,
                        playheadFrame: store.playheadFrame,
                        zoom: zoom,
                        snapping: snapping,
                        onSelect: { id, additive in store.selectNode(id, additive: additive) },
                        onBeginGesture: { owner in await store.acquireTimelineLease(owner: owner) },
                        onScrub: { frame, final in
                            store.setPlayhead(frame, final: final)
                        },
                        onTrim: { node, sourceIn, sourceOut, lease in store.trim(node: node, sourceIn: sourceIn, sourceOut: sourceOut, lease: lease) },
                        onReorder: { node, frame, beforeID, lease in
                            store.moveTimelineItem(node, toFrame: frame, beforeID: beforeID, lease: lease)
                        },
                        onDelete: { store.deleteSelection() },
                        onSplit: { store.splitAtPlayhead() }
                    )
                    .frame(width: max(contentWidth, proxy.size.width), height: max(trackHeight, proxy.size.height))
                    .dropDestination(for: String.self) { items, _ in
                        guard let first = items.first else { return false }
                        store.insertDroppedItem(first)
                        return true
                    }
                }
                .scrollIndicators(.visible)
                .onAppear { viewportWidth = proxy.size.width }
                .onChange(of: proxy.size.width) { _, width in viewportWidth = width }
            }
            .background(EditorPalette.canvas)
            .overlay {
                if !store.hasDocument {
                    ContentUnavailableView("No reel open", systemImage: "timeline.selection",
                                           description: Text("Pick a reel from the Reels list on the left. Its clips and timing appear here."))
                        .allowsHitTesting(false)
                } else if store.isReelEmpty {
                    emptyReel
                }
            }
        }
        .background(EditorPalette.panel)
        .onReceive(NotificationCenter.default.publisher(for: .reelsTimelineZoom)) { note in
            switch note.object as? String {
            case "in": zoom = TimelineMetrics.zoomIn(zoom)
            case "out": zoom = TimelineMetrics.zoomOut(zoom)
            default: fit()
            }
        }
    }

    private func fit() {
        zoom = TimelineMetrics.fitZoom(durationFrames: store.snapshot?.durationFrames ?? 0,
                                       viewportWidth: Double(viewportWidth), gutter: Self.gutter)
    }

    private var emptyReel: some View {
        VStack(spacing: 8) {
            Image(systemName: "film.stack").font(.system(size: 26)).foregroundStyle(.secondary)
            Text("This reel has no clips yet").font(.system(size: 14, weight: .semibold))
            Text("Drag clips from the Library onto the timeline, or mark a range in Source with I and O and press , to insert it.")
                .font(.system(size: 12))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)
            Button {
                store.insertFromSource()
            } label: {
                Label("Insert selected clip", systemImage: "arrow.down.to.line")
            }
            .controlSize(.regular)
            .disabled(store.selectedAssetID == nil)
            .help(store.selectedAssetID == nil ? "Select a clip in the Library first" : "Insert the selected library clip")
        }
        .padding(.leading, 126)
        .padding(.top, 20)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct TimelineCanvasRepresentable: NSViewRepresentable {
    let snapshot: EditorSnapshot?
    let selectedIDs: Set<String>
    let changedIDs: Set<String>
    let sourceURLs: [String: URL]
    let playheadFrame: Int
    let zoom: Double
    let snapping: Bool
    let onSelect: (String, Bool) -> Void
    let onBeginGesture: (String) async -> String?
    let onScrub: (Int, Bool) -> Void
    let onTrim: (EditorNode, Double, Double, String?) -> Void
    let onReorder: (EditorNode, Int, String?, String?) -> Void
    let onDelete: () -> Void
    let onSplit: () -> Void

    func makeNSView(context: Context) -> TimelineCanvas {
        let view = TimelineCanvas()
        view.update(
            snapshot: snapshot, selectedIDs: selectedIDs, changedIDs: changedIDs, sourceURLs: sourceURLs,
            playheadFrame: playheadFrame, zoom: zoom, snapping: snapping,
            onSelect: onSelect, onBeginGesture: onBeginGesture, onScrub: onScrub,
            onTrim: onTrim, onReorder: onReorder, onDelete: onDelete, onSplit: onSplit
        )
        return view
    }

    func updateNSView(_ view: TimelineCanvas, context: Context) {
        view.update(
            snapshot: snapshot, selectedIDs: selectedIDs, changedIDs: changedIDs, sourceURLs: sourceURLs,
            playheadFrame: playheadFrame, zoom: zoom, snapping: snapping,
            onSelect: onSelect, onBeginGesture: onBeginGesture, onScrub: onScrub,
            onTrim: onTrim, onReorder: onReorder, onDelete: onDelete, onSplit: onSplit
        )
    }
}

@MainActor
private final class TimelineCanvas: NSView {
    private static let labelWidth: CGFloat = 126
    private static let rulerHeight: CGFloat = 28
    private static let rowHeight: CGFloat = 38
    private var snapshot: EditorSnapshot?
    private var selectedIDs: Set<String> = []
    private var changedIDs: Set<String> = []
    private var sourceURLs: [String: URL] = [:]
    private let thumbnailCache = NSCache<NSString, NSImage>()
    private var pendingThumbnails: Set<String> = []
    private var waveformCache: [String: [Float]] = [:]
    private var pendingWaveforms: Set<String> = []
    private var playheadFrame = 0
    private var zoom = TimelineMetrics.defaultZoom
    private var snapping = true
    private var snapGuideFrame: Int?
    private var scrollObserver: NSObjectProtocol?
    private var onSelect: ((String, Bool) -> Void)?
    private var onScrub: ((Int, Bool) -> Void)?
    private var onTrim: ((EditorNode, Double, Double, String?) -> Void)?
    private var onReorder: ((EditorNode, Int, String?, String?) -> Void)?
    private var onDelete: (() -> Void)?
    private var onSplit: (() -> Void)?
    private var mode: DragMode?
    private var downPoint = CGPoint.zero
    private var activeNode: EditorNode?
    private var originalStart: Int = 0
    private var originalEnd: Int = 0
    private var ghostStart: Int?
    private var ghostEnd: Int?
    private var targetNodeID: String?
    private var playheadLayer: PlayheadLayer?

    private enum DragMode: Equatable { case scrub, trimStart, trimEnd, move }
    private var onBeginGesture: ((String) async -> String?)?
    private var gestureLeaseTask: Task<String?, Never>?
    private var gestureLease: String?

    override var acceptsFirstResponder: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        thumbnailCache.countLimit = 180
        setAccessibilityElement(true)
        setAccessibilityRole(.group)
        setAccessibilityLabel("Reel timeline")
        setAccessibilityHelp("Select a clip, drag its edges to trim, drag a clip to reorder, or click the ruler to move the playhead.")
        let overlay = PlayheadLayer(frame: bounds)
        overlay.autoresizingMask = [.width, .height]
        overlay.setAccessibilityElement(false)
        addSubview(overlay)
        playheadLayer = overlay
    }

    required init?(coder: NSCoder) { nil }

    func update(snapshot: EditorSnapshot?, selectedIDs: Set<String>, changedIDs: Set<String>,
                sourceURLs: [String: URL],
                playheadFrame: Int, zoom: Double, snapping: Bool,
                onSelect: @escaping (String, Bool) -> Void,
                onBeginGesture: @escaping (String) async -> String?,
                onScrub: @escaping (Int, Bool) -> Void,
                onTrim: @escaping (EditorNode, Double, Double, String?) -> Void,
                onReorder: @escaping (EditorNode, Int, String?, String?) -> Void,
                onDelete: @escaping () -> Void,
                onSplit: @escaping () -> Void) {
        let contentsChanged = self.snapshot != snapshot
            || self.selectedIDs != selectedIDs
            || self.changedIDs != changedIDs
            || self.sourceURLs != sourceURLs
            || self.zoom != zoom
        self.snapshot = snapshot
        self.selectedIDs = selectedIDs
        self.changedIDs = changedIDs
        self.sourceURLs = sourceURLs
        self.playheadFrame = playheadFrame
        self.zoom = zoom
        self.snapping = snapping
        self.onSelect = onSelect
        self.onBeginGesture = onBeginGesture
        self.onScrub = onScrub
        self.onTrim = onTrim
        self.onReorder = onReorder
        self.onDelete = onDelete
        self.onSplit = onSplit
        if contentsChanged {
            needsDisplay = true
            window?.invalidateCursorRects(for: self)
        }
        playheadLayer?.update(frame: playheadFrame, zoom: zoom, labelWidth: Self.labelWidth,
                              rulerHeight: Self.rulerHeight, fps: snapshot?.fps ?? 30)
        setAccessibilityChildren(makeAccessibilityChildren())
    }

    static func lanes(for snapshot: EditorSnapshot?) -> [String] {
        guard let snapshot else { return ["Video"] }
        let used = Set(snapshot.nodes.filter(TimelineLaneClassifier.visibleInTimeline).map(category(for:)))
        let order = ["Video", "B-roll", "Captions", "Text & graphics", "Audio", "Other"]
        let lanes = order.filter { used.contains($0) }
        return lanes.isEmpty ? ["Video"] : lanes
    }

    private static func category(for node: EditorNode) -> String {
        TimelineLaneClassifier.category(for: node)
    }

    private func category(for node: EditorNode) -> String { Self.category(for: node) }
    private var lanes: [String] { Self.lanes(for: snapshot) }
    private var scale: CGFloat { CGFloat(zoom) }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        NSColor.windowBackgroundColor.setFill()
        dirtyRect.fill()
        drawTracks()
        drawClips(dirtyRect)
        drawSnapGuide()
        drawRuler()
        drawGutter()
    }

    /// The lane header column stays pinned to the left edge while the timeline scrolls.
    private func drawGutter() {
        let left = visibleRect.minX
        let rulerBottom = bounds.maxY - Self.rulerHeight
        NSColor.controlBackgroundColor.setFill()
        NSRect(x: left, y: 0, width: Self.labelWidth, height: bounds.height).fill()
        let nameAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 11, weight: .medium),
            .foregroundColor: NSColor.labelColor.withAlphaComponent(0.82)
        ]
        for (index, lane) in lanes.enumerated() {
            let y = rowTop(index) - Self.rowHeight
            color(for: lane).setFill()
            NSBezierPath(roundedRect: NSRect(x: left + 10, y: y + Self.rowHeight / 2 - 4, width: 8, height: 8), xRadius: 2, yRadius: 2).fill()
            NSAttributedString(string: lane, attributes: nameAttrs)
                .draw(at: CGPoint(x: left + 24, y: y + Self.rowHeight / 2 - 7))
            NSColor.separatorColor.withAlphaComponent(0.38).setStroke()
            let rule = NSBezierPath()
            rule.move(to: CGPoint(x: left, y: y)); rule.line(to: CGPoint(x: left + Self.labelWidth, y: y))
            rule.lineWidth = 0.5; rule.stroke()
        }
        NSColor.separatorColor.setStroke()
        let edge = NSBezierPath()
        edge.move(to: CGPoint(x: left + Self.labelWidth - 0.5, y: 0))
        edge.line(to: CGPoint(x: left + Self.labelWidth - 0.5, y: rulerBottom))
        edge.lineWidth = 1
        edge.stroke()
        // ruler corner
        NSColor.controlBackgroundColor.setFill()
        NSRect(x: left, y: rulerBottom, width: Self.labelWidth, height: Self.rulerHeight).fill()
        NSAttributedString(string: "Lanes", attributes: [
            .font: NSFont.systemFont(ofSize: 11, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor
        ]).draw(at: CGPoint(x: left + 10, y: rulerBottom + 7))
    }

    private func drawSnapGuide() {
        guard let frame = snapGuideFrame else { return }
        let x = Self.labelWidth + CGFloat(frame) * scale
        NSColor.systemYellow.withAlphaComponent(0.9).setStroke()
        let path = NSBezierPath()
        path.move(to: CGPoint(x: x, y: 0)); path.line(to: CGPoint(x: x, y: bounds.maxY - Self.rulerHeight))
        path.lineWidth = 1
        path.setLineDash([3, 3], count: 2, phase: 0)
        path.stroke()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if let scrollObserver { NotificationCenter.default.removeObserver(scrollObserver) }
        scrollObserver = nil
        guard window != nil, let clip = enclosingScrollView?.contentView else { return }
        clip.postsBoundsChangedNotifications = true
        scrollObserver = NotificationCenter.default.addObserver(
            forName: NSView.boundsDidChangeNotification, object: clip, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.needsDisplay = true }
        }
    }

    override func resetCursorRects() {
        super.resetCursorRects()
        guard let snapshot else { return }
        for node in snapshot.nodes where TimelineLaneClassifier.visibleInTimeline(node) && !node.isReadOnly {
            guard let laneIndex = lanes.firstIndex(of: category(for: node)),
                  node.kind.lowercased() != "generated_sfx" else { continue }
            let x0 = Self.labelWidth + CGFloat(node.startFrame) * scale
            let x1 = Self.labelWidth + CGFloat(node.endFrame) * scale
            let zone = CGFloat(TimelineMetrics.edgeZone(clipWidth: Double(x1 - x0)))
            let y = rowTop(laneIndex) - Self.rowHeight
            addCursorRect(NSRect(x: x0 - zone, y: y, width: zone * 2, height: Self.rowHeight), cursor: .resizeLeftRight)
            addCursorRect(NSRect(x: x1 - zone, y: y, width: zone * 2, height: Self.rowHeight), cursor: .resizeLeftRight)
        }
    }

    private func drawRuler() {
        let ruler = NSRect(x: 0, y: bounds.maxY - Self.rulerHeight, width: bounds.width, height: Self.rulerHeight)
        NSColor.controlBackgroundColor.setFill()
        ruler.fill()
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedDigitSystemFont(ofSize: 10, weight: .regular),
            .foregroundColor: NSColor.secondaryLabelColor
        ]
        let fps = max(snapshot?.fps ?? 30, 1)
        let pixelsPerSecond = Double(scale) * fps
        let interval = TimelineMetrics.rulerStepSeconds(pixelsPerSecond: pixelsPerSecond)
        let duration = snapshot?.durationFrames ?? 0
        let seconds = Int(ceil(Double(duration) / fps))
        let last = max(seconds, 10) + interval
        for second in stride(from: 0, through: last, by: interval) {
            let x = Self.labelWidth + CGFloat(Double(second) * pixelsPerSecond)
            guard x <= bounds.maxX, x >= visibleRect.minX - 60, x <= visibleRect.maxX else { continue }
            let path = NSBezierPath()
            path.move(to: CGPoint(x: x, y: ruler.minY))
            path.line(to: CGPoint(x: x, y: bounds.maxY))
            NSColor.separatorColor.withAlphaComponent(0.3).setStroke()
            path.lineWidth = 0.5
            path.stroke()
            NSAttributedString(string: TimeFormat.clock(Double(second)), attributes: attrs)
                .draw(at: CGPoint(x: x + 4, y: ruler.minY + 8))
        }
        let border = NSBezierPath()
        border.move(to: CGPoint(x: 0, y: ruler.minY))
        border.line(to: CGPoint(x: bounds.width, y: ruler.minY))
        NSColor.separatorColor.setStroke()
        border.stroke()
    }

    private func drawTracks() {
        for (index, lane) in lanes.enumerated() {
            let y = rowTop(index) - Self.rowHeight
            let rect = NSRect(x: 0, y: y, width: bounds.width, height: Self.rowHeight)
            (index.isMultiple(of: 2) ? NSColor.textBackgroundColor : NSColor.controlBackgroundColor.withAlphaComponent(0.55)).setFill()
            rect.fill()
            let border = NSBezierPath()
            border.move(to: CGPoint(x: 0, y: y))
            border.line(to: CGPoint(x: bounds.width, y: y))
            NSColor.separatorColor.withAlphaComponent(0.38).setStroke()
            border.lineWidth = 0.5
            border.stroke()
        }
    }

    private func drawClips(_ dirtyRect: NSRect) {
        guard let snapshot else { return }
        for node in snapshot.nodes.filter(TimelineLaneClassifier.visibleInTimeline) {
            guard let laneIndex = lanes.firstIndex(of: category(for: node)) else { continue }
            let start = node.id == activeNode?.id ? (ghostStart ?? node.startFrame) : node.startFrame
            let end = node.id == activeNode?.id ? (ghostEnd ?? node.endFrame) : node.endFrame
            let x = Self.labelWidth + CGFloat(start) * scale
            let width = max(4, CGFloat(max(1, end - start)) * scale)
            let y = rowTop(laneIndex) - Self.rowHeight + 4
            let rect = NSRect(x: x, y: y, width: width, height: Self.rowHeight - 8)
            guard rect.intersects(dirtyRect) else { continue }
            let color = color(for: category(for: node))
            color.withAlphaComponent(selectedIDs.contains(node.id) ? 0.9 : 0.68).setFill()
            NSBezierPath(roundedRect: rect, xRadius: 3, yRadius: 3).fill()
            if category(for: node) == "Audio" {
                drawWaveform(for: node, in: rect)
            } else if ["Video", "B-roll"].contains(category(for: node)) {
                drawThumbnails(for: node, in: rect)
            }
            if selectedIDs.contains(node.id) {
                NSColor.controlAccentColor.setStroke()
                let outline = NSBezierPath(roundedRect: rect.insetBy(dx: 0.5, dy: 0.5), xRadius: 3, yRadius: 3)
                outline.lineWidth = 1.5
                outline.stroke()
            }
            if changedIDs.contains(node.id) {
                NSColor.systemYellow.setStroke()
                let outline = NSBezierPath(roundedRect: rect.insetBy(dx: 1, dy: 1), xRadius: 3, yRadius: 3)
                outline.setLineDash([3, 2], count: 2, phase: 0)
                outline.lineWidth = 1.4
                outline.stroke()
            }
            if !node.isReadOnly {
                let handleColor = NSColor.white.withAlphaComponent(0.65)
                handleColor.setFill()
                NSBezierPath(roundedRect: NSRect(x: rect.minX, y: rect.minY + 5, width: 2, height: rect.height - 10), xRadius: 1, yRadius: 1).fill()
                NSBezierPath(roundedRect: NSRect(x: rect.maxX - 2, y: rect.minY + 5, width: 2, height: rect.height - 10), xRadius: 1, yRadius: 1).fill()
            }
            if width > 36 {
                let font = NSFont.systemFont(ofSize: 11, weight: .medium)
                let textRect = rect.insetBy(dx: 8, dy: 0)
                let para = NSMutableParagraphStyle()
                para.lineBreakMode = .byTruncatingTail
                let attrs: [NSAttributedString.Key: Any] = [
                    .font: font, .foregroundColor: NSColor.white, .paragraphStyle: para
                ]
                let name = NSAttributedString(string: node.title, attributes: attrs)
                let size = name.size()
                let drawn = NSRect(x: textRect.minX, y: rect.midY - size.height / 2,
                                   width: min(size.width, textRect.width), height: size.height)
                // dark scrim keeps the label readable over thumbnails and waveforms
                NSColor.black.withAlphaComponent(0.5).setFill()
                NSBezierPath(roundedRect: drawn.insetBy(dx: -4, dy: -1), xRadius: 3, yRadius: 3).fill()
                name.draw(in: drawn)
            }
            if targetNodeID == node.id {
                NSColor.white.withAlphaComponent(0.85).setStroke()
                let guide = NSBezierPath()
                guide.move(to: CGPoint(x: rect.minX, y: rect.minY - 2))
                guide.line(to: CGPoint(x: rect.minX, y: rect.maxY + 2))
                guide.lineWidth = 2
                guide.stroke()
            }
        }
    }

    private func rowTop(_ index: Int) -> CGFloat {
        bounds.maxY - Self.rulerHeight - CGFloat(index) * Self.rowHeight
    }

    private func color(for lane: String) -> NSColor {
        switch lane {
        case "Audio": NSColor.systemGreen
        case "B-roll": NSColor.systemTeal
        case "Captions": NSColor.systemIndigo
        case "Text & graphics": NSColor.systemPurple
        case "Other": NSColor.systemGray
        default: NSColor.systemBlue
        }
    }

    private func drawThumbnails(for node: EditorNode, in rect: NSRect) {
        guard let assetID = node.assetId, sourceURLs[assetID] != nil else { return }
        let tileWidth: CGFloat = 92
        let tileCount = min(6, max(1, Int(rect.width / tileWidth)))
        let sourceIn = node.sourceIn ?? 0
        let sourceOut = max(sourceIn, node.sourceOut ?? sourceIn)
        let clip = NSBezierPath(roundedRect: rect, xRadius: 3, yRadius: 3)
        NSGraphicsContext.saveGraphicsState()
        clip.addClip()
        for tile in 0..<tileCount {
            let fraction = (Double(tile) + 0.5) / Double(tileCount)
            let seconds = sourceIn + (sourceOut - sourceIn) * fraction
            let key = "\(assetID):\(Int((seconds * 2).rounded()))"
            let tileRect = NSRect(
                x: rect.minX + CGFloat(tile) * rect.width / CGFloat(tileCount),
                y: rect.minY,
                width: rect.width / CGFloat(tileCount) + 1,
                height: rect.height
            )
            if let image = thumbnailCache.object(forKey: key as NSString) {
                image.draw(in: tileRect, from: .zero, operation: .sourceOver, fraction: 0.72)
            } else if Self.isStillImage(sourceURLs[assetID]!) {
                requestPhotoThumbnail(url: sourceURLs[assetID]!, key: key)
            } else {
                requestThumbnail(url: sourceURLs[assetID]!, seconds: seconds, key: key)
            }
        }
        NSGraphicsContext.restoreGraphicsState()
    }

    private func requestThumbnail(url: URL, seconds: Double, key: String) {
        guard pendingThumbnails.insert(key).inserted else { return }
        Task { [weak self] in
            let image = await Task.detached(priority: .utility) {
                let asset = AVURLAsset(url: url)
                let generator = AVAssetImageGenerator(asset: asset)
                generator.appliesPreferredTrackTransform = true
                generator.maximumSize = CGSize(width: 184, height: 64)
                let time = CMTime(seconds: max(0, seconds), preferredTimescale: 600)
                guard let cgImage = try? generator.copyCGImage(at: time, actualTime: nil) else { return nil as CGImage? }
                return cgImage
            }.value
            guard let self else { return }
            self.pendingThumbnails.remove(key)
            if let image {
                self.thumbnailCache.setObject(NSImage(cgImage: image, size: .zero), forKey: key as NSString)
                self.needsDisplay = true
            }
        }
    }

    private static func isStillImage(_ url: URL) -> Bool {
        ["jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "webp", "gif"]
            .contains(url.pathExtension.lowercased())
    }

    private func requestPhotoThumbnail(url: URL, key: String) {
        guard pendingThumbnails.insert(key).inserted else { return }
        Task { [weak self] in
            let data = await Task.detached(priority: .utility) {
                try? Data(contentsOf: url)
            }.value
            guard let self else { return }
            self.pendingThumbnails.remove(key)
            if let data, let image = NSImage(data: data) {
                self.thumbnailCache.setObject(image, forKey: key as NSString)
                self.needsDisplay = true
            }
        }
    }

    private func drawWaveform(for node: EditorNode, in rect: NSRect) {
        guard let assetID = node.assetId, let url = sourceURLs[assetID] else { return }
        let sourceIn = node.sourceIn ?? 0
        let sourceOut = max(sourceIn + 0.05, node.sourceOut ?? sourceIn + Double(node.frames) / (snapshot?.fps ?? 30))
        let key = "\(assetID):\(sourceIn.rounded(toPlaces: 2)):\(sourceOut.rounded(toPlaces: 2))"
        guard let peaks = waveformCache[key] else {
            requestWaveform(url: url, from: sourceIn, to: sourceOut, key: key)
            return
        }
        let center = rect.midY
        let path = NSBezierPath()
        path.lineWidth = max(0.8, rect.width / CGFloat(max(peaks.count, 1)) * 0.62)
        for (index, peak) in peaks.enumerated() {
            let x = rect.minX + CGFloat(index) * rect.width / CGFloat(peaks.count)
            let amplitude = max(1, CGFloat(peak) * (rect.height - 5) * 0.46)
            path.move(to: CGPoint(x: x, y: center - amplitude))
            path.line(to: CGPoint(x: x, y: center + amplitude))
        }
        NSColor.white.withAlphaComponent(0.78).setStroke()
        path.stroke()
    }

    private func requestWaveform(url: URL, from: Double, to: Double, key: String) {
        guard pendingWaveforms.insert(key).inserted else { return }
        Task { [weak self] in
            let peaks = await Task.detached(priority: .utility) {
                await extractAudioWaveform(url: url, from: from, to: to)
            }.value
            guard let self else { return }
            self.pendingWaveforms.remove(key)
            if let peaks {
                self.waveformCache[key] = peaks
                if self.waveformCache.count > 32,
                   let firstKey = self.waveformCache.keys.first {
                    self.waveformCache.removeValue(forKey: firstKey)
                }
                self.needsDisplay = true
            }
        }
    }

    private func makeAccessibilityChildren() -> [NSAccessibilityElement] {
        guard let snapshot else { return [] }
        return snapshot.nodes.filter(TimelineLaneClassifier.visibleInTimeline).map { node in
            let element = NSAccessibilityElement()
            element.setAccessibilityParent(self)
            element.setAccessibilityRole(.button)
            let start = timecode(node.startFrame, fps: snapshot.fps)
            element.setAccessibilityLabel("\(node.title), \(category(for: node)), \(start), \(node.frames) frames")
            element.setAccessibilityHelp("Clip handle: drag the left or right edge to trim; drag inside to reorder.")
            if let laneIndex = lanes.firstIndex(of: category(for: node)) {
                let rect = NSRect(
                    x: Self.labelWidth + CGFloat(node.startFrame) * scale,
                    y: rowTop(laneIndex) - Self.rowHeight + 4,
                    width: max(4, CGFloat(node.frames) * scale),
                    height: Self.rowHeight - 8
                )
                element.setAccessibilityFrame(window?.convertToScreen(convert(rect, to: nil)) ?? .zero)
            }
            return element
        }
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        downPoint = convert(event.locationInWindow, from: nil)
        let point = downPoint
        if point.y >= bounds.maxY - Self.rulerHeight {
            mode = .scrub
            scrub(point, final: false)
            return
        }
        guard let node = node(at: point) else {
            mode = .scrub
            scrub(point, final: false)
            return
        }
        let additive = event.modifierFlags.contains(.command) || event.modifierFlags.contains(.shift)
        onSelect?(node.id, additive)
        guard !node.isReadOnly else {
            mode = nil
            activeNode = nil
            return
        }
        activeNode = node
        originalStart = node.startFrame
        originalEnd = node.endFrame
        ghostStart = nil
        ghostEnd = nil
        targetNodeID = nil
        let clipStart = Self.labelWidth + CGFloat(node.startFrame) * scale
        let clipEnd = Self.labelWidth + CGFloat(node.endFrame) * scale
        if node.kind.lowercased() == "generated_sfx" { mode = .move }
        else if abs(point.x - clipStart) <= CGFloat(TimelineMetrics.edgeZone(clipWidth: Double(clipEnd - clipStart))) { mode = .trimStart }
        else if abs(point.x - clipEnd) <= CGFloat(TimelineMetrics.edgeZone(clipWidth: Double(clipEnd - clipStart))) { mode = .trimEnd }
        else { mode = .move }
        if mode == .trimStart || mode == .trimEnd || mode == .move {
            let owner = mode == .move ? "timeline-reorder" : "timeline-trim"
            gestureLeaseTask = Task { await onBeginGesture?(owner) }
        }
    }

    override func mouseDragged(with event: NSEvent) {
        guard let mode else { return }
        let point = convert(event.locationInWindow, from: nil)
        switch mode {
        case .scrub:
            autoscroll(with: event)
            scrub(point, final: false)
        case .trimStart, .trimEnd:
            let delta = Int(((point.x - downPoint.x) / max(scale, 0.05)).rounded())
            let candidates = snapping
                ? TimelineMetrics.snapCandidates(nodes: snapshot?.nodes ?? [], excluding: activeNode?.id, playhead: playheadFrame)
                : []
            if mode == .trimStart {
                var next = originalStart + delta
                snapGuideFrame = TimelineMetrics.snap(frame: next, candidates: candidates, zoom: zoom)
                if let snapped = snapGuideFrame { next = snapped }
                ghostStart = max(0, min(originalEnd - 1, next))
                ghostEnd = originalEnd
            } else {
                var next = originalEnd + delta
                snapGuideFrame = TimelineMetrics.snap(frame: next, candidates: candidates, zoom: zoom)
                if let snapped = snapGuideFrame { next = snapped }
                ghostStart = originalStart
                ghostEnd = max(originalStart + 1, next)
            }
            autoscroll(with: event)
            needsDisplay = true
        case .move:
            guard hypot(point.x - downPoint.x, point.y - downPoint.y) > 12 else { return }
            targetNodeID = node(at: point)?.id
            needsDisplay = true
        }
    }

    override func mouseUp(with event: NSEvent) {
        guard let mode else { return }
        let point = convert(event.locationInWindow, from: nil)
        switch mode {
        case .scrub:
            scrub(point, final: true)
        case .trimStart, .trimEnd:
            if let node = activeNode, node.kind.lowercased() == "cutaway", node.isPhoto {
                let start = ghostStart ?? node.startFrame
                let end = ghostEnd ?? node.endFrame
                Task {
                    let token = await gestureLeaseTask?.value
                    onTrim?(node, Double(start) / (snapshot?.fps ?? 30),
                            Double(max(1, end - start)) / (snapshot?.fps ?? 30), token)
                }
            } else if let node = activeNode, node.kind.lowercased() == "segment", node.isPhoto {
                let start = ghostStart ?? node.startFrame
                let end = ghostEnd ?? node.endFrame
                Task {
                    let token = await gestureLeaseTask?.value
                    onTrim?(node, 0, Double(max(1, end - start)) / (snapshot?.fps ?? 30), token)
                }
            } else if let node = activeNode, node.kind.lowercased() == "cutaway",
               let sourceIn = node.sourceIn, let sourceOut = node.sourceOut {
                let scaleFactor = (sourceOut - sourceIn) / Double(max(node.frames, 1))
                let deltaFrames = (ghostStart ?? node.startFrame) - node.startFrame
                    + (ghostEnd ?? node.endFrame) - node.endFrame
                let nextIn = mode == .trimStart ? sourceIn + Double(deltaFrames) * scaleFactor : sourceIn
                let nextOut = mode == .trimEnd ? sourceOut + Double(deltaFrames) * scaleFactor : sourceOut
                Task {
                    let token = await gestureLeaseTask?.value
                    onTrim?(node, max(0, nextIn), max(nextIn + 0.03, nextOut), token)
                }
            } else if let node = activeNode, isOverlay(node) {
                let start = ghostStart ?? node.startFrame
                let end = ghostEnd ?? node.endFrame
                Task {
                    let token = await gestureLeaseTask?.value
                    onTrim?(node, Double(start) / (snapshot?.fps ?? 30),
                            Double(max(1, end - start)) / (snapshot?.fps ?? 30), token)
                }
            } else if let node = activeNode, let sourceIn = node.sourceIn, let sourceOut = node.sourceOut {
                let scaleFactor = (sourceOut - sourceIn) / Double(max(node.frames, 1))
                let deltaFrames = (ghostStart ?? node.startFrame) - node.startFrame
                    + (ghostEnd ?? node.endFrame) - node.endFrame
                let nextIn = mode == .trimStart ? sourceIn + Double(deltaFrames) * scaleFactor : sourceIn
                let nextOut = mode == .trimEnd ? sourceOut + Double(deltaFrames) * scaleFactor : sourceOut
                Task {
                    let token = await gestureLeaseTask?.value
                    onTrim?(node, max(0, nextIn), max(nextIn + 0.03, nextOut), token)
                }
            } else if let node = activeNode {
                Task {
                    let token = await gestureLeaseTask?.value
                    onTrim?(node, 0, 0, token)
                }
            }
        case .move:
            if let node = activeNode, hypot(point.x - downPoint.x, point.y - downPoint.y) > 12 {
                let target = targetNodeID == node.id ? nil : targetNodeID
                let frame = Int(((point.x - Self.labelWidth) / max(scale, 0.05)).rounded())
                Task {
                    let token = await gestureLeaseTask?.value
                    onReorder?(node, max(0, frame), target, token)
                }
            }
        }
        self.mode = nil
        activeNode = nil
        ghostStart = nil
        ghostEnd = nil
        targetNodeID = nil
        snapGuideFrame = nil
        gestureLeaseTask = nil
        gestureLease = nil
        needsDisplay = true
    }

    override func keyDown(with event: NSEvent) {
        switch event.keyCode {
        case 49: NotificationCenter.default.post(name: .reelsTogglePlayback, object: nil)
        case 123: onScrub?(max(0, playheadFrame - 1), true)
        case 124: onScrub?(playheadFrame + 1, true)
        case 11: onSplit?()
        case 51, 117: onDelete?()
        default: super.keyDown(with: event)
        }
    }

    private func scrub(_ point: CGPoint, final: Bool) {
        let frames = Int(((point.x - Self.labelWidth) / max(scale, 0.05) ).rounded())
        onScrub?(max(0, min(snapshot?.durationFrames ?? frames, frames)), final)
    }

    private func node(at point: CGPoint) -> EditorNode? {
        guard let snapshot else { return nil }
        let row = Int((bounds.maxY - Self.rulerHeight - point.y) / Self.rowHeight)
        guard lanes.indices.contains(row) else { return nil }
        let frame = Double(point.x - Self.labelWidth) / max(scale, 0.05)
        return snapshot.nodes.filter(TimelineLaneClassifier.visibleInTimeline).first { node in
            category(for: node) == lanes[row]
                && frame >= Double(node.startFrame)
                && frame <= Double(node.endFrame)
        }
    }

    private func isOverlay(_ node: EditorNode) -> Bool {
        let kind = "\(node.kind) \(node.lane)".lowercased()
        return node.parentId != nil || kind.contains("overlay") || kind.contains("caption")
            || kind.contains("text") || kind.contains("cutaway")
            || kind.contains("b-roll") || kind.contains("broll")
    }
}

@MainActor
private final class PlayheadLayer: NSView {
    private var frameNumber = 0
    private var zoom = 0.56
    private var labelWidth: CGFloat = 126
    private var rulerHeight: CGFloat = 28
    private var fps = 30.0

    func update(frame: Int, zoom: Double, labelWidth: CGFloat, rulerHeight: CGFloat, fps: Double) {
        let old = playheadX
        frameNumber = frame
        self.zoom = zoom
        self.labelWidth = labelWidth
        self.rulerHeight = rulerHeight
        self.fps = fps
        setNeedsDisplay(NSRect(x: old - 5, y: 0, width: 12, height: bounds.height))
        setNeedsDisplay(NSRect(x: playheadX - 5, y: 0, width: 12, height: bounds.height))
    }

    private var playheadX: CGFloat { labelWidth + CGFloat(frameNumber) * CGFloat(zoom) }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        NSColor.systemRed.setStroke()
        let path = NSBezierPath()
        path.move(to: CGPoint(x: playheadX, y: 0))
        path.line(to: CGPoint(x: playheadX, y: bounds.maxY - rulerHeight))
        path.lineWidth = 1
        path.stroke()
        NSColor.systemRed.setFill()
        let cap = NSBezierPath()
        cap.move(to: CGPoint(x: playheadX - 5, y: bounds.maxY - rulerHeight))
        cap.line(to: CGPoint(x: playheadX + 5, y: bounds.maxY - rulerHeight))
        cap.line(to: CGPoint(x: playheadX, y: bounds.maxY - rulerHeight - 7))
        cap.close()
        cap.fill()
    }
}

private func timecode(_ frame: Int, fps: Double) -> String { TimeFormat.timecode(frame, fps: fps) }

private func extractAudioWaveform(url: URL, from: Double, to: Double) async -> [Float]? {
    guard to > from else { return nil }
    let asset = AVURLAsset(url: url)
    guard let tracks = try? await asset.loadTracks(withMediaType: .audio),
          let track = tracks.first,
          let reader = try? AVAssetReader(asset: asset) else { return nil }
    let settings: [String: Any] = [
        AVFormatIDKey: kAudioFormatLinearPCM,
        AVLinearPCMBitDepthKey: 16,
        AVLinearPCMIsFloatKey: false,
        AVLinearPCMIsBigEndianKey: false,
        AVLinearPCMIsNonInterleaved: false
    ]
    let output = AVAssetReaderTrackOutput(track: track, outputSettings: settings)
    output.alwaysCopiesSampleData = false
    guard reader.canAdd(output) else { return nil }
    reader.add(output)
    reader.timeRange = CMTimeRange(
        start: CMTime(seconds: max(0, from), preferredTimescale: 600),
        duration: CMTime(seconds: min(180, to - from), preferredTimescale: 600)
    )
    guard reader.startReading() else { return nil }

    var peaks = Array(repeating: Float.zero, count: 192)
    let rangeDuration = min(180, to - from)

    while reader.status == .reading, let sample = output.copyNextSampleBuffer(),
          let block = CMSampleBufferGetDataBuffer(sample) {
        let byteCount = CMBlockBufferGetDataLength(block)
        let frameCount = CMSampleBufferGetNumSamples(sample)
        guard frameCount > 0, byteCount >= frameCount * 2 else { continue }
        let channels = max(1, byteCount / frameCount / 2)
        var bytes = [UInt8](repeating: 0, count: byteCount)
        let status = bytes.withUnsafeMutableBytes { buffer in
            CMBlockBufferCopyDataBytes(
                block, atOffset: 0, dataLength: byteCount, destination: buffer.baseAddress!
            )
        }
        guard status == kCMBlockBufferNoErr else { continue }
        let time = CMSampleBufferGetPresentationTimeStamp(sample).seconds
        let sampleDuration = CMSampleBufferGetDuration(sample).seconds
        let duration = sampleDuration.isFinite && sampleDuration > 0
            ? sampleDuration
            : Double(frameCount) / 48_000
        for frame in 0..<frameCount {
            var amplitude: Float = 0
            for channel in 0..<channels {
                let offset = (frame * channels + channel) * 2
                let bits = UInt16(bytes[offset]) | (UInt16(bytes[offset + 1]) << 8)
                let sampleValue = Int(Int16(bitPattern: bits))
                amplitude = max(amplitude, Float(abs(sampleValue)) / Float(Int16.max))
            }
            let elapsed = max(0, time - from + duration * Double(frame) / Double(frameCount))
            let bucket = min(peaks.count - 1, Int(elapsed / rangeDuration * Double(peaks.count)))
            peaks[bucket] = max(peaks[bucket], amplitude)
        }
    }
    guard reader.status == .completed || peaks.contains(where: { $0 > 0 }) else { return nil }
    return peaks
}

private extension Double {
    func rounded(toPlaces places: Int) -> Double {
        let factor = pow(10, Double(places))
        return (self * factor).rounded() / factor
    }
}
