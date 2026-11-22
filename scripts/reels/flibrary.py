"""Backend for the flibrary Mac app (flibrary/CONTRACT.md): browse footage, 360 stills, 360 exports.

`./reels flibrary list|frame|estimate|export`. Every command prints JSON on stdout (errors as
`{"error": "..."}` with exit code 1). Nothing here writes under library/ or plans/: exports go to
flibrary-exports/, caches and temp files to .cache/flibrary/.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .config import Project
from .media import BIN, ffprobe_json
from .util import ReelsError

GIB = 1024**3
MIN_FREE_AFTER = 10 * GIB  # refuse any export that would leave less than this free
EQUIRECT_BPP = 0.15  # equirect HEVC bitrate = width * height * fps * 0.15 bits (2944x1472@59.94 = 39 Mbit/s)
FLAT_MIN_BPS = 20_000_000  # flat HEVC bitrate = max(20 Mbit/s, w * h * fps / 2): the Metal helper's own rule
AUDIO_BPS = 256_000  # allowance for the copied source audio when estimating
PEAK_FLAT = 2.0  # a flat export briefly holds the video-only render beside the muxed file
KEY_NAMES = ("yaw", "pitch", "roll", "fov")
LEVEL_VERSION = 1  # bump when the levelling maths changes: it is part of cached stills and flat export names


# --------------------------------------------------------------------------
# Paths and small helpers
# --------------------------------------------------------------------------


def exports_dir(project: Project) -> Path:
    return project.root / "flibrary-exports"


def cache_dir(project: Project) -> Path:
    return project.root / ".cache" / "flibrary"


def emit(obj: dict) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


def free_bytes(path: Path) -> int:
    path = Path(path)
    while not path.exists() and path != path.parent:
        path = path.parent
    return shutil.disk_usage(path).free


def load_asset_record(project: Project, asset_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", str(asset_id or "")):
        raise ReelsError(f"bad asset id {asset_id!r}")
    p = project.assets_dir / f"{asset_id}.json"
    if not p.exists():
        raise ReelsError(f"no asset {asset_id!r} in the library")
    return json.loads(p.read_text(encoding="utf-8"))


def exact_fps(fps: float) -> float:
    """59.94 -> 60000/1001 (the cameras' NTSC rates), so frame times don't drift over a long clip."""
    nominal = round(fps)
    if nominal and abs(fps - nominal) > 1e-3 and abs(fps - nominal * 1000 / 1001) < 0.01:
        return nominal * 1000 / 1001
    return float(fps)


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

SHEET_PER_SHEET_360 = (3, 12, 520)  # cols, tiles per sheet, tile width: sheets.make_video_sheets
SHEET_HEADER, SHEET_GUTTER = 72, 10  # sheets.build_sheet with a subtitle


def sheet_files(project: Project, asset_id: str) -> list[Path]:
    """The ingest contact sheets: <id>.jpg or <id>-1.jpg, <id>-2.jpg ... (not the zoomed '-view-' ones)."""
    d = project.sheets_dir
    single = d / f"{asset_id}.jpg"
    if single.exists():
        return [single]
    found = []
    for p in d.glob(f"{asset_id}-*.jpg"):
        m = re.fullmatch(re.escape(asset_id) + r"-(\d+)\.jpg", p.name)
        if m:
            found.append((int(m.group(1)), p))
    return [p for _, p in sorted(found)]


def sheet_grid(asset: dict, sheets: list[Path]) -> dict | None:
    """How the contact sheets are tiled, so the app can hover-scrub: tile i sits in sheet i // perSheet at
    (col, row) = divmod(i % perSheet, cols); pixel origin x = gutter + col * (tileW + gutter),
    y = header + row * (tileH + gutter). Timestamps are the ones ingest used (sheets.sheet_times)."""
    from .sheets import sheet_times

    probe = asset.get("probe") or {}
    if asset.get("kind") != "video" or not sheets or not probe.get("duration"):
        return None
    sph = probe.get("sphere")
    portrait = probe.get("height", 0) > probe.get("width", 0) and not sph
    cols, per_sheet, tile_w = SHEET_PER_SHEET_360 if sph else ((6, 12, 255) if portrait else (4, 16, 390))
    times = sheet_times(probe["duration"])
    if len(sheets) != math.ceil(len(times) / per_sheet):
        return None  # sheets were made with a custom range/count: the layout can't be inferred
    try:
        from PIL import Image

        with Image.open(sheets[0]) as im:
            width, height = im.size
    except Exception:  # noqa: BLE001
        return None
    first = min(len(times), per_sheet)
    rows = math.ceil(first / cols)
    tile_h = (height - SHEET_HEADER - SHEET_GUTTER) // rows - SHEET_GUTTER
    if tile_h <= 0:
        return None
    return {"cols": cols, "rows": rows, "perSheet": per_sheet, "times": times, "tileW": tile_w, "tileH": tile_h,
            "gutter": SHEET_GUTTER, "header": SHEET_HEADER, "sheetWidth": width, "sheetHeight": height}


def existing_exports(project: Project, asset_id: str) -> list[dict]:
    d = exports_dir(project)
    out = []
    if not d.is_dir():
        return out
    for p in sorted(d.glob(f"{asset_id}-*.mp4")):
        name = p.name
        if name.startswith("."):
            continue
        if name.startswith(f"{asset_id}-equirect"):
            kind, label = "equirect", "equirect stabilized" if name.endswith("-stab.mp4") else "equirect"
        else:
            m = re.fullmatch(re.escape(asset_id) + r"-flat-(\d+x\d+)-[0-9a-f]{8}\.mp4", name)
            if not m:
                continue
            kind, label = "flat", f"flat {m.group(1)}"
        out.append({"kind": kind, "path": str(p), "bytes": p.stat().st_size, "label": label})
    return out


def asset_entry(project: Project, a: dict) -> dict:
    probe = a.get("probe") or {}
    sph = probe.get("sphere") or {}
    tags = a.get("tags") or {}
    thumb = project.thumbs_dir / f"{a['id']}.jpg"
    sheets = sheet_files(project, a["id"])
    return {
        "id": a["id"], "kind": a.get("kind", "video"),
        "media": str(project.abs(a["media"])), "originalName": a.get("originalName") or "",
        "created": a.get("created") or "",
        # numeric fields are 0 (not null) when unknown, e.g. a photo has no duration/fps
        "duration": probe.get("duration") or 0, "width": probe.get("width") or 0,
        "height": probe.get("height") or 0, "fps": probe.get("fps") or 0,
        "is360": bool(sph), "projection": sph.get("projection"),
        "thumb": str(thumb) if thumb.exists() else None,
        "sheets": [str(s) for s in sheets], "sheetGrid": sheet_grid(a, sheets),
        "tags": {k: v for k, v in tags.items() if k not in ("angles", "moments")},
        "angles": tags.get("angles") or {},
        "exports": existing_exports(project, a["id"]),
    }


def cmd_list(project: Project) -> dict:
    assets = []
    for p in project.assets_dir.glob("*.json") if project.assets_dir.exists() else []:
        try:
            a = json.loads(p.read_text(encoding="utf-8"))
            assets.append(asset_entry(project, a))
        except Exception as e:  # noqa: BLE001  a broken record must not hide the rest of the library
            print(f"skipping {p.name}: {type(e).__name__}", file=sys.stderr)
    assets.sort(key=lambda e: (e["created"], e["id"]), reverse=True)
    return {"root": str(project.root), "exportsDir": str(exports_dir(project)),
            "freeBytes": free_bytes(project.root), "assets": assets}


# --------------------------------------------------------------------------
# frame
# --------------------------------------------------------------------------


def parse_view_arg(text: str) -> dict:
    try:
        yaw, pitch, roll, fov = (float(x) for x in text.split(","))
    except ValueError as e:
        raise ReelsError("--view wants YAW,PITCH,ROLL,FOV (four numbers)") from e
    return validate_key({"yaw": yaw, "pitch": pitch, "roll": roll, "fov": fov}, "--view")


def _tag_level(angles: dict | None) -> dict:
    """Fallback levelling from the asset's tagged `forward` view: its full yaw, pitch and roll."""
    fwd = (angles or {}).get("forward")
    if not isinstance(fwd, dict):
        return {"yaw": 0.0, "pitch": 0.0, "roll": 0.0, "source": "none"}
    from .sphere import wrap_yaw

    return {"yaw": wrap_yaw(float(fwd.get("yaw", 0))), "pitch": float(fwd.get("pitch", 0)),
            "roll": float(fwd.get("roll", 0)), "source": "tag"}


def level_rotation(project: Project, a: dict) -> dict:
    """The levelling rotation of an asset as v360 angles {"yaw","pitch","roll","source"}.

    It maps the LEVELLED frame (x right, y down, z forward along the bike, horizon level, sky up) onto the
    camera's own sphere: camera ray = v360_matrix(yaw, pitch, roll) * levelled ray. Computed once per file from
    the camera's gravity vector (GPMF GRAV, mean over the whole clip) and the forward direction
    (stabilize.front_back_views: accelerometer/GPS travel direction, else the tagged `forward` view), cached in
    .cache/flibrary/level/. Without orientation data it falls back to the full tagged forward angle (yaw, pitch
    and roll), and to the sphere as shot when there is no tag either."""
    angles = (a.get("tags") or {}).get("angles")
    src = project.abs(a["media"]) if a.get("media") else None
    if a.get("kind") != "video" or not src or not src.exists():
        return _tag_level(angles)
    st = src.stat()
    cache = cache_dir(project) / "level" / f"{a['id']}.json"
    fingerprint = {"v": LEVEL_VERSION, "size": st.st_size, "mtime": int(st.st_mtime), "angles": angles}
    try:
        got = json.loads(cache.read_text(encoding="utf-8"))
        if got.get("fingerprint") == fingerprint:
            return got["level"]
    except (OSError, ValueError, KeyError):
        pass
    level = None
    try:
        from .stabilize import front_back_views, read_orientation, travel_direction

        ori = read_orientation(src, 0.0, float((a.get("probe") or {}).get("duration") or 0) + 1.0)
        if ori is not None and ori.gravity:
            travel = None
            try:
                travel = travel_direction(src, ori.gravity)
            except (ReelsError, OSError, ValueError, struct.error):
                pass
            front = front_back_views(ori.gravity, angles if isinstance(angles, dict) else None, travel=travel)[0]
            level = {"yaw": front["yaw"], "pitch": front["pitch"], "roll": front["roll"], "source": "gravity"}
    except (ReelsError, OSError, ValueError, struct.error):
        level = None
    if level is None:
        level = _tag_level(angles)
    try:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"fingerprint": fingerprint, "level": level}), encoding="utf-8")
    except OSError:
        pass
    return level


