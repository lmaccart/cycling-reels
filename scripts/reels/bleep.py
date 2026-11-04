"""Bleeps: mute a stretch of a clip's own audio (swearing) and cover it with a censor tone.

Plan field on a video segment:
  "bleep": {"words": ["fucking"]}          — every spoken match in the segment (needs a transcript)
  "bleep": true  /  "auto"                  — every swear word from SWEARS spoken in the segment
  "bleep": [{"at": 1.4, "for": 0.5}]        — seconds from the segment start (reel time, like punch/nat)
  "bleep": {"windows": [{"at": 1.4, "for": 0.5}], "sound": "mute"}
  optional "sound": "tone" (default, 1 kHz censor beep) or "mute" (silence only), "pad": seconds each side

Bleeped words are masked in transcript captions ("fucking" → "f******").
"""
from __future__ import annotations

from typing import Any

from .punch import norm_word

SWEARS = {
    "fuck", "fucking", "fuckin", "fucked", "fucker", "fucks", "motherfucker", "motherfucking",
    "shit", "shitty", "shits", "bullshit", "bitch", "bitches", "asshole", "dick", "cunt", "damn", "goddamn",
}
SOUNDS = ("tone", "mute")
TONE_HZ = 1000
TONE_VOLUME = 1.0  # ffmpeg's sine is 1/8 amplitude ≈ -21 dBFS RMS: about the level of speech in phone clips
DEFAULT_PAD = 0.04


def check_bleep(spec: dict, asset: dict, errors: list[str], where: str) -> None:
    """Normalizes spec["bleep"] to None or {"words": set | None, "windows": [{"at","for"}], "sound", "pad"}."""
    raw = spec.get("bleep")
    w = f"{where} bleep"
    if raw in (None, False, [], {}):
        spec["bleep"] = None
        return
    if asset["kind"] != "video":
        errors.append(f"{w}: bleeps work on video clips (photos have no audio)")
        spec["bleep"] = None
        return
    opts = raw if isinstance(raw, dict) else {}
    sound = opts.get("sound", "tone")
    if sound not in SOUNDS:
        errors.append(f"{w}: sound must be one of {', '.join(SOUNDS)}")
        sound = "tone"
    pad = opts.get("pad", DEFAULT_PAD)
    if not isinstance(pad, (int, float)) or not 0 <= pad <= 0.3:
        errors.append(f"{w}: pad must be 0–0.3 seconds")
        pad = DEFAULT_PAD
    out: dict[str, Any] = {"words": None, "windows": [], "sound": sound, "pad": float(pad)}

    if raw is True or raw == "auto" or opts.get("words") in (True, "auto"):
        out["words"] = set(SWEARS)
    elif "words" in opts:
        words = opts["words"]
        if isinstance(words, str):
            words = words.split()
        if not isinstance(words, list) or not words or not all(isinstance(x, str) and norm_word(x) for x in words):
            errors.append(f'{w}: words must be a list like ["fucking"]')
        else:
            out["words"] = {norm_word(x) for x in words}
    else:
        items = raw if isinstance(raw, list) else opts["windows"] if isinstance(opts.get("windows"), list) else [raw] if opts else None
        if items is None:
            errors.append(f'{w}: use true, {{"words": [...]}} or [{{"at": 1.4, "for": 0.5}}]')
            spec["bleep"] = None
            return
        for j, it in enumerate(items):
            ww = f"{w}[{j}]" if isinstance(raw, list) else w
            at, dur = (it.get("at"), it.get("for")) if isinstance(it, dict) else (None, None)
            if not isinstance(at, (int, float)) or not isinstance(dur, (int, float)) or at < 0 or not 0.05 <= dur <= 10:
                errors.append(f'{ww}: needs "at" (≥ 0) and "for" (0.05–10 s), seconds from the segment start')
                continue
            out["windows"].append({"at": float(at), "for": float(dur)})

    if out["words"] is not None and not asset.get("transcript"):
        errors.append(f"{w}: word bleeps need a transcript (./reels transcribe {asset['id']}) — or use at/for")
    if not asset["probe"].get("hasAudio"):
        errors.append(f"{w}: {asset['id']} has no audio track to bleep")
    spec["bleep"] = out


