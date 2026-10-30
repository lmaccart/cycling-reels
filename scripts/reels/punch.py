from __future__ import annotations

import re
from typing import Any

from .util import clamp

# Punch-in zooms: a quick zoom on the footage (never the text) for emphasis, the way
# Submagic/FireCut-style editors do it. Segment `punch` (manual times or transcript words)
# and plan `punchOnBeats` resolve to per-segment keyframes the template eases between.
# All frames are reel frames unless a name says otherwise.

PUNCH_SCALE = 1.15  # default scale of a manual / word punch
BEAT_SCALE = 1.1  # default scale of a punchOnBeats punch
SCALE_RANGE = (1.05, 1.4)
PUNCH_IN = 4  # frames: snappy zoom in
PUNCH_OUT = 6  # frames: ease back out at the end of `for`
RELEASES = ("ease", "cut")


def _num(v: Any, key: str, lo: float, hi: float, errors: list[str], where: str) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        errors.append(f"{where}: {key} must be a number")
        return None
    if not lo <= x <= hi:
        errors.append(f"{where}: {key}={x:g} is outside {lo:g}–{hi:g}")
        return None
    return x


def _common(raw: dict, default_scale: float, errors: list[str], where: str) -> dict:
    out: dict[str, Any] = {"scale": default_scale, "for": None, "x": 0.5, "y": 0.5, "release": "ease"}
    if raw.get("scale") is not None:
        out["scale"] = _num(raw["scale"], "scale", *SCALE_RANGE, errors, where) or default_scale
    if raw.get("for") is not None:
        out["for"] = _num(raw["for"], "for", 0.1, 600, errors, where)
    for k in ("x", "y"):
        if raw.get(k) is not None:
            got = _num(raw[k], k, 0, 1, errors, where)
            out[k] = 0.5 if got is None else got
    rel = raw.get("release", "ease")
    if rel not in RELEASES:
        errors.append(f"{where}: release must be {' or '.join(RELEASES)}")
        rel = "ease"
    out["release"] = rel
    return out


def norm_word(w: str) -> str:
    return re.sub(r"[^\w']+", "", str(w).lower()).strip("'")


def check_punch(spec: dict, asset: dict, errors: list[str], where: str) -> None:
    """Validates a segment's `punch` and normalizes it to {"words": [...], …} or a list of
    {"at", "scale", "for" (None = hold to the segment end), "x", "y", "release"}."""
    raw = spec.get("punch")
    if raw in (None, False, []):
        spec["punch"] = None
        return
    w = f"{where} punch"
    if asset["kind"] != "video":
        errors.append(f"{w}: punch-ins work on video clips (photos already move with `motion`)")
        spec["punch"] = None
        return
    if isinstance(raw, dict) and "words" in raw:
        words = raw["words"]
        if isinstance(words, str):
            words = words.split()
        if not isinstance(words, list) or not words or not all(isinstance(x, str) and norm_word(x) for x in words):
            errors.append(f'{w}: words must be a list like ["no", "way"]')
            spec["punch"] = None
            return
        if spec.get("captions") != "transcript":
            errors.append(f'{w}: word punches need captions: "transcript" on this segment (they follow its transcript)')
        spec["punch"] = {"words": [norm_word(x) for x in words], **_common(raw, PUNCH_SCALE, errors, w)}
        return
    items = raw if isinstance(raw, list) else [raw]
    out = []
    for j, p in enumerate(items):
        pw = f"{w}[{j}]" if isinstance(raw, list) else w
        if not isinstance(p, dict) or p.get("at") is None:
            errors.append(f'{pw}: use {{"at": 1.2, "scale": 1.15, "for": 0.8}} (at = seconds from the segment start) or {{"words": ["no", "way"]}}')
            continue
        at = _num(p["at"], "at", 0, 600, errors, pw)
        if at is not None:
            out.append({"at": at, **_common(p, PUNCH_SCALE, errors, pw)})
    spec["punch"] = sorted(out, key=lambda p: p["at"]) or None


def check_beats_punch(plan: dict, errors: list[str]) -> dict | None:
    raw = plan.get("punchOnBeats")
    if raw in (None, False):
        return None
    if raw is True:
        raw = {}
    if not isinstance(raw, dict):
        errors.append('punchOnBeats must be {"every": 4, "scale": 1.1}')
        return None
    every = raw.get("every", 4)
    if not isinstance(every, int) or not 1 <= every <= 32:
        errors.append("punchOnBeats: every must be a whole number of beats, 1–32")
        every = 4
    return {"every": every, **_common(raw, BEAT_SCALE, errors, "punchOnBeats")}


def _punch(start: int, end: int | None, p: dict, why: str) -> dict:
    return {"start": start, "end": end, "scale": p["scale"], "x": p["x"], "y": p["y"], "release": p["release"], "why": why}


