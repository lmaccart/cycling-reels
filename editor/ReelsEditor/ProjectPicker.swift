import AppKit
import SwiftUI

// MARK: - Root

/// Chooses between the failure screen, the project picker and the editor.
struct RootView: View {
    @Bindable var store: EditorStore

    var body: some View {
        ZStack(alignment: .bottom) {
            if let failure = store.startupFailure {
                StartupFailureView(store: store, failure: failure)
            } else if store.showPicker {
                ProjectPicker(store: store)
            } else {
                ContentView(store: store)
            }
            if let toast = store.toast {
                ToastView(text: toast)
                    .padding(.bottom, 44)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .ignoresSafeArea(.container, edges: .top)
        .animation(.easeOut(duration: 0.18), value: store.toast)
        .footageDropTarget(store.importer)
        .sheet(isPresented: Binding(
            get: { store.importer.isPresented },
            set: { store.importer.isPresented = $0 }
        )) {
            ImportProgressSheet(importer: store.importer)
        }
        .alert("Reels Editor", isPresented: Binding(
            get: { store.errorMessage != nil && store.showPicker && store.startupFailure == nil },
            set: { if !$0 { store.errorMessage = nil } }
        )) {
            Button("OK", role: .cancel) { store.errorMessage = nil }
        } message: {
            Text(store.errorMessage ?? "")
        }
        .onReceive(NotificationCenter.default.publisher(for: .reelsEditorCaptureQuit)) { _ in
            store.stopServiceForCapture()
        }
        .preferredColorScheme(.dark)
    }
}

struct ToastView: View {
    let text: String

    var body: some View {
        HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            Text(text).font(.callout.weight(.medium))
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
        .background(.regularMaterial, in: Capsule())
        .overlay(Capsule().strokeBorder(Color.white.opacity(0.12)))
        .shadow(color: .black.opacity(0.35), radius: 12, y: 4)
        .accessibilityElement(children: .combine)
    }
}

// MARK: - Failure screen

struct StartupFailureView: View {
    @Bindable var store: EditorStore
    let failure: EditorStore.StartupFailure

    var body: some View {
        VStack(spacing: 18) {
            Image(systemName: "exclamationmark.triangle")
                .font(.system(size: 40, weight: .light))
                .foregroundStyle(.orange)
            Text(failure.title).font(.title2.weight(.semibold))
            Text(failure.detail)
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 520)
                .fixedSize(horizontal: false, vertical: true)
            if !failure.logLines.isEmpty {
                ScrollView {
                    Text(failure.logLines.joined(separator: "\n"))
                        .font(.system(size: 11, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(12)
                        .textSelection(.enabled)
                }
                .frame(maxWidth: 640, maxHeight: 190)
                .background(Color.black.opacity(0.35), in: RoundedRectangle(cornerRadius: 8))
                .accessibilityLabel("Last lines from the editing service")
            }
            HStack(spacing: 10) {
                Button("Retry") { store.retryConnection() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(store.repositoryRoot == nil)
                Button("Choose Folder…") { store.chooseRepository() }
                Button("Show Log") { store.showLog() }
            }
            .controlSize(.large)
            if let repo = store.repositoryRoot {
                Text(repo.path)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .textSelection(.enabled)
            }
        }
        .padding(40)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PickerPalette.background)
    }
}

// MARK: - Picker

enum PickerPalette {
    static let background = Color(red: 0.105, green: 0.108, blue: 0.118)
    static let sidebar = Color(red: 0.135, green: 0.138, blue: 0.150)
    static let row = Color.white.opacity(0.045)
}

struct ProjectPicker: View {
    @Bindable var store: EditorStore
    @State private var query = ""
    @State private var selection: String?
    @FocusState private var titleFocused: Bool

    private var filtered: [PlanSummary] { store.planSummaries.filter { $0.matches(query) } }

    var body: some View {
        HStack(spacing: 0) {
            sidebar
                .frame(width: 330)
                .background(PickerPalette.sidebar)
                .background(WindowDragArea())
            Divider()
            recents
        }
        .background(PickerPalette.background)
        .onAppear { store.importer.refreshInbox() }
        .onChange(of: store.isNamingNewReel) { _, naming in if naming { titleFocused = true } }
    }

    // Left column: identity and the two big actions.
    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            Spacer(minLength: 24)
            Image(nsImage: NSApp.applicationIconImage)
                .resizable()
                .frame(width: 96, height: 96)
                .accessibilityHidden(true)
            Text("Reels Editor")
                .font(.system(size: 30, weight: .semibold))
                .padding(.top, 14)
            Text("Version \(Self.version)")
                .font(.callout)
                .foregroundStyle(.secondary)
                .padding(.top, 2)

            VStack(spacing: 10) {
                if store.isNamingNewReel {
                    newReelField
                } else {
                    ActionButton(title: "New Reel", subtitle: "Start an empty reel", symbol: "plus.rectangle.on.rectangle",
                                 shortcut: "⌘N", prominent: true) { beginNaming() }
                }
                ActionButton(title: "Import Footage…", subtitle: "Copy clips and photos into your library",
                             symbol: "square.and.arrow.down", shortcut: "⌘I", prominent: false) {
                    store.importer.chooseFiles()
                }
                if store.importer.inboxCount > 0 {
                    ActionButton(
                        title: "Ingest \(store.importer.inboxCount) file\(store.importer.inboxCount == 1 ? "" : "s") waiting in inbox",
                        subtitle: "Add them to your library now",
                        symbol: "tray.and.arrow.down", shortcut: nil, prominent: false
                    ) { store.importer.ingestInbox() }
                }
            }
            .padding(.top, 30)

            Spacer(minLength: 16)
            if store.hasDocument {
                Button { store.showPicker = false } label: {
                    Label("Back to current reel", systemImage: "chevron.backward")
                }
                .buttonStyle(.link)
                .keyboardShortcut(.cancelAction)
                .padding(.bottom, 8)
            }
            if let repo = store.repositoryRoot {
                Text(repo.path)
                    .font(.system(size: 11, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.head)
                    .help(repo.path)
            }
            Text("Drop footage anywhere to import. Your files are copied; originals stay where they are.")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.top, 4)
                .padding(.bottom, 24)
        }
        .padding(.horizontal, 28)
    }

    private var newReelField: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("New Reel").font(.headline)
            TextField("Title (optional)", text: $store.newReelTitle)
                .textFieldStyle(.roundedBorder)
                .focused($titleFocused)
                .onSubmit(createReel)
            HStack {
                Button("Cancel") { store.isNamingNewReel = false }
                    .keyboardShortcut(.cancelAction)
                Spacer()
                Button("Create", action: createReel)
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
            }
        }
        .padding(14)
        .background(PickerPalette.row, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.accentColor.opacity(0.6)))
    }

    // Right column: recents.
    private var recents: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 12) {
                Text("Recent Reels").font(.title3.weight(.semibold))
                Spacer()
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                    TextField("Search reels", text: $query)
                        .textFieldStyle(.plain)
                        .frame(width: 190)
                    if !query.isEmpty {
                        Button { query = "" } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain).foregroundStyle(.secondary)
                            .accessibilityLabel("Clear search")
                    }
                }
                .padding(.horizontal, 10).padding(.vertical, 6)
                .background(PickerPalette.row, in: RoundedRectangle(cornerRadius: 8))
            }
            .background(WindowDragArea())
            .padding(.horizontal, 24)
            .padding(.top, 18)
            .padding(.bottom, 14)

            if filtered.isEmpty {
                emptyState
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 6) {
                            ForEach(filtered) { plan in
                                PlanRow(plan: plan, selected: selection == plan.id)
                                    .id(plan.id)
                                    .onTapGesture(count: 2) { open(plan.path) }
                                    .onTapGesture { selection = plan.id }
                                    .contextMenu {
                                        Button("Open") { open(plan.path) }
                                        Button("Reveal in Finder") { store.revealPlan(plan.path) }
                                    }
                            }
                        }
                        .padding(.horizontal, 12)
                        .padding(.vertical, 3)
                    }
                    .focusable()
                    .focusEffectDisabled()
                    .onKeyPress(.return) {
                        guard let selection else { return .ignored }
                        open(selection)
                        return .handled
                    }
                    .onKeyPress(.downArrow) { moveSelection(1, proxy) }
                    .onKeyPress(.upArrow) { moveSelection(-1, proxy) }
                }
            }
        }
        .frame(maxWidth: .infinity)
    }

    private func moveSelection(_ delta: Int, _ proxy: ScrollViewProxy) -> KeyPress.Result {
        let ids = filtered.map(\.id)
        guard let next = PickerSelection.move(from: selection, in: ids, by: delta) else { return .ignored }
        selection = next
        proxy.scrollTo(next)
        return .handled
    }

    @ViewBuilder private var emptyState: some View {
        if !store.plansLoaded {
            VStack(spacing: 10) {
                ProgressView()
                Text("Starting the editing service…").foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if query.isEmpty {
            ContentUnavailableView(
                "No reels yet",
                systemImage: "film.stack",
                description: Text("Create a new reel, or import footage first.")
            )
        } else {
            ContentUnavailableView.search(text: query)
        }
    }

    private func beginNaming() { store.beginNewReel() }

    func createReel() {
        let title = store.newReelTitle
        store.isNamingNewReel = false
        store.newReelTitle = ""
        Task { await store.createProject(title: title) }
    }

    private func open(_ path: String) {
        Task { await store.openPlan(path) }
    }

    private static var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "1.0"
    }
}

