"""Disposable compiled-result cache; never journal authority or offline input."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

VERSION = 1


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def valid_result(result: Any) -> bool:
    if not isinstance(result, dict):
        return False
    fps, total, segments = result.get("fps"), result.get("total"), result.get("segs")
    if (type(fps) is not int or not 0 < fps <= 1000 or type(total) is not int or total < 0
            or not isinstance(segments, list)):
        return False
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("spec"), dict):
            return False
        if not isinstance(segment.get("asset"), dict):
            return False
        for name in ("index", "frames", "start", "trans"):
            value = segment.get(name)
            if type(value) is not int or value < 0:
                return False
        if segment["frames"] <= 0 or segment["start"] + segment["frames"] > total:
            return False
    props = result.get("props", {})
    header = props.get("timeline") if isinstance(props, dict) else None
    if header is not None:
        if (not isinstance(header, dict) or type(header.get("version")) is not int or header["version"] != 1
                or type(header.get("fps")) is not int or header["fps"] != fps
                or type(header.get("durationFrames")) is not int or header["durationFrames"] != total
                or total < 1):
            return False
        for segment in segments:
            placement = segment["spec"].get("timeline")
            if (not isinstance(placement, dict)
                    or not isinstance(placement.get("track"), str)
                    or re.fullmatch(r"V[1-9][0-9]*", placement["track"]) is None
                    or type(placement.get("startFrame")) is not int
                    or type(placement.get("durationFrames")) is not int
                    or placement["startFrame"] != segment["start"]
                    or placement["durationFrames"] != segment["frames"]):
                return False
    elif total != (segments[-1]["start"] + segments[-1]["frames"] if segments else 0):
        return False
    return (isinstance(result.get("props", {}), dict)
            and isinstance(result.get("sound", {}), dict)
            and isinstance(result.get("warnings", []), list))


def read_cache(path: Path, key: dict) -> dict | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(record, dict) or record.get("version") != VERSION
                or record.get("key") != key):
            return None
        result = record.get("compiled")
        if not valid_result(result) or record.get("checksum") != digest(result):
            return None
        return result
    except (OSError, ValueError, TypeError, OverflowError):
        return None


def cache_record(key: dict, compiled: dict) -> dict:
    return {"version": VERSION, "key": key, "compiled": compiled, "checksum": digest(compiled)}
