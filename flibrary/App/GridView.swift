import SwiftUI

struct LibraryView: View {
    @Bindable var store: LibraryStore
    @Environment(ToolsModel.self) private var tools

    var body: some View {
        @Bindable var tools = tools
        VStack(spacing: 0) {
            FilterBar(store: store)
            Divider()
            GridView(store: store)
            JobsStrip()
            StatusBar(store: store)
        }
        .searchable(text: $store.filter.query, prompt: "Search summary, hooks, location, notes, file")
        .toolbar {
            ToolbarItem { Button { Task { await store.refresh() } } label: { Label("Refresh", systemImage: "arrow.clockwise") }.help("Refresh") }
        }
        .task { await store.refresh(); store.startWatching() }
        .sheet(item: $store.detail) { DetailView(store: store, asset: $0) }
        .confirmationDialog("This 360 clip needs an equirectangular version for Premiere — make one?",
                            isPresented: Binding(get: { !tools.equirectPrompt.isEmpty }, set: { if !$0 { tools.equirectPrompt = [] } }),
                            titleVisibility: .visible) {
            Button("Make equirect") { tools.confirmEquirectPrompt() }
            Button("Cancel", role: .cancel) { tools.equirectPrompt = [] }
        } message: {
            Text(tools.equirectPrompt.map { $0.originalName ?? $0.id }.joined(separator: ", "))
        }
        .alert("flibrary", isPresented: Binding(get: { tools.message != nil }, set: { if !$0 { tools.message = nil } })) {
            Button("OK") { tools.message = nil }
        } message: { Text(tools.message ?? "") }
    }
}

struct FilterBar: View {
    @Bindable var store: LibraryStore
    var body: some View {
        HStack(spacing: 12) {
            Picker("Kind", selection: $store.filter.kind) {
                ForEach(KindFilter.allCases) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented).frame(width: 170).labelsHidden()
            Picker("Type", selection: $store.filter.type) {
                Text("Any type").tag(String?.none)
                ForEach(store.types, id: \.self) { Text($0).tag(String?.some($0)) }
            }.frame(width: 150)
            Picker("Quality", selection: $store.filter.minQuality) {
                Text("Any quality").tag(0)
                ForEach(1...5, id: \.self) { Text("\($0)+ stars").tag($0) }
            }.frame(width: 140)
            Picker("Date", selection: $store.filter.date) {
                ForEach(DateFilter.allCases) { Text($0.rawValue).tag($0) }
            }.frame(width: 150)
            if store.filter.date == .day { DatePicker("", selection: $store.filter.day, displayedComponents: .date).labelsHidden() }
            Spacer()
        }
        .labelsHidden().padding(.horizontal, 12).padding(.vertical, 8)
    }
}

struct StatusBar: View {
    let store: LibraryStore
    @AppStorage(GridZoom.key) private var tileWidth = GridZoom.standard
    var body: some View {
        HStack {
            if let e = store.error { Text(e).foregroundStyle(.red).lineLimit(2).textSelection(.enabled) }
            else { Text("\(store.visible.count) of \(store.assets.count) clips" + (store.selection.isEmpty ? "" : " · \(store.selection.count) selected")) }
            Spacer()
            Image(systemName: "photo").imageScale(.small)
            Slider(value: $tileWidth, in: GridZoom.range).frame(width: 110).controlSize(.mini).help("Thumbnail size (Cmd+= / Cmd+-, 0 resets)")
            Image(systemName: "photo").imageScale(.large)
            if store.loading { ProgressView().controlSize(.small) }
            if let free = store.freeBytes { Text("\(ByteCountFormatter.string(fromByteCount: Int64(free), countStyle: .file)) free") }
        }
        .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 12).padding(.vertical, 6).background(.bar)
    }
}

enum GridZoom {
    static let key = "gridTileWidth"
    static let range: ClosedRange<Double> = 110...480
    static let standard = 210.0
    static func clamp(_ v: Double) -> Double { min(range.upperBound, max(range.lowerBound, v)) }
}

struct GridView: View {
    let store: LibraryStore
    @AppStorage(GridZoom.key) private var tileWidth = GridZoom.standard
    @State private var pinchBase: Double?

    private var days: [(day: Date, assets: [Asset])] {
        let cal = Calendar.current
        let groups = Dictionary(grouping: store.visible) { cal.startOfDay(for: $0.date ?? .distantPast) }
        return groups.keys.sorted(by: >).map { ($0, groups[$0]!) }
    }

