from __future__ import annotations

import copy
import re
import secrets
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

# Moment shelves. Definitions and borderline cases: .claude/skills/cycling-reels/reference/taxonomy.md
ROLES = ["voice", "reaction", "setup", "effort", "proof", "crew", "texture"]
SPEAKERS = ["me", "teammate", "none"]
MOODS = ["funny", "suffering", "calm", "fast", "awe", "honest"]
LINES = ["confession", "plan", "complaint", "joke", "result", "tip"]
BEATS = ["setup", "effort", "turn", "result"]
# Optional in the validator so older records stay valid; ./reels tag warns when new moments leave them out.
STANDARD_MOMENT_FIELDS = ("role", "speaker", "face", "mood", "opener")

MOMENT_ID = re.compile(r"m-[0-9a-f]{6}")
THREAD_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# Intersection over union: above this, a re-tagged span is the same moment with a tweaked trim.
SAME_SPAN = 0.75

_MOMENT_KEYS = {"id", "in", "out", "label", "score", "focusX", "focusY", "speech", "type", "view",
                "role", "speaker", "face", "mood", "opener", "line", "thread", "beat"}  # fmt: skip
_MERGE_KEYS = {"type", "shot", "quality", "issues", "people", "summary", "hooks", "location", "notes",
               "moments", "sounds", "angles", "score", "focusX", "focusY", "landmarks"}  # fmt: skip


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