def in_source_frame(level: dict, key: dict) -> dict:
    """A key given in the levelled frame, as the equivalent yaw/pitch/roll on the camera's sphere
    (source ray = L * V * ray); other fields (fov, at, cut) pass through."""
    from .stabilize import mmul, v360_angles, v360_matrix

    m = mmul(v360_matrix(level["yaw"], level["pitch"], level["roll"]), v360_matrix(key["yaw"], key["pitch"], key["roll"]))
    yaw, pitch, roll = v360_angles(m)
    return {**key, "yaw": round(yaw, 3), "pitch": round(pitch, 3), "roll": round(roll, 3)}


def levelled_equirect_still(src: Path, t: float | None, out: Path, probe: dict, width: int, level: dict) -> None:
    """A 2:1 equirect still with the levelling rotation, the same maths as the equirect export
    (t=None: `src` is an equirect photo)."""
    from .media import run
    from .sphere import source_graph

    h = max(2, width // 2)
    rot = f"yaw={level['yaw']:.3f}:pitch={level['pitch']:.3f}:roll={level['roll']:.3f}"
    if t is None:
        vf = f"v360=input=e:output=e:{rot}:w={width}:h={h}:interp=cubic:reset_rot=1"
        run(["ffmpeg", "-v", "error", "-y", "-i", src, "-vf", vf, "-frames:v", "1", "-q:v", "3", out])
    else:
        graph, pad, inproj = source_graph(probe)
        vf = f"v360=input={inproj}:output=e:{rot}:w={width}:h={h}:interp=cubic:reset_rot=1"
        run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-filter_complex", f"{graph};[{pad}]{vf}[o]",
             "-map", "[o]", "-frames:v", "1", "-q:v", "3", out])  # fmt: skip
    if not out.exists():
        raise ReelsError(f"couldn't extract a 360 frame from {src.name}")


