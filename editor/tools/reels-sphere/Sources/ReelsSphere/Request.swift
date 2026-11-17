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

    var mode: SphereProjection { projection == "flat" ? .flat : .tinyPlanet }

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
              ["flat", "tiny-planet"].contains(projection), ["h264", "hevc"].contains(codec)
        else { throw SphereFailure.invalid("projection, track IDs or encoder") }
        guard !keys.isEmpty, keys.allSatisfy({
            [$0.at, $0.yaw, $0.pitch, $0.roll, $0.fov].allSatisfy(\.isFinite) &&
                abs($0.pitch) <= 90 && abs($0.roll) <= 180 && (10...150).contains($0.fov)
        }), zip(keys, keys.dropFirst()).allSatisfy({ $0.at < $1.at })
        else { throw SphereFailure.invalid("finite, strictly ordered camera keys with valid angles/FOV required") }
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

    func matrix(frame: Int, keys: [SphereKey]) -> (simd_double3x3, Double) {
        let v = SphereMath.pathValue(keys, at: Double(frame) / Double(fps), projection: mode)
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
        let diag = hypot(Double(width), Double(height)) / Double(width)
        let dfov = mode == .flat ? 2 * atan(tan(v.fov * .pi / 360) * diag) * 180 / .pi : 4 * atan(1.25 * diag) * 180 / .pi
        let tanHalf = mode == .flat ? tan(rounded(dfov) * .pi / 360) / diag : tan(rounded(dfov) * .pi / 720) / diag
        return (m, tanHalf)
    }
}

/// vf_fps round=near chooses the last input whose rounded output tick is <= n.
func fpsTick(pts: Double, first: Double, speed: Double, fps: Int) -> Int {
    Int(((pts - first) / speed * Double(fps)).rounded(.toNearestOrAwayFromZero))
}
