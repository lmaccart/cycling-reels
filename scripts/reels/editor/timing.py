"""Frame-domain helpers and raw-plan timing edits for the native editor.

The renderer owns final timing.  These helpers are deliberately limited to
editing intent; compiled positions are supplied by ``render.build`` and passed
in when a gesture is anchored to a global frame.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def seconds_to_frames(seconds: float, fps: int) -> int:
    """Use the same Python ``round`` rule as the renderer."""
    return round(float(seconds) * fps)


def frames_to_seconds(frames: int, fps: int) -> float:
    return frames / fps


def source_duration(segment: dict[str, Any]) -> float:
    if "duration" in segment and "in" not in segment and "out" not in segment:
        return float(segment.get("duration", 2.5))
    speed = float(segment.get("speed", 1.0) or 1.0)
    return max(0.0, (float(segment.get("out", 0)) - float(segment.get("in", 0))) / speed)


def timeline_positions(segments: list[dict[str, Any]], fps: int, fade_frames: int) -> list[dict[str, int]]:
    """Approximate raw-plan positions using renderer fade semantics.

    For authoritative positions use ``render.build``. This helper is useful
    while forming an edit candidate before compilation.
    """
    positions: list[dict[str, int]] = []
    start = 0
    prior_frames = 0
    for index, segment in enumerate(segments):
        frames = max(1, seconds_to_frames(source_duration(segment), fps))
        trans = 0
        if index and segment.get("transition", "cut") == "fade":
            trans = max(0, min(fade_frames, prior_frames - 1, frames - 1))
        if index:
            start = positions[-1]["startFrame"] + prior_frames - trans
        positions.append({"startFrame": start, "endFrame": start + frames, "frames": frames, "transitionInFrames": trans})
        prior_frames = frames
    return positions


def _clip_events(value: Any, start: float, end: float, *, rebase: bool = False) -> Any:
    """Clip/rebase common segment-local ``at``/``for`` event collections."""
    if not isinstance(value, list):
        return deepcopy(value)
    kept = []
    for event in value:
        if isinstance(event, dict) and event.get("at") is None and event.get("endAt") is not None:
            at = float(event["endAt"])
            if start < at <= end:
                copy = deepcopy(event)
                copy["endAt"] = round(at - (start if rebase else 0), 6)
                kept.append(copy)
            continue
        if not isinstance(event, dict) or event.get("at") is None:
            kept.append(deepcopy(event))
            continue
        at = float(event["at"])
        duration = float(event["for"]) if event.get("for") is not None else max(0.0, end - at)
        event_end = at + duration
        left, right = max(at, start), min(event_end, end)
        if right <= left:
            continue
        copy = deepcopy(event)
        copy["at"] = round(left - start if rebase else left, 6)
        if "for" in copy:
            copy["for"] = round(right - left, 6)
        kept.append(copy)
    return kept


def _slice_cutaways(value: Any, start: float, end: float, *, rebase: bool) -> Any:
    if not isinstance(value, list):
        return deepcopy(value)
    out = []
    for cutaway in value:
        if not isinstance(cutaway, dict) or cutaway.get("at") is None:
            out.append(deepcopy(cutaway))
            continue
        speed = float(cutaway.get("speed", 1.0) or 1.0)
        at = float(cutaway["at"])
        if cutaway.get("duration") is not None and "in" not in cutaway:
            duration = float(cutaway["duration"])
        elif cutaway.get("out") is not None and cutaway.get("in") is not None:
            duration = (float(cutaway["out"]) - float(cutaway["in"])) / speed
        else:
            duration = float(cutaway.get("duration", 2.5))
        end_at = at + duration
        left, right = max(at, start), min(end_at, end)
        if right - left < 0.4:
            continue
        item = deepcopy(cutaway)
        source_in = float(cutaway.get("in", 0))
        item["at"] = round(left - start if rebase else left, 6)
        if "out" in item:
            item["in"] = round(source_in + (left - at) * speed, 6)
            item["out"] = round(source_in + (right - at) * speed, 6)
        elif "duration" in item:
            item["duration"] = round(right - left, 6)
        out.append(item)
    return out


def _slice_keys(keys: list, start: float, end: float, *, rebase: bool) -> list:
    """Keep boundary-interpolated keys so a split retains its camera/blur pose."""
    ordered = sorted((deepcopy(k) for k in keys if isinstance(k, dict) and "at" in k),
                     key=lambda k: float(k["at"]))
    if not ordered:
        return []
    def at_time(at):
        before = [k for k in ordered if float(k["at"]) <= at]
        after = [k for k in ordered if float(k["at"]) >= at]
        left = before[-1] if before else ordered[0]
        right = after[0] if after else ordered[-1]
        result = deepcopy(left)
        distance = float(right["at"]) - float(left["at"])
        if distance:
            fraction = (at - float(left["at"])) / distance
            for key, value in left.items():
                other = right.get(key)
                if key != "at" and isinstance(value, (int, float)) and isinstance(other, (int, float)):
                    result[key] = value + (other - value) * fraction
        result["at"] = at
        return result
    selected = [at_time(start)] + [k for k in ordered if start < float(k["at"]) < end] + [at_time(end)]
    for key in selected:
        key["at"] = round(float(key["at"]) - (start if rebase else 0), 6)
    return selected


def slice_local_events(segment: dict[str, Any], start: float, end: float, *, rebase: bool = False) -> dict[str, Any]:
    """Return segment metadata valid in [start, end), in segment-local seconds.

    ``bleep`` word selectors are intentionally retained; the renderer resolves
    them again against corrected transcript words. Explicit timed bleeps and
    ordinary segment-local events are clipped and, for a right-hand split,
    rebased to zero.
    """
    result = deepcopy(segment)
    for key in ("text", "stats", "nat", "sfx", "cutaways", "punch", "blur", "path", "exhibits", "progress"):
        if key not in result or key == "punch" and isinstance(result[key], dict):
            continue
        if key == "cutaways":
            result[key] = _slice_cutaways(result[key], start, end, rebase=rebase)
        elif key == "path" and isinstance(result[key], list):
            result[key] = _slice_keys(result[key], start, end, rebase=rebase)
        elif isinstance(result[key], dict) and "at" in result[key]:
            clipped = _clip_events([result[key]], start, end, rebase=rebase)
            result[key] = clipped[0] if clipped else False
        else:
            result[key] = _clip_events(result[key], start, end, rebase=rebase)
    if isinstance(result.get("blur"), list):
        patches = []
        for patch in result["blur"]:
            if not isinstance(patch, dict) or not isinstance(patch.get("keys"), list):
                patches.append(patch)
                continue
            keys = patch["keys"]
            if not keys:
                continue
            patch["keys"] = _slice_keys(keys, start, end, rebase=rebase)
            patches.append(patch)
        result["blur"] = patches
    bleep = result.get("bleep")
    if isinstance(bleep, list):
        result["bleep"] = _clip_events(bleep, start, end, rebase=rebase)
    elif isinstance(bleep, dict) and isinstance(bleep.get("at"), (int, float)):
        result["bleep"] = _clip_events([bleep], start, end, rebase=rebase)
    elif isinstance(bleep, dict) and isinstance(bleep.get("windows"), list):
        bleep["windows"] = _clip_events(bleep["windows"], start, end, rebase=rebase)
    sphere = result.get("sphere")
    if isinstance(sphere, dict) and isinstance(sphere.get("path"), list):
        sphere["path"] = _slice_keys(sphere["path"], start, end, rebase=rebase)
    return result


def ripple_global_events(plan: dict[str, Any], boundary_frame: int, delta_frames: int, fps: int) -> None:
    """Ripple top-level timed events after a changed segment boundary.

    Events straddling the boundary are shortened; an event wholly after it is
    shifted. The segment-local event collections are never touched here.
    """
    boundary = boundary_frame / fps
    delta = delta_frames / fps
    for key in ("text", "musicDrops", "sfx", "exhibits"):
        items = plan.get(key)
        if not isinstance(items, list):
            continue
        kept = []
        for item in items:
            if not isinstance(item, dict) or item.get("at") is None:
                kept.append(item)
                continue
            at = float(item["at"])
            duration = float(item.get("for", 0) or 0)
            end = float(item.get("endAt", at + duration) or at + duration)
            if at >= boundary:
                item["at"] = round(max(0.0, at + delta), 6)
                if "endAt" in item:
                    item["endAt"] = round(max(0.0, end + delta), 6)
            elif duration and end > boundary:
                new_end = max(at, end + delta)
                if "for" in item:
                    item["for"] = round(new_end - at, 6)
                if "endAt" in item:
                    item["endAt"] = round(new_end, 6)
                if new_end <= at:
                    continue
            kept.append(item)
        plan[key] = kept


def trim_segment(
    plan: dict[str, Any],
    index: int,
    source_in: float,
    source_out: float,
    *,
    fps: int,
    start_frame: int,
    old_frames: int,
    ripple: bool = True,
) -> int:
    """Trim video source bounds and ripple global events after its old end."""
    segments = plan["segments"]
    segment = segments[index]
    trim_local_source(segment, source_in, source_out)
    new_frames = max(1, seconds_to_frames(source_duration(segment), fps))
    new_end = start_frame + new_frames
    old_end = start_frame + old_frames
    if ripple:
        ripple_global_events(plan, old_end, new_end - old_end, fps)
    return new_frames


def trim_local_source(segment: dict[str, Any], source_in: float, source_out: float) -> None:
    """Trim a video in place, rebasing only its own events (never its parent)."""
    import math
    if "duration" in segment and "in" not in segment:
        raise ValueError("photos use duration, not source In/Out")
    speed = float(segment.get("speed", 1) or 1)
    if not all(math.isfinite(v) for v in (source_in, source_out, speed)) or speed <= 0:
        raise ValueError("source bounds and speed must be finite, with positive speed")
    if source_in < 0 or source_out <= source_in:
        raise ValueError("sourceOut must be greater than nonnegative sourceIn")
    offset = (source_in - float(segment.get("in", 0))) / speed
    sliced = slice_local_events(segment, offset, offset + (source_out - source_in) / speed, rebase=True)
    segment.clear()
    segment.update(sliced)
    segment.pop("moment", None)
    segment["in"], segment["out"] = round(source_in, 3), round(source_out, 3)


def split_segment(
    segment: dict[str, Any],
    local_frame: int,
    *,
    fps: int,
    total_frames: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split a source segment at a segment-local reel frame."""
    if "duration" in segment and "in" not in segment:
        raise ValueError("photo splitting is not supported by source-time split")
    speed = float(segment.get("speed", 1.0) or 1.0)
    local = local_frame / fps
    source_mid = float(segment["in"]) + local * speed
    source_mid = min(float(segment["out"]), max(float(segment["in"]), source_mid))
    local_end = total_frames / fps
    left = slice_local_events(segment, 0.0, local, rebase=False)
    right = slice_local_events(segment, local, local_end, rebase=True)
    left.update({"in": segment["in"], "out": round(source_mid, 3)})
    right.update({"in": round(source_mid, 3), "out": segment["out"]})
    # The source segment's outgoing transition belongs on the right half;
    # the new internal cut is hard.
    left["transition"] = segment.get("transition", "cut")
    right["transition"] = "cut"
    # Keep word/beat selectors: build regenerates their derived events for each
    # half. Dropping selectors would silently remove the requested effect.
    return left, right
