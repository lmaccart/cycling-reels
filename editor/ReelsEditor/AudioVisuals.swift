import AppKit
import SwiftUI

extension Notification.Name {
    /// Posted (on the main actor) when an audio visual finished loading; callers redraw.
    static let reelsAudioVisualReady = Notification.Name("ReelsEditor.audioVisualReady")
}

enum AudioVisualMode: String, CaseIterable, Sendable {
    case waveform, spectrogram
}

/// Waveform / spectrogram images for audio clips, produced by the Python service ("audio.visual")
/// and cached by node + source range + mode + bucketed size.
@MainActor
final class AudioVisualCache {
    static let shared = AudioVisualCache()
    static let defaultsKey = "ReelsEditor.audioVisualMode"
    private static let maxConcurrent = 3

    private weak var service: EditorService?
    private var images: [String: NSImage] = [:]
    /// Latest image per node+mode (any size), shown stretched while the exact size loads.
    private var fallback: [String: NSImage] = [:]
    private var inFlight: Set<String> = []
    private var failed: Set<String> = []
    private var queue: [(key: String, group: String, params: JSONValue)] = []
    private var running = 0

    var mode: AudioVisualMode {
        get { AudioVisualMode(rawValue: UserDefaults.standard.string(forKey: Self.defaultsKey) ?? "") ?? .waveform }
        set {
            guard newValue != mode else { return }
            UserDefaults.standard.set(newValue.rawValue, forKey: Self.defaultsKey)
            NotificationCenter.default.post(name: .reelsAudioVisualReady, object: nil)
        }
    }

    func configure(service: EditorService) { self.service = service }

    /// Drops everything cached (e.g. after the document changes drastically).
    func reset() {
        images.removeAll(); fallback.removeAll(); failed.removeAll(); queue.removeAll()
    }

    /// The image for an audio clip at roughly `pixelSize`, or nil while it loads (a
    /// `.reelsAudioVisualReady` notification follows). Non-audio nodes return nil.
    func image(for node: EditorNode, snapshot: EditorSnapshot, mode: AudioVisualMode, pixelSize: CGSize) -> NSImage? {
        guard pixelSize.width >= 4, pixelSize.height >= 4 else { return nil }
        let width = min(4000, Int((pixelSize.width / 64).rounded(.up)) * 64)
        let height = min(400, max(8, Int((pixelSize.height / 8).rounded(.up)) * 8))
        let group = "\(node.id)|\(mode.rawValue)"
        var range = "\(node.startFrame)-\(node.endFrame)|\(node.sourceIn ?? -1)-\(node.sourceOut ?? -1)"
        if node.kind.contains("music") {
            range += "|" + String(describing: snapshot.plan["music"]?.jsonData().map { String(decoding: $0, as: UTF8.self) } ?? "")
        }
        let key = "\(group)|\(range)|\(width)x\(mode == .spectrogram ? height : 0)"
        if let image = images[key] { return image }
        if !failed.contains(key), !inFlight.contains(key), !queue.contains(where: { $0.key == key }) {
            let params: JSONValue = .object([
                "nodeId": .string(node.id), "mode": .string(mode.rawValue),
                "width": .number(Double(width)), "height": .number(Double(height)),
            ])
            queue.removeAll { $0.group == group }  // an older size of the same clip is obsolete
            queue.append((key, group, params))
            pump()
        }
        return fallback[group]
    }

    private func pump() {
        while running < Self.maxConcurrent, !queue.isEmpty, let service {
            let job = queue.removeFirst()
            running += 1
            inFlight.insert(job.key)
            Task { [weak self] in
                var image: NSImage?
                if let value = try? await service.call("audio.visual", params: job.params) {
                    image = Self.makeImage(from: value)
                }
                self?.finish(job.key, group: job.group, image: image)
            }
        }
    }

    private func finish(_ key: String, group: String, image: NSImage?) {
        running -= 1
        inFlight.remove(key)
        if let image {
            images[key] = image
            fallback[group] = image
            if images.count > 600 { images = images.filter { $0.key == key } }
            NotificationCenter.default.post(name: .reelsAudioVisualReady, object: nil)
        } else {
            failed.insert(key)
        }
        pump()
    }

    // MARK: drawing

    private static func makeImage(from value: JSONValue) -> NSImage? {
        if value["mode"]?.stringValue == "spectrogram" {
            guard let path = value["path"]?.stringValue, let source = NSImage(contentsOfFile: path) else { return nil }
            let size = source.size
            return NSImage(size: size, flipped: false) { rect in
                source.draw(in: rect, from: .zero, operation: .sourceOver, fraction: 0.8)
                return true
            }
        }
        guard let peaks = value["peaks"]?.arrayValue else { return nil }
        let pairs: [(Float, Float)] = peaks.map { pair in
            let items = pair.arrayValue ?? []
            return (Float(items.first?.numberValue ?? 0), Float(items.dropFirst().first?.numberValue ?? 0))
        }
        guard !pairs.isEmpty else { return nil }
        let loudest = max(0.02, pairs.map { max(abs($0.0), abs($0.1)) }.max() ?? 1)
        let gain = min(4, 0.95 / loudest)
        return NSImage(size: NSSize(width: pairs.count, height: 64), flipped: false) { rect in
            let mid = rect.midY
            let half = rect.height / 2
            NSColor(calibratedRed: 0.86, green: 1.0, blue: 0.9, alpha: 0.72).setFill()
            for (index, pair) in pairs.enumerated() {
                let top = CGFloat(min(1, max(0, pair.1) * gain)) * half
                let bottom = CGFloat(min(1, max(0, -pair.0) * gain)) * half
                let h = max(1, top + bottom)
                NSRect(x: CGFloat(index), y: mid - bottom, width: 1, height: h).fill()
            }
            return true
        }
    }
}
