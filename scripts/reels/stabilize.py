"""360 stabilization from the camera's own orientation track.

GoPro MAX / MAX 2 files carry a per-frame camera orientation quaternion (GPMF
`CORI`) in the telemetry track. It is already fused from the gyro and
accelerometer by the camera, sampled once per video frame, and — checked
against the footage — its axes are exactly the sphere axes ffmpeg's v360 uses
after `sphere.source_graph` rebuilds the .360 (x right, y down, z front).

Stabilizing is then a pure rotation of the sphere, which costs nothing in
picture: no crop, no warping. For a source frame at time t with camera
orientation R(t) and a smoothed orientation S(t), the direction a viewer at
`view` should sample is

    A(t) · V · d          with  A(t) = R(t) · S(t)ᵀ

where V is the view's own v360 rotation and d the output ray. Shake (the part
of R that S doesn't follow) cancels; slow turns of the bike pass through, so
a "road" view keeps facing down the road through a corner. "lock" holds the
orientation at the start of the segment instead (S constant), for short shots
that should not turn at all.

Plans: `"stabilize": true` (medium, ~0.8 s), a number of seconds (0.1–5;
bigger = smoother but slower to follow turns) or `"lock"`.
"""

from __future__ import annotations

import bisect
import math
import struct
from pathlib import Path
from typing import Any

from .media import ffprobe_json
from .telemetry import _packets, _values, gpmd_stream, klv

DEFAULT_SMOOTH = 0.8  # s
MIN_SMOOTH, MAX_SMOOTH = 0.1, 5.0
PRESETS = {"light": 0.35, "medium": DEFAULT_SMOOTH, "strong": 2.0}

Quat = tuple[float, float, float, float]  # (w, x, y, z)
Mat = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------


def parse_setting(raw: Any, errors: list[str], where: str) -> dict | None:
    """`stabilize` from a plan → {"mode": "smooth", "seconds": s} | {"mode": "lock"} | None."""
    if raw in (None, False, 0, "off", "none"):
        return None
    if raw is True:
        return {"mode": "smooth", "seconds": DEFAULT_SMOOTH}
    if isinstance(raw, str):
        key = raw.strip().lower()
        if key == "lock":
            return {"mode": "lock"}
        if key in PRESETS:
            return {"mode": "smooth", "seconds": PRESETS[key]}
        errors.append(f"{where}: stabilize must be true, a number of seconds, light/medium/strong or \"lock\" (got {raw!r})")
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        s = float(raw)
        if not math.isfinite(s) or not MIN_SMOOTH <= s <= MAX_SMOOTH:
            errors.append(f"{where}: stabilize={s:g} s is outside {MIN_SMOOTH:g}–{MAX_SMOOTH:g}")
            return None
        return {"mode": "smooth", "seconds": round(s, 3)}
    errors.append(f"{where}: stabilize must be true, a number of seconds, light/medium/strong or \"lock\"")
    return None


def describe(setting: dict | None) -> str:
    if not setting:
        return ""
    return "stabilized (lock)" if setting["mode"] == "lock" else f"stabilized ({setting['seconds']:g} s)"


# --------------------------------------------------------------------------
# Quaternion / matrix helpers (plain Python: runs inside the sandboxed workers)
# --------------------------------------------------------------------------


def qmul(a: Quat, b: Quat) -> Quat:
    w1, x1, y1, z1 = a
    w2, x2, y2, z2 = b
    return (
        w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
        w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
        w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
        w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
    )


def qnorm(q: Quat) -> Quat:
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return (q[0] / n, q[1] / n, q[2] / n, q[3] / n)


def qconj(q: Quat) -> Quat:
    return (q[0], -q[1], -q[2], -q[3])


def qslerp(a: Quat, b: Quat, u: float) -> Quat:
    dot = sum(x * y for x, y in zip(a, b))
    if dot < 0:
        b, dot = tuple(-c for c in b), -dot  # type: ignore[assignment]
    if dot > 0.9995:
        return qnorm(tuple(x + (y - x) * u for x, y in zip(a, b)))  # type: ignore[arg-type]
    th = math.acos(min(1.0, dot))
    s = math.sin(th)
    wa, wb = math.sin((1 - u) * th) / s, math.sin(u * th) / s
    return tuple(wa * x + wb * y for x, y in zip(a, b))  # type: ignore[return-value]


