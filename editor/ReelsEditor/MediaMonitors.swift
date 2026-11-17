@preconcurrency import AVKit
import AppKit
import SwiftUI
@preconcurrency import WebKit

struct SourceMonitor: View {
    @Bindable var store: EditorStore
    @ObservedObject var model: SourcePlayerModel

    var body: some View {
        ZStack {
            Color.black
            if store.selectedAssetIsPhoto, let url = store.sourceURL {
                SourcePhotoView(url: url)
            } else if let player = model.player {
                ZStack {
                    SourcePlayerView(player: player)
                    if !model.isPlaying {
                        if let frame = model.pausedFrame {
                            Image(nsImage: frame)
                                .resizable()
                                .aspectRatio(contentMode: .fit)
                                .padding(12)
                                .allowsHitTesting(false)
                        } else {
                            ProgressView("Decoding source frame…")
                                .controlSize(.small)
                                .foregroundStyle(.white)
                                .allowsHitTesting(false)
                        }
                    }
                }
                .overlay {
                    if let id = store.selectedAssetID, store.sourceLayouts[id] == "front-back" {
                        FrontBackLabels()
                    }
                }
            } else if let id = store.selectedAssetID, ["pending", "loading"].contains(store.sourceStates[id] ?? ""),
                      store.selectedAsset?["probe"]?["sphere"] != nil {
                VStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    Text("Preparing the 360 front and back views…")
                        .font(EditorType.body)
                    Text("The first time takes a few minutes for a long clip; after that it opens instantly.")
                        .font(EditorType.caption)
                        .foregroundStyle(.white.opacity(0.7))
                        .multilineTextAlignment(.center)
                        .frame(maxWidth: 280)
                }
                .foregroundStyle(.white)
            } else {
                ContentUnavailableView(
                    store.selectedAsset == nil ? "Choose a source clip" : "Source preview unavailable",
                    systemImage: "play.rectangle",
                    description: Text(store.selectedAsset == nil
                        ? "Click a clip in the Library to watch it here, then mark In and Out (I / O)."
                        : "This clip has no playable preview file.")
                )
                .foregroundStyle(.white.opacity(0.86))
            }
        }
        .onAppear {
            model.load(store.selectedAssetIsPhoto ? nil : store.sourceURL, at: store.sourceIn ?? 0)
        }
        .onChange(of: store.sourceURL) { _, url in
            model.load(store.selectedAssetIsPhoto ? nil : url, at: store.sourceIn ?? 0)
        }
        .onChange(of: store.selectedAssetID) { _, _ in
            model.load(store.selectedAssetIsPhoto ? nil : store.sourceURL, at: store.sourceIn ?? 0, forceSeekIfSame: true)
        }
        .onChange(of: store.selectedAssetIsPhoto) { _, isPhoto in
            if isPhoto { model.load(nil) } else { model.load(store.sourceURL, at: store.sourceIn ?? 0) }
        }
        .onChange(of: store.sourceIn) { _, seconds in
            if let seconds, !store.selectedAssetIsPhoto { model.seek(to: seconds) }
        }
        .onDisappear {
            model.pause()
        }
    }
}