def cmd_level(project: Project, asset_id: str) -> dict:
    """The levelling rotation (v360 yaw/pitch/roll, degrees) of a 360 asset: camera ray = v360_matrix(it) * levelled ray."""
    a = load_asset_record(project, asset_id)
    if not (a.get("probe") or {}).get("sphere"):
        raise ReelsError(f"{asset_id} is not a 360 asset")
    return level_rotation(project, a)


def cmd_frame(project: Project, asset_id: str, t: float, width: int, view: str | None, w: int, h: int) -> dict:
    from .sphere import extract_view, photo_view

    a = load_asset_record(project, asset_id)
    sph = (a.get("probe") or {}).get("sphere")
    if not sph:
        raise ReelsError(f"{asset_id} is not a 360 asset")
    src = project.abs(a["media"])
    if not src.exists():
        raise ReelsError(f"media for {asset_id} is offline: {src}")
    if not (16 <= width <= 8192 and 16 <= w <= 8192 and 16 <= h <= 8192):
        raise ReelsError("width/w/h must be between 16 and 8192")
    is_video = a.get("kind") == "video"
    if not math.isfinite(t) or t < 0:
        raise ReelsError("--t must be a non-negative number of seconds")
    if is_video:
        t = min(t, max(0.0, float((a["probe"].get("duration") or 0)) - 0.05))
    vw = parse_view_arg(view) if view else None
    key = f"L{LEVEL_VERSION}-" + (f"w{width // 2 * 2}" if vw is None else "v" + "_".join(f"{vw[k]:g}" for k in KEY_NAMES) + f"-{w}x{h}")
    out = cache_dir(project) / "frames" / f"{asset_id}-{t:.3f}-{key}.jpg" if is_video else cache_dir(project) / "frames" / f"{asset_id}-{key}.jpg"
    if out.exists() and out.stat().st_size > 0:
        return {"path": str(out)}
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(f".{out.stem}.partial.jpg")
    try:
        probe = dict(a["probe"])
        level = level_rotation(project, a)
        if vw is None:
            levelled_equirect_still(src, t if is_video else None, tmp, probe, width // 2 * 2, level)
        elif is_video:
            extract_view(src, t, tmp, probe, in_source_frame(level, vw), w, h)
        else:
            photo_view(src, tmp, in_source_frame(level, vw), w, h)
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return {"path": str(out)}


# --------------------------------------------------------------------------
# Jobs: validation, naming, size estimate, disk guard
# --------------------------------------------------------------------------


def _finite(v: Any, name: str) -> float:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        raise ReelsError(f"{name} must be a finite number (got {v!r})")
    return float(v)


def validate_key(k: dict, where: str = "key") -> dict:
    """One camera key. The ranges are the Metal helper's validation rules (Request.swift)."""
    if not isinstance(k, dict):
        raise ReelsError(f"{where} must be an object with yaw/pitch/roll/fov")
    out = {n: _finite(k.get(n, 0 if n != "fov" else 90), f"{where}.{n}") for n in KEY_NAMES}
    if abs(out["pitch"]) > 90:
        raise ReelsError(f"{where}.pitch={out['pitch']:g} is outside -90..90")
    if abs(out["roll"]) > 180:
        raise ReelsError(f"{where}.roll={out['roll']:g} is outside -180..180")
    if not 10 <= out["fov"] <= 150:
        raise ReelsError(f"{where}.fov={out['fov']:g} is outside 10..150")
    return out


def validate_keys(raw: Any, seconds: float) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise ReelsError("flat exports need keys: [{at, yaw, pitch, roll, fov}, ...]")
    keys: list[dict] = []
    for i, k in enumerate(raw):
        key = validate_key(k, f"keys[{i}]")
        key["at"] = _finite(k.get("at", 0), f"keys[{i}].at")
        if key["at"] < 0 or key["at"] > seconds + 0.01:
            raise ReelsError(f"keys[{i}].at={key['at']:g}s is outside the {seconds:.2f}s export")
        if i and key["at"] <= keys[-1]["at"]:
            raise ReelsError("keys[].at must be strictly increasing")
        if k.get("cut") is True and i:
            key["cut"] = True
        keys.append(key)
    return keys


def validate_job(project: Project, job: Any) -> tuple[dict, dict]:
    """(normalised job, asset record). Raises ReelsError with a plain message."""
    if not isinstance(job, dict):
        raise ReelsError("job must be a JSON object")
    a = load_asset_record(project, job.get("id"))
    probe = a.get("probe") or {}
    sph = probe.get("sphere")
    if a.get("kind") != "video" or not sph:
        raise ReelsError(f"{a['id']} is not a 360 video")
    kind = job.get("kind")
    if kind not in ("equirect", "flat"):
        raise ReelsError("kind must be \"equirect\" or \"flat\"")
    duration = float(probe.get("duration") or 0)
    t_in = _finite(job.get("in", 0.0), "in")
    t_out = _finite(job.get("out", duration), "out")
    t_out = min(t_out, duration)
    if t_in < 0 or t_out - t_in < 0.1:
        raise ReelsError(f"in/out must leave at least 0.1 s (in={t_in:g}, out={t_out:g}, clip is {duration:g}s)")
    stabilize = job.get("stabilize", False)
    if not isinstance(stabilize, bool):
        raise ReelsError("stabilize must be true or false")
    fps_src = exact_fps(float(probe.get("fps") or 30))
    out: dict = {"id": a["id"], "kind": kind, "stabilize": stabilize, "in": round(t_in, 3), "out": round(t_out, 3)}
    if kind == "equirect":
        out["width"], out["height"], out["fps"] = int(probe["width"]), int(probe["height"]), fps_src
    else:
        w, h = job.get("width", 1080), job.get("height", 1920)
        fps = job.get("fps", 30)
        for name, v, lo, hi in (("width", w, 2, 8192), ("height", h, 2, 8192), ("fps", fps, 1, 240)):
            if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
                raise ReelsError(f"{name} must be a whole number {lo}..{hi} (got {v!r})")
        if w % 2 or h % 2:
            raise ReelsError("width and height must be even")
        out.update(width=w, height=h, fps=fps, keys=validate_keys(job.get("keys"), t_out - t_in))
    return out, a


def output_name(job: dict) -> str:
    if job["kind"] == "equirect":
        return f"{job['id']}-equirect{'-stab' if job['stabilize'] else ''}.mp4"
    # The hash covers every field that changes the picture, so a different range/path never reuses a file.
    material = {k: job[k] for k in ("id", "stabilize", "in", "out", "width", "height", "fps", "keys")} | {"level": LEVEL_VERSION}
    digest = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:8]
    return f"{job['id']}-flat-{job['width']}x{job['height']}-{digest}.mp4"


