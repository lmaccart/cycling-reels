import Foundation
import simd

/// A camera aim in v360 terms. Degrees; `fov` is the horizontal fov of the 9:16 frame.
struct SphereView: Equatable, Sendable {
    var yaw: Double
    var pitch: Double
    var fov: Double
    var roll: Double
}

/// A keyframe on the camera path. `cut` holds the previous view until `at`, then jumps.
struct SphereKey: Equatable, Sendable {
    var at: Double
    var view: SphereView
    var cut: Bool
}

enum SphereProjection: Sendable { case flat, tinyPlanet }

/// Pure math mirroring ffmpeg's v360 filter. Coordinates: x right, y DOWN, z forward.
enum SphereMath {
    /// Builds a matrix from rows (simd is column-major).
    static func rows(_ r0: simd_double3, _ r1: simd_double3, _ r2: simd_double3) -> simd_double3x3 {
        simd_double3x3(rows: [r0, r1, r2])
    }

    /// Element at (row, column).
    static func element(_ m: simd_double3x3, _ r: Int, _ c: Int) -> Double { m[c][r] }

    /// Unit-quaternion rotation matrix.
    static func matrix(quaternion w: Double, x: Double, y: Double, z: Double) -> simd_double3x3 {
        let n = (w * w + x * x + y * y + z * z).squareRoot()
        let (w, x, y, z) = n > 0 ? (w / n, x / n, y / n, z / n) : (1, 0, 0, 0)
        return rows(
            simd_double3(1 - 2 * (y * y + z * z), 2 * (x * y - w * z), 2 * (x * z + w * y)),
            simd_double3(2 * (x * y + w * z), 1 - 2 * (x * x + z * z), 2 * (y * z - w * x)),
            simd_double3(2 * (x * z - w * y), 2 * (y * z + w * x), 1 - 2 * (x * x + y * y)))
    }

    private static func hamilton(_ a: (Double, Double, Double, Double),
                                 _ b: (Double, Double, Double, Double)) -> (Double, Double, Double, Double) {
        (a.0 * b.0 - a.1 * b.1 - a.2 * b.2 - a.3 * b.3,
         a.0 * b.1 + a.1 * b.0 + a.2 * b.3 - a.3 * b.2,
         a.0 * b.2 - a.1 * b.3 + a.2 * b.0 + a.3 * b.1,
         a.0 * b.3 + a.1 * b.2 - a.2 * b.1 + a.3 * b.0)
    }

    /// v360's rotation: input ray = M · output ray. Angles in degrees.
    static func v360Matrix(yaw: Double, pitch: Double, roll: Double) -> simd_double3x3 {
        let a = yaw * .pi / 360, b = pitch * .pi / 360, c = roll * .pi / 360
        let qy = (cos(a), 0.0, sin(a), 0.0)
        let qp = (cos(b), sin(b), 0.0, 0.0)
        let qr = (cos(c), 0.0, 0.0, sin(c))
        let q = hamilton(hamilton(qy, qp), qr)
        return matrix(quaternion: q.0, x: q.1, y: q.2, z: q.3)
    }

    /// Inverse of `v360Matrix`, degrees.
    static func v360Angles(_ m: simd_double3x3) -> (yaw: Double, pitch: Double, roll: Double) {
        let sp = min(1, max(-1, -element(m, 1, 2)))
        let cp = hypot(element(m, 0, 2), element(m, 2, 2))
        let pitch = atan2(sp, cp)
        let yaw: Double, roll: Double
        if cp > 1e-12 {
            yaw = atan2(element(m, 0, 2), element(m, 2, 2))
            roll = atan2(element(m, 1, 0), element(m, 1, 1))
        } else {
            roll = 0
            yaw = atan2(-element(m, 2, 0), element(m, 0, 0))
        }
        let d = 180 / Double.pi
        return (yaw * d, pitch * d, roll * d)
    }

    static func matrix(for view: SphereView) -> simd_double3x3 {
        v360Matrix(yaw: view.yaw, pitch: view.pitch, roll: view.roll)
    }

    static func wrapYaw(_ y: Double) -> Double {
        let m = (y + 180).truncatingRemainder(dividingBy: 360)
        return (m < 0 ? m + 360 : m) - 180
    }

