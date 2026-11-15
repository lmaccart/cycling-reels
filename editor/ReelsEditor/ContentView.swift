import AppKit
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
        Group {
            if !store.hasLaunchConfiguration || store.configurationError != nil {
                WorkspaceSetupPane(store: store)
            } else {
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
            }
        }
        .alert("Editing service", isPresented: Binding(
            get: { store.errorMessage != nil },
            set: { if !$0 { store.errorMessage = nil } }
        )) {
            Button("Dismiss", role: .cancel) { store.errorMessage = nil }
        } message: {
            Text(store.errorMessage ?? "")
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
            store.previewFailureMessage = "Interactive preview was not ready within the 20-second capture deadline."
        }
    }

    private struct WorkspaceSetupPane: View {
        @Bindable var store: EditorStore
        @State private var repository: URL?
        @State private var workspacePath = ""

        var body: some View {
            VStack(alignment: .leading, spacing: 14) {
                Label("Reels Editor", systemImage: "film.stack")
                    .font(.system(size: 18, weight: .semibold))
                    .foregroundStyle(EditorPalette.accent)
                Text("Choose the isolated editor worktree and its private workspace. The main checkout is rejected.")
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Text("Repository")
                        .font(.system(size: 11, weight: .medium))
                        .frame(width: 92, alignment: .leading)
                    Text(repository?.path ?? "No isolated repository selected")
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(repository == nil ? .tertiary : .secondary)
                        .lineLimit(2)
                        .textSelection(.enabled)
                    Spacer()
                    Button("Choose…", action: chooseRepository)
                }
                HStack(spacing: 8) {
                    Text("Workspace")
                        .font(.system(size: 11, weight: .medium))
                        .frame(width: 92, alignment: .leading)
                    TextField("Private workspace path", text: $workspacePath)
                        .font(.system(size: 10, design: .monospaced))
                        .textFieldStyle(.roundedBorder)
                        .accessibilityLabel("Private editor workspace path")
                    Button("Choose…", action: chooseWorkspace)
                        .disabled(repository == nil)
                }
                if let message = store.configurationError {
                    Label(message, systemImage: "exclamationmark.triangle.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                HStack {
                    Spacer()
                    Button("Launch Editor") { launch() }
                        .keyboardShortcut(.defaultAction)
                        .disabled(repository == nil || workspacePath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .padding(20)
            .frame(width: 560)
            .background(EditorPalette.panel, in: RoundedRectangle(cornerRadius: 8))
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(EditorPalette.canvas)
            .onAppear {
                if let saved = UserDefaults.standard.string(forKey: "reelsEditor.repositoryRoot") {
                    repository = URL(fileURLWithPath: saved)
                    workspacePath = UserDefaults.standard.string(forKey: "reelsEditor.workspaceRoot") ?? ""
                }
            }
        }

        private func chooseRepository() {
            let panel = NSOpenPanel()
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.allowsMultipleSelection = false
            panel.message = "Select the isolated cycling-editor worktree, not the production checkout."
            if panel.runModal() == .OK, let url = panel.url {
                repository = url
                if workspacePath.isEmpty {
                    workspacePath = url.appending(path: ".cache/editor/workspace").path
                }
            }
        }

        private func chooseWorkspace() {
            guard let repository else { return }
            let panel = NSOpenPanel()
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.allowsMultipleSelection = false
            panel.canCreateDirectories = true
            panel.directoryURL = URL(fileURLWithPath: workspacePath.isEmpty
                ? repository.appending(path: ".cache/editor").path
                : workspacePath)
            panel.message = "Choose a private workspace, never production files or original media."
            if panel.runModal() == .OK, let url = panel.url { workspacePath = url.path }
        }

        private func launch() {
            guard let repository else { return }
            let workspace = URL(fileURLWithPath: workspacePath).standardizedFileURL
            do {
                try FileManager.default.createDirectory(at: workspace, withIntermediateDirectories: true)
            } catch {
                store.configurationError = "Could not create the private workspace: \(error.localizedDescription)"
                return
            }
            store.configure(repository: repository, workspace: workspace)
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
        HStack(spacing: 7) {
            Image(systemName: "film.stack")
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(EditorPalette.accent)
                .accessibilityHidden(true)
            Text(store.snapshot?.plan["slug"]?.stringValue ?? "Reels Editor")
                .font(.system(size: 13, weight: .semibold))
                .lineLimit(1)
                .frame(maxWidth: 190, alignment: .leading)
            Rectangle().fill(EditorPalette.separator).frame(width: 1, height: 20)
            Button { store.undo() } label: { Image(systemName: "arrow.uturn.backward") }
                .help("Undo  ⌘Z")
                .disabled(!store.hasDocument)
            Button { store.redo() } label: { Image(systemName: "arrow.uturn.forward") }
                .help("Redo  ⇧⌘Z")
                .disabled(!store.hasDocument)
            Button { store.splitAtPlayhead() } label: { Image(systemName: "scissors") }
                .help("Split selected clip at playhead")
                .disabled(store.selectedNode == nil)
            Button { store.deleteSelection() } label: { Image(systemName: "trash") }
                .help("Delete selected clip")
                .disabled(store.selectedIDs.isEmpty)
            Rectangle().fill(EditorPalette.separator).frame(width: 1, height: 20)
            Picker("Preview mode", selection: Binding(
                get: { store.currentPreviewMode },
                set: { store.preparePreview(mode: $0) }
            )) {
                Text("Interactive").tag("interactive")
                Text("Exact").tag("exact")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .accessibilityLabel("Preview mode")
            .frame(width: 160)
            Button {
                store.preparePreview(mode: store.currentPreviewMode)
            } label: {
                Image(systemName: store.isPreparingPreview ? "arrow.trianglehead.2.clockwise" : "arrow.clockwise")
            }
            .disabled(store.isPreparingPreview || !store.hasDocument)
            .help(store.isPreparingPreview ? "Preparing preview" : "Refresh preview")
            .accessibilityLabel(store.isPreparingPreview ? "Preparing preview" : "Refresh preview")
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
            Button { store.export() } label: {
                Label("Export", systemImage: "square.and.arrow.up")
            }
            .disabled(!store.hasDocument)
            .help("Render this revision to the private editor workspace")
        }
        .buttonStyle(.borderless)
        .controlSize(.small)
        .padding(.horizontal, 10)
        .frame(height: 42)
        .background(EditorPalette.panel)
        .overlay(alignment: .bottom) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
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
        .accessibilityElement(children: .combine)
        .accessibilityLabel("Editing service: \(label)")
    }

    private var label: String {
        switch state {
        case .stopped: "Offline"
        case .starting: "Starting"
        case .connected: "Connected"
        case .failed: "Disconnected"
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

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(section == "Projects" ? "PROJECT" : "LIBRARY")
                    .font(.system(size: 11, weight: .bold))
                    .tracking(0.6)
                    .foregroundStyle(.secondary)
                Spacer()
                Picker("Browser", selection: $section) {
                    Text("Footage").tag("Footage")
                    Text("Projects").tag("Projects")
                }
                .labelsHidden()
                .pickerStyle(.segmented)
                .frame(width: 130)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 9)

            if section == "Footage" {
                HStack(spacing: 7) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Regex search…", text: $localQuery)
                        .textFieldStyle(.plain)
                        .onChange(of: localQuery) { _, value in store.search(value) }
                        .accessibilityLabel("Search footage by regular expression")
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
                    Text(localQuery.isEmpty ? "ASSETS" : "MATCHES")
                    Spacer()
                    Text(localQuery.isEmpty ? "\(store.library.count)" : "\(store.searchHits.count)")
                        .monospacedDigit()
                }
                .font(.system(size: 10, weight: .semibold))
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 10)
                .padding(.bottom, 4)
                Divider()
                if localQuery.isEmpty {
                    libraryList
                } else {
                    searchList
                }
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
            ForEach(Array(store.library.enumerated()), id: \.offset) { _, item in
                let id = item["id"]?.stringValue ?? UUID().uuidString
                AssetRow(item: item)
                    .tag(id)
                    .draggable(id)
            }
        }
        .listStyle(.sidebar)
        .overlay {
            if store.library.isEmpty {
                ContentUnavailableView("No footage loaded", systemImage: "film", description: Text("The workspace library will appear here."))
            }
        }
    }

    private var searchList: some View {
        List {
            if store.searchHits.contains(where: { !$0.related }) {
                Section("Regex") {
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
            if store.searchHits.contains(where: \.related) {
                Section("Related · Haiku") {
                    ForEach(store.searchHits.filter(\.related)) { hit in
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
            if store.searchHits.isEmpty && !store.semanticSearchPending {
                ContentUnavailableView("No matching footage", systemImage: "text.magnifyingglass",
                                       description: Text("Try a phrase, rider, place, or activity."))
            } else if store.searchHits.isEmpty && store.semanticSearchPending {
                ProgressView("Finding related moments…").controlSize(.small)
            }
        }
    }

    private var plansList: some View {
        List {
            ForEach(Array(store.plans.enumerated()), id: \.offset) { _, plan in
                Button {
                    if let path = plan["path"]?.stringValue { Task { await store.openPlan(path) } }
                } label: {
                    VStack(alignment: .leading, spacing: 3) {
                        Text(plan["title"]?.stringValue ?? plan["slug"]?.stringValue ?? plan["name"]?.stringValue ?? "Untitled plan")
                            .font(.system(size: 12, weight: .medium))
                            .lineLimit(2)
                        Text(plan["path"]?.stringValue ?? plan["postDate"]?.stringValue ?? "")
                            .font(.system(size: 10, design: .monospaced))
                            .foregroundStyle(.tertiary)
                            .lineLimit(1)
                    }
                    .padding(.vertical, 3)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .disabled(plan["path"]?.stringValue == nil)
            }
        }
        .listStyle(.sidebar)
        .overlay {
            if store.plans.isEmpty {
                ContentUnavailableView("No plans available", systemImage: "doc.text", description: Text("Choose a project after the service connects."))
            }
        }
    }
}

private struct AssetRow: View {
    let item: JSONValue

    private var title: String {
        item["originalName"]?.stringValue
            ?? item["name"]?.stringValue
            ?? item["filename"]?.stringValue
            ?? item["id"]?.stringValue
            ?? "Media"
    }

    private var detail: String? {
        let candidates = [
            item["summary"]?.stringValue,
            item["duration"]?.displayValue,
            item["media"]?.stringValue.map { URL(fileURLWithPath: $0).lastPathComponent },
            item["filename"]?.stringValue.map { URL(fileURLWithPath: $0).lastPathComponent },
            item["type"]?.stringValue
        ]
        return candidates.compactMap { $0 }
            .first { !$0.isEmpty && $0.localizedCaseInsensitiveCompare(title) != .orderedSame
                && $0 != item["id"]?.stringValue }
    }

    var body: some View {
        HStack(spacing: 8) {
            RoundedRectangle(cornerRadius: 3)
                .fill(EditorPalette.video.opacity(0.3))
                .frame(width: 46, height: 30)
                .overlay(Image(systemName: "play.fill").font(.system(size: 10)).foregroundStyle(EditorPalette.video))
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 2) {
                Text(title)
                    .font(.system(size: 11, weight: .medium))
                    .lineLimit(1)
                if let detail {
                    Text(detail)
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                }
            }
        }
        .padding(.vertical, 2)
        .accessibilityElement(children: .combine)
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
                    .font(.system(size: 9, design: .monospaced)).foregroundStyle(.tertiary)
            }
            if !hit.evidence.isEmpty {
                Text(highlightedEvidence(hit.evidence, offsets: hit.raw["highlights"]?.arrayValue ?? []))
                    .font(.system(size: 10))
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            if hit.related || hit.relatedReason != nil || !hit.sourceRanges.isEmpty {
                Label("RELATED · HAIKU", systemImage: "sparkle.magnifyingglass")
                    .font(.system(size: 9, weight: .semibold))
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
                            .font(.system(size: 9, design: .monospaced))
                            .foregroundStyle(.tertiary)
                        if !range.evidence.isEmpty {
                            Text(highlightedEvidence(range.evidence, offsets: range.highlights))
                                .font(.system(size: 10))
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        if !range.reason.isEmpty {
                            Text(range.reason)
                                .font(.system(size: 9))
                                .foregroundStyle(.tertiary)
                                .lineLimit(2)
                        }
                    }
                    .padding(.leading, 9)
                }
                if hit.sourceRanges.count > 4 {
                    Text("+ \(hit.sourceRanges.count - 4) more source ranges")
                        .font(.system(size: 9))
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
        .accessibilityLabel("\(hit.related ? "Related match" : "Regex match"), \(hit.title), source \(hit.sourceIn) to \(hit.sourceOut) seconds")
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
                monitorTitle("SOURCE")
                Spacer()
                monitorActions(source: true)
            }
            .padding(.horizontal, 9)
            .frame(height: 32)
            .background(EditorPalette.panel)
            Divider()
            SourceMonitor(store: store, model: sourceModel)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            SourceControls(store: store, model: sourceModel)
        }
        .background(EditorPalette.panel)
    }

    private var programSurface: some View {
        VStack(spacing: 0) {
            HStack {
                monitorTitle("PROGRAM")
                Spacer()
                monitorActions(source: false)
            }
            .padding(.horizontal, 9)
            .frame(height: 32)
            .background(EditorPalette.panel)
            Divider()
            ProgramMonitor(store: store)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            ProgramControls(store: store)
        }
        .background(EditorPalette.panel)
    }

    private var selectedMonitor: some View {
        Group {
            if store.activeTab == "source" {
                SourceMonitor(store: store, model: sourceModel)
            } else {
                ProgramMonitor(store: store)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color.black)
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
        Text(title)
            .font(.system(size: 10, weight: .semibold))
            .tracking(0.6)
            .foregroundStyle(.tertiary)
    }

    @ViewBuilder
    private func monitorActions(source: Bool) -> some View {
        if source {
            Button { store.insertSourceSelection() } label: {
                Label("Insert", systemImage: "arrow.down.to.line")
            }
            .disabled(store.selectedAssetID == nil || store.sourceIn == nil || store.sourceOut == nil)
            .help("Insert marked source range into the timeline")
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
                .disabled(store.selectedAssetID == nil)
                .help("Attach this source range to the selected primary clip")
            }
        } else {
            Text(store.currentPreviewMode.capitalized)
                .font(.system(size: 10, weight: .medium))
                .foregroundStyle(.secondary)
            Button { store.preparePreview(mode: store.currentPreviewMode) } label: {
                Image(systemName: "arrow.clockwise")
            }
            .help("Prepare the current document revision")
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
                Text("PHOTO")
                Text("Set still duration in Inspector")
                    .foregroundStyle(.secondary)
                Spacer()
                if let id = store.selectedAssetID {
                    Button { store.insertDroppedItem(id) } label: {
                        Label("Insert photo", systemImage: "arrow.down.to.line")
                    }
                }
            } else {
                Button { model.togglePlayback() } label: {
                    Image(systemName: model.isPlaying ? "pause.fill" : "play.fill")
                }
                .help("Play / pause source")
                .accessibilityLabel(model.isPlaying ? "Pause source" : "Play source")
                Button { setMark(in: true) } label: { Label("In", systemImage: "bracket.left") }
                Button { setMark(in: false) } label: { Label("Out", systemImage: "bracket.right") }
                Rectangle().fill(EditorPalette.separator).frame(width: 1, height: 18)
                Text("IN \(store.sourceIn.map(timeString) ?? "—")")
                Text("OUT \(store.sourceOut.map(timeString) ?? "—")")
                if model.durationSeconds > 0 {
                    Slider(value: Binding(
                        get: { model.currentSeconds },
                        set: { model.seek(to: $0) }
                    ), in: 0...model.durationSeconds)
                    .frame(width: 110)
                    .help("Seek source")
                    Text(timeString(model.currentSeconds))
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Button { store.insertSourceSelection() } label: { Image(systemName: "arrow.down.to.line") }
                    .help("Insert source In/Out")
                    .disabled(store.selectedAssetID == nil || store.sourceIn == nil || store.sourceOut == nil)
            }
        }
        .font(.system(size: 10, design: .monospaced))
        .buttonStyle(.borderless)
        .controlSize(.small)
        .padding(.horizontal, 10)
        .frame(height: 33)
        .background(EditorPalette.panel)
        .overlay(alignment: .top) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
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
                .help("Previous frame")
            Button { NotificationCenter.default.post(name: .reelsTogglePlayback, object: nil) } label: {
                Image(systemName: "playpause.fill")
            }
            .help("Play / pause")
            Button { store.setPlayhead(store.playheadFrame + 1) } label: { Image(systemName: "forward.frame") }
                .help("Next frame")
            Text(timeString(Double(store.playheadFrame) / store.fps))
                .font(.system(size: 11, design: .monospaced))
                .monospacedDigit()
            Picker("Playback rate", selection: Binding(
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
            .frame(width: 55)
            .onChange(of: store.playbackRate) { _, value in
                NotificationCenter.default.post(name: .reelsPlaybackRate, object: value)
            }
            Spacer()
            if let snapshot = store.snapshot {
                Text("DOC \(snapshot.revisions.document.prefix(8))")
                    .help("Document revision \(snapshot.revisions.document)")
                if store.isPreparingPreview {
                    Label("Preview pending", systemImage: "clock")
                        .foregroundStyle(EditorPalette.warning)
                } else {
                    Text("PREVIEW \(snapshot.revisions.renderer.prefix(8))")
                        .foregroundStyle(.secondary)
                        .help("Renderer revision \(snapshot.revisions.renderer)")
                }
            }
        }
        .font(.system(size: 10, weight: .medium))
        .buttonStyle(.borderless)
        .controlSize(.small)
        .padding(.horizontal, 10)
        .frame(height: 33)
        .background(EditorPalette.panel)
        .overlay(alignment: .top) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
    }
}

func timeString(_ seconds: Double) -> String {
    let safe = max(0, Int(seconds.rounded()))
    return String(format: "%02d:%02d:%02d", safe / 3600, (safe / 60) % 60, safe % 60)
}

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
            } else {
                Text(store.hasDocument ? "Ready" : "Connect to an editing workspace, then open a plan.")
            }
            Spacer()
            if let snapshot = store.snapshot {
                Text("\(snapshot.nodes.count) nodes")
                Text("\(snapshot.fps.formatted(.number.precision(.fractionLength(0...2)))) fps")
                Text("\(snapshot.durationFrames) fr")
                    .monospacedDigit()
            }
        }
        .font(.system(size: 10))
        .foregroundStyle(.secondary)
        .padding(.horizontal, 10)
        .frame(height: 22)
        .background(EditorPalette.panel)
        .overlay(alignment: .top) { Rectangle().fill(EditorPalette.separator).frame(height: 1) }
    }
}

extension Notification.Name {
    static let reelsTogglePlayback = Notification.Name("ReelsEditor.TogglePlayback")
    static let reelsPlaybackRate = Notification.Name("ReelsEditor.PlaybackRate")
}
