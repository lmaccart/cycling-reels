import AVKit
import ObjectiveC.runtime
import os
import SwiftUI
import WebKit
import XCTest
@testable import ReelsEditor

/// Unordered, non-key windows with visible-view scheduling; no WindowServer input or AX client.
@MainActor
private final class FullWindowTestWindow: NSWindow {
    override var occlusionState: NSWindow.OcclusionState { .visible }
}

/// Test-local Objective-C instrumentation. Times the real hosting views, including SwiftUI
/// graph/layout work below their AppKit entry points; restores every IMP before returning.
@MainActor
private final class HostingWorkTrace {
    struct Span {
        let name: String
        let start: Double
        let end: Double
    }
    var enabled = false
    var spans: [Span] = []
    var names: [ObjectIdentifier: String] = [:]
    var didLayout: ((String, Double) -> Void)?
    private var restorations: [(AnyClass, Selector, IMP, IMP)] = []
    private let log = OSLog(subsystem: "com.cycling.ReelsEditorTests", category: "FullWindowDelete")

    func install(_ views: [NSView]) {
        var classes = Set<ObjectIdentifier>()
        for view in views where names[ObjectIdentifier(view)] != nil {
            let cls: AnyClass = type(of: view)
            guard classes.insert(ObjectIdentifier(cls)).inserted else { continue }
            for selector in [#selector(NSView.layout), #selector(NSView.updateConstraints)] {
                guard let method = class_getInstanceMethod(cls, selector) else { continue }
                let original = method_getImplementation(method)
                let call = unsafeBitCast(original, to: (@convention(c) (NSView, Selector) -> Void).self)
                let block: @convention(block) (NSView) -> Void = { [weak self] view in
                    MainActor.assumeIsolated {
                        guard let self, self.enabled, let name = self.names[ObjectIdentifier(view)] else {
                            call(view, selector)
                            return
                        }
                        let start = ProcessInfo.processInfo.systemUptime
                        let id = OSSignpostID(log: self.log)
                        os_signpost(.begin, log: self.log, name: "HostingWork", signpostID: id,
                                    "%{public}s %{public}s", name, NSStringFromSelector(selector))
                        call(view, selector)
                        os_signpost(.end, log: self.log, name: "HostingWork", signpostID: id)
                        let end = ProcessInfo.processInfo.systemUptime
                        self.spans.append(Span(name: name + "." + NSStringFromSelector(selector),
                                               start: start, end: end))
                        if selector == #selector(NSView.layout) { self.didLayout?(name, end) }
                    }
                }
                let replacement = imp_implementationWithBlock(block)
                // Add an own slot if inherited: never replace NSView's implementation globally.
                class_addMethod(cls, selector, original, method_getTypeEncoding(method))
                class_replaceMethod(cls, selector, replacement, method_getTypeEncoding(method))
                restorations.append((cls, selector, original, replacement))
            }
            for selector in [#selector(getter: NSView.fittingSize), #selector(getter: NSView.intrinsicContentSize)] {
                guard let method = class_getInstanceMethod(cls, selector) else { continue }
                let original = method_getImplementation(method)
                let call = unsafeBitCast(original, to: (@convention(c) (NSView, Selector) -> NSSize).self)
                let block: @convention(block) (NSView) -> NSSize = { [weak self] view in
                    MainActor.assumeIsolated {
                        let start = ProcessInfo.processInfo.systemUptime
                        let size = call(view, selector)
                        if let self, self.enabled, let name = self.names[ObjectIdentifier(view)] {
                            self.spans.append(Span(name: name + "." + NSStringFromSelector(selector),
                                                   start: start, end: ProcessInfo.processInfo.systemUptime))
                        }
                        return size
                    }
                }
                let replacement = imp_implementationWithBlock(block)
                class_addMethod(cls, selector, original, method_getTypeEncoding(method))
                class_replaceMethod(cls, selector, replacement, method_getTypeEncoding(method))
                restorations.append((cls, selector, original, replacement))
            }
            if view is TimelineCanvas {
                let selector = NSSelectorFromString("setAccessibilityChildren:")
                if let method = class_getInstanceMethod(cls, selector) {
                    let original = method_getImplementation(method)
                    let call = unsafeBitCast(original, to: (@convention(c) (NSView, Selector, NSArray?) -> Void).self)
                    let block: @convention(block) (NSView, NSArray?) -> Void = { [weak self] view, children in
                        MainActor.assumeIsolated {
                            let start = ProcessInfo.processInfo.systemUptime
                            call(view, selector, children)
                            if let self, self.enabled {
                                self.spans.append(Span(name: "TimelineCanvas.accessibilityChildren", start: start,
                                                       end: ProcessInfo.processInfo.systemUptime))
                            }
                        }
                    }
                    let replacement = imp_implementationWithBlock(block)
                    class_addMethod(cls, selector, original, method_getTypeEncoding(method))
                    class_replaceMethod(cls, selector, replacement, method_getTypeEncoding(method))
                    restorations.append((cls, selector, original, replacement))
                }
            }
        }
    }