    var body: some View {
        ScrollView {
            LazyVGrid(columns: [GridItem(.adaptive(minimum: GridZoom.clamp(tileWidth), maximum: GridZoom.clamp(tileWidth) * 1.4), spacing: 12, alignment: .top)], spacing: 12, pinnedViews: [.sectionHeaders]) {
                ForEach(days, id: \.day) { group in
                    Section {
                        ForEach(group.assets) { AssetCell(store: store, asset: $0, pixels: Media.pixelBucket(forTileWidth: GridZoom.clamp(tileWidth) * 1.4)) }
                    } header: {
                        Text(group.day == .distantPast ? "Undated" : group.day.formatted(.dateTime.weekday(.wide).month().day().year()))
                            .font(.headline).frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 4).padding(.vertical, 6).background(.bar)
                    }
                }
            }
            .padding(12)
        }
        .overlay { if store.visible.isEmpty && !store.loading { Text(store.assets.isEmpty ? "No footage yet" : "Nothing matches").foregroundStyle(.secondary) } }
        .simultaneousGesture(MagnifyGesture().onChanged { v in
            if pinchBase == nil { pinchBase = tileWidth }
            tileWidth = GridZoom.clamp((pinchBase ?? tileWidth) * v.magnification)
        }.onEnded { _ in pinchBase = nil })
        .focusable()
        .onKeyPress(.space) { store.openDetail(); return .handled }
    }
}

struct AssetCell: View {
    let store: LibraryStore
    let asset: Asset
    var pixels = 480
    @Environment(ToolsModel.self) private var tools
    @State private var thumb: CGImage?
    @State private var scrub: CGImage?
    @State private var scrubTask: Task<Void, Never>?
    @State private var pending: [Asset] = []

    private var selected: Bool { store.selection.contains(asset.id) }
    private var shown: CGImage? { scrub ?? thumb }

    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            ZStack(alignment: .topLeading) {
                Color.black.opacity(0.85)
                if let shown { Image(decorative: shown, scale: 1).resizable().scaledToFill() }
                else { Image(systemName: asset.isPhoto ? "photo" : "film").foregroundStyle(.secondary) }
                if asset.is360 { badge("360").padding(5) }
                if asset.isPhoto { badge("photo").frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomTrailing).padding(5) }
                else if let d = asset.duration { badge(formatDuration(d)).frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .bottomTrailing).padding(5) }
            }
            .aspectRatio(16.0 / 9.0, contentMode: .fit)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Color.accentColor, lineWidth: selected ? 3 : 0))
            HStack {
                Text(asset.date?.formatted(.dateTime.month(.abbreviated).day().hour().minute()) ?? "").foregroundStyle(.secondary)
                Spacer()
                Text(asset.shotLabel).lineLimit(1).foregroundStyle(.secondary)
            }.font(.caption)
            Text(asset.summary).font(.caption).lineLimit(1)
        }
        .padding(4)
        .background(selected ? Color.accentColor.opacity(0.18) : .clear, in: RoundedRectangle(cornerRadius: 8))
        .overlay { DragSurface(mouseDown: mouseDown, click: { store.click(asset.id, modifiers: $0) }, dragURLs: dragURLs,
                               dragImage: { shown.map { NSImage(cgImage: $0, size: NSSize(width: $0.width, height: $0.height)) } },
                               dragEnded: dragEnded, hover: hover, menu: menu) }
        .task(id: "\(asset.id)@\(pixels)") { if let t = await Media.thumbnail(for: asset, maxPixel: pixels) { thumb = t } }
    }

    private func badge(_ text: String) -> some View {
        Text(text).font(.caption2.bold()).padding(.horizontal, 5).padding(.vertical, 2)
            .background(.black.opacity(0.7), in: RoundedRectangle(cornerRadius: 4)).foregroundStyle(.white)
    }

    private func mouseDown(_ mods: NSEvent.ModifierFlags, _ clicks: Int) {
        store.mouseDown(asset.id, modifiers: mods)
        if clicks == 2 { store.openDetail(asset.id) }
    }

    private func dragSelection() -> [Asset] { selected ? store.selectedAssets : [asset] }

    private func dragURLs() -> [URL] {
        let plan = DragPlanner.plan(dragSelection())
        pending = plan.needsEquirect
        if plan.urls.isEmpty { dragEnded() }
        return plan.urls
    }

    private func dragEnded() {
        if !pending.isEmpty { tools.promptForEquirect(pending); pending = [] }
    }

    private func hover(_ f: Double?) {
        scrubTask?.cancel()
        guard let f else { scrub = nil; return }
        scrubTask = Task {
            try? await Task.sleep(for: .milliseconds(30))
            guard !Task.isCancelled else { return }
            let img = await Media.scrubFrame(for: asset, fraction: f, maxPixel: min(pixels, 720))
            if !Task.isCancelled, let img { scrub = img }
        }
    }

    private func menu() -> [MenuAction] {
        let list = dragSelection()
        return [MenuAction(title: "Open Details") { store.openDetail(asset.id) },
                MenuAction(title: "Reveal in Finder") { store.reveal(list) },
                MenuAction(title: "Copy path") { store.copyPaths(list) }]
    }
}
