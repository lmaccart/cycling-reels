import AppKit
import AVFoundation
import ImageIO
import SwiftUI

enum EditorPalette {
    static let canvas = Color(nsColor: .windowBackgroundColor)
    static let panel = Color(nsColor: .controlBackgroundColor)
    static let raised = Color(nsColor: .underPageBackgroundColor)
    static let separator = Color(nsColor: .separatorColor).opacity(0.45)
    static let accent = Color.accentColor
    static let video = Color(red: 0.22, green: 0.46, blue: 0.78)
    static let overlay = Color(red: 0.58, green: 0.37, blue: 0.72)
    static let audio = Color(red: 0.26, green: 0.58, blue: 0.44)
    static let cutaway = Color(red: 0.24, green: 0.55, blue: 0.58)
    static let warning = Color.orange
}

struct ContentView: View {
    @Bindable var store: EditorStore

    var body: some View {
        VStack(spacing: 0) {
            EditorToolbar(store: store)
            NativeSplitView(
                views: [AnyView(workspace), AnyView(TimelinePanel(store: store))],
                vertical: false,
                autosaveName: "ReelsEditor.WorkspaceTimeline",
                minimumSizes: [250, 190],
                initialFractions: [0.58, 0.42]
            )
            .background(EditorPalette.canvas)
            StatusBar(store: store)
        }
        .background(EditorPalette.canvas)
        .background(ShortcutMonitor { store.performShortcut($0) })
        .alert("Editing service", isPresented: Binding(
            get: { store.errorMessage != nil },
            set: { if !$0 { store.errorMessage = nil } }
        )) {
            Button("Dismiss", role: .cancel) { store.errorMessage = nil }
        } message: {
            Text(store.errorMessage ?? "")
        }
        .sheet(isPresented: Binding(
            get: { store.schedule.isPresented },
            set: { store.schedule.isPresented = $0 }
        )) {
            ScheduleSheet(store: store)
        }
        .alert("Permission request", isPresented: Binding(
            get: { store.isPermissionPromptPresented && store.activePermission != nil },
            set: { store.isPermissionPromptPresented = $0 }
        )) {
            Button("Grant") { store.respondToPermission(granted: true) }
            Button("Deny", role: .destructive) { store.respondToPermission(granted: false) }
            Button("Later", role: .cancel) { store.isPermissionPromptPresented = false }
        } message: {
            Text(store.permissionPrompt)
        }
        .onReceive(NotificationCenter.default.publisher(for: .reelsEditorCaptureQuit)) { _ in
            store.stopServiceForCapture()
        }
        .onReceive(NotificationCenter.default.publisher(for: .reelsEditorPreviewTimedOut)) { _ in
            store.previewFailureMessage = "Fast preview was not ready within the 20-second capture deadline."
        }
    }

    private var workspace: some View {
        NativeSplitView(
            views: [
                AnyView(LibraryPane(store: store)),
                AnyView(monitorAndInspector)
            ],
            vertical: true,
            autosaveName: "ReelsEditor.ProjectMonitorsInspector",
            minimumSizes: [196, 690],
            initialFractions: [0.19, 0.81]
        )
    }

    private var monitorAndInspector: some View {
        NativeSplitView(
            views: [
                AnyView(MonitorPanel(store: store)),
                AnyView(RightRail(store: store))
            ],
            vertical: true,
            autosaveName: "ReelsEditor.MonitorsInspector",
            minimumSizes: [390, 274],
            initialFractions: [0.69, 0.31]
        )
    }
}

private struct EditorToolbar: View {
    @Bindable var store: EditorStore

