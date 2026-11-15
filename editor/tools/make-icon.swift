// Draws the Reels Editor app icon into an AppIcon.appiconset. Run: swift tools/make-icon.swift <output .appiconset dir>
import AppKit
import CoreGraphics

func draw(size: Int) -> Data {
    let s = CGFloat(size)
    let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: size, pixelsHigh: size, bitsPerSample: 8,
                               samplesPerPixel: 4, hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB,
                               bytesPerRow: 0, bitsPerPixel: 0)!
    let ctx = NSGraphicsContext(bitmapImageRep: rep)!
    NSGraphicsContext.current = ctx
    let c = ctx.cgContext
    let cs = CGColorSpaceCreateDeviceRGB()

    // macOS icon grid: 824/1024 rounded square with ~22.4% corner radius.
    let body = CGRect(x: s * 0.0977, y: s * 0.0977, width: s * 0.8047, height: s * 0.8047)
    let path = CGPath(roundedRect: body, cornerWidth: body.width * 0.2237, cornerHeight: body.width * 0.2237, transform: nil)
    c.saveGState()
    c.setShadow(offset: CGSize(width: 0, height: -s * 0.012), blur: s * 0.03, color: CGColor(gray: 0, alpha: 0.45))
    c.addPath(path); c.setFillColor(CGColor(gray: 0.1, alpha: 1)); c.fillPath()
    c.restoreGState()

    c.saveGState()
    c.addPath(path); c.clip()
    let bg = CGGradient(colorsSpace: cs, colors: [
        CGColor(red: 0.20, green: 0.23, blue: 0.32, alpha: 1),
        CGColor(red: 0.07, green: 0.08, blue: 0.12, alpha: 1)] as CFArray, locations: [0, 1])!
    c.drawLinearGradient(bg, start: CGPoint(x: body.midX, y: body.maxY), end: CGPoint(x: body.midX, y: body.minY), options: [])

    // Bicycle wheel: rim, spokes, hub, centered slightly low and bleeding off the lower right.
    let center = CGPoint(x: body.midX, y: body.midY)
    let radius = body.width * 0.36
    c.setStrokeColor(CGColor(red: 0.45, green: 0.62, blue: 0.95, alpha: 0.55))
    c.setLineCap(.round)
    c.setLineWidth(body.width * 0.028)
    c.strokeEllipse(in: CGRect(x: center.x - radius, y: center.y - radius, width: radius * 2, height: radius * 2))
    c.setLineWidth(body.width * 0.006)
    c.setStrokeColor(CGColor(red: 0.45, green: 0.62, blue: 0.95, alpha: 0.28))
    for i in 0..<24 {
        let a = CGFloat(i) / 24 * .pi * 2
        c.move(to: CGPoint(x: center.x + cos(a) * radius * 0.12, y: center.y + sin(a) * radius * 0.12))
        c.addLine(to: CGPoint(x: center.x + cos(a) * radius * 0.97, y: center.y + sin(a) * radius * 0.97))
    }
    c.strokePath()

    // Film frame (rounded rectangle) with a play triangle.
    let frame = CGRect(x: center.x - body.width * 0.20, y: center.y - body.width * 0.15, width: body.width * 0.40, height: body.width * 0.30)
    let fp = CGPath(roundedRect: frame, cornerWidth: body.width * 0.05, cornerHeight: body.width * 0.05, transform: nil)
    c.saveGState()
    c.setShadow(offset: CGSize(width: 0, height: -s * 0.01), blur: s * 0.025, color: CGColor(gray: 0, alpha: 0.5))
    c.addPath(fp)
    c.setFillColor(CGColor(red: 0.96, green: 0.97, blue: 1, alpha: 1))
    c.fillPath()
    c.restoreGState()

    // Sprocket holes top and bottom.
    c.setFillColor(CGColor(red: 0.20, green: 0.23, blue: 0.32, alpha: 1))
    let holeW = frame.width * 0.075, holeH = frame.height * 0.07
    for i in 0..<5 {
        let x = frame.minX + frame.width * (0.12 + CGFloat(i) * 0.19)
        for y in [frame.minY + frame.height * 0.08, frame.maxY - frame.height * 0.08 - holeH] {
            c.addPath(CGPath(roundedRect: CGRect(x: x, y: y, width: holeW, height: holeH), cornerWidth: holeH * 0.3, cornerHeight: holeH * 0.3, transform: nil))
            c.fillPath()
        }
    }

    // Play triangle in accent blue.
    let t = frame.height * 0.24
    let tc = CGPoint(x: frame.midX + t * 0.12, y: frame.midY)
    c.move(to: CGPoint(x: tc.x - t * 0.8, y: tc.y + t))
    c.addLine(to: CGPoint(x: tc.x + t * 1.05, y: tc.y))
    c.addLine(to: CGPoint(x: tc.x - t * 0.8, y: tc.y - t))
    c.closePath()
    c.setFillColor(CGColor(red: 0.22, green: 0.47, blue: 0.98, alpha: 1))
    c.fillPath()
    c.restoreGState()

    // Hairline highlight.
    c.addPath(path); c.setStrokeColor(CGColor(gray: 1, alpha: 0.10)); c.setLineWidth(max(1, s * 0.004)); c.strokePath()
    NSGraphicsContext.current = nil
    return rep.representation(using: .png, properties: [:])!
}

let out = URL(fileURLWithPath: CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppIcon.appiconset")
try FileManager.default.createDirectory(at: out, withIntermediateDirectories: true)
var images: [[String: String]] = []
for base in [16, 32, 128, 256, 512] {
    for scale in [1, 2] {
        let px = base * scale
        let name = "icon_\(base)x\(base)\(scale == 2 ? "@2x" : "").png"
        try draw(size: px).write(to: out.appending(path: name))
        images.append(["idiom": "mac", "size": "\(base)x\(base)", "scale": "\(scale)x", "filename": name])
    }
}
let json: [String: Any] = ["images": images, "info": ["version": 1, "author": "xcode"]]
try JSONSerialization.data(withJSONObject: json, options: [.prettyPrinted, .sortedKeys]).write(to: out.appending(path: "Contents.json"))