    /// Smootherstep on u clamped to [0, 1].
    static func ease(_ u: Double) -> Double {
        let u = min(1, max(0, u))
        return u * u * u * (u * (u * 6 - 15) + 10)
    }

    /// Unit quaternion (w, x, y, z) of v360's yaw · pitch · roll rotation, degrees.
    static func viewQuaternion(yaw: Double, pitch: Double, roll: Double) -> (Double, Double, Double, Double) {
        let a = yaw * .pi / 360, b = pitch * .pi / 360, c = roll * .pi / 360
        let (ca, sa, cb, sb, cc, sc) = (cos(a), sin(a), cos(b), sin(b), cos(c), sin(c))
        return (ca * cb * cc + sa * sb * sc, ca * sb * cc + sa * cb * sc,
                sa * cb * cc - ca * sb * sc, ca * cb * sc - sa * sb * cc)
    }

    /// Shortest-arc slerp (flips `b` when the dot product is negative); nlerp when nearly equal.
    /// Same steps and thresholds as `stabilize.qslerp` in Python.
    static func slerp(_ a: (Double, Double, Double, Double), _ b0: (Double, Double, Double, Double),
                      _ u: Double) -> (Double, Double, Double, Double) {
        var b = b0
        var dot = a.0 * b.0 + a.1 * b.1 + a.2 * b.2 + a.3 * b.3
        if dot < 0 { b = (-b.0, -b.1, -b.2, -b.3); dot = -dot }
        if dot > 0.9995 {
            let q = (a.0 + (b.0 - a.0) * u, a.1 + (b.1 - a.1) * u, a.2 + (b.2 - a.2) * u, a.3 + (b.3 - a.3) * u)
            let n = (q.0 * q.0 + q.1 * q.1 + q.2 * q.2 + q.3 * q.3).squareRoot()
            return n > 0 ? (q.0 / n, q.1 / n, q.2 / n, q.3 / n) : (1, 0, 0, 0)
        }
        let th = acos(min(1, dot)), s = sin(th)
        let wa = sin((1 - u) * th) / s, wb = sin(u * th) / s
        return (wa * a.0 + wb * b.0, wa * a.1 + wb * b.1, wa * a.2 + wb * b.2, wa * a.3 + wb * b.3)
    }

    /// Camera path value at time `t`. `keys` sorted by `at`, non-empty.
    /// Orientation follows the shortest arc (eased quaternion slerp); fov eases linearly.
    /// Mirrors `sphere.path_value` in Python, held together by tests/fixtures/sphere_path_golden.json.
    static func pathValue(_ keys: [SphereKey], at t: Double, projection: SphereProjection = .flat) -> SphereView {
        guard let first = keys.first else { return SphereView(yaw: 0, pitch: 0, fov: 90, roll: 0) }
        if t <= first.at { return effectiveView(first.view, projection: projection) }
        for i in 0..<(keys.count - 1) {
            let a = keys[i], b = keys[i + 1]
            let av = effectiveView(a.view, projection: projection)
            let bv = effectiveView(b.view, projection: projection)
            if b.cut {
                if t < b.at { return av }
                continue
            }
            if t <= b.at {
                let e = ease((t - a.at) / max(1e-6, b.at - a.at))
                if e <= 0 || e >= 1 {
                    var v = e <= 0 ? av : bv
                    v.yaw = wrapYaw(v.yaw)
                    return v
                }
                let qa = viewQuaternion(yaw: av.yaw, pitch: av.pitch, roll: av.roll)
                let qb = viewQuaternion(yaw: bv.yaw, pitch: bv.pitch, roll: bv.roll)
                let q = slerp(qa, qb, e)
                let angles = v360Angles(matrix(quaternion: q.0, x: q.1, y: q.2, z: q.3))
                return SphereView(yaw: wrapYaw(angles.yaw), pitch: angles.pitch,
                                  fov: a.view.fov + (b.view.fov - a.view.fov) * e, roll: angles.roll)
            }
        }
        return effectiveView(keys[keys.count - 1].view, projection: projection)
    }

