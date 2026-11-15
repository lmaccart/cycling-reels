import Foundation

enum PreviewBridgeContract {
    static let channel = "cycling-reel-preview"
    static let messageHandler = "reelsPreview"

    static func loadCommand(manifest: JSONValue, revision: String, startFrame: Int) -> JSONValue? {
        guard let propsValue = manifest["props"],
              case .object(let props) = propsValue,
              let base = manifest["staticBaseUrl"]?.stringValue ?? manifest["staticURL"]?.stringValue,
              let baseURL = URL(string: base),
              let page = manifest["url"]?.stringValue,
              let pageURL = URL(string: page),
              sameOrigin(baseURL, pageURL),
              pathHasTrailingSlash(baseURL) else { return nil }
        return .object([
            "type": .string("loadRevision"),
            "revision": .string(revision),
            "props": .object(props),
            "staticBaseUrl": .string(base),
            "startFrame": .number(Double(max(0, startFrame)))
        ])
    }

    static func envelope(_ command: JSONValue) -> JSONValue {
        .object([
            "channel": .string(channel),
            "command": command
        ])
    }

    static func allowsPage(_ candidate: URL, origin: URL, capability: URL) -> Bool {
        guard sameOrigin(candidate, origin), sameOrigin(capability, origin),
              let next = URLComponents(url: candidate, resolvingAgainstBaseURL: false),
              let expected = URLComponents(url: capability, resolvingAgainstBaseURL: false) else { return false }
        return next.path == expected.path && next.query == expected.query
    }

    static func allowsStaticBase(_ candidate: URL, origin: URL) -> Bool {
        sameOrigin(candidate, origin) && pathHasTrailingSlash(candidate)
    }

    static func sameOrigin(_ lhs: URL, _ rhs: URL) -> Bool {
        guard let left = URLComponents(url: lhs, resolvingAgainstBaseURL: false),
              let right = URLComponents(url: rhs, resolvingAgainstBaseURL: false) else { return false }
        return left.scheme?.lowercased() == right.scheme?.lowercased()
            && left.host?.lowercased() == right.host?.lowercased()
            && left.port == right.port
    }

    static func originString(_ url: URL) -> String? {
        guard let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
              let scheme = components.scheme, let host = components.host else { return nil }
        return "\(scheme)://\(host)\(components.port.map { ":\($0)" } ?? "")"
    }

    static func pathHasTrailingSlash(_ url: URL) -> Bool {
        URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath.hasSuffix("/") == true
    }

    static func eventMatches(revision: String?, loadedRevision: String?, type: String) -> Bool {
        if type == "error" { return revision == nil || revision == loadedRevision }
        return revision != nil && revision == loadedRevision
    }
}

struct PreviewPlaybackProbe {
    private(set) var firstFrame: Int?
    private(set) var lastFrame: Int?
    private(set) var completed = false

    mutating func begin(at frame: Int) {
        firstFrame = frame
        lastFrame = frame
        completed = false
    }

    mutating func observe(_ frame: Int) -> Bool {
        guard let firstFrame, !completed else { return false }
        lastFrame = max(lastFrame ?? firstFrame, frame)
        guard (lastFrame ?? firstFrame) - firstFrame >= 15 else { return false }
        completed = true
        return true
    }
}

@MainActor
enum NativePreviewCapture {
    static var playbackRequested: Bool {
        ProcessInfo.processInfo.arguments.contains("--capture-playback") && destination != nil
    }

    static var destination: URL? {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "--capture-window"), args.indices.contains(index + 1) else { return nil }
        return URL(fileURLWithPath: args[index + 1])
    }

    static func ready(mode: String, revision: String?, probe: PreviewPlaybackProbe? = nil) {
        let evidence: JSONValue = .object([
            "mode": .string(mode),
            "revision": revision.map(JSONValue.string) ?? .null,
            "firstFrame": probe?.firstFrame.map { .number(Double($0)) } ?? .null,
            "lastFrame": probe?.lastFrame.map { .number(Double($0)) } ?? .null,
            "playbackObserved": .bool(probe?.completed == true),
            "subjectiveAudioVerified": .bool(false)
        ])
        if playbackRequested, let destination, let data = evidence.jsonData(pretty: true) {
            do {
                try FileManager.default.createDirectory(
                    at: destination.deletingLastPathComponent(), withIntermediateDirectories: true)
                try data.write(
                    to: destination.deletingPathExtension().appendingPathExtension("playback.json"), options: .atomic)
            } catch {
                NSLog("Reels Editor playback evidence could not be saved: %@", error.localizedDescription)
            }
        }
        NotificationCenter.default.post(name: .reelsEditorProgramReady, object: evidence)
    }
}
