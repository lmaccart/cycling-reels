"""Serial, resumable 360 cache warming; originals are read-only inputs."""
from __future__ import annotations

import hashlib
import math
import shutil
from pathlib import Path
from typing import Callable

from .config import Project
from .library import all_assets, load_asset
from .proxy import SPHERE_PROXY_MAX, proxy_reservation, source_cache_key, sphere_cache_key
from .proxy_cache import MIN_FREE_BYTES, ProxyDiskFull
from .util import ReelsError, log, read_json, warn, write_json


def _queue_path(project: Project, asset_id: str) -> Path:
    name = hashlib.sha256(asset_id.encode()).hexdigest()
    return project.root / ".cache/editor/prewarm-360" / f"{name}.json"


def queue_sources(project: Project, assets: list[dict]) -> list[str]:
    """Persist only newly ingested spherical videos, before starting expensive work."""
    ids = []
    for asset in assets:
        if asset.get("kind") == "video" and isinstance((asset.get("probe") or {}).get("sphere"), dict):
            write_json(_queue_path(project, asset["id"]), {"assetId": asset["id"]})
            ids.append(asset["id"])
    return ids


def _cache_size(root: Path) -> int:
    return sum(p.stat().st_size for p in root.rglob("*") if p.is_file() and not p.is_symlink()) if root.exists() else 0


def prewarm(project: Project, ids=(), *, all_clips: bool = False, source_only: bool = False,
            free_disk: Callable[[Path], int] | None = None) -> dict:
    """Use the editor's exact cache keys/workers, one job at a time.

    No ids means resume the ingest queue. Explicit ids/--all also warm medium CORI
    corrections and the whole clip's aim media in <=180 s ranges. A reservation
    is checked before each encode; no originals or plans are changed.
    """
    from .editor.worker import _stat_identity, prepare_source_proxy, prepare_sphere_proxy

    if all_clips and ids:
        raise ReelsError("choose --all or asset ids, not both")
    if all_clips:
        assets = [a for a in all_assets(project) if a.get("kind") == "video"
                  and isinstance((a.get("probe") or {}).get("sphere"), dict)]
    else:
        tokens = list(ids)
        if not tokens:
            directory = project.root / ".cache/editor/prewarm-360"
            tokens = [read_json(p)["assetId"] for p in sorted(directory.glob("*.json"))]
        assets = [load_asset(project, token) for token in dict.fromkeys(tokens)]
    state = project.root / ".cache/editor/live"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    preview = state / "preview"
    free_disk = free_disk or (lambda path: shutil.disk_usage(path).free)
    report = {"writtenBytes": 0, "prepared": 0, "reused": 0, "failed": 0, "stopped": False}
    for asset in assets:
        probe = asset.get("probe") or {}
        if asset.get("kind") != "video" or not isinstance(probe.get("sphere"), dict):
            raise ReelsError(f"{asset['id']} is not a 360 video")
        source = project.abs(asset["media"])
        if not source.is_file() or source.is_symlink() or asset.get("deleted"):
            warn(f"{asset['id']}: original offline; skipped")
            continue
        duration = float(probe.get("duration") or 0)
        if not math.isfinite(duration) or duration <= 0:
            warn(f"{asset['id']}: no duration; skipped")
            continue
        identity = _stat_identity(source)
        common = {"sourcePath": str(source), "sourceIdentity": identity, "assetId": asset["id"],
                  "sourceKind": "video", "probe": probe, "mediaRoot": str(project.media_dir),
                  "workspaceRoot": str(state), "freeDisk": free_disk}
        source_root = preview / "sources" / source_cache_key(asset, identity, {})
        sphere_root = preview / "spheres" / sphere_cache_key(asset["id"], identity, probe)
        jobs = [(prepare_source_proxy, {**common, "revisionDir": str(source_root), "view": {},
                                       "angles": (asset.get("tags") or {}).get("angles") or {}}, duration, "front/back")]
        if not source_only:
            for chunk in range(math.ceil(duration / SPHERE_PROXY_MAX)):
                start = chunk * SPHERE_PROXY_MAX
                end = min(duration, start + SPHERE_PROXY_MAX)
                jobs.append((prepare_sphere_proxy, {**common, "revisionDir": str(sphere_root),
                             "start": start, "end": end, "stabilize": "medium"}, end - start,
                             f"aim {start:g}–{end:g}s"))
        complete = True
        for worker, job, seconds, label in jobs:
            # Reserve the full output even on a probable hit: stale/corrupt entries
            # may need rebuilding. Never start work based on an optimistic hit.
            reserve = proxy_reservation(seconds)
            if free_disk(state) - reserve < MIN_FREE_BYTES:
                log(f"stopped before {asset['id']} {label}: keeping 50 GB free (needs {reserve / 1e9:.2f} GB headroom)")
                report["stopped"] = True
                complete = False
                break
            root = Path(job["revisionDir"])
            before = _cache_size(root)
            try:
                result = worker(job)
                if result.get("status") != "ready":
                    raise ReelsError(result.get("reason") or "proxy unavailable")
            except ProxyDiskFull as exc:
                log(f"stopped before {asset['id']} {label}: {exc}")
                report["stopped"] = True
                complete = False
                break
            except (ReelsError, OSError) as exc:
                warn(f"{asset['id']} {label}: {exc}")
                report["failed"] += 1
                complete = False
                break
            written = max(0, _cache_size(root) - before)
            report["writtenBytes"] += written
            report["reused" if result.get("reused") else "prepared"] += 1
            log(f"{asset['id']} {label}: {'cached' if result.get('reused') else 'ready'}, {written:,} bytes written")
        if complete:
            _queue_path(project, asset["id"]).unlink(missing_ok=True)
        if report["stopped"]:
            break
    log(f"360 prewarm: {report['writtenBytes']:,} bytes written ({report['writtenBytes'] / 1e9:.3f} GB); "
        f"{report['prepared']} prepared, {report['reused']} cached, {report['failed']} failed")
    return report


def run(project: Project, args) -> int:
    result = prewarm(project, args.ids, all_clips=args.all)
    return 1 if result["failed"] or result["stopped"] else 0


def add_commands(sub) -> None:
    parser = sub.add_parser("prewarm-360", help="warm front/back and whole-clip aim proxies; keep 50 GB free")
    parser.add_argument("--all", action="store_true", help="backfill all online 360 videos")
    parser.add_argument("ids", nargs="*", help="asset ids (default: resume pending ingest proxies)")
    parser.set_defaults(fn=run)
