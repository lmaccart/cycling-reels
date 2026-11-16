"""Flagged clips: footage ranges the user (or Claude) marked to come back to.

Stored in flags.json at the project root, shared by the editor app, ./reels and
Claude. A flag is a library asset plus an in/out range and a short note; it
never changes the footage, its tags or any plan.
"""

from __future__ import annotations

import secrets
from pathlib import Path

from .config import Project
from .library import load_asset, resolve_id
from .util import ReelsError, fmt_ts, now_iso, read_json, write_json

FILE = "flags.json"
MAX_NOTE = 200


def path(project: Project) -> Path:
    return project.root / FILE


def load(project: Project) -> list[dict]:
    p = path(project)
    if not p.is_file():
        return []
    data = read_json(p)
    flags = data.get("flags") if isinstance(data, dict) else None
    return [f for f in flags or [] if isinstance(f, dict) and f.get("id") and f.get("asset")]


def _save(project: Project, flags: list[dict]) -> None:
    write_json(path(project), {"version": 1, "flags": flags})


def add(project: Project, asset: str, start: float | None = None, end: float | None = None, note: str = "") -> dict:
    """Flags [start, end] of an asset (the whole clip when no range is given)."""
    a = load_asset(project, resolve_id(project, str(asset)))
    if a.get("deleted"):
        raise ReelsError(f"{a['id']}: its footage was deleted")
    flag: dict = {"id": "f-" + secrets.token_hex(3), "asset": a["id"]}
    if a.get("kind") == "video":
        duration = float((a.get("probe") or {}).get("duration") or 0)
        s = 0.0 if start is None else float(start)
        e = duration if end is None else float(end)
        if duration > 0:
            s, e = max(0.0, min(s, duration)), max(0.0, min(e, duration))
        if e - s < 0.1:
            raise ReelsError("a flag needs out at least 0.1 s after in")
        flag["in"], flag["out"] = round(s, 2), round(e, 2)
    note = " ".join(str(note or "").split())
    if len(note) > MAX_NOTE:
        raise ReelsError(f"keep the note under {MAX_NOTE} characters")
    if note:
        flag["note"] = note
    flag["createdAt"] = now_iso()
    flags = load(project)
    flags.append(flag)
    _save(project, flags)
    return flag


def update(project: Project, flag_id: str, note: str | None = None) -> dict:
    flags = load(project)
    for f in flags:
        if f["id"] == flag_id:
            if note is not None:
                text = " ".join(str(note).split())
                if len(text) > MAX_NOTE:
                    raise ReelsError(f"keep the note under {MAX_NOTE} characters")
                if text:
                    f["note"] = text
                else:
                    f.pop("note", None)
            _save(project, flags)
            return f
    raise ReelsError(f"no flag {flag_id}")


def unflag(project: Project, flag_id: str) -> dict:
    flags = load(project)
    for i, f in enumerate(flags):
        if f["id"] == flag_id:
            flags.pop(i)
            _save(project, flags)
            return f
    raise ReelsError(f"no flag {flag_id}")


def describe(f: dict) -> str:
    span = f" {fmt_ts(f['in'])}-{fmt_ts(f['out'])}" if "in" in f else ""
    note = f"  {f['note']}" if f.get("note") else ""
    return f"{f['id']}  {f['asset']}{span}{note}"
