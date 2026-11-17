"""Trusted proxy-side CORI cache, independent of the selected renderer's age."""
from __future__ import annotations

import math
from pathlib import Path

from .. import stabilize


def cached_correction_quats(src: Path, cache_dir: Path, identity: dict, start: float, end: float,
                            fps: int, setting: dict | None, *, reserve=None) -> dict:
    """Persist CORI corrections per asset/range/strength, including absent telemetry.

    Kept under the proxy's writable cache root, never next to the original. Lock
    corrections are range-anchored, so their key must include the range start.
    """
    import json
    import os
    import uuid

    from .proxy import cache_key
    from .proxy_cache import ProxyDiskFull
    from ..util import ReelsError

    frames = max(1, int(round((end - start) * fps)))
    key = cache_key({"identity": identity, "start": start, "end": end, "fps": fps,
                     "setting": setting, "correctionVersion": 1})
    if cache_dir.is_symlink():
        raise ReelsError("CORI cache directory cannot be a symlink")
    target = cache_dir / f"{key}.json"
    if target.is_symlink():
        raise ReelsError("CORI cache entry cannot be a symlink")
    try:
        cached = json.loads(target.read_text(encoding="utf-8"))
        stabilization = cached["stabilization"]
        if (isinstance(cached["hasOrientation"], bool) and "gravity" in cached
                and (stabilization is None or (
                    stabilization["start"] == start and stabilization["fps"] == fps
                    and len(stabilization["quats"]) == frames
                    and all(len(q) == 4 and all(math.isfinite(c) for c in q)
                            for q in stabilization["quats"])))):
            return cached
    except (OSError, ValueError, KeyError, TypeError):
        pass
    persist = True
    if reserve is not None:
        try:
            reserve()
        except ProxyDiskFull:
            # Covered media remains playable: compute the request's own clock
            # and lock anchor in memory without writing a correction sidecar.
            persist = False
    pad_s = stabilize.margin(setting) if setting else 0.5
    try:
        ori = stabilize.read_orientation(src, max(0.0, start - pad_s), end + pad_s)
    except (ReelsError, OSError, ValueError):
        # W5a: unreadable telemetry must not prevent playback. Do not persist a
        # transient read failure as permanently absent orientation data.
        return {"hasOrientation": False, "gravity": None, "stabilization": None}
    result = {
        "hasOrientation": ori is not None,
        "gravity": list(ori.gravity) if ori is not None and ori.gravity else None,
        "stabilization": None,
    }
    if setting and ori is not None:
        result["stabilization"] = {
            "start": start, "fps": fps,
            "quats": [[round(c, 6) for c in q] for q in stabilize.correction_quats(
                ori, [start + n / fps for n in range(frames)], setting)],
        }
    if not persist:
        return result
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = cache_dir / f".{key}-{uuid.uuid4().hex}.tmp"
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, separators=(",", ":"), allow_nan=False)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return result
