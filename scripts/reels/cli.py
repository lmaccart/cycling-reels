from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from PIL import Image

from . import __version__
from .config import Project
from .sheets import build_sheet
from .util import ReelsError, fmt_ts, log, now_iso, read_json


def _assets(project: Project):
    from .library import all_assets

    return all_assets(project)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_doctor(project: Project, args) -> int:
    from .doctor import doctor

    return 0 if doctor(project, fix=args.fix) else 1


def cmd_ingest(project: Project, args) -> int:
    from .ingest import ingest

    ingest(project, dry_run=args.dry_run, speech=not args.no_speech)
    return 0


def cmd_pending(project: Project, args) -> int:
    from .ingest import describe

    items = [a for a in _assets(project) if a.get("status") == "needs_tags"]
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "id": a["id"],
                        "kind": a["kind"],
                        "sheets": a.get("sheets"),
                        "duration": a["probe"].get("duration"),
                        "speechSeconds": (a.get("audio") or {}).get("speechSeconds"),
                        "transcript": a.get("transcript"),
                    }
                    for a in items
                ],
                indent=2,
            )
        )
        return 0
    if not items:
        log("nothing waiting to be tagged ✓")
        return 0
    log(f"{len(items)} asset(s) need tags — view each sheet, then ./reels tag <id> --json '{{…}}'")
    for a in items:
        log(f"  {a['id']}  {a['kind']}  {describe(a)}")
        for s in a.get("sheets") or []:
            log(f"      sheet: {s}")
        if a.get("transcript"):
            log(f"      transcript: {a['transcript']}")
    return 0


def cmd_frames(project: Project, args) -> int:
    from .media import extract_frame, probe_video
    from .sheets import build_sheet, make_video_sheets

    target = Path(args.target)
    if target.suffix.lower() in (".mp4", ".mov") and project.abs(target).exists():
        src = project.abs(target)
        probe = probe_video(src)
        times = [float(t) for t in args.at.split(",")] if args.at else [probe["duration"] * f for f in (0.1, 0.3, 0.5, 0.7, 0.9)]
        out = src.parent / f"frames-{'-'.join(f'{t:.1f}' for t in times[:4])}.jpg"
        with tempfile.TemporaryDirectory() as td:
            tiles = []
            for i, t in enumerate(times):
                fp = Path(td) / f"{i}.jpg"
                extract_frame(src, t, fp, width=720, probe=probe)
                with Image.open(fp) as im:
                    tiles.append((fmt_ts(t), im.convert("RGB")))
            build_sheet(tiles, out, src.name, cols=min(3, len(tiles)), tile_w=360 if probe["height"] > probe["width"] else 640)
        log(project.rel(out))
        return 0

    from .library import load_asset

    asset = load_asset(project, args.target)
    wants_360 = args.views or args.view or args.planet
    if wants_360:
        if not asset["probe"].get("sphere"):
            raise ReelsError("--views / --view / --planet only apply to 360 footage")
        return _frames_360(project, asset, args)
    if asset["kind"] != "video":
        log((asset.get("sheets") or ["(no sheet — re-run ./reels ingest)"])[0])
        return 0
    if args.at:
        times = [float(t) for t in args.at.split(",")]
        with tempfile.TemporaryDirectory() as td:
            tiles = []
            for i, t in enumerate(times):
                fp = Path(td) / f"{i}.jpg"
                extract_frame(project.abs(asset["media"]), t, fp, width=960, probe=asset["probe"])
                with Image.open(fp) as im:
                    tiles.append((fmt_ts(t), im.convert("RGB")))
            out = project.sheets_dir / f"{asset['id']}-at-{'-'.join(f'{t:.1f}' for t in times[:4])}.jpg"
            portrait = asset["probe"]["height"] > asset["probe"]["width"]
            build_sheet(
                tiles,
                out,
                asset["id"],
                "yellow lines = default 9:16 crop" if not portrait else "",
                cols=min(len(tiles), 3 if portrait else 2),
                tile_w=360 if portrait else 760,
                guide_aspect=None if portrait else 9 / 16,
            )
        log(project.rel(out))
        return 0
    start = args.start or 0.0
    end = args.end
    suffix = f"-z{start:g}-{end:g}" if end is not None else f"-z{start:g}"
    for p in make_video_sheets(project, asset, start=start, end=end, count=args.count, suffix=suffix):
        log(project.rel(p))
    return 0


def parse_view_arg(text: str, angles: dict) -> dict:
    """--view rider | --view 180,-10,80 (yaw, pitch, fov, roll)."""
    from .sphere import parse_view

    raw: Any = text.strip()
    if re.fullmatch(r"-?\d+(?:\.\d+)?(?:\s*,\s*-?\d+(?:\.\d+)?){0,3}", raw):
        raw = dict(zip(("yaw", "pitch", "fov", "roll"), (float(x) for x in raw.split(","))))
    errors: list[str] = []
    view = parse_view(raw, angles, errors, "--view")
    if errors:
        raise ReelsError("; ".join(errors))
    return view


