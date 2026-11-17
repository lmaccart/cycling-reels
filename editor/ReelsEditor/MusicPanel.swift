import SwiftUI
import AppKit
import AVFoundation

struct MusicTrack: Identifiable, Hashable {
    var id: String { slug }
    var slug: String
    var title: String
    var artist: String
    var duration: Double?
    var defaultStart: Double?
    var credit: String?
    var trending: Bool
    var path: String?

    init?(_ value: JSONValue) {
        guard let slug = value["slug"]?.stringValue, !slug.isEmpty else { return nil }
        self.slug = slug
        title = value["title"]?.stringValue ?? slug
        artist = value["artist"]?.stringValue ?? ""
        duration = value["duration"]?.numberValue
        defaultStart = value["defaultStart"]?.numberValue
        credit = value["credit"]?.stringValue
        trending = value["trending"]?.boolValue ?? false
        path = value["path"]?.stringValue
    }
}

struct MusicResult: Identifiable, Hashable {
    var id: String
    var title: String
    var channel: String
    var duration: Double?
    var url: String

    init?(_ value: JSONValue) {
        guard let url = value["url"]?.stringValue else { return nil }
        self.url = url
        id = value["id"]?.stringValue ?? url
        title = value["title"]?.stringValue ?? url
        channel = value["channel"]?.stringValue ?? ""
        duration = value["duration"]?.numberValue
    }
}

enum MusicTime {
    static func clock(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite else { return "–" }
        let total = Int(seconds.rounded())
        return String(format: "%d:%02d", total / 60, total % 60)
    }

    /// "1:05", "65" or "65.5" -> seconds.
    static func parse(_ text: String) -> Double? {
        let parts = text.trimmingCharacters(in: .whitespaces).split(separator: ":", omittingEmptySubsequences: false)
        guard !parts.isEmpty, parts.count <= 2 else { return nil }
        let numbers = parts.compactMap { Double($0) }
        guard numbers.count == parts.count, numbers.allSatisfy({ $0 >= 0 }) else { return nil }
        return parts.count == 2 ? numbers[0] * 60 + numbers[1] : numbers[0]
    }
}

/// State behind the Music tab: tracks in music/, YouTube search results, the running download, and preview playback.
@MainActor @Observable
final class MusicLibraryModel {
    var tracks: [MusicTrack] = []
    var results: [MusicResult] = []
    var isSearching = false
    var searchError: String?
    var addingURL: String?
    var addProgress: Double = 0
    var addMessage = ""
    var addError: String?
    var previewingSlug: String?

    @ObservationIgnored var call: ((String, JSONValue) async throws -> JSONValue)?
    @ObservationIgnored var onToast: ((String) -> Void)?
    @ObservationIgnored private var player: AVPlayer?
    @ObservationIgnored private var stopTask: Task<Void, Never>?
    @ObservationIgnored private var jobID: String?

    func reload() async {
        guard let call, let value = try? await call("music.list", .object([:])) else { return }
        tracks = (value["tracks"]?.arrayValue ?? []).compactMap(MusicTrack.init)
    }

    @ObservationIgnored private var searchID: String?

    /// Starts a YouTube search in the service's background thread and returns at once; results
    /// arrive as a `music.searchResults` event (`handleSearchResults`). A newer search replaces it.
    func search(_ query: String) {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let call else { return }
        isSearching = true
        searchError = nil
        searchID = nil
        Task {
            do {
                let value = try await call("music.search", .object(["query": .string(trimmed)]))
                searchID = value["searchId"]?.stringValue
            } catch {
                isSearching = false
                searchError = error.localizedDescription
            }
        }
    }

    func cancelSearch() {
        searchID = nil
        isSearching = false
        if let call { Task { _ = try? await call("music.searchCancel", .object([:])) } }
    }

    func handleSearchResults(_ payload: JSONValue) {
        let id = payload["searchId"]?.stringValue
        // The event can beat the request's reply; accept it if no newer search has replied yet.
        if let current = searchID, current != id { return }
        if searchID == nil, !isSearching { return }
        isSearching = false
        if let error = payload["error"]?.stringValue {
            results = []
            searchError = error
            return
        }
        results = (payload["results"]?.arrayValue ?? []).compactMap(MusicResult.init)
        if results.isEmpty { searchError = "No results." }
    }

    func add(_ result: MusicResult, trending: Bool) {
        guard let call, addingURL == nil else { return }
        addingURL = result.url
        addProgress = 0.05
        addMessage = "Starting…"
        addError = nil
        Task {
            var params: [String: JSONValue] = ["url": .string(result.url), "title": .string(result.title)]
            if trending { params["instagram"] = .string(result.title) }
            do {
                let value = try await call("music.add", .object(params))
                jobID = value["jobId"]?.stringValue
            } catch {
                addError = error.localizedDescription
                addingURL = nil
            }
        }
    }