private struct ActionButton: View {
    let title: String
    let subtitle: String
    let symbol: String
    let shortcut: String?
    let prominent: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                Image(systemName: symbol)
                    .font(.system(size: 22, weight: .regular))
                    .frame(width: 30)
                VStack(alignment: .leading, spacing: 2) {
                    Text(title).font(.system(size: 14, weight: .semibold))
                    Text(subtitle).font(.system(size: 11)).opacity(0.75).lineLimit(2)
                }
                Spacer(minLength: 4)
                if let shortcut {
                    Text(shortcut).font(.system(size: 11, weight: .medium, design: .rounded)).opacity(0.6)
                }
            }
            .foregroundStyle(prominent ? Color.white : Color.primary)
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(background, in: RoundedRectangle(cornerRadius: 10))
            .contentShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
    }

    private var background: Color {
        if prominent { return Color.accentColor.opacity(hovering ? 1 : 0.88) }
        return Color.white.opacity(hovering ? 0.11 : 0.07)
    }
}

/// Keyboard selection movement for the picker list.
enum PickerSelection {
    /// Next id after moving `delta` rows, clamped to the ends. No current selection
    /// (or one filtered out) lands on the first row going down, the last going up.
    static func move(from current: String?, in ids: [String], by delta: Int) -> String? {
        guard !ids.isEmpty else { return nil }
        guard let current, let index = ids.firstIndex(of: current) else {
            return delta >= 0 ? ids.first : ids.last
        }
        return ids[min(max(index + delta, 0), ids.count - 1)]
    }
}

