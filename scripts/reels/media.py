from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Any

from .util import ReelsError, warn

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".mts", ".m2ts", ".webm", ".360"}
PHOTO_EXT = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".tif", ".tiff"}
# GoPro low-res proxies/thumbnails, Insta360 raw, and sidecars we never ingest.
IGNORE_EXT = {".lrv", ".thm", ".insv", ".insp", ".xmp", ".aae", ".json", ".txt", ".srt", ".gpx", ".fit", ".dng"}


# Which ffmpeg/ffprobe binaries to run. Overridable with config tools.ffmpeg /
# tools.ffprobe or REELS_FFMPEG / REELS_FFPROBE; see configure() and hdr_filters().
BIN: dict[str, str] = {
    "ffmpeg": os.environ.get("REELS_FFMPEG") or "ffmpeg",
    "ffprobe": os.environ.get("REELS_FFPROBE") or "ffprobe",
}
# Homebrew's slim `ffmpeg` lacks zimg (zscale), which HDR tone-mapping needs;
# `brew install ffmpeg-full` provides it at one of these keg-only paths.
FULL_FFMPEG_DIRS = ["/opt/homebrew/opt/ffmpeg-full/bin", "/usr/local/opt/ffmpeg-full/bin"]


def configure(ffmpeg: str | None = None, ffprobe: str | None = None) -> None:
    if ffmpeg and not os.environ.get("REELS_FFMPEG"):
        BIN["ffmpeg"] = str(Path(ffmpeg).expanduser())
    if ffprobe and not os.environ.get("REELS_FFPROBE"):
        BIN["ffprobe"] = str(Path(ffprobe).expanduser())
    ffmpeg_filters.cache_clear()


def which(name: str) -> str | None:
    return shutil.which(BIN.get(name, name))


def require(name: str) -> str:
    path = which(name)
    if not path:
        hint = {"ffmpeg": "brew install ffmpeg", "ffprobe": "brew install ffmpeg", "node": "brew install node"}.get(name, "")
        raise ReelsError(f"`{BIN.get(name, name)}` not found. Install it ({hint}) and retry.")
    return path


def run(cmd: list[Any], *, check: bool = True, cwd: Path | None = None, timeout: float | None = None) -> subprocess.CompletedProcess:
    cmd = [str(c) for c in cmd]
    cmd[0] = BIN.get(cmd[0], cmd[0])
    # stdin=DEVNULL: ffmpeg otherwise reads the terminal/pipe as interactive key commands.
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
    if check and proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-2500:]
        raise ReelsError(f"command failed (exit {proc.returncode}): {' '.join(cmd[:4])} …\n{tail}")
    return proc


def ffprobe_json(path: Path) -> dict:
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path]).stdout
    return json.loads(out or "{}")


def _rotation(stream: dict) -> int:
    rot = 0
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                rot = int(round(float(sd["rotation"])))
            except (TypeError, ValueError):
                pass
    tags = stream.get("tags") or {}
    if not rot and "rotate" in tags:
        try:
            rot = int(tags["rotate"])
        except ValueError:
            pass
    return rot % 360


def _fps(stream: dict) -> float | None:
    for key in ("avg_frame_rate", "r_frame_rate"):
        v = stream.get(key)
        if v and v != "0/0":
            try:
                f = float(Fraction(v))
            except (ValueError, ZeroDivisionError):
                continue
            if 1 < f < 1000:
                return round(f, 3)
    return None


