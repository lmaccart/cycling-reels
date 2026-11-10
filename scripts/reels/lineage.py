"""What went into each rendered reel, saved as out/<slug>/lineage.json.

Tags change over time, so the values a reel was built from are snapshotted when it renders. Reels
rendered before this existed get the same file from `./reels stats backfill-lineage`, built from
their plan and today's tags and marked `"backfilled": true`. Nothing here changes a tag or score.
Definitions of the reel-level numbers: .claude/skills/cycling-reels/reference/analytics.md
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from .config import Project
from .library import load_asset, resolve_id
from .util import ReelsError, log, now_iso, read_json, warn, write_json

TAG_FIELDS = ("score", "role", "speaker", "face", "mood", "opener", "line", "thread", "beat")


def best_moment(asset: dict, start: float, end: float) -> tuple[dict | None, float]:
    """The moment sharing the most seconds with [start, end] (ties: the closest fit, then the
    earliest), and the share of the span it covers. (None, 0.0) when nothing overlaps."""
    best, best_key = None, (0.0, 0.0)
    for m in (asset.get("tags") or {}).get("moments") or []:
        overlap = min(end, m["out"]) - max(start, m["in"])
        if overlap <= 0:
            continue
        key = (round(overlap, 3), overlap / (max(end, m["out"]) - min(start, m["in"])))
        if key > best_key:
            best, best_key = m, key
    return best, (best_key[0] / (end - start) if best else 0.0)


def _snapshot(visual: dict) -> dict:
    """Asset id, matched moment and a copy of the tag values for one clip or photo."""
    asset = visual["asset"]
    tags = asset.get("tags") or {}
    entry: dict[str, Any] = {"asset": asset["id"], "kind": asset.get("kind"), "cutaway": visual["cutaway"]}
    source: dict = {}
    if asset.get("kind") == "photo":
        entry.update(moment=None, match="photo")
        source = tags
    else:
        entry.update({"in": visual.get("in"), "out": visual.get("out")})
        moment, covered = None, 0.0
        if visual.get("in") is not None and visual.get("out") is not None and visual["out"] > visual["in"]:
            moment, covered = best_moment(asset, visual["in"], visual["out"])
        entry.update(moment=(moment or {}).get("id"), match="moment" if moment else "none", momentCoverage=round(covered, 4))
        source = moment or {}
    entry["tags"] = {f: source.get(f) for f in TAG_FIELDS}
    return entry


def _picture_spans(segments: list[dict]) -> list[tuple[int, float, float]]:
    """The picture timeline as (entry index, start, end). A segment ends where the next one
    starts, so a fade counts toward the outgoing shot; cutaways split the segment they cover."""
    spans: list[tuple[int, float, float]] = []
    index = 0
    for i, seg in enumerate(segments):
        own = index
        index += 1
        cursor, end = seg["start"], segments[i + 1]["start"] if i + 1 < len(segments) else seg["start"] + seg["seconds"]
        seg["end"] = end
        for c in sorted(seg["cutaways"], key=lambda c: c["start"]):
            c_index = index
            index += 1
            a, b = max(c["start"], cursor), min(c["start"] + c["seconds"], end)
            if b <= a:
                continue
            if a > cursor:
                spans.append((own, cursor, a))
            spans.append((c_index, a, b))
            cursor = b
        if end > cursor:
            spans.append((own, cursor, end))
    return spans


def _lineage(slug: str, segments: list[dict], *, backfilled: bool) -> dict:
    """The lineage document for a timeline of segments, each {"asset", "in", "out", "captions",
    "start", "seconds", "cutaway": False, "cutaways": [{"asset", "in", "out", "start", "seconds",
    "cutaway": True}]}, with times in reel seconds."""
    spans = _picture_spans(segments)
    runtime = max((e for _, _, e in spans), default=0.0)
    visuals = [v for seg in segments for v in (seg, *seg["cutaways"])]
    entries = [_snapshot(v) for v in visuals]
    on_screen = [0.0] * len(entries)
    for i, a, b in spans:
        on_screen[i] += b - a
    first, last = spans[0][0] if spans else None, spans[-1][0] if spans else None
    for i, entry in enumerate(entries):
        entry["position"] = "first" if i == first else "last" if i == last else "middle"
        entry["captions"] = visuals[i].get("captions") or "none"
        entry["seconds"] = round(on_screen[i], 3)
        entry["share"] = round(on_screen[i] / runtime, 4) if runtime else 0.0
    # A segment's voice carries on under its cutaways; the cutaways themselves are silent.
    voice = 0.0
    threads: Counter[str] = Counter()
    thread_seconds: Counter[str] = Counter()
    for seg, entry in ((v, e) for v, e in zip(visuals, entries) if not e["cutaway"]):
        tags = entry["tags"]
        if entry["captions"] == "transcript" or (tags["role"] == "voice" and tags["speaker"] == "me"):
            voice += seg["end"] - seg["start"]
        if tags["thread"]:
            threads[tags["thread"]] += 1
            thread_seconds[tags["thread"]] += seg["end"] - seg["start"]
    face = sum(on_screen[i] for i, e in enumerate(entries) if e["tags"]["face"] is True)
    thread = max(threads, key=lambda t: (threads[t], thread_seconds[t], t)) if threads else None
    return {
        "version": 1, "slug": slug, "createdAt": now_iso(), "backfilled": backfilled, "runtimeSeconds": round(runtime, 3),
        "voiceShare": round(voice / runtime, 4) if runtime else 0.0, "faceShare": round(face / runtime, 4) if runtime else 0.0,
        "avgShotSeconds": round(runtime / len(spans), 3) if spans else None, "thread": thread, "segments": entries,
    }  # fmt: skip


def from_built(slug: str, built: dict) -> dict:
    """Lineage for a plan as render.build() laid it out (retimed and beat-snapped)."""
    fps = built["fps"]
    segments = []
    for sg in built["segs"]:
        cutaways = [{"asset": c["seg"].asset, "in": c["seg"].spec.get("in"), "out": c["seg"].spec.get("out"), "start": (sg.start + c["start"]) / fps,
                     "seconds": c["seg"].frames / fps, "cutaway": True} for c in sg.spec.get("_cutaways") or []]  # fmt: skip
        segments.append({"asset": sg.asset, "in": sg.spec.get("in"), "out": sg.spec.get("out"), "captions": sg.spec.get("captions"),
                         "start": sg.start / fps, "seconds": sg.frames / fps, "cutaway": False, "cutaways": cutaways})  # fmt: skip
    return _lineage(slug, segments, backfilled=False)


def record(project: Project, slug: str, built: dict) -> None:
    """Saves lineage.json for a reel that just rendered. Bookkeeping must not fail a finished render."""
    try:
        write_json(project.out_dir / slug / "lineage.json", from_built(slug, built))
    except Exception as e:  # noqa: BLE001
        warn(f"couldn't save lineage for {slug} ({type(e).__name__}: {e}); ./reels stats backfill-lineage can write it later")


# ---------------------------------------------------------------- backfill


def _load(project: Project, token: Any) -> dict | None:
    try:
        return load_asset(project, resolve_id(project, str(token or "")))
    except ReelsError:
        return None


def _span(spec: dict, rendered: dict | None, asset: dict) -> tuple[float | None, float | None]:
    """In/out in the source: as rendered (post.json), else the plan, else the plan's moment index."""
    for source in (rendered or {}, spec):
        if source.get("in") is not None and source.get("out") is not None:
            return float(source["in"]), float(source["out"])
    moments = (asset.get("tags") or {}).get("moments") or []
    i = spec.get("moment")
    if isinstance(i, int) and 0 <= i < len(moments):
        return float(moments[i]["in"]), float(moments[i]["out"])
    return None, None