def _frames_360(project: Project, asset: dict, args) -> int:
    from .sphere import extract_view, photo_view

    probe = asset["probe"]
    src = project.abs(asset["media"])
    is_video = asset["kind"] == "video"
    if args.at:
        times = [float(t) for t in args.at.split(",")]
    else:
        times = [round(probe["duration"] * f, 2) for f in (0.2, 0.5, 0.8)] if is_video else [0.0]
    angles = (asset.get("tags") or {}).get("angles") or {}

    def render(view: dict, t: float, fp: Path, w: int, h: int, projection: str) -> Image.Image:
        if is_video:
            extract_view(src, t, fp, probe, view, w, h, projection)
        else:
            photo_view(src, fp, view, w, h, projection)
        with Image.open(fp) as im:
            return im.convert("RGB")

    tiles = []
    with tempfile.TemporaryDirectory() as td:
        if args.views:
            dirs = [("front, yaw 0", 0), ("right, yaw +90", 90), ("behind, yaw ±180", 180), ("left, yaw -90", -90)]
            for i, t in enumerate(times):
                for j, (label, yaw) in enumerate(dirs):
                    view = {"yaw": yaw, "pitch": -10.0, "fov": 90.0, "roll": 0.0}
                    im = render(view, t, Path(td) / f"v{i}-{j}.jpg", 480, 480, "flat")
                    tiles.append((f"{fmt_ts(t)}  {label}" if is_video else label, im))
            out = project.sheets_dir / f"{asset['id']}-views-{'-'.join(f'{t:.1f}' for t in times[:3])}.jpg"
            build_sheet(tiles, out, f"{asset['id']}  ·  four directions (pitch -10°, fov 90°)",
                        "pick yaw/pitch for the road, the rider etc. and save them as angles", cols=4, tile_w=380)  # fmt: skip
        else:
            projection = "tiny-planet" if args.planet else "flat"
            view = parse_view_arg(args.view or "front", angles)
            for i, t in enumerate(times):
                im = render(view, t, Path(td) / f"p{i}.jpg", 540, 960, projection)
                tiles.append((fmt_ts(t) if is_video else "photo", im))
            what = "tiny planet" if args.planet else f"yaw {view['yaw']:g}, pitch {view['pitch']:g}, fov {view['fov']:g}"
            out = project.sheets_dir / f"{asset['id']}-{'planet' if args.planet else 'view'}-{'-'.join(f'{t:.1f}' for t in times[:3])}.jpg"
            build_sheet(tiles, out, f"{asset['id']}  ·  9:16 reframe: {what}", cols=min(3, len(tiles)), tile_w=300)
    log(project.rel(out))
    return 0


def cmd_tag(project: Project, args) -> int:
    from .library import load_asset, save_asset
    from .tags import validate_tags

    asset = load_asset(project, args.asset)
    if args.json:
        try:
            raw = json.loads(args.json)
        except json.JSONDecodeError as e:
            raise ReelsError(f"--json isn't valid JSON: {e}") from e
    elif args.file:
        raw = read_json(args.file)
    else:
        raise ReelsError("pass the tags with --json '{...}' or --file tags.json")
    tags = validate_tags(asset, raw)
    asset["tags"] = tags
    asset["status"] = "skip" if tags.get("skip") else "tagged"
    asset["taggedAt"] = now_iso()
    save_asset(project, asset)
    if tags.get("skip"):
        log(f"skipped {asset['id']}" + (f" ({tags['reason']})" if tags.get("reason") else ""))
    else:
        n = len(tags.get("moments") or [])
        log(f"tagged {asset['id']}: {tags['type']} · quality {tags['quality']}" + (f" · {n} moment(s)" if asset["kind"] == "video" else ""))
    return 0


def cmd_skip(project: Project, args) -> int:
    args.json = json.dumps({"skip": True, "reason": args.reason or ""})
    args.file = None
    return cmd_tag(project, args)


