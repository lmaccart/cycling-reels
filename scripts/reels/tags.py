from __future__ import annotations

from typing import Any

from .sphere import parse_angles, parse_view
from .util import ReelsError

TYPES = [
    "race",
    "climb",
    "descent",
    "sprint",
    "group-ride",
    "solo-ride",
    "scenery",
    "bike-check",
    "gear",
    "indoor",
    "talking",
    "cafe",
    "travel",
    "other",
]
SHOTS = ["pov", "chase", "roadside", "selfie", "handheld", "static", "aerial", "screen", "photo"]
ISSUES = ["shaky", "dark", "overexposed", "blurry", "wind-noise", "obstructed", "low-res", "boring"]
PEOPLE = ["none", "me", "others", "group"]
SOUND_KINDS = ["freehub", "clip-in", "shift", "pass-by", "gravel", "bell", "brakes", "chain", "tire-hum", "cheer", "crowd", "car", "other"]


def _choice(value: Any, allowed: list[str], field: str, errors: list[str]) -> Any:
    if value not in allowed:
        errors.append(f"{field} must be one of: {', '.join(allowed)} (got {value!r})")
    return value


def _float(value: Any, field: str, lo: float, hi: float, errors: list[str]) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be a number (got {value!r})")
        return lo
    if not (lo <= v <= hi):
        errors.append(f"{field}={v} is outside {lo}–{hi}")
    return v


def _int(value: Any, field: str, lo: int, hi: int, errors: list[str]) -> int:
    try:
        v = int(value)
    except (TypeError, ValueError):
        errors.append(f"{field} must be an integer {lo}–{hi} (got {value!r})")
        return lo
    if not (lo <= v <= hi):
        errors.append(f"{field}={v} is outside {lo}–{hi}")
    return v


def _sounds(raw: Any, dur: float, errors: list[str]) -> list[dict]:
    """Clean, usable ride sounds: {"at", "for", "kind", "score", "note"}."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append("sounds must be a list")
        return []
    out = []
    for n, s in enumerate(raw):
        where = f"sounds[{n}]"
        if not isinstance(s, dict):
            errors.append(f"{where} must be an object")
            continue
        at = _float(s.get("at"), f"{where}.at", 0, dur, errors)
        length = _float(s.get("for"), f"{where}.for", 0.1, 30, errors)
        if at + length > dur + 0.05:
            errors.append(f"{where}: at + for ({at + length:.2f}) runs past the end of the clip ({dur:.2f})")
        note = str(s.get("note", "")).strip()[:160]
        if not note:
            errors.append(f"{where}.note is required (what's audible, e.g. 'freehub ticking as I sit up, little wind')")
        out.append(
            {
                "at": round(at, 2),
                "for": round(min(length, max(0.1, dur - at)), 2),
                "kind": _choice(s.get("kind"), SOUND_KINDS, f"{where}.kind", errors),
                "score": _int(s.get("score", 3), f"{where}.score", 1, 5, errors),
                "note": note,
            }
        )
    return sorted(out, key=lambda x: x["at"])


def validate_tags(asset: dict, raw: dict) -> dict:
    """Normalises Claude's classification of one asset; raises with every problem at once."""
    if not isinstance(raw, dict):
        raise ReelsError("tags must be a JSON object")
    if raw.get("skip"):
        return {"skip": True, "reason": str(raw.get("reason", ""))[:200]}

    errors: list[str] = []
    is_video = asset["kind"] == "video"
    t: dict[str, Any] = {}
    t["type"] = _choice(raw.get("type"), TYPES, "type", errors)
    t["shot"] = _choice(raw.get("shot", "photo" if not is_video else None), SHOTS, "shot", errors)
    t["quality"] = _int(raw.get("quality", 3), "quality", 1, 5, errors)
    issues = raw.get("issues") or []
    if not isinstance(issues, list):
        errors.append("issues must be a list")
        issues = []
    for i in issues:
        _choice(i, ISSUES, "issues[]", errors)
    t["issues"] = issues
    t["people"] = _choice(raw.get("people", "none"), PEOPLE, "people", errors)
    summary = str(raw.get("summary", "")).strip()
    if not summary:
        errors.append("summary is required (one line describing the shot)")
    t["summary"] = summary[:240]
    hooks = raw.get("hooks") or []
    t["hooks"] = [str(h).strip() for h in hooks if str(h).strip()][:6] if isinstance(hooks, list) else []
    for key in ("location", "notes"):
        if raw.get(key):
            t[key] = str(raw[key]).strip()[:240]

    is_360 = bool((asset.get("probe") or {}).get("sphere"))
    angles: dict = {}
    if is_360:
        angles = parse_angles(raw.get("angles"), errors)
        t["angles"] = angles
    elif raw.get("angles"):
        errors.append("angles only apply to 360 footage — use focusX/focusY for normal clips")

    if is_video:
        dur = float(asset["probe"]["duration"])
        moments_out = []
        moments = raw.get("moments") or []
        if not isinstance(moments, list):
            errors.append("moments must be a list")
            moments = []
        for n, m in enumerate(moments):
            where = f"moments[{n}]"
            if not isinstance(m, dict):
                errors.append(f"{where} must be an object")
                continue
            a = _float(m.get("in"), f"{where}.in", 0, dur, errors)
            b = _float(m.get("out"), f"{where}.out", 0, dur + 0.05, errors)
            b = min(b, dur)
            if b - a < 0.5:
                errors.append(f"{where}: out must be at least 0.5 s after in")
            if b - a > 60:
                errors.append(f"{where}: keep moments under 60 s (split long stretches)")
            mm: dict[str, Any] = {
                "in": round(a, 2),
                "out": round(b, 2),
                "label": str(m.get("label", "")).strip()[:120],
                "score": _int(m.get("score", 3), f"{where}.score", 1, 5, errors),
                "focusX": round(_float(m.get("focusX", 0.5), f"{where}.focusX", 0, 1, errors), 3),
                "focusY": round(_float(m.get("focusY", 0.5), f"{where}.focusY", 0, 1, errors), 3),
                "speech": bool(m.get("speech", False)),
            }
            if m.get("type"):
                mm["type"] = _choice(m["type"], TYPES, f"{where}.type", errors)
            if m.get("view") is not None:
                if not is_360:
                    errors.append(f"{where}.view only applies to 360 footage")
                else:
                    v = m["view"]
                    parse_view(v, angles, errors, f"{where}.view")  # validate; stored as given so names stay readable
                    mm["view"] = v.strip().lower() if isinstance(v, str) else v
            if not mm["label"]:
                errors.append(f"{where}.label is required")
            moments_out.append(mm)
        t["moments"] = sorted(moments_out, key=lambda x: x["in"])
        t["sounds"] = _sounds(raw.get("sounds"), dur, errors)
    elif raw.get("sounds"):
        errors.append("sounds only apply to video clips")
    else:
        t["score"] = _int(raw.get("score", t["quality"]), "score", 1, 5, errors)
        t["focusX"] = round(_float(raw.get("focusX", 0.5), "focusX", 0, 1, errors), 3)
        t["focusY"] = round(_float(raw.get("focusY", 0.5), "focusY", 0, 1, errors), 3)

    if errors:
        raise ReelsError("tag problems:\n  - " + "\n  - ".join(errors))
    return t