def _visual_from_plan(project: Project, spec: dict, rendered: dict | None, cutaway: bool) -> dict:
    asset = _load(project, spec.get("asset")) or {"id": str(spec.get("asset")), "kind": None, "tags": {}, "_missing": True}
    if asset.get("kind") == "photo":
        return {"asset": asset, "seconds": float(spec.get("duration", 2.5)), "cutaway": cutaway}
    a, b = _span(spec, rendered, asset)
    seconds = (b - a) / float(spec.get("speed") or 1) if a is not None and b is not None else 0.0
    return {"asset": asset, "in": a, "out": b, "seconds": seconds, "cutaway": cutaway}


def from_plan(project: Project, plan: dict, post: dict) -> dict:
    """Lineage rebuilt from a rendered reel's plan and post.json, without re-validating the plan
    (older plans needn't pass today's checks). post.json's in/out are preferred: beat snapping
    moved them, and plans may give only a moment index."""
    fps = int(project.cfg("render", "fps"))
    fade = int(project.cfg("render", "transitionFrames"))
    rendered = list(post.get("segments") or [])
    order = 0

    def next_rendered(spec: dict) -> dict | None:
        nonlocal order
        hit = rendered[order] if order < len(rendered) else None
        order += 1
        if hit and str(hit.get("asset", "")).startswith(str(spec.get("asset", ""))):
            return hit
        return None

    segments: list[dict] = []
    start, prev_frames = 0.0, None
    for raw in plan.get("segments") or []:
        seg = _visual_from_plan(project, raw, next_rendered(raw), False)
        seg["captions"] = raw.get("captions")
        frames = max(1, round(seg["seconds"] * fps))
        if prev_frames is not None:
            trans = max(0, min(fade, prev_frames - 1, frames - 1)) if raw.get("transition") == "fade" else 0
            start += (prev_frames - trans) / fps
        seg["start"] = start
        seg["cutaways"] = []
        for c in sorted(raw.get("cutaways") or [], key=lambda c: c.get("at", 0)):
            visual = _visual_from_plan(project, c, next_rendered(c), True)
            visual["start"] = start + float(c.get("at", 0))
            seg["cutaways"].append(visual)
        segments.append(seg)
        prev_frames = frames
    return _lineage(str(post.get("slug") or plan.get("slug")), segments, backfilled=True)


