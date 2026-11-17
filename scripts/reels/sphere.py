"""360° footage: GoPro MAX / MAX 2 .360 files and equirectangular video/photos.

A GoPro .360 holds two video tracks, each a strip of three cube faces in
equi-angular cubemap (EAC) form. The four side faces carry a duplicated seam
band (64 px on the MAX at 5.6K) that has to be cross-faded and the remaining
picture stretched back to a full face. Rebuilt and stacked (front track on top,
rear below), the result is exactly ffmpeg's v360 `eac` layout — same face
order and rotations as FFmpeg's gopromax_opencl filter — so v360 can point a
virtual camera anywhere in the sphere.

Conventions (match the yaw labels on 360 contact sheets):
  yaw   0 = camera front, +90 = right, ±180 = behind, -90 = left
  pitch 0 = horizon, negative looks down
  fov   horizontal field of view of the 9:16 frame, in degrees
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from .media import hdr_filters, run
from .util import ReelsError

DEFAULT_VIEW = {"yaw": 0.0, "pitch": 0.0, "fov": 75.0, "roll": 0.0}
BUILTIN_ANGLES = {"front": 0.0, "right": 90.0, "back": 180.0, "left": -90.0}
PROJECTIONS = ("flat", "tiny-planet")
PLANET_FILL = 1.25  # tiny planet: horizon circle at 1/1.25 = 80% of the frame's half-width


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------


def detect_video(data: dict, path: Path) -> dict | None:
    """360 info from ffprobe output, or None for ordinary video."""
    vids = [
        s
        for s in data.get("streams") or []
        if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not vids:
        return None
    big = [s for s in vids if int(s.get("width") or 0) >= 1000]
    if len(big) >= 2:
        a, b = big[0], big[1]
        w, h = int(a["width"]), int(a["height"])
        extra = w - 3 * h
        same = (int(b.get("width") or 0), int(b.get("height") or 0)) == (w, h)
        if same and 0 < extra <= h // 4 and extra % 4 == 0:
            return {
                "projection": "gopro-eac",
                "source": "GoPro .360",
                "tracks": [int(a["index"]), int(b["index"])],
                "face": h,
                "overlap": extra,
                "width": 4 * h,
                "height": 2 * h,
            }
    v = vids[0]
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    for sd in v.get("side_data_list") or []:
        if "spherical" in str(sd.get("side_data_type", "")).lower() and "equirect" in str(sd.get("projection", "")).lower():
            return {"projection": "equirect", "source": "360 metadata", "width": w, "height": h}
    if path.suffix.lower() == ".360":
        raise ReelsError(f"{path.name}: a .360 file without the two GoPro video tracks — export it as an MP4 from GoPro Quik/Player")
    if w == 2 * h and w >= 3840:
        return {"projection": "equirect", "source": "2:1 frame (assumed 360)", "width": w, "height": h}
    return None


def detect_photo(path: Path, width: int, height: int) -> dict | None:
    """360 photos are 2:1 equirectangular JPGs, usually tagged with GPano XMP."""
    if width != 2 * height:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(512 * 1024)
    except OSError:
        head = b""
    tagged = re.search(rb"ProjectionType\s*(?:=\s*\"|>)\s*equirectangular", head) is not None
    if tagged or width >= 4000:
        return {"projection": "equirect", "source": "360 photo" if tagged else "2:1 photo (assumed 360)", "width": width, "height": height}
    return None


# --------------------------------------------------------------------------
# Filter graphs
# --------------------------------------------------------------------------


def _track_chain(src: str, out: str, w: int, h: int, tag: str, pre: list[str]) -> str:
    o = w - 3 * h  # duplicated seam band
    g = o // 2  # unique picture per side face is h - g
    cs = h - g
    p = (cs - o) // 2  # picture either side of the band
    blend = "blend=all_expr='A*(1-(X+0.5)/W)+B*((X+0.5)/W)'"

    def side(dx: int, s: str) -> str:
        return (
            f"[{s}0]crop={p}:{h}:{dx}:0[{s}p1];[{s}1]crop={o}:{h}:{dx + p}:0[{s}ba];"
            f"[{s}2]crop={o}:{h}:{dx + p + o}:0[{s}bb];[{s}3]crop={p}:{h}:{dx + p + 2 * o}:0[{s}p2];"
            f"[{s}ba][{s}bb]{blend}[{s}bl];[{s}p1][{s}bl][{s}p2]hstack=3,scale={h}:{h}:flags=bicubic[{s}f]"
        )

    left, right = f"{tag}l", f"{tag}r"
    head = ",".join(pre + ["format=yuv420p"])
    return (
        f"[{src}]{head},split=9[{left}0][{left}1][{left}2][{left}3][{tag}m][{right}0][{right}1][{right}2][{right}3];"
        + side(0, left)
        + f";[{tag}m]crop={h}:{h}:{h + g}:0[{tag}mf];"
        + side(2 * h + g, right)
        + f";[{left}f][{tag}mf][{right}f]hstack=3[{out}]"
    )


def source_graph(probe: dict, input_index: int = 0) -> tuple[str, str, str]:
    """(filter graph text, output pad name, v360 input projection) for a 360 video."""
    sph = probe["sphere"]
    pre = hdr_filters(probe)
    if sph["projection"] == "gopro-eac":
        w, h = sph["face"] * 3 + sph["overlap"], sph["face"]
        t0, t1 = sph["tracks"]
        graph = (
            _track_chain(f"{input_index}:{t0}", "sphA", w, h, "a", pre)
            + ";"
            + _track_chain(f"{input_index}:{t1}", "sphB", w, h, "b", pre)
            + ";[sphA][sphB]vstack=2[sph]"
        )
        return graph, "sph", "eac"
    head = ",".join(pre + ["format=yuv420p"])
    return f"[{input_index}:v:0]{head}[sph]", "sph", "e"


def wrap_yaw(y: float) -> float:
    return ((float(y) + 180.0) % 360.0) - 180.0


def flat_dfov(fov: float, w: int, h: int) -> float:
    """Diagonal fov of a w x h rectilinear frame whose horizontal fov is `fov`.

    v360 derives both horizontal and vertical fov from `d_fov`, so one value
    (one runtime command) describes the zoom.
    """
    fov = max(10.0, min(float(fov), 170.0))
    return 2 * math.degrees(math.atan(math.tan(math.radians(fov / 2)) * math.hypot(w, h) / w))


def planet_dfov(w: int, h: int) -> float:
    """Stereographic d_fov that puts the horizon circle at 1/PLANET_FILL of the half-width."""
    return 4 * math.degrees(math.atan(PLANET_FILL * math.hypot(w, h) / w))


CAMERA_KEYS = ("yaw", "pitch", "roll", "d_fov")


def view_params(view: dict, projection: str, w: int, h: int) -> dict:
    """Absolute v360 parameters for one camera position."""
    if projection == "tiny-planet":
        return {"yaw": wrap_yaw(view["yaw"]), "pitch": -90.0, "roll": float(view.get("roll", 0)), "d_fov": planet_dfov(w, h)}
    return {
        "yaw": wrap_yaw(view["yaw"]),
        "pitch": max(-90.0, min(90.0, float(view.get("pitch", 0)))),
        "roll": float(view.get("roll", 0)),
        "d_fov": flat_dfov(view.get("fov", DEFAULT_VIEW["fov"]), w, h),
    }


def v360_filter(inproj: str, projection: str, params: dict, w: int, h: int, name: str | None = None) -> str:
    out = "sg" if projection == "tiny-planet" else "flat"
    label = f"v360@{name}" if name else "v360"
    angles = ":".join(f"{k}={params[k]:.3f}" for k in CAMERA_KEYS)
    return f"{label}=input={inproj}:output={out}:{angles}:w={w}:h={h}:interp=cubic:reset_rot=1"


def _ease(u: float) -> float:
    u = max(0.0, min(1.0, u))
    return u * u * u * (u * (u * 6 - 15) + 10)  # smootherstep: gentle start and stop, like a hand pan


def view_quat(view: dict) -> tuple[float, float, float, float]:
    """The unit quaternion (w, x, y, z) of v360's yaw · pitch · roll rotation for a view."""
    a, b, c = (math.radians(float(view.get(k, 0.0))) / 2 for k in ("yaw", "pitch", "roll"))
    ca, sa, cb, sb, cc, sc = math.cos(a), math.sin(a), math.cos(b), math.sin(b), math.cos(c), math.sin(c)
    # (cos a + sin a·j)(cos b + sin b·i)(cos c + sin c·k), multiplied out
    return (ca * cb * cc + sa * sb * sc, ca * sb * cc + sa * cb * sc, sa * cb * cc - ca * sb * sc, ca * cb * sc - sa * sb * cc)


