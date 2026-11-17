import CoreMedia
import Foundation
import simd

enum SphereFailure: Error, CustomStringConvertible {
    case invalid(String), unsupported(String), runtime(String)
    var description: String {
        switch self {
        case .invalid(let s): return "invalid input: \(s)"
        case .unsupported(let s): return "unsupported: \(s)"
        case .runtime(let s): return "render failed: \(s)"
        }
    }
}

struct CameraKey: Decodable {
    let at: Double
    let yaw: Double
    let pitch: Double
    let roll: Double
    let fov: Double
    let cut: Bool?
    var key: SphereKey { SphereKey(at: at, view: SphereView(yaw: yaw, pitch: pitch, fov: fov, roll: roll), cut: cut ?? false) }
}

/// Corrections, not raw CORI: A(t) = R(t) S(t)^T, w/x/y/z, on the SOURCE clock.
struct Corrections: Decodable {
    let sourceStart: Double
    let sourceStep: Double
    let quaternions: [[Double]]
}

struct RenderRequest: Decodable {
    let version: Int
    let source: String
    let destination: String
    let sourceProjection: String
    /// MP4 track IDs, not ffprobe indexes or video ordinal numbers.
    let trackIDs: [Int32]
    let sourceIn: Double
    let sourceOut: Double
    let speed: Double
    let width: Int
    let height: Int
    let fps: Int
    let frames: Int
    let projection: String
    /// Already compiled keys; exterior keys preserve cameraWindow's original ease interval.
    let keys: [CameraKey]
    let corrections: Corrections?
    let fullRange: Bool
    let codec: String
    /// Absent on the W7 render contract.
    let preset: String?
    let views: [CameraKey]?
    let maxOutputBytes: Int64?
    /// Opt-in benchmark readback only; files are siblings of the private destination.
    let proofSampleFrames: [Int]?

    var isProxy: Bool { preset == "proxy" }
    var presetName: String { preset ?? "render" }
    var viewWidth: Int { projection == "front-back" ? width / 2 : width }

    var mode: SphereProjection { projection == "tiny-planet" ? .tinyPlanet : .flat }

    func validate() throws {
        guard version == 1 else { throw SphereFailure.invalid("request version must be 1") }
        guard source.hasPrefix("/"), destination.hasPrefix("/"), source != destination,
              URL(fileURLWithPath: source).standardizedFileURL != URL(fileURLWithPath: destination).standardizedFileURL,
              FileManager.default.fileExists(atPath: source), !FileManager.default.fileExists(atPath: destination)
        else { throw SphereFailure.invalid("absolute, distinct source and NEW destination required") }
        guard sourceIn.isFinite, sourceOut.isFinite, sourceIn >= 0, sourceOut > sourceIn,
              speed.isFinite, speed > 0, speed <= 16,
              (2...8192).contains(width), (2...8192).contains(height), width % 2 == 0, height % 2 == 0,
              (1...240).contains(fps), frames > 0, frames <= 10_000_000,
              Double(frames) / Double(fps) <= (sourceOut - sourceIn) / speed + 2
        else { throw SphereFailure.invalid("invalid source interval, speed, even dimensions, fps or frame count") }
        guard ["equirect", "gopro-eac"].contains(sourceProjection),
              trackIDs.count == (sourceProjection == "gopro-eac" ? 2 : 1), Set(trackIDs).count == trackIDs.count,
              (isProxy ? ["equirect", "front-back"] : ["flat", "tiny-planet"]).contains(projection),
              ["h264", "hevc"].contains(codec), ["render", "proxy"].contains(presetName)
        else { throw SphereFailure.invalid("projection, preset, track IDs or encoder") }
        func validCamera(_ k: CameraKey) -> Bool {
            [k.at, k.yaw, k.pitch, k.roll, k.fov].allSatisfy(\.isFinite) &&
                abs(k.pitch) <= 90 && abs(k.roll) <= 180 && (10...150).contains(k.fov)
        }
        guard (isProxy || !keys.isEmpty), keys.allSatisfy(validCamera),
              zip(keys, keys.dropFirst()).allSatisfy({ $0.at < $1.at })
        else { throw SphereFailure.invalid("finite, strictly ordered camera keys with valid angles/FOV required") }
        if isProxy {
            guard speed == 1, fps == 30, corrections == nil, let limit = maxOutputBytes, limit > 0
            else { throw SphereFailure.invalid("proxy requires speed=1, fps=30, no corrections and positive maxOutputBytes") }
            if projection == "equirect" {
                guard width == 2048, height == 1024, sourceOut - sourceIn <= 180, views == nil
                else { throw SphereFailure.invalid("equirect proxy requires 2048x1024, <=180 seconds and no views") }
            } else {
                guard width == 1440, height == 1280, let views, views.count == 2,
                      views.allSatisfy({ validCamera($0) && $0.at == 0 && $0.cut != true })
                else { throw SphereFailure.invalid("front-back proxy requires 1440x1280 and exactly two resolved views at zero without cuts") }
            }
        } else if views != nil || maxOutputBytes != nil {
            throw SphereFailure.invalid("views and maxOutputBytes are proxy-only")
        }
        if let samples = proofSampleFrames {
            guard isProxy, !samples.isEmpty, samples.count <= 8,
                  samples.allSatisfy({ (0..<frames).contains($0) }),
                  zip(samples, samples.dropFirst()).allSatisfy({ $0 < $1 })
            else { throw SphereFailure.invalid("proof samples require a proxy and at most eight ordered, unique output frames") }
            for frame in samples {
                guard !FileManager.default.fileExists(atPath: proofSampleURL(frame: frame).path)
                else { throw SphereFailure.invalid("proof sample destination already exists") }
            }
        }
        if let c = corrections {
            guard c.sourceStart.isFinite, c.sourceStep.isFinite, c.sourceStep > 0,
                  !c.quaternions.isEmpty, c.quaternions.allSatisfy({ q in
                      q.count == 4 && q.allSatisfy(\.isFinite) && abs(q.reduce(0) { $0 + $1 * $1 } - 1) < 1e-4
                  }), abs(c.sourceStart - sourceIn) < 1e-8,
                  abs(c.sourceStep - speed / Double(fps)) < 1e-10,
                  c.quaternions.count == frames
            else { throw SphereFailure.invalid("correction quaternions must cover every output frame on the explicit source clock") }
        }
    }

