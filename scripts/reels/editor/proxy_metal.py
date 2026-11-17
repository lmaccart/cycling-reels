"""Trusted native proxy adapter, independent of the selected renderer snapshot."""
from __future__ import annotations

import json
import math
import os
import platform
import subprocess
import time
import sys
from fractions import Fraction
from pathlib import Path

from ..media import ffprobe_json
from ..util import ReelsError
from . import proxy
from .proxy import PROXY_FPS, proxy_reservation


def executable(code_root: Path) -> Path | None:
    if platform.system() != "Darwin":
        return None
    value = os.environ.get("REELS_SPHERE")
    path = Path(value) if value else code_root / ".cache/editor/bin/reels-sphere"
    if (path.is_absolute() and path.is_file() and os.access(path, os.X_OK)
            and path.with_name("sphere.metallib").is_file()):
        return path.resolve()
    return None


def capability(probe: dict, streams: list[dict]) -> str | None:
    """W7's proven input gate, kept in trusted runtime rather than frozen code."""
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
        videos = [s for i in sphere["tracks"] for s in streams if s.get("index") == i]
        if len(videos) != 2 or any((s.get("width"), s.get("height")) != (3*h+o, h) for s in videos):
            return "GoPro track layout does not match the proven source transform"
    else:
        videos = [s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")][:1]
    if not videos:
        return "source video track is missing"
    for stream in videos:
        if stream.get("codec_name") not in ("h264", "hevc") or stream.get("pix_fmt") not in ("yuv420p", "yuvj420p"):
            return "only hardware-decodable 8-bit 4:2:0 H.264/HEVC is supported"
        if any(stream.get(k) not in (None, "unknown", "bt709") for k in ("color_space", "color_primaries", "color_transfer")):
            return "non-BT.709 colour conversion remains in v360"
        if stream.get("color_space") in (None, "unknown") and int(stream.get("width", 0)) * int(stream.get("height", 0)) <= 720 * 576:
            return "untagged SD uses staging's BT.601 colour interpretation"
        if float(stream.get("start_time", 0)) != 0:
            return "nonzero source track origin remains in v360"
        if any(d.get("rotation", 0) != 0 for d in stream.get("side_data_list", [])):
            return "rotated source track remains in v360"
        try:
            int(str(stream["id"]), 0)
        except (KeyError, ValueError):
            return "container lacks explicit MP4 track IDs"
    if len({s.get("color_range", "pc" if s["pix_fmt"].startswith("yuvj") else "tv") for s in videos}) != 1:
        return "GoPro tracks have different colour ranges"
    return None


def cached_execution(marker: dict) -> dict:
    evidence = marker.get("execution")
    return evidence if isinstance(evidence, dict) else {}


def reusable_execution(marker: dict) -> bool:
    # Backend selection is not a cache key. An explicitly required Metal run,
    # however, must never claim a cached ffmpeg result as native execution.
    return os.environ.get("REELS_SPHERE_BACKEND") != "metal" or cached_execution(marker).get("backend") == "metal"


def render_proxy(job: dict, source: Path, output: Path, *, projection: str,
                 start: float, end: float, views: list[dict] | None = None,
                 inputs=()) -> dict:
    """Return native evidence or an explicit reason to run the accurate ffmpeg path."""
    backend = os.environ.get("REELS_SPHERE_BACKEND", "auto")
    if backend not in ("auto", "metal", "v360"):
        raise ReelsError("REELS_SPHERE_BACKEND must be auto, metal or v360")

    def fallback(reason: str) -> dict:
        if backend == "metal":
            raise ReelsError(f"Metal required: {reason}")
        return {"backend": "ffmpeg", "fallbackReason": reason}

    if backend == "v360":
        return fallback("v360 explicitly requested")
    code_root = Path(job.get("codeRoot") or Path(__file__).resolve().parents[3])
    binary = executable(code_root)
    if binary is None:
        return fallback("reels-sphere is not built on this macOS runtime")
    probe = job.get("probe") or {}
    try:
        container = ffprobe_json(source)
        streams = container.get("streams", [])
        reason = capability(probe, streams)
    except (ReelsError, OSError, subprocess.SubprocessError, KeyError, TypeError, ValueError) as exc:
        return fallback(f"native input capability check failed: {exc}")
    if reason:
        return fallback(reason)
    seconds = end - start
    if not math.isfinite(seconds) or seconds <= 0:
        return fallback("native proxy requires a finite positive duration")
    if projection == "front-back":
        if not isinstance(views, list) or len(views) != 2:
            raise ReelsError("native front-back proxy requires the two resolved worker views")
        width, height = 1440, 1280
    elif projection == "equirect":
        if seconds > 180 + 1e-6:
            raise ReelsError("native sphere proxy covers at most 180 seconds")
        width, height = 2048, 1024
    else:
        raise ReelsError("unsupported native proxy projection")
    sphere = probe["sphere"]
    indices = sphere["tracks"] if sphere["projection"] == "gopro-eac" else [next(
        s["index"] for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic"))]
    videos = [next(s for s in streams if s["index"] == i) for i in indices]
    # Aim's existing ffmpeg seek/trim is millisecond-quantized. Match its media
    # clock exactly; the worker still computes CORI on the unrounded request.
    media_start = float(f"{start:.3f}") if inputs else start
    media_seconds = float(f"{seconds:.3f}") if inputs else seconds
    # Container duration includes audio/data tails. vf_fps ends at the selected
    # VIDEO EOF, not at the container's rounded library duration. Keep the mux's
    # original interval; never synthesize an extra video frame to cover audio.
    try:
        video_ends = [int(s["duration_ts"]) * Fraction(s["time_base"]) for s in videos]
        if len(videos) > 1:
            rates = {Fraction(s[key]) for s in videos for key in ("r_frame_rate", "avg_frame_rate")}
            if len(rates) != 1 or next(iter(rates)) <= 0:
                return fallback("selected video tracks have different or unsupported frame rates")
            # GoPro chapter tails differ by up to two 59.94 fps source frames.
            # Bound the discrepancy in source frames, not rounded proxy ticks;
            # only decode the intersection, never hold one track past its EOF.
            if max(video_ends) - min(video_ends) > 2 / rates.pop():
                return fallback("selected video tracks have different EOF timestamps")
        video_end = min(video_ends)
        container_end = Fraction(container["format"]["duration"])
        # Library durations and ffmpeg input trims are millisecond-quantized.
        # Accept that exact EOF representation, not an arbitrary EOF tolerance.
        requested_limit = max(container_end, Fraction(f"{float(container_end):.3f}"))
        if Fraction(str(end)) > requested_limit:
            return fallback("native proxy interval exceeds container EOF")
        video_out = min(Fraction(str(media_start)) + Fraction(str(media_seconds)), video_end)
        if not 0 <= media_start < video_out or video_end <= 0:
            return fallback("native proxy interval has no video samples")
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        return fallback(f"native video EOF is unavailable: {exc}")
    frames = math.floor((video_out - Fraction(str(media_start))) * PROXY_FPS + Fraction(1, 2))
    # Both native video and the audio-mux output can coexist. Their hard bounds
    # together leave 16 MB of the existing reservation for sidecars and metadata.
    limit = (proxy_reservation(seconds) - 16_000_000) // 2
    native = output.with_suffix(".native.mp4")
    request_path = output.with_suffix(".request.json")
    request = {
        "version": 1, "preset": "proxy", "source": str(source), "destination": str(native),
        "sourceProjection": sphere["projection"], "trackIDs": [int(str(s["id"]), 0) for s in videos],
        "sourceIn": media_start, "sourceOut": float(video_out),
        "speed": 1, "fps": PROXY_FPS, "frames": frames,
        "width": width, "height": height, "projection": projection, "keys": [], "corrections": None,
        "fullRange": videos[0].get("color_range") == "pc" or videos[0]["pix_fmt"].startswith("yuvj"),
        "maxOutputBytes": limit,
    }
    if projection == "front-back":
        request["views"] = [{"at": 0, **view} for view in views]
    failures = []
    audio_inputs = list(inputs) or ["-ss", f"{start:.3f}", "-t", f"{seconds:.3f}"]
    started = time.monotonic()
    try:
        for codec in ("h264", "hevc"):
            native.unlink(missing_ok=True)
            output.unlink(missing_ok=True)
            request["codec"] = codec
            previous_partials = set(output.parent.glob(".source-*.mp4")) | set(output.parent.glob(".sphere-*.mp4"))
            request_path.write_text(json.dumps(request, allow_nan=False), encoding="utf-8")
            try:
                check = proxy._run_encoder([binary, "--check", request_path], timeout=60, check=False,
                                           max_output_bytes=limit)
                if check.returncode:
                    raise ReelsError((check.stderr or check.stdout or f"capability exit {check.returncode}").strip())
                result = proxy._run_encoder([binary, "--render", request_path], timeout=1800,
                                            max_output_bytes=limit)
                try:
                    report = json.loads(result.stdout)
                    size = native.stat().st_size
                    if (report["renderer"] != "metal" or report["frames"] != frames
                            or report["encoder"] != f"{codec}_videotoolbox"
                            or not math.isfinite(report["wallSeconds"]) or report["wallSeconds"] < 0
                            or not 0 < size < limit - 1_000_000
                            or report.get("outputBytes", size) != size):
                        raise ValueError("wrong renderer, encoder, frame count, timing or byte count")
                except (KeyError, TypeError, ValueError) as exc:
                    raise ReelsError("reels-sphere returned invalid proxy execution evidence") from exc
                mux_started = time.monotonic()
                # Native video has a zero-based output clock. Keep the source
                # audio's original ffmpeg input seek/trim and first-audio mapping.
                proxy._run_encoder([
                    "ffmpeg", "-v", "error", "-y", "-i", native, *audio_inputs, "-i", source,
                    "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                    "-fs", str(limit), output,
                ], timeout=1800, max_output_bytes=limit)
                size = output.stat().st_size
                if not 0 < size < limit - 1_000_000:
                    raise ReelsError("proxy mux exceeded its disk reservation; refusing a truncated cache entry")
                return {"backend": "metal", "encoder": report["encoder"],
                        "fallbackReason": "; ".join(failures) or None, "native": report,
                        "muxSeconds": time.monotonic() - mux_started,
                        "encodeSeconds": time.monotonic() - started, "outputBytes": size}
            except (ReelsError, OSError, subprocess.SubprocessError) as exc:
                failures.append(f"native {codec}: {exc}")
                # These calls returned or killed and reaped their child. A
                # KeyboardInterrupt may leave a writer alive: leave its files
                # for startup cleanup, protected by its inherited key lock.
                partials = set(output.parent.glob(".source-*.mp4")) | set(output.parent.glob(".sphere-*.mp4"))
                for partial in partials - previous_partials:
                    partial.unlink(missing_ok=True)
        output.unlink(missing_ok=True)
        return fallback("; ".join(failures))
    finally:
        if not isinstance(sys.exc_info()[1], (KeyboardInterrupt, SystemExit)):
            native.unlink(missing_ok=True)
            request_path.unlink(missing_ok=True)
