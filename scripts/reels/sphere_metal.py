"""Offline Metal capability gate. No fallback after a supported render starts.

The helper is a trusted installed runtime, also pinned explicitly into frozen
editor workers. Python owns CORI smoothing and cameraWindow compilation; Swift
uses the viewer's W4 quaternion evaluator and source-clock corrections.
"""
from __future__ import annotations

import json
import os
import platform
from pathlib import Path

from .media import ffprobe_json, run
from .util import ReelsError, log


def executable(code_root: Path) -> Path | None:
    if platform.system() != "Darwin":
        return None
    # The broker supplies this exact trusted binary to frozen renderer snapshots.
    value = os.environ.get("REELS_SPHERE")
    path = Path(value) if value else Path(code_root) / ".cache/editor/bin/reels-sphere"
    if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) and path.with_name("sphere.metallib").is_file():
        return path.resolve()
    return None


def capability(probe: dict, streams: list[dict]) -> str | None:
    """Return the reason for v360, never guess colour/layout from a research table."""
    sphere = probe.get("sphere") or {}
    projection = sphere.get("projection")
    if projection not in ("gopro-eac", "equirect"):
        return "source is not a supported spherical video"
    if probe.get("hdr"):
        return "HDR tone mapping remains in v360"
    if projection == "gopro-eac":
        h, o = sphere["face"], sphere["overlap"]
        if h % 4 or o % 8 or not 0 < o <= h // 4:
            return "GoPro seam crops are not chroma-aligned"
        indices = sphere["tracks"]
        videos = [s for i in indices for s in streams if s.get("index") == i]
        if len(videos) != 2 or any((s.get("width"), s.get("height")) != (3*h+o, h) for s in videos):
            return "GoPro track layout does not match the proven source transform"
    else:
        videos = [s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")][:1]
    if not videos:
        return "source video track is missing"
    for s in videos:
        if s.get("codec_name") not in ("h264", "hevc") or s.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
            return "only hardware-decodable 8-bit 4:2:0 H.264/HEVC is supported"
        if any(s.get(k) not in (None, "unknown", "bt709") for k in ("color_space", "color_primaries", "color_transfer")):
            return "non-BT.709 colour conversion remains in v360"
        if s.get("color_space") in (None, "unknown") and int(s.get("width", 0)) * int(s.get("height", 0)) <= 720 * 576:
            return "untagged SD uses staging's BT.601 colour interpretation"
        if float(s.get("start_time", 0)) != 0:
            return "nonzero source track origin remains in v360"
        if any(d.get("rotation", 0) != 0 for d in s.get("side_data_list", [])):
            return "rotated source track remains in v360"
        try:
            int(str(s["id"]), 0)
        except (KeyError, ValueError):
            return "container lacks explicit MP4 track IDs"
    if len({s.get("color_range", "pc" if s["pix_fmt"].startswith("yuvj") else "tv") for s in videos}) != 1:
        return "GoPro tracks have different colour ranges"
    return None


def render(project, sg, destination: Path, fps: int, width: int, height: int, work: Path) -> dict | None:
    backend = os.environ.get("REELS_SPHERE_BACKEND", "auto")
    if backend not in ("auto", "metal", "v360"):
        raise ReelsError("REELS_SPHERE_BACKEND must be auto, metal or v360")
    if backend == "v360":
        log("  sphere renderer: v360 (explicit backend)")
        return None
    binary = executable(project.code_root)
    reason = None if binary else "reels-sphere is not built on this macOS runtime"
    src = project.abs(sg.asset["media"])
    streams = ffprobe_json(src).get("streams", []) if binary else []
    if reason is None:
        reason = capability(sg.asset["probe"], streams)
    if reason:
        if backend == "metal":
            raise ReelsError(f"Metal required: {reason}")
        log(f"  sphere renderer: v360 ({reason})")
        return None
    spec, sphere = sg.spec, sg.spec["sphere"]
    source = sg.asset["probe"]["sphere"]
    indices = source["tracks"] if source["projection"] == "gopro-eac" else [next(s["index"] for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"))]
    videos = [next(s for s in streams if s["index"] == i) for i in indices]
    correction = None
    if sphere.get("stabilize"):
        from .stabilize import read_orientation, correction_quats, margin
        setting = sphere["stabilize"]
        start, step = spec["in"], spec["speed"] / fps
        end = start + (sg.frames - 1) * step
        pad = margin(setting)
        ori = read_orientation(src, max(0, start-pad), end+pad)
        if ori is not None:
            correction = {"sourceStart": start, "sourceStep": step,
                          "quaternions": correction_quats(ori, [start+n*step for n in range(sg.frames)], setting)}
        else:
            log(f"  segment {sg.index + 1}: no camera orientation data in {src.name} — not stabilized")
    request = {
        "version": 1, "source": str(src.resolve()), "destination": str(destination.resolve()),
        "sourceProjection": source["projection"], "trackIDs": [int(str(s["id"]), 0) for s in videos],
        "sourceIn": spec["in"], "sourceOut": spec["out"], "speed": spec["speed"],
        "width": width, "height": height, "fps": fps, "frames": sg.frames,
        "projection": sphere["projection"], "keys": sphere.get("path") or [{"at": 0, **sphere["view"]}],
        "corrections": correction, "fullRange": videos[0].get("color_range") == "pc" or videos[0]["pix_fmt"].startswith("yuvj"),
        "codec": "h264",
    }
    path = work / "request.json"
    path.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
    check = run([binary, "--check", path], check=False)
    if check.returncode == 3:
        reason = check.stderr.strip()
        if backend == "metal":
            raise ReelsError(f"Metal required: {reason}")
        log(f"  sphere renderer: v360 ({reason})")
        return None
    if check.returncode:
        raise ReelsError(f"reels-sphere capability check failed: {check.stderr.strip()}")
    result = run([binary, "--render", path])
    try:
        report = json.loads(result.stdout)
        if report["renderer"] != "metal" or report["frames"] != sg.frames:
            raise ValueError("wrong renderer/frame count")
    except (ValueError, KeyError) as exc:
        raise ReelsError("reels-sphere returned invalid execution evidence") from exc
    log(f"  sphere renderer: metal ({report['encoder']}, {report['frames']} frames, {report['wallSeconds']:.3f}s)")
    return report