private struct PlanRow: View {
    let plan: PlanSummary
    let selected: Bool
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 14) {
            thumbnail
            VStack(alignment: .leading, spacing: 4) {
                Text(plan.title)
                    .font(.system(size: 14, weight: .semibold))
                    .lineLimit(1)
                Text(detail)
                    .font(.system(size: 12))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                Text(plan.path)
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundStyle(.tertiary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }
            Spacer(minLength: 8)
            if let status = plan.status { StatusBadge(status: status) }
        }
        .padding(8)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(rowFill, in: RoundedRectangle(cornerRadius: 10))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.accentColor.opacity(selected ? 0.9 : 0), lineWidth: 1.5))
        .contentShape(RoundedRectangle(cornerRadius: 10))
        .onHover { hovering = $0 }
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(selected ? .isSelected : [])
    }

    private var rowFill: Color {
        if selected { return Color.accentColor.opacity(0.28) }
        return hovering ? Color.white.opacity(0.09) : PickerPalette.row
    }

    private var detail: String {
        var parts: [String] = []
        if let date = plan.dateText { parts.append(date) }
        if let format = plan.format, !format.isEmpty { parts.append(format) }
        if let count = plan.segmentCount { parts.append("\(count) clip\(count == 1 ? "" : "s")") }
        if let duration = plan.durationText { parts.append(duration) }
        return parts.isEmpty ? "No details yet" : parts.joined(separator: "  ·  ")
    }

    private var thumbnail: some View {
        ZStack {
            Rectangle().fill(Color.black.opacity(0.4))
            if let url = plan.thumbnailURL {
                AsyncImage(url: url) { phase in
                    if let image = phase.image {
                        image.resizable().scaledToFill()
                    } else {
                        Image(systemName: "film").foregroundStyle(.tertiary)
                    }
                }
            } else {
                Image(systemName: "film").foregroundStyle(.tertiary)
            }
        }
        .frame(width: 45, height: 80)
        .clipShape(RoundedRectangle(cornerRadius: 6))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.white.opacity(0.1)))
    }
}

struct StatusBadge: View {
    let status: String

    var body: some View {
        Text(status.capitalized)
            .font(.system(size: 11, weight: .semibold))
            .padding(.horizontal, 9)
            .padding(.vertical, 3)
            .foregroundStyle(color)
            .background(color.opacity(0.16), in: Capsule())
    }

    private var color: Color {
        switch status.lowercased() {
        case "posted": .green
        case "scheduled", "queued": .blue
        case "approved": .teal
        case "rendered": .purple
        case "rejected": .red
        default: .gray
        }
    }
}