    var body: some View {
        HStack(spacing: 8) {
            VStack(alignment: .leading, spacing: 1) {
                Text(store.hasDocument ? store.reelTitle : "Reels Editor")
                    .font(.system(size: 13, weight: .semibold))
                    .lineLimit(1)
                    .truncationMode(.tail)
                if let file = store.planFileLabel {
                    Text(file)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                        .help("Edits save to this file as you make them. Claude Code and ./reels render read the same file.")
                }
            }
            .frame(minWidth: 120, maxWidth: 260, alignment: .leading)
            .accessibilityElement(children: .combine)
            toolbarDivider
            Button { store.undo() } label: { Image(systemName: "arrow.uturn.backward") }
                .help("Undo  ⌘Z")
                .accessibilityLabel("Undo")
                .disabled(!store.hasDocument)
            Button { store.redo() } label: { Image(systemName: "arrow.uturn.forward") }
                .help("Redo  ⇧⌘Z")
                .accessibilityLabel("Redo")
                .disabled(!store.hasDocument)
            toolbarDivider
            Button { store.splitAtPlayhead() } label: { Image(systemName: "scissors") }
                .help("Split the selected clip at the playhead  ⌘B")
                .accessibilityLabel("Split at playhead")
                .disabled(store.selectedNode == nil)
            Button { store.deleteSelection() } label: { Image(systemName: "trash") }
                .help("Delete the selected clip  ⌫")
                .accessibilityLabel("Delete selection")
                .disabled(store.selectedIDs.isEmpty)
            toolbarDivider
            Picker("Preview", selection: Binding(
                get: { store.currentPreviewMode },
                set: { store.preparePreview(mode: $0) }
            )) {
                Text(PreviewModeLabel.title("interactive")).tag("interactive")
                Text(PreviewModeLabel.title("exact")).tag("exact")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .accessibilityLabel("Preview quality")
            .help(PreviewModeLabel.help(store.currentPreviewMode))
            .frame(width: 210)
            .disabled(!store.hasDocument)
            Button {
                store.preparePreview(mode: store.currentPreviewMode)
            } label: {
                if store.isPreparingPreview {
                    ProgressView().controlSize(.mini)
                } else {
                    Image(systemName: "arrow.clockwise")
                }
            }
            .disabled(store.isPreparingPreview || !store.hasDocument)
            .help(store.isPreparingPreview ? "Preparing preview…" : "Reload the preview from the latest edits")
            .accessibilityLabel(store.isPreparingPreview ? "Preparing preview" : "Reload preview")
            Spacer(minLength: 12)
            ConnectionBadge(state: store.serviceState)
            if store.activePermission != nil && !store.isPermissionPromptPresented {
                Button {
                    store.isPermissionPromptPresented = true
                } label: {
                    Label("Permission", systemImage: "lock")
                }
                .help("Review a pending permission request")
            }
            Button { store.beginSchedule() } label: {
                Label("Schedule…", systemImage: "calendar.badge.clock")
                    .font(EditorType.bodyEmphasis)
                    .padding(.horizontal, 10).padding(.vertical, 4)
                    .background(EditorPalette.accent, in: RoundedRectangle(cornerRadius: 6))
                    .foregroundStyle(.white)
            }
            .disabled(!store.hasDocument)
            .help("Send this reel to Instagram, TikTok and YouTube through Metricool. Nothing posts until you confirm.")
        }
        .buttonStyle(.borderless)
        .controlSize(.regular)
        .padding(.leading, 80)  // clears the window's traffic-light buttons
        .padding(.trailing, 12)
        .frame(height: 40)
        .background(WindowDragArea())
        .background(EditorPalette.panel)
        .overlay(alignment: .bottom) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
    }

    private var toolbarDivider: some View {
        Rectangle().fill(EditorPalette.separator).frame(width: 1, height: 20).accessibilityHidden(true)
    }
}

private struct ConnectionBadge: View {
    let state: EditorService.ServiceState

    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(color)
                .frame(width: 7, height: 7)
            Text(label)
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
        }
        .help(helpText)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Editing service: \(label)")
    }

    private var label: String {
        switch state {
        case .stopped: "Offline"
        case .starting: "Starting…"
        case .connected: "Connected"
        case .failed: "Disconnected"
        }
    }

    private var helpText: String {
        switch state {
        case .connected: "The editing service is running. Edits save to plans/ as you make them."
        case .starting: "Starting the editing service…"
        case .stopped, .failed: "The editing service is not running, so edits can't be saved. Reopen the reel or restart the app."
        }
    }

    private var color: Color {
        switch state {
        case .stopped, .failed: .red
        case .starting: .orange
        case .connected: .green
        }
    }
}

private struct LibraryPane: View {
    @Bindable var store: EditorStore
    @State private var section = "Footage"
    @State private var localQuery = ""
    private var localClips: [JSONValue] { store.library.filter { AssetPresentation.matches($0, query: localQuery) } }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Picker("Browser", selection: $section) {
                    Text("Footage").tag("Footage")
                    Text("Reels").tag("Projects")
                    Text("Music").tag("Music")
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .accessibilityLabel("Library section")
                .frame(maxWidth: .infinity)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 9)