def resolve_punches(
    segs: list, words: dict[str, list[dict]], pages: list[dict], on_beats: dict | None, beats: list[float] | None, fps: int, warnings: list[str]
) -> None:
    """Sets sg.spec["_punches"]: [{start, end (None = hold to the segment end), scale, x, y, release, why}]
    in reel frames. `words` = transcript words by asset id, `beats` = reel seconds of the music's beats."""
    for sg in segs:
        sg.spec["_punches"] = []
        p = sg.spec.get("punch")
        where = f"segment {sg.index + 1} punch"
        found: list[dict] = []
        if isinstance(p, dict):  # words
            a, b, speed = sg.spec["in"], sg.spec["out"], sg.spec["speed"]
            for w in words.get(sg.asset["id"]) or []:
                if not a - 0.02 <= w["s"] < b - 0.1 or norm_word(w["w"]) not in p["words"]:
                    continue
                s = sg.start + round((w["s"] - a) / speed * fps)
                if p["for"] is not None:
                    e = s + round(p["for"] * fps)
                else:  # hold for the caption page (the phrase) the word is on
                    e = next((pg["endFrame"] for pg in pages if pg["startFrame"] <= s < pg["endFrame"]), None)
                found.append(_punch(s, e, p, f"word '{norm_word(w['w'])}'"))
            if not found:
                warnings.append(f"{where}: none of {', '.join(p['words'])} is spoken between {a:g}s and {b:g}s — no punch")
        elif p:
            for q in p:
                s = sg.start + round(q["at"] * fps)
                found.append(_punch(s, None if q["for"] is None else s + round(q["for"] * fps), q, f"at {q['at']:g}s"))
        elif on_beats and beats:
            if sg.spec["captions"] == "transcript" or (sg.spec.get("sphere") or {}).get("path"):
                continue  # talking clips and 360 camera moves keep still
            grid = [round(t * fps) for i, t in enumerate(beats) if i % on_beats["every"] == 0]
            inside = [g for g in grid if sg.start + max(PUNCH_IN, sg.trans) <= g <= sg.end - PUNCH_IN - 2]
            for k in range(0, len(inside), 2):  # in, out, in, out… so it never keeps creeping in
                e = inside[k + 1] if k + 1 < len(inside) else None
                found.append(_punch(inside[k], e, on_beats, f"beat {grid.index(inside[k]) * on_beats['every']}"))
        kept: list[dict] = []
        for q in sorted(found, key=lambda q: q["start"]):
            if q["start"] > sg.end - PUNCH_IN - 1:
                warnings.append(f"{where} {q['why']}: starts too close to the end of the segment — dropped")
                continue
            q["start"] = max(q["start"], sg.start)
            if q["end"] is not None and q["end"] >= sg.end - 1:
                q["end"] = None
            if q["end"] is not None and q["end"] - q["start"] < PUNCH_IN + 2:
                q["end"] = q["start"] + PUNCH_IN + 2
            prev = kept[-1] if kept else None
            if prev and (prev["end"] is None or prev["end"] > q["start"] - PUNCH_IN):
                same = abs(prev["scale"] - q["scale"]) < 0.01 and abs(prev["x"] - q["x"]) < 0.01 and abs(prev["y"] - q["y"]) < 0.01
                if same:  # already zoomed there: just hold longer
                    if prev["end"] is not None:
                        prev["end"] = None if q["end"] is None else max(prev["end"], q["end"])
                    continue
                prev["end"] = q["start"]  # straight from one punch to the next
                prev["handoff"] = True
            kept.append(q)
        sg.spec["_punches"] = kept


def punch_keys(sg, fps: int) -> list[list[float]] | None:
    """[[frame from the segment start, scale, originX, originY], …]; the template eases between keys."""
    punches = sg.spec.get("_punches") or []
    if not punches:
        return None
    dur = sg.frames
    keys: list[list[float]] = []

    def put(f: float, s: float, x: float, y: float) -> None:
        f = int(clamp(round(f), 0, dur))
        k = [f, round(s, 3), round(x, 3), round(y, 3)]
        if keys and f <= keys[-1][0]:
            keys[-1] = [keys[-1][0]] + k[1:]
            return
        keys.append(k)

    cur = (1.0, punches[0]["x"], punches[0]["y"])
    put(0, *cur)
    for p in punches:
        s = p["start"] - sg.start
        # at 1x the origin doesn't show, so it can jump to the new one
        put(s, cur[0], *((p["x"], p["y"]) if cur[0] == 1.0 else cur[1:]))
        put(s + PUNCH_IN, p["scale"], p["x"], p["y"])
        cur = (p["scale"], p["x"], p["y"])
        if p["end"] is not None and not p.get("handoff"):
            e = p["end"] - sg.start
            put(e, *cur)
            put(e + (PUNCH_OUT if p["release"] == "ease" else 1), 1.0, p["x"], p["y"])
            cur = (1.0, p["x"], p["y"])
    if keys[-1][0] < dur:
        put(dur, *keys[-1][1:])
    return keys


def ends_zoomed(sg) -> float:
    """Scale the footage is at on the segment's last frame (1.0 = not punched in)."""
    last = (sg.spec.get("_punches") or [None])[-1]
    return last["scale"] if last and last["end"] is None else 1.0


# --------------------------------------------------------------------------
# Hi-res staging: a punch scales the staged clip up, so segments with punches are
# staged larger (when the source has the detail) and scaled down by the template.
# --------------------------------------------------------------------------


def source_density(sg, tw: int, th: int) -> float:
    """Source pixels per staged pixel at 1x (> 1 = the source has detail to spare)."""
    probe, spec = sg.asset["probe"], sg.spec
    w, h = float(probe["width"]), float(probe["height"])
    sph = spec.get("sphere")
    if sph:
        if sph["projection"] != "flat":
            return 1.0
        fovs = [sph["view"].get("fov", 75)] + [k.get("fov", 75) for k in sph.get("path") or []]
        width = float((probe.get("sphere") or {}).get("width") or w)
        return width / 360.0 * min(fovs) / tw
    if spec["frame"] == "crop":
        zoom = spec["zoom"]
        sw = max(tw * zoom, w * th * zoom / h) if w / h > tw / th else tw * zoom
        return w / max(sw, tw)
    return min(w / tw, h / th)


def stage_scale(sg, tw: int, th: int, most: float) -> float:
    """How much larger than the reel frame to stage this segment (1 = normal)."""
    punches = sg.spec.get("_punches") or []
    if not punches or most <= 1:
        return 1.0
    want = min(most, max(p["scale"] for p in punches))
    k = min(want, source_density(sg, tw, th))
    return round(k, 3) if k >= 1.05 else 1.0