/// "Front" / "Back" over the two halves of a 360 Source proxy (1440×1280, aspect-fit with padding 0).
private struct FrontBackLabels: View {
    var body: some View {
        GeometryReader { geo in
            let aspect = 1440.0 / 1280.0
            let width = min(geo.size.width, geo.size.height * aspect)
            let height = width / aspect
            let x0 = (geo.size.width - width) / 2
            let y0 = (geo.size.height - height) / 2
            ForEach(Array(["Front", "Back"].enumerated()), id: \.offset) { index, label in
                Text(label.uppercased())
                    .font(EditorType.panelTitle)
                    .tracking(EditorType.panelTitleTracking)
                    .padding(.horizontal, 6).padding(.vertical, 3)
                    .background(.black.opacity(0.55), in: RoundedRectangle(cornerRadius: 4))
                    .foregroundStyle(.white)
                    .position(x: x0 + width * (0.25 + 0.5 * Double(index)), y: y0 + 16)
            }
        }
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

private struct SourcePhotoView: View {
    let url: URL
    @State private var imageData: Data?
    @State private var failed = false

    var body: some View {
        Group {
            if url.isFileURL {
                if let imageData, let image = NSImage(data: imageData) {
                    Image(nsImage: image)
                        .resizable()
                        .aspectRatio(contentMode: .fit)
                        .padding(12)
                } else if failed {
                    ContentUnavailableView("Photo unavailable", systemImage: "photo",
                                           description: Text("This photo could not be opened."))
                        .foregroundStyle(.white)
                } else {
                    ProgressView("Loading photo…").controlSize(.small).foregroundStyle(.white)
                }
            } else {
                AsyncImage(url: url) { phase in
                    if let image = phase.image {
                        image.resizable().aspectRatio(contentMode: .fit).padding(12)
                    } else if phase.error != nil {
                        ContentUnavailableView("Photo unavailable", systemImage: "photo",
                                               description: Text("This photo could not be loaded."))
                            .foregroundStyle(.white)
                    } else {
                        ProgressView("Loading photo…").controlSize(.small).foregroundStyle(.white)
                    }
                }
            }
        }
        .task(id: url) {
            guard url.isFileURL else { return }
            let data = await Task.detached(priority: .userInitiated) {
                try? Data(contentsOf: url)
            }.value
            imageData = data
            failed = data == nil
        }
        .accessibilityLabel("Source photo")
    }
}

@MainActor
final class SourcePlayerModel: ObservableObject {
    private static let posterCache: NSCache<NSString, NSImage> = {
        let cache = NSCache<NSString, NSImage>()
        cache.countLimit = 48
        cache.totalCostLimit = 48 * 1024 * 1024
        return cache
    }()

    @Published var player: AVPlayer?
    @Published private(set) var pausedFrame: NSImage?
    @Published private(set) var isPlaying = false
    @Published private(set) var currentSeconds = 0.0
    @Published private(set) var durationSeconds = 0.0
    private var currentURL: URL?
    private var timeObserver: Any?
    private var timeJumpObserver: NSObjectProtocol?
    private var rateObservation: NSKeyValueObservation?
    private var itemStatusObservation: NSKeyValueObservation?
    private var requestedPosterKey: String?
    private var posterGeneration = 0

    isolated deinit {
        if let timeJumpObserver { NotificationCenter.default.removeObserver(timeJumpObserver) }
        rateObservation?.invalidate()
        itemStatusObservation?.invalidate()
        if let timeObserver, let player { player.removeTimeObserver(timeObserver) }
    }

    func load(_ url: URL?, at seconds: Double = 0, forceSeekIfSame: Bool = false) {
        guard let url else {
            posterGeneration += 1
            detachObservers()
            player?.pause()
            player = nil
            currentURL = nil
            pausedFrame = nil
            isPlaying = false
            currentSeconds = 0
            durationSeconds = 0
            return
        }
        guard currentURL != url else {
            if forceSeekIfSame { seek(to: seconds) }
            return
        }
        player?.pause()
        detachObservers()
        currentURL = url
        pausedFrame = nil
        currentSeconds = 0
        durationSeconds = 0
        requestedPosterKey = nil
        let nextPlayer = AVPlayer(url: url)
        player = nextPlayer
        timeObserver = nextPlayer.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 0.15, preferredTimescale: 600),
            queue: .main
        ) { [weak self, weak nextPlayer] time in
            guard time.seconds.isFinite else { return }
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.isPlaying = (nextPlayer?.rate ?? 0) != 0
                self.currentSeconds = time.seconds
                if let duration = nextPlayer?.currentItem?.duration.seconds, duration.isFinite, duration > 0 {
                    self.durationSeconds = duration
                }
                if !self.isPlaying { self.refreshPausedFrame(at: time.seconds) }
            }
        }
        if let item = nextPlayer.currentItem {
            itemStatusObservation = item.observe(\.status, options: [.initial, .new]) { [weak self, weak nextPlayer] _, _ in
                let duration = nextPlayer?.currentItem?.duration.seconds ?? 0
                Task { @MainActor [weak self] in
                    guard let self, duration.isFinite, duration > 0 else { return }
                    self.durationSeconds = duration
                }
            }
            timeJumpObserver = NotificationCenter.default.addObserver(
                forName: .AVPlayerItemTimeJumped, object: item, queue: .main
            ) { [weak self] _ in
                Task { @MainActor [weak self] in self?.refreshPausedFrame() }
            }
        }
        rateObservation = nextPlayer.observe(\.rate, options: [.initial, .new]) { [weak self] _, change in
            let rate = change.newValue ?? 0
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.isPlaying = rate != 0
                if !self.isPlaying { self.refreshPausedFrame() }
            }
        }
        seek(to: seconds)
    }

    func seek(to seconds: Double) {
        guard let player else { return }
        let target = max(0, seconds.isFinite ? seconds : 0)
        pausedFrame = nil
        currentSeconds = target
        requestedPosterKey = nil
        posterGeneration += 1
        player.seek(
            to: CMTime(seconds: target, preferredTimescale: 600),
            toleranceBefore: .zero,
            toleranceAfter: .zero
        ) { [weak self] finished in
            guard finished else { return }
            Task { @MainActor [weak self] in self?.refreshPausedFrame(at: target) }
        }
    }

    func pause() { player?.pause() }

    func togglePlayback() {
        guard let player else { return }
        if player.rate == 0 { player.play() } else { player.pause() }
    }

    private func detachObservers() {
        if let timeObserver, let player { player.removeTimeObserver(timeObserver) }
        timeObserver = nil
        if let timeJumpObserver { NotificationCenter.default.removeObserver(timeJumpObserver) }
        timeJumpObserver = nil
        rateObservation?.invalidate()
        rateObservation = nil
        itemStatusObservation?.invalidate()
        itemStatusObservation = nil
    }

    private func refreshPausedFrame(at seconds: Double? = nil) {
        guard !isPlaying, let url = currentURL else { return }
        let time = max(0, seconds ?? currentSeconds)
        currentSeconds = time
        let key = "\(url.absoluteString)#\((time * 30).rounded())"
        if let cached = Self.posterCache.object(forKey: key as NSString) {
            pausedFrame = cached
            return
        }
        guard requestedPosterKey != key else { return }
        requestedPosterKey = key
        posterGeneration += 1
        let generation = posterGeneration
        Task { [weak self] in
            let image = await Task.detached(priority: .utility) {
                let asset = AVURLAsset(url: url)
                let generator = AVAssetImageGenerator(asset: asset)
                generator.appliesPreferredTrackTransform = true
                generator.maximumSize = CGSize(width: 1600, height: 900)
                generator.requestedTimeToleranceBefore = CMTime(seconds: 0.04, preferredTimescale: 600)
                generator.requestedTimeToleranceAfter = CMTime(seconds: 0.04, preferredTimescale: 600)
                let time = CMTime(seconds: time, preferredTimescale: 600)
                guard let cgImage = try? generator.copyCGImage(at: time, actualTime: nil) else { return nil as CGImage? }
                return cgImage
            }.value
            guard let self, generation == self.posterGeneration, self.currentURL == url,
                  self.player != nil, !self.isPlaying else { return }
            self.requestedPosterKey = nil
            guard let image else { return }
            let poster = NSImage(cgImage: image, size: .zero)
            Self.posterCache.setObject(poster, forKey: key as NSString, cost: image.bytesPerRow * image.height)
            self.pausedFrame = poster
        }
    }
}

