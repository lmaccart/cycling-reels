"""Versioned, frame-exact footage placement shared by editor and renderer."""
from __future__ import annotations

import hashlib
import re
from typing import Any
from uuid import UUID

CAPABILITIES = {
    "version": 1, "overwrite": True, "crossTrack": True, "libraryInsert": True,
    "multiMove": True, "cutawayPromotion": True, "insert": False, "idAlgorithm": "sha256-v1",
}


def positioned(plan: dict) -> bool:
    return (isinstance(plan.get("timeline"), dict)
            and type(plan["timeline"].get("version")) is int and plan["timeline"]["version"] == 1)


def video_track(value: Any) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"V[1-9][0-9]*", value):
        raise ValueError("destination must be a canonical video track V1, V2, …")
    return int(value[1:])


def integer(value: Any, name: str, minimum=0) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def edit_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("editId must be a UUID string")
    parsed = UUID(value)
    if str(parsed) != value.lower():
        raise ValueError("editId must be a canonical UUID string")
    return str(parsed)


def deterministic_id(prefix: str, edit: str, origin: str, start: int, end: int) -> str:
    if not isinstance(origin, str) or not origin or "\0" in origin:
        raise ValueError("node IDs must be nonempty and cannot contain NUL")
    text = f"reels-overwrite-v1\0{edit_id(edit)}\0{origin}\0{int(start)}\0{int(end)}"
    return prefix + hashlib.sha256(text.encode("utf-8")).hexdigest()


def duration(plan: dict, segs: list, fps: int) -> int:
    if not positioned(plan):
        return segs[-1].end if segs else 0
    header = plan["timeline"]
    if integer(header.get("fps"), "timeline.fps", 1) != fps:
        raise ValueError("timeline FPS does not match render FPS; requantize placements first")
    end = integer(header.get("durationFrames"), "timeline.durationFrames", 1)
    if any(segment.end > end for segment in segs):
        raise ValueError("timeline ends before a placed clip")
    return end


def place(plan: dict, segs: list, fps: int) -> None:
    """Validate real placement without changing source or array identity."""
    tracks: dict[str, list] = {}
    for segment in segs:
        record = segment.spec.get("timeline")
        if not isinstance(record, dict):
            raise ValueError("every positioned clip needs a timeline placement")
        video_track(record.get("track"))
        segment.start = integer(record.get("startFrame"), "startFrame")
        frames = integer(record.get("durationFrames"), "durationFrames", 1)
        if abs(frames - segment.frames) > 1:
            raise ValueError("placed duration differs from source range/speed")
        segment.frames = frames
        segment.trans = integer(record.get("transitionInFrames", 0), "transitionInFrames")
        if segment.trans > frames:
            raise ValueError("transition cannot exceed its clip")
        if getattr(segment, "disabled", False):
            continue  # a disabled clip is never drawn, so it can't collide with anything
        tracks.setdefault(record["track"], []).append(segment)
    for clips in tracks.values():
        clips.sort(key=lambda item: (item.start, item.index))
        for left, right in zip(clips, clips[1:]):
            overlap = left.end - right.start
            if overlap > 0 and (overlap != right.trans or left.start > right.start):
                raise ValueError("clips on the same track overlap without a preserved transition")
    duration(plan, [segment for segment in segs if not getattr(segment, "disabled", False)], fps)


def requantize(plan: dict, fps: int) -> None:
    if not positioned(plan):
        return
    old = integer(plan["timeline"].get("fps"), "timeline.fps", 1)
    if old == fps:
        return
    for segment in plan.get("segments") or []:
        item = segment["timeline"]
        start = round(item["startFrame"] * fps / old)
        end = round((item["startFrame"] + item["durationFrames"]) * fps / old)
        item.update(startFrame=start, durationFrames=max(1, end - start),
                    transitionInFrames=round(item.get("transitionInFrames", 0) * fps / old))
    plan["timeline"].update(fps=fps, durationFrames=max(
        1, round(plan["timeline"]["durationFrames"] * fps / old),
        max((item["timeline"]["startFrame"] + item["timeline"]["durationFrames"]
             for item in plan.get("segments") or []), default=0),
    ))
