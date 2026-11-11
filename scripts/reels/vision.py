"""On-device looks at the user's own footage for exhibits: where the pointing fingertip is
(hand landmarks) and where the rider is (person mask). Both run through Apple's Vision framework
in a small Swift helper (vision.swift, built on demand with swiftc) — nothing is generated,
downloaded or sent anywhere. macOS only; everything else in exhibits works without it."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Any

from .config import Project
from .media import run
from .util import ReelsError, read_json, write_json

SOURCE = Path(__file__).with_name("vision.swift")
MIN_CONF = 0.3  # a landmark Vision is less sure of than this isn't used
MIN_EXTENSION = 1.25  # index tip at least this much further from the wrist than its knuckle = extended
MIN_SHARE = 0.15  # of the window's frames must show a pointing hand, or there's no anchor


def available() -> str | None:
    """None when the helper can run here, else why not."""
    if sys.platform != "darwin":
        return "hand anchoring and depth need macOS (Apple's Vision framework)"
    if not shutil.which("swiftc"):
        return "hand anchoring and depth need the Swift compiler — install Xcode's command line tools (xcode-select --install)"
    return None


def helper(project: Project) -> Path:
    """The compiled helper, rebuilt when vision.swift changes."""
    why = available()
    if why:
        raise ReelsError(why)
    tag = hashlib.sha1(SOURCE.read_bytes()).hexdigest()[:12]
    out = project.root / ".cache" / "bin" / f"reels-vision-{tag}"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".build")
        proc = subprocess.run(["swiftc", "-O", "-swift-version", "5", "-suppress-warnings", str(SOURCE), "-o", str(tmp)],
                              capture_output=True, text=True)  # fmt: skip
        if proc.returncode != 0 or not tmp.exists():
            raise ReelsError(f"couldn't build the Vision helper:\n{(proc.stderr or proc.stdout)[-2000:]}")
        tmp.replace(out)
        for old in out.parent.glob("reels-vision-*"):
            if old != out:
                old.unlink(missing_ok=True)
    return out


def _call(project: Project, args: list[Any]) -> dict:
    proc = subprocess.run([str(helper(project)), *map(str, args)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise ReelsError((proc.stderr or "the Vision helper failed").strip()[-1500:])
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ReelsError(f"the Vision helper returned something unreadable: {e}") from e


# --------------------------------------------------------------------------
# Pointing fingertip
# --------------------------------------------------------------------------


def hand_rows(project: Project, video: Path, key: str | None = None) -> list[dict]:
    """Per-frame hand landmarks of a (staged) clip: [{"i": frame, "hands": [{"tip": [x, y, conf], …}]}],
    0..1 from the top-left. Cached under .cache/vision when `key` is given."""
    cache = project.root / ".cache" / "vision" / f"hands-{key}.json" if key else None
    if cache and cache.exists():
        return read_json(cache)["rows"]
    got = _call(project, ["hands", video])
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        write_json(cache, got)
    return got["rows"]


def _pt(hand: dict, name: str) -> tuple[float, float] | None:
    p = hand.get(name)
    if not p or p[2] < MIN_CONF:
        return None
    return float(p[0]), float(p[1])


def pointing(hand: dict, aspect: float = 9 / 16) -> dict | None:
    """The index fingertip and where it points, if this hand's index finger is extended.
    `aspect` = frame width / height, so distances are measured in real proportions."""
    tip, mcp, wrist = _pt(hand, "tip"), _pt(hand, "mcp"), _pt(hand, "wrist")
    if not tip or not mcp:
        return None

    def d(a: tuple[float, float], b: tuple[float, float]) -> float:
        return math.hypot((a[0] - b[0]) * aspect, a[1] - b[1])

    base = _pt(hand, "pip") or mcp
    if wrist:
        if d(tip, wrist) < MIN_EXTENSION * d(mcp, wrist):
            return None  # index finger curled
        ext = d(tip, wrist) / max(1e-6, d(mcp, wrist))
    else:
        ext = 1.3
    # An open palm also has the index finger out: prefer hands whose other fingers are curled.
    others = 0
    for f in ("middle", "ring", "little"):
        ft, fp = _pt(hand, f"{f}Tip"), _pt(hand, f"{f}Pip")
        if wrist and ft and fp and d(ft, wrist) > 1.1 * d(fp, wrist):
            others += 1
    dx, dy = (tip[0] - base[0]) * aspect, tip[1] - base[1]
    n = math.hypot(dx, dy)
    if n < 1e-6:
        return None
    return {"x": tip[0], "y": tip[1], "dx": dx / n, "dy": dy / n, "score": ext * float(hand["tip"][2]) * (0.6 if others >= 2 else 1.0)}


def fingertips(rows: list[dict], aspect: float = 9 / 16) -> list[dict | None]:
    """Per frame: the best pointing hand's fingertip ({"x", "y", "dx", "dy"}), or None."""
    out: list[dict | None] = []
    for r in rows:
        best = max((p for p in (pointing(h, aspect) for h in r.get("hands") or []) if p), key=lambda p: p["score"], default=None)
        out.append(best)
    return out