private struct SourcePlayerView: NSViewRepresentable {
    let player: AVPlayer
    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.controlsStyle = .none
        view.showsFullScreenToggleButton = false
        view.videoGravity = .resizeAspect
        view.player = player
        return view
    }
    func updateNSView(_ view: AVPlayerView, context: Context) {
        if view.player !== player { view.player = player }
    }
}

struct ProgramMonitor: View {
    @Bindable var store: EditorStore
    @StateObject private var exactPlayer = ExactProgramPlayerModel()

    var body: some View {
        Group {
            if store.isExactPreview {
                if let url = store.exactMovieURL {
                    ExactProgramPlayerView(player: exactPlayer.player)
                        .onAppear {
                            exactPlayer.update(
                                url: url, revision: store.previewRevision, frame: store.playheadFrame,
                                seekGeneration: store.seekGeneration, fps: store.fps,
                                rate: store.playbackRate, store: store
                            )
                        }
                        .onChange(of: url) { _, value in
                            exactPlayer.update(
                                url: value, revision: store.previewRevision, frame: store.playheadFrame,
                                seekGeneration: store.seekGeneration, fps: store.fps,
                                rate: store.playbackRate, store: store
                            )
                        }
                        .onChange(of: store.seekGeneration) { _, _ in
                            exactPlayer.seek(frame: store.playheadFrame, fps: store.fps, generation: store.seekGeneration)
                        }
                    unavailable(
                        store.isEmptyPreview ? "Nothing to preview" : "Final-quality render isn't ready",
                        detail: store.isEmptyPreview
                            ? "All clips are disabled. Re-enable a clip to restore picture and sound."
                            : "Wait a moment, or reload the preview."
                    )
                }
            } else if let url = store.previewURL, let origin = store.previewOrigin {
                ProgramWebView(
                    url: url,
                    manifest: store.previewManifest ?? store.snapshot?.preview,
                    origin: origin,
                    revision: store.previewRevision,
                    playheadFrame: store.playheadFrame,
                    seekGeneration: store.seekGeneration,
                    rate: store.playbackRate,
                    store: store
                )
            } else {
                VStack(spacing: 8) {
                    unavailable(
                        store.hasDocument ? "Preview isn't loaded" : "No reel open",
                        detail: store.previewFailureMessage
                            ?? (store.hasDocument ? "Load the preview to watch the reel with your latest edits." : "Open a reel from the Reels list to preview it.")
                    )
                    Button("Load \(PreviewModeLabel.title(store.currentPreviewMode).lowercased())") {
                        store.preparePreview(mode: store.currentPreviewMode)
                    }
                    .controlSize(.small)
                    .disabled(!store.hasDocument || store.isPreparingPreview)
                }
            }
        }
        .background(.black)
        .onChange(of: store.isExactPreview) { _, active in
            exactPlayer.setActive(active)
        }
        .onChange(of: store.isEmptyPreview) { _, empty in
            if empty { exactPlayer.clear() }
        }
    }

