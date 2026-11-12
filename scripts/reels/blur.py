"""Blur patches: hide one thing in a talking clip (a product held in the hand before its reveal).

Plan, on a video segment:  "blur": [{"keys": [{"at": 0.0, "x": 0.2, "y": 0.6, "w": 0.3, "h": 0.2}, ...], "strength": 28}]
`at` = seconds from the segment start; x/y = centre and w/h = size of the patch, as fractions of the framed
9:16 picture (what `./reels frames` / the look strips show for that segment's crop). The patch moves in a
straight line between keys and is shown from the first key to the last. The template draws it over the
footage (under cutaways, text and exhibits) and it follows punch-ins.
"""

from __future__ import annotations

from typing import Any

from .util import ReelsError

MAX_STRENGTH = 80


def blur_props(raw: Any, frames: int, fps: int, where: str) -> list[dict]:
    """[{"strength": px at 1080 wide, "keys": [[frame, x, y, w, h], ...]}] for the template; raises on a bad spec."""
    if not raw:
        return []
    if not isinstance(raw, list):
        raise ReelsError(f"{where}: blur must be a list of patches")
    out = []
    for i, patch in enumerate(raw):
        at = f"{where}: blur {i + 1}"
        if not isinstance(patch, dict) or not isinstance(patch.get("keys"), list) or not patch["keys"]:
            raise ReelsError(f'{at}: needs "keys": [{{"at", "x", "y", "w", "h"}}, ...]')
        strength = patch.get("strength", 28)
        if not isinstance(strength, (int, float)) or not 4 <= strength <= MAX_STRENGTH:
            raise ReelsError(f"{at}: strength must be 4–{MAX_STRENGTH}")
        keys = []
        for k in patch["keys"]:
            try:
                t, x, y, w, h = (float(k[n]) for n in ("at", "x", "y", "w", "h"))
            except (KeyError, TypeError, ValueError):
                raise ReelsError(f"{at}: every key needs numbers for at, x, y, w, h") from None
            if t < 0 or not (0 < w <= 1.5 and 0 < h <= 1.5) or not (-0.5 <= x <= 1.5 and -0.5 <= y <= 1.5):
                raise ReelsError(f"{at}: key at {t:g}s is outside the picture")
            keys.append([min(frames, round(t * fps)), round(x, 4), round(y, 4), round(w, 4), round(h, 4)])
        keys.sort(key=lambda k: k[0])
        if keys[0][0] >= frames:
            raise ReelsError(f"{at}: starts after the segment ends")
        if len(keys) == 1:
            keys.append([frames, *keys[0][1:]])  # a single key holds to the end of the segment
        out.append({"strength": strength, "keys": keys})
    return out
