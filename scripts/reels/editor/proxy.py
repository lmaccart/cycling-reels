"""Disposable editor proxies. Bump PROXY_VERSION only when their media contract changes."""
from __future__ import annotations

import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

from ..util import ReelsError

PROXY_VERSION = 4
PROXY_FPS = 30
PROXY_GOP = 10
SPHERE_PROXY_WIDTH = 2048
SPHERE_PROXY_MAX = 180.0


def cache_key(value: dict) -> str:
    return hashlib.sha256(json.dumps({"version": PROXY_VERSION, **value}, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def source_cache_key(asset: dict, identity: dict, view: dict) -> str:
    return cache_key({"asset": asset["id"], "identity": identity, "kind": asset.get("kind"),
                      "probe": asset.get("probe") or {}, "view": view,
                      "angles": (asset.get("tags") or {}).get("angles") or {}})


def sphere_cache_key(asset_id: str, identity: dict, probe: dict) -> str:
    # Stabilization is a separate sidecar: it never changes the sphere's pixels.
    return cache_key({"asset": asset_id, "identity": identity, "probe": probe,
                      "fps": PROXY_FPS, "width": SPHERE_PROXY_WIDTH})


def proxy_reservation(seconds: float) -> int:
    """Bound encoded media, faststart's in-place move, audio and correction sidecars.

    Twice the 12 Mb/s video ceiling, plus audio/mux headroom and 32 MB fixed slack.
    The encoder also enforces a smaller file limit; an exhausted limit is a failure,
    never a successful (but truncated) cache entry.
    """
    return math.ceil(max(0.0, seconds) * 4_000_000) + 32_000_000


def encode_proxy(source: Path, output: Path, arguments: list, *, inputs=(),
                 seconds: float = 0, crf: int = 23) -> str:
    """Hardware decode + encode on macOS; retry the complete job in software on failure."""
    from ..media import run

    hardware = ["h264_videotoolbox", "hevc_videotoolbox"]
    encoders = [*hardware, "libx264"] if sys.platform == "darwin" else ["libx264"]
    limit = proxy_reservation(seconds) - 16_000_000 if seconds > 0 else None
    for encoder in encoders:
        accelerated = encoder != "libx264"
        codec = (["-b:v", "8M", "-allow_sw", "0"] if accelerated else
                 ["-preset", "ultrafast", "-crf", str(crf)])
        if encoder == "hevc_videotoolbox":
            codec += ["-tag:v", "hvc1"]
        command = [
            "ffmpeg", "-v", "error", "-y", *inputs,
            *(["-hwaccel", "videotoolbox"] if accelerated else []), "-i", source,
            *arguments, "-c:v", encoder, *codec, "-maxrate", "12M", "-bufsize", "24M",
            "-g", str(PROXY_GOP), "-bf", "0", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
            *(["-fs", str(limit)] if limit else []), output,
        ]
        try:
            run(command, timeout=1800)
        except (ReelsError, OSError, subprocess.SubprocessError):
            if not accelerated:
                raise
            continue
        if limit and output.stat().st_size >= limit - 1_000_000:
            raise ReelsError("proxy exceeded its disk reservation; refusing a truncated cache entry")
        return encoder
    raise AssertionError("software encoder was not attempted")