    private func unavailable(_ title: String, detail: String) -> some View {
        VStack(spacing: 8) {
            Image(systemName: "rectangle.on.rectangle.slash")
                .font(.system(size: 24))
                .foregroundStyle(.secondary)
            Text(title).font(.system(size: 13, weight: .medium))
            Text(detail).font(.system(size: 12)).foregroundStyle(.white.opacity(0.7))
                .multilineTextAlignment(.center)
                .frame(maxWidth: 320)
            if store.isPreparingPreview { ProgressView().controlSize(.small) }
        }
        .padding(16)
        .foregroundStyle(.white)
    }
}

@MainActor
final class ExactProgramPlayerModel: ObservableObject {
    @Published var player: AVPlayer?
    private weak var store: EditorStore?
    private var currentURL: URL?
    private var revision: String?
    private var rate = 1.0
    private var observer: NSObjectProtocol?
    private var rateObserver: NSObjectProtocol?
    private var timeObserver: Any?
    private var lastSeekGeneration: Int?
    private(set) var isActive = false
    private var playbackProbe = PreviewPlaybackProbe()
    private var programReadyPosted = false

    init() {
        observer = NotificationCenter.default.addObserver(
            forName: .reelsTogglePlayback, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in
                guard self?.isActive == true else { return }
                self?.togglePlayback()
            }
        }
        rateObserver = NotificationCenter.default.addObserver(
            forName: .reelsPlaybackRate, object: nil, queue: .main
        ) { [weak self] notification in
            guard let rate = notification.object as? Double else { return }
            Task { @MainActor in
                guard self?.isActive == true else { return }
                self?.setRate(rate)
            }
        }
    }

    deinit {
        if let observer { NotificationCenter.default.removeObserver(observer) }
        if let rateObserver { NotificationCenter.default.removeObserver(rateObserver) }
    }

    func update(url: URL, revision: String?, frame: Int, seekGeneration: Int,
                fps: Double, rate: Double, store: EditorStore) {
        isActive = true
        self.store = store
        self.revision = revision
        self.rate = rate
        if currentURL != url {
            if let timeObserver, let player { player.removeTimeObserver(timeObserver) }
            lastSeekGeneration = nil
            playbackProbe = PreviewPlaybackProbe()
            programReadyPosted = false
            currentURL = url
            let nextPlayer = AVPlayer(url: url)
            player = nextPlayer
            timeObserver = nextPlayer.addPeriodicTimeObserver(
                forInterval: CMTime(seconds: 0.1, preferredTimescale: 600),
                queue: .main
            ) { [weak self] time in
                guard time.seconds.isFinite else { return }
                Task { @MainActor [weak self] in
                    guard let self, let store = self.store else { return }
                    store.updatePlayheadFromPreview(
                        Int((time.seconds * store.fps).rounded()),
                        revision: self.revision
                    )
                    guard !self.programReadyPosted,
                          self.player?.currentItem?.status == .readyToPlay else { return }
                    let frame = Int((time.seconds * store.fps).rounded())
                    if NativePreviewCapture.playbackRequested {
                        if self.playbackProbe.firstFrame == nil {
                            self.playbackProbe.begin(at: frame)
                            self.player?.playImmediately(atRate: Float(self.rate))
                        } else if self.playbackProbe.observe(frame) {
                            self.player?.pause()
                            self.programReadyPosted = true
                            NativePreviewCapture.ready(mode: "exact", revision: self.revision, probe: self.playbackProbe)
                        }
                    } else {
                        self.programReadyPosted = true
                        NativePreviewCapture.ready(mode: "exact", revision: self.revision)
                    }
                }
            }
        }
        seek(frame: frame, fps: fps, generation: seekGeneration)
    }

    func setActive(_ active: Bool) {
        isActive = active
        if !active { player?.pause() }
    }
    func clear() {
        isActive = false
        player?.pause()
        if let timeObserver, let player { player.removeTimeObserver(timeObserver) }
        timeObserver = nil
        player?.replaceCurrentItem(with: nil)
        player = nil
        currentURL = nil
        revision = nil
        lastSeekGeneration = nil
        playbackProbe = PreviewPlaybackProbe()
        programReadyPosted = false
    }

    func seek(frame: Int, fps: Double, generation: Int) {
        guard generation != lastSeekGeneration, let player else { return }
        lastSeekGeneration = generation
        let boundedFrame = min(max(0, frame), store?.snapshot?.durationFrames ?? frame)
        player.seek(to: CMTime(seconds: Double(boundedFrame) / max(fps, 1), preferredTimescale: 600))
    }

    private func togglePlayback() {
        guard let player else { return }
        if player.rate == 0 { player.playImmediately(atRate: Float(rate)) }
        else { player.pause() }
    }

    private func setRate(_ value: Double) {
        rate = min(4, max(0.25, value))
        if let player, player.rate != 0 { player.rate = Float(rate) }
    }
}

