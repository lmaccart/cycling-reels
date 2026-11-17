"""Waveform peaks for the timeline's audio clips.

Read-only on the library/music/sfx; everything generated lands in <repo>/.cache/editor/audio."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .protocol import ProtocolError

SAMPLE_RATE = 8000
MAX_WIDTH = 4000
CACHE_REL = Path(".cache/editor/audio")


def cache_dir(project) -> Path:
    path = Path(project.root) / CACHE_REL
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_key(source: Path, start: float, end: float, width: int) -> str:
    try:
        stat = source.stat()
        stamp = f"{stat.st_size}:{int(stat.st_mtime)}"
    except OSError:
        stamp = "?"
    text = f"{source}|{stamp}|{start:.3f}|{end:.3f}|{width}"
    return hashlib.sha1(text.encode()).hexdigest()[:20]


def _plain(value):
    return value if isinstance(value, dict) else {}


def resolve_source(project, snapshot: dict, node_id: str) -> dict:
    """{path, start, end} (seconds into the file) for a timeline node's audio."""
    nodes = {n.get("id"): n for n in snapshot.get("nodes", [])}
    node = nodes.get(node_id)
    if node is None:
        raise ProtocolError("node_not_found", "That clip is not in the document.")
    fps = float(snapshot.get("fps") or 30)
    kind = str(node.get("kind", ""))
    raw = _plain(node.get("raw"))
    duration = max(0.05, (int(node.get("endFrame", 0)) - int(node.get("startFrame", 0))) / fps)
    if kind == "music" or kind == "generated_music":
        plan_music = (_plain(snapshot.get("plan")).get("music"))
        merged = {**(plan_music if isinstance(plan_music, dict) else {}), **raw}
        slug = merged.get("track") or (plan_music if isinstance(plan_music, str) else None)
        if not slug:
            raise ProtocolError("no_audio", "This reel has no music track.")
        path = project.music_dir / f"{slug}.mp3"
        start = float(merged.get("start") or 0)
        return _checked(path, start, start + duration)
    if kind in ("sfx", "generated_sfx", "resolved_sfx"):
        name = next((raw[k] for k in ("sound", "name", "sfx", "slug") if isinstance(raw.get(k), str)), None)
        if not name:
            raise ProtocolError("no_audio", "This sound effect has no file.")
        base = Path(project.root) / "sfx"
        for candidate in (base / f"{name}.wav", base / name):
            if candidate.is_file():
                return _checked(candidate, 0.0, duration)
        raise ProtocolError("no_audio", f"No sound effect file for {name}.")
    asset_id = node.get("assetId")
    if asset_id:
        from ..library import load_asset
        asset = load_asset(project, str(asset_id))
        path = project.abs(asset["media"])
        a = float(node.get("sourceIn") or 0)
        b = node.get("sourceOut")
        b = float(b) if b is not None else a + duration
        return _checked(path, a, max(b, a + 0.05))
    raise ProtocolError("no_audio", "That clip has no audio to draw.")


def _checked(path: Path, start: float, end: float) -> dict:
    if not path.is_file():
        raise ProtocolError("no_audio", f"Missing audio file {path.name}.")
    return {"path": str(path), "start": max(0.0, start), "end": max(start + 0.05, end)}


def compute_peaks(samples, buckets: int) -> list[list[float]]:
    """[[min, max], ...] per bucket, normalized to -1..1. `samples` is a sequence/array of signed 16-bit ints."""
    n = len(samples)
    buckets = max(1, int(buckets))
    if n == 0:
        return [[0.0, 0.0] for _ in range(buckets)]
    try:
        import numpy as np
        arr = np.asarray(samples, dtype=np.float32) / 32768.0
        edges = np.linspace(0, n, buckets + 1).astype(int)
        out = []
        for i in range(buckets):
            lo, hi = edges[i], max(edges[i + 1], edges[i] + 1)
            chunk = arr[lo:hi] if lo < n else arr[-1:]
            out.append([round(float(chunk.min()), 4), round(float(chunk.max()), 4)])
        return out
    except ImportError:
        out = []
        for i in range(buckets):
            lo = i * n // buckets
            hi = max((i + 1) * n // buckets, lo + 1)
            chunk = samples[lo:hi] or samples[-1:]
            out.append([round(min(chunk) / 32768.0, 4), round(max(chunk) / 32768.0, 4)])
        return out


def _ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise ProtocolError("ffmpeg_missing", "ffmpeg is not installed.")
    return exe


def decode_pcm(path: str, start: float, end: float) -> bytes:
    cmd = [_ffmpeg(), "-v", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}", "-i", path,
           "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    run = subprocess.run(cmd, capture_output=True, timeout=60)
    if run.returncode != 0:
        raise ProtocolError("audio_decode_failed", run.stderr.decode(errors="replace")[-300:] or "ffmpeg failed")
    return run.stdout


def waveform(project, source: dict, width: int) -> dict:
    width = max(8, min(MAX_WIDTH, int(width)))
    key = cache_key(Path(source["path"]), source["start"], source["end"], width)
    cached = cache_dir(project) / f"{key}.json"
    if cached.is_file():
        try:
            return json.loads(cached.read_text())
        except ValueError:
            pass
    pcm = decode_pcm(source["path"], source["start"], source["end"])
    try:
        import numpy as np
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2")
    except ImportError:
        import array
        samples = array.array("h")
        samples.frombytes(pcm[: len(pcm) // 2 * 2])
        samples = list(samples)
    result = {"mode": "waveform", "key": key, "width": width, "peaks": compute_peaks(samples, width),
              "start": source["start"], "end": source["end"]}
    cached.write_text(json.dumps(result))
    return result



def audio_visual(project, snapshot: dict | None, params: dict[str, Any]) -> dict:
    mode = params.get("mode", "waveform")
    if mode != "waveform":
        raise ProtocolError("invalid_mode", "mode must be waveform.")
    if params.get("nodeId"):
        if snapshot is None:
            raise ProtocolError("no_document", "Open a reel first.")
        source = resolve_source(project, snapshot, str(params["nodeId"]))
    else:
        src = _plain(params.get("source"))
        path = project.abs(str(src.get("path", ""))).resolve()
        roots = [Path(project.root).resolve()]
        if not any(path.is_relative_to(r) for r in roots):
            raise ProtocolError("invalid_path", "Audio source must be inside the workspace.")
        start = float(src.get("start") or 0)
        source = _checked(path, start, float(src.get("end") or start + 1))
    width = int(params.get("width") or 400)
    return waveform(project, source, width)