    /// `job.changed` events with kind "music".
    func handleJob(_ payload: JSONValue) {
        guard let id = payload["id"]?.stringValue, jobID == nil || jobID == id, addingURL != nil else { return }
        jobID = id
        if let log = payload["log"]?.stringValue, !log.isEmpty {
            addMessage = log
            if let match = log.range(of: #"\d+(?=%)"#, options: .regularExpression), let pct = Double(log[match]) {
                addProgress = max(addProgress, pct / 100)
            }
        }
        switch payload["state"]?.stringValue {
        case "completed":
            finishAdd()
            onToast?("Added to music library")
            Task { await reload() }
        case "failed", "interrupted", "cancelled":
            addError = payload["error"]?["message"]?.stringValue ?? "The download did not finish."
            finishAdd()
        default: break
        }
    }

    private func finishAdd() {
        addingURL = nil
        jobID = nil
        addProgress = 0
    }

    func cancelAdd() {
        guard let jobID, let call else { return }
        Task { _ = try? await call("job.cancel", .object(["id": .string(jobID)])) }
    }

    func togglePreview(_ track: MusicTrack) {
        if previewingSlug == track.slug { stopPreview(); return }
        guard let path = track.path else { return }
        stopPreview()
        let item = AVPlayerItem(url: URL(fileURLWithPath: path))
        let player = AVPlayer(playerItem: item)
        self.player = player
        previewingSlug = track.slug
        if let start = track.defaultStart, start > 0 {
            player.seek(to: CMTime(seconds: start, preferredTimescale: 600))
        }
        player.volume = 0.7
        player.play()
        stopTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(30))
            if !Task.isCancelled { self?.stopPreview() }
        }
    }

    func stopPreview() {
        stopTask?.cancel()
        player?.pause()
        player = nil
        previewingSlug = nil
    }
}

struct MusicPanel: View {
    @Bindable var store: EditorStore
    @State private var query = ""
    @State private var trending = false

    private var model: MusicLibraryModel { store.music }