private struct ExactProgramPlayerView: NSViewRepresentable {
    let player: AVPlayer?
    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.controlsStyle = .none
        view.videoGravity = .resizeAspect
        view.player = player
        return view
    }
    func updateNSView(_ view: AVPlayerView, context: Context) {
        if view.player !== player { view.player = player }
    }
}

private struct ProgramWebView: NSViewRepresentable {
    let url: URL?
    let manifest: JSONValue?
    let origin: URL
    let revision: String?
    let playheadFrame: Int
    let seekGeneration: Int
    let rate: Double
    let store: EditorStore

    func makeCoordinator() -> Coordinator { Coordinator(store: store, origin: origin) }

    func makeNSView(context: Context) -> WKWebView {
        let controller = WKUserContentController()
        controller.add(context.coordinator, name: "reelsPreview")
        let config = WKWebViewConfiguration()
        config.userContentController = controller
        config.websiteDataStore = .nonPersistent()
        let webView = WKWebView(frame: .zero, configuration: config)
        webView.navigationDelegate = context.coordinator
        webView.setValue(false, forKey: "drawsBackground")
        webView.wantsLayer = true
        webView.layer?.backgroundColor = NSColor.black.cgColor
        context.coordinator.webView = webView
        context.coordinator.load(
            url, manifest: manifest, revision: revision, frame: playheadFrame,
            rate: rate, origin: origin
        )
        return webView
    }

    func updateNSView(_ webView: WKWebView, context: Context) {
        context.coordinator.webView = webView
        context.coordinator.load(
            url, manifest: manifest, revision: revision, frame: playheadFrame,
            rate: rate, origin: origin
        )
        context.coordinator.seekIfNeeded(frame: playheadFrame, generation: seekGeneration)
    }

