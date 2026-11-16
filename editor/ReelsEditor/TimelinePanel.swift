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

    static func shouldDrawLinkedAudio(for node: EditorNode, nodes: [EditorNode]) -> Bool {
        guard node.kind.lowercased() == "segment", !node.isPhoto else { return false }
        return !nodes.contains {
            $0.kind.lowercased() == "source_audio" && $0.generated == true && $0.isReadOnly
                && $0.parentId == node.id
        }
    }

    static func interactionNode(for node: EditorNode, nodes: [EditorNode]) -> EditorNode? {
        guard node.kind.lowercased() == "source_audio", node.isReadOnly else { return node }
        guard let parentID = node.parentId,
              let owner = nodes.first(where: { $0.id == parentID }),
              owner.kind.lowercased() == "segment", !owner.isReadOnly else { return nil }
        return owner
    }
}


/// Geometry shared by the SwiftUI panel and the AppKit canvas.
private enum TimelineLayout {
    static let labelWidth: CGFloat = 132
    static let rulerHeight: CGFloat = 30
    static let workBarHeight: CGFloat = 5
    static var topInset: CGFloat { rulerHeight + workBarHeight }
    static let videoRow: CGFloat = 46
    static let audioRow: CGFloat = 40
    static let divider: CGFloat = 8
    static let addStrip: CGFloat = 28

    struct Row {
        let track: EditorTrack
        let offset: CGFloat   // distance below the work-area bar
        let height: CGFloat
    }

    /// Premiere order: video tracks high-to-low on top, a divider, then audio tracks A1 downward.
    static func rows(tracks: [EditorTrack]) -> [Row] {
        let video = tracks.filter { $0.isVideo }.sorted { $0.index > $1.index }
        let audio = tracks.filter { !$0.isVideo }.sorted { $0.index < $1.index }
        var rows: [Row] = []
        var offset: CGFloat = 0
        for track in video { rows.append(Row(track: track, offset: offset, height: videoRow)); offset += videoRow }
        offset += divider
        for track in audio { rows.append(Row(track: track, offset: offset, height: audioRow)); offset += audioRow }
        return rows
    }

    static func addStripOffset(tracks: [EditorTrack]) -> CGFloat {
        guard let last = rows(tracks: tracks).last else { return 0 }
        return last.offset + last.height
    }

    static func contentHeight(tracks: [EditorTrack]) -> CGFloat {
        topInset + addStripOffset(tracks: tracks) + addStrip + 14
    }
}

struct TimelineDropLocation: Equatable {
    var track: String
    var atFrame: Int

    static func resolve(_ point: CGPoint, canvasSize: CGSize, zoom: Double,
                        tracks: [EditorTrack]) -> TimelineDropLocation? {
        guard zoom > 0, point.x >= TimelineLayout.labelWidth, point.x < canvasSize.width,
              point.y >= TimelineLayout.topInset else { return nil }
        let target = TimelineLayout.rows(tracks: tracks).first { row in
            let top = TimelineLayout.topInset + row.offset
            return point.y >= top && point.y < top + row.height
        }
        guard let target, target.track.isVideo, !target.track.locked else { return nil }
        let rawFrame = Double(point.x - TimelineLayout.labelWidth) / zoom
        guard rawFrame.isFinite else { return nil }
        return TimelineDropLocation(
            track: target.track.canonicalId ?? target.track.id,
            atFrame: max(0, Int(rawFrame.rounded(.toNearestOrEven)))
        )
    }

    /// Uses canvas/document coordinates after NSView conversion, so horizontal scroll is
    /// already reflected in point.x. Vertical cross-track drags preserve the anchor frame.
    static func overwriteFrame(at point: CGPoint, downPoint: CGPoint, anchorFrame: Int,
                               grabOffset: Int, zoom: Double, snapping: Bool) -> (frame: Int, verticalSameTime: Bool) {
        let horizontal = abs(point.x - downPoint.x)
        let vertical = abs(point.y - downPoint.y)
        let verticalSameTime = snapping && vertical > horizontal
        if verticalSameTime { return (anchorFrame, true) }
        let scale = CGFloat(max(zoom, 0.05))
        let frameAtX = Int(((point.x - TimelineLayout.labelWidth) / scale).rounded(.toNearestOrEven))
        return (max(0, frameAtX - grabOffset), false)
    }

    static func resolveAppKit(_ point: CGPoint, canvasSize: CGSize, zoom: Double,
                              tracks: [EditorTrack]) -> TimelineDropLocation? {
        // NSView.convert(_:from:) returns document-space x even when its clip view is scrolled.
        resolve(CGPoint(x: point.x, y: canvasSize.height - point.y),
                canvasSize: canvasSize, zoom: zoom, tracks: tracks)
    }
}

private struct ScrollRequest: Equatable {
    var id: Int
    var x: CGFloat
}

private enum TimelineColors {
    static let video = NSColor(srgbRed: 0.36, green: 0.40, blue: 0.92, alpha: 1)
    static let broll = NSColor(srgbRed: 0.16, green: 0.62, blue: 0.66, alpha: 1)
    static let graphics = NSColor(srgbRed: 0.80, green: 0.34, blue: 0.72, alpha: 1)
    static let audio = NSColor(srgbRed: 0.20, green: 0.64, blue: 0.40, alpha: 1)
    static let playhead = NSColor(srgbRed: 0.30, green: 0.62, blue: 1.0, alpha: 1)
    static let workArea = NSColor(srgbRed: 1.0, green: 0.82, blue: 0.20, alpha: 1)
    static let background = NSColor(white: 0.105, alpha: 1)
    static let rowEven = NSColor(white: 0.135, alpha: 1)
    static let rowOdd = NSColor(white: 0.155, alpha: 1)
    static let header = NSColor(white: 0.175, alpha: 1)
    static let ruler = NSColor(white: 0.125, alpha: 1)
    static let line = NSColor(white: 1, alpha: 0.10)
}

struct TimelinePanel: View {
    @Bindable var store: EditorStore
    @State private var zoom = TimelineMetrics.defaultZoom
    @State private var snapping = true
    @State private var viewportWidth: CGFloat = 0
    @State private var scrollX: CGFloat = 0
    @State private var scrollRequest: ScrollRequest?
    @State private var pendingFit = true
    @State private var audioMode = AudioVisualCache.shared.mode

    private static let gutter = Double(TimelineLayout.labelWidth)