def video_bps(job: dict) -> int:
    px_fps = job["width"] * job["height"] * job["fps"]
    return int(px_fps * EQUIRECT_BPP) if job["kind"] == "equirect" else int(max(FLAT_MIN_BPS, px_fps / 2))


def estimate_bytes(job: dict, has_audio: bool = True) -> int:
    seconds = job["out"] - job["in"]
    return int(seconds * (video_bps(job) + (AUDIO_BPS if has_audio else 0)) / 8 * 1.03)


def disk_check(project: Project, job: dict, has_audio: bool = True, free: int | None = None) -> dict:
    size = estimate_bytes(job, has_audio)
    peak = int(size * PEAK_FLAT)  # the Metal path briefly holds the video-only render beside the muxed file
    free = free_bytes(project.root) if free is None else free
    ok = free - peak >= MIN_FREE_AFTER
    res: dict = {"bytes": size, "freeBytes": free, "ok": ok}
    if not ok:
        res["reason"] = (f"not enough disk space: the export needs about {peak / GIB:.1f} GB and only {free / GIB:.1f} GB is free; "
                         f"at least {MIN_FREE_AFTER / GIB:.0f} GB must stay free")
    return res


def load_job(path: str) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ReelsError(f"can't read job file {path}: {e}") from e


def cmd_estimate(project: Project, job_path: str) -> dict:
    job, a = validate_job(project, load_job(job_path))
    return disk_check(project, job, bool(a["probe"].get("hasAudio")))


# --------------------------------------------------------------------------
# Spherical Video V1 metadata (MP4 boxes)
# --------------------------------------------------------------------------

SPHERICAL_UUID = bytes.fromhex("ffcc8263f8554a938814587a02521fdd")
SPHERICAL_XMP = (
    '<?xml version="1.0"?>'
    '<rdf:SphericalVideo xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" '
    'xmlns:GSpherical="http://ns.google.com/videos/1.0/spherical/">'
    "<GSpherical:Spherical>true</GSpherical:Spherical>"
    "<GSpherical:Stitched>true</GSpherical:Stitched>"
    "<GSpherical:StitchingSoftware>flibrary</GSpherical:StitchingSoftware>"
    "<GSpherical:ProjectionType>equirectangular</GSpherical:ProjectionType>"
    "</rdf:SphericalVideo>"
)


def spherical_uuid_box() -> bytes:
    payload = SPHERICAL_UUID + SPHERICAL_XMP.encode("utf-8")
    return struct.pack(">I4s", 8 + len(payload), b"uuid") + payload