def resolve_bleeps(spec: dict, words: list[dict]) -> list[tuple[float, float]]:
    """Sets spec["_bleeps"]: merged (start, end) windows in source seconds inside the segment's in/out,
    and spec["_bleepWords"]: start times of the transcript words they cover. Returns the windows."""
    b = spec.get("bleep")
    spec["_bleeps"], spec["_bleepWords"] = [], []
    if not b:
        return []
    a, z, speed, pad = spec["in"], spec["out"], spec["speed"], b["pad"]
    raw: list[tuple[float, float]] = []
    if b["words"] is not None:
        for w in words:
            if w["e"] <= a or w["s"] >= z or norm_word(w["w"]) not in b["words"]:
                continue
            raw.append((w["s"] - pad, w["e"] + pad))
            spec["_bleepWords"].append(w["s"])
    for win in b["windows"]:
        s = a + win["at"] * speed
        raw.append((s - pad, s + win["for"] * speed + pad))
    merged: list[list[float]] = []
    for s, e in sorted((max(s, a), min(e, z)) for s, e in raw):
        if e <= s:
            continue
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    spec["_bleeps"] = [(round(s, 3), round(e, 3)) for s, e in merged]
    if b["words"] is None:  # time windows: mask whichever words they cover
        spec["_bleepWords"] = [w["s"] for w in words if covered(w, spec["_bleeps"])]
    return spec["_bleeps"]


def covered(w: dict, windows: list[tuple[float, float]]) -> bool:
    """True when at least half of the word sits inside a bleep window."""
    length = max(1e-3, w["e"] - w["s"])
    inside = sum(max(0.0, min(w["e"], e) - max(w["s"], s)) for s, e in windows)
    return inside / length >= 0.5


def mask(word: str) -> str:
    """'fucking,' → 'f******,' — first letter kept, punctuation kept."""
    core = word.rstrip(",.;:!?\"”")
    tail = word[len(core):]
    letters = [i for i, ch in enumerate(core) if ch.isalpha()]
    if len(letters) < 2:
        return word
    chars = list(core)
    for i in letters[1:]:
        chars[i] = "*"
    return "".join(chars) + tail


def masked_words(spec: dict, words: list[dict]) -> list[dict]:
    """Transcript words with the bleeped ones masked, for this segment's captions."""
    hit = set(spec.get("_bleepWords") or [])
    if not hit:
        return words
    return [{**w, "w": mask(str(w["w"]))} if w["s"] in hit else w for w in words]


def unbleeped_swears(spec: dict, words: list[dict]) -> list[str]:
    """Swear words spoken inside the segment that no bleep covers."""
    a, z = spec["in"], spec["out"]
    wins = spec.get("_bleeps") or []
    return [
        norm_word(w["w"]) for w in words
        if w["e"] > a and w["s"] < z and norm_word(w["w"]) in SWEARS and not covered(w, wins)
    ]


def _between(windows: list[tuple[float, float]]) -> str:
    return "+".join(f"between(t,{s:.3f},{e:.3f})" for s, e in windows)


def mute_filter(spec: dict) -> str | None:
    """Audio filter (source time from the segment's in point, before atempo) that silences the bleeps."""
    wins = spec.get("_bleeps") or []
    if not wins:
        return None
    a = spec["in"]
    rel = [(s - a, e - a) for s, e in wins]
    return f"volume=0:enable='{_between(rel)}'"


def tone_graph(spec: dict, duration: float, label: str = "bt") -> str | None:
    """A filter-graph chain producing [label]: the censor tone during the bleeps (reel time), silent elsewhere."""
    b = spec.get("bleep")
    wins = spec.get("_bleeps") or []
    if not b or not wins or b["sound"] != "tone":
        return None
    a, speed = spec["in"], spec["speed"]
    out = [((s - a) / speed, (e - a) / speed) for s, e in wins]
    return (
        f"sine=frequency={TONE_HZ}:sample_rate=48000:duration={duration:.4f},"
        f"volume=0:enable='not({_between(out)})',volume={TONE_VOLUME:g},"
        f"aformat=sample_rates=48000:channel_layouts=stereo[{label}]"
    )