def steady_point(tips: list[dict | None]) -> dict | None:
    """One fingertip for the whole window (the median, so the hand coming up and going down
    doesn't drag it), or None when too few frames show a pointing hand."""
    seen = [t for t in tips if t]
    if len(seen) < 3 or len(seen) < MIN_SHARE * len(tips):
        return None
    dx, dy = statistics.median(t["dx"] for t in seen), statistics.median(t["dy"] for t in seen)
    n = math.hypot(dx, dy) or 1.0
    return {"x": statistics.median(t["x"] for t in seen), "y": statistics.median(t["y"] for t in seen), "dx": dx / n, "dy": dy / n,
            "share": len(seen) / len(tips)}  # fmt: skip


def smooth_track(tips: list[dict | None], radius: int = 4) -> list[dict] | None:
    """A fingertip for every frame: gaps filled from their neighbours, then a moving average
    (±radius frames) so the exhibit glides instead of jittering. None when too few detections."""
    idx = [i for i, t in enumerate(tips) if t]
    if len(idx) < 3 or len(idx) < MIN_SHARE * len(tips):
        return None
    filled: list[dict] = []
    for i in range(len(tips)):
        if tips[i]:
            filled.append(tips[i])  # type: ignore[arg-type]
            continue
        before = max((j for j in idx if j < i), default=None)
        after = min((j for j in idx if j > i), default=None)
        if before is None or after is None:
            filled.append(tips[after if before is None else before])  # type: ignore[arg-type,index]
        else:
            f = (i - before) / (after - before)
            a, b = tips[before], tips[after]
            filled.append({k: a[k] + (b[k] - a[k]) * f for k in ("x", "y", "dx", "dy")})  # type: ignore[index]
    out = []
    for i in range(len(filled)):
        win = filled[max(0, i - radius) : i + radius + 1]
        p = {k: sum(w[k] for w in win) / len(win) for k in ("x", "y", "dx", "dy")}
        n = math.hypot(p["dx"], p["dy"]) or 1.0
        p["dx"], p["dy"] = p["dx"] / n, p["dy"] / n
        out.append(p)
    return out


# --------------------------------------------------------------------------
# Person in front (depth)
# --------------------------------------------------------------------------


def foreground(project: Project, clip: Path, out: Path, *, start: int, frames: int, fps: int, feather: float = 1.2) -> None:
    """Cuts the rider out of `frames` frames of a staged clip (from frame `start`) into a video
    with an alpha channel (VP9 WebM): the same pixels, transparent everywhere but the person.
    Laid over an exhibit, it puts the rider — and the pointing hand — in front of it."""
    probe = json.loads(run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "json", clip]).stdout)["streams"][0]  # fmt: skip
    w, h = int(probe["width"]), int(probe["height"])
    cut, mask = out.with_suffix(".cut.mp4"), out.with_suffix(".gray")
    try:
        # frame-exact window of the staged clip (already at the reel's fps)
        run(["ffmpeg", "-v", "error", "-y", "-i", clip, "-vf", f"trim=start_frame={start}:end_frame={start + frames},setpts=PTS-STARTPTS",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "12", "-g", fps, "-pix_fmt", "yuv420p", cut])  # fmt: skip
        info = _call(project, ["matte", cut, mask])
        if not info.get("frames"):
            raise ReelsError(f"no frames to cut the rider out of in {clip.name}")
        soften = f",gblur=sigma={feather:g}" if feather > 0 else ""
        graph = (f"[1:v]scale={w}:{h}:flags=bilinear{soften},format=gray[m];"
                 f"[0:v][m]alphamerge,format=yuva420p[v]")  # fmt: skip
        run(["ffmpeg", "-v", "error", "-y", "-i", cut, "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{info['width']}x{info['height']}",
             "-r", fps, "-i", mask, "-filter_complex", graph, "-map", "[v]", "-frames:v", frames, "-c:v", "libvpx-vp9",
             "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-b:v", "0", "-crf", "20", "-deadline", "realtime", "-cpu-used", "5",
             "-row-mt", "1", "-g", fps, "-an", out])  # fmt: skip
    finally:
        cut.unlink(missing_ok=True)
        mask.unlink(missing_ok=True)
    if not out.exists():
        raise ReelsError(f"couldn't make the foreground layer for {clip.name}")