            if section == "Footage" {
                HStack(spacing: 7) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Search what was said or shown", text: $localQuery)
                        .textFieldStyle(.plain)
                        .onChange(of: localQuery) { _, value in store.search(value) }
                        .accessibilityLabel("Search footage")
                    if !localQuery.isEmpty {
                        Button { localQuery = ""; store.search("") } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain).foregroundStyle(.secondary)
                            .accessibilityLabel("Clear search")
                    }
                }
                .font(.system(size: 12))
                .padding(.horizontal, 9)
                .padding(.vertical, 7)
                .background(EditorPalette.raised, in: RoundedRectangle(cornerRadius: 5))
                .padding(.horizontal, 8)
                .padding(.bottom, 7)

                HStack {
                    Text(localQuery.isEmpty ? "Clips" : "Matches")
                    Spacer()
                    Text(localQuery.isEmpty ? "\(store.library.count)" : "\(store.searchHits.count + localClips.count)")
                        .monospacedDigit()
                }
                .font(.system(size: 11, weight: .medium))
                .foregroundStyle(.secondary)
                .padding(.horizontal, 12)
                .padding(.bottom, 4)
                Divider()
                if localQuery.isEmpty {
                    libraryList
                } else {
                    searchList
                }
            } else if section == "Music" {
                Divider()
                MusicPanel(store: store)
            } else {
                Divider()
                plansList
            }
        }
        .background(EditorPalette.panel)
    }

    private var libraryList: some View {
        List(selection: Binding(
            get: { store.selectedAssetID },
            set: { id in if let id, let item = store.library.first(where: { $0["id"]?.stringValue == id }) { store.selectAsset(item) } }
        )) {
            if !store.flags.isEmpty {
                Section("Flagged") {
                    ForEach(store.flags) { flag in
                        FlagRow(store: store, flag: flag)
                    }
                }
            }
            Section {
                ForEach(Array(store.library.enumerated()), id: \.offset) { _, item in
                    let id = item["id"]?.stringValue ?? UUID().uuidString
                    AssetRow(item: item, root: store.repositoryRoot, flagged: store.isFlagged(assetID: item["id"]?.stringValue))
                        .tag(id)
                        .draggable(id)
                }
            } header: {
                if !store.flags.isEmpty { Text("Clips") }
            }
        }
        .listStyle(.sidebar)
        .overlay {
            if store.library.isEmpty {
                ContentUnavailableView("No footage yet", systemImage: "film", description: Text("Put videos in inbox/ and run ./reels ingest, or import footage from the File menu. Clips show up here."))
            }
        }
    }

    private var searchList: some View {
        List(selection: Binding(
            get: { store.selectedAssetID },
            set: { id in if let id, let item = store.library.first(where: { $0["id"]?.stringValue == id }) { store.selectAsset(item) } }
        )) {
            let clips = localClips
            if !clips.isEmpty {
                Section("Clips") {
                    ForEach(Array(clips.prefix(50).enumerated()), id: \.offset) { _, item in
                        let id = item["id"]?.stringValue ?? UUID().uuidString
                        AssetRow(item: item, root: store.repositoryRoot, flagged: store.isFlagged(assetID: item["id"]?.stringValue)).tag(id).draggable(id)
                    }
                }
            }
            if store.searchHits.contains(where: { !$0.related }) {
                Section("Exact matches") {
                    ForEach(store.searchHits.filter { !$0.related }) { hit in
                        SearchHitRow(
                            hit: hit,
                            addAsBroll: { store.addSearchHitAsBroll(hit) },
                            replaceCutaway: store.selectedCutaway == nil
                                ? nil
                                : { store.replaceSelectedCutaway(with: hit) }
                        )
                    }
                }
            }
        }
        .listStyle(.sidebar)
        .overlay {
            if AssetPresentation.showsNoResults(clipCount: localClips.count, hitCount: store.searchHits.count, searchInFlight: store.searchInFlight) {
                ContentUnavailableView("No matching footage", systemImage: "text.magnifyingglass",
                                       description: Text("Try a word from the transcript, a place, or an activity. Regular expressions work too."))
            }
        }
    }

    private var plansList: some View {
        List {
            ForEach(Array(store.plans.enumerated()), id: \.offset) { _, plan in
                let path = plan["path"]?.stringValue
                let isOpen = path != nil && path == store.planFileLabel
                Button {
                    if let path { Task { await store.openPlan(path) } }
                } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(plan["title"]?.stringValue ?? plan["slug"]?.stringValue ?? plan["name"]?.stringValue ?? "Untitled reel")
                            .font(.system(size: 12, weight: .medium))
                            .lineLimit(2)
                        Text(PlanRowSummary.detail(plan))
                            .font(.system(size: 11))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .padding(.vertical, 3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(path == nil)
                .listRowBackground(isOpen ? EditorPalette.accent.opacity(0.18) : Color.clear)
                .accessibilityAddTraits(isOpen ? .isSelected : [])
                .help(path ?? "")
            }
        }
        .listStyle(.sidebar)
        .overlay {
            if store.plans.isEmpty {
                ContentUnavailableView("No reels yet", systemImage: "doc.text", description: Text("Reels are the files in plans/. Create one from the File menu, or ask Claude Code to plan one."))
            }
        }
    }
}