def path_value(keys: list[dict], t: float, projection: str = "flat") -> dict:
    """Camera at time t along keyframes [{'at', 'yaw', 'pitch', 'fov', 'roll'[, 'cut']}].

    Orientation moves along the shortest great-circle arc (quaternion slerp, eased), so a
    keyframe pair never spins the long way round in any axis; fov eases linearly.
    A keyframe with `cut: true` is reached by a hard cut instead of an eased move.
    The editor's live viewer (SphereMath.pathValue) runs the same algorithm; the
    golden vectors in tests/fixtures/sphere_path_golden.json hold both to it."""
    from .stabilize import qmat, qslerp, v360_angles

    def effective(key):
        return {**key, "pitch": -90.0} if projection == "tiny-planet" else key

    if t <= keys[0]["at"]:
        return dict(effective(keys[0]))
    for a, b in zip(keys, keys[1:]):
        if b.get("cut"):  # hard cut: hold the previous view until this keyframe, then jump
            if t < b["at"]:
                return {**effective(a), "at": t, "cut": False}
            continue
        if t <= b["at"]:
            a, b = effective(a), effective(b)
            span = max(1e-6, b["at"] - a["at"])
            e = _ease((t - a["at"]) / span)
            if e <= 0.0 or e >= 1.0:
                k = a if e <= 0.0 else b
                return {"at": t, "yaw": wrap_yaw(k["yaw"]), "pitch": float(k["pitch"]),
                        "fov": float(k["fov"]), "roll": float(k["roll"])}
            yaw, pitch, roll = v360_angles(qmat(qslerp(view_quat(a), view_quat(b), e)))
            return {"at": t, "yaw": wrap_yaw(yaw), "pitch": pitch, "fov": a["fov"] + (b["fov"] - a["fov"]) * e, "roll": roll}
    return dict(effective(keys[-1]))