def cmd_show(project: Project, args) -> int:
    from .ingest import describe
    from .library import load_asset
    from .sheets import audio_line

    a = load_asset(project, args.asset)
    if args.json:
        print(json.dumps(a, indent=2))
        return 0
    log(f"{a['id']}  ·  {a['kind']}  ·  {describe(a)}  ·  status: {a.get('status')}")
    log(f"  file: {a['media']}  (was {a.get('originalName')})")
    if a["kind"] == "video":
        log(f"  {audio_line(a)}")
    for s in a.get("sheets") or []:
        log(f"  sheet: {s}")
    t = a.get("tags") or {}
    if t and not t.get("skip"):
        log(f"  {t.get('type')} · {t.get('shot')} · quality {t.get('quality')} · people: {t.get('people')} · issues: {', '.join(t.get('issues') or []) or '-'}")
        log(f"  {t.get('summary')}")
        for name, v in (t.get("angles") or {}).items():
            log(f"    angle {name}: yaw {v['yaw']:g}  pitch {v['pitch']:g}  fov {v['fov']:g}" + (f"  roll {v['roll']:g}" if v.get("roll") else ""))
        for i, m in enumerate(t.get("moments") or []):
            from .library import moment_used

            used = " (used)" if moment_used(a, m) else ""
            where = f"view {m['view']}" if m.get("view") is not None else f"focusX {m['focusX']}"
            log(f"    moment {i}: {fmt_ts(m['in'])}-{fmt_ts(m['out'])}  score {m['score']}  {where}  {m['label']}{used}")
        for h in t.get("hooks") or []:
            log(f"    hook idea: {h}")
    elif t.get("skip"):
        log(f"  skipped: {t.get('reason') or '(no reason)'}")
    if a.get("transcript"):
        tr = read_json(project.abs(a["transcript"]))
        text = " ".join(s["text"] for s in tr.get("segments") or []) or " ".join(w["w"] for w in tr.get("words") or [])
        log(f"  transcript ({a['transcript']}): \"{text[:400]}{'…' if len(text) > 400 else ''}\"")
    if a["kind"] == "video" and a["probe"].get("hasAudio"):
        from .sounds import describe_event, load_cache

        snd = load_cache(project, a["id"])
        if snd is None:
            log(f"  sounds: not analysed yet — ./reels sounds {a['id']}")
        else:
            best = sorted(snd.get("events") or [], key=lambda e: -e["conf"])[:5]
            log(f"  sounds: {len(snd.get('events') or [])} candidate(s), wind in {snd.get('windShare', 0):.0%} of the clip"
                + (" — top:" if best else ""))  # fmt: skip
            for e in best:
                log(f"    {describe_event(e)}")
        for s in (t.get("sounds") or []) if not t.get("skip") else []:
            log(f"    sound tag: {fmt_ts(s['at'])} {s['for']:.1f}s  {s['kind']}  score {s['score']}  {s.get('note', '')}")
    if a.get("gpsSummary"):
        from .telemetry import summary_line

        log(f"  {summary_line(project, a['gpsSummary'])}" + (f"  ({a['telemetry']})" if a.get("telemetry") else ""))
    from .strava import show_lines

    for line in show_lines(project, a):
        log(line)
    for u in a.get("used") or []:
        span = f" {fmt_ts(u['in'])}-{fmt_ts(u['out'])}" if "in" in u else ""
        log(f"  used in {u['plan']}{span}")
    return 0


def cmd_telemetry(project: Project, args) -> int:
    from .library import load_asset
    from .telemetry import fast_stretches, print_at, print_range, run_telemetry, speed_in, unit_names

    if args.at is not None or args.start is not None or args.fast:
        if len(args.assets) != 1:
            raise ReelsError("--at / --start / --fast take exactly one asset id")
        asset = load_asset(project, args.assets[0])
        if args.at is not None:
            for t in str(args.at).split(","):
                print_at(project, asset, float(t))
        if args.start is not None:
            print_range(project, asset, args.start, args.end if args.end is not None else args.start + 5)
        if args.fast:
            u = unit_names(project)["speed"]
            rows = fast_stretches(project, asset, seconds=args.fast)
            if not rows:
                log(f"{asset['id']}: no gap-free {args.fast:g}s stretch with GPS")
            for a, b, avg in rows:
                log(f"  {fmt_ts(a)}-{fmt_ts(b)}  avg {speed_in(project, avg):.1f} {u}")
        return 0
    done = run_telemetry(project, args.assets or None, force=args.force)
    if not done:
        log("no new GoPro telemetry to read" + ("" if args.force else " (--force redoes it)"))
    return 0


def cmd_sounds(project: Project, args) -> int:
    from .library import load_asset
    from .sounds import analyse, describe_event, draw, has_cache, summary

    if args.pending:
        todo = [
            a
            for a in _assets(project)
            if a["kind"] == "video" and a["probe"].get("hasAudio") and a.get("status") != "skip"
            and (args.force or not has_cache(project, a))
        ]  # fmt: skip
        if not todo:
            log("every video with audio already has sound candidates ✓" + ("" if args.force else " (--force redoes them)"))
            return 0
        for i, a in enumerate(todo, 1):
            try:
                data = analyse(project, a, force=args.force)
            except ReelsError as e:
                print(f"error: {a['id']}: {e}", file=sys.stderr)
                continue
            log(f"[{i}/{len(todo)}] {a['id']}: {summary(data)}")
        log("\nNext: ./reels sounds <id> for the list and a labelled spectrogram to Read.")
        return 0
    if not args.asset:
        raise ReelsError("give an asset id, or --pending for every video without sound candidates")
    asset = load_asset(project, args.asset)
    data = analyse(project, asset, force=args.force)
    start = args.start or 0.0
    end = args.end
    events = [e for e in data.get("events") or [] if e["at"] + e["for"] > start and (end is None or e["at"] < end)]
    events = [e for e in events if e["conf"] >= args.min_conf and (not args.kind or e["kind"] == args.kind)]
    if args.json:
        print(json.dumps(events, indent=2))
        return 0
    log(f"{asset['id']}: {summary(data)}")
    shown = sorted(events, key=lambda e: -e["conf"])[: args.limit] if args.best else events[: args.limit]
    for e in shown:
        log(f"  {describe_event(e)}")
    if len(events) > len(shown):
        log(f"  … {len(events) - len(shown)} more (--limit, --best, --kind, --start/--end)")
    if not args.no_image:
        for p in draw(project, asset, data, start, end):
            log(project.rel(p))
    return 0


def cmd_transcribe(project: Project, args) -> int:
    from .transcribe import transcribe

    transcribe(project, args.assets or None, force=args.force, model_name=args.model)
    return 0