def parse_boxes(buf: bytes | memoryview, start: int = 0, end: int | None = None) -> list[tuple[bytes, int, int, int]]:
    """Boxes in buf[start:end] as (type, offset, size, header_size); handles 64-bit and to-end sizes."""
    end = len(buf) if end is None else end
    out, pos = [], start
    while pos + 8 <= end:
        size, typ = struct.unpack_from(">I4s", buf, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                raise ReelsError("truncated MP4 box header")
            size = struct.unpack_from(">Q", buf, pos + 8)[0]
            header = 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            raise ReelsError(f"corrupt MP4 box {typ!r} at {pos}")
        out.append((bytes(typ), pos, size, header))
        pos += size
    return out


def _child(buf: bytes, box: tuple, typ: bytes):
    return next((b for b in parse_boxes(buf, box[1] + box[3], box[1] + box[2]) if b[0] == typ), None)


def patch_moov(moov: bytes, moov_pos: int, uuid_box: bytes) -> bytes:
    """Pure: a moov box with `uuid_box` appended inside the first video trak (replacing any box with the
    same UUID). Sizes of trak and moov are fixed up, and every stco/co64 chunk offset at or beyond the end
    of the old trak (absolute file position moov_pos + ...) moves by the size change. With moov after mdat
    no offset is that large, so nothing shifts; with moov before mdat every mdat offset does."""
    top = parse_boxes(moov)
    if len(top) != 1 or top[0][0] != b"moov" or top[0][3] != 8:
        raise ReelsError("expected a single plain moov box")
    moov_box = top[0]
    trak = None
    for b in parse_boxes(moov, 8, len(moov)):
        if b[0] != b"trak":
            continue
        hdlr = None
        mdia = _child(moov, b, b"mdia")
        if mdia:
            hdlr = _child(moov, mdia, b"hdlr")
        if hdlr and moov[hdlr[1] + 16 : hdlr[1] + 20] == b"vide":
            trak = b
            break
    if trak is None:
        raise ReelsError("no video track in the MP4")
    if trak[3] != 8:
        raise ReelsError("64-bit trak box is not supported")
    t_off, t_size = trak[1], trak[2]
    body = bytearray()
    for b in parse_boxes(moov, t_off + 8, t_off + t_size):
        if b[0] == b"uuid" and moov[b[1] + b[3] : b[1] + b[3] + 16] == SPHERICAL_UUID:
            continue  # re-injection replaces the old box
        body += moov[b[1] : b[1] + b[2]]
    body += uuid_box
    new_trak = struct.pack(">I4s", 8 + len(body), b"trak") + bytes(body)
    delta = len(new_trak) - t_size
    new = bytearray(moov[:t_off] + new_trak + moov[t_off + t_size :])
    struct.pack_into(">I", new, 0, moov_box[2] + delta)
    threshold = moov_pos + t_off + t_size  # first byte after the old trak, as a file position
    if delta:
        _shift_chunk_offsets(new, threshold, delta)
    return bytes(new)


def _shift_chunk_offsets(moov: bytearray, threshold: int, delta: int) -> None:
    """Add delta to every stco/co64 entry >= threshold, in place (walks moov > trak > mdia > minf > stbl)."""

    def walk(start: int, end: int, path: tuple) -> None:
        for typ, off, size, hdr in parse_boxes(moov, start, end):
            if typ in (b"stco", b"co64"):
                count = struct.unpack_from(">I", moov, off + hdr + 4)[0]
                fmt, step = (">I", 4) if typ == b"stco" else (">Q", 8)
                for i in range(count):
                    p = off + hdr + 8 + i * step
                    v = struct.unpack_from(fmt, moov, p)[0]
                    if v >= threshold:
                        if typ == b"stco" and v + delta > 0xFFFFFFFF:
                            raise ReelsError("chunk offset overflows stco; file too large to patch")
                        struct.pack_into(fmt, moov, p, v + delta)
            elif typ in (b"trak", b"mdia", b"minf", b"stbl"):
                walk(off + hdr, off + size, path + (typ,))

    walk(8, len(moov), ())


def inject_spherical_v1(data: bytes) -> bytes:
    """Pure: the bytes of an MP4 with Spherical Video V1 equirectangular metadata in its video trak."""
    boxes = parse_boxes(data)
    moovs = [b for b in boxes if b[0] == b"moov"]
    if len(moovs) != 1:
        raise ReelsError("expected exactly one moov box")
    _, pos, size, _ = moovs[0]
    new = patch_moov(data[pos : pos + size], pos, spherical_uuid_box())
    return data[:pos] + new + data[pos + size :]


def inject_spherical_file(path: Path) -> None:
    """The file version, without loading the media: moov is read (it is small); with moov last the
    file is patched in place (truncate and rewrite moov), otherwise it is streamed to a sibling and swapped."""
    path = Path(path)
    with open(path, "rb") as f:
        boxes, pos = [], 0
        end = path.stat().st_size
        while pos + 8 <= end:
            f.seek(pos)
            head = f.read(16)
            size, typ = struct.unpack_from(">I4s", head, 0)
            if size == 1:
                size = struct.unpack_from(">Q", head, 8)[0]
            elif size == 0:
                size = end - pos
            if size < 8 or pos + size > end:
                raise ReelsError(f"corrupt MP4 box {typ!r} at {pos}")
            boxes.append((bytes(typ), pos, size))
            pos += size
        moovs = [b for b in boxes if b[0] == b"moov"]
        if len(moovs) != 1:
            raise ReelsError("expected exactly one moov box")
        _, mpos, msize = moovs[0]
        f.seek(mpos)
        new = patch_moov(f.read(msize), mpos, spherical_uuid_box())
        if boxes[-1][0] == b"moov":
            pass
        else:
            tmp = path.with_name(f".{path.name}.inject")
            try:
                with open(tmp, "wb") as g:
                    f.seek(0)
                    shutil.copyfileobj(_Limited(f, mpos), g, 1 << 20)
                    g.write(new)
                    f.seek(mpos + msize)
                    shutil.copyfileobj(f, g, 1 << 20)
                os.replace(tmp, path)
            finally:
                tmp.unlink(missing_ok=True)
            return
    with open(path, "r+b") as f:
        f.seek(mpos)
        f.truncate(mpos)
        f.write(new)


class _Limited:
    def __init__(self, f, n: int):
        self.f, self.n = f, n

    def read(self, size: int = -1) -> bytes:
        size = self.n if size < 0 else min(size, self.n)
        chunk = self.f.read(size)
        self.n -= len(chunk)
        return chunk


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


class Cancelled(Exception):
    pass


class Runner:
    """Tracks the child process and temp files so SIGTERM/SIGINT can kill and clean up."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.temps: list[Path] = []
        self.cancelled = False

    def install(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    def _on_signal(self, *_: Any) -> None:
        self.cancelled = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def cleanup(self) -> None:
        for p in self.temps:
            p.unlink(missing_ok=True)

    def wait(self, proc: subprocess.Popen, tick=None) -> int:
        self.proc = proc
        while proc.poll() is None:
            if self.cancelled:
                proc.terminate()
                try:
                    proc.wait(5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                raise Cancelled
            if tick:
                tick()
            time.sleep(0.25)
        if self.cancelled:
            raise Cancelled
        return proc.returncode


def _ffmpeg_with_progress(runner: Runner, cmd: list, expected: float, base: float = 0.0, span: float = 1.0,
                          log_path: Path | None = None) -> None:
    """Runs ffmpeg with `-progress pipe:1` and prints {"progress": x} as out_time reaches expected seconds."""
    import threading

    cmd = [str(c) for c in cmd]
    cmd[0] = BIN.get(cmd[0], cmd[0])
    errlog = open(log_path, "w+") if log_path else subprocess.DEVNULL
    proc = subprocess.Popen(cmd[:1] + ["-nostats", "-progress", "pipe:1"] + cmd[1:], stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=errlog, text=True)
    last = [-1.0]

    def reader() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):  # both are microseconds
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                p = base + span * min(0.99, max(0.0, us / 1e6 / expected))
                if p - last[0] >= 0.01:
                    last[0] = p
                    emit({"progress": round(p, 3)})

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    try:
        code = runner.wait(proc)
    finally:
        th.join(2)
    if code:
        tail = ""
        if log_path:
            errlog.seek(0)  # type: ignore[union-attr]
            tail = errlog.read()[-1500:]  # type: ignore[union-attr]
        raise ReelsError(f"ffmpeg failed (exit {code}): {tail.strip()}")


def equirect_filter(inproj: str, w: int, h: int, fps: float, frames: int, base: dict, corrections: list | None,
                    cmd_file: Path) -> str:
    """v360 to a full equirect frame with the levelling rotation; with `corrections` (stabilize, one rotation per
    frame, same maths as sphere.camera_filter) the angles are updated per frame through sendcmd."""
    from .stabilize import mmul, v360_angles, v360_matrix

    def f(p: dict) -> str:
        return f"v360=input={inproj}:output=e:yaw={p['yaw']:.3f}:pitch={p['pitch']:.3f}:roll={p['roll']:.3f}:w={w}:h={h}:interp=cubic:reset_rot=1"

    if not corrections:
        return f(base)
    base_m = v360_matrix(base["yaw"], base["pitch"], base["roll"])

    def params(n: int) -> dict:
        y, p, r = v360_angles(mmul(corrections[min(n, len(corrections) - 1)], base_m))
        return {"yaw": y, "pitch": p, "roll": r}

    first = params(0)
    cur = {k: f"{v:.3f}" for k, v in first.items()}
    lines = []
    for n in range(1, frames):
        p = params(n)
        cmds = []
        for k in ("yaw", "pitch", "roll"):
            v = f"{p[k]:.3f}"
            if v != cur[k]:
                cmds.append(f"v360@cam {k} {v}")
                cur[k] = v
        if cmds:
            lines.append(f"{max(0.0, (n - 0.5) / fps):.4f} " + ", ".join(cmds) + ";")
    if not lines:
        return f(first)
    cmd_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f"sendcmd=f={cmd_file},{f(first).replace('v360=', 'v360@cam=', 1)}"


def _corrections(src: Path, t_in: float, fps: float, frames: int) -> list | None:
    from .stabilize import parse_setting, segment_corrections

    errors: list[str] = []
    setting = parse_setting(True, errors, "stabilize")  # the default smooth setting
    return segment_corrections(src, t_in, 1.0, fps, frames, setting)  # type: ignore[arg-type]


def _audio_args(a: dict) -> list:
    return ["-map", "0:a:0", "-c:a", "copy"] if (a["probe"].get("hasAudio")) else ["-an"]


def _metal_supported(project: Project, a: dict, src: Path) -> tuple[Path | None, list]:
    from . import sphere_metal

    binary = sphere_metal.executable(project.code_root)
    if not binary or os.environ.get("REELS_SPHERE_BACKEND") == "v360":
        return None, []
    streams = ffprobe_json(src).get("streams", [])
    if sphere_metal.capability(a["probe"], streams):
        return None, []
    return binary, streams


def metal_request(a: dict, job: dict, src: Path, dest: Path, streams: list, frames: int, corr_quats: list | None,
                  keys: list | None = None) -> dict:
    """A reels-sphere render request. Flat: `keys` (camera path, already in the source frame). Equirect: one constant
    key whose yaw/pitch/roll is the output-sphere (levelling) rotation, at full resolution."""
    sph = a["probe"]["sphere"]
    idx = sph["tracks"] if sph["projection"] == "gopro-eac" else [next(
        s["index"] for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"))]
    vids = [next(s for s in streams if s["index"] == i) for i in idx]
    fps = job["fps"]
    nominal = int(round(fps))
    ntsc = abs(fps - nominal) > 1e-6  # 59.94 -> 60 with fpsDivisor 1001
    req = {
        "version": 1, "source": str(src.resolve()), "destination": str(dest.resolve()),
        "sourceProjection": sph["projection"], "trackIDs": [int(str(s["id"]), 0) for s in vids],
        "sourceIn": job["in"], "sourceOut": job["out"], "speed": 1.0,
        "width": job["width"], "height": job["height"], "fps": nominal, "frames": frames,
        "projection": job["kind"], "keys": [dict(k) for k in (keys if keys is not None else job["keys"])],
        "corrections": None if corr_quats is None else {"sourceStart": job["in"], "sourceStep": 1.0 / fps, "quaternions": corr_quats},
        "fullRange": vids[0].get("color_range") == "pc" or str(vids[0].get("pix_fmt", "")).startswith("yuvj"),
        "codec": "hevc",
    }
    if ntsc:
        req["fpsDivisor"] = 1001
    return req


def _equirect_keys(level: dict) -> list:
    return [{"at": 0, "yaw": level["yaw"], "pitch": level["pitch"], "roll": level["roll"], "fov": 90}]


def _stab_quats(src: Path, job: dict, frames: int, setting: dict | None) -> list | None:
    if not setting:
        return None
    from .stabilize import correction_quats, margin, read_orientation

    pad = margin(setting)
    ori = read_orientation(src, max(0.0, job["in"] - pad), job["out"] + pad)
    if ori is None:
        print(f"warning: no camera orientation data in {src.name}; exporting without stabilization", file=sys.stderr)
        return None
    return correction_quats(ori, [job["in"] + n / job["fps"] for n in range(frames)], setting)


def _try_metal(runner: Runner, project: Project, a: dict, job: dict, src: Path, tmp: Path, work: Path, frames: int,
               quats: list | None, keys: list | None) -> bool:
    """Renders the video with the Metal helper and muxes the audio. False (with a warning) means use ffmpeg."""
    from .media import run

    binary, streams = _metal_supported(project, a, src)
    if not binary:
        return False
    video = work / "metal.mp4"
    req = work / "request.json"
    req.write_text(json.dumps(metal_request(a, job, src, video, streams, frames, quats, keys), allow_nan=False), encoding="utf-8")
    runner.temps.append(video)
    chk = run([binary, "--check", req], check=False)
    if chk.returncode != 0:
        print(f"warning: Metal unavailable ({chk.stderr.strip()[-200:]}); using ffmpeg", file=sys.stderr)
        return False
    estimate = estimate_bytes(job, False)
    proc = subprocess.Popen([str(binary), "--render", str(req)], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)

    def tick() -> None:
        size = video.stat().st_size if video.exists() else 0
        p = round(0.9 * min(1.0, size / max(1, estimate)), 3)
        if p - tick.last >= 0.02:  # type: ignore[attr-defined]
            tick.last = p  # type: ignore[attr-defined]
            emit({"progress": p})

    tick.last = -1.0  # type: ignore[attr-defined]
    code = runner.wait(proc, tick)
    if code == 0 and video.exists():
        _finish_video(runner, a, job, src, video, tmp, job["out"] - job["in"], frames)
        return True
    print(f"warning: Metal render failed ({(proc.stderr.read() or '').strip()[-300:]}); using ffmpeg", file=sys.stderr)
    video.unlink(missing_ok=True)
    return False


def export_equirect(project: Project, runner: Runner, job: dict, a: dict, tmp: Path, work: Path) -> None:
    from .grade import ENCODE_TAGS
    from .sphere import source_graph

    probe, src = a["probe"], project.abs(a["media"])
    fps, dur = job["fps"], job["out"] - job["in"]
    frames = max(1, int(dur * fps + 1e-6))
    level = level_rotation(project, a)
    setting = parse_setting_default() if job["stabilize"] else None
    if _try_metal(runner, project, a, job, src, tmp, work, frames, _stab_quats(src, job, frames, setting), _equirect_keys(level)):
        inject_spherical_file(tmp)
        return
    # ffmpeg v360 fallback (slow when stabilized: the remap table is rebuilt every frame)
    graph, pad, inproj = source_graph(probe)
    corr = None
    if setting:
        corr = _corrections(src, job["in"], fps, frames)
        if corr is None:
            print(f"warning: no camera orientation data in {src.name}; exporting without stabilization", file=sys.stderr)
    cam = equirect_filter(inproj, job["width"], job["height"], fps, frames, level, corr, work / f"{tmp.stem}.cam.cmd")
    fg = f"{graph};[{pad}]setpts=PTS-STARTPTS,{cam},format=yuv420p[v]"
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{job['in']:.6f}", "-t", f"{dur:.6f}", "-i", src,
           "-filter_complex", fg, "-map", "[v]", *_audio_args(a), "-frames:v", frames,
           "-c:v", "hevc_videotoolbox", "-b:v", video_bps(job), "-tag:v", "hvc1", *ENCODE_TAGS, "-f", "mp4", tmp]  # fmt: skip
    _ffmpeg_with_progress(runner, cmd, dur, 0.0, 0.97, work / "ffmpeg.log")
    inject_spherical_file(tmp)


def parse_setting_default() -> dict:
    from .stabilize import parse_setting

    return parse_setting(True, [], "stabilize")  # type: ignore[return-value]


def export_flat(project: Project, runner: Runner, job: dict, a: dict, tmp: Path, work: Path) -> None:
    from .grade import ENCODE_TAGS
    from .sphere import camera_filter, source_graph

    probe, src = a["probe"], project.abs(a["media"])
    fps, dur = job["fps"], job["out"] - job["in"]
    frames = max(1, int(dur * fps + 1e-6))
    level = level_rotation(project, a)
    keys = [in_source_frame(level, k) for k in job["keys"]]  # job keys are in the levelled frame
    setting = parse_setting_default() if job["stabilize"] else None
    if _try_metal(runner, project, a, job, src, tmp, work, frames, _stab_quats(src, job, frames, setting), keys):
        return
    # ffmpeg v360 fallback
    graph, pad, inproj = source_graph(probe)
    stab = None
    if setting:
        from .stabilize import segment_corrections

        stab = segment_corrections(src, job["in"], 1.0, fps, frames, setting)
        if stab is None:
            print(f"warning: no camera orientation data in {src.name}; exporting without stabilization", file=sys.stderr)
    spec = {"projection": "flat", "view": keys[0], "path": keys if len(keys) > 1 else None}
    cam = camera_filter(spec, inproj, job["width"], job["height"], fps, frames, work / f"{tmp.stem}.cam.cmd", stab=stab)
    if (work / f"{tmp.stem}.cam.cmd").exists():  # sendcmd resolves the file relative to cwd; use the absolute path
        cam = cam.replace(f"sendcmd=f={tmp.stem}.cam.cmd", f"sendcmd=f={work / (tmp.stem + '.cam.cmd')}")
    fg = f"{graph};[{pad}]setpts=PTS-STARTPTS,fps={fps},{cam},format=yuv420p[v]"
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{job['in']:.6f}", "-t", f"{dur:.6f}", "-i", src,
           "-filter_complex", fg, "-map", "[v]", *_audio_args(a), "-frames:v", frames,
           "-c:v", "hevc_videotoolbox", "-b:v", video_bps(job), "-tag:v", "hvc1", *ENCODE_TAGS, "-f", "mp4", tmp]  # fmt: skip
    _ffmpeg_with_progress(runner, cmd, dur, 0.0, 1.0, work / "ffmpeg.log")


def _finish_video(runner: Runner, a: dict, job: dict, src: Path, video: Path, tmp: Path, dur: float, frames: int) -> None:
    """Adds the source audio for the range to the Metal render (video-only) and moves it to the temp name."""
    if not a["probe"].get("hasAudio"):
        video.replace(tmp)
        return
    cmd = ["ffmpeg", "-v", "error", "-y", "-i", video, "-ss", f"{job['in']:.6f}", "-t", f"{frames / job['fps']:.6f}", "-i", src,
           "-map", "0:v:0", "-map", "1:a:0", "-c", "copy", "-tag:v", "hvc1", "-movflags", "+faststart", "-f", "mp4", tmp]
    _ffmpeg_with_progress(runner, cmd, dur, 0.92, 0.07, None)


def cmd_export(project: Project, job_path: str) -> int:
    job, a = validate_job(project, load_job(job_path))
    src = project.abs(a["media"])
    if not src.exists():
        raise ReelsError(f"media for {a['id']} is offline: {src}")
    out_dir = exports_dir(project)
    final = out_dir / output_name(job)
    if final.exists() and final.stat().st_size > 0:
        emit({"progress": 1.0})
        emit({"done": True, "path": str(final)})
        return 0
    guard = disk_check(project, job, bool(a["probe"].get("hasAudio")))
    if not guard["ok"]:
        raise ReelsError(guard["reason"])
    out_dir.mkdir(parents=True, exist_ok=True)
    work = cache_dir(project) / "work" / f"{final.stem}-{os.getpid()}"
    work.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / f".{final.name}.partial"
    runner = Runner()
    runner.temps += [tmp, work / "ffmpeg.log"]
    runner.install()
    try:
        emit({"progress": 0.0})
        (export_equirect if job["kind"] == "equirect" else export_flat)(project, runner, job, a, tmp, work)
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise ReelsError("the export produced no file")
        tmp.replace(final)
    except Cancelled:
        runner.cleanup()
        emit({"error": "cancelled"})
        return 1
    except BaseException:
        runner.cleanup()
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)
    emit({"progress": 1.0})
    emit({"done": True, "path": str(final)})
    return 0


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def run_command(project: Project, args) -> int:
    try:
        if args.flib_cmd == "list":
            print(json.dumps(cmd_list(project), ensure_ascii=False))
        elif args.flib_cmd == "frame":
            print(json.dumps(cmd_frame(project, args.id, args.t, args.width, args.view, args.w, args.h)))
        elif args.flib_cmd == "level":
            print(json.dumps(cmd_level(project, args.id)))
        elif args.flib_cmd == "estimate":
            print(json.dumps(cmd_estimate(project, args.job)))
        else:
            return cmd_export(project, args.job)
        return 0
    except ReelsError as e:
        emit({"error": str(e)})
        return 1


def add_commands(sub) -> None:
    p = sub.add_parser("flibrary", help="backend for the flibrary app: list footage, 360 stills, 360 exports (JSON)")
    fs = p.add_subparsers(dest="flib_cmd", required=True)
    fs.add_parser("list", help="every asset with thumbs, sheets, tags, angles and existing exports")
    s = fs.add_parser("frame", help="a cached JPEG still of a 360 asset")
    s.add_argument("id")
    s.add_argument("--t", type=float, default=0.0, help="seconds into the clip")
    s.add_argument("--width", type=int, default=2048, help="equirect still width")
    s.add_argument("--view", help="YAW,PITCH,ROLL,FOV: a flat reframe as in the flat export")
    s.add_argument("--w", type=int, default=1080)
    s.add_argument("--h", type=int, default=1920)
    s = fs.add_parser("level", help="the levelling rotation of a 360 asset as v360 yaw/pitch/roll")
    s.add_argument("id")
    s = fs.add_parser("estimate", help="size and disk check for a job.json")
    s.add_argument("job")
    s = fs.add_parser("export", help="run a job.json; progress as JSON lines")
    s.add_argument("job")
    p.set_defaults(fn=run_command)
