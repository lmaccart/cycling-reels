// Draws the flibrary app icon into an AppIcon.appiconset. Run: swift tools/make-icon.swift App/Assets.xcassets/AppIcon.appiconset
// A grid of film-frame thumbnails ringed by a tilted 360 orbit.
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

    let body = CGRect(x: s * 0.0977, y: s * 0.0977, width: s * 0.8047, height: s * 0.8047)
    let path = CGPath(roundedRect: body, cornerWidth: body.width * 0.2237, cornerHeight: body.width * 0.2237, transform: nil)
    c.saveGState()
    c.setShadow(offset: CGSize(width: 0, height: -s * 0.012), blur: s * 0.03, color: CGColor(gray: 0, alpha: 0.45))
    c.addPath(path); c.setFillColor(CGColor(gray: 0.1, alpha: 1)); c.fillPath()
    c.restoreGState()

    c.saveGState()
    c.addPath(path); c.clip()
    // Charcoal-teal background (editor is blue; flibrary leans teal).
    let bg = CGGradient(colorsSpace: cs, colors: [
        CGColor(red: 0.10, green: 0.25, blue: 0.30, alpha: 1),
        CGColor(red: 0.05, green: 0.09, blue: 0.12, alpha: 1)] as CFArray, locations: [0, 1])!
    c.drawLinearGradient(bg, start: CGPoint(x: body.midX, y: body.maxY), end: CGPoint(x: body.midX, y: body.minY), options: [])

    let u = body.width
    let center = CGPoint(x: body.midX, y: body.midY)

    // Back half of the orbit ring (behind the grid).
    func orbit(front: Bool) {
        c.saveGState()
        c.translateBy(x: center.x, y: center.y)
        c.rotate(by: -0.38)
        let rx = u * 0.43, ry = u * 0.15
        c.setLineWidth(u * 0.026); c.setLineCap(.round)
        c.setStrokeColor(CGColor(red: 0.30, green: 0.85, blue: 0.80, alpha: front ? 1 : 0.4))
        let start: CGFloat = front ? .pi : 0, end: CGFloat = front ? .pi * 2 : .pi
        var first = true
        for i in 0...90 {
            let a = start + (end - start) * CGFloat(i) / 90
            let p = CGPoint(x: cos(a) * rx, y: sin(a) * ry)
            if first { c.move(to: p); first = false } else { c.addLine(to: p) }
        }
        c.strokePath()
        c.restoreGState()
    }
    orbit(front: false)

    // 3x3 thumbnail grid, each tile a different warm/cool "frame".
    let cell = u * 0.20, gap = u * 0.028
    let total = cell * 3 + gap * 2
    let tint: [[CGFloat]] = [[0.95,0.62,0.30],[0.45,0.70,0.95],[0.55,0.85,0.60],
                             [0.40,0.55,0.85],[0.96,0.97,1.0],[0.95,0.50,0.55],
                             [0.60,0.80,0.90],[0.90,0.78,0.40],[0.50,0.60,0.95]]
    for r in 0..<3 { for col in 0..<3 {
        let rect = CGRect(x: center.x - total / 2 + CGFloat(col) * (cell + gap),
                          y: center.y - total / 2 + CGFloat(2 - r) * (cell + gap) * 1.0,
                          width: cell, height: cell * 0.72).offsetBy(dx: 0, dy: cell * 0.14)
        let rp = CGPath(roundedRect: rect, cornerWidth: cell * 0.12, cornerHeight: cell * 0.12, transform: nil)
        let t = tint[r * 3 + col]
        c.saveGState()
        c.setShadow(offset: CGSize(width: 0, height: -s * 0.006), blur: s * 0.014, color: CGColor(gray: 0, alpha: 0.5))
        c.addPath(rp); c.setFillColor(CGColor(red: t[0], green: t[1], blue: t[2], alpha: 1)); c.fillPath()
        c.restoreGState()
        c.saveGState(); c.addPath(rp); c.clip()
        let sh = CGGradient(colorsSpace: cs, colors: [CGColor(gray: 1, alpha: 0.28), CGColor(gray: 0, alpha: 0.22)] as CFArray, locations: [0, 1])!
        c.drawLinearGradient(sh, start: CGPoint(x: rect.midX, y: rect.maxY), end: CGPoint(x: rect.midX, y: rect.minY), options: [])
        c.restoreGState()
    } }

    orbit(front: true)
    // Orbit marker dot.
    c.saveGState()
    c.translateBy(x: center.x, y: center.y); c.rotate(by: -0.38)
    let dot = CGPoint(x: u * 0.43 * cos(CGFloat.pi * 1.82), y: u * 0.15 * sin(CGFloat.pi * 1.82))
    c.setFillColor(CGColor(gray: 1, alpha: 1))
    c.fillEllipse(in: CGRect(x: dot.x - u * 0.03, y: dot.y - u * 0.03, width: u * 0.06, height: u * 0.06))
    c.restoreGState()
    c.restoreGState()

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