def camera_filter(
    sph_spec: dict, inproj: str, w: int, h: int, fps: int, frames: int, cmd_file: Path,
    stab: list | None = None,
) -> str:
    """v360 for a segment; animated paths get per-frame commands in cmd_file.

    With reset_rot=1 every value is absolute and v360 keeps the ones not sent,
    so each frame only carries what changed. That matters: every command makes
    v360 rebuild its whole remap table, by far the slowest part of a camera move.

    `stab` (stabilize.segment_corrections: one rotation per output frame) turns
    the sphere under the view each frame, so the camera's shake cancels out.
    """
    from .stabilize import mmul, v360_angles, v360_matrix

    projection = sph_spec["projection"]
    keys = sph_spec.get("path") or []
    start = keys[0] if keys else sph_spec["view"]
    first = view_params(start, projection, w, h)
    if not keys and not stab:
        return v360_filter(inproj, projection, first, w, h)

    def frame_params(n: int) -> dict:
        p = view_params(path_value(keys, n / fps, projection), projection, w, h) if keys else dict(first)
        if stab:
            a = stab[min(n, len(stab) - 1)]
            p["yaw"], p["pitch"], p["roll"] = v360_angles(mmul(a, v360_matrix(p["yaw"], p["pitch"], p["roll"])))
        return p

    first = frame_params(0)
    current = {k: f"{first[k]:.3f}" for k in CAMERA_KEYS}
    lines = []
    for n in range(1, frames):
        p = frame_params(n)
        cmds = []
        for k in CAMERA_KEYS:
            value = f"{p[k]:.3f}"
            if value != current[k]:
                cmds.append(f"v360@cam {k} {value}")
                current[k] = value
        if cmds:
            # half a frame early: sendcmd fires on the first frame at/after this time,
            # and rounding the exact frame time up would push the move a frame late
            lines.append(f"{max(0.0, (n - 0.5) / fps):.4f} " + ", ".join(cmds) + ";")
    if not lines:  # keyframes that don't actually move
        return v360_filter(inproj, projection, first, w, h)
    cmd_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"sendcmd=f={cmd_file.name}," + v360_filter(inproj, projection, first, w, h, name="cam")


