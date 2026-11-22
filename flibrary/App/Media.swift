import AVFoundation
import AppKit
import ImageIO

/// CGImage is immutable; this box lets decoded images cross actor boundaries.
struct ImageBox: @unchecked Sendable { let image: CGImage }

final class ImageCache: @unchecked Sendable {
    static let shared = ImageCache()
    private let cache: NSCache<NSString, CGImageObject> = {
        let c = NSCache<NSString, CGImageObject>(); c.totalCostLimit = 256 * 1024 * 1024; return c
    }()
    final class CGImageObject { let image: CGImage; init(_ i: CGImage) { image = i } }
    func get(_ key: String) -> CGImage? { cache.object(forKey: key as NSString)?.image }
    func set(_ key: String, _ image: CGImage) {
        cache.setObject(CGImageObject(image), forKey: key as NSString, cost: image.bytesPerRow * image.height)
    }
}

enum Media {
    /// Downsampled image from disk (thumbnails, sheets, stills), cached, decoded off the main thread.
    static func image(path: String, maxPixel: Int) async -> CGImage? {
        let key = "\(path)@\(maxPixel)"
        if let hit = ImageCache.shared.get(key) { return hit }
        let box: ImageBox? = await Task.detached(priority: .utility) {
            guard let src = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil) else { return nil }
            let opts: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                         kCGImageSourceCreateThumbnailWithTransform: true,
                                         kCGImageSourceThumbnailMaxPixelSize: maxPixel]
            return CGImageSourceCreateThumbnailAtIndex(src, 0, opts as CFDictionary).map(ImageBox.init)
        }.value
        if let box { ImageCache.shared.set(key, box.image) }
        return box?.image
    }

    /// Thumbnail for a grid cell: backend thumb, the photo itself, the first contact-sheet tile, else a video frame.
    static func thumbnail(for a: Asset, maxPixel: Int = 480) async -> CGImage? {
        if let t = a.thumb, let img = await image(path: t, maxPixel: maxPixel) { return img }
        if a.isPhoto { return await image(path: a.media, maxPixel: maxPixel) }
        if let tile = await sheetFrame(for: a, fraction: 0) { return tile }
        return a.is360 ? nil : await scrubFrame(for: a, fraction: 0, maxPixel: maxPixel)
    }

    /// Pixel-size buckets so zooming reuses cached images instead of re-decoding at every slider step.
    static let pixelBuckets = [240, 480, 720, 960, 1440]
    static func pixelBucket(forTileWidth w: Double, scale: Double = 2) -> Int {
        let need = w * scale
        return pixelBuckets.first { Double($0) >= need } ?? pixelBuckets.last!
    }

    /// Where tile `index` sits: which sheet and the pixel rect inside it (`scale` = loaded width / sheetWidth).
    static func sheetTile(grid: SheetGrid, sheetCount: Int, fraction: Double, imageSize: CGSize) -> (sheet: Int, rect: CGRect)? {
        let perSheet = grid.perSheet ?? grid.cols * grid.rows
        guard perSheet > 0, sheetCount > 0 else { return nil }
        let total = grid.times.isEmpty ? perSheet * sheetCount : grid.times.count
        let index = min(total - 1, max(0, Int(fraction * Double(total))))
        let sheet = index / perSheet
        guard sheet < sheetCount else { return nil }
        let slot = index % perSheet, col = slot % grid.cols, row = slot / grid.cols
        if let tw = grid.tileW, let th = grid.tileH {
            let g = grid.gutter ?? 0, h = grid.header ?? 0
            let k = grid.sheetWidth.map { imageSize.width / CGFloat($0) } ?? 1
            return (sheet, CGRect(x: (g + Double(col) * (tw + g)) * k, y: (h + Double(row) * (th + g)) * k, width: tw * k, height: th * k).integral)
        }
        let w = imageSize.width / CGFloat(grid.cols), h = imageSize.height / CGFloat(grid.rows)
        return (sheet, CGRect(x: CGFloat(col) * w, y: CGFloat(row) * h, width: w, height: h).integral)
    }

    /// Crops the contact-sheet tile for `fraction` of the clip.
    static func sheetFrame(for a: Asset, fraction: Double) async -> CGImage? {
        guard let grid = a.sheetGrid, let sheets = a.sheets, !sheets.isEmpty else { return nil }
        let perSheet = grid.perSheet ?? grid.cols * grid.rows
        let total = grid.times.isEmpty ? perSheet * sheets.count : grid.times.count
        let index = min(total - 1, max(0, Int(fraction * Double(total))))
        let sheetIndex = index / max(1, perSheet)
        guard sheetIndex < sheets.count, let sheet = await image(path: sheets[sheetIndex], maxPixel: 4096),
              let tile = sheetTile(grid: grid, sheetCount: sheets.count, fraction: fraction,
                                   imageSize: CGSize(width: sheet.width, height: sheet.height))
        else { return nil }
        return sheet.cropping(to: tile.rect)
    }

    /// Hover-scrub frame. Flat video: AVAssetImageGenerator; 360: crop a contact-sheet tile.
    static func scrubFrame(for a: Asset, fraction: Double, maxPixel: Int = 360) async -> CGImage? {
        if a.is360 { return await sheetFrame(for: a, fraction: fraction) }
        guard a.kind == "video", let duration = a.duration, duration > 0 else { return nil }
        let step = min(39, Int(fraction * 40))
        let key = "scrub:\(a.id)#\(step)@\(maxPixel)"
        if let hit = ImageCache.shared.get(key) { return hit }
        let box = await Scrubber.shared.frame(url: a.mediaURL, id: a.id, at: duration * (Double(step) + 0.5) / 40, maxPixel: maxPixel)
        if let box { ImageCache.shared.set(key, box.image) }
        return box?.image
    }
}

/// Keeps a few AVAssetImageGenerators alive so repeated hovers on a clip stay quick.
actor Scrubber {
    static let shared = Scrubber()
    private var generators: [(id: String, gen: AVAssetImageGenerator)] = []

    func frame(url: URL, id: String, at seconds: Double, maxPixel: Int = 360) async -> ImageBox? {
        let gen: AVAssetImageGenerator
        if let existing = generators.first(where: { $0.id == id }) { gen = existing.gen } else {
            gen = AVAssetImageGenerator(asset: AVURLAsset(url: url))
            gen.appliesPreferredTrackTransform = true
            gen.requestedTimeToleranceBefore = CMTime(seconds: 1, preferredTimescale: 600)
            gen.requestedTimeToleranceAfter = CMTime(seconds: 1, preferredTimescale: 600)
            generators.append((id, gen))
            if generators.count > 6 { generators.removeFirst() }
        }
        gen.maximumSize = CGSize(width: maxPixel, height: maxPixel)
        guard !Task.isCancelled else { return nil }
        return (try? gen.copyCGImage(at: CMTime(seconds: seconds, preferredTimescale: 600), actualTime: nil))?.asBox
    }
}

private extension CGImage { var asBox: ImageBox { ImageBox(image: self) } }
