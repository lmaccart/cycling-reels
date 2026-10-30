from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from .config import Project
from .images import parse_date, photo_info
from .library import find_by_hash, save_asset
from .media import IGNORE_EXT, PHOTO_EXT, VIDEO_EXT, probe_video, volume_stats
from .sheets import make_photo_sheet, make_video_sheets
from .speech import detect_speech
from .sphere import describe as describe_sphere
from .util import ReelsError, fmt_ts, log, now_iso, quick_hash, slugify, warn


def _inbox_files(project: Project) -> list[Path]:
    files = []
    for p in sorted(project.inbox.rglob("*")):
        if not p.is_file():
            continue
        parts = p.relative_to(project.inbox).parts
        if any(part.startswith((".", "_")) for part in parts):
            continue
        if p.name.endswith((".part", ".crdownload", ".download")):
            continue
        files.append(p)
    return files


def describe(asset: dict) -> str:
    probe = asset["probe"]
    sph = probe.get("sphere")
    if asset["kind"] == "photo":
        return f"{probe['width']}x{probe['height']}" + ("  360°" if sph else "")
    bits = [f"{fmt_ts(probe['duration'])}", describe_sphere(sph) if sph else f"{probe['width']}x{probe['height']}"]
    if probe.get("fps"):
        bits.append(f"{probe['fps']:g}fps")
    if probe.get("hdr"):
        bits.append("HDR")
    secs = (asset.get("audio") or {}).get("speechSeconds")
    if isinstance(secs, (int, float)) and secs >= 1.5:
        bits.append(f"speech ~{secs:.0f}s")
    return "  ".join(bits)


def ingest(project: Project, *, dry_run: bool = False, speech: bool = True) -> list[dict]:
    """Moves new files from inbox/ into the library and builds contact sheets."""
    project.ensure_dirs()
    ignored: dict[str, int] = {}
    new: list[dict] = []
    files = _inbox_files(project)
    if not files:
        log("inbox is empty — drop ride videos and photos into inbox/ first")
        return new

    for f in files:
        ext = f.suffix.lower()
        if ext in IGNORE_EXT:
            ignored[ext] = ignored.get(ext, 0) + 1
            continue
        if ext not in VIDEO_EXT | PHOTO_EXT:
            warn(f"skip {f.name}: unsupported file type")
            continue
        if f.stat().st_size == 0:
            warn(f"skip {f.name}: empty file")
            continue

        h = quick_hash(f)
        dup = find_by_hash(project, h)
        if dup:
            log(f"= {f.name} is already in the library as {dup}")
            if not dry_run:
                dest = project.inbox / "_duplicates" / f.name
                dest.parent.mkdir(exist_ok=True)
                shutil.move(str(f), str(dest))
            continue

        kind = "video" if ext in VIDEO_EXT else "photo"
        try:
            probe = probe_video(f) if kind == "video" else photo_info(f)
        except (ReelsError, OSError) as e:
            warn(f"skip {f.name}: {e}")
            continue
        if kind == "video" and probe["duration"] < 0.5:
            warn(f"skip {f.name}: shorter than half a second")
            continue

        created = parse_date(probe.get("created")) or datetime.fromtimestamp(f.stat().st_mtime)
        asset_id = f"{created:%y%m%d}-{slugify(f.stem, 24)}-{h[:4]}"
        if dry_run:
            log(f"would ingest {f.name} -> {asset_id} ({kind})")
            continue

        dest = project.media_dir / f"{asset_id}{ext}"
        if project.cfg("ingest", "moveOriginals"):
            shutil.move(str(f), str(dest))
        else:
            shutil.copy2(str(f), str(dest))

        asset: dict = {
            "id": asset_id,
            "kind": kind,
            "media": project.rel(dest),
            "originalName": f.name,
            "hash": h,
            "created": created.isoformat(),
            "ingestedAt": now_iso(),
            "probe": probe,
            "status": "needs_tags",
            "tags": None,
            "transcript": None,
            "used": [],
        }
        try:
            if kind == "video":
                if probe["hasAudio"]:
                    audio: dict = {"volume": volume_stats(dest)}
                    if speech:
                        audio.update(detect_speech(dest, probe["duration"]) or {})
                    asset["audio"] = audio
                asset["sheets"] = [project.rel(p) for p in make_video_sheets(project, asset)]
            else:
                asset["sheets"] = [project.rel(make_photo_sheet(project, asset))]
        except ReelsError as e:
            warn(f"{asset_id}: contact sheet failed ({e}); you can retry with ./reels frames {asset_id}")
            asset["sheets"] = []
        save_asset(project, asset)
        new.append(asset)
        log(f"+ {asset_id}  {kind}  {describe(asset)}")
        if kind == "video" and probe["hasAudio"]:  # sound events (freehub, shifts, pass-bys…) -> library/sounds/
            from .sounds import on_ingest

            on_ingest(project, asset)

    # GPS from GoPro telemetry tracks (clips without one are skipped silently)
    videos = [a["id"] for a in new if a["kind"] == "video"]
    if videos:
        from .telemetry import run_telemetry

        run_telemetry(project, videos, quiet_none=True)

    for ext, n in sorted(ignored.items()):
        log(f"  (left {n} {ext} file{'s' if n > 1 else ''} in inbox — not ingestible)")
    if new:
        log(f"\n{len(new)} new asset(s). Next: view their contact sheets and tag them (./reels pending).")
    return new