def _landmarks(raw: Any, dur: float, errors: list[str]) -> list[dict]:
    """Rough signposts along a long clip, so untagged stretches can be found: {"at", "label"}."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        errors.append("landmarks must be a list")
        return []
    out = []
    for n, lm in enumerate(raw):
        where = f"landmarks[{n}]"
        if not isinstance(lm, dict):
            errors.append(f"{where} must be an object")
            continue
        at = _float(lm.get("at"), f"{where}.at", 0, dur, errors)
        label = lm.get("label")
        if not isinstance(label, str) or not label.strip():
            errors.append(f"{where}.label is required (what's happening at that point)")
            continue
        out.append({"at": round(at, 2), "label": label.strip()[:120]})
    return sorted(out, key=lambda x: x["at"])


def _shelves(m: dict, mm: dict, where: str, errors: list[str]) -> None:
    """Copies the optional shelf fields from raw moment m to normalised moment mm."""
    for key, allowed in (("role", ROLES), ("speaker", SPEAKERS), ("mood", MOODS), ("line", LINES)):
        if m.get(key) is not None:
            mm[key] = _choice(m[key], allowed, f"{where}.{key}", errors)
    for key in ("face", "opener"):
        if m.get(key) is not None:
            if isinstance(m[key], bool):
                mm[key] = m[key]
            else:
                errors.append(f"{where}.{key} must be true or false (got {m[key]!r})")
    if m.get("line") is not None and m.get("role") != "voice":
        errors.append(f"{where}.line only applies when role is voice (role is {m.get('role')!r})")
    thread = m.get("thread")
    if thread is not None:
        if isinstance(thread, str) and THREAD_SLUG.fullmatch(thread):
            mm["thread"] = thread
        else:
            errors.append(f"{where}.thread must be a kebab-case slug like road-to-5-ep1 (got {thread!r})")
    if m.get("beat") is not None:
        mm["beat"] = _choice(m["beat"], BEATS, f"{where}.beat", errors)
        if thread is None:
            errors.append(f"{where}.beat needs a thread")


def _new_moment_id(taken: set[str]) -> str:
    while True:
        mid = f"m-{secrets.token_hex(3)}"
        if mid not in taken:
            taken.add(mid)
            return mid


def _span_match(a: dict, b: dict) -> float:
    shared = min(a["out"], b["out"]) - max(a["in"], b["in"])
    if shared <= 0:
        return 0.0
    return shared / (max(a["out"], b["out"]) - min(a["in"], b["in"]))


def _with_ids(moments: list[dict], given: list[str | None], prior: list[dict]) -> list[dict]:
    """Gives every moment an id: the one it was given, else that of the same span in the previous
    tags (best match first, so a tweaked trim can't steal an exact match), else a new one that no
    previous moment of this clip has used."""
    ids = list(given)
    candidates = [p for p in prior if p.get("id") and p["id"] not in given]
    pairs = sorted(((_span_match(m, p), n, k) for n, m in enumerate(moments) for k, p in enumerate(candidates)),
                   key=lambda x: -x[0])  # fmt: skip
    claimed: set[int] = set()
    for match, n, k in pairs:
        if match >= SAME_SPAN and ids[n] is None and k not in claimed:
            ids[n] = candidates[k]["id"]
            claimed.add(k)
    taken = {p["id"] for p in prior if p.get("id")} | {i for i in ids if i}
    return [{"id": mid or _new_moment_id(taken), **m} for mid, m in zip(ids, moments)]


def backfill_moment_ids(tags: dict) -> int:
    """Adds ids to moments that have none, in place. Returns how many it added."""
    moments = tags.get("moments") or []
    taken = {m["id"] for m in moments if m.get("id")}
    added = 0
    for n, m in enumerate(moments):
        if not m.get("id"):
            moments[n] = {"id": _new_moment_id(taken), **m}
            added += 1
    return added


def missing_standard_fields(tags: dict) -> list[str]:
    """One line per moment that lacks a standard shelf field."""
    out = []
    for m in tags.get("moments") or []:
        gone = [f for f in STANDARD_MOMENT_FIELDS if f not in m]
        if gone:
            out.append(f"{m.get('id') or m.get('label')}: {', '.join(gone)}")
    return out


def validate_tags(asset: dict, raw: dict) -> dict:
    """Normalises Claude's classification of one asset; raises with every problem at once."""
    if not isinstance(raw, dict):
        raise ReelsError("tags must be a JSON object")
    if raw.get("skip"):
        return {"skip": True, "reason": str(raw.get("reason", ""))[:200]}
    t, errors = _check_tags(asset, raw)
    _raise(errors)
    return t


def _raise(errors: list[str]) -> None:
    if errors:
        raise ReelsError("tag problems:\n  - " + "\n  - ".join(errors))


def merge_tags(asset: dict, patch: dict) -> dict:
    """Applies a partial update to an asset's saved tags and returns the new tags.

    Only what the patch names changes: moments are addressed by id and take just the supplied
    fields, and a null removes an optional field. "addMoments" adds new moments, which get the
    same validation as a full tag and new ids, and may not overlap anything already on the clip.
    The whole merged record is validated, but everything the patch didn't name is returned
    exactly as saved, since validation would clip long notes and labels and add empty lists.
    """
    prior = asset.get("tags") or {}
    if not isinstance(patch, dict):
        raise ReelsError("tags must be a JSON object")
    if not prior or prior.get("skip"):
        raise ReelsError(f"{asset['id']} has no tags to merge into; tag it in full first")

    errors = [f"can't merge {key!r}: not a tag field" + (" (use ./reels skip)" if key in ("skip", "reason") else "")
              for key in patch if key not in _MERGE_KEYS and key != "addMoments"]  # fmt: skip
    result = copy.deepcopy(prior)
    backfill_moment_ids(result)
    changes = patch.get("moments", [])
    if not isinstance(changes, list):
        errors.append("moments must be a list of {id, ...} updates")
        changes = []
    adds = patch.get("addMoments", [])
    if not isinstance(adds, list):
        errors.append("addMoments must be a list of new moments")
        adds = []

    merged = copy.deepcopy(result)
    for key, value in patch.items():
        if key in _MERGE_KEYS and key != "moments":
            _set_or_clear(merged, key, value)
    by_id = {m["id"]: m for m in merged.get("moments") or []}
    named: dict[str, dict] = {}
    for n, change in enumerate(changes):
        where = f"patch moments[{n}]"
        if not isinstance(change, dict):
            errors.append(f"{where} must be an object")
            continue
        mid = change.get("id")
        if mid is None:
            errors.append(f"{where} needs an id (to add moments use addMoments; to remove them, re-tag in full)")
        elif not isinstance(mid, str) or mid not in by_id:
            errors.append(f"{where}: no moment {mid!r} on this clip")
        elif mid in named:
            errors.append(f"{where}: moment {mid} is updated twice")
        else:
            bad = [k for k in change if k not in _MOMENT_KEYS]
            errors.extend(f"{where}: unknown field {k!r}" for k in bad)
            named[mid] = {k: v for k, v in change.items() if k != "id" and k not in bad}
            for key, value in named[mid].items():
                _set_or_clear(by_id[mid], key, value)

    checked, problems = _check_tags({**asset, "tags": result}, merged)
    added = _new_moments(asset, merged, checked, adds, errors)
    _raise(errors + problems)

    for key in patch:
        if key not in ("moments", "addMoments"):
            _set_or_clear(result, key, checked.get(key))
    checked_by_id = {m["id"]: m for m in checked.get("moments") or []}
    for moment in result.get("moments") or []:
        for key in named.get(moment["id"], ()):
            _set_or_clear(moment, key, checked_by_id[moment["id"]].get(key))
    if added:
        result.setdefault("moments", []).extend(added)
    if added or any("in" in fields for fields in named.values()):
        result["moments"].sort(key=lambda m: m["in"])
    return result


def _new_moments(asset: dict, merged: dict, checked: dict, adds: list, errors: list[str]) -> list[dict]:
    """Validates the moments to add against the merged record; returns them with fresh ids."""
    if not adds:
        return []
    if asset["kind"] != "video":
        errors.append("addMoments only apply to video clips")
        return []
    raw: list[Any] = []
    for n, m in enumerate(adds):
        if isinstance(m, dict):
            errors.extend(f"addMoments[{n}]: unknown field {k!r}" for k in m if k not in _MOMENT_KEYS)
            if "id" in m:
                errors.append(f"addMoments[{n}]: new moments get their id from the tool; leave it out")
            m = {k: v for k, v in m.items() if k in _MOMENT_KEYS and k != "id"}
        raw.append(m)
    # _check_tags repeats the whole-record problems the caller already has, so only its addMoments ones count here.
    new, problems = _check_tags({**asset, "tags": merged}, {**merged, "moments": raw}, moments_field="addMoments")
    bad = [p for p in problems if p.startswith("addMoments[")]
    errors.extend(bad)
    if bad:
        return []
    moments = new["moments"]
    for n, m in enumerate(moments):
        for other in moments[:n] + (checked.get("moments") or []):
            if min(m["out"], other["out"]) - max(m["in"], other["in"]) > 0:
                errors.append(f"addMoments {m['in']:g}-{m['out']:g} overlaps {other.get('id')} {other['in']:g}-{other['out']:g}")
    return moments


def _set_or_clear(target: dict, key: str, value: Any) -> None:
    if value is None:
        target.pop(key, None)
    else:
        target[key] = value


def _check_tags(asset: dict, raw: dict, moments_field: str = "moments") -> tuple[dict, list[str]]:
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
        given_ids: list[str | None] = []
        prior = (asset.get("tags") or {}).get("moments") or []
        prior_ids = {p.get("id") for p in prior} - {None}
        moments = raw.get("moments") or []
        if not isinstance(moments, list):
            errors.append("moments must be a list")
            moments = []
        for n, m in enumerate(moments):
            where = f"{moments_field}[{n}]"
            if not isinstance(m, dict):
                errors.append(f"{where} must be an object")
                continue
            mid = m.get("id")
            if mid is not None:
                where = f"{moments_field}[{n}] ({mid})"
                if not isinstance(mid, str) or not MOMENT_ID.fullmatch(mid):
                    errors.append(f"{where}: id must look like m-1a2b3c")
                    mid = None
                elif mid not in prior_ids:
                    errors.append(f"{where}: not a moment on this clip (leave id out for a new moment)")
                    mid = None
                elif mid in given_ids:
                    errors.append(f"{where}: the same id is on two moments")
                    mid = None
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
            _shelves(m, mm, where, errors)
            if not mm["label"]:
                errors.append(f"{where}.label is required")
            moments_out.append(mm)
            given_ids.append(mid)
        t["moments"] = sorted(_with_ids(moments_out, given_ids, prior), key=lambda x: x["in"])
        t["sounds"] = _sounds(raw.get("sounds"), dur, errors)
        landmarks = _landmarks(raw.get("landmarks"), dur, errors)
        if landmarks:  # left out when empty so older records don't change and [] clears them in a merge
            t["landmarks"] = landmarks
    elif raw.get("sounds"):
        errors.append("sounds only apply to video clips")
    elif raw.get("landmarks"):
        errors.append("landmarks only apply to video clips")
    else:
        t["score"] = _int(raw.get("score", t["quality"]), "score", 1, 5, errors)
        t["focusX"] = round(_float(raw.get("focusX", 0.5), "focusX", 0, 1, errors), 3)
        t["focusY"] = round(_float(raw.get("focusY", 0.5), "focusY", 0, 1, errors), 3)

    return t, errors
