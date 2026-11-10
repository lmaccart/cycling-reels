"""Retention checks for ./reels check and render: quick, deterministic flags for what makes people
scroll away — a still opening, slideshow fades, long stretches where nothing on screen changes,
and speech captions that cut into words. They're warnings (advice), never errors: a deliberate
held view or a quiet fade can be the right call, and the planner decides.

Everything here works on the built timeline (reel frames), so it needs no media.
"""

from __future__ import annotations

from typing import Any

STILL_SECONDS = 5.0  # nothing changes on screen for this long → flag it
EARLY_FADE_SECONDS = 2.0  # a crossfade this early slows the opening
LOW_CONFIDENCE = 0.35  # Whisper word probability under which captions are worth a second look
WORD_EDGE = 0.05  # s: a word has to start this far before an in-point (or end after an out-point) to count as cut
CAPTION_EDGE = 0.02  # s: the same tolerance caption_pages_for uses to decide a word is on screen

SPINES = ("voice", "story", "sound", "trend")  # what carries a reel (plan-schema.md)
MIN_VOICE_SHARE = 0.30  # spine: voice with less of the rider's voice than this isn't carried by it
BATCH_SIZE = 7  # the weekly mix below is stated per seven reels
MIN_VOICE_REELS = 4
MAX_MONTAGE_REELS = 1
PEOPLE_ROLES = ("voice", "reaction", "crew")  # taxonomy roles whose subject is a person or what they say
MONTAGE_FORMATS = ("ride-montage", "beat-edit")  # the recipes (recipes.md) that are only shots on a song


def opening(segs: list[Any], fps: int) -> list[str]:
    """The first seconds decide the 3 s hold: open on motion, cut hard."""
    out = []
    first = segs[0]
    if first.kind == "photo" and not (first.spec.get("sphere") or {}).get("path"):
        out.append("retention: the reel opens on a photo — a still first frame is the easiest one to scroll past; "
                   "open on a moving clip and use the photo later")  # fmt: skip
    for sg in segs[1:]:
        if sg.trans and sg.start < EARLY_FADE_SECONDS * fps:
            out.append(f"retention: crossfade at {sg.start / fps:.1f}s — in the first {EARLY_FADE_SECONDS:g} s a hard cut keeps the "
                       "energy up (fades read as a slideshow)")  # fmt: skip
            break
    return out


def fades(segs: list[Any]) -> list[str]:
    """Mostly-fade edits read as a slideshow (recipes.md: 'Cut with the music')."""
    cuts = len(segs) - 1
    n = sum(1 for sg in segs[1:] if sg.trans)
    if n >= 2 and 2 * n > cuts:
        return [f"retention: {n} of {cuts} cuts are crossfades — hard cuts on the beat read as edited, a run of fades reads as a "
                "slideshow; keep a fade for one deliberate moment"]  # fmt: skip
    return []


def change_marks(segs: list[Any], pages: list[dict], beats: list[dict], nat: list[dict], hook: dict | None, fps: int) -> set[int]:
    """Reel frames where something on screen (or the ride sound) changes."""
    marks: set[int] = set()
    for sg in segs:
        marks.add(sg.start)
        for c in sg.spec.get("_cutaways") or []:
            marks.update((sg.start + c["start"], sg.start + c["start"] + c["seg"].frames))
        for p in sg.spec.get("_punches") or []:
            marks.add(p["start"])
            if p["end"] is not None:
                marks.add(p["end"])
        for k in (sg.spec.get("sphere") or {}).get("path") or []:
            marks.add(sg.start + round(k["at"] * fps))
    marks.update(p["startFrame"] for p in pages)
    for b in beats:
        marks.update((b["startFrame"], b["endFrame"]))
    marks.update(w["startFrame"] for w in nat)
    if hook:
        marks.add(hook["endFrame"])
    return marks


def still_stretches(segs: list[Any], pages: list[dict], beats: list[dict], nat: list[dict], hook: dict | None, fps: int, total: int) -> list[str]:
    """Stretches of STILL_SECONDS or more with no cut, cutaway, punch-in, 360 camera keyframe,
    caption page, text beat, hook exit or ride-sound moment."""
    marks = sorted(m for m in change_marks(segs, pages, beats, nat, hook, fps) | {0, total} if 0 <= m <= total)
    limit = round(STILL_SECONDS * fps)
    return [
        f"retention: nothing changes on screen for {(b - a) / fps:.1f}s ({a / fps:.1f}–{b / fps:.1f}s) — a cut, cutaway, punch-in "
        "or text beat there keeps people watching (fine for a deliberate held view)"
        for a, b in zip(marks, marks[1:])
        if b - a >= limit
    ]