# --------------------------------------------------------------------------
# Single frames (contact sheets and previews)
# --------------------------------------------------------------------------


def extract_equirect(src: Path, t: float, out: Path, probe: dict, width: int) -> None:
    """One unwrapped 2:1 frame of the whole sphere (yaw 0 at the centre)."""
    graph, pad, inproj = source_graph(probe)
    h = max(2, width // 2)
    tail = f"scale={width}:{h}" if inproj == "e" else f"v360=input={inproj}:output=e:w={width}:h={h}:interp=cubic"
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-filter_complex", f"{graph};[{pad}]{tail}[o]",
         "-map", "[o]", "-frames:v", "1", "-q:v", "3", out])  # fmt: skip
    if not out.exists():
        raise ReelsError(f"couldn't extract a 360 frame at {t:.2f}s from {src.name}")


def extract_view(src: Path, t: float, out: Path, probe: dict, view: dict, w: int, h: int, projection: str = "flat") -> None:
    graph, pad, inproj = source_graph(probe)
    vf = v360_filter(inproj, projection, view_params(view, projection, w, h), w, h)
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-filter_complex", f"{graph};[{pad}]{vf}[o]",
         "-map", "[o]", "-frames:v", "1", "-q:v", "3", out])  # fmt: skip
    if not out.exists():
        raise ReelsError(f"couldn't render a 360 view at {t:.2f}s from {src.name}")


def photo_view(src: Path, out: Path, view: dict, w: int, h: int, projection: str = "flat") -> None:
    """Reframes an equirectangular photo to a flat (or tiny-planet) image."""
    vf = v360_filter("e", projection, view_params(view, projection, w, h), w, h)
    run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", vf, "-frames:v", "1", "-q:v", "2", out])
    if not out.exists():
        raise ReelsError(f"couldn't reframe 360 photo {src.name}")


# --------------------------------------------------------------------------
# Views from tags and plans
# --------------------------------------------------------------------------


def _num(v: Any, name: str, lo: float, hi: float, errors: list[str], where: str) -> float:
    try:
        x = float(v)
    except (TypeError, ValueError):
        errors.append(f"{where}: {name} must be a number (got {v!r})")
        return lo
    if not lo <= x <= hi:
        errors.append(f"{where}: {name}={x:g} is outside {lo:g}–{hi:g}")
    return x


def parse_view(raw: Any, angles: dict, errors: list[str], where: str, base: dict | None = None) -> dict:
    """A view from a name ("rider", "front"…) or {"yaw","pitch","fov","roll"[,"view"]}."""
    view = dict(base or DEFAULT_VIEW)
    if raw is None:
        return view
    if isinstance(raw, str):
        name = raw.strip().lower()
        if name in angles:
            return {**view, **angles[name]}
        if name in BUILTIN_ANGLES:
            return {**view, "yaw": BUILTIN_ANGLES[name]}
        errors.append(f"{where}: unknown view {raw!r} (tagged angles: {', '.join(angles) or 'none'}; built in: front, right, back, left)")
        return view
    if not isinstance(raw, dict):
        errors.append(f"{where}: view must be a name or an object with yaw/pitch/fov")
        return view
    if raw.get("view") is not None:
        view = parse_view(raw["view"], angles, errors, where, view)
    if "yaw" in raw:
        view["yaw"] = _num(raw["yaw"], "yaw", -360, 360, errors, where)
    if "pitch" in raw:
        view["pitch"] = _num(raw["pitch"], "pitch", -90, 90, errors, where)
    if "fov" in raw:
        view["fov"] = _num(raw["fov"], "fov", 20, 150, errors, where)
    if "roll" in raw:
        view["roll"] = _num(raw["roll"], "roll", -180, 180, errors, where)
    return view


