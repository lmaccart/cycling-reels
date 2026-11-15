"""Whole-chapter pruning of library/media/, in three steps: --mark records a judgment on the asset's
sidecar; --dry-run lists what's eligible and writes that list as a round; --apply deletes one round,
which the user has approved in the conversation, and only while free disk is under the threshold.

Only the media file goes: tags, sheets, transcripts and sounds stay, and the sidecar gets `deleted`.
The one call that removes anything is in _delete_one, after the live re-check."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timedelta
from typing import Any

from .config import Project
from .library import all_assets, asset_file, load_asset, save_asset, update_asset
from .util import ReelsError, log, now_iso, read_json, write_json

RECENT_DAYS = 14
# The only protection a marked chapter can wait out; every other one refuses the mark.
WAITABLE = "recent"
GB = 1_000_000_000  # decimal, as Finder shows disk space


def _is_recent(created: str | None, now: datetime) -> bool:
    """An unknown or unreadable date counts as recent, so it can't be offered by accident."""
    if not created:
        return True
    try:
        when = datetime.fromisoformat(created)
    except (TypeError, ValueError):
        return True
    return _wall_clock(now) - _wall_clock(when) <= timedelta(days=RECENT_DAYS)


def _wall_clock(when: datetime) -> datetime:
    """Local wall-clock time: camera dates have no timezone, so a day stays a day across a clock change."""
    return datetime.fromtimestamp(when.timestamp()) if when.tzinfo else when


def _assets_named_in(node: Any) -> set[str]:
    """Every "asset" value in a plan, at any depth: segments, cutaways and anything added later."""
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "asset" and isinstance(value, str):
                found.add(value)
            else:
                found |= _assets_named_in(value)
    elif isinstance(node, list):
        for value in node:
            found |= _assets_named_in(value)
    return found


def _in_a_plan(project: Project, asset: dict) -> bool:
    """A written plan may not be rendered yet, so `used` can't see it. Resolve its references exactly
    as the renderer does; unreadable plans and unresolved references stop pruning."""
    aid = asset["id"]
    for plan in project.plans_dir.glob("*.json"):
        try:
            named = _assets_named_in(read_json(plan))
        except (OSError, json.JSONDecodeError) as e:
            raise ReelsError(f"can't check {plan.name} for footage in use ({type(e).__name__}); fix it before pruning") from e
        for name in named:
            try:
                resolved = load_asset(project, name)["id"]
            except (ReelsError, OSError, ValueError, KeyError, TypeError) as e:
                raise ReelsError(f"can't resolve {name!r} in {plan.name}; fix it before pruning: {e}") from e
            if resolved == aid:
                return True
    return False


def protections(project: Project, asset: dict, now: datetime | None = None) -> list[str]:
    """Every reason this asset can't be deleted; empty means nothing protects it."""
    tags = asset.get("tags") or {}
    moments = tags.get("moments") or []
    reasons = []
    if asset.get("used"):
        reasons.append("used")
    if _in_a_plan(project, asset):
        reasons.append("planned")
    if any(m.get("thread") for m in moments):
        reasons.append("thread")
    if tags.get("sounds"):
        reasons.append("sound")
    if any(m.get("speaker") == "me" or m.get("role") == "voice" for m in moments):
        reasons.append("voice")
    if _is_recent(asset.get("created"), now or datetime.now()):
        reasons.append("recent")
    if asset.get("kind") != "video":
        reasons.append("not-video-chapter")
    if asset.get("deleted"):
        reasons.append("deleted")
    return reasons


def mark(project: Project, token: str, reason: str, now: datetime | None = None) -> dict:
    """Records the candidate verdict; a chapter protected only by its age still keeps the judgment."""
    asset = load_asset(project, token)
    if not reason or not reason.strip():
        raise ReelsError("--mark needs --reason: what makes the whole chapter empty")
    found = protections(project, asset, now)
    blocking = [p for p in found if p != WAITABLE]
    if blocking:
        raise ReelsError(f"{asset['id']} can't be marked, protected by: {', '.join(found)}")
    review = {"verdict": "candidate", "reason": reason.strip(), "at": now_iso()}
    if found:
        review["waiting"] = WAITABLE
    asset["pruneReview"] = review
    save_asset(project, asset)
    return review


def unmark(project: Project, token: str) -> bool:
    """Removes the review. Returns whether there was one."""
    asset = load_asset(project, token)
    had = asset.pop("pruneReview", None) is not None
    if had:
        save_asset(project, asset)
    return had


def free_gb(project: Project) -> float:
    return shutil.disk_usage(project.media_dir).free / GB


def min_free_gb(project: Project) -> float:
    value = project.cfg("prune", "minFreeGB")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ReelsError(f"config prune.minFreeGB must be a positive number of GB (got {value!r})")
    return float(value)


def _is_candidate(asset: dict) -> bool:
    return (asset.get("pruneReview") or {}).get("verdict") == "candidate"