def qmat(q: Quat) -> Mat:
    w, x, y, z = q
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def mmul(a: Mat, b: Mat) -> Mat:
    return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))  # type: ignore[return-value]


def mT(a: Mat) -> Mat:
    return tuple(tuple(a[j][i] for j in range(3)) for i in range(3))  # type: ignore[return-value]


IDENTITY: Mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def v360_matrix(yaw: float, pitch: float, roll: float) -> Mat:
    """The rotation v360 applies for yaw/pitch/roll (degrees): input ray = M · output ray.

    v360 multiplies yaw (about y), pitch (about x) and roll (about z) quaternions in
    that order; verified against ffmpeg on a direction-encoded test sphere."""
    a, b, c = (math.radians(v) / 2 for v in (yaw, pitch, roll))
    q = qmul(qmul((math.cos(a), 0.0, math.sin(a), 0.0), (math.cos(b), math.sin(b), 0.0, 0.0)), (math.cos(c), 0.0, 0.0, math.sin(c)))
    return qmat(q)


def v360_angles(m: Mat) -> tuple[float, float, float]:
    """Inverse of v360_matrix: (yaw, pitch, roll) in degrees."""
    sp = max(-1.0, min(1.0, -m[1][2]))
    pitch = math.asin(sp)
    if abs(sp) < 0.99999:
        yaw = math.atan2(m[0][2], m[2][2])
        roll = math.atan2(m[1][0], m[1][1])
    else:  # looking straight up/down: only yaw ± roll is defined — put it all in yaw
        roll = 0.0
        yaw = math.atan2(-m[2][0], m[0][0])
    return math.degrees(yaw), math.degrees(pitch), math.degrees(roll)


# --------------------------------------------------------------------------
# Reading the orientation track
# --------------------------------------------------------------------------


class Orientation:
    """Camera orientation samples (CORI) between two source times."""

    def __init__(self, times: list[float], quats: list[Quat], gravity: tuple[float, float, float] | None = None):
        self.times = times
        self.quats = quats
        self.gravity = gravity  # mean gravity direction in sphere coordinates (GRAV), when recorded

    def __len__(self) -> int:
        return len(self.times)

    def at(self, t: float) -> Quat:
        ts = self.times
        i = bisect.bisect_left(ts, t)
        if i <= 0:
            return self.quats[0]
        if i >= len(ts):
            return self.quats[-1]
        a, b = ts[i - 1], ts[i]
        return qslerp(self.quats[i - 1], self.quats[i], (t - a) / max(b - a, 1e-9))

    def smoothed(self, t: float, sigma: float) -> Quat:
        """Gaussian-weighted mean orientation around t (sign-aligned quaternion average)."""
        ts = self.times
        lo = bisect.bisect_left(ts, t - 3 * sigma)
        hi = bisect.bisect_right(ts, t + 3 * sigma)
        ref = self.at(t)
        acc = [0.0, 0.0, 0.0, 0.0]
        for i in range(lo, hi):
            q = self.quats[i]
            if sum(x * y for x, y in zip(q, ref)) < 0:
                q = (-q[0], -q[1], -q[2], -q[3])
            w = math.exp(-0.5 * ((ts[i] - t) / sigma) ** 2)
            for k in range(4):
                acc[k] += w * q[k]
        if not any(acc):
            return ref
        return qnorm((acc[0], acc[1], acc[2], acc[3]))


