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


def _slice_cutaways(value: Any, start: float, end: float, *, rebase: bool,
                    minimum: float = 0.4, frame_exact=False) -> Any:
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
        if right - left < minimum - 1e-10:
            continue
        item = deepcopy(cutaway)
        rounded = (lambda value: value) if frame_exact else (lambda value: round(value, 6))
        source_in = float(cutaway.get("in", 0))
        item["at"] = rounded(left - start if rebase else left)
        if "out" in item:
            item["in"] = rounded(source_in + (left - at) * speed)
            item["out"] = rounded(source_in + (right - at) * speed)
        elif "duration" in item:
            item["duration"] = rounded(right - left)
            window = cutaway.get("motionWindow") or {"offsetSeconds": 0, "durationSeconds": duration}
            item["motionWindow"] = {**window, "offsetSeconds": window["offsetSeconds"] + left-at}
        if isinstance(item.get("path"), list):
            if frame_exact:
                window = cutaway.get("cameraWindow") or {
                    "offsetSeconds": 0, "durationSeconds": duration, "path": deepcopy(cutaway["path"])}
                item["cameraWindow"] = {**deepcopy(window), "offsetSeconds": window["offsetSeconds"] + left-at}
            item["path"] = _slice_keys(item["path"], left-at, right-at, rebase=True)
        if isinstance(item.get("sphere"), dict) and isinstance(item["sphere"].get("path"), list):
            item["sphere"]["path"] = _slice_keys(item["sphere"]["path"], left-at, right-at, rebase=True)
        out.append(item)
    return out


def _slice_keys(keys: list, start: float, end: float, *, rebase: bool,
                view: dict | None = None, projection: str = "flat") -> list:
    """Keep boundary-interpolated keys so a split retains its camera/blur pose.

    360 camera keys (numeric yaw) get their boundary pose from ``sphere.path_value``,
    the same shortest-arc path the renderer and the live viewer follow."""
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
        if at != float(left["at"]):
            result.pop("cut", None)
        distance = float(right["at"]) - float(left["at"])
        if right.get("cut") is True and at < float(right["at"]):
            distance = 0  # before a hard-cut keyframe the camera holds the previous pose
        if _is_camera_key(left) and _is_camera_key(right):
            result.update(_camera_pose(left, right, at, view=view, projection=projection))
        elif distance:
            fraction = (at - float(left["at"])) / distance
            for key, value in left.items():
                other = right.get(key)
                if key != "at" and isinstance(value, (int, float)) and isinstance(other, (int, float)):
                    result[key] = value + (other - value) * fraction
        result["at"] = at
        return result
    selected = [at_time(start)] + [k for k in ordered if start < float(k["at"]) < end] + [at_time(end)]
    selected[0].pop("cut", None)  # no incoming move at the piece's start
    for key in selected:
        key["at"] = round(float(key["at"]) - (start if rebase else 0), 6)
    return selected


def _is_camera_key(key: dict) -> bool:
    return isinstance(key.get("yaw"), (int, float)) and not isinstance(key.get("yaw"), bool)


def _camera_pose(left: dict, right: dict, at: float, *, view: dict | None = None,
                 projection: str = "flat") -> dict:
    """Sample complete views; missing components inherit the base, not the other key."""
    from ..sphere import parse_view, path_value

    def full(key):
        return {**key, **parse_view(key, {}, [], "split boundary", view)}

    pose = path_value([full(left), full(right)], at, projection)
    return {name: pose[name] for name in ("yaw", "pitch", "fov", "roll")}


MIN_KEY_GAP = 0.1  # plan_camera: path keyframes at least 0.1 s apart


def window_piece_keys(window_path: list, offset: float, seconds: float, *, projection: str = "flat") -> list:
    """A split piece's camera keys in its own 0…seconds, from its `cameraWindow` path.

    `window_path` holds resolved keys in window time; the piece starts `offset` into
    it. Keys outside the piece become boundary poses at 0 and `seconds` (on the same
    path the renderer follows), and keys too close to a boundary make way for it, so
    the result is a path `plan_camera` accepts for this piece."""
    if seconds < MIN_KEY_GAP or not any(isinstance(k, dict) and "at" in k for k in window_path):
        return []
    keys = _slice_keys(window_path, offset, offset + seconds, rebase=True, projection=projection)
    first, *inner, last = keys
    last["at"] = round(seconds, 3)
    if last["at"] > seconds:
        last["at"] = round(last["at"] - 0.001, 3)
    end_ms = round(last["at"] * 1000)
    kept = [first]
    previous_ms = 0
    for key in inner:
        key["at"] = round(key["at"], 3)
        at_ms = round(key["at"] * 1000)
        # Match plan_camera after millisecond rounding, including inner gaps
        # which can shrink when shifted from the source window.
        if at_ms - previous_ms >= 100 and end_ms - at_ms >= 100:
            kept.append(key)
            previous_ms = at_ms
    return [*kept, last]