def probe_video(path: Path) -> dict:
    data = ffprobe_json(path)
    streams = data.get("streams") or []
    v = next(
        (s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    if v is None:
        raise ReelsError(f"{path.name}: no video stream")
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    rot = _rotation(v)
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    if rot in (90, 270):
        w, h = h, w
    fmt = data.get("format") or {}
    dur = float(fmt.get("duration") or v.get("duration") or 0)
    tags = {**(fmt.get("tags") or {}), **(v.get("tags") or {})}
    created = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
    transfer = v.get("color_transfer")
    probe = {
        "duration": round(dur, 3),
        "width": w,
        "height": h,
        "fps": _fps(v),
        "rotation": rot,
        "vcodec": v.get("codec_name"),
        "pixFmt": v.get("pix_fmt"),
        "hdr": transfer in ("arib-std-b67", "smpte2084"),
        "colorTransfer": transfer,
        "hasAudio": a is not None,
        "acodec": (a or {}).get("codec_name"),
        "created": created,
    }
    from .sphere import detect_video  # local import: sphere imports from this module

    sph = detect_video(data, path)
    if sph:
        probe["sphere"] = sph
        probe["width"], probe["height"], probe["rotation"] = sph["width"], sph["height"], 0
    return probe


@lru_cache(maxsize=None)
def ffmpeg_filters() -> str:
    try:
        return run(["ffmpeg", "-hide_banner", "-filters"], check=False).stdout
    except FileNotFoundError:
        return ""


def has_filter(name: str) -> bool:
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", ffmpeg_filters(), re.M) is not None


TONEMAP = (
    "zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,zscale=t=bt709:m=bt709:r=tv,format=yuv420p"
)
# Without zimg: HLG is designed to look acceptable on SDR screens, so converting
# its BT.2020 colours to BT.709 (and a touch of contrast) gets close.
HLG_FALLBACK = "colorspace=all=bt709:iall=bt2020:format=yuv420p,eq=contrast=1.06:saturation=1.08"


_BIN_LOCK = threading.Lock()


def can_tonemap() -> bool:
    # One thread at a time: this may swap BIN to ffmpeg-full, and clips staged in parallel must never
    # see the half-switched state (they'd pick a different colour converter, or the slim ffmpeg).
    with _BIN_LOCK:
        if has_filter("zscale") and has_filter("tonemap"):
            return True
        if os.environ.get("REELS_FFMPEG"):
            return False
        for d in FULL_FFMPEG_DIRS:  # switch to Homebrew's ffmpeg-full if it's installed
            full = Path(d) / "ffmpeg"
            if full.exists() and full != Path(BIN["ffmpeg"]):
                BIN["ffmpeg"], BIN["ffprobe"] = str(full), str(Path(d) / "ffprobe")
                ffmpeg_filters.cache_clear()
                if has_filter("zscale") and has_filter("tonemap"):
                    return True
        return False


_warned_hdr = False


def hdr_filters(probe: dict) -> list[str]:
    """HDR (HLG/PQ from iPhone or GoPro) -> SDR BT.709 filters for this clip."""
    global _warned_hdr
    if not probe.get("hdr"):
        return []
    if can_tonemap():
        return [TONEMAP]
    if not _warned_hdr:
        warn(
            "HDR footage, but this ffmpeg can't tone-map (no zimg). Using an approximation"
            + (" for HLG" if probe.get("colorTransfer") == "arib-std-b67" else "; PQ clips may look washed out")
            + ". For accurate colour: brew install ffmpeg-full (picked up automatically)."
        )
        _warned_hdr = True
    if probe.get("colorTransfer") == "arib-std-b67" and has_filter("colorspace"):
        return [HLG_FALLBACK]
    return []


def extract_frame(src: Path, t: float, out: Path, *, width: int | None = None, probe: dict | None = None) -> None:
    vf = hdr_filters(probe or {})
    if width:
        vf.append(f"scale={width}:-2")
    cmd: list[Any] = ["ffmpeg", "-v", "error", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-frames:v", "1"]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-q:v", "3", out]
    run(cmd)
    if not out.exists():
        raise ReelsError(f"could not extract a frame at {t:.2f}s from {src.name}")


def load_audio_16k(src: Path):
    """First audio track as 16 kHz mono float32 samples (what Whisper and the VAD expect).

    Decoded with the system ffmpeg rather than faster-whisper's PyAV loader,
    which breaks whenever PyAV changes its API (it did in PyAV 16+).
    """
    import numpy as np

    proc = subprocess.run(
        [BIN["ffmpeg"], "-nostdin", "-v", "error", "-i", str(src), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", "-"],
        capture_output=True,
    )
    if proc.returncode != 0:
        raise ReelsError(f"couldn't decode audio from {Path(src).name}: {proc.stderr.decode(errors='replace')[-400:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


def volume_stats(src: Path) -> dict | None:
    proc = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", src, "-map", "0:a:0", "-af", "volumedetect", "-vn", "-sn", "-dn", "-f", "null", "-"],
        check=False,
    )
    text = proc.stderr or ""
    mean = re.search(r"mean_volume:\s*(-?[\d.]+) dB", text)
    peak = re.search(r"max_volume:\s*(-?[\d.]+) dB", text)
    if not mean:
        return None
    return {"meanDb": float(mean.group(1)), "peakDb": float(peak.group(1)) if peak else None}