/// One-line summary under a reel's title in the Reels list.
enum PlanRowSummary {
    static func detail(_ plan: JSONValue) -> String {
        var parts: [String] = []
        if let date = plan["postDate"]?.stringValue { parts.append(date) }
        if let count = plan["segmentCount"]?.numberValue {
            parts.append("\(Int(count)) clip\(Int(count) == 1 ? "" : "s")")
        }
        if let seconds = plan["durationSeconds"]?.numberValue { parts.append(TimeFormat.clock(seconds)) }
        if let status = plan["status"]?.stringValue { parts.append(status) }
        if parts.isEmpty { parts.append(plan["path"]?.stringValue ?? "") }
        return parts.joined(separator: " · ")
    }
}

/// A flagged range in the Library: click to open it in Source with In/Out marked.
private struct FlagRow: View {
    @Bindable var store: EditorStore
    let flag: ClipFlag
    @State private var editing = false
    @State private var draft = ""

    private var asset: JSONValue? { store.library.first { $0["id"]?.stringValue == flag.asset } }

    var body: some View {
        Button { store.openFlag(flag) } label: {
            HStack(spacing: 8) {
                Image(systemName: "flag.fill")
                    .foregroundStyle(EditorPalette.warning)
                    .font(.system(size: 11))
                VStack(alignment: .leading, spacing: 2) {
                    Text(flag.note ?? asset.map(AssetPresentation.title) ?? flag.asset)
                        .font(.system(size: 11, weight: .medium))
                        .lineLimit(2)
                    Text([flag.range, asset.map(AssetPresentation.title).flatMap { flag.note == nil ? nil : $0 } ?? flag.asset]
                        .compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                Spacer(minLength: 0)
            }
            .padding(.vertical, 2)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("Open in Source with this range marked")
        .contextMenu {
            Button("Edit note…") { draft = flag.note ?? ""; editing = true }
            Button("Remove flag", role: .destructive) { store.removeFlag(flag) }
        }
        .popover(isPresented: $editing) {
            FlagNoteEditor(title: "Flag note", note: $draft, confirm: "Save") {
                store.renameFlag(flag, note: draft)
                editing = false
            }
        }
        .accessibilityLabel("Flagged clip \(flag.note ?? flag.asset)")
    }
}

/// The Source monitor's Flag button: flags the marked range (In–Out, In + 5 s, or the whole clip).
private struct FlagSourceButton: View {
    @Bindable var store: EditorStore
    @State private var asking = false
    @State private var note = ""

    var body: some View {
        Button { note = ""; asking = true } label: {
            Label("Flag", systemImage: "flag")
        }
        .disabled(store.selectedAssetID == nil)
        .help("Flag this clip (the marked In–Out range) to come back to")
        .popover(isPresented: $asking) {
            FlagNoteEditor(title: flagTitle, note: $note, confirm: "Flag") {
                store.flagSource(note: note)
                asking = false
            }
        }
    }

    private var flagTitle: String {
        if store.selectedAssetIsPhoto { return "Flag this photo" }
        let range = store.sourceFlagRange
        guard let a = range.in, let b = range.out else { return "Flag the whole clip" }
        return "Flag \(TimeFormat.precise(a))–\(TimeFormat.precise(b))"
    }
}

private struct FlagNoteEditor: View {
    let title: String
    @Binding var note: String
    let confirm: String
    let action: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(EditorType.section)
            TextField("Note (optional)", text: $note)
                .textFieldStyle(.roundedBorder)
                .frame(width: 240)
                .onSubmit(action)
            HStack {
                Spacer()
                Button(confirm, action: action).keyboardShortcut(.defaultAction)
            }
        }
        .padding(12)
    }
}

private struct AssetRow: View {
    let item: JSONValue
    var root: URL?
    var flagged = false

    private var detail: String {
        AssetPresentation.detailParts(item, currentYear: Calendar.current.component(.year, from: Date()))
            .joined(separator: " · ")
    }

