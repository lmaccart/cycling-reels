"""Live-mode helpers: plan listing/creation, footage import, plan watching, context.json.

Nothing here deletes, moves or edits an existing file in library/ or plans/.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ..media import IGNORE_EXT, PHOTO_EXT, VIDEO_EXT
from .jobs import atomic_json
from .protocol import ProtocolError

SUPPORTED = VIDEO_EXT | PHOTO_EXT
_STATUSES = {"draft", "rendered", "approved", "queued", "scheduled", "posted", "rejected"}


# ---- plans ---------------------------------------------------------------

def _hook_text(plan: dict) -> str:
    hook = plan.get("hook")
    if isinstance(hook, dict):
        return str(hook.get("text") or "").strip()
    if isinstance(hook, str):
        return hook.strip()
    return ""


def _duration(project, plan: dict) -> float | None:
    segments = plan.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    total, known = 0.0, False
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        try:
            if "in" in seg and "out" in seg:
                speed = float(seg.get("speed") or 1.0) or 1.0
                total += max(0.0, (float(seg["out"]) - float(seg["in"])) / speed)
                known = True
            elif "duration" in seg:
                total += float(seg["duration"])
                known = True
        except (TypeError, ValueError):
            continue
    return round(total, 2) if known else None


def _status(project, slug: str) -> str | None:
    post = project.out_dir / slug / "post.json"
    if post.is_file() and not post.is_symlink():
        try:
            value = json.loads(post.read_text(encoding="utf-8")).get("status")
        except (OSError, ValueError, AttributeError):
            return None
        return value if isinstance(value, str) and value in _STATUSES else (value if isinstance(value, str) else None)
    return "draft"


def _thumbnail(project, http, plan: dict, slug: str) -> str | None:
    candidates: list[Path] = []
    cover = project.out_dir / slug / "cover.jpg"
    candidates.append(cover)
    for seg in plan.get("segments") or []:
        if isinstance(seg, dict) and isinstance(seg.get("asset"), str):
            aid = seg["asset"]
            if "/" in aid or "\\" in aid or aid.startswith("."):
                break
            for ext in (".jpg", ".jpeg", ".png"):
                candidates.append(project.thumbs_dir / f"{aid}{ext}")
            asset_path = project.assets_dir / f"{aid}.json"
            try:
                sheets = json.loads(asset_path.read_text(encoding="utf-8")).get("sheets") or []
            except (OSError, ValueError, AttributeError):
                sheets = []
            for sheet in sheets[:1]:
                if isinstance(sheet, str) and ".." not in Path(sheet).parts and not Path(sheet).is_absolute():
                    candidates.append(project.root / sheet)
            break
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            try:
                stamp = path.stat().st_mtime_ns
                return http.register_file(f"thumbs/{slug}-{stamp}/{path.name}", path)
            except Exception:  # noqa: BLE001
                continue
    return None


def list_plans(project, http) -> dict:
    items = []
    for path in project.plans_dir.glob("*.json"):
        if path.is_symlink():
            continue
        rel = project.rel(path)
        try:
            modified = path.stat().st_mtime
            plan = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(plan, dict):
                raise ValueError("plan is not an object")
        except (OSError, ValueError):
            items.append({"path": rel, "name": path.stem, "slug": path.stem, "title": path.stem, "invalid": True,
                          "postDate": None, "format": None, "segmentCount": 0, "durationSeconds": None,
                          "status": None, "thumbnailURL": None, "modified": 0.0})
            continue
        slug = str(plan.get("slug") or path.stem)
        title = _hook_text(plan) or str(plan.get("title") or "") or slug
        segments = plan.get("segments")
        items.append({
            "path": rel, "name": path.stem, "slug": slug, "title": title,
            "postDate": plan.get("postDate") if isinstance(plan.get("postDate"), str) else None,
            "format": plan.get("format") if isinstance(plan.get("format"), str) else None,
            "segmentCount": len(segments) if isinstance(segments, list) else 0,
            "durationSeconds": _duration(project, plan),
            "status": _status(project, slug),
            "thumbnailURL": _thumbnail(project, http, plan, slug),
            "modified": modified,
        })
    items.sort(key=lambda item: (str(item.get("postDate") or ""), item.get("modified") or 0), reverse=True)
    return {"plans": items}


def plans_stamp(project) -> dict[str, tuple[int, int]]:
    stamp = {}
    for path in project.plans_dir.glob("*.json"):
        try:
            info = path.stat()
        except OSError:
            continue
        stamp[path.name] = (info.st_mtime_ns, info.st_size)
    return stamp


def kebab(text: str, limit: int = 48) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")
    return value[:limit].strip("-") or "untitled"


def today(project) -> str:
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(str(project.cfg("schedule", "timezone") or "UTC"))
        return datetime.now(zone).strftime("%Y-%m-%d")
    except Exception:  # noqa: BLE001
        return datetime.now().strftime("%Y-%m-%d")


def create_plan(project, params: dict) -> dict:
    title = str(params.get("title") or "")
    date = params.get("postDate") or today(project)
    if not isinstance(date, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise ProtocolError("invalid_date", "postDate must look like YYYY-MM-DD.")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise ProtocolError("invalid_date", "postDate is not a real date.") from exc
    project.plans_dir.mkdir(parents=True, exist_ok=True)
    base = f"{date}-{kebab(title)}"
    for n in range(1, 1000):
        stem = base if n == 1 else f"{base}-{n}"
        path = project.plans_dir / f"{stem}.json"
        plan = {"slug": stem, "postDate": date, "hook": {"text": ""}, "segments": []}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)  # never overwrites
        except FileExistsError:
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(plan, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
        return {"path": project.rel(path)}
    raise ProtocolError("plan_name_taken", "Too many plans share that name.")


# ---- footage import --------------------------------------------------------

def inbox_files(project) -> list[Path]:
    """The media files `reels ingest` would actually add (not README, sidecars or empty files)."""
    from ..ingest import _inbox_files
    from ..media import PHOTO_EXT, VIDEO_EXT
    wanted = VIDEO_EXT | PHOTO_EXT
    found = []
    for f in _inbox_files(project):
        try:
            if f.suffix.lower() in wanted and f.stat().st_size > 0:
                found.append(f)
        except OSError:
            continue
    return found


def plan_import(project, paths: list[Any]) -> tuple[list[Path], list[str]]:
    """Enumerate importable files. Hidden files, .LRV/.THM sidecars and unsupported types are skipped."""
    files: list[Path] = []
    skipped: list[str] = []
    library = project.library.resolve()

    def consider(path: Path, top_level: bool) -> None:
        name = path.name
        if name.startswith("."):
            return
        ext = path.suffix.lower()
        if ext in IGNORE_EXT:
            if top_level:
                skipped.append(f"{name}: sidecar file")
            return
        if ext not in SUPPORTED:
            skipped.append(f"{name}: unsupported file type")
            return
        try:
            if path.resolve().is_relative_to(library):
                skipped.append(f"{name}: already in the library")
                return
        except OSError:
            return
        files.append(path)

    for raw in paths:
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw).expanduser()
        if not path.is_absolute():
            skipped.append(f"{raw}: not an absolute path")
        elif path.is_dir():
            for current, dirs, names in os.walk(path, topdown=True, followlinks=False):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(names):
                    consider(Path(current) / name, False)
        elif path.is_file():
            consider(path, True)
        else:
            skipped.append(f"{raw}: not found")
    return files, skipped


def copy_into_inbox(project, files: list[Path], log: Callable[[str], None], check: Callable[[], None]) -> int:
    """Copy (never move) files into inbox/. Existing inbox files are never replaced."""
    inbox = project.inbox
    inbox.mkdir(parents=True, exist_ok=True)
    copied = 0
    for index, source in enumerate(files, 1):
        check()
        target = inbox / source.name
        n = 1
        while target.exists():
            n += 1
            target = inbox / f"{source.stem} ({n}){source.suffix}"
        partial = inbox / f".{target.name}.{uuid.uuid4().hex}.part"
        log(f"Copying {source.name} ({index}/{len(files)})")
        try:
            shutil.copy2(source, partial)
            os.link(partial, target)  # fails rather than replace anything that appeared meanwhile
            copied += 1
        except FileExistsError:
            log(f"Skipped {source.name}: a file with that name appeared in the inbox")
        finally:
            if partial.exists():
                partial.unlink()  # our own temporary copy inside inbox/
    return copied


# ---- context.json ------------------------------------------------------------

def describe_nodes(snapshot: dict, ids: list[str]) -> list[dict]:
    index: dict[str, dict] = {}
    for node in snapshot.get("nodes") or []:
        if isinstance(node, dict) and isinstance(node.get("id"), str):
            index[node["id"]] = node
    result = []
    for node_id in ids:
        node = index.get(node_id) or {}
        raw = node.get("raw") if isinstance(node.get("raw"), dict) else {}
        hint = raw.get("text") or raw.get("asset") or raw.get("name") or ""
        if isinstance(hint, list):
            hint = hint[0].get("text", "") if hint and isinstance(hint[0], dict) else ""
        summary = " ".join(x for x in (str(node.get("kind") or ""), str(hint)[:60]) if x)
        result.append({"id": node_id, "description": summary or "unknown"})
    return result


def write_context(workspace, plan: str | None, selection: list[dict], playhead_seconds: float) -> None:
    value = {
        "plan": plan, "selection": selection, "playheadSeconds": playhead_seconds,
        "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    atomic_json(Path(workspace.root) / "context.json", value)