def caption_cuts(sg: Any, words: list[dict]) -> list[str]:
    """Captioned segments: an in-point inside a spoken word puts a stray half-word caption on screen
    (it happened twice in the Nov 2–8 batch), an out-point inside one clips it; and low-confidence
    words are often wind that Whisper turned into speech."""
    a, b = sg.spec["in"], sg.spec["out"]
    where = f"segment {sg.index + 1}"
    out = []
    for w in words:
        s, e, text = float(w["s"]), float(w["e"]), str(w["w"]).strip()
        if s < a - WORD_EDGE and e > a + CAPTION_EDGE:
            out.append(f"{where}: in-point {a:.2f}s cuts into '{text}' ({s:.2f}–{e:.2f}s) — it shows as a stray caption; "
                       f"start at {e:.2f}s or before {s:.2f}s")  # fmt: skip
        if s < b - CAPTION_EDGE and e > b + WORD_EDGE:
            out.append(f"{where}: out-point {b:.2f}s (as cut) clips '{text}' ({s:.2f}–{e:.2f}s) — end at {e:.2f}s or before {s:.2f}s")
    shaky = [w for w in words if a <= float(w["s"]) < b and isinstance(w.get("p"), (int, float)) and w["p"] < LOW_CONFIDENCE]
    if shaky:
        shown = ", ".join(f"'{str(w['w']).strip()}' ({w['p']:.2f})" for w in shaky[:3]) + (" …" if len(shaky) > 3 else "")
        out.append(f"{where}: low-confidence transcript words {shown} — check them in the transcript (wind often turns into words)")
    return out


def check_spine(plan: dict, errors: list[str]) -> None:
    spine = plan.get("spine")
    if spine is not None and spine not in SPINES:
        errors.append(f"spine must be one of {', '.join(SPINES)} (got {spine!r})")


def spine_warnings(plan: dict, voice_share: float | None) -> list[str]:
    """voice_share is lineage's voiceShare, needed only when the plan says spine: voice."""
    spine = plan.get("spine")
    if spine is None:
        return [f"spine: not set — say what carries this reel ({', '.join(SPINES)}); voice and story reels start from the line or the story, not the best clips"]
    if spine == "voice" and voice_share is not None and voice_share < MIN_VOICE_SHARE:
        return [f"spine: marked voice, but the rider's voice covers only {voice_share:.0%} of the reel (under {MIN_VOICE_SHARE:.0%}) — "
                "let the talking clip run with cutaways over it, or change the spine"]  # fmt: skip
    return []


def opener_warnings(plan: dict, lineage: dict | None) -> list[str]:
    """The shot on screen at 0 s, from lineage: flag it when its tags say there's no face in it and
    nobody is talking. Lesson aud-person-01 (tentative): openings with a person or a real voice held
    63–76% past 3 s against 44–50% for scenery. Unknown is not scenery: an untagged span, a moment
    without a `face` tag and a photo (flagged separately) say nothing. A `sound` reel opens on its sound,
    and `crew` shots have people as their subject even from behind."""
    if not lineage or plan.get("spine") == "sound":
        return []
    first = next((e for e in lineage.get("segments") or [] if e.get("position") == "first"), None)
    if not first or first.get("match") != "moment":
        return []
    tags = first.get("tags") or {}
    if tags.get("face") is not False or tags.get("role") in PEOPLE_ROLES or first.get("captions") == "transcript":
        return []
    role = f" (role: {tags['role']})" if tags.get("role") else ""
    return [f"retention: the reel opens on a shot with no face and nobody talking{role} — openings with a person or a voice have "
            "held better so far (aud-person-01, a tentative lesson); open on the face or the line, or keep it if the shot is exceptional"]  # fmt: skip


def is_montage(fmt: Any) -> bool:
    return str(fmt or "").strip().lower().replace(" ", "-").replace("_", "-") in MONTAGE_FORMATS


def batch_warnings(plans: list[dict]) -> list[str]:
    """The weekly mix, for a seven-reel batch: at least 4 voice-led, at most 1 pure montage."""
    if len(plans) != BATCH_SIZE:
        return []
    out = []
    voice = sum(1 for p in plans if p.get("spine") == "voice")
    if voice < MIN_VOICE_REELS:
        out.append(f"batch: {voice} of {BATCH_SIZE} reels are spine: voice — at least {MIN_VOICE_REELS} should be carried by the rider's voice")
    filler = [str(p.get("slug")) for p in plans if p.get("spine") is None or is_montage(p.get("format"))]
    if len(filler) > MAX_MONTAGE_REELS:
        out.append(f"batch: {len(filler)} of {BATCH_SIZE} reels have no spine or are a pure montage — at most {MAX_MONTAGE_REELS}: {', '.join(filler)}")
    return out


def retention_warnings(segs: list[Any], pages: list[dict], beats: list[dict], nat: list[dict], hook: dict | None, fps: int, total: int) -> list[str]:
    return opening(segs, fps) + fades(segs) + still_stretches(segs, pages, beats, nat, hook, fps, total)
