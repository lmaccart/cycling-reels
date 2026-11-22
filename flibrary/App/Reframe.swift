import CoreGraphics
import Foundation
import simd

// Reframe data, keyframe edits, and the persisted state. SphereMath (editor/) supplies
// the v360 conventions: yaw right, pitch up, roll about the view axis; fov is the horizontal fov of the output.

struct ReframeKey: Codable, Equatable, Sendable {
    var at: Double, yaw: Double, pitch: Double, roll: Double, fov: Double
    var cut: Bool? = nil
    init(at: Double, view v: SphereView) { self.at = at; yaw = v.yaw; pitch = v.pitch; roll = v.roll; fov = v.fov }
    var view: SphereView { SphereView(yaw: yaw, pitch: pitch, fov: fov, roll: roll) }
}

struct ReframeSettings: Codable, Equatable, Sendable {
    var `in`: Double = 0
    var out: Double = 0
    var width = 1080, height = 1920, fps = 30
    var stabilize = false
    var keys: [ReframeKey] = []

    static let straight = SphereView(yaw: 0, pitch: 0, fov: 90, roll: 0)
    static let behind = SphereView(yaw: 180, pitch: 0, fov: 90, roll: 0)
    static let aspects: [(String, Int, Int)] = [("9:16", 1080, 1920), ("16:9", 1920, 1080), ("1:1", 1080, 1080)]

    /// Adds a key, or replaces one within `tolerance` seconds; keeps `at` strictly increasing.
    mutating func setKey(at: Double, view: SphereView, tolerance: Double = 0.05) {
        let at = max(0, at)
        let k = ReframeKey(at: at, view: SphereView(yaw: SphereMath.wrapYaw(view.yaw), pitch: view.pitch, fov: view.fov, roll: view.roll))
        if let i = keys.firstIndex(where: { abs($0.at - at) <= tolerance }) { keys[i] = ReframeKey(at: keys[i].at, view: k.view) }
        else { keys.append(k); keys.sort { $0.at < $1.at } }
    }

    mutating func deleteKey(near at: Double, tolerance: Double = 0.05) {
        keys.removeAll { abs($0.at - at) <= tolerance }
    }

    /// Camera at `t` seconds after `in`: same eased shortest-arc path as the backend's `sphere.path_value`.
    func view(at t: Double) -> SphereView? {
        keys.isEmpty ? nil : SphereMath.pathValue(keys.map { SphereKey(at: $0.at, view: $0.view, cut: $0.cut ?? false) }, at: t)
    }
}

/// `.cache/flibrary/state.json`: `{"reframe": {<asset id>: settings}}`.
struct ReframeState: Codable, Equatable, Sendable {
    var reframe: [String: ReframeSettings] = [:]

    static func url(_ root: URL) -> URL { root.appending(path: ".cache/flibrary/state.json") }

    static func load(_ root: URL) -> ReframeState {
        (try? JSONDecoder().decode(ReframeState.self, from: Data(contentsOf: url(root)))) ?? ReframeState()
    }

    func save(_ root: URL) {
        let u = Self.url(root)
        try? FileManager.default.createDirectory(at: u.deletingLastPathComponent(), withIntermediateDirectories: true)
        let enc = JSONEncoder(); enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        try? enc.encode(self).write(to: u, options: .atomic)
    }
}

/// Decoded RGBA8 pixels (image comparison for Check frame and tests).
struct EquirectPixels: Sendable {
    let width: Int, height: Int, data: [UInt8]

    init(width: Int, height: Int, data: [UInt8]) { self.width = width; self.height = height; self.data = data }

    init?(_ image: CGImage, maxWidth: Int = 2048) {
        let w = min(image.width, maxWidth), h = max(1, image.height * w / max(1, image.width))
        var buf = [UInt8](repeating: 0, count: w * h * 4)
        let ok = buf.withUnsafeMutableBytes { p -> Bool in
            guard let ctx = CGContext(data: p.baseAddress, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
                                      space: CGColorSpace(name: CGColorSpace.sRGB)!,
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return false }
            ctx.interpolationQuality = .high
            ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
            return true
        }
        guard ok else { return nil }
        width = w; height = h; data = buf
    }
}

/// Image comparison helpers (the live preview itself is drawn on the GPU, see Viewer.swift).
enum FlatRenderer {
    /// Mean absolute RGB difference (0...255) after scaling `b` to `a`'s size; nil if either cannot be decoded.
    static func meanAbsDiff(_ a: CGImage, _ b: CGImage) -> Double? {
        guard let pa = EquirectPixels(a, maxWidth: a.width), let pb = EquirectPixels(b, maxWidth: a.width),
              pa.width == pb.width, pa.height == pb.height else { return nil }
        var sum = 0
        for i in stride(from: 0, to: pa.data.count, by: 4) { for c in 0..<3 { sum += abs(Int(pa.data[i + c]) - Int(pb.data[i + c])) } }
        return Double(sum) / Double(pa.width * pa.height * 3)
    }
}