def parse_angles(raw: Any, errors: list[str]) -> dict:
    """Tag-time named directions: {"rider": {"yaw": 180, "pitch": -15}, "road": {"yaw": 0}}."""
    if raw in (None, {}):
        return {}
    if not isinstance(raw, dict):
        errors.append("angles must be an object of name → {yaw, pitch, fov}")
        return {}
    out = {}
    for name, v in raw.items():
        key = str(name).strip().lower()
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,19}", key):
            errors.append(f"angles: name {name!r} should be a short lowercase word (e.g. rider, road)")
            continue
        v = {"yaw": v} if isinstance(v, (int, float)) else v
        view = parse_view(v, {}, errors, f"angles.{key}")
        out[key] = {k: round(float(view[k]), 2) for k in ("yaw", "pitch", "fov", "roll")}
    return out


def plan_camera(
    spec: dict,
    tags: dict,
    moment_view: Any,
    seconds: float,
    errors: list[str],
    warnings: list[str],
    where: str,
    raw_segment: dict | None = None,
) -> dict:
    """Resolves a plan segment's view / path / projection for 360 footage."""
    angles = tags.get("angles") or {}
    projection = spec.get("projection") or "flat"
    if projection not in PROJECTIONS:
        errors.append(f"{where}: projection must be one of {', '.join(PROJECTIONS)} (got {projection!r})")
        projection = "flat"
    if moment_view is not None:
        base = parse_view(moment_view, angles, errors, where)
    elif angles:
        base = parse_view(next(iter(angles)), angles, errors, where)
    else:
        base = dict(DEFAULT_VIEW)
    view = parse_view(spec.get("view"), angles, errors, where, base) if spec.get("view") is not None else base
    overrides = {k: spec[k] for k in ("yaw", "pitch", "fov", "roll") if spec.get(k) is not None}
    if overrides:
        view = parse_view(overrides, angles, errors, where, view)
    written = raw_segment if raw_segment is not None else spec
    for k in ("frame", "zoom", "focusX", "focusY", "fit"):
        if k in written:
            warnings.append(f"{where}: '{k}' doesn't apply to 360 footage — framing comes from view/fov/path")
    path = None
    raw = spec.get("path")
    if raw is not None:
        if not isinstance(raw, list) or len(raw) < 2:
            errors.append(f"{where}: path needs at least two keyframes, e.g. [{{\"at\": 0, \"view\": \"road\"}}, {{\"at\": 2, \"view\": \"rider\"}}]")
        else:
            keys = []
            for i, k in enumerate(raw):
                w = f"{where} path[{i}]"
                if isinstance(k, str):
                    k = {"view": k}
                if not isinstance(k, dict):
                    errors.append(f"{w}: keyframes are objects like {{\"at\": 1.5, \"view\": \"rider\"}}")
                    continue
                try:
                    at = float(k.get("at", 0 if i == 0 else seconds))
                except (TypeError, ValueError):
                    errors.append(f"{w}: at must be seconds from the start of the segment")
                    continue
                if not 0 <= at <= seconds + 0.01:
                    errors.append(f"{w}: at={at:g}s is outside this {seconds:.1f}s segment")
                key = {"at": round(at, 3), **parse_view(k, angles, errors, w, view)}
                if k.get("cut") is True and i > 0:
                    key["cut"] = True
                elif k.get("cut") not in (None, False, True):
                    errors.append(f"{w}: cut must be true or false")
                keys.append(key)
            keys.sort(key=lambda x: x["at"])
            if any(b["at"] - a["at"] < 0.1 for a, b in zip(keys, keys[1:])):
                errors.append(f"{where}: path keyframes need to be at least 0.1 s apart")
            path = keys
    from .stabilize import parse_setting

    stabilize = parse_setting(spec.get("stabilize"), errors, where)
    return {"projection": projection, "view": view, "path": path, "stabilize": stabilize}


def describe(sph: dict) -> str:
    return f"360° {sph.get('width')}x{sph.get('height')} ({sph.get('source')})"