    var body: some View {
        HStack(spacing: 8) {
            AssetThumbnail(item: item, root: root)
            VStack(alignment: .leading, spacing: 2) {
                Text(AssetPresentation.title(item))
                    .font(.system(size: 11, weight: .medium))
                    .lineLimit(2)
                HStack(spacing: 4) {
                    if flagged {
                        Image(systemName: "flag.fill")
                            .font(.system(size: 9))
                            .foregroundStyle(EditorPalette.warning)
                            .help("Flagged")
                    }
                    if AssetPresentation.hasTranscript(item) {
                        Image(systemName: "text.bubble")
                            .font(.system(size: 9))
                            .help("Has a transcript")
                    }
                    Text(detail).lineLimit(1)
                }
                .font(.system(size: 10))
                .foregroundStyle(.secondary)
            }
        }
        .padding(.vertical, 2)
        .help(item["originalName"]?.stringValue ?? "")
        .accessibilityElement(children: .combine)
    }
}

/// Small frame grab (or the photo itself) read from the read-only originals in library/media.
private struct AssetThumbnail: View {
    let item: JSONValue
    var root: URL?
    @State private var image: NSImage?

    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 3).fill(EditorPalette.video.opacity(0.3))
            if let image {
                Image(nsImage: image).resizable().scaledToFill()
            } else {
                Image(systemName: item["kind"]?.stringValue == "photo" ? "photo" : "play.fill")
                    .font(.system(size: 10)).foregroundStyle(EditorPalette.video)
            }
        }
        .frame(width: 46, height: 30)
        .clipShape(RoundedRectangle(cornerRadius: 3))
        .accessibilityHidden(true)
        .task(id: item["id"]?.stringValue) {
            guard let root, let media = item["media"]?.stringValue else { return }
            image = await ThumbnailCache.shared.thumbnail(
                for: root.appendingPathComponent(media), isPhoto: item["kind"]?.stringValue == "photo")
        }
    }
}

final class ThumbnailCache: @unchecked Sendable {
    static let shared = ThumbnailCache()
    private let cache = NSCache<NSURL, NSImage>()

    func thumbnail(for url: URL, isPhoto: Bool) async -> NSImage? {
        if let hit = cache.object(forKey: url as NSURL) { return hit }
        let result: NSImage? = await Task.detached(priority: .utility) { () -> NSImage? in
            guard FileManager.default.fileExists(atPath: url.path) else { return nil }
            if isPhoto {
                guard let source = CGImageSourceCreateWithURL(url as CFURL, nil) else { return nil }
                let options: [CFString: Any] = [
                    kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceCreateThumbnailWithTransform: true,
                    kCGImageSourceThumbnailMaxPixelSize: 160
                ]
                guard let cg = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
                return NSImage(cgImage: cg, size: NSSize(width: cg.width, height: cg.height))
            }
            let generator = AVAssetImageGenerator(asset: AVURLAsset(url: url))
            generator.appliesPreferredTrackTransform = true
            generator.maximumSize = CGSize(width: 160, height: 160)
            guard let cg = try? await generator.image(at: CMTime(seconds: 0.4, preferredTimescale: 600)).image else { return nil }
            return NSImage(cgImage: cg, size: NSSize(width: cg.width, height: cg.height))
        }.value
        if let result { cache.setObject(result, forKey: url as NSURL) }
        return result
    }
}