    /// Tiny planet always looks straight down (pitch -90); yaw and roll come from the view.
    static func effectiveView(_ view: SphereView, projection: SphereProjection) -> SphereView {
        var v = view
        if projection == .tinyPlanet { v.pitch = -90 }
        return v
    }

    /// Output ray for a pixel at (u, v) in [0,1] (v top to bottom); `aspect` = height / width.
    static func ray(u: Double, v: Double, fov: Double, aspect: Double,
                    projection: SphereProjection) -> simd_double3 {
        switch projection {
        case .flat:
            let tx = tan(fov * .pi / 360)
            return simd_normalize(simd_double3(tx * (2 * u - 1), tx * aspect * (2 * v - 1), 1))
        case .tinyPlanet:
            let x = 1.25 * (2 * u - 1), y = 1.25 * aspect * (2 * v - 1)
            let r = (x * x + y * y).squareRoot()
            if r == 0 { return simd_double3(0, 0, 1) }
            let th = 2 * atan(r)
            return simd_double3(x / r * sin(th), y / r * sin(th), cos(th))
        }
    }

    /// Sampling direction in the sphere for an output pixel, given the full matrix (stabilization · view).
    static func sampleDirection(matrix m: simd_double3x3, u: Double, v: Double, fov: Double,
                                aspect: Double, projection: SphereProjection) -> simd_double3 {
        m * ray(u: u, v: v, fov: fov, aspect: aspect, projection: projection)
    }

    /// Equirectangular texture coordinates (v = 0 is the top row).
    static func equirectUV(_ d: simd_double3) -> (u: Double, v: Double) {
        let phi = atan2(d.x, d.z)
        let theta = asin(min(1, max(-1, d.y)))
        return ((phi / .pi + 1) / 2, (theta / (.pi / 2) + 1) / 2)
    }

    /// Axis-angle rotation (right-handed), angle in degrees.
    static func rotation(axis: simd_double3, degrees: Double) -> simd_double3x3 {
        let n = simd_normalize(axis)
        let h = degrees * .pi / 360
        return matrix(quaternion: cos(h), x: n.x * sin(h), y: n.y * sin(h), z: n.z * sin(h))
    }

    private static func view(_ m: simd_double3x3, fov: Double) -> SphereView {
        let a = v360Angles(m)
        return SphereView(yaw: a.yaw, pitch: a.pitch, fov: fov, roll: a.roll)
    }

    /// Turns the camera: positive `right` looks further right, positive `up` looks further up.
    /// `upAxis` is the sphere's up direction (unit, sphere coords); nil uses the view's own up.
    static func turn(_ view0: SphereView, right a: Double, up b: Double,
                     upAxis: simd_double3? = nil) -> SphereView {
        let v = matrix(for: view0)
        let up = upAxis ?? v * simd_double3(0, -1, 0)
        let r = rotation(axis: -up, degrees: a)
        return view(r * v * v360Matrix(yaw: 0, pitch: b, roll: 0), fov: view0.fov)
    }

    /// Rolls about the view's forward axis so the horizon is level against `upAxis`.
    static func level(_ view0: SphereView, upAxis: simd_double3) -> SphereView {
        let v = matrix(for: view0)
        let u = simd_normalize(upAxis)
        let r = v * simd_double3(1, 0, 0), d = v * simd_double3(0, 1, 0)
        let probe = v360Matrix(yaw: 0, pitch: 0, roll: 30)
        let k: Double = element(probe, 1, 0) > 0 ? 1 : -1
        var phi = atan2(-simd_dot(u, r), k * simd_dot(u, d))
        func downDot(_ p: Double) -> Double {
            -k * sin(p) * simd_dot(u, r) + cos(p) * simd_dot(u, d)
        }
        if downDot(phi) > 0 { phi += .pi }
        let m = v * v360Matrix(yaw: 0, pitch: 0, roll: phi * 180 / .pi)
        return view(m, fov: view0.fov)
    }

    /// Degrees of camera turn per screen point so dragged content roughly follows the cursor.
    static func degreesPerPoint(fov: Double, viewWidthPoints: Double) -> Double {
        fov / max(1, viewWidthPoints)
    }
}