def read_orientation(path: Path, start: float, end: float, info: dict | None = None) -> Orientation | None:
    """CORI samples covering [start, end] (source seconds), or None if the file has none."""
    info = info or ffprobe_json(path)
    index = gpmd_stream(info)
    if index is None:
        return None
    video_start = 0.0
    for s in info.get("streams") or []:
        if s.get("codec_type") == "video":
            video_start = float(s.get("start_time") or 0)
            break
    times: list[float] = []
    quats: list[Quat] = []
    grav = [0.0, 0.0, 0.0]
    with open(path, "rb") as f:
        for pts, dur, pos, size in _packets(path, index):
            t0 = pts - video_start
            if t0 + dur < start or t0 > end:
                continue
            f.seek(pos)
            gravity: list[tuple[float, float, float]] = []
            for samples in _cori(f.read(size), gravity):
                n = len(samples)
                for i, q in enumerate(samples):
                    times.append(t0 + i * dur / n)
                    quats.append(q)
            for g in gravity:
                for k in range(3):
                    grav[k] += g[k]
    if len(times) < 2:
        return None
    order = sorted(range(len(times)), key=times.__getitem__)
    norm = math.sqrt(sum(g * g for g in grav))
    gravity_mean = (grav[0] / norm, grav[1] / norm, grav[2] / norm) if norm > 0 else None
    return Orientation([times[i] for i in order], [quats[i] for i in order], gravity_mean)


def _cori(buf: bytes, gravity: list | None = None) -> list[list[Quat]]:
    """CORI quaternion runs in one telemetry packet; GRAV vectors are appended to `gravity`."""
    out: list[list[Quat]] = []
    for key, typ, _, _, data in klv(buf):
        if key != "DEVC" or typ != "\0":
            continue
        for skey, styp, _, _, sdata in klv(data):
            if skey != "STRM" or styp != "\0":
                continue
            scal = 1.0
            for k, t, size, rep, d in klv(sdata):
                if k == "SCAL":
                    scal = float((_values(t, size, rep, d) or [1])[0]) or 1.0
                elif k == "CORI" and t == "s" and size == 8:
                    rows = struct.iter_unpack(">4h", d[: rep * 8])
                    out.append([qnorm((w / scal, x / scal, y / scal, z / scal)) for w, x, y, z in rows])
                elif k == "GRAV" and t == "s" and size == 6 and gravity is not None:
                    gravity.extend((x / scal, y / scal, z / scal) for x, y, z in struct.iter_unpack(">3h", d[: rep * 6]))
    return out


# --------------------------------------------------------------------------
# Corrections for a segment
# --------------------------------------------------------------------------


def margin(setting: dict) -> float:
    return 0.5 if setting["mode"] == "lock" else 3 * _sigma(setting) + 0.5


def _sigma(setting: dict) -> float:
    return max(0.05, float(setting.get("seconds", DEFAULT_SMOOTH)) / 2)


def corrections(ori: Orientation, times: list[float], setting: dict) -> list[Mat]:
    """A(t) = R(t)·S(t)ᵀ for each source time (in v360 sphere coordinates)."""
    if setting["mode"] == "lock":
        ref = ori.at(times[0]) if times else ori.quats[0]
        return [qmat(qmul(ori.at(t), qconj(ref))) for t in times]
    sigma = _sigma(setting)
    return [qmat(qmul(ori.at(t), qconj(ori.smoothed(t, sigma)))) for t in times]


def correction_quats(ori: Orientation, times: list[float], setting: dict) -> list[Quat]:
    """Same as corrections() but as unit quaternions (compact, for the editor's live viewer)."""
    if setting["mode"] == "lock":
        ref = ori.at(times[0]) if times else ori.quats[0]
        return [qnorm(qmul(ori.at(t), qconj(ref))) for t in times]
    sigma = _sigma(setting)
    return [qnorm(qmul(ori.at(t), qconj(ori.smoothed(t, sigma)))) for t in times]


def segment_corrections(src: Path, start: float, speed: float, fps: int, frames: int, setting: dict) -> list[Mat] | None:
    """Per output frame corrections for a 360 video segment, or None without orientation data."""
    end = start + (frames - 1) * speed / fps
    pad = margin(setting)
    ori = read_orientation(src, max(0.0, start - pad), end + pad)
    if ori is None:
        return None
    return corrections(ori, [start + n * speed / fps for n in range(frames)], setting)
