"""Organisational editor tracks (V1.., A1..). Never read by the renderer.

Tracks live in the private ``_editor`` metadata (``_editor.tracks`` and
``_editor.nodes[id].track``), which is kept in a sidecar and never written into
the plan, so ``./reels check`` and ``./reels render`` cannot be affected.
"""
from __future__ import annotations

from typing import Any

AUDIO_KINDS = {"music", "music_drop", "sfx", "generated_sfx", "resolved_sfx", "nat", "bleep",
               "bleep_window", "generated_bleep"}
SFX_KINDS = AUDIO_KINDS - {"music", "music_drop"}
VIDEO_CUTAWAY_KINDS = {"cutaway", "generated_cutaway"}
DEFAULT_TRACKS = (("V1", "video", 1), ("V2", "video", 2), ("V3", "video", 3),
                  ("A1", "audio", 1), ("A2", "audio", 2), ("A3", "audio", 3))
TRACK_OPS = {"add_track", "remove_track", "rename_track", "set_track_locked", "set_node_track"}
MAX_NAME = 40


def node_track_kind(node_kind: str) -> str:
    return "audio" if node_kind in AUDIO_KINDS else "video"


def default_track(node_kind: str) -> str:
    if node_kind == "segment":
        return "V1"
    if node_kind in VIDEO_CUTAWAY_KINDS:
        return "V2"
    if node_kind in ("music", "music_drop"):
        return "A2"
    if node_kind in SFX_KINDS:
        return "A3"
    return "V3"


def _user_tracks(editor: dict[str, Any]) -> list[dict[str, Any]]:
    raw = editor.get("tracks")
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item.get("kind") in ("video", "audio"):
            out.append(item)
    return out


def track_list(editor: dict[str, Any] | None) -> list[dict[str, Any]]:
    editor = editor or {}
    overrides = {t["id"]: t for t in _user_tracks(editor)}
    tracks = []
    for tid, kind, index in DEFAULT_TRACKS:
        o = overrides.pop(tid, {})
        tracks.append({"id": tid, "kind": kind, "name": o.get("name") or tid, "index": index,
                       "locked": bool(o.get("locked")), "userAdded": False})
    for tid, o in overrides.items():
        try:
            index = int(tid[1:])
        except ValueError:
            continue
        if tid[0] != ("V" if o["kind"] == "video" else "A"):
            continue
        tracks.append({"id": tid, "kind": o["kind"], "name": o.get("name") or tid, "index": index,
                       "locked": bool(o.get("locked")), "userAdded": True})
    tracks.sort(key=lambda t: (t["kind"] != "video", t["index"]))
    return tracks


def annotate_nodes(plan: dict[str, Any], nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Set ``track`` on each node: stored assignment if its track still exists, else the default."""
    editor = plan.get("_editor") or {}
    stored = editor.get("nodes") if isinstance(editor.get("nodes"), dict) else {}
    valid = {t["id"]: t for t in track_list(editor)}
    for node in nodes:
        want = (stored.get(node["id"]) or {}).get("track")
        track = valid.get(want)
        if track is None or track["kind"] != node_track_kind(node["kind"]):
            want = default_track(node["kind"])
        node["track"] = want
    return nodes


def _editor(plan: dict[str, Any]) -> dict[str, Any]:
    return plan.setdefault("_editor", {})


def _find(plan: dict[str, Any], tid: Any) -> dict[str, Any]:
    for track in track_list(plan.get("_editor")):
        if track["id"] == tid:
            return track
    raise ValueError(f"unknown track: {tid!r}")


def _set_track_record(plan: dict[str, Any], tid: str, **changes: Any) -> None:
    track = _find(plan, tid)
    records = _user_tracks(_editor(plan))
    record = next((r for r in records if r["id"] == tid), None)
    if record is None:
        record = {"id": tid, "kind": track["kind"]}
        records.append(record)
    record.update(changes)
    _editor(plan)["tracks"] = records


def _stored_track(plan: dict[str, Any], node_id: str) -> str | None:
    return ((plan.get("_editor") or {}).get("nodes") or {}).get(node_id, {}).get("track")


def effective_track(plan: dict[str, Any], node_id: str) -> str | None:
    entry = ((plan.get("_editor") or {}).get("nodes") or {}).get(node_id)
    if not entry:
        return None
    want = entry.get("track")
    track = next((t for t in track_list(plan.get("_editor")) if t["id"] == want), None)
    if track is None or track["kind"] != node_track_kind(entry.get("kind", "")):
        return default_track(entry.get("kind", ""))
    return want


def locked_target(plan: dict[str, Any], operation: dict[str, Any]) -> str | None:
    """A message when the operation edits an item that sits on a locked track, else None."""
    ids = []
    if isinstance(operation.get("id"), str):
        ids.append(operation["id"])
    if isinstance(operation.get("ids"), list):
        ids.extend(i for i in operation["ids"] if isinstance(i, str))
    if operation.get("op") == "insert_overlay" and isinstance(operation.get("parentId"), str):
        ids = [operation["parentId"]] if operation["parentId"] != "document" else []
    locked = {t["id"] for t in track_list(plan.get("_editor")) if t["locked"]}
    if not locked:
        return None
    for node_id in ids:
        tid = effective_track(plan, node_id)
        if tid in locked:
            return f"Track {tid} is locked. Unlock it to edit this item."
    return None


def apply_track_operation(plan: dict[str, Any], operation: dict[str, Any]) -> list[str]:
    """Apply one track op to ``plan['_editor']``. Returns affected node ids."""
    op = operation["op"]
    editor = _editor(plan)
    if op == "add_track":
        kind = operation.get("kind")
        if kind not in ("video", "audio"):
            raise ValueError("kind must be video or audio")
        used = [t["index"] for t in track_list(editor) if t["kind"] == kind]
        index = max(used) + 1
        tid = f"{'V' if kind == 'video' else 'A'}{index}"
        editor["tracks"] = _user_tracks(editor) + [{"id": tid, "kind": kind, "name": tid, "locked": False}]
        return []
    tid = operation.get("id") if op != "set_node_track" else operation.get("track")
    track = _find(plan, tid)
    if op == "remove_track":
        if not track["userAdded"]:
            raise ValueError(f"{tid} is a built-in track and cannot be removed")
        users = [n for n in (editor.get("nodes") or {})
                 if effective_track(plan, n) == tid]
        if users:
            raise ValueError(f"{tid} still has {len(users)} item(s); move them first")
        editor["tracks"] = [r for r in _user_tracks(editor) if r["id"] != tid]
        return []
    if op == "rename_track":
        name = str(operation.get("name") or "").strip()
        if not name or len(name) > MAX_NAME:
            raise ValueError(f"name must be 1-{MAX_NAME} characters")
        _set_track_record(plan, tid, name=name)
        return []
    if op == "set_track_locked":
        _set_track_record(plan, tid, locked=bool(operation.get("locked")))
        return []
    if op == "set_node_track":
        node_id = str(operation.get("id"))
        entry = (editor.get("nodes") or {}).get(node_id)
        if entry is None:
            raise KeyError(f"unknown node id: {node_id}")
        if node_track_kind(entry["kind"]) != track["kind"]:
            raise ValueError(f"{entry['kind']} items belong on {node_track_kind(entry['kind'])} tracks, not {tid}")
        source = effective_track(plan, node_id)
        for check in (source, tid):
            if _find(plan, check)["locked"]:
                raise ValueError(f"Track {check} is locked. Unlock it to move this item.")
        entry["track"] = tid
        return [node_id]
    raise ValueError(f"unknown track operation: {op}")