def _library_sounds(project: Project, assets: list[dict], args) -> int:
    from .library import moment_used

    rows = []
    for a in assets:
        t = a.get("tags") or {}
        if a.get("status") != "tagged":
            continue
        for i, s in enumerate(t.get("sounds") or []):
            rows.append({"asset": a["id"], "sound": i, **s, "used": moment_used(a, {"in": s["at"], "out": s["at"] + s["for"]}),
                         "created": a.get("created")})  # fmt: skip
    if args.kind:
        rows = [r for r in rows if r["kind"] == args.kind]
    if args.unused:
        rows = [r for r in rows if not r["used"]]
    if args.min_score:
        rows = [r for r in rows if r["score"] >= args.min_score]
    rows.sort(key=lambda r: (-r["score"], r.get("created") or ""))
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    if not rows:
        log("no tagged sounds" + (" match" if args.kind or args.unused or args.min_score else " yet — see ./reels sounds"))
    for r in rows[: args.limit]:
        span = f"{fmt_ts(r['at'])} ({r['for']:.1f}s)"
        log(f"  {r['score']}★ {r['kind']:<9} {r['asset']:<30} {span:<16} {r.get('note', '')}" + (" [used]" if r["used"] else ""))
    if len(rows) > args.limit:
        log(f"  … {len(rows) - args.limit} more (use --limit)")
    return 0


def cmd_library(project: Project, args) -> int:
    from .library import moment_used

    assets = _assets(project)
    if args.sounds:
        return _library_sounds(project, assets, args)
    status: dict[str, int] = {}
    for a in assets:
        status[a.get("status", "?")] = status.get(a.get("status", "?"), 0) + 1
    rows = []
    for a in assets:
        t = a.get("tags") or {}
        if a.get("status") != "tagged":
            continue
        if a["kind"] == "video":
            for i, m in enumerate(t.get("moments") or []):
                rows.append(
                    {
                        "asset": a["id"],
                        "moment": i,
                        "kind": "video",
                        "type": m.get("type") or t.get("type"),
                        "score": m["score"],
                        "in": m["in"],
                        "out": m["out"],
                        "label": m["label"],
                        "speech": m.get("speech", False),
                        "used": moment_used(a, m),
                        "created": a.get("created"),
                    }
                )
        else:
            rows.append(
                {
                    "asset": a["id"],
                    "moment": None,
                    "kind": "photo",
                    "type": t.get("type"),
                    "score": t.get("score", t.get("quality", 3)),
                    "label": t.get("summary", ""),
                    "used": bool(a.get("used")),
                    "created": a.get("created"),
                }
            )
    if args.unused:
        rows = [r for r in rows if not r["used"]]
    if args.min_score:
        rows = [r for r in rows if r["score"] >= args.min_score]
    if args.type:
        rows = [r for r in rows if r["type"] == args.type]
    rows.sort(key=lambda r: (-r["score"], r.get("created") or ""), reverse=False)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    log(f"{len(assets)} assets · " + " · ".join(f"{v} {k}" for k, v in sorted(status.items())))
    for r in rows[: args.limit]:
        ref = f"{r['asset']}#{r['moment']}" if r["moment"] is not None else r["asset"]
        span = f"{fmt_ts(r['in'])}-{fmt_ts(r['out'])} ({r['out'] - r['in']:.1f}s)" if r["kind"] == "video" else "photo"
        flags = (" [speech]" if r.get("speech") else "") + (" [used]" if r["used"] else "")
        log(f"  {r['score']}★ {r['type']:<11} {ref:<40} {span:<22} {r['label']}{flags}")
    if len(rows) > args.limit:
        log(f"  … {len(rows) - args.limit} more (use --limit)")
    good = sum(1 for r in rows if not r["used"] and r["score"] >= 3)
    log(f"\n{good} unused moments scoring 3+ ≈ {good // 3} reels of material at ~3 moments per reel")
    return 0


def cmd_check(project: Project, args) -> int:
    """Checks every plan given (a batch: plans/2026-11-*.json) and reports all the problems, not just the first plan's."""
    from .render import check_plan
    from .review import auto_event

    failed = []
    for i, p in enumerate(args.plans):
        if i:
            log("")
        try:
            check_plan(project, p)
        except ReelsError as e:
            failed.append(p)
            print(f"error: {p}: {e}", file=sys.stderr)
    if failed:
        auto_event(project, "command-failed", f"check failed for {len(failed)} of {len(args.plans)} plan(s); record a sanitized cause and fix with review feedback")
        if len(args.plans) > 1:
            print(f"\n{len(failed)} of {len(args.plans)} plans have problems: {', '.join(Path(p).name for p in failed)}", file=sys.stderr)
    return 1 if failed else 0


def cmd_project(project: Project, args) -> int:
    from .kino import export_project

    export_project(project, args.plan, out=args.out)
    return 0


def cmd_render(project: Project, args) -> int:
    from .render import render_plan
    from .review import auto_event

    failed = 0
    for p in args.plans:
        auto_event(project, "render-attempt", f"Rendering {Path(p).name}")
        started = time.monotonic()
        try:
            result = render_plan(project, p, keep_staging=args.keep_staging)
            auto_event(project, "render-success", f"Rendered {Path(p).name} in {time.monotonic() - started:.1f}s", slug=result.get("slug"))
        except ReelsError as e:
            failed += 1
            auto_event(project, "render-failed", f"Render failed for {Path(p).name} after {time.monotonic() - started:.1f}s; record a sanitized cause and fix with review feedback")
            print(f"error: {p}: {e}", file=sys.stderr)
    return 1 if failed else 0