def backfill(project: Project) -> dict:
    """Writes lineage.json for every rendered reel that lacks one; reels that have one are left alone."""
    result: dict[str, Any] = {"written": [], "alreadyHad": 0, "noPlan": [], "momentMatched": 0, "photos": 0, "unmatched": []}
    for post_path in sorted(project.out_dir.glob("*/post.json")):
        out = post_path.parent
        if (out / "lineage.json").exists():
            result["alreadyHad"] += 1
            continue
        plan_path = next((p for p in (out / "plan.json", project.plans_dir / f"{out.name}.json") if p.exists()), None)
        if plan_path is None:
            result["noPlan"].append(out.name)
            continue
        doc = from_plan(project, read_json(plan_path), read_json(post_path))
        write_json(out / "lineage.json", doc)
        result["written"].append(out.name)
        for e in doc["segments"]:
            if e["match"] == "moment":
                result["momentMatched"] += 1
            elif e["match"] == "photo":
                result["photos"] += 1
            else:
                result["unmatched"].append({"slug": out.name, "asset": e["asset"], "in": e.get("in"), "out": e.get("out"), "cutaway": e["cutaway"]})
    log(f"lineage backfilled for {len(result['written'])} reel(s) ({result['alreadyHad']} already had one)")
    log(f"  segments: {result['momentMatched']} matched a moment, {result['photos']} photo(s) use the photo score, {len(result['unmatched'])} unmatched")
    for u in result["unmatched"]:
        log(f"  UNMATCHED {u['slug']}: {u['asset']} {u['in']}-{u['out']}" + (" (cutaway)" if u["cutaway"] else ""))
    for slug in result["noPlan"]:
        log(f"  no plan found for {slug}; skipped")
    return result