    private var contentWidth: CGFloat {
        max(650, TimelineLayout.labelWidth + CGFloat(store.snapshot?.durationFrames ?? 0) * zoom + 120)
    }
    private var trackHeight: CGFloat {
        TimelineLayout.contentHeight(tracks: store.tracks)
    }
    private var sequenceName: String {
        guard let path = store.openPlanPath else { return "" }
        return URL(fileURLWithPath: path).deletingPathExtension().lastPathComponent
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                PanelTitle("Timeline")
                if !sequenceName.isEmpty {
                    Text(sequenceName)
                        .font(EditorType.bodyEmphasis)
                        .foregroundStyle(.primary.opacity(0.85))
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .frame(maxWidth: 220, alignment: .leading)
                }
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
                        .font(EditorType.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .frame(maxWidth: 180, alignment: .trailing)
                }
                Picker("Audio display", selection: $audioMode) {
                    Text("Waveform").tag(AudioVisualMode.waveform)
                    Text("Spectrogram").tag(AudioVisualMode.spectrogram)
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .frame(width: 170)
                .help("Draw audio clips as a waveform or a spectrogram")
                .accessibilityLabel("Audio display")
                Toggle(isOn: $snapping) { Image(systemName: "magnet") }
                    .toggleStyle(.button)
                    .help("Snap edges to the playhead and neighboring clips")
                    .accessibilityLabel("Snapping")
                Button { zoom = TimelineMetrics.zoomOut(zoom) } label: { Image(systemName: "minus.magnifyingglass") }
                    .help("Zoom out  ⌘−")
                    .accessibilityLabel("Zoom out")
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
            .frame(height: 34)
            .background(EditorPalette.panel)
            Divider()

            GeometryReader { proxy in
                ScrollView(.horizontal) {
                    TimelineCanvasRepresentable(
                        snapshot: store.snapshot,
                        tracks: store.tracks,
                        trackMap: trackMap,
                        selectedIDs: store.selectedIDs,
                        changedIDs: store.recentlyChangedIDs,
                        sourceURLs: store.timelineSourceURLs,
                        playheadFrame: store.playheadFrame,
                        zoom: zoom,
                        snapping: snapping,
                        scrollRequest: scrollRequest,
                        onSelect: { id, additive in store.selectNode(id, additive: additive) },
                        onBeginGesture: { owner in await store.acquireTimelineLease(owner: owner) },
                        onEndGesture: { token in store.releaseTimelineLease(token) },
                        onScrub: { frame, final in
                            store.setPlayhead(frame, final: final)
                        },
                        onTrim: { node, sourceIn, sourceOut, ghost in store.trim(node: node, sourceIn: sourceIn, sourceOut: sourceOut, ghost: ghost) },
                         onReorder: { node, frame, beforeID in
                             store.moveTimelineItem(node, toFrame: frame, beforeID: beforeID)
                         },
                         onOverwrite: { placements, editID in
                             store.overwriteTimeline(placements, editID: editID)
                         },
                         onDropRejected: { store.notice = $0 },
                         onMoveToTrack: { node, trackID in store.moveNode(node, toTrack: trackID) },
                        onDelete: { store.deleteSelection() },
                        onSplit: { store.splitAtPlayhead() },
                        onScroll: { x in if abs(x - scrollX) > 0.5 { scrollX = x } },
                        onToggleLock: { id, locked in store.setTrackLocked(id: id, locked: locked) },
                        onAddTrack: { kind in store.addTrack(kind: kind) },
                        onRenameTrack: { id in promptRename(trackID: id) },
                        onDeleteTrack: { id in store.removeTrack(id: id) },
                         onDropMusic: { slug, trackID in dropMusic(slug: slug, onTrack: trackID) },
                         onPreviewDrop: { id, frame, trackID, command in
                             store.previewTimelineDrop(id, atFrame: frame, trackID: trackID, commandInsert: command)
                         },
                         onDropItem: { id, frame, trackID, command in
                             store.insertDroppedItem(id, atFrame: frame, trackID: trackID, commandInsert: command)
                         }
                     )
                    .frame(width: max(contentWidth, proxy.size.width), height: max(trackHeight, proxy.size.height))
                }
                .scrollIndicators(.hidden)
                .onAppear { viewportWidth = proxy.size.width; fitIfPending() }
                .onChange(of: proxy.size.width) { _, width in viewportWidth = width; fitIfPending() }
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

            TimelineZoomScroller(
                durationFrames: store.snapshot?.durationFrames ?? 0,
                zoom: zoom, viewportWidth: Double(viewportWidth), gutter: Self.gutter, scrollX: Double(scrollX),
                onChange: { newZoom, newScroll in
                    zoom = TimelineMetrics.clamp(newZoom)
                    scrollRequest = ScrollRequest(id: (scrollRequest?.id ?? 0) + 1, x: CGFloat(newScroll))
                }
            )
            .frame(height: 20)
            .padding(.horizontal, 10)
            .background(EditorPalette.panel)
        }
        .background(EditorPalette.panel)
        .onReceive(NotificationCenter.default.publisher(for: .reelsTimelineZoom)) { note in
            switch note.object as? String {
            case "in": zoom = TimelineMetrics.zoomIn(zoom)
            case "out": zoom = TimelineMetrics.zoomOut(zoom)
            default: fit()
            }
        }
        // Fit the reel to the visible width whenever a reel is opened (not on every edit).
        .onChange(of: store.openPlanPath) { _, _ in pendingFit = true; fitIfPending() }
        .onChange(of: store.snapshot?.documentId) { _, _ in pendingFit = true; fitIfPending() }
        .onChange(of: store.snapshot?.durationFrames) { _, _ in fitIfPending() }
        .onChange(of: audioMode) { _, mode in AudioVisualCache.shared.mode = mode }
        .onAppear { fitIfPending() }
    }

    private var trackMap: [String: String] {
        var map: [String: String] = [:]
        for node in store.snapshot?.nodes ?? [] { map[node.id] = store.trackID(for: node) }
        return map
    }

    private func fit() {
        zoom = TimelineMetrics.fitZoom(durationFrames: store.snapshot?.durationFrames ?? 0,
                                       viewportWidth: Double(viewportWidth), gutter: Self.gutter)
        scrollRequest = ScrollRequest(id: (scrollRequest?.id ?? 0) + 1, x: 0)
    }

    private func fitIfPending() {
        guard pendingFit, viewportWidth > 0, (store.snapshot?.durationFrames ?? 0) > 0 else { return }
        pendingFit = false
        fit()
    }

    private func promptRename(trackID: String) {
        guard let track = store.tracks.first(where: { $0.id == trackID }) else { return }
        let alert = NSAlert()
        alert.messageText = "Rename track \(trackID)"
        alert.addButton(withTitle: "Rename")
        alert.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 220, height: 24))
        field.stringValue = track.name
        alert.accessoryView = field
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let name = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        if !name.isEmpty { store.renameTrack(id: trackID, name: name) }
    }

    /// A library track dropped on an audio track becomes the reel's music; if it landed on another
    /// audio track than the default, move the music clip there once the snapshot has updated.
    private func dropMusic(slug: String, onTrack trackID: String) {
        store.setMusic(track: slug, startSeconds: nil)
        guard trackID != EditorStore.defaultTrackID(kind: "music") else { return }
        Task { @MainActor in
            for _ in 0..<10 {
                try? await Task.sleep(nanoseconds: 400_000_000)
                if let node = store.snapshot?.nodes.first(where: { $0.kind == "music" }) {
                    if store.trackID(for: node) != trackID { store.moveNode(node, toTrack: trackID) }
                    return
                }
            }
        }
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
        .padding(.leading, TimelineLayout.labelWidth)
        .padding(.top, 20)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Premiere-style zoom scroller: the bar is the visible range of the reel; drag it to scroll,
/// drag either round handle to zoom.
private struct TimelineZoomScroller: View {
    let durationFrames: Int
    let zoom: Double
    let viewportWidth: Double
    let gutter: Double
    let scrollX: Double
    let onChange: (Double, Double) -> Void

    private enum Part { case bar, left, right }
    @State private var origin: (start: Double, end: Double, part: Part)?

    private var total: Double { max(1, Double(durationFrames) * 1.05, visStart + vis) }
    private var vis: Double { TimelineMetrics.visibleFrames(zoom: zoom, viewportWidth: viewportWidth, gutter: gutter) }
    private var visStart: Double { max(0, scrollX / max(zoom, 0.001)) }

    var body: some View {
        GeometryReader { proxy in
            let width = max(1, proxy.size.width)
            let a = CGFloat(min(1, visStart / total)) * width
            let b = CGFloat(min(1, (visStart + vis) / total)) * width
            ZStack(alignment: .leading) {
                Capsule().fill(Color.white.opacity(0.07)).frame(height: 8)
                Capsule().fill(Color.white.opacity(0.28))
                    .frame(width: max(14, b - a), height: 8)
                    .offset(x: a)
                    .gesture(drag(.bar, width: width))
                    .help("Drag to scroll the timeline")
                Circle().fill(Color.white.opacity(0.9)).frame(width: 12, height: 12)
                    .offset(x: a - 6)
                    .gesture(drag(.left, width: width))
                    .help("Drag to zoom (left edge)")
                Circle().fill(Color.white.opacity(0.9)).frame(width: 12, height: 12)
                    .offset(x: max(a + 8, b) - 6)
                    .gesture(drag(.right, width: width))
                    .help("Drag to zoom (right edge)")
            }
            .frame(height: proxy.size.height)
            .padding(.horizontal, 6)
        }
        .accessibilityElement()
        .accessibilityLabel("Timeline zoom scroller")
    }

    private func drag(_ part: Part, width: CGFloat) -> some Gesture {
        DragGesture(minimumDistance: 1)
            .onChanged { value in
                if origin == nil { origin = (visStart, visStart + vis, part) }
                guard let o = origin else { return }
                let delta = Double(value.translation.width / width) * total
                let minVis = TimelineMetrics.visibleFrames(zoom: TimelineMetrics.maxZoom, viewportWidth: viewportWidth, gutter: gutter)
                switch o.part {
                case .bar:
                    let span = o.end - o.start
                    let start = min(max(0, o.start + delta), max(0, total - span))
                    onChange(zoom, start * zoom)
                case .left:
                    let start = min(max(0, o.start + delta), o.end - minVis)
                    let z = TimelineMetrics.zoom(forVisibleFrames: o.end - start, viewportWidth: viewportWidth, gutter: gutter)
                    onChange(z, start * z)
                case .right:
                    let end = min(max(o.start + minVis, o.end + delta), total)
                    let z = TimelineMetrics.zoom(forVisibleFrames: end - o.start, viewportWidth: viewportWidth, gutter: gutter)
                    onChange(z, o.start * z)
                }
            }
            .onEnded { _ in origin = nil }
    }
}

private struct TimelineCanvasRepresentable: NSViewRepresentable {
    let snapshot: EditorSnapshot?
    let tracks: [EditorTrack]
    let trackMap: [String: String]
    let selectedIDs: Set<String>
    let changedIDs: Set<String>
    let sourceURLs: [String: URL]
    let playheadFrame: Int
    let zoom: Double
    let snapping: Bool
    let scrollRequest: ScrollRequest?
    let onSelect: (String, Bool) -> Void
    let onBeginGesture: (String) async -> String?
    let onEndGesture: (String) -> Void
    let onScrub: (Int, Bool) -> Void
    let onTrim: (EditorNode, Double, Double, TimelineGhost) -> Void
    let onReorder: (EditorNode, Int, String?) -> Void
    let onOverwrite: ([TimelinePlacement], String) -> Bool
    let onDropRejected: (String) -> Void
    let onMoveToTrack: (EditorNode, String) -> Void
    let onDelete: () -> Void
    let onSplit: () -> Void
    let onScroll: (CGFloat) -> Void
    let onToggleLock: (String, Bool) -> Void
    let onAddTrack: (String) -> Void
    let onRenameTrack: (String) -> Void
    let onDeleteTrack: (String) -> Void
    let onDropMusic: (String, String) -> Void
    let onPreviewDrop: (String, Int, String, Bool) -> TimelineDropResolution
    let onDropItem: (String, Int, String, Bool) -> Bool

    func makeNSView(context: Context) -> TimelineCanvas {
        let view = TimelineCanvas()
        push(to: view)
        return view
    }

    func updateNSView(_ view: TimelineCanvas, context: Context) { push(to: view) }

    private func push(to view: TimelineCanvas) {
        view.update(
            snapshot: snapshot, tracks: tracks, trackMap: trackMap, selectedIDs: selectedIDs, changedIDs: changedIDs,
            sourceURLs: sourceURLs, playheadFrame: playheadFrame, zoom: zoom, snapping: snapping,
            scrollRequest: scrollRequest,
            callbacks: TimelineCanvas.Callbacks(
                onSelect: onSelect, onBeginGesture: onBeginGesture, onEndGesture: onEndGesture,
                onScrub: onScrub, onTrim: onTrim, onReorder: onReorder, onOverwrite: onOverwrite,
                onDropRejected: onDropRejected, onMoveToTrack: onMoveToTrack, onDelete: onDelete, onSplit: onSplit,
                onScroll: onScroll, onToggleLock: onToggleLock, onAddTrack: onAddTrack,
                onRenameTrack: onRenameTrack, onDeleteTrack: onDeleteTrack, onDropMusic: onDropMusic,
                onPreviewDrop: onPreviewDrop, onDropItem: onDropItem
            )
        )
    }
}

@MainActor
private final class TimelineCanvas: NSView {
    struct Callbacks {
        var onSelect: (String, Bool) -> Void
        var onBeginGesture: (String) async -> String?
        var onEndGesture: (String) -> Void
        var onScrub: (Int, Bool) -> Void
        var onTrim: (EditorNode, Double, Double, TimelineGhost) -> Void
        var onReorder: (EditorNode, Int, String?) -> Void
        var onOverwrite: ([TimelinePlacement], String) -> Bool
        var onDropRejected: (String) -> Void
        var onMoveToTrack: (EditorNode, String) -> Void
        var onDelete: () -> Void
        var onSplit: () -> Void
        var onScroll: (CGFloat) -> Void
        var onToggleLock: (String, Bool) -> Void
        var onAddTrack: (String) -> Void
        var onRenameTrack: (String) -> Void
        var onDeleteTrack: (String) -> Void
        var onDropMusic: (String, String) -> Void
        var onPreviewDrop: (String, Int, String, Bool) -> TimelineDropResolution
        var onDropItem: (String, Int, String, Bool) -> Bool
    }

    private static let labelWidth = TimelineLayout.labelWidth
    private static let rulerHeight = TimelineLayout.rulerHeight
    private static let topInset = TimelineLayout.topInset

    private var snapshot: EditorSnapshot?
    private var tracks: [EditorTrack] = EditorTrack.defaults
    private var rows: [TimelineLayout.Row] = TimelineLayout.rows(tracks: EditorTrack.defaults)
    private var trackMap: [String: String] = [:]
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
    private var audioObserver: NSObjectProtocol?
    private var callbacks: Callbacks?
    private var lastScrollRequestID = 0
    private var mode: DragMode?
    private var downPoint = CGPoint.zero
    private var activeNode: EditorNode?
    private var originalStart: Int = 0
    private var originalEnd: Int = 0
    private var ghostStart: Int?
    private var ghostEnd: Int?
    private var targetNodeID: String?
    /// Frames between the clip's start and where it was grabbed, so a moved clip keeps
    /// its position under the pointer instead of jumping its start to the cursor.
    private var grabOffset = 0
    /// Track a clip is being dragged onto (vertical drag), nil when the drag stays on its own track.
    private var moveTargetTrackID: String?
    private var overwriteDrag = false
    private var dragNodes: [EditorNode] = []
    private var overwritePlacements: [TimelinePlacement] = []
    private var overwriteRegions: [TimelineOverwriteRegion] = []
    private var overwriteBlockedReason: String?
    private var overwriteDropTrackID: String?
    private var dragAnchorStart = 0
    private var playheadLayer: PlayheadLayer?
    private var dropTrackID: String?
    private var dropItemID: String?
    private var dropItemCommandInsert = false

    private enum DragMode: Equatable { case scrub, trimStart, trimEnd, move }
    /// The edit lease is requested only once a drag really starts, and always handed back
    /// if the gesture ends without an edit (a plain click must never hold the document).
    private var gestureLeaseTask: Task<String?, Never>?

    override var acceptsFirstResponder: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.backgroundColor = TimelineColors.background.cgColor
        thumbnailCache.countLimit = 180
        setAccessibilityElement(true)
        setAccessibilityRole(.group)
        setAccessibilityLabel("Reel timeline")
        setAccessibilityHelp("Select a clip, drag its edges to trim, drag a clip sideways to reorder or up/down to another track, or click the ruler to move the playhead.")
        let overlay = PlayheadLayer(frame: bounds)
        overlay.autoresizingMask = [.width, .height]
        overlay.setAccessibilityElement(false)
        addSubview(overlay)
        playheadLayer = overlay
        registerForDraggedTypes([.reelsMusicTrack, .string])
        audioObserver = NotificationCenter.default.addObserver(
            forName: .reelsAudioVisualReady, object: nil, queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.needsDisplay = true }
        }
    }

    required init?(coder: NSCoder) { nil }

    isolated deinit {
        if let audioObserver { NotificationCenter.default.removeObserver(audioObserver) }
        if let scrollObserver { NotificationCenter.default.removeObserver(scrollObserver) }
    }

    func update(snapshot: EditorSnapshot?, tracks: [EditorTrack], trackMap: [String: String],
                selectedIDs: Set<String>, changedIDs: Set<String>, sourceURLs: [String: URL],
                playheadFrame: Int, zoom: Double, snapping: Bool, scrollRequest: ScrollRequest?,
                callbacks: Callbacks) {
        let contentsChanged = self.snapshot != snapshot
            || self.tracks != tracks
            || self.trackMap != trackMap
            || self.selectedIDs != selectedIDs
            || self.changedIDs != changedIDs
            || self.sourceURLs != sourceURLs
            || self.zoom != zoom
        let playheadMoved = self.playheadFrame != playheadFrame
        self.snapshot = snapshot
        if self.tracks != tracks {
            self.tracks = tracks
            rows = TimelineLayout.rows(tracks: tracks)
        }
        self.trackMap = trackMap
        self.selectedIDs = selectedIDs
        self.changedIDs = changedIDs
        self.sourceURLs = sourceURLs
        self.playheadFrame = playheadFrame
        self.zoom = zoom
        self.snapping = snapping
        self.callbacks = callbacks
        if contentsChanged {
            needsDisplay = true
            window?.invalidateCursorRects(for: self)
        } else if playheadMoved {
            setNeedsDisplay(cornerRect)
        }
        playheadLayer?.update(frame: playheadFrame, zoom: zoom, labelWidth: Self.labelWidth,
                              rulerHeight: Self.rulerHeight, fps: snapshot?.fps ?? 30)
        setAccessibilityChildren(makeAccessibilityChildren())
        if let scrollRequest, scrollRequest.id != lastScrollRequestID {
            lastScrollRequestID = scrollRequest.id
            let x = scrollRequest.x
            // wait for SwiftUI to resize this view to the new zoom before scrolling
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.04) { [weak self] in
                guard let self, let scroll = self.enclosingScrollView else { return }
                let maxX = max(0, self.bounds.width - scroll.contentView.bounds.width)
                scroll.contentView.scroll(to: NSPoint(x: min(max(0, x), maxX), y: 0))
                scroll.reflectScrolledClipView(scroll.contentView)
            }
        }
    }

    private var scale: CGFloat { CGFloat(zoom) }
    private var cornerRect: NSRect {
        NSRect(x: visibleRect.minX, y: bounds.maxY - Self.topInset, width: Self.labelWidth, height: Self.topInset)
    }

    private func category(for node: EditorNode) -> String { TimelineLaneClassifier.category(for: node) }

    private func clipColor(for node: EditorNode) -> NSColor {
        switch category(for: node) {
        case "Audio": TimelineColors.audio
        case "B-roll": TimelineColors.broll
        case "Video": TimelineColors.video
        default: TimelineColors.graphics
        }
    }

    // MARK: layout

    private func rowRect(_ row: TimelineLayout.Row) -> NSRect {
        NSRect(x: 0, y: bounds.maxY - Self.topInset - row.offset - row.height, width: bounds.width, height: row.height)
    }

    private func row(at point: CGPoint) -> TimelineLayout.Row? {
        rows.first { rowRect($0).contains(CGPoint(x: rowRect($0).midX, y: point.y)) }
    }

    /// Track id the node lives on, validated against existing tracks.
    private func resolvedTrackID(for node: EditorNode) -> String {
        var id = trackMap[node.id] ?? node.track ?? EditorStore.defaultTrackID(kind: node.kind)
        if node.track == nil, category(for: node) == "Audio", id.hasPrefix("V") { id = "A3" }
        if let row = row(forTrack: id) { return row.track.id }
        let fallback = EditorStore.defaultTrackID(kind: node.kind)
        return row(forTrack: fallback)?.track.id ?? (rows.first?.track.id ?? "V1")
    }

    private func row(forTrack id: String) -> TimelineLayout.Row? {
        rows.first { $0.track.id == id || $0.track.canonicalId == id }
    }

    private func canonicalTrackID(_ track: EditorTrack) -> String {
        track.canonicalId ?? track.id
    }

    private func videoTrack(for node: EditorNode) -> EditorTrack? {
        let resolved = resolvedTrackID(for: node)
        return rows.first(where: { $0.track.id == resolved })?.track
    }

    private func updateOverwriteDrag(_ point: CGPoint) {
        guard let snapshot, let anchor = activeNode,
              let capabilities = snapshot.timelineCapabilities,
              capabilities.version == 1, capabilities.idAlgorithm == "sha256-v1",
              capabilities.overwrite else {
            overwriteBlockedReason = "Overwrite editing is unavailable for this renderer."
            overwritePlacements = []
            overwriteRegions = []
            overwriteDropTrackID = nil
            return
        }
        guard let targetRow = row(at: point) else {
            overwriteBlockedReason = "Drop on a video track."
            overwritePlacements = []
            overwriteRegions = []
            overwriteDropTrackID = nil
            return
        }
        overwriteDropTrackID = targetRow.track.id
        guard targetRow.track.isVideo else {
            overwriteBlockedReason = "Drop on a video track."
            overwritePlacements = []
            overwriteRegions = []
            return
        }
        let destination = targetRow.track
        let destinationID = canonicalTrackID(destination)
        let moving = dragNodes.isEmpty ? [anchor] : dragNodes
        let anchorSourceTrack = videoTrack(for: anchor)
        guard let anchorSourceTrack else {
            overwriteBlockedReason = "The source clip has no video track assignment."
            overwritePlacements = []
            overwriteRegions = []
            return
        }
        if canonicalTrackID(anchorSourceTrack) != destinationID && !capabilities.crossTrack {
            overwriteBlockedReason = "Cross-track overwrite is unavailable for this renderer."
            overwritePlacements = []
            overwriteRegions = []
            return
        }
        guard !destination.locked else {
            overwriteBlockedReason = "Track \(destination.name) is locked."
            overwritePlacements = []
            overwriteRegions = []
            return
        }
        if moving.count > 1 && !capabilities.multiMove {
            overwriteBlockedReason = "Multi-clip overwrite is unavailable for this renderer."
            overwritePlacements = []
            overwriteRegions = []
            return
        }
        for node in moving {
            guard ["segment", "cutaway"].contains(node.kind.lowercased()), !node.isReadOnly,
                  let sourceTrack = videoTrack(for: node), !sourceTrack.locked else {
                overwriteBlockedReason = "A selected source clip is locked or cannot be moved."
                overwritePlacements = []
                overwriteRegions = []
                return
            }
            if node.kind.lowercased() == "cutaway"
                && destinationID == "V1" && !capabilities.cutawayPromotion {
                overwriteBlockedReason = "Cutaway promotion is unavailable for this renderer."
                overwritePlacements = []
                overwriteRegions = []
                return
            }
        }

        let videoTracks = tracks.filter(\.isVideo).sorted { $0.index < $1.index }
        guard let anchorRank = videoTracks.firstIndex(where: { canonicalTrackID($0) == canonicalTrackID(anchorSourceTrack) }),
              let destinationRank = videoTracks.firstIndex(where: { canonicalTrackID($0) == destinationID }) else {
            overwriteBlockedReason = "The destination track is unavailable."
            overwritePlacements = []
            overwriteRegions = []
            return
        }
        let dropFrame = TimelineDropLocation.overwriteFrame(
            at: point, downPoint: downPoint, anchorFrame: dragAnchorStart,
            grabOffset: grabOffset, zoom: zoom, snapping: snapping
        )
        let verticalSameTime = dropFrame.verticalSameTime
        var anchorFrame = dropFrame.frame
        let movingIDs = Set(moving.map(\.id))
        let targetNodes = snapshot.nodes.filter {
            !movingIDs.contains($0.id)
                && ["segment", "cutaway"].contains($0.kind.lowercased())
                && canonicalTrackID(for: $0) == destinationID
        }
        let candidateFrames = TimelineMetrics.snapCandidates(nodes: targetNodes, excluding: nil,
                                                              playhead: playheadFrame)
        snapGuideFrame = nil
        if snapping {
            if verticalSameTime {
                snapGuideFrame = dragAnchorStart
            } else if let snapped = TimelineMetrics.snap(frame: anchorFrame, candidates: candidateFrames, zoom: zoom) {
                anchorFrame = snapped
                snapGuideFrame = snapped
            }
        }

        var placements: [TimelinePlacement] = []
        for node in moving {
            guard let sourceTrack = videoTrack(for: node),
                  let sourceRank = videoTracks.firstIndex(where: { canonicalTrackID($0) == canonicalTrackID(sourceTrack) }) else {
                overwriteBlockedReason = "A selected source clip has no video track assignment."
                overwritePlacements = []
                overwriteRegions = []
                return
            }
            let targetRank = destinationRank + sourceRank - anchorRank
            guard videoTracks.indices.contains(targetRank) else {
                overwriteBlockedReason = "The selected clips will not fit on the destination tracks."
                overwritePlacements = []
                overwriteRegions = []
                return
            }
            let target = videoTracks[targetRank]
            guard !target.locked else {
                overwriteBlockedReason = "Track \(target.name) is locked."
                overwritePlacements = []
                overwriteRegions = []
                return
            }
            let frame = max(0, anchorFrame + node.startFrame - dragAnchorStart)
            placements.append(.moving(id: node.id, track: canonicalTrackID(target), atFrame: frame))
        }
        guard !OptimisticTimeline.hasSameTrackPlacementOverlap(placements, nodes: snapshot.nodes,
                                                                fps: Int(snapshot.fps.rounded(.toNearestOrEven))) else {
            overwriteBlockedReason = "Selected clips would overlap each other on the destination track."
            overwritePlacements = placements
            overwriteRegions = []
            return
        }
        let regions = OptimisticTimeline.overwriteRegions(
            placements: placements, nodes: snapshot.nodes,
            fps: Int(snapshot.fps.rounded(.toNearestOrEven))
        )
        let lockedLinkedAudio = rows.first(where: { ($0.track.canonicalId ?? $0.track.id) == "A1" })?.track.locked == true
            && EditorStore.overwriteChangesLockedAudio(
                placements: placements, regions: regions, nodes: snapshot.nodes
            )
        if lockedLinkedAudio || regions.contains(where: { region in
            guard let node = snapshot.nodes.first(where: { $0.id == region.nodeID }) else { return true }
            return node.isReadOnly || rows.first(where: {
                $0.track.id == resolvedTrackID(for: node)
            })?.track.locked == true
        }) {
            overwriteBlockedReason = "An affected destination clip is locked."
            overwritePlacements = placements
            overwriteRegions = regions
            return
        }
        overwriteBlockedReason = nil
        overwritePlacements = placements
        overwriteRegions = regions
    }

    private func canonicalTrackID(for node: EditorNode) -> String {
        if let row = videoTrack(for: node) { return canonicalTrackID(row) }
        return node.track ?? EditorStore.defaultTrackID(kind: node.kind)
    }

    /// Nodes drawn on a row; A1 also shows the linked ride audio of video segments.
    private func clips(in row: TimelineLayout.Row) -> [(node: EditorNode, linked: Bool)] {
        guard let snapshot else { return [] }
        var result: [(EditorNode, Bool)] = []
        for node in snapshot.nodes where TimelineLaneClassifier.visibleInTimeline(node) {
            let id = resolvedTrackID(for: node)
            if id == row.track.id {
                result.append((node, node.kind.lowercased() == "source_audio"))
            } else if row.track.id == "A1",
                    TimelineLaneClassifier.shouldDrawLinkedAudio(for: node, nodes: snapshot.nodes),
                    rows.first(where: { $0.track.id == id })?.track.isVideo == true {
                result.append((node, true))
            }
        }
        return result
    }

    private func clipRect(_ node: EditorNode, in row: TimelineLayout.Row, start: Int? = nil, end: Int? = nil) -> NSRect {
        let s = start ?? node.startFrame
        let e = end ?? node.endFrame
        let r = rowRect(row)
        return NSRect(x: Self.labelWidth + CGFloat(s) * scale, y: r.minY + 3,
                      width: max(4, CGFloat(max(1, e - s)) * scale), height: r.height - 6)
    }

    // MARK: drawing

    override func draw(_ dirtyRect: NSRect) {
        TimelineColors.background.setFill()
        dirtyRect.fill()
        drawTracks()
        drawClips(dirtyRect)
        drawSnapGuide()
        drawRuler()
        drawGutter()
    }

    private func drawTracks() {
        for (index, row) in rows.enumerated() {
            let rect = rowRect(row)
            (index.isMultiple(of: 2) ? TimelineColors.rowEven : TimelineColors.rowOdd).setFill()
            rect.fill()
            if dropTrackID == row.track.id {
                NSColor.white.withAlphaComponent(0.10).setFill()
                rect.fill()
            }
            if overwriteDropTrackID == row.track.id {
                let tint = overwriteBlockedReason == nil ? TimelineColors.playhead : NSColor.systemRed
                tint.withAlphaComponent(overwriteBlockedReason == nil ? 0.13 : 0.18).setFill()
                rect.fill()
            }
            if row.track.locked {
                NSColor.black.withAlphaComponent(0.28).setFill()
                rect.fill()
            }
            TimelineColors.line.setFill()
            NSRect(x: 0, y: rect.minY, width: bounds.width, height: 0.5).fill()
        }
    }

    /// Track headers stay pinned to the left edge while the timeline scrolls.
    private func drawGutter() {
        let left = visibleRect.minX
        TimelineColors.header.setFill()
        NSRect(x: left, y: 0, width: Self.labelWidth, height: bounds.height - Self.topInset).fill()
        for row in rows {
            let r = rowRect(row)
            let header = NSRect(x: left, y: r.minY, width: Self.labelWidth, height: r.height)
            (row.track.isVideo ? TimelineColors.video : TimelineColors.audio).withAlphaComponent(0.9).setFill()
            NSRect(x: header.minX, y: header.minY, width: 3, height: header.height).fill()
            let idAttrs: [NSAttributedString.Key: Any] = [
                .font: EditorType.nsTrackHeader, .foregroundColor: NSColor.white.withAlphaComponent(0.92)
            ]
            NSAttributedString(string: row.track.id, attributes: idAttrs)
                .draw(at: CGPoint(x: header.minX + 12, y: header.midY - 7))
            if row.track.name != row.track.id {
                let para = NSMutableParagraphStyle(); para.lineBreakMode = .byTruncatingTail
                NSAttributedString(string: row.track.name, attributes: [
                    .font: NSFont.systemFont(ofSize: 10), .foregroundColor: NSColor.white.withAlphaComponent(0.55),
                    .paragraphStyle: para
                ]).draw(in: NSRect(x: header.minX + 38, y: header.midY - 6, width: Self.labelWidth - 38 - 34, height: 14))
            }
            drawSymbol(row.track.locked ? "lock.fill" : "lock.open", in: lockRect(for: header),
                       color: row.track.locked ? NSColor.systemYellow : NSColor.white.withAlphaComponent(0.4))
            TimelineColors.line.setFill()
            NSRect(x: left, y: r.minY, width: Self.labelWidth, height: 0.5).fill()
        }
        // video / audio divider
        if let firstAudio = rows.first(where: { !$0.track.isVideo }) {
            let y = rowRect(firstAudio).maxY
            NSColor.black.withAlphaComponent(0.6).setFill()
            NSRect(x: left, y: y, width: bounds.width, height: TimelineLayout.divider).fill()
        }
        // add-track strip
        let stripTop = bounds.maxY - Self.topInset - TimelineLayout.addStripOffset(tracks: tracks)
        let plus = addButtonRect()
        NSColor.white.withAlphaComponent(0.10).setFill()
        NSBezierPath(roundedRect: plus, xRadius: 4, yRadius: 4).fill()
        NSAttributedString(string: "+  Track", attributes: [
            .font: NSFont.systemFont(ofSize: 10.5, weight: .semibold), .foregroundColor: NSColor.white.withAlphaComponent(0.7)
        ]).draw(at: CGPoint(x: plus.minX + 12, y: plus.midY - 7))
        _ = stripTop
        TimelineColors.line.setFill()
        NSRect(x: left + Self.labelWidth - 1, y: 0, width: 1, height: bounds.height - Self.topInset).fill()
        drawCorner()
    }

    private func lockRect(for header: NSRect) -> NSRect {
        NSRect(x: header.maxX - 26, y: header.midY - 8, width: 16, height: 16)
    }

    private func addButtonRect() -> NSRect {
        let top = bounds.maxY - Self.topInset - TimelineLayout.addStripOffset(tracks: tracks)
        return NSRect(x: visibleRect.minX + 10, y: top - TimelineLayout.addStrip + 2, width: 74, height: 22)
    }

    private func drawSymbol(_ name: String, in rect: NSRect, color: NSColor) {
        guard let base = NSImage(systemSymbolName: name, accessibilityDescription: nil),
              let image = base.withSymbolConfiguration(NSImage.SymbolConfiguration(paletteColors: [color])) else { return }
        let size = image.size
        let scale = min(rect.width / size.width, rect.height / size.height)
        let target = NSRect(x: rect.midX - size.width * scale / 2, y: rect.midY - size.height * scale / 2,
                            width: size.width * scale, height: size.height * scale)
        image.draw(in: target)
    }

    /// Big blue timecode readout above the track headers.
    private func drawCorner() {
        let corner = cornerRect
        TimelineColors.ruler.setFill()
        corner.fill()
        let text = TimeFormat.timecode(playheadFrame, fps: snapshot?.fps ?? 30)
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 15, weight: .medium),
            .foregroundColor: TimelineColors.playhead
        ]
        let string = NSAttributedString(string: text, attributes: attrs)
        let size = string.size()
        string.draw(at: CGPoint(x: corner.minX + 10, y: corner.midY - size.height / 2))
        TimelineColors.line.setFill()
        NSRect(x: corner.minX, y: corner.minY, width: corner.width, height: 0.5).fill()
        NSRect(x: corner.maxX - 1, y: corner.minY, width: 1, height: corner.height).fill()
    }

    private func drawSnapGuide() {
        guard let frame = snapGuideFrame else { return }
        let x = Self.labelWidth + CGFloat(frame) * scale
        NSColor.systemYellow.withAlphaComponent(0.9).setStroke()
        let path = NSBezierPath()
        path.move(to: CGPoint(x: x, y: 0)); path.line(to: CGPoint(x: x, y: bounds.maxY - Self.topInset))
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
            MainActor.assumeIsolated {
                guard let self else { return }
                self.needsDisplay = true
                self.playheadLayer?.needsDisplay = true
                self.callbacks?.onScroll(self.visibleRect.minX)
            }
        }
    }

    override func resetCursorRects() {
        super.resetCursorRects()
        for row in rows where !row.track.locked {
            for (node, linked) in clips(in: row) where !node.isReadOnly && node.kind.lowercased() != "generated_sfx" {
                _ = linked
                let rect = clipRect(node, in: row)
                let zone = CGFloat(TimelineMetrics.edgeZone(clipWidth: Double(rect.width)))
                addCursorRect(NSRect(x: rect.minX - zone, y: rect.minY, width: zone * 2, height: rect.height), cursor: .resizeLeftRight)
                addCursorRect(NSRect(x: rect.maxX - zone, y: rect.minY, width: zone * 2, height: rect.height), cursor: .resizeLeftRight)
            }
        }
    }

    private func drawRuler() {
        let ruler = NSRect(x: 0, y: bounds.maxY - Self.rulerHeight, width: bounds.width, height: Self.rulerHeight)
        TimelineColors.ruler.setFill()
        ruler.fill()
        let attrs: [NSAttributedString.Key: Any] = [
            .font: EditorType.nsRuler,
            .foregroundColor: NSColor.white.withAlphaComponent(0.6)
        ]
        let fps = max(snapshot?.fps ?? 30, 1)
        let pixelsPerSecond = Double(scale) * fps
        let interval = TimelineMetrics.rulerStepSeconds(pixelsPerSecond: pixelsPerSecond)
        let duration = snapshot?.durationFrames ?? 0
        let seconds = Int(ceil(Double(duration) / fps))
        let last = max(seconds, 10) + interval
        let ticks = NSBezierPath()
        ticks.lineWidth = 0.5
        for second in stride(from: 0, through: last, by: interval) {
            let x = Self.labelWidth + CGFloat(Double(second) * pixelsPerSecond)
            guard x <= bounds.maxX, x >= visibleRect.minX - 60, x <= visibleRect.maxX else { continue }
            ticks.move(to: CGPoint(x: x, y: ruler.minY + 8))
            ticks.line(to: CGPoint(x: x, y: ruler.maxY))
            NSAttributedString(string: TimeFormat.clock(Double(second)), attributes: attrs)
                .draw(at: CGPoint(x: x + 4, y: ruler.minY + 8))
            let minor = interval > 1 ? 4 : 2
            for step in 1..<minor {
                let mx = x + CGFloat(Double(interval) * pixelsPerSecond) * CGFloat(step) / CGFloat(minor)
                ticks.move(to: CGPoint(x: mx, y: ruler.minY + 2))
                ticks.line(to: CGPoint(x: mx, y: ruler.minY + 7))
            }
        }
        NSColor.white.withAlphaComponent(0.28).setStroke()
        ticks.stroke()
        TimelineColors.line.setFill()
        NSRect(x: 0, y: ruler.minY, width: bounds.width, height: 0.5).fill()
        // yellow work-area bar spanning the reel
        let barRect = NSRect(x: 0, y: ruler.minY - TimelineLayout.workBarHeight, width: bounds.width, height: TimelineLayout.workBarHeight)
        TimelineColors.ruler.setFill()
        barRect.fill()
        if duration > 0 {
            TimelineColors.workArea.withAlphaComponent(0.9).setFill()
            NSBezierPath(roundedRect: NSRect(x: Self.labelWidth, y: barRect.minY + 1,
                                             width: CGFloat(duration) * scale, height: 3), xRadius: 1.5, yRadius: 1.5).fill()
        }
    }

    private func hasEffects(_ node: EditorNode) -> Bool {
        for key in ["punch", "zoom", "blur", "nat", "grade", "motion", "transition", "effects", "effect", "filter", "fx"] {
            guard let value = node.raw[key] else { continue }
            if case .null = value { continue }
            if case .bool(let flag) = value, !flag { continue }
            if let text = value.stringValue, text.isEmpty { continue }
            return true
        }
        if let speed = node.raw["speed"]?.numberValue, abs(speed - 1) > 0.001 { return true }
        return false
    }

    private func displayName(_ node: EditorNode) -> String {
        let title = node.title
        return title.contains("/") ? URL(fileURLWithPath: title).lastPathComponent : title
    }

    private func drawClips(_ dirtyRect: NSRect) {
        if overwriteDrag {
            let hideSources = overwriteBlockedReason == nil && !overwritePlacements.isEmpty
            let movingIDs = hideSources ? Set(dragNodes.map(\.id)) : []
            for row in rows {
                for (node, linked) in clips(in: row) {
                    guard !movingIDs.contains(node.id) else { continue }
                    let rect = clipRect(node, in: row)
                    guard rect.intersects(dirtyRect) else { continue }
                    drawClip(node, linked: linked, in: rect, row: row)
                }
            }
            drawOverwriteGhosts()
            drawOverwriteRegions()
            return
        }
        for row in rows {
            for (node, linked) in clips(in: row) {
                let active = node.id == activeNode?.id
                let rect = clipRect(node, in: row, start: active ? ghostStart : nil, end: active ? ghostEnd : nil)
                guard rect.intersects(dirtyRect) else { continue }
                drawClip(node, linked: linked, in: rect, row: row)
            }
        }
        if let node = activeNode, let trackID = moveTargetTrackID,
           let row = rows.first(where: { $0.track.id == trackID }) {
            let rect = clipRect(node, in: row)
            clipColor(for: node).withAlphaComponent(0.35).setFill()
            let ghost = NSBezierPath(roundedRect: rect, xRadius: 4, yRadius: 4)
            ghost.fill()
            NSColor.white.withAlphaComponent(0.9).setStroke()
            ghost.setLineDash([4, 3], count: 2, phase: 0)
            ghost.lineWidth = 1.5
            ghost.stroke()
        }
    }

    private func drawOverwriteRegions() {
        for region in overwriteRegions {
            guard let row = row(forTrack: region.track),
                  let node = snapshot?.nodes.first(where: { $0.id == region.nodeID }) else { continue }
            let rect = clipRect(node, in: row, start: region.startFrame, end: region.endFrame)
            let shape = NSBezierPath(roundedRect: rect.insetBy(dx: 1, dy: 1), xRadius: 3, yRadius: 3)
            NSColor.systemOrange.withAlphaComponent(0.40).setFill()
            shape.fill()
            NSColor.systemYellow.withAlphaComponent(0.95).setStroke()
            shape.setLineDash([3, 2], count: 2, phase: 0)
            shape.lineWidth = 1.3
            shape.stroke()
            if rect.width > 66 {
                NSAttributedString(string: "OVERWRITE", attributes: [
                    .font: EditorType.nsClipLabel,
                    .foregroundColor: NSColor.white,
                ]).draw(at: CGPoint(x: rect.minX + 4, y: rect.midY - 5))
            }
            if node.kind.lowercased() == "segment", !node.isPhoto,
               let linkedAudio = rows.first(where: { $0.track.id == "A1" }) {
                drawOverwriteTint(node, in: linkedAudio, start: region.startFrame, end: region.endFrame)
            }
        }
    }

    private func drawOverwriteTint(_ node: EditorNode, in row: TimelineLayout.Row, start: Int, end: Int) {
        let rect = clipRect(node, in: row, start: start, end: end)
        let shape = NSBezierPath(roundedRect: rect.insetBy(dx: 1, dy: 1), xRadius: 3, yRadius: 3)
        NSColor.systemOrange.withAlphaComponent(0.40).setFill()
        shape.fill()
        NSColor.systemYellow.withAlphaComponent(0.95).setStroke()
        shape.setLineDash([3, 2], count: 2, phase: 0)
        shape.lineWidth = 1.3
        shape.stroke()
    }

    private func drawOverwriteGhosts() {
        guard let snapshot else { return }
        for (index, placement) in overwritePlacements.enumerated() {
            guard let row = row(forTrack: placement.track),
                  let duration = OptimisticTimeline.placementDuration(
                    placement, nodes: snapshot.nodes, fps: Int(snapshot.fps.rounded(.toNearestOrEven))
                  ) else { continue }
            let node = placement.id.flatMap { id in snapshot.nodes.first { $0.id == id } }
                ?? insertionGhost(placement, index: index, duration: duration)
            let rect = clipRect(node, in: row, start: placement.atFrame, end: placement.atFrame + duration)
            let color = overwriteBlockedReason == nil ? clipColor(for: node) : NSColor.systemRed
            let shape = NSBezierPath(roundedRect: rect, xRadius: 4, yRadius: 4)
            color.withAlphaComponent(overwriteBlockedReason == nil ? 0.34 : 0.25).setFill()
            shape.fill()
            color.withAlphaComponent(0.95).setStroke()
            shape.setLineDash([4, 3], count: 2, phase: 0)
            shape.lineWidth = 1.5
            shape.stroke()
            let labelHeight: CGFloat = min(15, rect.height * 0.4)
            let labelRect = NSRect(
                x: rect.minX, y: rect.maxY - labelHeight, width: rect.width, height: labelHeight
            )
            color.withAlphaComponent(overwriteBlockedReason == nil ? 0.86 : 0.78).setFill()
            labelRect.fill()
            if rect.width > 44 {
                NSAttributedString(string: overwriteBlockedReason == nil ? displayName(node) : "Blocked", attributes: [
                    .font: EditorType.nsClipLabel,
                    .foregroundColor: NSColor.white,
                ]).draw(at: CGPoint(x: rect.minX + 5, y: labelRect.midY - 5))
            }
            if rect.width > 64 {
                let position = NSAttributedString(string: "@\(timecode(placement.atFrame, fps: snapshot.fps))", attributes: [
                    .font: NSFont.monospacedSystemFont(ofSize: 8, weight: .medium),
                    .foregroundColor: NSColor.white.withAlphaComponent(0.9),
                ])
                position.draw(at: CGPoint(x: rect.minX + 5, y: rect.minY + 5))
            }
            if node.kind.lowercased() == "segment", !node.isPhoto,
               let linkedAudio = rows.first(where: { $0.track.id == "A1" }) {
                let linkedRect = clipRect(node, in: linkedAudio,
                                          start: placement.atFrame, end: placement.atFrame + duration)
                let linked = NSBezierPath(roundedRect: linkedRect, xRadius: 4, yRadius: 4)
                TimelineColors.audio.withAlphaComponent(overwriteBlockedReason == nil ? 0.28 : 0.16).setFill()
                linked.fill()
                NSColor.white.withAlphaComponent(0.75).setStroke()
                linked.setLineDash([4, 3], count: 2, phase: 0)
                linked.lineWidth = 1
                linked.stroke()
            }
        }
    }

    private func insertionGhost(_ placement: TimelinePlacement, index: Int, duration: Int) -> EditorNode {
        let raw = placement.insert ?? .object([:])
        let photo = raw["duration"] != nil || raw["assetKind"]?.stringValue == "photo"
        return EditorNode(
            id: "pending-insert-\(index)", kind: "segment", lane: "primary", parentId: nil,
            startFrame: placement.atFrame, endFrame: placement.atFrame + duration, frames: duration,
            raw: raw, assetId: raw["asset"]?.stringValue, sourceIn: raw["in"]?.numberValue,
            sourceOut: raw["out"]?.numberValue, sourceDuration: raw["sourceDuration"]?.numberValue,
            speed: raw["speed"]?.numberValue ?? 1, assetKind: photo ? "photo" : "video",
            mediaKind: photo ? "photo" : "video", generated: false, readOnly: false, track: placement.track
        )
    }

    /// The track under a vertical drag, if the active clip may move there: mostly-vertical drag,
    /// same kind of track (video/audio), not locked, not its current track. Main clips stay on V1
    /// (they are the reel's spine; tracks only organise overlays, b-roll and sound).
    private func trackMoveTarget(at point: CGPoint) -> String? {
        guard let node = activeNode, node.kind.lowercased() != "segment",
              abs(point.y - downPoint.y) > abs(point.x - downPoint.x),
              let target = row(at: point) else { return nil }
        let currentID = resolvedTrackID(for: node)
        guard let current = rows.first(where: { $0.track.id == currentID }),
              target.track.id != currentID, target.track.isVideo == current.track.isVideo,
              !target.track.locked else { return nil }
        return target.track.id
    }

    private func drawClip(_ node: EditorNode, linked: Bool, in rect: NSRect, row: TimelineLayout.Row) {
        let selected = selectedIDs.contains(node.id)
            || (node.kind.lowercased() == "source_audio" && node.parentId.map(selectedIDs.contains) == true)
        let isAudioClip = linked || category(for: node) == "Audio"
        let base = isAudioClip ? TimelineColors.audio : clipColor(for: node)
        let shape = NSBezierPath(roundedRect: rect, xRadius: 4, yRadius: 4)
        base.blended(withFraction: 0.5, of: .black)?.setFill()
        shape.fill()
        let labelHeight: CGFloat = min(15, rect.height * 0.4)
        let body = NSRect(x: rect.minX, y: rect.minY, width: rect.width, height: rect.height - labelHeight)
        NSGraphicsContext.saveGraphicsState()
        shape.addClip()
        if isAudioClip {
            drawAudioVisual(for: node, in: body)
        } else if ["Video", "B-roll"].contains(category(for: node)) {
            drawThumbnails(for: node, in: body)
        }
        base.withAlphaComponent(selected ? 1 : 0.9).setFill()
        NSRect(x: rect.minX, y: rect.maxY - labelHeight, width: rect.width, height: labelHeight).fill()
        NSGraphicsContext.restoreGraphicsState()
        if row.track.locked {
            NSColor.black.withAlphaComponent(0.3).setFill()
            shape.fill()
        }
        if selected {
            NSColor.white.setStroke()
            let outline = NSBezierPath(roundedRect: rect.insetBy(dx: 0.75, dy: 0.75), xRadius: 4, yRadius: 4)
            outline.lineWidth = 1.5
            outline.stroke()
        }
        if changedIDs.contains(node.id) {
            NSColor.systemYellow.setStroke()
            let outline = NSBezierPath(roundedRect: rect.insetBy(dx: 1.5, dy: 1.5), xRadius: 3, yRadius: 3)
            outline.setLineDash([3, 2], count: 2, phase: 0)
            outline.lineWidth = 1.2
            outline.stroke()
        }
        if rect.width > 30 {
            NSGraphicsContext.saveGraphicsState()
            NSRect(x: rect.minX, y: rect.maxY - labelHeight, width: rect.width, height: labelHeight).clip()
            let para = NSMutableParagraphStyle()
            para.lineBreakMode = .byTruncatingTail
            var textWidth = rect.width - 10
            let fx = hasEffects(node)
            if fx { textWidth -= 18 }
            let name = (linked ? "♪ " : "") + displayName(node)
            let attrs: [NSAttributedString.Key: Any] = [
                .font: EditorType.nsClipLabel, .foregroundColor: NSColor.white, .paragraphStyle: para
            ]
            NSAttributedString(string: name, attributes: attrs).draw(in: NSRect(
                x: rect.minX + 6, y: rect.maxY - labelHeight + (labelHeight - 13) / 2, width: max(0, textWidth), height: 14))
            if fx {
                let badge = NSRect(x: rect.maxX - 20, y: rect.maxY - labelHeight + 2, width: 16, height: labelHeight - 4)
                NSColor.black.withAlphaComponent(0.35).setFill()
                NSBezierPath(roundedRect: badge, xRadius: 3, yRadius: 3).fill()
                let font = NSFontManager.shared.convert(NSFont.systemFont(ofSize: 8.5, weight: .bold), toHaveTrait: .italicFontMask)
                let fxText = NSAttributedString(string: "fx", attributes: [.font: font, .foregroundColor: NSColor.white])
                let size = fxText.size()
                fxText.draw(at: CGPoint(x: badge.midX - size.width / 2, y: badge.midY - size.height / 2))
            }
            NSGraphicsContext.restoreGraphicsState()
        }
        if !node.isReadOnly && !row.track.locked {
            NSColor.white.withAlphaComponent(0.55).setFill()
            NSBezierPath(roundedRect: NSRect(x: rect.minX, y: rect.minY + 4, width: 2, height: max(2, rect.height - 8)), xRadius: 1, yRadius: 1).fill()
            NSBezierPath(roundedRect: NSRect(x: rect.maxX - 2, y: rect.minY + 4, width: 2, height: max(2, rect.height - 8)), xRadius: 1, yRadius: 1).fill()
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

    private func drawAudioVisual(for node: EditorNode, in rect: NSRect) {
        guard rect.width > 2, rect.height > 2, let snapshot else { return }
        let backing = window?.backingScaleFactor ?? 2
        let pixelSize = CGSize(width: min(4096, rect.width * backing), height: rect.height * backing)
        if let image = AudioVisualCache.shared.image(for: node, snapshot: snapshot, mode: AudioVisualCache.shared.mode,
                                                     pixelSize: pixelSize) {
            image.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 0.95)
        } else if AudioVisualCache.shared.mode == .waveform {
            drawWaveform(for: node, in: rect)
        }
    }

    private func drawThumbnails(for node: EditorNode, in rect: NSRect) {
        guard let assetID = node.assetId, sourceURLs[assetID] != nil else { return }
        let tileWidth: CGFloat = 92
        let tileCount = min(6, max(1, Int(rect.width / tileWidth)))
        let sourceIn = node.sourceIn ?? 0
        let sourceOut = max(sourceIn, node.sourceOut ?? sourceIn)
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
                image.draw(in: tileRect, from: .zero, operation: .sourceOver, fraction: 0.8)
            } else if Self.isStillImage(sourceURLs[assetID]!) {
                requestPhotoThumbnail(url: sourceURLs[assetID]!, key: key)
            } else {
                requestThumbnail(url: sourceURLs[assetID]!, seconds: seconds, key: key)
            }
        }
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

    /// Fallback while the shared audio cache is still loading.
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
            let amplitude = max(1, CGFloat(peak) * (rect.height - 3) * 0.46)
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
        guard snapshot != nil else { return [] }
        var elements: [NSAccessibilityElement] = []
        for row in rows {
            for (node, linked) in clips(in: row) {
                let element = NSAccessibilityElement()
                element.setAccessibilityParent(self)
                element.setAccessibilityRole(.button)
                let owner = TimelineLaneClassifier.interactionNode(for: node, nodes: snapshot?.nodes ?? [])
                let accessibleNode = owner ?? node
                let start = timecode(accessibleNode.startFrame, fps: snapshot?.fps ?? 30)
                element.setAccessibilityLabel("\(accessibleNode.title), \(row.track.id)\(linked ? " linked audio" : ""), \(category(for: node)), \(start), \(accessibleNode.frames) frames")
                element.setAccessibilityHelp(owner?.id == node.id
                    ? "Clip handle: drag the left or right edge to trim; drag inside to move."
                    : "Read-only linked audio; interact with its owning video clip.")
                let rect = clipRect(node, in: row)
                element.setAccessibilityFrame(window?.convertToScreen(convert(rect, to: nil)) ?? .zero)
                elements.append(element)
            }
        }
        return elements
    }

    // MARK: drag and drop (music from the library)

    private func musicSlug(_ sender: NSDraggingInfo) -> String? {
        let value = sender.draggingPasteboard.string(forType: .reelsMusicTrack)
        return (value?.isEmpty == false) ? value : nil
    }

    private func timelineItemID(_ sender: NSDraggingInfo) -> String? {
        let value = sender.draggingPasteboard.string(forType: .string)
        return (value?.isEmpty == false) ? value : nil
    }

    private func audioRow(for sender: NSDraggingInfo) -> TimelineLayout.Row? {
        let point = convert(sender.draggingLocation, from: nil)
        guard let row = row(at: point), !row.track.isVideo, !row.track.locked else { return nil }
        return row
    }

    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation { draggingUpdated(sender) }

    override func draggingUpdated(_ sender: NSDraggingInfo) -> NSDragOperation {
        if musicSlug(sender) != nil {
            guard let row = audioRow(for: sender) else {
                dropTrackID = nil
                overwriteDrag = false
                overwriteDropTrackID = nil
                overwritePlacements = []
                overwriteRegions = []
                overwriteBlockedReason = nil
                return []
            }
            dropTrackID = row.track.id
            overwriteDrag = false
            overwriteDropTrackID = nil
            overwritePlacements = []
            overwriteRegions = []
            overwriteBlockedReason = nil
            needsDisplay = true
            return .copy
        }
        guard let itemID = timelineItemID(sender) else {
            draggingExited(sender)
            return []
        }
        overwriteDrag = true
        let point = convert(sender.draggingLocation, from: nil)
        guard let target = TimelineDropLocation.resolveAppKit(
            point, canvasSize: bounds.size, zoom: zoom, tracks: tracks
        ), let row = row(forTrack: target.track) else {
            overwriteDropTrackID = nil
            overwritePlacements = []
            overwriteRegions = []
            overwriteBlockedReason = "Drop footage on a video track."
            NSCursor.operationNotAllowed.set()
            needsDisplay = true
            return .copy
        }
        dropTrackID = nil
        dropItemID = itemID
        dropItemCommandInsert = NSApp.currentEvent?.modifierFlags.contains(.command) == true
        overwriteDropTrackID = row.track.id
        let resolution = callbacks?.onPreviewDrop(
            itemID, target.atFrame, target.track, dropItemCommandInsert
        ) ?? .rejected("Timeline drop is unavailable.")
        overwriteBlockedReason = resolution.rejection
        overwritePlacements = resolution.placement.map { [$0] } ?? []
        overwriteRegions = resolution.placement.map {
            OptimisticTimeline.overwriteRegions(
                placements: [$0], nodes: snapshot?.nodes ?? [],
                fps: Int((snapshot?.fps ?? 30).rounded(.toNearestOrEven))
            )
        } ?? []
        if overwriteBlockedReason != nil {
            NSCursor.operationNotAllowed.set()
        } else {
            NSCursor.arrow.set()
        }
        needsDisplay = true
        return .copy
    }

    override func draggingExited(_ sender: NSDraggingInfo?) {
        dropTrackID = nil
        dropItemID = nil
        overwriteDrag = false
        overwriteDropTrackID = nil
        overwritePlacements = []
        overwriteRegions = []
        overwriteBlockedReason = nil
        needsDisplay = true
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        defer { draggingExited(sender) }
        if let slug = musicSlug(sender), let row = audioRow(for: sender) {
            callbacks?.onDropMusic(slug, row.track.id)
            return true
        }
        guard let itemID = timelineItemID(sender) ?? dropItemID else { return false }
        let point = convert(sender.draggingLocation, from: nil)
        guard let target = TimelineDropLocation.resolveAppKit(
            point, canvasSize: bounds.size, zoom: zoom, tracks: tracks
        ) else {
            callbacks?.onDropRejected(overwriteBlockedReason ?? "Drop footage on a video track.")
            return false
        }
        if let overwriteBlockedReason {
            callbacks?.onDropRejected(overwriteBlockedReason)
            return false
        }
        return callbacks?.onDropItem(
            itemID, target.atFrame, target.track,
            NSApp.currentEvent?.modifierFlags.contains(.command) ?? dropItemCommandInsert
        ) ?? false
    }

    // MARK: mouse

    private func isHeaderPoint(_ point: CGPoint) -> Bool {
        point.x < visibleRect.minX + Self.labelWidth && point.y < bounds.maxY - Self.topInset
    }

    private func handleHeaderClick(_ point: CGPoint) {
        if addButtonRect().contains(point) {
            let menu = addTrackMenu()
            menu.popUp(positioning: nil, at: NSPoint(x: addButtonRect().minX, y: addButtonRect().minY), in: self)
            return
        }
        if let row = row(at: CGPoint(x: Self.labelWidth, y: point.y)) {
            let header = NSRect(x: visibleRect.minX, y: rowRect(row).minY, width: Self.labelWidth, height: row.height)
            if lockRect(for: header).insetBy(dx: -4, dy: -4).contains(point) {
                callbacks?.onToggleLock(row.track.id, !row.track.locked)
            }
        }
    }

    private func addTrackMenu() -> NSMenu {
        let menu = NSMenu()
        let video = NSMenuItem(title: "Add Video Track", action: #selector(addVideoTrack), keyEquivalent: "")
        let audio = NSMenuItem(title: "Add Audio Track", action: #selector(addAudioTrack), keyEquivalent: "")
        video.target = self; audio.target = self
        menu.addItem(video); menu.addItem(audio)
        return menu
    }

    @objc private func addVideoTrack() { callbacks?.onAddTrack("video") }
    @objc private func addAudioTrack() { callbacks?.onAddTrack("audio") }
    @objc private func renameTrackItem(_ sender: NSMenuItem) {
        if let id = sender.representedObject as? String { callbacks?.onRenameTrack(id) }
    }
    @objc private func deleteTrackItem(_ sender: NSMenuItem) {
        if let id = sender.representedObject as? String { callbacks?.onDeleteTrack(id) }
    }
    @objc private func toggleLockItem(_ sender: NSMenuItem) {
        guard let id = sender.representedObject as? String,
              let track = tracks.first(where: { $0.id == id }) else { return }
        callbacks?.onToggleLock(id, !track.locked)
    }

    override func menu(for event: NSEvent) -> NSMenu? {
        let point = convert(event.locationInWindow, from: nil)
        guard isHeaderPoint(point) else { return nil }
        let menu = NSMenu()
        if let row = row(at: CGPoint(x: Self.labelWidth, y: point.y)) {
            let lock = NSMenuItem(title: row.track.locked ? "Unlock Track" : "Lock Track",
                                  action: #selector(toggleLockItem(_:)), keyEquivalent: "")
            lock.target = self; lock.representedObject = row.track.id
            menu.addItem(lock)
            if row.track.userAdded {
                let rename = NSMenuItem(title: "Rename…", action: #selector(renameTrackItem(_:)), keyEquivalent: "")
                rename.target = self; rename.representedObject = row.track.id
                let delete = NSMenuItem(title: "Delete Track", action: #selector(deleteTrackItem(_:)), keyEquivalent: "")
                delete.target = self; delete.representedObject = row.track.id
                menu.addItem(rename); menu.addItem(delete)
            }
            menu.addItem(.separator())
        }
        let video = NSMenuItem(title: "Add Video Track", action: #selector(addVideoTrack), keyEquivalent: "")
        let audio = NSMenuItem(title: "Add Audio Track", action: #selector(addAudioTrack), keyEquivalent: "")
        video.target = self; audio.target = self
        menu.addItem(video); menu.addItem(audio)
        return menu
    }

    /// Pointer travel before a click becomes a move.
    private static let moveThreshold: CGFloat = 4

    private func frame(atX x: CGFloat) -> Int {
        Int(((x - Self.labelWidth) / max(scale, 0.05)).rounded(.toNearestOrEven))
    }

    private func beginLease(owner: String) {
        guard gestureLeaseTask == nil, let begin = callbacks?.onBeginGesture else { return }
        gestureLeaseTask = Task { await begin(owner) }
    }

    /// Hand back a lease that did not turn into an edit.
    private func releaseLease() {
        guard let task = gestureLeaseTask, let end = callbacks?.onEndGesture else { gestureLeaseTask = nil; return }
        gestureLeaseTask = nil
        Task { if let token = await task.value { end(token) } }
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        downPoint = convert(event.locationInWindow, from: nil)
        let point = downPoint
        mode = nil
        activeNode = nil
        releaseLease()
        overwriteDrag = false
        dragNodes = []
        overwritePlacements = []
        overwriteRegions = []
        overwriteBlockedReason = nil
        overwriteDropTrackID = nil
        if isHeaderPoint(point) {
            handleHeaderClick(point)
            return
        }
        if point.x < visibleRect.minX + Self.labelWidth { return } // ruler corner
        if point.y >= bounds.maxY - Self.rulerHeight {
            mode = .scrub
            scrub(point, final: false)
            return
        }
        guard let (node, row) = hit(at: point) else {
            mode = .scrub
            scrub(point, final: false)
            return
        }
        let additive = event.modifierFlags.contains(.command) || event.modifierFlags.contains(.shift)
        callbacks?.onSelect(node.id, additive)
        let overwriteNode = ["segment", "cutaway"].contains(node.kind.lowercased())
        if node.isReadOnly || row.track.locked {
            guard overwriteNode, snapshot?.timelineCapabilities != nil else { return }
            activeNode = node
            originalStart = node.startFrame
            originalEnd = node.endFrame
            dragAnchorStart = node.startFrame
            grabOffset = frame(atX: point.x) - node.startFrame
            mode = .move
            overwriteDrag = true
            dragNodes = [node]
            overwriteBlockedReason = node.isReadOnly
                ? "This source clip is read-only."
                : "Source track \(row.track.name) is locked."
            overwriteDropTrackID = row.track.id
            return
        }
        activeNode = node
        originalStart = node.startFrame
        originalEnd = node.endFrame
        dragAnchorStart = node.startFrame
        grabOffset = frame(atX: point.x) - node.startFrame
        ghostStart = nil
        ghostEnd = nil
        targetNodeID = nil
        let clipStart = Self.labelWidth + CGFloat(node.startFrame) * scale
        let clipEnd = Self.labelWidth + CGFloat(node.endFrame) * scale
        let zone = CGFloat(TimelineMetrics.edgeZone(clipWidth: Double(clipEnd - clipStart)))
        if node.kind.lowercased() == "generated_sfx" { mode = .move }
        else if abs(point.x - clipStart) <= zone { mode = .trimStart }
        else if abs(point.x - clipEnd) <= zone { mode = .trimEnd }
        else { mode = .move }
        if snapshot?.timelineCapabilities != nil && overwriteNode {
            overwriteDrag = true
            let selected = selectedIDs.contains(node.id) ? selectedIDs : [node.id]
            dragNodes = snapshot?.nodes.filter {
                selected.contains($0.id) && ["segment", "cutaway"].contains($0.kind.lowercased())
            } ?? [node]
            if !dragNodes.contains(where: { $0.id == node.id }) { dragNodes.insert(node, at: 0) }
        }
        // No lease yet: a plain click must never hold the document. It is requested on the first real drag.
    }

    override func mouseDragged(with event: NSEvent) {
        guard let mode else { return }
        let point = convert(event.locationInWindow, from: nil)
        switch mode {
        case .scrub:
            autoscroll(with: event)
            scrub(point, final: false)
        case .trimStart, .trimEnd:
            let delta = Int(((point.x - downPoint.x) / max(scale, 0.05)).rounded(.toNearestOrEven))
            if delta != 0 { beginLease(owner: "timeline-trim") }
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
            guard hypot(point.x - downPoint.x, point.y - downPoint.y) > Self.moveThreshold else { return }
            if overwriteDrag {
                if overwriteBlockedReason == nil { beginLease(owner: "timeline-overwrite") }
                updateOverwriteDrag(point)
                if overwriteBlockedReason == nil { NSCursor.closedHand.set() }
                else { NSCursor.operationNotAllowed.set() }
                autoscroll(with: event)
                needsDisplay = true
                return
            }
            beginLease(owner: "timeline-reorder")
            moveTargetTrackID = trackMoveTarget(at: point)
            targetNodeID = moveTargetTrackID == nil ? hit(at: point)?.0.id : nil
            // The clip follows the pointer while it is dragged along its track.
            if moveTargetTrackID == nil {
                let start = max(0, frame(atX: point.x) - grabOffset)
                ghostStart = start
                ghostEnd = start + (originalEnd - originalStart)
            } else {
                ghostStart = nil
                ghostEnd = nil
            }
            autoscroll(with: event)
            needsDisplay = true
        }
    }

    override func mouseUp(with event: NSEvent) {
        defer {
            self.mode = nil
            activeNode = nil
            ghostStart = nil
            ghostEnd = nil
            targetNodeID = nil
            moveTargetTrackID = nil
            snapGuideFrame = nil
            overwriteDrag = false
            dragNodes = []
            overwritePlacements = []
            overwriteRegions = []
            overwriteBlockedReason = nil
            overwriteDropTrackID = nil
            gestureLeaseTask = nil
            needsDisplay = true
        }
        guard let mode else { releaseLease(); return }
        let point = convert(event.locationInWindow, from: nil)
        if overwriteDrag && mode == .move {
            if let reason = overwriteBlockedReason {
                callbacks?.onDropRejected(reason)
            } else if !overwritePlacements.isEmpty {
                callbacks?.onOverwrite(overwritePlacements, UUID().uuidString.lowercased())
            }
            releaseLease()
            return
        }
        // The edit goes out immediately: the app shows it at once and the service saves it
        // in order. The gesture's lease only keeps AI edits out; it is handed back right away.
        func send(_ edit: () -> Void) { edit() }
        let fps = snapshot?.fps ?? 30
        switch mode {
        case .scrub:
            scrub(point, final: true)
        case .trimStart, .trimEnd:
            let changed = (ghostStart != nil && ghostStart != originalStart) || (ghostEnd != nil && ghostEnd != originalEnd)
            guard changed, let node = activeNode else { break }
            let start = ghostStart ?? node.startFrame
            let end = ghostEnd ?? node.endFrame
            let kind = node.kind.lowercased()
            let trimStartMode = mode == .trimStart
            let deltaFrames = start - node.startFrame + end - node.endFrame
            if kind == "cutaway", node.isPhoto {
                send { callbacks?.onTrim(node, Double(start) / fps, Double(max(1, end - start)) / fps, TimelineGhost(start: start, end: end)) }
            } else if kind == "segment", node.isPhoto {
                send { callbacks?.onTrim(node, 0, Double(max(1, end - start)) / fps, TimelineGhost(start: start, end: end)) }
            } else if kind == "cutaway", let sourceIn = node.sourceIn, let sourceOut = node.sourceOut {
                let factor = (sourceOut - sourceIn) / Double(max(node.frames, 1))
                let nextIn = trimStartMode ? sourceIn + Double(deltaFrames) * factor : sourceIn
                let nextOut = !trimStartMode ? sourceOut + Double(deltaFrames) * factor : sourceOut
                send { callbacks?.onTrim(node, max(0, nextIn), max(nextIn + 0.03, nextOut), TimelineGhost(start: start, end: end)) }
            } else if isOverlay(node) {
                send { callbacks?.onTrim(node, Double(start) / fps, Double(max(1, end - start)) / fps, TimelineGhost(start: start, end: end)) }
            } else if let sourceIn = node.sourceIn, let sourceOut = node.sourceOut {
                let factor = (sourceOut - sourceIn) / Double(max(node.frames, 1))
                let nextIn = trimStartMode ? sourceIn + Double(deltaFrames) * factor : sourceIn
                let nextOut = !trimStartMode ? sourceOut + Double(deltaFrames) * factor : sourceOut
                send { callbacks?.onTrim(node, max(0, nextIn), max(nextIn + 0.03, nextOut), TimelineGhost(start: start, end: end)) }
            } else {
                send { callbacks?.onTrim(node, 0, 0, TimelineGhost(start: start, end: end)) }
            }
        case .move:
            if let node = activeNode, let trackID = moveTargetTrackID {
                send { callbacks?.onMoveToTrack(node, trackID) }
            } else if let node = activeNode, hypot(point.x - downPoint.x, point.y - downPoint.y) > Self.moveThreshold {
                let target = targetNodeID == node.id ? nil : targetNodeID
                let start = max(0, frame(atX: point.x) - grabOffset)
                send { callbacks?.onReorder(node, start, target) }
            }
        }
        releaseLease()
    }

    override func keyDown(with event: NSEvent) {
        switch event.keyCode {
        case 49: NotificationCenter.default.post(name: .reelsTogglePlayback, object: nil)
        case 123: callbacks?.onScrub(max(0, playheadFrame - 1), true)
        case 124: callbacks?.onScrub(playheadFrame + 1, true)
        case 11: callbacks?.onSplit()
        case 51, 117: callbacks?.onDelete()
        default: super.keyDown(with: event)
        }
    }

    private func scrub(_ point: CGPoint, final: Bool) {
        let frames = Int(((point.x - Self.labelWidth) / max(scale, 0.05) ).rounded())
        callbacks?.onScrub(max(0, min(snapshot?.durationFrames ?? frames, frames)), final)
    }

    private func hit(at point: CGPoint) -> (EditorNode, TimelineLayout.Row)? {
        guard let row = row(at: point) else { return nil }
        let frame = Double(point.x - Self.labelWidth) / max(scale, 0.05)
        for (node, _) in clips(in: row)
        where frame >= Double(node.startFrame) && frame <= Double(node.endFrame) {
            guard let interactionNode = TimelineLaneClassifier.interactionNode(
                for: node, nodes: snapshot?.nodes ?? []
            ) else { continue }
            return (interactionNode, row)
        }
        return nil
    }

    private func isOverlay(_ node: EditorNode) -> Bool {
        let kind = "\(node.kind) \(node.lane)".lowercased()
        return node.parentId != nil || kind.contains("overlay") || kind.contains("caption")
            || kind.contains("text") || kind.contains("cutaway")
            || kind.contains("b-roll") || kind.contains("broll")
    }
}

/// Instantiates the production AppKit canvas for offscreen visual regression tests.
/// It has no service, library, or media dependencies.
@MainActor
func makeTimelineCanvasCaptureView(
    snapshot: EditorSnapshot,
    tracks: [EditorTrack],
    trackMap: [String: String],
    selectedIDs: Set<String> = [],
    changedIDs: Set<String> = [],
    playheadFrame: Int = 0,
    zoom: Double = TimelineMetrics.defaultZoom,
    snapping: Bool = true,
    onOverwrite: (([TimelinePlacement], String) -> Bool)? = nil,
    onDropRejected: ((String) -> Void)? = nil
) -> NSView {
    let view = TimelineCanvas(frame: .zero)
    view.update(
        snapshot: snapshot,
        tracks: tracks,
        trackMap: trackMap,
        selectedIDs: selectedIDs,
        changedIDs: changedIDs,
        sourceURLs: [:],
        playheadFrame: playheadFrame,
        zoom: zoom,
        snapping: snapping,
        scrollRequest: nil,
        callbacks: TimelineCanvas.Callbacks(
            onSelect: { _, _ in },
            onBeginGesture: { _ in nil },
            onEndGesture: { _ in },
            onScrub: { _, _ in },
            onTrim: { _, _, _, _ in },
            onReorder: { _, _, _ in },
            onOverwrite: onOverwrite ?? { _, _ in true },
            onDropRejected: onDropRejected ?? { _ in },
            onMoveToTrack: { _, _ in },
            onDelete: {},
            onSplit: {},
            onScroll: { _ in },
            onToggleLock: { _, _ in },
            onAddTrack: { _ in },
            onRenameTrack: { _ in },
            onDeleteTrack: { _ in },
            onDropMusic: { _, _ in },
            onPreviewDrop: { _, _, _, _ in .rejected("Preview unavailable") },
            onDropItem: { _, _, _, _ in false }
        )
    )
    return view
}

@MainActor
private final class PlayheadLayer: NSView {
    private var frameNumber = 0
    private var zoom = 0.56
    private var labelWidth: CGFloat = 132
    private var rulerHeight: CGFloat = 30
    private var fps = 30.0

    func update(frame: Int, zoom: Double, labelWidth: CGFloat, rulerHeight: CGFloat, fps: Double) {
        let old = playheadX
        frameNumber = frame
        self.zoom = zoom
        self.labelWidth = labelWidth
        self.rulerHeight = rulerHeight
        self.fps = fps
        setNeedsDisplay(NSRect(x: old - 8, y: 0, width: 17, height: bounds.height))
        setNeedsDisplay(NSRect(x: playheadX - 8, y: 0, width: 17, height: bounds.height))
    }

    private var playheadX: CGFloat { labelWidth + CGFloat(frameNumber) * CGFloat(zoom) }

    override func draw(_ dirtyRect: NSRect) {
        let x = playheadX
        // the pinned track headers cover the left edge of the scrolled content
        guard x >= visibleRect.minX + labelWidth - 1 else { return }
        let blue = TimelineColors.playhead
        let top = bounds.maxY
        blue.setStroke()
        let path = NSBezierPath()
        path.move(to: CGPoint(x: x, y: 0))
        path.line(to: CGPoint(x: x, y: top - 6))
        path.lineWidth = 1.25
        path.stroke()
        blue.setFill()
        let handle = NSBezierPath()
        handle.move(to: CGPoint(x: x - 6.5, y: top - 2))
        handle.line(to: CGPoint(x: x + 6.5, y: top - 2))
        handle.line(to: CGPoint(x: x + 6.5, y: top - 11))
        handle.line(to: CGPoint(x: x, y: top - 18))
        handle.line(to: CGPoint(x: x - 6.5, y: top - 11))
        handle.close()
        handle.fill()
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

/// Where the timeline showed a clip when a trim ended, in reel frames.
struct TimelineGhost: Equatable {
    let start: Int
    let end: Int
}