private struct SearchHitRow: View {
    let hit: SearchHit
    let addAsBroll: () -> Void
    var replaceCutaway: (() -> Void)?
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            HStack(spacing: 5) {
                Image(systemName: hit.related ? "sparkle.magnifyingglass" : "film")
                    .foregroundStyle(hit.related ? .purple : EditorPalette.video)
                    .accessibilityHidden(true)
                Text(hit.title).font(.system(size: 11, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 2)
                Text("\(hit.sourceIn.formatted(.number.precision(.fractionLength(1))))s")
                    .font(.system(size: 10, design: .monospaced)).foregroundStyle(.tertiary)
            }
            if !hit.evidence.isEmpty {
                Text(highlightedEvidence(hit.evidence, offsets: hit.raw["highlights"]?.arrayValue ?? []))
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            if hit.related || hit.relatedReason != nil || !hit.sourceRanges.isEmpty {
                Label("Related moment", systemImage: "sparkle.magnifyingglass")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.purple)
                    .labelStyle(.titleAndIcon)
                if let reason = hit.relatedReason, !reason.isEmpty {
                    Text(reason)
                        .font(.system(size: 10))
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
                ForEach(hit.sourceRanges.prefix(4)) { range in
                    VStack(alignment: .leading, spacing: 2) {
                        Text("\(range.sourceIn.formatted(.number.precision(.fractionLength(1))))–\(range.sourceOut.formatted(.number.precision(.fractionLength(1))))s")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.tertiary)
                        if !range.evidence.isEmpty {
                            Text(highlightedEvidence(range.evidence, offsets: range.highlights))
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        if !range.reason.isEmpty {
                            Text(range.reason)
                                .font(.system(size: 10))
                                .foregroundStyle(.tertiary)
                                .lineLimit(2)
                        }
                    }
                    .padding(.leading, 9)
                }
                if hit.sourceRanges.count > 4 {
                    Text("+ \(hit.sourceRanges.count - 4) more source ranges")
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .padding(.leading, 9)
                }
            }
            HStack {
                Spacer(minLength: 2)
                Menu {
                    Button("Add as B-roll", systemImage: "rectangle.on.rectangle", action: addAsBroll)
                    if let replaceCutaway {
                        Button("Replace selected cutaway source", systemImage: "arrow.triangle.2.circlepath", action: replaceCutaway)
                    }
                } label: {
                    Label("B-roll", systemImage: "rectangle.on.rectangle")
                }
                .labelStyle(.titleAndIcon)
                .controlSize(.mini)
                .menuStyle(.borderlessButton)
                .accessibilityLabel("B-roll actions for \(hit.title)")
            }
        }
        .padding(.vertical, 3)
        .draggable(hit.id)
        .contextMenu {
            Button("Add as B-roll", systemImage: "rectangle.on.rectangle", action: addAsBroll)
            if let replaceCutaway {
                Button("Replace selected cutaway source", systemImage: "arrow.triangle.2.circlepath", action: replaceCutaway)
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(hit.related ? "Related match" : "Exact match"), \(hit.title), source \(hit.sourceIn) to \(hit.sourceOut) seconds")
        .accessibilityHint("Drag to the timeline for a primary clip, or open the context menu for B-roll actions.")
    }
}

private func highlightedEvidence(_ text: String, offsets: [JSONValue]) -> AttributedString {
    var result = AttributedString(text)
    for offset in offsets {
        guard let startValue = offset["start"]?.numberValue,
              let endValue = offset["end"]?.numberValue else { continue }
        let start = Int(startValue)
        let end = Int(endValue)
        guard let stringRange = UnicodeCodepointOffsets.stringRange(in: text, start: start, end: end),
              let lower = AttributedString.Index(stringRange.lowerBound, within: result),
              let upper = AttributedString.Index(stringRange.upperBound, within: result) else { continue }
        result[lower..<upper].backgroundColor = Color.yellow.opacity(0.24)
        result[lower..<upper].foregroundColor = Color.primary
    }
    return result
}

private struct MonitorPanel: View {
    @Bindable var store: EditorStore
    @StateObject private var sourceModel = SourcePlayerModel()

    var body: some View {
        GeometryReader { proxy in
            if proxy.size.width >= 740 {
                NativeSplitView(
                    views: [AnyView(sourceSurface), AnyView(programSurface)],
                    vertical: true,
                    autosaveName: "ReelsEditor.SourceProgram",
                    minimumSizes: [250, 250],
                    initialFractions: [0.5, 0.5]
                )
            } else {
                VStack(spacing: 0) {
                    HStack {
                        Picker("Monitor", selection: $store.activeTab) {
                            Text("Source").tag("source")
                            Text("Program").tag("program")
                        }
                        .labelsHidden()
                        .pickerStyle(.segmented)
                        .frame(width: 184)
                        Spacer()
                        monitorActions(source: store.activeTab == "source")
                    }
                    .controlSize(.small)
                    .padding(.horizontal, 10)
                    .frame(height: 34)
                    .background(EditorPalette.panel)
                    Divider()
                    selectedMonitor
                    selectedControls
                }
            }
        }
        .background(EditorPalette.panel)
    }

    private var sourceSurface: some View {
        VStack(spacing: 0) {
            HStack {
                monitorTitle("Source")
                Spacer()
                monitorActions(source: true)
            }
            .padding(.horizontal, 9)
            .frame(height: 32)
            .background(EditorPalette.panel)
            Divider()
            sourceMonitor
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            SourceControls(store: store, model: sourceModel)
        }
        .background(EditorPalette.panel)
    }

    private var programSurface: some View {
        VStack(spacing: 0) {
            HStack {
                monitorTitle("Program")
                Spacer()
                monitorActions(source: false)
            }
            .padding(.horizontal, 9)
            .frame(height: 32)
            .background(EditorPalette.panel)
            Divider()
            programMonitor
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            ProgramControls(store: store)
        }
        .background(EditorPalette.panel)
    }

    private var selectedMonitor: some View {
        Group {
            if store.activeTab == "source" {
                sourceMonitor
            } else {
                programMonitor
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
    }

    @ViewBuilder
    private var programMonitor: some View {
        if store.sphereAim.isActive {
            SphereAimPanel(store: store, aim: store.sphereAim)
        } else {
            ProgramMonitor(store: store)
        }
    }

    @ViewBuilder
    private var sourceMonitor: some View {
        if let assetID = store.selectedAssetID {
            SourceMonitor(store: store, model: sourceModel).draggable(assetID)
        } else {
            SourceMonitor(store: store, model: sourceModel)
        }
    }

    @ViewBuilder
    private var selectedControls: some View {
        if store.activeTab == "source" {
            SourceControls(store: store, model: sourceModel)
        } else {
            ProgramControls(store: store)
        }
    }

    private func monitorTitle(_ title: String) -> some View {
        PanelTitle(title)
    }

    @ViewBuilder
    private func monitorActions(source: Bool) -> some View {
        if source {
            FlagSourceButton(store: store)
            Button { store.insertFromSource() } label: {
                Label("Insert", systemImage: "arrow.down.to.line")
            }
            .disabled(store.selectedAssetID == nil || !store.hasDocument)
            .help("Insert the marked range (or the whole clip) into the reel  ,")
            if store.selectedCutaway != nil {
                Button { store.replaceSelectedCutawayFromSource() } label: {
                    Label("Replace B-roll", systemImage: "arrow.triangle.2.circlepath")
                }
                .disabled(store.selectedAssetID == nil)
                .help("Replace only the selected cutaway source range")
            } else {
                Button { store.addSelectedAssetAsBroll() } label: {
                    Label("Add as B-roll", systemImage: "rectangle.on.rectangle")
                }
                .disabled(store.selectedAssetID == nil || store.selectedPrimaryForBroll == nil)
                .help("Lay this clip over the selected clip as a cutaway. Select a clip on the timeline first.")
            }
        } else {
            if store.sphereAim.isActive {
                Text("360° aim")
                    .font(EditorType.bodyEmphasis)
                    .foregroundStyle(EditorPalette.accent)
            } else if store.selectedSphereNode != nil {
                Button { store.openSphereAim() } label: {
                    Label("Aim 360°", systemImage: "rotate.3d")
                }
                .help("Aim the selected 360 clip: drag to look around, scroll to zoom, set keyframes and stabilization")
            }
            Text(PreviewModeLabel.title(store.currentPreviewMode))
                .font(EditorType.bodyEmphasis)
                .foregroundStyle(.secondary)
                .help(PreviewModeLabel.help(store.currentPreviewMode))
            Button { store.preparePreview(mode: store.currentPreviewMode) } label: {
                Image(systemName: "arrow.clockwise")
            }
            .help("Reload the preview from the latest edits")
            .accessibilityLabel("Reload preview")
        }
    }
}

private struct SourceControls: View {
    @Bindable var store: EditorStore
    @ObservedObject var model: SourcePlayerModel

    var body: some View {
        HStack(spacing: 8) {
            if store.selectedAssetIsPhoto {
                Image(systemName: "photo").foregroundStyle(EditorPalette.video)
                Text("Photo")
                Text("Set how long it shows in the Inspector")
                    .foregroundStyle(.secondary)
                Spacer()
                if let id = store.selectedAssetID {
                    Button { _ = store.insertDroppedItem(id, atFrame: store.playheadFrame, trackID: "V1",
                                                         sourceIn: store.sourceIn, sourceOut: store.sourceOut) } label: {
                        Label("Insert photo", systemImage: "arrow.down.to.line")
                    }
                }
            } else {
                Button { model.togglePlayback() } label: {
                    Image(systemName: model.isPlaying ? "pause.fill" : "play.fill")
                }
                .help("Play / pause the source clip")
                .accessibilityLabel(model.isPlaying ? "Pause source" : "Play source")
                Button("Set In") { setMark(in: true) }
                    .help("Mark In at the current position  I")
                Text(store.sourceIn.map(TimeFormat.precise) ?? "—")
                    .font(EditorType.timecode)
                Button("Set Out") { setMark(in: false) }
                    .help("Mark Out at the current position  O")
                Text(store.sourceOut.map(TimeFormat.precise) ?? "—")
                    .font(EditorType.timecode)
                if model.durationSeconds > 0 {
                    Slider(value: Binding(
                        get: { model.currentSeconds },
                        set: { model.seek(to: $0) }
                    ), in: 0...model.durationSeconds)
                    .frame(minWidth: 60, maxWidth: 140)
                    .accessibilityLabel("Source position")
                    Text(TimeFormat.precise(model.currentSeconds))
                        .font(EditorType.timecodeLarge)
                        .foregroundStyle(EditorPalette.accent)
                }
                Spacer(minLength: 0)
                Button { store.insertFromSource() } label: { Image(systemName: "arrow.down.to.line") }
                    .help("Insert the marked range into the reel  ,")
                    .accessibilityLabel("Insert into reel")
                    .disabled(store.selectedAssetID == nil || !store.hasDocument)
            }
        }
        .font(EditorType.body)
        .buttonStyle(.borderless)
        .controlSize(.small)
        .lineLimit(1)
        .padding(.horizontal, 10)
        .frame(height: 34)
        .background(EditorPalette.panel)
        .overlay(alignment: .top) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
        .onReceive(NotificationCenter.default.publisher(for: .reelsMarkSourceIn)) { _ in
            if !store.selectedAssetIsPhoto, model.player != nil { setMark(in: true) }
        }
        .onReceive(NotificationCenter.default.publisher(for: .reelsMarkSourceOut)) { _ in
            if !store.selectedAssetIsPhoto, model.player != nil { setMark(in: false) }
        }
    }

    private func setMark(in isIn: Bool) {
        let seconds = model.currentSeconds
        if isIn { store.setSourceIn(seconds) } else { store.setSourceOut(seconds) }
    }
}

private struct ProgramControls: View {
    @Bindable var store: EditorStore

    var body: some View {
        HStack(spacing: 8) {
            Button { store.setPlayhead(store.playheadFrame - 1) } label: { Image(systemName: "backward.frame") }
                .help("Previous frame  ←")
                .accessibilityLabel("Previous frame")
            Button { NotificationCenter.default.post(name: .reelsTogglePlayback, object: nil) } label: {
                Image(systemName: "playpause.fill")
            }
            .help("Play / pause  Space or K")
            .accessibilityLabel("Play or pause")
            Button { store.setPlayhead(store.playheadFrame + 1) } label: { Image(systemName: "forward.frame") }
                .help("Next frame  →")
                .accessibilityLabel("Next frame")
            Text(TimeFormat.timecode(store.playheadFrame, fps: store.fps))
                .font(EditorType.timecodeLarge)
                .foregroundStyle(EditorPalette.accent)
                .padding(.horizontal, 4)
                .help("Playhead timecode (hours:minutes:seconds:frames)")
            Picker("Playback speed", selection: Binding(
                get: { store.playbackRate },
                set: { store.playbackRate = $0 }
            )) {
                Text("0.5×").tag(0.5)
                Text("1×").tag(1.0)
                Text("1.5×").tag(1.5)
                Text("2×").tag(2.0)
            }
            .labelsHidden()
            .pickerStyle(.menu)
            .frame(width: 62)
            .help("Playback speed")
            .onChange(of: store.playbackRate) { _, value in
                NotificationCenter.default.post(name: .reelsPlaybackRate, object: value)
            }
            Spacer()
            if store.snapshot != nil {
                if store.isPreparingPreview {
                    Label("Updating preview…", systemImage: "clock")
                        .foregroundStyle(EditorPalette.warning)
                } else if let snapshot = store.snapshot {
                    Text("Preview ready")
                        .foregroundStyle(.secondary)
                        .help("Revision \(snapshot.revisions.document.prefix(8)); renderer \(snapshot.revisions.renderer.prefix(8))")
                }
            }
        }
        .font(EditorType.bodyEmphasis)
        .buttonStyle(.borderless)
        .controlSize(.small)
        .lineLimit(1)
        .padding(.horizontal, 10)
        .frame(height: 34)
        .background(EditorPalette.panel)
        .overlay(alignment: .top) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
    }
}

func timeString(_ seconds: Double) -> String { TimeFormat.precise(seconds) }

struct StatusBar: View {
    @Bindable var store: EditorStore
    var body: some View {
        HStack(spacing: 10) {
            if let failure = store.previewFailureMessage {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                Text(failure).lineLimit(1)
            } else if let notice = store.notice {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                Text(notice).lineLimit(1)
            } else if let warnings = store.snapshot?.warnings, !warnings.isEmpty {
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                Text(warnings.first ?? "").lineLimit(1)
            } else if store.hasDocument {
                Text(store.planFileLabel.map { "Saved to \($0)" } ?? "Ready")
            } else {
                Text("Pick a reel from the Reels list to start editing.")
            }
            Spacer()
            if let snapshot = store.snapshot {
                Text("\(TimeFormat.clock(Double(snapshot.durationFrames) / max(snapshot.fps, 1))) long")
                    .font(EditorType.timecode)
                Text("\(snapshot.fps.formatted(.number.precision(.fractionLength(0...2)))) fps")
            }
        }
        .font(EditorType.body)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 12)
        .frame(height: 24)
        .background(EditorPalette.panel)
        .overlay(alignment: .top) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
    }
}

extension Notification.Name {
    static let reelsTogglePlayback = Notification.Name("ReelsEditor.TogglePlayback")
    static let reelsPlaybackRate = Notification.Name("ReelsEditor.PlaybackRate")
}

/// Lets the custom (title-bar-less) header drag the window.
struct WindowDragArea: NSViewRepresentable {
    final class DragView: NSView {
        override var mouseDownCanMoveWindow: Bool { true }
    }
    func makeNSView(context: Context) -> NSView { DragView() }
    func updateNSView(_ nsView: NSView, context: Context) {}
}
