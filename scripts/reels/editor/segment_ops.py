"""Raw-plan mechanics for duplicate_segments, ripple_remove, paste_segments and set_enabled.

Everything here edits ``plan["segments"]`` in place and keeps object identity for the items that stay,
so ``DocumentStore._apply_edit_operation`` can carry node IDs across the edit. Copies are new objects
and receive fresh IDs when the node index is rebuilt.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..timeline import positioned, video_track
from .document import _descriptors, node_location
from .tracks import track_list

# Editor metadata that never travels with a pasted segment.
PASTE_STRIP = ("timeline", "_editor", "id", "__timelineEditorId")


def primary_segments(plan: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    """The segments behind ``ids``, deduplicated and in plan order."""
    segments = plan.get("segments") or []
    picked: list[dict[str, Any]] = []
    for node_id in ids:
        _, item = node_location(plan, node_id)
        if not any(item is candidate for candidate in segments):
            raise ValueError(f"{node_id} is not a primary segment")
        if not any(item is candidate for candidate in picked):
            picked.append(item)
    return sorted(picked, key=lambda item: next(i for i, candidate in enumerate(segments) if candidate is item))


def _index(plan: dict[str, Any], item: dict[str, Any]) -> int:
    return next(i for i, candidate in enumerate(plan["segments"]) if candidate is item)


def _bounds(clip: dict[str, Any]) -> tuple[int, int]:
    value = clip["timeline"]
    return value["startFrame"], value["startFrame"] + value["durationFrames"]


def _track(clip: dict[str, Any]) -> str:
    return clip["timeline"]["track"]


def set_enabled(plan: dict[str, Any], ids: list[str], enabled: bool) -> None:
    if not isinstance(enabled, bool):
        raise ValueError("set_enabled.enabled must be true or false")
    for clip in primary_segments(plan, ids):
        if enabled:
            clip.pop("enabled", None)
        else:
            clip["enabled"] = False


def duplicate(plan: dict[str, Any], ids: list[str], ctx: dict[str, Any]) -> None:
    picked = primary_segments(plan, ids)
    if positioned(plan):
        copies = _duplicate_positioned(plan, picked)
    else:
        copies = [deepcopy(item) for item in picked]
    last = _index(plan, picked[-1])
    plan["segments"][last + 1:last + 1] = copies
    ctx["copyPairs"] = list(zip(picked, copies))
    ctx["newObjects"] = copies


def _duplicate_positioned(plan: dict[str, Any], picked: list[dict[str, Any]]) -> list[dict[str, Any]]:
    start = min(_bounds(item)[0] for item in picked)
    end = max(_bounds(item)[1] for item in picked)
    shift = end - start
    for item in picked:
        if item.get("enabled") is False:
            continue
        a, b = _bounds(item)
        for other in plan["segments"]:
            if other.get("enabled") is False:
                continue
            c, d = _bounds(other)
            if _track(other) == _track(item) and c < b + shift and d > a + shift:
                raise ValueError(
                    f"No room to duplicate: frames {a + shift}-{b + shift} on {_track(item)} are occupied. "
                    "Move or trim the clip after it, or paste it somewhere free.")
    copies = []
    for item in picked:
        copy = deepcopy(item)
        stamp = copy["timeline"]
        a, _ = _bounds(item)
        # A fade overlaps its predecessor; keep it only when that predecessor is copied too.
        partner = next((other for other in picked if other is not item and _track(other) == _track(item)
                        and _bounds(other)[0] < a < _bounds(other)[1]), None)
        stamp["startFrame"] = a + shift
        if partner is None and stamp.get("transitionInFrames", 0):
            stamp["transitionInFrames"] = 0
            copy["transition"] = "cut"
        copies.append(copy)
    plan["timeline"]["durationFrames"] = max(
        plan["timeline"]["durationFrames"], max((_bounds(copy)[1] for copy in copies
                                               if copy.get("enabled") is not False), default=0))
    return copies


def ripple_remove(plan: dict[str, Any], ids: list[str], affected: set[str]) -> list[list[int]]:
    picked = primary_segments(plan, ids)
    affected.update(ids)
    segments = plan["segments"]
    if not positioned(plan):
        segments[:] = [item for item in segments if not any(item is gone for gone in picked)]
        return []
    gone = {id(item) for item in picked}
    survivors = [item for item in segments if id(item) not in gone]
    # Each removed clip frees [a, b) on its own track, minus any fade overlap it shared with a neighbour
    # that stays: that neighbour keeps its frames.
    intervals = []
    for item in picked:
        a, b = _bounds(item)
        for other in survivors:
            c, d = _bounds(other)
            if (_track(other) != _track(item) or item.get("enabled") is False
                    or other.get("enabled") is False):
                continue
            if c < a < d <= b and item["timeline"].get("transitionInFrames", 0) == d - a:
                a = d
            elif a < c < b < d and other["timeline"].get("transitionInFrames", 0) == b - c:
                b = c
        if a < b:
            intervals.append([a, b])
    merged: list[list[int]] = []
    for a, b in sorted(intervals):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    for other in survivors:
        c, d = _bounds(other)
        for a, b in merged:
            if c < b and d > a:
                raise ValueError(
                    f"Ripple delete is blocked: a clip on {_track(other)} (frames {c}-{d}) crosses the removed "
                    f"range {a}-{b}. Remove or trim it too, or use a plain delete.")
    orphaned = [other for other in survivors if other["timeline"].get("transitionInFrames", 0)
                and any(_track(item) == _track(other) and _bounds(item)[0] < _bounds(other)[0] < _bounds(item)[1]
                        for item in picked)]
    for other in survivors:
        other["timeline"]["startFrame"] -= sum(b - a for a, b in merged if b <= _bounds(other)[0])
    for other in orphaned:
        other["timeline"]["transitionInFrames"] = 0
        other["transition"] = "cut"
    segments[:] = survivors
    header = plan["timeline"]
    header["durationFrames"] = max(
        1, max((_bounds(other)[1] for other in survivors if other.get("enabled") is not False), default=0),
        header["durationFrames"] - sum(b - a for a, b in merged))
    return merged


def clean_value(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("asset"), str) or not value["asset"]:
        raise ValueError("each pasted value must be a segment object with an asset")
    clean = deepcopy(value)
    for key in PASTE_STRIP:
        clean.pop(key, None)
    return clean


def paste_sequential(plan: dict[str, Any], compiled: dict[str, Any], operation: dict[str, Any]) -> list[dict[str, Any]]:
    values = [clean_value(value) for value in operation["values"]]
    at = operation["atFrame"]
    if type(at) is not int or at < 0:
        raise ValueError("paste_segments.atFrame must be an integer >= 0")
    segments = plan.setdefault("segments", [])
    index = len(segments)
    if at < int(compiled["total"]):
        index = 0
        for position, seg in enumerate(compiled["segs"][:len(segments)]):
            if int(seg["start"]) <= at:
                index = position
    segments[index:index] = values
    return values


def _frames(value: dict[str, Any], fps: int, project) -> int:
    from ..library import load_asset, resolve_id, resolve_moment
    from .overwrite import video_source_bounds
    asset = load_asset(project, resolve_id(project, value["asset"]))
    if asset["kind"] != "video":
        return round(float(value.get("duration", 2.5)) * fps)
    if "moment" in value:
        moment = resolve_moment(asset, value["moment"])
        value.setdefault("in", moment["in"])
        value.setdefault("out", moment["out"])
    if "in" not in value or "out" not in value:
        raise ValueError("pasted videos need in/out or a moment")
    source_in, source_out, speed = video_source_bounds(asset, value["in"], value["out"], value.get("speed", 1))
    return round((source_out - source_in) * fps / speed)


def paste_placements(plan: dict[str, Any], operation: dict[str, Any], fps: int, project) -> list[dict[str, Any]]:
    """Positioned paste: anchor the earliest clip at atFrame, preserving layout regardless of input order."""
    raw = operation["values"]
    values = [clean_value(value) for value in raw]
    at = operation["atFrame"]
    if type(at) is not int or at < 0:
        raise ValueError("paste_segments.atFrame must be an integer >= 0")
    track = operation.get("track")
    if track is None:
        track = next((row["id"] for row in track_list(plan.get("_editor"), plan=plan) if row["kind"] == "video"), "V1")
    video_track(track)
    starts = [item.get("timeline", {}).get("startFrame") if isinstance(item, dict) and isinstance(item.get("timeline"), dict)
              else None for item in raw]
    if all(type(start) is int for start in starts):
        offsets = [start - min(starts) for start in starts]
    else:
        offsets, cursor = [], 0
        for value in values:
            offsets.append(cursor)
            cursor += _frames(value, fps, project)
    return [{"insert": value, "track": track, "atFrame": at + offset} for value, offset in zip(values, offsets)]


def inherit_tracks(plan: dict[str, Any], pairs: list[tuple[dict[str, Any], dict[str, Any]]]) -> None:
    """Copies keep the track assignment of the node they were copied from (stored in ``_editor.nodes``)."""
    nodes = plan["_editor"]["nodes"]
    roots = {id(segment): f"/segments/{index}" for index, segment in enumerate(plan["segments"])}
    by_path = {entry["locator"]: entry for entry in nodes.values()}
    for original, copy in pairs:
        source_root, target_root = roots[id(original)], roots[id(copy)]
        for desc in _descriptors({"segments": [original]}):
            suffix = desc["path"][len("/segments/0"):]
            source, target = by_path.get(source_root + suffix), by_path.get(target_root + suffix)
            if source and target and source.get("track"):
                target["track"] = source["track"]