    @MainActor
    final class Coordinator: NSObject, WKScriptMessageHandler, WKNavigationDelegate {
        weak var store: EditorStore?
        weak var webView: WKWebView?
        private var expectedOrigin: URL
        private var loadedURL: URL?
        private var loadedManifest: JSONValue?
        private var loadedRevision: String?
        private var pageReady = false
        private var lastSeekGeneration: Int?
        private var lastFrame = 0
        private var playbackObserver: NSObjectProtocol?
        private var rateObserver: NSObjectProtocol?
        private var currentRate = 1.0
        private var isPlaying = false
        private var playbackProbe = PreviewPlaybackProbe()

        init(store: EditorStore, origin: URL) {
            self.store = store
            expectedOrigin = origin
            super.init()
            playbackObserver = NotificationCenter.default.addObserver(
                forName: .reelsTogglePlayback, object: nil, queue: .main
            ) { [weak self] _ in
                Task { @MainActor in self?.togglePlayback() }
            }
            rateObserver = NotificationCenter.default.addObserver(
                forName: .reelsPlaybackRate, object: nil, queue: .main
            ) { [weak self] notification in
                guard let rate = notification.object as? Double else { return }
                Task { @MainActor in self?.setPlaybackRate(rate) }
            }
        }

        deinit {
            if let playbackObserver { NotificationCenter.default.removeObserver(playbackObserver) }
            if let rateObserver { NotificationCenter.default.removeObserver(rateObserver) }
        }

        func load(_ url: URL?, manifest: JSONValue?, revision: String?, frame: Int,
                  rate: Double, origin: URL) {
            lastFrame = frame
            currentRate = rate
            expectedOrigin = origin
            guard let url else { return }
            guard allowedCapability(url, expectedOrigin: origin) else {
                store?.errorMessage = "The Program preview URL does not match the editing service capability."
                return
            }
            guard let manifest else {
                store?.requestExactPreview(reason: "The fast preview could not load")
                return
            }
            if loadedURL == url, loadedRevision == revision, loadedManifest == manifest {
                return
            }
            let pageChanged = loadedURL != url
            loadedURL = url
            loadedManifest = manifest
            loadedRevision = revision
            if pageChanged {
                pageReady = false
                isPlaying = false
                playbackProbe = PreviewPlaybackProbe()
                webView?.load(URLRequest(url: url, cachePolicy: .reloadIgnoringLocalCacheData))
            } else if pageReady {
                sendLoadRevision()
            }
        }

        func seekIfNeeded(frame: Int, generation: Int) {
            lastFrame = frame
            guard generation != lastSeekGeneration, loadedURL != nil, pageReady,
                  let revision = loadedRevision else { return }
            lastSeekGeneration = generation
            send(["type": "seek", "revision": revision, "frame": frame])
        }

        private func sendLoadRevision() {
            guard let manifest = loadedManifest, let revision = loadedRevision,
                  let command = PreviewBridgeContract.loadCommand(
                    manifest: manifest, revision: revision, startFrame: lastFrame
                  ),
                  let commandData = command.jsonData(),
                  let commandObject = try? JSONSerialization.jsonObject(with: commandData) as? [String: Any] else {
                store?.requestExactPreview(reason: "Interactive preview is missing protected props or static media mapping")
                return
            }
            send(commandObject)
        }

        private func send(_ command: [String: Any]) {
            guard let url = loadedURL,
                  let commandData = try? JSONSerialization.data(withJSONObject: command),
                  let commandValue = try? JSONDecoder().decode(JSONValue.self, from: commandData),
                  let envelopeJSON = PreviewBridgeContract.envelope(commandValue)
                    .jsonData()
                    .flatMap({ String(data: $0, encoding: .utf8) }),
                  let origin = PreviewBridgeContract.originString(expectedOrigin),
                  isCapabilityURL(url) else { return }
            webView?.evaluateJavaScript(
                "window.postMessage(\(envelopeJSON), \(String(reflecting: origin)));",
                completionHandler: { [weak self] _, error in
                    if let error {
                        Task { @MainActor in
                            self?.store?.requestExactPreview(reason: "WebKit command bridge failed: \(error.localizedDescription)")
                        }
                    }
                }
            )
        }