def cmd_discard(project: Project, args) -> int:
    from .render import discard

    discard(project, args.slug)
    return 0


def cmd_queue(project: Project, args) -> int:
    from .queue import list_posts, queue

    if args.list:
        list_posts(project)
        return 0
    times = args.times.split(",") if args.times else None
    queued = queue(project, start=args.start, times=times)
    if args.json:
        print(json.dumps(queued, indent=2))
    log(f"queue written to {project.rel(project.out_dir / 'QUEUE.md')}")
    return 0


def cmd_mark(project: Project, args) -> int:
    from .queue import mark, posts

    if args.slug == "rendered":  # e.g. the user approved the whole batch they just reviewed
        slugs = [p["slug"] for _, p in posts(project) if p.get("status") == "rendered"]
        if not slugs:
            log("no rendered (unreviewed) reels")
        for s in slugs:
            mark(project, s, args.status, note=args.note)
        return 0
    mark(project, args.slug, args.status, metricool_id=args.metricool_id, url=args.url, note=args.note)
    return 0


def cmd_export(project: Project, args) -> int:
    from .queue import export

    export(project, args.to, args.slugs or None)
    return 0


def cmd_upload(project: Project, args) -> int:
    from .queue import posts
    from .upload import upload

    slugs = list(args.slugs)
    if args.queued:
        slugs += [p["slug"] for _, p in posts(project) if p.get("status") == "queued" and not p.get("mediaUrl")]
    upload(project, slugs)
    return 0


def cmd_music(project: Project, args) -> int:
    from . import music

    if args.music_cmd == "search":
        results = music.search(project, " ".join(args.query), limit=args.limit, free_only=args.free)
        if args.json:
            print(json.dumps(results, indent=2))
        else:
            music.print_results(results)
        return 0
    if args.music_cmd == "beats":
        path, meta = music.resolve_track(project, args.track)
        found = music.ensure_beats(project, path, meta)
        if not found:
            raise ReelsError("couldn't find a steady beat in that track")
        start = music.parse_time(args.start) or 0.0
        beats = [b - start for b in found["beats"] if start - 0.01 <= b <= start + args.seconds]
        if args.json:
            print(json.dumps({"bpm": found["bpm"], "from": start, "beats": [round(b, 3) for b in beats]}, indent=2))
            return 0
        log(f"{found['bpm']:g} bpm (one beat = {60 / found['bpm']:.3f}s) — beat times counted from {fmt_ts(start)[:-2]} in the track:")
        log("  " + "  ".join(f"{b:.2f}" for b in beats))
        return 0
    if args.music_cmd in ("add", "yt2mp3"):
        meta = music.add(
            project,
            args.source,
            name=args.name,
            start=music.parse_time(args.start),
            credit=args.credit,
            allow_long=args.allow_long,
            instagram=args.instagram,
        )
        log(f"♪ {music.describe_track(meta)}  →  {meta['file']}" + (f"  ·  {meta['bpm']:g} bpm" if meta.get("bpm") else ""))
        if meta.get("instagram") and project.config["audio"]["attachSongs"]:
            log(f"  Instagram audio: \"{meta['instagram']['audio']}\" — reels upload without this copy (it's the preview/beat guide)")
            log("  and the licensed song is attached from Instagram's library when they're scheduled; it plays from 0:00")
            return 0
        start = meta.get("defaultStart", meta.get("suggestedStart") or 0)
        log(f"  starts at {fmt_ts(start)[:-2]} in reels (skip the intro) — override per reel with \"music\": {{\"track\": \"{meta['slug']}\", \"start\": …}}")
        if meta.get("instagram"):
            log("  trending song: mixed into reels like any track — Instagram usually recognizes it and shows the song on the reel")
        elif meta.get("freeUse"):
            log(f"  free to use: {', '.join(meta['freeUse'])}")
        if meta.get("caution"):
            log(f"  ⚠ {meta['caution']} — Instagram will probably mute reels using it")
        elif not (meta.get("freeUse") or meta.get("instagram")):
            log("  rights: unknown — copyrighted songs often get muted on Instagram")
        if meta.get("credit"):
            log(f"  credit (added to captions automatically): {meta['credit']}")
        return 0
    tracks = music.list_tracks(project)
    if args.json:
        print(json.dumps(tracks, indent=2))
    else:
        music.print_tracks(tracks, attach=project.config["audio"]["attachSongs"])
    return 0


def sfx_categories() -> list[str]:
    from .sfx import CATEGORIES

    return CATEGORIES


def cmd_sfx(project: Project, args) -> int:
    from . import sfx

    if args.sfx_cmd == "list":
        items = [m for m in sfx.list_sfx(project) if not args.category or m["category"] == args.category]
        if args.json:
            print(json.dumps(items, indent=2))
        else:
            sfx.print_sfx(items)
        return 0
    meta = sfx.add(
        project, args.source, name=args.name, category=args.category or "other", license=args.license, source_page=args.source_page,
        author=args.author, trim=args.trim, tags=(args.tags or "").split(","), from_asset=args.from_asset, at=args.at,
        length=args.length, clean=args.clean,
    )  # fmt: skip
    log(f"♪ sfx/{meta['slug']}.wav  ·  {meta['category']}  ·  {meta['duration']:.2f}s  ·  {meta['normalized']}  ·  {meta['license']}")
    return 0