    func restore() {
        enabled = false
        for (cls, selector, original, replacement) in restorations.reversed() {
            class_replaceMethod(cls, selector, original, nil)
            imp_removeBlock(replacement)
        }
        restorations.removeAll()
    }

    func report(until deadline: Double) {
        func duration(_ intervals: [Span], until cutoff: Double) -> Double {
            var end = -Double.infinity
            var total = 0.0
            for span in intervals.sorted(by: { $0.start < $1.start }) where span.start < cutoff {
                let clippedEnd = min(span.end, cutoff)
                total += max(0, clippedEnd - max(span.start, end))
                end = max(end, clippedEnd)
            }
            return total
        }
        for name in Set(spans.map(\.name)).sorted() {
            let matching = spans.filter { $0.name == name && $0.start < deadline }
            // Union reentrant calls rather than double-counting inclusive layout spans.
            let elapsed = duration(matching, until: deadline)
            let total = duration(spans.filter { $0.name == name }, until: .infinity)
            print("FULL-WINDOW-WORK \(name) calls=\(matching.count) before-redraw=\(elapsed * 1000)ms total=\(total * 1000)ms")
        }
    }
}

@MainActor
final class FullWindowDeleteTests: XCTestCase {
    private func descendants(_ view: NSView) -> [NSView] {
        [view] + view.subviews.flatMap(descendants)
    }

    private func eventually(_ message: String, _ predicate: () -> Bool) async throws {
        let deadline = ContinuousClock.now + .seconds(30)
        while !predicate(), ContinuousClock.now < deadline { try await Task.sleep(for: .milliseconds(5)) }
        XCTAssertTrue(predicate(), message)
        guard predicate() else { throw NSError(domain: "FullWindowFixture", code: 1) }
    }

    /// Pump the application's normal run loop, not layoutSubtreeIfNeeded, during the metric.
    private func pump(until predicate: () -> Bool) {
        let deadline = Date().addingTimeInterval(5)
        while !predicate(), Date() < deadline {
            RunLoop.current.run(until: Date().addingTimeInterval(0.001))
        }
    }