        func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
            guard message.frameInfo.isMainFrame,
                  allowed(message.frameInfo.securityOrigin),
                  let requestURL = message.frameInfo.request.url,
                  isCapabilityURL(requestURL) else { return }
            guard let body = message.body as? [String: Any],
                  let type = body["type"] as? String else { return }
            let eventRevision = body["revision"] as? String
            guard PreviewBridgeContract.eventMatches(
                revision: eventRevision, loadedRevision: loadedRevision, type: type
            ) else { return }
            switch type {
            case "ready":
                guard let eventRevision else { return }
                pageReady = true
                isPlaying = false
                send(["type": "playbackRate", "revision": eventRevision, "rate": currentRate])
                send(["type": "seek", "revision": eventRevision, "frame": lastFrame])
                if NativePreviewCapture.playbackRequested {
                    if playbackProbe.firstFrame == nil {
                        playbackProbe.begin(at: lastFrame)
                        isPlaying = true
                        send(["type": "play", "revision": eventRevision])
                    }
                } else {
                    NativePreviewCapture.ready(mode: "interactive", revision: eventRevision)
                }
            case "frame":
                guard let eventRevision, let frame = body["frame"] as? NSNumber else { return }
                store?.updatePlayheadFromPreview(frame.intValue, revision: eventRevision)
                if NativePreviewCapture.playbackRequested, playbackProbe.observe(frame.intValue) {
                    isPlaying = false
                    send(["type": "pause", "revision": eventRevision])
                    NativePreviewCapture.ready(mode: "interactive", revision: eventRevision, probe: playbackProbe)
                    Task { @MainActor [weak self] in
                        guard let webView = self?.webView,
                              let image = try? await webView.takeSnapshot(configuration: nil),
                              let destination = NativePreviewCapture.destination,
                              let tiff = image.tiffRepresentation,
                              let bitmap = NSBitmapImageRep(data: tiff),
                              let png = bitmap.representation(using: .png, properties: [:]) else { return }
                        try? png.write(
                            to: destination.deletingPathExtension().appendingPathExtension("program.png"), options: .atomic)
                    }
                }
            case "error":
                store?.requestExactPreview(reason: body["message"] as? String ?? "Remotion preview error")
            default:
                break
            }
        }

        func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
            guard let url = webView.url, isCapabilityURL(url) else { return }
            pageReady = true
            sendLoadRevision()
        }

        func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction) async -> WKNavigationActionPolicy {
            guard let url = navigationAction.request.url else {
                return .cancel
            }
            return isCapabilityURL(url) ? .allow : .cancel
        }

        func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
            store?.requestExactPreview(reason: "WebKit navigation failed: \(error.localizedDescription)")
        }

        private func togglePlayback() {
            guard let revision = loadedRevision else { return }
            isPlaying.toggle()
            send(["type": isPlaying ? "play" : "pause", "revision": revision])
        }

        private func setPlaybackRate(_ rate: Double) {
            currentRate = min(4, max(0.25, rate))
            if let revision = loadedRevision {
                send(["type": "playbackRate", "revision": revision, "rate": currentRate])
            }
        }

        private func allowedCapability(_ url: URL, expectedOrigin: URL) -> Bool {
            guard PreviewBridgeContract.sameOrigin(url, expectedOrigin),
                  let scheme = url.scheme?.lowercased(), scheme == "http" || scheme == "https" else { return false }
            return !url.path.isEmpty
        }

        private func isCapabilityURL(_ url: URL) -> Bool {
            guard let loadedURL else { return false }
            return PreviewBridgeContract.allowsPage(url, origin: expectedOrigin, capability: loadedURL)
        }

        private func allowed(_ origin: WKSecurityOrigin) -> Bool {
            guard let expected = URLComponents(url: expectedOrigin, resolvingAgainstBaseURL: false) else { return false }
            return origin.protocol.lowercased() == expected.scheme?.lowercased()
                && origin.host.lowercased() == expected.host?.lowercased()
                && origin.port == (expected.port ?? (expected.scheme == "https" ? 443 : 80))
        }

        private func sameOrigin(_ lhs: URL, _ rhs: URL) -> Bool {
            PreviewBridgeContract.sameOrigin(lhs, rhs)
        }
    }
}