    var body: some View {
        VStack(spacing: 0) {
            inThisReel
            Divider()
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0, pinnedViews: []) {
                    sectionHeader("In music/ (\(model.tracks.count))")
                    if model.tracks.isEmpty {
                        Text("No tracks yet. Search YouTube below and add one.")
                            .font(EditorType.caption).foregroundStyle(.secondary)
                            .padding(.horizontal, 12).padding(.vertical, 6)
                    }
                    ForEach(model.tracks) { track in trackRow(track) }
                    sectionHeader("Add from YouTube")
                    searchField
                    if let error = model.searchError {
                        Text(error).font(EditorType.caption).foregroundStyle(.red)
                            .padding(.horizontal, 12).padding(.bottom, 4)
                    }
                    ForEach(model.results) { result in resultRow(result) }
                }
            }
        }
        .background(EditorPalette.panel)
        .task { await model.reload() }
        .onDisappear { model.stopPreview() }
    }

    private func sectionHeader(_ text: String) -> some View {
        PanelTitle(text)
            .padding(.horizontal, 12).padding(.top, 10).padding(.bottom, 5)
    }

    // MARK: In this reel

    @ViewBuilder private var inThisReel: some View {
        VStack(alignment: .leading, spacing: 6) {
            PanelTitle("In this reel")
            if let snapshot = store.snapshot, let node = store.musicNode {
                let slug = node.raw["track"]?.stringValue ?? node.raw.stringValue ?? "music"
                let known = model.tracks.first { $0.slug == slug }
                HStack(spacing: 8) {
                    Image(systemName: "music.note").foregroundStyle(EditorPalette.audio)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(known?.title ?? slug).font(EditorType.bodyEmphasis).lineLimit(1)
                        if let artist = known?.artist, !artist.isEmpty {
                            Text(artist).font(EditorType.caption).foregroundStyle(.secondary).lineLimit(1)
                        }
                    }
                    Spacer()
                    Button("Remove") { store.removeMusic() }
                        .controlSize(.small)
                        .accessibilityLabel("Remove music from this reel")
                }
                HStack(spacing: 8) {
                    Text("Start").font(EditorType.caption).foregroundStyle(.secondary)
                    MusicStartField(seconds: node.raw["start"]?.numberValue ?? known?.defaultStart) { value in
                        store.editField(documentID: snapshot.documentId, nodeID: node.id, path: "/start", value: .number(value))
                    }
                    Text("Volume").font(EditorType.caption).foregroundStyle(.secondary)
                    MusicVolumeSlider(value: node.raw["volume"]?.numberValue ?? 0.5) { value in
                        store.editField(documentID: snapshot.documentId, nodeID: node.id, path: "/volume", value: .number(value))
                    }
                }
            } else {
                Text("No music. Double-click a track, or drag one onto the timeline.")
                    .font(EditorType.caption).foregroundStyle(.secondary)
            }
        }
        .padding(12)
    }

    // MARK: Rows

    private func trackRow(_ track: MusicTrack) -> some View {
        let active = store.musicNode?.raw["track"]?.stringValue == track.slug
        return HStack(spacing: 8) {
            Button { model.togglePreview(track) } label: {
                Image(systemName: model.previewingSlug == track.slug ? "stop.circle.fill" : "play.circle")
                    .font(.system(size: 18)).foregroundStyle(EditorPalette.audio)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(model.previewingSlug == track.slug ? "Stop preview" : "Preview \(track.title)")
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 5) {
                    Text(track.title).font(EditorType.bodyEmphasis).lineLimit(1)
                    if track.trending {
                        Text("TRENDING").font(.system(size: 8, weight: .bold)).padding(.horizontal, 4).padding(.vertical, 1)
                            .background(EditorPalette.audio.opacity(0.25), in: Capsule())
                    }
                }
                Text([track.artist, MusicTime.clock(track.duration)].filter { !$0.isEmpty }.joined(separator: "  ·  "))
                    .font(EditorType.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Spacer(minLength: 4)
            Button(active ? "In reel" : "Use") { store.setMusic(track: track.slug, startSeconds: nil) }
                .controlSize(.small)
                .disabled(active || store.snapshot == nil)
        }
        .padding(.horizontal, 12).padding(.vertical, 5)
        .background(active ? EditorPalette.audio.opacity(0.14) : Color.clear)
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { store.setMusic(track: track.slug, startSeconds: nil) }
        .onDrag {
            let provider = NSItemProvider()
            provider.registerDataRepresentation(forTypeIdentifier: NSPasteboard.PasteboardType.reelsMusicTrack.rawValue,
                                                visibility: .all) { completion in
                completion(Data(track.slug.utf8), nil); return nil
            }
            provider.registerObject(track.slug as NSString, visibility: .all)
            return provider
        }
        .help(track.credit ?? track.title)
    }

    private var searchField: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass").foregroundStyle(.secondary)
                TextField("Song title and artist", text: $query)
                    .textFieldStyle(.plain).font(EditorType.body)
                    .onSubmit { model.search(query) }
                    .accessibilityLabel("Search YouTube for music")
                if model.isSearching { ProgressView().controlSize(.small) }
            }
            .padding(.horizontal, 9).padding(.vertical, 6)
            .background(EditorPalette.raised, in: RoundedRectangle(cornerRadius: 5))
            Toggle("Trending song (credits it as Instagram's)", isOn: $trending)
                .toggleStyle(.checkbox).font(EditorType.caption)
        }
        .padding(.horizontal, 10).padding(.bottom, 6)
    }

    private func resultRow(_ result: MusicResult) -> some View {
        let busy = model.addingURL == result.url
        return VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(result.title).font(EditorType.body).lineLimit(2)
                    Text([result.channel, MusicTime.clock(result.duration)].filter { !$0.isEmpty }.joined(separator: "  ·  "))
                        .font(EditorType.caption).foregroundStyle(.secondary).lineLimit(1)
                }
                Spacer(minLength: 4)
                Button(busy ? "Adding…" : "Add to Library") { model.add(result, trending: trending) }
                    .controlSize(.small)
                    .disabled(model.addingURL != nil)
            }
            if busy {
                ProgressView(value: model.addProgress).tint(EditorPalette.audio)
                HStack {
                    Text(model.addMessage).font(EditorType.caption).foregroundStyle(.secondary).lineLimit(1)
                    Spacer()
                    Button("Cancel") { model.cancelAdd() }.controlSize(.mini)
                }
            }
            if let error = model.addError, model.addingURL == nil, result.url == model.results.first?.url {
                Text(error).font(EditorType.caption).foregroundStyle(.red)
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 5)
    }
}

private struct MusicStartField: View {
    let seconds: Double?
    let commit: (Double) -> Void
    @State private var text = ""

    var body: some View {
        TextField("auto", text: $text)
            .textFieldStyle(.roundedBorder)
            .font(EditorType.timecode)
            .frame(width: 56)
            .onAppear { text = seconds.map { MusicTime.clock($0) } ?? "" }
            .onChange(of: seconds) { _, value in text = value.map { MusicTime.clock($0) } ?? "" }
            .onSubmit { if let value = MusicTime.parse(text) { commit(value) } }
            .accessibilityLabel("Music start, minutes and seconds")
    }
}

private struct MusicVolumeSlider: View {
    let value: Double
    let commit: (Double) -> Void
    @State private var draft: Double = 0.5

    var body: some View {
        HStack(spacing: 4) {
            Slider(value: $draft, in: 0...1) { editing in if !editing { commit((draft * 100).rounded() / 100) } }
                .controlSize(.small).tint(EditorPalette.audio)
            Text(String(format: "%.2f", draft)).font(EditorType.timecode).foregroundStyle(.secondary)
        }
        .onAppear { draft = value }
        .onChange(of: value) { _, new in draft = new }
        .accessibilityLabel("Music volume")
    }
}