    func testFullWindowDeleteInputToRedraw() async throws {
        let root = FileManager.default.temporaryDirectory.appending(path: UUID().uuidString)
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let code = ProcessInfo.processInfo.environment["REELS_TEST_CODE_ROOT"].map { URL(fileURLWithPath: $0) }
            ?? URL(fileURLWithPath: #filePath).deletingLastPathComponent()
                .deletingLastPathComponent().deletingLastPathComponent()
        let python = ProcessInfo.processInfo.environment["REELS_TEST_PYTHON"]
            ?? "/Users/leif/rice/cycling/.venv/bin/python"
        let cache = ProcessInfo.processInfo.environment["REELS_TEST_PREVIEW_CACHE"]
            ?? "/Users/leif/rice/cycling/.cache/editor/live/preview"
        guard FileManager.default.isExecutableFile(atPath: python),
              FileManager.default.fileExists(atPath: cache + "/last-ready.json") else {
            throw XCTSkip("Full-window media fixture needs REELS_TEST_PYTHON and REELS_TEST_PREVIEW_CACHE (existing own-footage Remotion cache).")
        }
        // Serve the real cached Remotion bundle and own-footage media, read-only. The short
        // Source proxy is a front/back reproject of existing own footage, never synthetic media.
        // This helper has no edit implementation: persistence is gated independently below.
        let server = #"""
        import json, os, subprocess, sys
        from pathlib import Path
        from reels.config import DEFAULTS
        from reels.editor.http import CapabilityServer
        from reels.editor.schema import inspector_schema
        cache = Path(os.environ['REELS_TEST_PREVIEW_CACHE'])
        ready = json.loads((cache / 'last-ready.json').read_text())
        sphere = next(p for p in sorted((cache / 'spheres').glob('*/sphere.mp4')) if p.stat().st_size > 100000)
        proxy = Path.cwd() / 'front-back.mp4'
        subprocess.run([os.environ.get('REELS_TEST_FFMPEG', '/opt/homebrew/bin/ffmpeg'), '-nostdin', '-v', 'error',
            '-i', str(sphere), '-t', '2', '-filter_complex',
            '[0:v]split[a][b];[a]v360=input=equirect:output=flat:yaw=0:w=720:h=1280[front];'
            '[b]v360=input=equirect:output=flat:yaw=180:w=720:h=1280[back];[front][back]hstack[v]',
            '-map', '[v]', '-an', '-c:v', 'h264_videotoolbox', '-b:v', '2M', str(proxy)], check=True)
        http = CapabilityServer()
        page = http.register_tree('bundle', ready['bundleDir'])
        for p in Path(ready['staticDir']).rglob('*'):
            if p.is_file(): http.register_file('media/' + p.relative_to(ready['staticDir']).as_posix(), p)
        props = ready['props']
        source = props['segments'][0]['src']
        props.update(totalFrames=1950, timeline={'version': 1, 'fps': 30, 'durationFrames': 1950},
            segments=[{'kind':'video','src':source,'startFrame':0,'durationInFrames':1248,'track':'V1','volume':0,'transitionInFrames':0},
                      {'kind':'video','src':source,'startFrame':300,'durationInFrames':1650,'track':'V3','volume':0,'transitionInFrames':0}])
        fixture = {'origin': http.origin, 'sourceURL': http.register_file('front-back.mp4', proxy),
            'schema': inspector_schema(), 'config': DEFAULTS,
            'manifest': {'state':'ready','mode':'interactive','revision':'initial', 'url':page,
                'staticBaseUrl':http.url('media/'), 'props':props}}
        try:
            for line in sys.stdin:
                request = json.loads(line)
                print(json.dumps({'id':request['id'],'result':fixture if request['method']=='fixture' else {}}), flush=True)
                if request['method']=='shutdown': break
        finally: http.close()
        """#
        try server.write(to: root.appending(path: "serve.py"), atomically: true, encoding: .utf8)
        let launcher = root.appending(path: "reels")
        try "#!/bin/sh\nexec \"$REELS_TEST_PYTHON\" serve.py\n".write(to: launcher, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: launcher.path)
        var environment = ProcessInfo.processInfo.environment
        environment["REELS_TEST_PYTHON"] = python
        environment["REELS_TEST_PREVIEW_CACHE"] = cache
        environment["PYTHONPATH"] = code.appending(path: "scripts").path
        let mediaService = EditorService()
        try mediaService.start(repositoryRoot: root, environment: environment)
        defer { mediaService.stopSynchronously() }
        let fixture = try await mediaService.call("fixture")
        let manifest = try XCTUnwrap(fixture["manifest"])
        let asset: JSONValue = .object([
            "id": .string("ride-360"), "kind": .string("video"), "type": .string("360"),
            "name": .string("360 ride"), "probe": .object([
                "duration": .number(120), "fps": .number(30), "width": .number(5760), "height": .number(2880),
                "hasAudio": .bool(true), "sphere": .object(["projection": .string("equirectangular")])
            ])
        ])
        let raws: [JSONValue] = [
            .object(["asset": .string("ride-360"), "in": .number(0), "out": .number(41.6)]),
            .object(["asset": .string("ride-360"), "in": .number(0), "out": .number(55),
                     "timeline": .object(["track": .string("V3"), "startFrame": .number(300), "durationFrames": .number(1650)])])
        ]
        let nodes = zip(["first", "flagged"], raws).enumerated().map { index, item -> JSONValue in
            let start = index == 0 ? 0 : 300
            let length = index == 0 ? 1248 : 1650
            return .object(["id": .string(item.0), "kind": .string("segment"), "lane": .string("primary"),
                "startFrame": .number(Double(start)), "endFrame": .number(Double(start + length)),
                "frames": .number(Double(length)), "raw": item.1, "assetId": .string("ride-360"),
                "assetKind": .string("video"), "sourceIn": .number(0), "sourceOut": .number(index == 0 ? 41.6 : 55),
                "track": .string(index == 0 ? "V1" : "V3"),
                "playbackStartFrame": .number(Double(start)), "playbackEndFrame": .number(Double(start + length))])
        }
        let initial = try XCTUnwrap(EditorSnapshot.decode(.object([
            "documentId": .string("full-window"), "revisions": .object([
                "document": .string("initial"), "library": .string("library"), "renderer": .string("renderer")]),
            "plan": .object(["slug": .string("full-window"), "segments": .array(raws),
                "timeline": .object(["version": .number(1), "fps": .number(30), "durationFrames": .number(1950)])]),
            "nodes": .array(nodes), "fps": .number(30), "durationFrames": .number(1950), "warnings": .array([]),
            "preview": manifest, "projectConfig": try XCTUnwrap(fixture["config"])
        ])))
        let store = EditorStore()
        store.snapshot = initial
        store.authoritativeSnapshot = initial
        store.openPlanPath = "plans/full-window.json"
        store.library = [asset]
        store.selectedAsset = asset
        store.selectedAssetID = "ride-360"
        store.sourceLayouts["ride-360"] = "front-back"
        store.sourceStates["ride-360"] = "ready"
        store.timelineSourceURLs["ride-360"] = try XCTUnwrap(URL(string: XCTUnwrap(fixture["sourceURL"]?.stringValue)))
        store.inspectorSchema = try XCTUnwrap(fixture["schema"])
        store.previewManifest = manifest
        store.workspaceInfo = .object(["previewOrigin": try XCTUnwrap(fixture["origin"]),
                                       "config": try XCTUnwrap(fixture["config"])])
        store.selectedIDs = ["flagged"]
        var calls: [String] = []
        var snapshotResponse = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(initial))
        var acknowledge: CheckedContinuation<JSONValue, Error>?
        store.service.callStub = { method, _ in
            calls.append(method)
            if method == "document.apply" {
                return try await withCheckedThrowingContinuation { acknowledge = $0 }
            }
            if method == "inspector.schema" { return try XCTUnwrap(fixture["schema"]) }
            if method == "document.snapshot" { return snapshotResponse }
            return .object([:])
        }
        let host = NSHostingView(rootView: ContentView(store: store))
        let window = FullWindowTestWindow(contentRect: NSRect(x: 0, y: 0, width: 1920, height: 1080),
                                          styleMask: .titled, backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        defer { window.close() }
        host.layoutSubtreeIfNeeded()
        try await eventually("Both monitors and the timeline must exist in the full window") {
            let views = self.descendants(host)
            return views.contains { $0 is TimelineCanvas } && views.contains { $0 is WKWebView }
                && views.contains { ($0 as? AVPlayerView)?.player?.currentItem?.status == .readyToPlay }
        }
        let canvas = try XCTUnwrap(descendants(host).compactMap { $0 as? TimelineCanvas }.first)
        let web = try XCTUnwrap(descendants(host).compactMap { $0 as? WKWebView }.first)
        let sourcePlayer = try XCTUnwrap(descendants(host).compactMap { $0 as? AVPlayerView }.first?.player)
        sourcePlayer.isMuted = true
        // Confirm the real Remotion page, not an uninitialised WKWebView or a stand-in HTML page.
        var videoReady = false
        for _ in 0..<200 where !videoReady {
            videoReady = (try? await web.evaluateJavaScript("document.querySelector('video')?.readyState >= 2")) as? Bool == true
            if !videoReady { try await Task.sleep(for: .milliseconds(25)) }
        }
        XCTAssertTrue(videoReady, "Interactive Remotion must decode own footage before measuring")
        XCTAssertEqual(store.currentPreviewMode, "interactive")
        XCTAssertNil(store.errorMessage)
        XCTAssertFalse(window.isVisible)
        XCTAssertFalse(window.isKeyWindow)
        XCTAssertFalse(NSApp.isActive)
        XCTAssertEqual(NSApp.activationPolicy(), .prohibited)
        // Bound the non-layout suspects separately, outside input-to-redraw. These use
        // the actual loaded bridge/manifest and full snapshot, not reduced model values.
        let bridge = try XCTUnwrap(web.navigationDelegate as? ProgramWebView.Coordinator)
        let bridgeStart = ProcessInfo.processInfo.systemUptime
        for _ in 0..<100 {
            bridge.load(store.previewURL, manifest: store.previewManifest, revision: store.previewRevision,
                        frame: store.playheadFrame, rate: store.playbackRate,
                        origin: try XCTUnwrap(store.previewOrigin))
        }
        let bridgeMS = (ProcessInfo.processInfo.systemUptime - bridgeStart) * 10
        let predicted = OptimisticTimeline.apply(
            OptimisticTimeline.remove(ids: ["flagged"], ripple: false, positioned: true), to: initial)
        let equalityStart = ProcessInfo.processInfo.systemUptime
        var equalCount = 0
        for candidate in [initial, predicted] {
            for _ in 0..<1000 { if initial == candidate { equalCount += 1 } }
        }
        let equalityMS = (ProcessInfo.processInfo.systemUptime - equalityStart) / 2
        XCTAssertEqual(equalCount, 1000)
        let hashStart = ProcessInfo.processInfo.systemUptime
        var hash = 0
        for _ in 0..<100 { hash ^= initial.hashValue }
        let hashMS = (ProcessInfo.processInfo.systemUptime - hashStart) * 10
        XCTAssertEqual(hash, 0)
        print("FULL-WINDOW-SUSPECTS unchanged-bridge-load=\(bridgeMS)ms snapshot-equality=\(equalityMS)ms snapshot-hash=\(hashMS)ms per-call")

        let trace = HostingWorkTrace()
        trace.names[ObjectIdentifier(host)] = "ContentView"
        trace.names[ObjectIdentifier(canvas)] = "TimelineCanvas"
        for split in descendants(host).compactMap({ $0 as? NSSplitView }) {
            for (index, pane) in split.subviews.enumerated() {
                trace.names[ObjectIdentifier(pane)] = "\(split.autosaveName ?? "split")[\(index)]"
            }
        }
        trace.install(descendants(host))
        defer { trace.restore() }
        var inputTime: Double?
        var redrawTime: Double?
        var inspectorTime: Double?
        var handlerTime = 0.0
        var milliseconds = 0.0
        var turnMilliseconds = 0.0
        var redrawSamples: [Double] = []
        var inspectorSamples: [Double] = []
        let deleteKey = try XCTUnwrap(NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
            windowNumber: window.windowNumber, context: nil, characters: "\u{7f}",
            charactersIgnoringModifiers: "\u{7f}", isARepeat: false, keyCode: 51))
        let signposter = OSSignposter(subsystem: "com.cycling.ReelsEditorTests", category: "FullWindowDelete")
        var interval: OSSignpostIntervalState?
        var inspectorInterval: OSSignpostIntervalState?
        trace.didLayout = { name, end in
            guard name == "ReelsEditor.MonitorsInspector[1]",
                  inputTime != nil, inspectorTime == nil,
                  store.inspector.snapshot?.nodes.contains(where: { $0.id == "flagged" }) == false,
                  store.inspector.selectedNode == nil else { return }
            inspectorTime = end
            if let active = inspectorInterval {
                signposter.endInterval("InputToInspector", active)
                inspectorInterval = nil
            }
        }
        let observation = canvas.observe(\.needsDisplay, options: [.new]) { _, _ in
            MainActor.assumeIsolated {
                var mirror: Mirror? = Mirror(reflecting: canvas)
                var displayed: EditorSnapshot?
                while let current = mirror {
                    if let value = current.children.first(where: { $0.label == "snapshot" }) {
                        displayed = value.value as? EditorSnapshot
                        break
                    }
                    mirror = current.superclassMirror
                }
                if inputTime != nil, redrawTime == nil,
                   displayed?.nodes.contains(where: { $0.id == "flagged" }) == false {
                    redrawTime = ProcessInfo.processInfo.systemUptime
                    if let active = interval {
                        signposter.endInterval("InputToRedraw", active)
                        interval = nil
                    }
                }
            }
        }
        defer { observation.invalidate() }
        let options = XCTMeasureOptions()
        options.iterationCount = 1
        options.invocationOptions = [.manuallyStart, .manuallyStop]
        // Layout/decoding settles before the timed input. No forced layout is used to make delete fast.
        try await Task.sleep(for: .milliseconds(250))
        measure(metrics: [XCTClockMetric(), XCTOSSignpostMetric(
            subsystem: "com.cycling.ReelsEditorTests", category: "FullWindowDelete", name: "InputToRedraw"),
            XCTOSSignpostMetric(subsystem: "com.cycling.ReelsEditorTests", category: "FullWindowDelete",
                                name: "InputToInspector")], options: options) {
            inputTime = nil
            redrawTime = nil
            inspectorTime = nil
            store.snapshot = initial
            store.authoritativeSnapshot = initial
            store.selectedIDs = ["flagged"]
            self.pump { store.inspector.selectedNode?.id == "flagged" }
            XCTAssertEqual(store.inspector.selectedNode?.id, "flagged")
            host.layoutSubtreeIfNeeded()
            self.pump { !host.needsLayout }
            inspectorTime = nil
            calls.removeAll()
            trace.spans.removeAll(keepingCapacity: true)
            trace.enabled = true
            self.startMeasuring()
            interval = signposter.beginInterval("InputToRedraw")
            inspectorInterval = signposter.beginInterval("InputToInspector")
            inputTime = ProcessInfo.processInfo.systemUptime
            canvas.keyDown(with: deleteKey)
            handlerTime = (ProcessInfo.processInfo.systemUptime - inputTime!) * 1000
            XCTAssertFalse(store.snapshot?.nodes.contains { $0.id == "flagged" } ?? true)
            XCTAssertEqual(store.inspector.selectedNode?.id, "flagged", "Inspector must not switch on the input turn")
            self.pump { redrawTime != nil && inspectorTime != nil }
            turnMilliseconds = (ProcessInfo.processInfo.systemUptime - inputTime!) * 1000
            if let active = interval {
                signposter.endInterval("InputToRedraw", active)
                interval = nil
            }
            if let active = inspectorInterval {
                signposter.endInterval("InputToInspector", active)
                inspectorInterval = nil
            }
            self.stopMeasuring()
            trace.enabled = false
            XCTAssertNotNil(redrawTime, "Full-window prediction must request a canvas redraw before apply acknowledgement")
            XCTAssertNotNil(inspectorTime, "Inspector must eventually finish its deferred reel-properties layout")
            if let redrawTime, let inspectorTime { XCTAssertLessThan(redrawTime, inspectorTime) }
            milliseconds = ((redrawTime ?? ProcessInfo.processInfo.systemUptime) - inputTime!) * 1000
            redrawSamples.append(milliseconds)
            let inspectorMS = ((inspectorTime ?? ProcessInfo.processInfo.systemUptime) - inputTime!) * 1000
            inspectorSamples.append(inspectorMS)
            trace.report(until: redrawTime ?? .infinity)
            print("FULL-WINDOW-SAMPLE \(redrawSamples.count) handler=\(handlerTime)ms redraw=\(milliseconds)ms inspector=\(inspectorMS)ms turn=\(turnMilliseconds)ms")
        }
        print("FULL-WINDOW-INSPECTOR layout-complete-samples=\(inspectorSamples)ms")
        let baseline = ProcessInfo.processInfo.environment["REELS_LATENCY_BASELINE"] == "1"
        print("FULL-WINDOW-DELETE \(baseline ? "baseline" : "fixed") input-handler=\(handlerTime)ms input-to-redraw=\(milliseconds)ms input-to-turn-complete=\(turnMilliseconds)ms calls=\(calls)")
        #if !DEBUG
        if !baseline { XCTAssertLessThan(redrawSamples.max() ?? .infinity, 50, "Release full-window input-to-redraw request budget, including XCTest's warm-up") }
        #endif
        XCTAssertFalse(calls.contains("preview.prepare"), "A timeline prediction must not synchronously prepare preview")
        XCTAssertFalse(calls.contains("selection.set"))
        XCTAssertTrue(store.authoritativeSnapshot == initial, "No backend acknowledgement before redraw")
        XCTAssertTrue(descendants(host).contains { $0 === web }, "Do not reconstruct WebKit on a timeline prediction")
        XCTAssertTrue(descendants(host).compactMap { $0 as? AVPlayerView }.contains { $0.player === sourcePlayer },
                      "Unchanged Source must keep its AVPlayer")
        // Let the serial queue receive the request; its reply remains held until after the metric.
        defer {
            acknowledge?.resume(throwing: CancellationError())
        }
        var saved = try XCTUnwrap(store.snapshot)
        saved.revisions.document = "saved"
        saved.preview = manifest.setting(.string("saved"), at: ["revision"])
        saved.preview = saved.preview.setting(.array([try XCTUnwrap(manifest["props"]?["segments"]?.arrayValue?.first)]),
                                              at: ["props", "segments"])
        snapshotResponse = try JSONDecoder().decode(JSONValue.self, from: JSONEncoder().encode(saved))
        // XCTest invokes iterationCount + 1 times (the first is its warm-up).
        // Its synchronous measure block cannot run the MainActor persistence Tasks;
        // drain every queued reply here, after every sample has requested redraw.
        for _ in redrawSamples {
            try await eventually("Apply reaches the serial persistence queue") { acknowledge != nil }
            let reply = acknowledge
            acknowledge = nil
            reply?.resume(returning: snapshotResponse)
            await Task.yield()
        }
        await store.editTail?.value
        XCTAssertTrue(store.pendingPredictions.isEmpty)
        XCTAssertEqual(store.snapshot?.revisions.document, "saved")
        XCTAssertNil(store.errorMessage)
        // Observe the real postMessage envelope after authoritative preview publication.
        // Internal WKWebView inspection, not system input or a browser/AX client.
        _ = try await web.evaluateJavaScript("""
            window.__deleteFixtureLoad = null;
            window.addEventListener('message', event => {
              if (event.data?.command?.type === 'loadRevision') window.__deleteFixtureLoad = event.data.command;
            });
            """)
        store.service.onEvent?(.object([
            "event": .string("preview.ready"),
            "payload": .object(["manifest": saved.preview])
        ]))
        var previewRevision: String?
        for _ in 0..<200 where previewRevision != "saved" {
            previewRevision = try await web.evaluateJavaScript("window.__deleteFixtureLoad?.revision ?? null") as? String
            if previewRevision != "saved" { try await Task.sleep(for: .milliseconds(10)) }
        }
        XCTAssertEqual(previewRevision, "saved")
        let displayedClips = try await web.evaluateJavaScript("window.__deleteFixtureLoad?.props.segments.length ?? -1") as? Int
        XCTAssertEqual(displayedClips, 1, "Program receives reconciled props, not the stale two-clip prediction")
        XCTAssertTrue(descendants(host).contains { $0 === web })
        XCTAssertNil(store.errorMessage)
        XCTAssertFalse(window.isVisible)
        XCTAssertFalse(NSApp.isActive)
    }
}