def _key_times(path: list, duration: float) -> list:
    """`at` of each raw window key, with plan_camera's defaults for a missing one."""
    times = []
    for index, key in enumerate(path):
        at = key.get("at", 0 if index == 0 else duration) if isinstance(key, dict) else None
        times.append(float(at) if isinstance(at, (int, float)) and not isinstance(at, bool) else None)
    return times


def window_with_piece_keys(window: dict, segment: dict[str, Any], keys: list) -> dict | None:
    """`window` after an aim edit wrote `keys` (piece time) to a split piece's /path.

    The window path keeps its keys outside the piece, so a later trim still continues
    the original move, and takes the edited keys inside it. None when the window can't
    carry them (no path, unreadable times); the caller then drops the window."""
    path = window.get("path")
    duration = window.get("durationSeconds")
    offset = window.get("offsetSeconds", 0)
    if (not isinstance(path, list) or not isinstance(duration, (int, float))
            or not isinstance(offset, (int, float)) or isinstance(offset, bool)):
        return None
    try:
        seconds = source_duration(segment)
    except (TypeError, ValueError, ZeroDivisionError):
        seconds = 0.0
    inside = [deepcopy(k) for k in keys if isinstance(k, dict) and isinstance(k.get("at"), (int, float))]
    if len(inside) != len(keys) or not inside:
        return None
    times = _key_times(path, float(duration))
    if None in times:
        return None
    inside.sort(key=lambda k: float(k["at"]))
    piece = max(seconds, float(inside[-1]["at"]))
    # A local path holds outside its keys. Put matching anchors at the piece
    # edges (just outside for sub-0.1s gaps) before reconnecting exterior keys.
    lo, hi = 0.0, piece
    gap = MIN_KEY_GAP + 0.001  # stay valid after plan_camera's millisecond rounding
    if float(inside[0]["at"]) > 0:
        lo = min(0.0, float(inside[0]["at"]) - gap)
        if float(offset) + lo >= 0:
            inside.insert(0, {**deepcopy(inside[0]), "at": lo})
    inside[0].pop("cut", None)
    if float(inside[-1]["at"]) < piece:
        hi = max(piece, float(inside[-1]["at"]) + gap)
        inside.append({**deepcopy(inside[-1]), "at": hi, "cut": False})
    lo, hi = float(offset) + lo, float(offset) + hi
    before = [{**deepcopy(k), "at": at} for k, at in zip(path, times)
              if at < float(offset) - 1e-6 and at <= lo - MIN_KEY_GAP]
    after = [{**deepcopy(k), "at": at} for k, at in zip(path, times)
             if at > float(offset) + piece + 1e-6 and at >= hi + MIN_KEY_GAP]
    for key in inside:
        key["at"] = round(float(key["at"]) + float(offset), 6)
    return {**deepcopy(window), "durationSeconds": max(float(duration), hi),
            "path": before + sorted(inside, key=lambda k: k["at"]) + after}


def slice_local_events(segment: dict[str, Any], start: float, end: float, *, rebase: bool = False,
                       minimum_cutaway_frames: int | None = None, fps: int = 30) -> dict[str, Any]:
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
            result[key] = _slice_cutaways(result[key], start, end, rebase=rebase,
                minimum=minimum_cutaway_frames / fps if minimum_cutaway_frames is not None else 0.4,
                frame_exact=minimum_cutaway_frames is not None)
        elif key == "path" and isinstance(result[key], list):
            from ..sphere import parse_view
            base = parse_view(segment.get("view"), {}, [], "split")
            base.update({k: segment[k] for k in ("yaw", "pitch", "fov", "roll") if segment.get(k) is not None})
            result[key] = _slice_keys(result[key], start, end, rebase=rebase,
                                      view=base, projection=segment.get("projection", "flat"))
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
        sphere["path"] = _slice_keys(sphere["path"], start, end, rebase=rebase,
                                     view=sphere.get("view"), projection=sphere.get("projection", "flat"))
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


def trim_local_source(segment: dict[str, Any], source_in: float, source_out: float, *, precision: int = 3) -> None:
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
    segment["in"], segment["out"] = round(source_in, precision), round(source_out, precision)


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
