// reels-vision: looks at the user's own (already staged) footage with Apple's on-device Vision
// framework. Nothing is generated and nothing leaves the Mac.
//
//   reels-vision hands <video>             hand landmarks per frame, JSON on stdout
//   reels-vision matte <video> <out.gray>  person mask per frame as raw 8-bit grey frames in
//                                          <out.gray>; {"width","height","frames"} on stdout
//
// Built on demand by scripts/reels/vision.py (swiftc). Coordinates are 0..1 from the top-left.

import AVFoundation
import CoreVideo
import Foundation
import Vision

func fail(_ msg: String) -> Never {
    FileHandle.standardError.write((msg + "\n").data(using: .utf8)!)
    exit(1)
}

func eachFrame(_ path: String, _ body: (Int, CVPixelBuffer) throws -> Void) async throws -> (Int, Int, Int) {
    let asset = AVURLAsset(url: URL(fileURLWithPath: path))
    guard let track = try await asset.loadTracks(withMediaType: .video).first else { fail("no video track in \(path)") }
    let size = try await track.load(.naturalSize)
    let reader = try AVAssetReader(asset: asset)
    let out = AVAssetReaderTrackOutput(
        track: track, outputSettings: [kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA])
    out.alwaysCopiesSampleData = false
    reader.add(out)
    guard reader.startReading() else { fail("can't read \(path): \(reader.error?.localizedDescription ?? "unknown error")") }
    var i = 0
    while let sample = out.copyNextSampleBuffer() {
        if let pb = CMSampleBufferGetImageBuffer(sample) {
            try autoreleasepool { try body(i, pb) }
            i += 1
        }
    }
    if reader.status == .failed { fail("reading \(path) failed: \(reader.error?.localizedDescription ?? "unknown error")") }
    return (Int(size.width), Int(size.height), i)
}

let joints: [(String, VNHumanHandPoseObservation.JointName)] = [
    ("tip", .indexTip), ("dip", .indexDIP), ("pip", .indexPIP), ("mcp", .indexMCP), ("wrist", .wrist),
    ("middleTip", .middleTip), ("middlePip", .middlePIP), ("ringTip", .ringTip), ("ringPip", .ringPIP),
    ("littleTip", .littleTip), ("littlePip", .littlePIP), ("thumbTip", .thumbTip),
]

func hands(_ path: String) async throws {
    var rows: [[String: Any]] = []
    let req = VNDetectHumanHandPoseRequest()
    req.maximumHandCount = 2
    let (w, h, n) = try await eachFrame(path) { i, pb in
        try VNImageRequestHandler(cvPixelBuffer: pb, options: [:]).perform([req])
        var found: [[String: Any]] = []
        for obs in req.results ?? [] {
            let pts = try obs.recognizedPoints(.all)
            var hand: [String: Any] = ["side": obs.chirality == .left ? "left" : obs.chirality == .right ? "right" : "unknown"]
            for (name, joint) in joints {
                if let p = pts[joint] {
                    // Vision's origin is bottom-left
                    hand[name] = [Double(p.location.x), 1 - Double(p.location.y), Double(p.confidence)]
                }
            }
            found.append(hand)
        }
        rows.append(["i": i, "hands": found])
    }
    let data = try JSONSerialization.data(withJSONObject: ["width": w, "height": h, "frames": n, "rows": rows])
    FileHandle.standardOutput.write(data)
}

func matte(_ path: String, _ outPath: String) async throws {
    FileManager.default.createFile(atPath: outPath, contents: nil)
    guard let out = FileHandle(forWritingAtPath: outPath) else { fail("can't write \(outPath)") }
    let req = VNGeneratePersonSegmentationRequest()
    req.qualityLevel = .accurate
    req.outputPixelFormat = kCVPixelFormatType_OneComponent8
    let seq = VNSequenceRequestHandler()  // one handler for the whole clip: the mask is smoothed over time
    var mw = 0
    var mh = 0
    let (_, _, n) = try await eachFrame(path) { _, pb in
        try seq.perform([req], on: pb)
        guard let mask = req.results?.first?.pixelBuffer else { fail("no mask returned") }
        CVPixelBufferLockBaseAddress(mask, .readOnly)
        defer { CVPixelBufferUnlockBaseAddress(mask, .readOnly) }
        let w = CVPixelBufferGetWidth(mask)
        let h = CVPixelBufferGetHeight(mask)
        if mw == 0 {
            mw = w
            mh = h
        } else if w != mw || h != mh {
            fail("mask size changed mid-clip")
        }
        let stride = CVPixelBufferGetBytesPerRow(mask)
        guard let base = CVPixelBufferGetBaseAddress(mask) else { fail("mask has no pixels") }
        var frame = Data(capacity: w * h)
        for y in 0..<h {
            frame.append(base.advanced(by: y * stride).assumingMemoryBound(to: UInt8.self), count: w)
        }
        out.write(frame)
    }
    try out.close()
    let data = try JSONSerialization.data(withJSONObject: ["width": mw, "height": mh, "frames": n])
    FileHandle.standardOutput.write(data)
}

let args = CommandLine.arguments
do {
    if args.count == 3 && args[1] == "hands" {
        try await hands(args[2])
    } else if args.count == 4 && args[1] == "matte" {
        try await matte(args[2], args[3])
    } else {
        fail("usage: reels-vision hands <video> | reels-vision matte <video> <out.gray>")
    }
} catch {
    fail("reels-vision: \(error.localizedDescription)")
}