    func proofSampleURL(frame: Int) -> URL {
        URL(fileURLWithPath: destination).appendingPathExtension("proof-\(frame).nv12")
    }

    func validateProxyTiming(firstPTS: CMTime) throws {
        guard proxyFPSTick(pts: firstPTS, sourceIn: sourceIn) == 0,
              frames == proxyFPSTick(pts: CMTime(seconds: sourceOut, preferredTimescale: 600_000_000), sourceIn: sourceIn)
        else { throw SphereFailure.unsupported("proxy trim has nonzero first fps tick or mismatched EOF frame count; use ffmpeg") }
    }

    func matrix(frame: Int, keys: [SphereKey]) -> (simd_double3x3, Double) {
        if projection == "equirect" { return (matrix_identity_double3x3, 1) }
        let v = projection == "front-back" ? views![0].key.view :
            SphereMath.pathValue(keys, at: Double(frame) / Double(fps), projection: mode)
        return cameraMatrix(view: v, frame: frame)
    }

    func cameraMatrix(view v: SphereView, frame: Int = 0) -> (simd_double3x3, Double) {
        var m = SphereMath.matrix(for: v)
        if let c = corrections {
            let q = c.quaternions[frame]
            m = SphereMath.matrix(quaternion: q[0], x: q[1], y: q[2], z: q[3]) * m
        }
        // camera_filter sends absolute Euler angles rounded to 0.001 degrees.
        let a = SphereMath.v360Angles(m)
        func rounded(_ x: Double) -> Double { (x * 1000).rounded() / 1000 }
        m = SphereMath.v360Matrix(yaw: rounded(a.yaw), pitch: rounded(a.pitch), roll: rounded(a.roll))
        // camera_filter rounds diagonal FOV, not horizontal FOV.
        let diag = hypot(Double(viewWidth), Double(height)) / Double(viewWidth)
        let dfov = mode == .flat ? 2 * atan(tan(v.fov * .pi / 360) * diag) * 180 / .pi : 4 * atan(1.25 * diag) * 180 / .pi
        let tanHalf = mode == .flat ? tan(rounded(dfov) * .pi / 360) / diag : tan(rounded(dfov) * .pi / 720) / diag
        return (m, tanHalf)
    }
}

/// vf_fps round=near chooses the last input whose rounded output tick is <= n.
func fpsTick(pts: Double, first: Double, speed: Double, fps: Int) -> Int {
    Int(((pts - first) / speed * Double(fps)).rounded(.toNearestOrAwayFromZero))
}

/// Keep exact source timestamps at half-tick boundaries; Double subtraction
/// can put an exact 1/60-second tie on the wrong side of fps round=near.
func proxyFPSTick(pts: CMTime, sourceIn: Double) -> Int {
    let origin = CMTime(seconds: sourceIn, preferredTimescale: 600_000_000)
    return Int(CMTimeConvertScale(pts - origin, timescale: 30, method: .roundHalfAwayFromZero).value)
}