def _add_music_add_args(s: argparse.ArgumentParser) -> None:
    s.add_argument("source", help="YouTube (or other) URL, or a local audio file")
    s.add_argument("--name", help="short name for the track (default: from the title)")
    s.add_argument("--start", help="where reels should start in the song, seconds or m:ss (default: auto, skips the intro)")
    s.add_argument("--credit", help="attribution line to add to captions, e.g. 'Music: Song by Artist'")
    s.add_argument("--allow-long", action="store_true", help="allow videos over 20 minutes")
    s.add_argument(
        "--instagram",
        help="marks a trending song: 'Title Artist' as Instagram lists it (or an audio id). Mixed into reels like any track; "
        "with audio.attachSongs on, reels upload without it and Instagram's copy is attached when scheduled",
    )
    s.set_defaults(fn=cmd_music)


def cmd_stats(project: Project, args) -> int:
    from .analytics import import_file, stats

    if args.words and args.words[0] == "import":
        if len(args.words) != 2:
            raise ReelsError("usage: ./reels stats import <file.json>")
        import_file(project, args.words[1])
        return 0
    if args.words:
        raise ReelsError("usage: ./reels stats [--since YYYY-MM-DD]  |  ./reels stats import <file.json>")
    stats(project, since=args.since)
    return 0


def cmd_strava(project: Project, args) -> int:
    from .strava import import_file, link_assets, print_list

    if args.strava_cmd == "import":
        for f in args.files:
            import_file(project, f, args.activity)
    elif args.strava_cmd == "link":
        link_assets(project, args.activities or None)
    else:
        print_list(project)
    return 0


def cmd_progress(project: Project, args) -> int:
    from .progress import print_progress, set_entry

    if args.progress_cmd == "set":
        set_entry(project, ftp=args.ftp, weight=args.weight, date=args.date, goal=args.goal)
    print_progress(project)
    return 0


def cmd_review(project: Project, args) -> int:
    from .review import record_feedback, register_experiment, review

    data = None
    if args.file:
        try:
            data = read_json(args.file)
        except (OSError, ValueError) as e:
            raise ReelsError(f"can't read review input: {e}") from e
    if args.review_cmd == "feedback":
        record_feedback(project, args.category, args.kind, args.note, slug=args.slug)
        log("feedback recorded")
    elif args.review_cmd == "experiment":
        register_experiment(project, data)
        log("experiment registered (design retained before rendering)")
    else:
        review(project, week=args.week, close=args.review_cmd == "close",
               slugs=getattr(args, "slugs", None), notes=data)
    return 0


def cmd_selftest(project: Project, args) -> int:
    from .selftest import selftest

    return 0 if selftest(project, keep=args.keep) else 1


# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reels", description="Turn ride footage into captioned Instagram Reels.")
    p.add_argument("--version", action="version", version=f"cycling-reels {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("doctor", help="check that everything needed is installed")
    s.add_argument("--fix", action="store_true", help="install/download what's missing")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("ingest", help="move new files from inbox/ into the library and make contact sheets")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--no-speech", action="store_true", help="skip speech detection")
    s.set_defaults(fn=cmd_ingest)

    s = sub.add_parser("pending", help="list assets waiting to be tagged")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_pending)

    s = sub.add_parser("frames", help="closer look: zoomed contact sheet or frames at exact times")
    s.add_argument("target", help="asset id, or a path to a rendered .mp4")
    s.add_argument("--start", type=float)
    s.add_argument("--end", type=float)
    s.add_argument("--count", type=int)
    s.add_argument("--at", help="comma-separated seconds, e.g. 12.5,14")
    s.add_argument("--views", action="store_true", help="360 only: front/right/behind/left at each time")
    s.add_argument("--view", help="360 only: preview a 9:16 reframe — angle name (rider) or yaw,pitch,fov e.g. 180,-10,80")
    s.add_argument("--planet", action="store_true", help="360 only: preview the tiny-planet look (yaw from --view)")
    s.set_defaults(fn=cmd_frames)

    s = sub.add_parser("tag", help="save Claude's classification for an asset")
    s.add_argument("asset")
    s.add_argument("--json")
    s.add_argument("--file")
    s.set_defaults(fn=cmd_tag)

    s = sub.add_parser("skip", help="mark an asset as unusable")
    s.add_argument("asset")
    s.add_argument("--reason")
    s.set_defaults(fn=cmd_skip)

    s = sub.add_parser("show", help="everything known about one asset")
    s.add_argument("asset")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("telemetry", help="GPS speed/route from GoPro telemetry; --at/--start/--fast look up a clip")
    s.add_argument("assets", nargs="*", help="asset ids (default: every GoPro clip not read yet)")
    s.add_argument("--force", action="store_true", help="read it again")
    s.add_argument("--at", help="speed/altitude at these seconds, e.g. 120.5 or 120,130")
    s.add_argument("--start", type=float, help="min/avg/max speed from here…")
    s.add_argument("--end", type=float, help="…to here (default start+5)")
    s.add_argument("--fast", type=float, metavar="SECONDS", help="fastest gap-free stretches of this length")
    s.set_defaults(fn=cmd_telemetry)

    s = sub.add_parser("transcribe", help="word-level transcripts (default: clips with detected speech)")
    s.add_argument("assets", nargs="*")
    s.add_argument("--force", action="store_true")
    s.add_argument("--model", help="e.g. small.en, medium.en, large-v3-turbo")
    s.set_defaults(fn=cmd_transcribe)

    s = sub.add_parser("sounds", help="candidate ride sounds (freehub, shifts, pass-bys…) + a labelled spectrogram image")
    s.add_argument("asset", nargs="?")
    s.add_argument("--start", type=float)
    s.add_argument("--end", type=float)
    s.add_argument("--pending", action="store_true", help="analyse every video with audio that has no cached candidates")
    s.add_argument("--force", action="store_true", help="re-run detection instead of using library/sounds/<id>.json")
    s.add_argument("--kind", help="only this kind (freehub, shift, pass-by, bell, …)")
    s.add_argument("--best", action="store_true", help="sort by confidence instead of time")
    s.add_argument("--min-conf", type=float, default=0.0)
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--no-image", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sounds)

    s = sub.add_parser("library", help="tagged moments, best first")
    s.add_argument("--unused", action="store_true")
    s.add_argument("--min-score", type=int)
    s.add_argument("--type")
    s.add_argument("--sounds", action="store_true", help="tagged sounds instead of moments")
    s.add_argument("--kind", help="with --sounds: only this kind")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_library)

    s = sub.add_parser("check", help="validate plan files and print their timelines (no rendering)")
    s.add_argument("plans", nargs="+")
    s.set_defaults(fn=cmd_check)

    s = sub.add_parser("render", help="render plan files to out/<slug>/reel.mp4")
    s.add_argument("plans", nargs="+")
    s.add_argument("--keep-staging", action="store_true")
    s.set_defaults(fn=cmd_render)

    s = sub.add_parser(
        "project",
        help="write one reel as a self-contained Remotion project (open it in Remotion Studio, or import the folder into Kino)",
    )
    s.add_argument("plan")
    s.add_argument("--out", help="empty folder to write it to (default: out/<slug>/project)")
    s.set_defaults(fn=cmd_project)

    s = sub.add_parser("discard", help="delete a rendered reel and free its clips")
    s.add_argument("slug")
    s.set_defaults(fn=cmd_discard)

    s = sub.add_parser("queue", help="give approved reels posting slots and write out/QUEUE.md")
    s.add_argument("--start", help="first day, YYYY-MM-DD (default: tomorrow)")
    s.add_argument("--times", help="comma-separated HH:MM slots per day (default: schedule.times)")
    s.add_argument("--list", action="store_true", help="just list every reel and its status")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_queue)

    s = sub.add_parser("mark", help="update a reel's status (approved, scheduled, posted, rejected…)")
    s.add_argument("slug", help="reel slug, or 'rendered' to mark every rendered (unreviewed) reel")
    s.add_argument("status")
    s.add_argument("--metricool-id")
    s.add_argument("--url")
    s.add_argument("--note")
    s.set_defaults(fn=cmd_mark)

    s = sub.add_parser("export", help="copy approved/queued reels + caption .txt files to a folder (e.g. iCloud Drive)")
    s.add_argument("--to", required=True, help='destination folder, e.g. "~/Library/Mobile Documents/com~apple~CloudDocs/Reels"')
    s.add_argument("slugs", nargs="*", help="specific reels (default: everything approved, queued or scheduled)")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("upload", help="push reels to S3/R2 storage for a public URL (optional)")
    s.add_argument("slugs", nargs="*")
    s.add_argument("--queued", action="store_true", help="every queued reel without a URL yet")
    s.set_defaults(fn=cmd_upload)

    s = sub.add_parser("music", help="background music: search YouTube, download tracks as MP3, list them")
    msub = s.add_subparsers(dest="music_cmd", required=True)
    m = msub.add_parser("search", help="search YouTube for tracks")
    m.add_argument("query", nargs="+")
    m.add_argument("--limit", type=int, default=8)
    m.add_argument("--free", action="store_true", help="only tracks marked free to use (NCS, Creative Commons, no-copyright)")
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_music)
    _add_music_add_args(msub.add_parser("add", help="download a track into music/ (MP3) and record its source and credit"))
    m = msub.add_parser("list", help="tracks in music/ with source, rights and credit")
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_music)
    m = msub.add_parser("beats", help="tempo and beat times of a track, for timing cuts and text")
    m.add_argument("track", help="track slug from ./reels music list")
    m.add_argument("--from", dest="start", help="count from this point in the track, seconds or m:ss (default 0)")
    m.add_argument("--for", dest="seconds", type=float, default=30.0, help="how many seconds of beats to list (default 30)")
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_music)

    s = sub.add_parser("sfx", help="free sound-effect library: list, add from a file/URL, or cut a sound out of your own footage")
    fsub = s.add_subparsers(dest="sfx_cmd", required=True)
    m = fsub.add_parser("list", help="sounds in sfx/ with length, level and license")
    m.add_argument("--category", choices=sfx_categories())
    m.add_argument("--json", action="store_true")
    m.set_defaults(fn=cmd_sfx)
    m = fsub.add_parser("add", help="add a CC0/Pixabay sound (file or URL), or cut one from footage with --from")
    m.add_argument("source", nargs="?", help="audio file or URL (omit with --from)")
    m.add_argument("--name", required=True, help="slug, e.g. whoosh-short")
    m.add_argument("--category", choices=sfx_categories(), help="whoosh, swish, riser, hit, pop, click, shutter, bell, scratch, stop, nat, other")
    m.add_argument("--license", help='e.g. "CC0-1.0" or "Pixabay Content License" (required for files/URLs)')
    m.add_argument("--source", dest="source_page", help="URL of the sound's page (required for files/URLs)")
    m.add_argument("--author", help="who made it")
    m.add_argument("--trim", help="START,END seconds to keep before silence trimming")
    m.add_argument("--tags", help="comma-separated tags")
    m.add_argument("--from", dest="from_asset", metavar="ASSET", help="cut from this library asset's audio instead (category nat)")
    m.add_argument("--at", type=float, help="with --from: start second")
    m.add_argument("--for", dest="length", type=float, help="with --from: length in seconds")
    m.add_argument("--clean", action="store_true", help="with --from: highpass ~90 Hz + light denoise for wind")
    m.set_defaults(fn=cmd_sfx)

    s = sub.add_parser("yt2mp3", help="download a YouTube track into music/ as MP3 (same as: music add)")
    _add_music_add_args(s)
    s.set_defaults(music_cmd="yt2mp3")

    s = sub.add_parser("stats", help="reel performance: 'stats import <file>' a Metricool pull, 'stats' for the report")
    s.add_argument("words", nargs="*", help="'import <file.json>' to load a pull (default: print the report)")
    s.add_argument("--since", help="only reels posted on/after YYYY-MM-DD")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("strava", help="Strava rides: import what Claude pulled from the Strava connector, link clips, list rides")
    s.set_defaults(fn=cmd_strava, strava_cmd="list")
    ssub = s.add_subparsers(dest="strava_cmd")
    r = ssub.add_parser("import", help="save list_activities / get_activity_performance / get_activity_streams output and link clips")
    r.add_argument("files", nargs="+", help="saved connector responses (JSON)")
    r.add_argument("--activity", help="ride id (needed for performance and streams files)")
    r = ssub.add_parser("link", help="re-link library clips to the imported rides (e.g. after ingesting more footage)")
    r.add_argument("activities", nargs="*", help="ride ids (default: all)")
    ssub.add_parser("list", help="imported rides, their numbers and linked clips")

    s = sub.add_parser("progress", help="series progress bar (road to 5 W/kg): the FTP and weight the user gave, by date")
    s.set_defaults(fn=cmd_progress, progress_cmd="show")
    psub = s.add_subparsers(dest="progress_cmd")
    r = psub.add_parser("set", help="log the user's FTP and weight for a date (replaces that date's entry)")
    r.add_argument("--ftp", type=float, help="FTP in watts, as the user gave it")
    r.add_argument("--weight", type=float, help="weight in kg, as the user gave it")
    r.add_argument("--date", help="YYYY-MM-DD (default: today)")
    r.add_argument("--goal", type=float, help="goal W/kg (default 5)")
    psub.add_parser("show", help="the logged entries")

    s = sub.add_parser("review", help="weekly learning: review evidence, record feedback/experiments, close a scheduled batch")
    s.add_argument("--week", help="date in the batch week, YYYY-MM-DD (default: current local week)")
    s.add_argument("--file", help="JSON strengths, weaknesses, decisions, proposals and lessons")
    s.set_defaults(fn=cmd_review)
    rsub = s.add_subparsers(dest="review_cmd")
    r = rsub.add_parser("close", help="snapshot this batch after scheduling, including unresolved posts")
    r.add_argument("--week", default=argparse.SUPPRESS, help="date in the batch week, YYYY-MM-DD")
    r.add_argument("--file", default=argparse.SUPPRESS, help="JSON review notes/lessons")
    r.add_argument("--slugs", nargs="+", required=True)
    r = rsub.add_parser("feedback", help="record an explicit user preference or a production issue")
    r.add_argument("--category", choices=("preference", "production"), required=True)
    r.add_argument("--kind", required=True, help="e.g. revision, rejection, crop, audio, scheduling-error")
    r.add_argument("--note", required=True, help="actual feedback or sanitized issue and fix; never credentials")
    r.add_argument("--slug", help="reel slug, optional for project-wide issues")
    r = rsub.add_parser("experiment", help="register a hypothesis and test/control slugs before rendering tests")
    r.add_argument("--file", required=True, help="experiment JSON; see reference/learning.md")

    s = sub.add_parser("selftest", help="end-to-end test on throwaway clips in a temp folder")
    s.add_argument("--keep", action="store_true")
    s.set_defaults(fn=cmd_selftest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project = None
    try:
        project = Project()
        return int(args.fn(project, args) or 0)
    except ReelsError as e:
        if project is not None and args.cmd != "review":
            from .review import auto_event

            # Do not persist raw errors/arguments: uploads and remote responses can contain secrets.
            auto_event(project, "command-failed", f"{args.cmd} failed; record a sanitized cause and fix with review feedback")
        print(f"error: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