def _media_problem(project: Project, asset: dict) -> str | None:
    """Why this asset's media path isn't a regular file strictly inside library/media/, if it isn't."""
    if not asset.get("media"):
        return "no-media-path"
    raw = project.abs(asset["media"])
    if raw.is_symlink():
        return "media-is-a-symlink"
    path = raw.resolve()
    if project.media_dir.resolve() not in path.parents:
        return "media-outside-library"
    return None if path.is_file() else "media-missing"


def blockers(project: Project, asset: dict, now: datetime | None = None) -> list[str]:
    """Every reason not to delete this asset's media right now; empty means it can go."""
    found = protections(project, asset, now)
    if not _is_candidate(asset):
        found.append("not-marked")
    problem = _media_problem(project, asset)
    if problem:
        found.append(problem)
    return found


def survey(project: Project, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Marked chapters, checked live: (eligible oldest first, protected with why)."""
    eligible, protected = [], []
    for asset in all_assets(project):
        if not _is_candidate(asset) or asset.get("deleted"):
            continue
        row: dict[str, Any] = {"id": asset["id"], "name": asset.get("originalName"), "media": asset.get("media"), "reason": asset["pruneReview"].get("reason"), "created": asset.get("created")}
        found = blockers(project, asset, now)
        if found:
            protected.append({**row, "protections": found})
        else:
            identity = _media_identity(project, asset)
            eligible.append({**row, "bytes": identity["bytes"], "identity": identity})
    # Being eligible means `created` parsed: an unreadable date counts as recent.
    eligible.sort(key=lambda r: (_wall_clock(datetime.fromisoformat(r["created"])), r["id"]))
    return eligible, protected


def _media_identity(project: Project, asset: dict) -> dict:
    """Bind approval to the resolved file, including replacement and in-place edit detection."""
    path = project.abs(asset["media"]).resolve()
    stat = path.stat()
    return {"path": str(path), "device": stat.st_dev, "inode": stat.st_ino, "bytes": stat.st_size,
            "mtimeNs": stat.st_mtime_ns, "ctimeNs": stat.st_ctime_ns}


def _write_round(project: Project, listed: list[dict], free: float, threshold: float) -> str:
    folder = project.out_dir / "prune-rounds"
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    path, n = folder / f"{stamp}.json", 1
    while path.exists():
        n += 1
        path = folder / f"{stamp}-{n}.json"
    write_json(path, {"version": 2, "createdAt": now_iso(), "freeGB": round(free, 1), "minFreeGB": threshold, "triggered": free < threshold,
                      "totalBytes": sum(r["bytes"] for r in listed),
                       "assets": [{k: r[k] for k in ("id", "media", "bytes", "reason", "identity")} for r in listed]})  # fmt: skip
    return project.rel(path)


def dry_run(project: Project, limit: int | None = None, now: datetime | None = None) -> dict:
    """Lists the round without deleting anything. The round file is the only thing it writes."""
    if limit is not None and limit < 1:
        raise ReelsError("--limit must be at least 1")
    eligible, protected = survey(project, now)
    listed = eligible[:limit] if limit else eligible
    free, threshold = free_gb(project), min_free_gb(project)
    triggered = free < threshold
    log(f"free disk: {free:.1f} GB · threshold: {threshold:g} GB (prune.minFreeGB) · pruning "
        + ("IS triggered: free disk is under the threshold" if triggered else "is NOT triggered: free disk is at or above the threshold, so --apply would refuse"))  # fmt: skip
    log(f"\neligible, oldest first: {len(listed)}" + (f" of {len(eligible)} (--limit {limit})" if len(listed) < len(eligible) else ""))
    running = 0
    for r in listed:
        running += r["bytes"]
        log(f"  {r['id']} ({r['name']})  {r['bytes'] / GB:.2f} GB  running total {running / GB:.2f} GB\n      {r['reason']}")
    log(f"\nmarked but protected: {len(protected)}")
    for r in protected:
        log(f"  {r['id']} ({r['name']})  protected by: {', '.join(r['protections'])}\n      {r['reason']}")
    log("")
    round_path = _write_round(project, listed, free, threshold) if listed else None
    if round_path:
        log(f"round written: {round_path}\nnothing was deleted. Deleting needs the user's approval of this exact list, then: ./reels prune --apply --round {round_path} --yes")
    else:
        log("nothing eligible, so no round was written. Nothing was deleted.")
    return {"eligible": listed, "protected": protected, "freeGB": free, "minFreeGB": threshold, "triggered": triggered, "round": round_path}


def _load_round(project: Project, round_path: str) -> tuple[Any, dict]:
    path = project.abs(round_path).resolve()
    if (project.out_dir / "prune-rounds").resolve() not in path.parents:
        raise ReelsError(f"{round_path} isn't in out/prune-rounds/: only a list made by --dry-run can be applied")
    if not path.is_file():
        raise ReelsError(f"round not found: {round_path}")
    try:
        doc = read_json(path)
    except (OSError, json.JSONDecodeError) as e:
        raise ReelsError(f"{path.name} can't be read as a round ({type(e).__name__})") from e
    if not isinstance(doc, dict) or not isinstance(doc.get("assets"), list):
        raise ReelsError(f"{path.name} isn't a round: it has no asset list")
    if doc.get("version") != 2:
        raise ReelsError(f"{path.name} has no supported file-identity snapshot; run --dry-run and approve a new round")
    if doc.get("appliedAt"):
        raise ReelsError(f"{path.name} was already applied at {doc['appliedAt']}; run --dry-run for a new list")
    return path, doc


def _delete_one(project: Project, entry: Any, known: set[str], now: datetime | None) -> dict:
    """Re-checks one listed asset as it is on disk now, then removes its media file."""
    project.assert_media_writable()
    aid = entry.get("id") if isinstance(entry, dict) else None
    if not isinstance(aid, str) or aid not in known:
        return {"status": "skipped", "why": "unknown-asset"}
    asset = read_json(asset_file(project, aid))
    found = blockers(project, asset, now)
    if found:
        return {"status": "skipped", "why": ", ".join(found)}
    if asset.get("id") != aid or asset.get("media") != entry.get("media"):
        return {"status": "skipped", "why": "approved-media-changed"}
    if _media_identity(project, asset) != entry.get("identity"):
        return {"status": "skipped", "why": "approved-file-changed"}
    # Use the identity just checked, not another resolution of a possibly redirected media path.
    path = project.abs(entry["identity"]["path"])
    size = entry["identity"]["bytes"]
    try:
        path.unlink()
    except OSError as e:
        return {"status": "failed", "why": f"{type(e).__name__}: couldn't remove the media file"}
    update_asset(project, asset, deleted={"at": now_iso(), "reason": asset["pruneReview"].get("reason") or ""})
    return {"status": "deleted", "bytes": size}


def apply_round(project: Project, round_path: str | None, *, yes: bool = False, now: datetime | None = None) -> list[dict]:
    """Deletes the media of the assets one round lists, each re-checked first. Free disk gates the
    start of a round: once it starts, the whole list the user approved is carried out."""
    project.assert_media_writable()
    if not round_path:
        raise ReelsError("--apply needs --round <file> from ./reels prune --dry-run")
    if not yes:
        raise ReelsError("--apply needs --yes, and the user's approval of this exact list in the conversation")
    path, doc = _load_round(project, round_path)
    free, threshold = free_gb(project), min_free_gb(project)
    if free >= threshold:
        raise ReelsError(f"refusing to delete anything: {free:.1f} GB free is not under the {threshold:g} GB threshold (prune.minFreeGB), so there's nothing to do")
    known = {p.stem for p in project.assets_dir.glob("*.json")}
    doc["appliedAt"] = now_iso()
    results = []
    for entry in doc["assets"]:
        aid = entry.get("id") if isinstance(entry, dict) else None
        result = _delete_one(project, entry, known, now)
        results.append({"id": aid, **result})
        if isinstance(entry, dict):
            entry["result"] = result
        write_json(path, doc)  # after every asset, so an interrupted round still says what it did
        log(f"  {aid}: {result['status']}" + (f" ({result['why']})" if result.get("why") else f", {result['bytes'] / GB:.2f} GB"))
    freed = sum(r.get("bytes", 0) for r in results)
    log(f"deleted {sum(r['status'] == 'deleted' for r in results)} of {len(results)}, freed {freed / GB:.2f} GB; results saved in {project.rel(path)}")
    return results


def cmd_prune(project: Project, args) -> int:
    if args.mark:
        review = mark(project, args.mark, args.reason)
        log(f"marked {args.mark}" + (" (waiting: recent; can't be offered until it ages out)" if review.get("waiting") else ""))
    elif args.unmark:
        log(f"unmarked {args.unmark}" if unmark(project, args.unmark) else f"{args.unmark} wasn't marked")
    elif args.dry_run:
        dry_run(project, args.limit)
    else:
        if args.limit is not None:
            raise ReelsError("--limit only works with --dry-run: a round is fixed when it's listed")
        apply_round(project, args.round, yes=args.yes)
    return 0


def add_commands(sub) -> None:
    s = sub.add_parser("prune", help="mark whole video chapters as empty, list what could go, or delete a round the user approved")
    action = s.add_mutually_exclusive_group(required=True)
    action.add_argument("--mark", metavar="ASSET")
    action.add_argument("--unmark", metavar="ASSET")
    action.add_argument("--dry-run", action="store_true", help="list eligible candidates oldest first and write them as a round; deletes nothing")
    action.add_argument("--apply", action="store_true", help="delete the media of one round: needs --round, --yes and free disk under prune.minFreeGB")
    s.add_argument("--reason", help="required with --mark: why the whole chapter is empty")
    s.add_argument("--limit", type=int, help="with --dry-run: list at most this many, oldest first")
    s.add_argument("--round", metavar="FILE", help="with --apply: a round file from out/prune-rounds/")
    s.add_argument("--yes", action="store_true", help="with --apply: the user approved this exact list in the conversation")
    s.set_defaults(fn=cmd_prune)
