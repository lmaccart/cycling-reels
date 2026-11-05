from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from .bleep import check_bleep, masked_words, mute_filter, resolve_bleeps, tone_graph, unbleeped_swears
from .config import Project
from .images import open_image
from .library import all_assets, load_asset, resolve_id, save_asset
from .loop import check_loop, check_seam, describe_seam, seam_score, staged_seam, swap_halves
from .media import extract_frame, ffprobe_json, hdr_filters, require, run
from .mix import CLEAN_AF, EDGE_FADE, RECOGNITION_MAX, build_cues, check_segment, covered_share, cue_props, cue_sheet
from .mix import music_drops, music_levels, nat_windows, quiet_nat, volume_keys
from .music import ensure_beats, parse_time, record_usage, release_usage, resolve_track
from .punch import check_beats_punch, check_punch, ends_zoomed, punch_keys, resolve_punches, stage_scale
from .queue import caption_block, carry_over, post_extras
from .retention import caption_cuts, retention_warnings
from .sheets import build_sheet
from .sphere import camera_filter, photo_view, plan_camera, source_graph
from .util import ReelsError, clamp, even, fmt_ts, log, now_iso, quick_hash, read_json, slugify, warn, write_json

FRAMES = ("crop", "blurfill", "fit")
FITS = ("cover", "blurfill")
MOTIONS = ("none", "zoom-in", "zoom-out", "pan-left", "pan-right", "pan-up", "pan-down")
POSITIONS = ("top", "center", "lower")
HOOK_POSITIONS = ("top", "center")
STYLES = ("light", "dark", "plain")
TRANSITIONS = ("cut", "fade")
_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
# Layout of the 1080x1920 frame (px): stat pills stack from STATS_TOP; hook and
# top text sit at TOP_TEXT unless pills push them down.
STATS_TOP, STAT_ROW, TOP_TEXT = 240, 78, 420
# GPS route map: a square in the top-right corner, level with the stat pills.
ROUTE_TOP, ROUTE_SIZE = STATS_TOP, 220
GPS_VALUES = ("gps", "gps:max", "gps:avg")
GPS_LABELS = {"gps": "speed", "gps:max": "top speed", "gps:avg": "avg"}
from .strava import RIDE_VALUES as STRAVA_RIDE_VALUES  # noqa: E402
from .strava import VALUES as STRAVA_VALUES  # noqa: E402


@dataclass
class Seg:
    index: int
    kind: str
    asset: dict
    spec: dict
    frames: int
    trans: int = 0
    start: int = 0

    @property
    def end(self) -> int:
        return self.start + self.frames


# --------------------------------------------------------------------------
# Plan loading and validation
# --------------------------------------------------------------------------


def load_plan(project: Project, plan_path: str | Path) -> dict:
    p = project.abs(plan_path)
    if not p.exists():
        raise ReelsError(f"plan not found: {plan_path}")
    try:
        plan = read_json(p)
    except Exception as e:  # noqa: BLE001
        raise ReelsError(f"{p.name} is not valid JSON: {e}") from e
    if not isinstance(plan, dict):
        raise ReelsError(f"{p.name} must contain a JSON object")
    plan.setdefault("slug", slugify(p.stem, 60))
    return plan


def _num(spec: dict, key: str, default: float, lo: float, hi: float, errors: list[str], where: str) -> float:
    v = spec.get(key, default)
    if v is None:
        return default
    try:
        v = float(v)
    except (TypeError, ValueError):
        errors.append(f"{where}: {key} must be a number")
        return default
    if not (lo <= v <= hi):
        errors.append(f"{where}: {key}={v:g} is outside {lo:g}–{hi:g}")
    return v


def _pick(spec: dict, key: str, default: str, allowed: tuple[str, ...], errors: list[str], where: str) -> str:
    v = spec.get(key) or default
    if v not in allowed:
        errors.append(f"{where}: {key} must be one of {', '.join(allowed)} (got {v!r})")
    return v


def footage_problem(project: Project, asset: dict, cache: dict[str, str | None]) -> str | None:
    """Every visual must be the user's own recording: a file in library/media,
    unchanged since it was ingested from inbox/ (fingerprint check)."""
    if asset["id"] in cache:
        return cache[asset["id"]]
    problem = None
    path = project.abs(asset["media"]).resolve()
    if asset.get("kind") not in ("video", "photo"):
        problem = f"{asset['id']} isn't a video or photo from your library"
    elif project.media_dir.resolve() not in path.parents:
        problem = f"{asset['id']} isn't in library/media — only footage ingested from inbox/ can be used"
    elif not path.is_file():
        problem = f"{asset['id']}: its file is missing ({asset['media']})"
    elif not asset.get("hash") or quick_hash(path) != asset["hash"]:
        problem = f"{asset['id']}: the file changed after it was ingested — put the original back in inbox/ and re-ingest"
    cache[asset["id"]] = problem
    return problem


def _visual(
    project: Project, s: dict, where: str, errors: list[str], warnings: list[str], verified: dict[str, str | None]
) -> tuple[dict, dict, float] | None:
    """One clip or photo from the library — a segment or a cutaway: footage check, in/out (or a
    tagged moment), speed, framing and 360 camera. Returns (asset, spec, seconds on screen), or
    None after recording the problem in `errors`."""
    try:
        asset = load_asset(project, resolve_id(project, str(s.get("asset", ""))))
    except ReelsError as e:
        errors.append(f"{where}: {e}")
        return None
    problem = footage_problem(project, asset, verified)
    if problem:
        errors.append(f"{where}: {problem}")
        return None
    spec: dict[str, Any] = dict(s)
    spec["asset"] = asset["id"]
    tags = asset.get("tags") or {}
    if asset.get("status") == "skip":
        warnings.append(f"{where}: {asset['id']} was tagged skip")

    is_360 = bool(asset["probe"].get("sphere"))
    moment_view = None
    if asset["kind"] == "video":
        dur = float(asset["probe"]["duration"])
        if "moment" in spec:
            moments = tags.get("moments") or []
            mi = spec["moment"]
            if not isinstance(mi, int) or not 0 <= mi < len(moments):
                errors.append(f"{where}: moment {mi!r} doesn't exist on {asset['id']} ({len(moments)} moments)")
                return None
            m = moments[mi]
            spec.setdefault("in", m["in"])
            spec.setdefault("out", m["out"])
            moment_view = m.get("view")
            if not is_360:
                spec.setdefault("focusX", m.get("focusX", 0.5))
                spec.setdefault("focusY", m.get("focusY", 0.5))
        if "in" not in spec or "out" not in spec:
            errors.append(f"{where}: video clips need in/out seconds (or a moment index)")
            return None
        spec["in"] = _num(spec, "in", 0, 0, dur, errors, where)
        spec["out"] = min(_num(spec, "out", dur, 0, dur + 0.05, errors, where), dur)
        if spec["out"] - spec["in"] < 0.3:
            errors.append(f"{where}: out must be at least 0.3 s after in")
            return None
        spec["speed"] = _num(spec, "speed", 1.0, 0.25, 4.0, errors, where)
        spec["zoom"] = _num(spec, "zoom", 1.0, 1.0, 3.0, errors, where)
        spec["focusX"] = _num(spec, "focusX", 0.5, 0, 1, errors, where)
        spec["focusY"] = _num(spec, "focusY", 0.5, 0, 1, errors, where)
        spec["frame"] = _pick(spec, "frame", "crop", FRAMES, errors, where)
        spec["captions"] = _pick(spec, "captions", "none", ("transcript", "none"), errors, where)
        if spec["captions"] == "transcript" and not asset.get("transcript"):
            errors.append(f"{where}: captions=transcript but {asset['id']} has no transcript (./reels transcribe {asset['id']})")
        seconds = (spec["out"] - spec["in"]) / spec["speed"]
        if is_360:
            spec["sphere"] = plan_camera(spec, tags, moment_view, seconds, errors, warnings, where, s)
    else:
        spec["duration"] = _num(spec, "duration", 2.5, 0.8, 12, errors, where)
        spec["motion"] = _pick(spec, "motion", "zoom-in", MOTIONS, errors, where)
        spec["fit"] = _pick(spec, "fit", "cover", FITS, errors, where)
        spec["focusX"] = _num(spec, "focusX", tags.get("focusX", 0.5), 0, 1, errors, where)
        spec["focusY"] = _num(spec, "focusY", tags.get("focusY", 0.5), 0, 1, errors, where)
        if spec.get("captions") == "transcript":
            errors.append(f"{where}: photos can't have transcript captions")
        spec["captions"] = "none"
        seconds = spec["duration"]
        if is_360:
            spec["sphere"] = plan_camera(spec, tags, None, seconds, errors, warnings, where, s)
            spec["fit"] = "cover"
            if spec["sphere"]["path"]:
                spec["motion"] = "none"  # the camera move is the motion
    return asset, spec, seconds


# Cutaways: B-roll laid over a video segment for a moment while the segment's own audio (the
# voice), captions and text carry on. The segment owns everything but the picture, so these
# fields belong on it, not on a cutaway.
CUTAWAY_SEGMENT_ONLY = ("captions", "text", "stats", "route", "nat", "bleep", "punch", "sfx", "transition", "payoff",
                        "cutaways", "volume", "clean")  # fmt: skip
CUTAWAY_MIN_FRAMES = 12  # 0.4 s at 30 fps: anything shorter reads as a glitch


def _cutaways(
    project: Project, sg: Seg, raw: Any, fps: int, errors: list[str], warnings: list[str], verified: dict[str, str | None], where: str
) -> list[dict]:
    """A segment's cutaways, sorted: [{"seg": Seg (the B-roll; staged silent), "start": frames from the segment start, "where"}]."""
    if raw in (None, False, []):
        return []
    if not isinstance(raw, list):
        errors.append(f'{where}: cutaways must be a list like [{{"at": 1.5, "asset": "<id>", "in": 3, "out": 5}}]')
        return []
    if sg.kind != "video":
        errors.append(f"{where}: cutaways go on video segments — they cover the picture while the clip's own audio keeps going")
        return []
    out: list[dict] = []
    for k, c in enumerate(raw):
        cw = f"{where} cutaway {k + 1}"
        if not isinstance(c, dict) or c.get("at") is None or not c.get("asset"):
            errors.append(f'{cw}: use {{"at": <seconds from the segment start>, "asset": "<id>", "in": …, "out": …}} (or "moment"; "duration" for a photo)')
            continue
        misplaced = [key for key in CUTAWAY_SEGMENT_ONLY if key in c]
        if misplaced:
            errors.append(f"{cw}: {', '.join(misplaced)} belong on the segment — under a cutaway its own audio, captions and text keep going")
            continue
        at = _num(c, "at", 0, 0, 600, errors, cw)
        got = _visual(project, {key: v for key, v in c.items() if key != "at"}, cw, errors, warnings, verified)
        if got is None:
            continue
        asset, spec, seconds = got
        # silent B-roll: nothing of its own but the picture
        spec.update(captions="none", transition="cut", nat=[], clean=False, payoff=False, bleep=None, _bleeps=[], punch=None, _punches=[])
        frames = max(1, round(seconds * fps))
        start = round(at * fps)
        if frames < CUTAWAY_MIN_FRAMES:
            errors.append(f"{cw}: {seconds:.2f}s is too short to register — make it at least {CUTAWAY_MIN_FRAMES / fps:.1f}s")
            continue
        if start + frames > sg.frames + 1:
            errors.append(f"{cw}: runs {(start + frames - sg.frames) / fps:.2f}s past the end of its segment ({sg.frames / fps:.2f}s) — move \"at\" or shorten it")
            continue
        out.append({"seg": Seg(index=sg.index, kind=asset["kind"], asset=asset, spec=spec, frames=frames), "start": start, "where": cw})
    out.sort(key=lambda c: c["start"])
    for a, b in zip(out, out[1:]):
        if b["start"] < a["start"] + a["seg"].frames:
            errors.append(f"{b['where']}: overlaps {a['where']} — one cutaway at a time")
    if out:
        # A data pill describes the clip under the cutaway, not the footage on screen.
        values = [str(st.get("value") or "").strip().lower() for st in sg.spec.get("stats") or [] if isinstance(st, dict)]
        clip_data = [v for v in values if v.startswith("gps") or (v.startswith("strava:") and v not in STRAVA_RIDE_VALUES)]
        if clip_data or sg.spec.get("route"):
            errors.append(f"{where}: {', '.join(clip_data) or 'the route map'} would describe the clip under the cutaways, not the footage "
                          "on screen — put live numbers on a segment without cutaways (whole-ride Strava totals are fine)")  # fmt: skip
    return out


def fit_cutaways(segs: list[Seg], fps: int, warnings: list[str]) -> None:
    """Beat snapping / loop retiming can shorten a segment by up to a beat: cutaways that no
    longer fit are trimmed to its end, or dropped when too little is left."""
    for sg in segs:
        kept = []
        for c in sg.spec.get("_cutaways") or []:
            room = sg.frames - c["start"]
            if room < CUTAWAY_MIN_FRAMES:
                warnings.append(f"{c['where']}: its segment got shorter when it was retimed to the music — the cutaway no longer fits, dropped")
                continue
            if c["seg"].frames > room:
                _set_frames(c["seg"], room, fps)
            kept.append(c)
        sg.spec["_cutaways"] = kept


def cutaway_name(sg: Seg, k: int) -> str:
    """Staged file stem of a segment's k-th cutaway (the template and stage() agree on it)."""
    return f"cut{sg.index}-{k}"


def cutaway_props(sg: Seg) -> list[dict]:
    """Template props for a segment's cutaways (frames relative to the segment start)."""
    out = []
    for k, c in enumerate(sg.spec.get("_cutaways") or []):
        cs = c["seg"]
        p: dict[str, Any] = {"startFrame": c["start"], "durationInFrames": cs.frames}
        if cs.kind == "video" or (cs.spec.get("sphere") or {}).get("path"):
            p.update(kind="video", src=f"{cutaway_name(sg, k)}.mp4")
        else:
            p.update(kind="photo", src=f"{cutaway_name(sg, k)}.jpg", motion=cs.spec["motion"], fit=cs.spec["fit"],
                     focusX=cs.spec["focusX"], focusY=cs.spec["focusY"])  # fmt: skip
        out.append(p)
    return out


def visuals(segs: list[Seg]) -> list[tuple[str, Seg, bool]]:
    """Every clip and photo on screen: (where, Seg, is_cutaway) for the segments and their cutaways."""
    out = []
    for sg in segs:
        out.append((f"segment {sg.index + 1}", sg, False))
        out += [(c["where"], c["seg"], True) for c in sg.spec.get("_cutaways") or []]
    return out


def reuse_warnings(slug: str, segs: list[Seg]) -> list[str]:
    """Footage another reel already used (rendered, not discarded): followers notice repeats."""
    out = []
    for where, v, _ in visuals(segs):
        for u in v.asset.get("used") or []:
            other = u.get("plan")
            if not other or other == slug:
                continue
            if v.kind == "video" and "in" in u:
                if u["in"] < v.spec["out"] and v.spec["in"] < u["out"]:
                    span = f"{fmt_ts(max(u['in'], v.spec['in']))}-{fmt_ts(min(u['out'], v.spec['out']))}"
                    out.append(f"{where}: {v.asset['id']} {span} is already in {other} — pick unused footage (./reels library --unused)")
                    break
            elif v.kind == "photo":
                out.append(f"{where}: photo {v.asset['id']} is already in {other} — pick an unused one (./reels library --unused)")
                break
    return out


def normalize(project: Project, plan: dict) -> tuple[list[Seg], list[str]]:
    fps = int(project.cfg("render", "fps"))
    errors: list[str] = []
    warnings: list[str] = []
    verified: dict[str, str | None] = {}
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", str(plan.get("slug") or "")):
        errors.append("slug must be lowercase letters, digits and dashes (e.g. 2026-10-26-climb-attack)")
    raw = plan.get("segments") or []
    if not raw:
        errors.append("plan needs at least one segment")

    segs: list[Seg] = []
    for i, s in enumerate(raw):
        where = f"segment {i + 1}"
        if not isinstance(s, dict):
            errors.append(f"{where}: must be an object")
            continue
        got = _visual(project, s, where, errors, warnings, verified)
        if got is None:
            continue
        asset, spec, seconds = got
        spec["transition"] = _pick(spec, "transition", "cut", TRANSITIONS, errors, where)
        check_segment(spec, asset, errors, where)
        check_punch(spec, asset, errors, where)
        check_bleep(spec, asset, errors, where)
        if spec.get("volume") is not None:
            spec["volume"] = _num(spec, "volume", 1.0, 0, 1.5, errors, where)
        frames = max(1, round(seconds * fps))
        if frames < 10:
            warnings.append(f"{where}: only {frames} frames long")
        sg = Seg(index=len(segs), kind=asset["kind"], asset=asset, spec=spec, frames=frames)
        spec["_cutaways"] = _cutaways(project, sg, s.get("cutaways"), fps, errors, warnings, verified, where)
        segs.append(sg)

    if errors:
        raise ReelsError("plan problems:\n  - " + "\n  - ".join(errors))

    tf = int(project.cfg("render", "transitionFrames"))
    for i, sg in enumerate(segs):
        if i > 0 and sg.spec["transition"] == "fade":
            sg.trans = max(0, min(tf, segs[i - 1].frames - 1, sg.frames - 1))
        sg.start = 0 if i == 0 else segs[i - 1].start + segs[i - 1].frames - sg.trans
    return segs, warnings


# --------------------------------------------------------------------------
# Timeline -> template props
# --------------------------------------------------------------------------


def screen_text(text: Any, warnings: list[str], where: str) -> str:
    """On-screen text is shown exactly as written (no uppercasing); *asterisks* are dropped."""
    t = re.sub(r"\s+", " ", str(text or "").replace("*", "")).strip()
    if len(_EMOJI.findall(t)) > 2:
        warnings.append(f"{where}: more than two emoji — one usually reads more natural")
    return t


def _clean_word(w: str) -> str:
    w = w.strip().strip('"“”')
    return re.sub(r"[,.;:]+$", "", w)


def caption_pages_for(sg: Seg, words: list[dict], fps: int, cfg: dict) -> list[dict]:
    a, b, speed = sg.spec["in"], sg.spec["out"], sg.spec["speed"]
    items: list[list[Any]] = []
    for w in words:
        if w["e"] <= a + 0.02 or w["s"] >= b - 0.02:
            continue
        text = _clean_word(str(w["w"]))
        if not text:
            continue
        s, e = max(w["s"], a), min(w["e"], b)
        sf = sg.start + round((s - a) / speed * fps)
        ef = max(sf + 1, sg.start + round((e - a) / speed * fps))
        items.append([text, sf, ef, str(w["w"]).strip()])

    max_words, max_chars = int(cfg["maxWordsPerPage"]), int(cfg["maxCharsPerPage"])
    pages: list[list[list[Any]]] = []
    cur: list[list[Any]] = []
    for it in items:
        if cur:
            gap = it[1] - cur[-1][2]
            chars = sum(len(x[0]) for x in cur) + len(cur) + len(it[0])
            sentence_end = cur[-1][3].endswith((".", "?", "!", ","))
            if len(cur) >= max_words or chars > max_chars or gap > 0.6 * fps or sentence_end:
                pages.append(cur)
                cur = []
        cur.append(it)
    if cur:
        pages.append(cur)

    out = []
    for pi, pg in enumerate(pages):
        for wi in range(len(pg) - 1):
            pg[wi][2] = max(pg[wi][2], pg[wi + 1][1])
        start = pg[0][1]
        end = pg[-1][2] + round(0.25 * fps)
        if pi + 1 < len(pages):
            end = min(end, pages[pi + 1][0][1])
        end = min(max(end, start + round(0.4 * fps)), sg.end)
        if pi + 1 < len(pages):
            end = min(end, pages[pi + 1][0][1])
        if end <= start:
            continue
        out.append(
            {
                "startFrame": start,
                "endFrame": end,
                "words": [{"text": t, "startFrame": sf, "endFrame": min(ef, end)} for t, sf, ef, _ in pg],
            }
        )
    return out


def _trim(items: list[dict], limit: int, min_len: int = 8) -> list[dict]:
    out = []
    for it in items:
        if it["startFrame"] >= limit - min_len:
            continue
        it = dict(it)
        it["endFrame"] = min(it["endFrame"], limit)
        if "words" in it:
            it["words"] = [dict(w, endFrame=min(w["endFrame"], limit)) for w in it["words"] if w["startFrame"] < limit]
        out.append(it)
    return out


def _overlaps(a: dict, b: dict) -> bool:
    return a["startFrame"] < b["endFrame"] and b["startFrame"] < a["endFrame"]


def place_beats(
    beats: list[dict], hook: dict | None, pages: list[dict], stats: list[dict], warnings: list[str], routes: list[dict] = ()
) -> list[dict]:
    """Moves text beats out of the way of the hook, captions and stat chips."""
    out = []
    for b in beats:
        b = dict(b)
        talking = any(_overlaps(b, p) for p in pages)
        if b["position"] == "lower" and talking:
            b["position"] = "top"
            warnings.append(f"text '{b['text']}' would cover captions — moved it to the top")
        if hook and _overlaps(b, hook) and b["position"] in ("top", "center"):
            if not talking:
                b["position"] = "lower"
                warnings.append(f"text '{b['text']}' would collide with the hook — moved it to the lower third")
            elif b["endFrame"] - hook["endFrame"] >= 8:
                b["startFrame"] = hook["endFrame"]
                warnings.append(f"text '{b['text']}' now starts after the hook")
            else:
                warnings.append(f"text '{b['text']}' collides with the hook and captions — dropped")
                continue
        if b["position"] == "top":
            top = top_offset(b, stats, routes)
            if top:
                b["offsetTop"] = top
        out.append(b)
    return out


def top_offset(item: dict, stats: list[dict], routes: list[dict] = ()) -> int | None:
    """Pushes top-placed text below any stat pills (or route map) visible while it's on screen."""
    chips = max(
        (sum(1 for s in stats if s["startFrame"] <= f < s["endFrame"]) for f in range(item["startFrame"], item["endFrame"])),
        default=0,
    )
    needed = STATS_TOP + chips * STAT_ROW + 24
    if any(_overlaps(item, r) for r in routes):
        needed = max(needed, ROUTE_TOP + ROUTE_SIZE + 24)
    return needed if needed > TOP_TEXT else None


def _duck_ranges(pages: list[dict]) -> list[list[int]]:
    ranges: list[list[int]] = []
    for p in sorted(pages, key=lambda p: p["startFrame"]):
        s, e = p["startFrame"], p["endFrame"]
        if ranges and s - ranges[-1][1] <= 15:
            ranges[-1][1] = max(ranges[-1][1], e)
        else:
            ranges.append([s, e])
    return ranges


def _style(value: Any, default: str, warnings: list[str], where: str) -> str:
    if value in (None, ""):
        return default
    if value not in STYLES:
        warnings.append(f"{where}: style {value!r} unknown — using '{default}' (options: {', '.join(STYLES)})")
        return default
    return str(value)


def _beats(plan: dict, segs: list[Seg], total: int, fps: int, default_style: str, warnings: list[str]) -> list[dict]:
    beats: list[dict] = []

    def add(b: dict, s: int, e: int, pos: str, where: str) -> None:
        text = screen_text(b.get("text"), warnings, where)
        if not text:
            return
        if pos not in POSITIONS:
            warnings.append(f"{where}: position {pos!r} unknown — using 'lower'")
            pos = "lower"
        s = int(clamp(s, 0, total - 1))
        e = int(clamp(e, s + 1, total))
        if e - s < 8:
            warnings.append(f"{where}: on screen for under 8 frames — dropped")
            return
        style = _style(b.get("style"), default_style, warnings, where)
        beats.append({"text": text, "startFrame": s, "endFrame": e, "position": pos, "style": style})

    for sg in segs:
        default_pos = "top" if sg.spec["captions"] == "transcript" else "lower"
        for j, b in enumerate(sg.spec.get("text") or []):
            b = {"text": b} if isinstance(b, str) else b
            at, dur = float(b.get("at", 0.15)), float(b.get("for", 2.2))
            add(b, sg.start + round(at * fps), sg.start + round((at + dur) * fps), b.get("position") or default_pos, f"segment {sg.index + 1} text[{j}]")
    for j, b in enumerate(plan.get("text") or []):
        b = {"text": b} if isinstance(b, str) else b
        at, dur = float(b.get("at", 0)), float(b.get("for", 2.2))
        add(b, round(at * fps), round((at + dur) * fps), b.get("position") or "lower", f"text[{j}]")

    for i, x in enumerate(beats):
        for y in beats[i + 1 :]:
            if x["position"] == y["position"] and x["startFrame"] < y["endFrame"] and y["startFrame"] < x["endFrame"]:
                warnings.append(f"text '{x['text']}' and '{y['text']}' overlap at the same position")
    return beats


def _stats(project: Project, segs: list[Seg], total: int, fps: int, warnings: list[str]) -> list[dict]:
    stats = []
    errors: list[str] = []
    for sg in segs:
        for j, st in enumerate(sg.spec.get("stats") or []):
            where = f"segment {sg.index + 1} stats[{j}]"
            raw = str(st.get("value") or "").strip().lower()
            gps = raw if raw.startswith(("gps", "strava:")) else None
            if gps and gps.startswith("gps") and gps not in GPS_VALUES:
                errors.append(f"{where}: value {gps!r} unknown — GPS values are {', '.join(GPS_VALUES)}")
                continue
            if gps and gps.startswith("strava:") and gps not in STRAVA_VALUES:
                errors.append(f"{where}: value {gps!r} unknown — Strava values are {', '.join(STRAVA_VALUES)}")
                continue
            label = screen_text(st.get("label") or ((GPS_LABELS.get(gps) or STRAVA_VALUES.get(gps, "")) if gps else ""), warnings, where)
            value = gps or screen_text(st.get("value"), warnings, where)
            if not label or not value:
                warnings.append(f"{where}: needs both label and value — dropped")
                continue
            at = float(st.get("at", 0.3))
            s = sg.start + round(at * fps)
            e = sg.end - round(0.15 * fps) if st.get("for") is None else s + round(float(st["for"]) * fps)
            s, e = int(clamp(s, 0, total - 1)), int(clamp(e, 0, total))
            if e - s < 10:
                warnings.append(f"{where}: too short to read — dropped")
                continue
            chip: dict[str, Any] = {"label": label, "value": value, "startFrame": s, "endFrame": e}
            if gps:
                stat_fn = _strava_stat if gps.startswith("strava:") else _gps_stat
                got = stat_fn(project, sg, gps, s, e, fps, errors, where)
                if not got:
                    continue
                chip.update(got)
            stats.append(chip)
    if errors:
        raise ReelsError("plan problems:\n  - " + "\n  - ".join(errors))
    return stats


# --------------------------------------------------------------------------
# GPS (GoPro telemetry): live speed pill, top/avg speed, route map
# --------------------------------------------------------------------------


def _clip_time(sg: Seg, frame: float, fps: int) -> float:
    """Source-clip seconds shown at a reel frame (honours in and speed)."""
    return sg.spec["in"] + (frame - sg.start) / fps * sg.spec["speed"]


def _track(project: Project, sg: Seg, errors: list[str], where: str):
    from .telemetry import load_track

    if sg.kind != "video":
        errors.append(f"{where}: GPS values only work on video segments")
        return None
    tr = load_track(project, sg.asset)
    if tr is None:
        errors.append(f"{where}: {sg.asset['id']} has no GPS telemetry (./reels telemetry {sg.asset['id']}) — only GoPro clips with a fix have it")
    return tr


def _gap_error(project: Project, tr, sg: Seg, a: float, b: float, errors: list[str], where: str) -> bool:
    gaps = tr.gaps(a, b)
    if gaps:
        spans = ", ".join(f"{fmt_ts(x)}-{fmt_ts(y)}" for x, y in gaps[:3])
        errors.append(f"{where}: no good GPS fix in {sg.asset['id']} at {spans} — the speed can't be shown there (./reels telemetry {sg.asset['id']} --start {a:.1f} --end {b:.1f})")
        return True
    return False


def _gps_stat(project: Project, sg: Seg, kind: str, s: int, e: int, fps: int, errors: list[str], where: str) -> dict | None:
    from .telemetry import speed_in, unit_names

    if kind not in GPS_VALUES:
        errors.append(f"{where}: value {kind!r} unknown — GPS values are {', '.join(GPS_VALUES)}")
        return None
    tr = _track(project, sg, errors, where)
    if tr is None:
        return None
    unit = unit_names(project)["speed"]
    if kind == "gps":
        a, b = _clip_time(sg, s, fps), _clip_time(sg, e, fps)
        if _gap_error(project, tr, sg, a, b, errors, where):
            return None
        every = max(1, round(fps / 5))  # ~5 updates a second
        values = []
        for f in range(s, e, every):
            r = tr.at(_clip_time(sg, f + every / 2, fps))
            if r is None:
                errors.append(f"{where}: no GPS fix at {fmt_ts(_clip_time(sg, f, fps))} in {sg.asset['id']}")
                return None
            values.append(str(int(round(speed_in(project, r["speed"])))))
        return {"value": values[0], "unit": unit, "live": {"everyFrames": every, "values": values}}
    a, b = sg.spec["in"], sg.spec["out"]
    if _gap_error(project, tr, sg, a, b, errors, where):
        return None
    r = tr.range(a, b)
    v = r["max"] if kind == "gps:max" else r["avg"]
    return {"value": str(int(round(speed_in(project, v)))), "unit": unit}


# --------------------------------------------------------------------------
# Strava (the linked ride): ride totals, live speed/power/HR, route map
# --------------------------------------------------------------------------


def _strava_act(project: Project, sg: Seg, errors: list[str], where: str) -> dict | None:
    from .strava import linked_activity

    if not (sg.asset.get("strava") or {}).get("activity"):
        errors.append(f"{where}: {sg.asset['id']} has no Strava ride linked (./reels strava list) — fetch that ride and ./reels strava import it")
        return None
    act = linked_activity(project, sg.asset)
    if act is None:
        errors.append(f"{where}: Strava ride {sg.asset['strava']['activity']} isn't in strava/activities/ — import it again")
    return act


def _ride_time(sg: Seg, frame: float, fps: int) -> float:
    """Ride seconds (Strava stream time) shown at a reel frame."""
    t = _clip_time(sg, frame, fps) if sg.kind == "video" else 0.0
    return float(sg.asset["strava"]["offset"]) + t


def _strava_stat(project: Project, sg: Seg, kind: str, s: int, e: int, fps: int, errors: list[str], where: str) -> dict | None:
    from .strava import CLIP_VALUES, LIVE_UNITS, LIVE_VALUES, RIDE_VALUES, live_value, moment_value, ride_value
    from .telemetry import unit_names

    act = _strava_act(project, sg, errors, where)
    if act is None:
        return None
    if kind in RIDE_VALUES:
        got = ride_value(project, act, kind)
    elif kind in CLIP_VALUES:
        a = _ride_time(sg, sg.start, fps) if kind == "strava:mile" else _ride_time(sg, s, fps)
        got = moment_value(project, act, kind, a, max(a + 0.5, _ride_time(sg, e, fps)))
    else:
        assert kind in LIVE_VALUES
        if sg.kind != "video":
            errors.append(f"{where}: live Strava values only work on video segments")
            return None
        every = max(1, round(fps / 5))
        values = []
        for f in range(s, e, every):
            v = live_value(project, act, kind, _ride_time(sg, f + every / 2, fps))
            if v is None:
                errors.append(f"{where}: no Strava {LIVE_VALUES[kind]} at {fmt_ts(_ride_time(sg, f, fps))} into ride {act['id']} (needs its streams, without a gap there)")
                return None
            values.append(v)
        unit = unit_names(project)["speed"] if kind == "strava:speed" else LIVE_UNITS[kind]
        return {"value": values[0], "unit": unit, "live": {"everyFrames": every, "values": values}, "source": "strava"}
    if isinstance(got, str):
        errors.append(f"{where}: {got}")
        return None
    return {"value": got[0], "unit": got[1], "source": "strava"}


def _route_norm(pts: list[tuple[float, float]]):
    import math

    lat0 = math.radians(sum(p[0] for p in pts) / len(pts))

    def xy(lat: float, lon: float) -> tuple[float, float]:
        return lon * math.cos(lat0), lat

    xs, ys = [xy(*p)[0] for p in pts], [xy(*p)[1] for p in pts]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
    cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

    def norm(lat: float, lon: float) -> list[float]:
        x, y = xy(lat, lon)
        return [round(0.5 + (x - cx) / span, 4), round(0.5 - (y - cy) / span, 4)]  # north up

    return norm


def _strava_route(project: Project, sg: Seg, s: int, e: int, fps: int, errors: list[str], where: str) -> dict | None:
    from .strava import Streams, route_points

    act = _strava_act(project, sg, errors, where)
    if act is None:
        return None
    pts, trimmed = route_points(act)
    if len(pts) < 2:
        errors.append(f"{where}: ride {act['id']} has no location stream — import get_activity_streams with --activity {act['id']}")
        return None
    norm = _route_norm([(p[1], p[2]) for p in pts])
    step = max(1, len(pts) // 600)
    path = [norm(p[1], p[2]) for p in pts[::step]] + ([norm(pts[-1][1], pts[-1][2])] if (len(pts) - 1) % step else [])
    st = Streams(act["streams"])
    t0, t1 = pts[0][0], pts[-1][0]
    dots = []
    for f in range(s, e):
        t = _ride_time(sg, f + 0.5, fps)
        loc = st.location(t)
        if loc is None or (trimmed and not t0 <= t <= t1):
            why = "within the trimmed first/last km of a Maine ride" if loc is not None else "outside the ride's GPS"
            errors.append(f"{where}: clip moment {fmt_ts(t)} into ride {act['id']} is {why} — no map here")
            return None
        dots.append(norm(*loc))
    return {"_key": f"strava:{act['id']}", "startFrame": s, "endFrame": e, "paths": [path], "dots": dots}


def _chapters(project: Project, asset: dict) -> list[dict]:
    """GoPro splits one recording into chapters (GS010099, GS020099…); the route spans them all."""
    m = re.match(r"^(G[A-Z])\d\d(\d{4})\.", str(asset.get("originalName") or ""), re.I)
    if not m:
        return [asset]
    sibs = []
    for a in all_assets(project):
        n = re.match(r"^(G[A-Z])(\d\d)(\d{4})\.", str(a.get("originalName") or ""), re.I)
        if n and n.group(1).upper() == m.group(1).upper() and n.group(3) == m.group(2) and a.get("telemetry"):
            sibs.append((n.group(2), a))
    return [a for _, a in sorted(sibs, key=lambda x: x[0])] or [asset]


def _routes(project: Project, segs: list[Seg], total: int, fps: int, limit: int) -> list[dict]:
    """Segments with "route": true get a small map of the recording's GPS track (lines only
    where there was a fix) with a dot at the current position."""
    import math

    from .telemetry import MAX_GAP, load_track

    errors: list[str] = []
    out: list[dict] = []
    for sg in segs:
        if not sg.spec.get("route"):
            continue
        where = f"segment {sg.index + 1} route"
        if sg.spec.get("route") == "strava":
            s, e = sg.start, min(sg.end, limit)
            if e - s < 10:
                continue
            got = _strava_route(project, sg, s, e, fps, errors, where)
            if got:
                prev = out[-1] if out else None
                if prev and prev["_key"] == got["_key"] and s <= prev["endFrame"]:
                    prev["dots"] = prev["dots"][: s - prev["startFrame"]] + got["dots"]
                    prev["endFrame"] = e
                else:
                    out.append(got)
            continue
        if sg.spec.get("route") is not True:
            errors.append(f"{where}: route is true (GoPro GPS) or \"strava\" (the linked ride)")
            continue
        tr = _track(project, sg, errors, where)
        if tr is None:
            continue
        s, e = sg.start, min(sg.end, limit)
        if e - s < 10:
            continue
        if _gap_error(project, tr, sg, _clip_time(sg, s, fps), _clip_time(sg, e, fps), errors, where):
            continue
        chapters = _chapters(project, sg.asset)
        key = "+".join(a["id"] for a in chapters)
        lines: list[list[tuple[float, float]]] = []
        for a in chapters:
            t = tr if a["id"] == sg.asset["id"] else load_track(project, a)
            cur: list[tuple[float, float]] = []
            last = None
            for row in t.samples if t else []:
                if last is not None and row[0] - last > MAX_GAP:  # no line across a gap
                    if len(cur) > 1:
                        lines.append(cur)
                    cur = []
                cur.append((row[1], row[2]))
                last = row[0]
            if len(cur) > 1:
                lines.append(cur)
        pts = [p for ln in lines for p in ln]
        lat0 = math.radians(sum(p[0] for p in pts) / len(pts))

        def xy(lat: float, lon: float) -> tuple[float, float]:
            return lon * math.cos(lat0), lat

        xs, ys = [xy(*p)[0] for p in pts], [xy(*p)[1] for p in pts]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1e-6)
        cx, cy = (max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2

        def norm(lat: float, lon: float) -> list[float]:
            x, y = xy(lat, lon)
            return [round(0.5 + (x - cx) / span, 4), round(0.5 - (y - cy) / span, 4)]  # north up

        paths = []
        for ln in lines:
            step = max(1, len(ln) // 300)
            paths.append([norm(*p) for p in ln[::step] + ([ln[-1]] if (len(ln) - 1) % step else [])])
        dots = []
        for f in range(s, e):
            r = tr.at(_clip_time(sg, f + 0.5, fps))
            if r is None:
                errors.append(f"{where}: no GPS fix at {fmt_ts(_clip_time(sg, f, fps))} in {sg.asset['id']}")
                break
            dots.append(norm(r["lat"], r["lon"]))
        else:
            prev = out[-1] if out else None
            if prev and prev["_key"] == key and s <= prev["endFrame"]:
                prev["dots"] = prev["dots"][: s - prev["startFrame"]] + dots
                prev["endFrame"] = e
            else:
                out.append({"_key": key, "startFrame": s, "endFrame": e, "paths": paths, "dots": dots})
    if errors:
        raise ReelsError("plan problems:\n  - " + "\n  - ".join(errors))
    for r in out:
        del r["_key"]
    return out


def _max_frames(sg: Seg, fps: int) -> int:
    if sg.kind == "video":
        left = sg.spec["out"] if sg.spec.get("_keepOut") else float(sg.asset["probe"]["duration"]) - sg.spec["in"]
        return int(left / sg.spec["speed"] * fps)
    return int(12 * fps)


def _set_frames(sg: Seg, frames: int, fps: int) -> None:
    """Retimes a segment by moving its out (or its in, for the end of a swap-halves loop, whose out is the seam)."""
    sg.frames = frames
    if sg.kind == "video" and sg.spec.get("_keepOut"):
        sg.spec["in"] = round(max(0.0, sg.spec["out"] - frames / fps * sg.spec["speed"]), 3)
    elif sg.kind == "video":
        sg.spec["out"] = round(sg.spec["in"] + frames / fps * sg.spec["speed"], 3)
    else:
        sg.spec["duration"] = round(frames / fps, 3)


def snap_to_beats(segs: list[Seg], beats: list[float], beat_len: float, fps: int, warnings: list[str]) -> list[float]:
    """Moves every cut, and the ending, onto the nearest beat of the music by trimming or
    extending segments (by less than a beat). A fade puts its midpoint on the beat.
    Returns the cut times in seconds."""
    grid = sorted(round(b * fps) for b in beats if b > 0.3)
    reach = beat_len * fps + 1
    cuts: list[float] = []
    start = 0
    for i, sg in enumerate(segs):
        sg.start = start
        nxt = segs[i + 1] if i + 1 < len(segs) else None
        trans = nxt.trans if nxt else 0
        felt = start + sg.frames - trans + trans // 2  # where the change happens on screen
        lo = max(12, sg.trans + 1, trans + 1)
        path = (sg.spec.get("sphere") or {}).get("path")
        if path:  # let a 360 camera move finish
            lo = max(lo, int(path[-1]["at"] * fps) + 2)
        hi = _max_frames(sg, fps)
        new = None
        for g in sorted(grid, key=lambda g: abs(g - felt)):
            if abs(g - felt) > reach:
                break
            if lo <= sg.frames + (g - felt) <= hi:
                new = sg.frames + (g - felt)
                break
        if new is None:
            warnings.append(f"segment {i + 1}: no beat within reach of its {'end' if nxt is None else 'cut'} — left as planned")
        else:
            _set_frames(sg, new, fps)
            cuts.append(round((start + new - trans + trans // 2) / fps, 3))
        start += sg.frames - trans
    return cuts


def snap_loop(segs: list[Seg], beats: list[float], fps: int, warnings: list[str]) -> dict | None:
    """Loop reels: retimes the last segment so the reel is a whole number of bars (4 beats) long,
    counted on the beat grid from the music start, so the song restarts on a bar."""
    grid = [b for b in beats if b >= -0.02]
    if len(grid) < 5:
        return None
    last = segs[-1]
    total = last.end
    lo = max(12, last.trans + 1)
    path = (last.spec.get("sphere") or {}).get("path")
    if path:
        lo = max(lo, int(path[-1]["at"] * fps) + 2)
    hi = _max_frames(last, fps)
    options = [(n, round((grid[4 * n] - grid[0]) * fps)) for n in range(1, (len(grid) - 1) // 4 + 1)]
    for n, frames in sorted(options, key=lambda o: abs(o[1] - total)):
        if abs(frames - total) > 4 * (grid[4] - grid[0]) * fps:
            break
        if lo <= last.frames + frames - total <= hi:
            _set_frames(last, last.frames + frames - total, fps)
            return {"bars": n, "seconds": round(frames / fps, 3), "bpm": round(240 / (grid[4] - grid[0]), 1)}
    warnings.append(f"loop: couldn't make the reel a whole number of bars by retiming segment {last.index + 1} — the song won't restart on a bar")
    return None


def resolve_music(project: Project, plan: dict, segs: list[Seg], fps: int, warnings: list[str], *, loop: bool = False, grid_for: str = "") -> dict | None:
    """`grid_for` names a feature that needs the beat grid (punchOnBeats) — the cached grid is used without cutOnBeat."""
    m = plan.get("music")
    if not m:
        if loop:
            warnings.append("loop: no music — nothing to restart on a bar; the seam is all about the footage")
        return None
    acfg = project.config["audio"]
    m = {"track": m} if isinstance(m, str) else dict(m)
    path, meta = resolve_track(project, m.get("track") or m.get("file"))
    ig = (meta or {}).get("instagram") if m.get("instagram", acfg["attachSongs"]) else None
    start = m.get("start")
    if start is None:
        start = 0 if ig else (meta or {}).get("defaultStart", (meta or {}).get("suggestedStart", 0))
    start = float(parse_time(str(start)) or 0)
    track_len = float((meta or {}).get("duration") or 0)
    if track_len and start > track_len - 5:
        warnings.append(f"music start {start:g}s is past the end of the track — starting at 0")
        start = 0.0
    music: dict[str, Any] = {"path": path, "volume": float(m.get("volume", acfg["musicVolume"])), "start": start, "meta": meta}
    if ig:
        # Instagram mixes the song in at constant levels (no ducking), so talking reels keep it low.
        talking = any(sg.spec["captions"] == "transcript" for sg in segs)
        vols = {}
        for key, default in (
            ("audioVolume", acfg["instagramSongUnderSpeech"] if talking else 100),
            ("videoVolume", 100 if talking else acfg["instagramRideVolume"]),
        ):
            v = m.get(key, default)
            if not isinstance(v, (int, float)) or not 0 <= v <= 100:
                warnings.append(f"music.{key} must be 0–100 — using {default}")
                v = default
            vols[key] = int(round(v))
        music["instagram"] = {"audio": str(ig.get("id") or ig.get("audio")), **vols, "start": start, "manual": start > 0}
        if start > 0:
            warnings.append(
                f"music starts at {fmt_ts(start)[:-2]}, but songs attached by Metricool always play from 0:00 — "
                "this reel will have to be posted by hand so the song can be set to that point in the Instagram app"
            )
    elif meta and meta.get("caution"):
        warnings.append(f"music '{meta['slug']}': {meta['caution']} — Instagram may mute this reel")
    elif meta and not (meta.get("freeUse") or meta.get("rightsOk") or meta.get("instagram")):
        warnings.append(f"music '{meta['slug']}' isn't marked free to use — Instagram often mutes reels with copyrighted songs")
    elif not meta:
        warnings.append(f"music '{path.name}' has no source/credit record — add tracks with ./reels music add")
    grid = None
    if m.get("cutOnBeat"):
        grid = ensure_beats(project, path, meta)
        if grid:
            beats = [b - start for b in grid["beats"] if b >= start - 0.02]
            music["beatSync"] = {"bpm": grid["bpm"], "cuts": snap_to_beats(segs, beats, 60 / grid["bpm"], fps, warnings)}
        else:
            warnings.append("cutOnBeat: couldn't find a steady beat in this track — cuts left as planned")
    elif (loop or grid_for) and meta and meta.get("beats"):
        grid = {"bpm": meta.get("bpm"), "beats": meta["beats"]}  # cached by ./reels music beats
    if grid:
        music["beats"] = [round(b - start, 3) for b in grid["beats"] if b >= start - 0.02]
    if loop:
        if grid:
            before = segs[-1].end / fps
            music["loop"] = snap_loop(segs, music["beats"], fps, warnings)
            sync = music.get("beatSync")
            if music["loop"] and sync:  # the ending moved from the beat to the bar line
                sync["cuts"] = [c for c in sync["cuts"] if c < before - 0.01] + [music["loop"]["seconds"]]
        else:
            warnings.append(f"loop: '{(meta or {}).get('slug') or path.name}' has no beat grid — add \"cutOnBeat\": true (or run ./reels music beats) so the song restarts on a bar")
    if grid_for and not grid:
        warnings.append(f"{grid_for}: needs the music's beat grid — add \"cutOnBeat\": true to music (or run ./reels music beats <track>); no beat punches")
    return music


def build(project: Project, plan: dict) -> dict:
    fps = int(project.cfg("render", "fps"))
    errors: list[str] = []
    loop, on_beats = check_loop(plan, errors), check_beats_punch(plan, errors)
    if errors:
        raise ReelsError("plan problems:\n  - " + "\n  - ".join(errors))
    segs, warnings = normalize(project, plan)
    acfg = project.config["audio"]
    brand = project.config["brand"]
    default_style = brand.get("textStyle") if brand.get("textStyle") in STYLES else "light"
    swap = swap_halves(segs, fps) if loop else (False, "")
    if swap[0]:  # retiming must keep the seam: the last segment gives way at its start
        segs[-1].spec["_keepOut"] = True
    music = resolve_music(project, plan, segs, fps, warnings, loop=loop, grid_for="punchOnBeats" if on_beats else "")
    fit_cutaways(segs, fps, warnings)  # after retiming: segments may have moved their out by up to a beat
    warnings += reuse_warnings(str(plan.get("slug") or ""), segs)
    if loop:
        first = (segs[0].spec.get("_cutaways") or [None])[0]
        last = (segs[-1].spec.get("_cutaways") or [None])[-1]
        if (first and first["start"] == 0) or (last and last["start"] + last["seg"].frames >= segs[-1].frames):
            warnings.append("loop: a cutaway covers the seam, but the seam check measures the clip under it — start the first "
                            "cutaway after 0 s and end the last one before the reel does")  # fmt: skip
    baked = music if music and not music.get("instagram") else None
    total = segs[-1].end
    secs = total / fps
    if secs < 5:
        warnings.append(f"reel is only {secs:.1f}s — 7–30s performs best")
    if secs > 90:
        warnings.append(f"reel is {secs:.0f}s — consider trimming under 60s")

    pages: list[dict] = []
    transcripts: dict[str, dict] = {}
    for sg in segs:
        if sg.kind == "video" and (sg.spec["captions"] == "transcript" or sg.spec.get("bleep")) and sg.asset.get("transcript"):
            aid = sg.asset["id"]
            if aid not in transcripts:
                transcripts[aid] = read_json(project.abs(sg.asset["transcript"]))
        seg_words = (transcripts.get(sg.asset["id"]) or {}).get("words") or []
        if sg.kind == "video":
            resolve_bleeps(sg.spec, seg_words)
            b = sg.spec.get("bleep")
            if b and not sg.spec["_bleeps"]:
                warnings.append(f"segment {sg.index + 1} bleep: nothing to bleep between {sg.spec['in']:g}s and {sg.spec['out']:g}s")
            loud = sg.spec.get("volume") is None or sg.spec["volume"] > 0
            left = unbleeped_swears(sg.spec, seg_words) if loud else []
            if left:
                warnings.append(f"segment {sg.index + 1}: swearing not bleeped ({', '.join(left)}) — add \"bleep\": true or cut around it")
        if sg.spec["captions"] == "transcript":
            aid = sg.asset["id"]
            got = caption_pages_for(sg, masked_words(sg.spec, transcripts[aid].get("words") or []), fps, project.config["captions"])
            if not got:
                warnings.append(f"segment {sg.index + 1}: no transcript words between {sg.spec['in']}s and {sg.spec['out']}s")
            warnings += caption_cuts(sg, transcripts[aid].get("words") or [])
            pages += got

    words = {aid: t.get("words") or [] for aid, t in transcripts.items()}
    if on_beats and not music:
        warnings.append("punchOnBeats: the reel has no music — no beat punches")
    resolve_punches(segs, words, pages, on_beats, (music or {}).get("beats"), fps, warnings)
    if loop and ends_zoomed(segs[-1]) > 1:
        warnings.append(f"loop: segment {len(segs)} ends punched in ({ends_zoomed(segs[-1]):g}×) and the replay starts at 1× — give that punch a \"for\"")

    nat = nat_windows(project, segs, fps)
    seg_props = []
    for sg in segs:
        if sg.kind == "video":
            if sg.spec.get("volume") is not None:
                vol = sg.spec["volume"]
            elif sg.spec["captions"] == "transcript":
                vol = acfg["speechVolume"]
            else:
                vol = acfg["ambientVolume"] if baked else acfg["ambientVolumeNoMusic"]
            if not sg.asset["probe"].get("hasAudio") or not 0.5 <= sg.spec["speed"] <= 2.0:
                vol = 0
            seg_props.append(
                {"kind": "video", "src": f"seg{sg.index}.mp4", "durationInFrames": sg.frames, "transitionInFrames": sg.trans, "volume": round(float(vol), 3)}
            )
            keys = volume_keys(sg, round(float(vol), 3), nat)
            if keys:
                seg_props[-1]["volumeKeys"] = keys
            zoom = punch_keys(sg, fps)
            if zoom:
                seg_props[-1]["punch"] = zoom
            cuts = cutaway_props(sg)
            if cuts:
                seg_props[-1]["cutaways"] = cuts
        elif (sg.spec.get("sphere") or {}).get("path"):
            # 360 photo with a camera move: staged as a short silent clip
            seg_props.append({"kind": "video", "src": f"seg{sg.index}.mp4", "durationInFrames": sg.frames, "transitionInFrames": sg.trans, "volume": 0})
        else:
            seg_props.append(
                {
                    "kind": "photo",
                    "src": f"seg{sg.index}.jpg",
                    "durationInFrames": sg.frames,
                    "transitionInFrames": sg.trans,
                    "volume": 0,
                    "motion": sg.spec["motion"],
                    "fit": sg.spec["fit"],
                    "focusX": sg.spec["focusX"],
                    "focusY": sg.spec["focusY"],
                }
            )

    hook = plan.get("hook")
    hook = {"text": hook} if isinstance(hook, str) else hook
    hook_props = None
    if isinstance(hook, dict) and str(hook.get("text", "")).strip():
        text = screen_text(hook["text"], warnings, "hook")
        words = len(text.split())
        # Long enough to read comfortably: ~0.28 s a word, at least hookSeconds, at most 4.5 s.
        hs = float(hook.get("seconds") or min(4.5, max(float(project.cfg("render", "hookSeconds")), 0.6 + 0.28 * words)))
        pos = hook.get("position") or "top"
        if pos not in HOOK_POSITIONS:
            warnings.append(f"hook: position {pos!r} unknown — using 'top'")
            pos = "top"
        hook_props = {
            "text": text,
            "startFrame": 0,
            "endFrame": int(max(12, min(total, round(hs * fps)))),
            "style": _style(hook.get("style"), default_style, warnings, "hook"),
            "position": pos,
        }
        if words > 12:
            warnings.append("hook is over 12 words — trim it so it reads in a couple of seconds")

    end_props = None
    ec = plan.get("endCard", bool(brand.get("endCard", False)))
    if loop and ec:
        if plan.get("endCard"):
            warnings.append("loop: end card turned off — it would stand between the last frame and the replay")
        ec = False
    if ec:
        ec = ec if isinstance(ec, dict) else {}
        es = float(ec.get("seconds") or project.cfg("render", "endCardSeconds"))
        start = total - round(es * fps)
        hook_end = hook_props["endFrame"] if hook_props else 0
        if start < hook_end + 10:
            warnings.append("reel is too short for an end card — skipped it")
        else:
            handle = screen_text(ec.get("handle") or brand.get("handle"), warnings, "endCard")
            if handle in ("", "@yourhandle"):
                warnings.append("brand.handle in config.json is still @yourhandle")
            cta = screen_text(ec.get("cta", brand.get("endCardCta")) or "", warnings, "endCard")
            style = _style(ec.get("style"), default_style, warnings, "endCard")
            end_props = {"handle": handle, "cta": cta, "startFrame": int(start), "endFrame": int(total), "style": style}
    limit = end_props["startFrame"] if end_props else total

    pages = _trim(pages, limit, 4)
    stats = _trim(_stats(project, segs, total, fps, warnings), limit)
    routes = _routes(project, segs, total, fps, limit)
    if hook_props and hook_props["position"] == "top":
        top = top_offset(hook_props, stats, routes)
        if top:
            hook_props["offsetTop"] = top
    beats = place_beats(_trim(_beats(plan, segs, total, fps, default_style, warnings), limit), hook_props, pages, stats, warnings, routes)
    warnings += retention_warnings(segs, pages, beats, nat, hook_props, fps, total)

    drops = music_drops(plan, total, fps, errors, warnings)
    post = plan.get("post") or {}
    if not isinstance(post, dict):
        errors.append('post must be an object like {"caption": "...", "hashtags": ["#cycling"]}')
        post = {}
    extras = post_extras(project, post, errors)
    if errors:
        raise ReelsError("plan problems:\n  - " + "\n  - ".join(errors))
    if drops and not baked:
        warnings.append("musicDrops: there's no music in this reel's audio" + (" (the song is attached by Instagram)" if music else "") + " — ignored")
        drops = []
    cues = build_cues(project, plan, segs, beats, pages, total, fps, warnings)

    music_props = None
    if baked:
        music_props = {
            "src": f"music{baked['path'].suffix.lower()}",
            "volume": round(baked["volume"], 3),
            "trimBefore": int(round(baked["start"] * fps)),
            "duckRanges": _duck_ranges(pages),
            "duckVolume": acfg["duckVolume"],
            "fadeOutFrames": int(round(1.2 * fps)),
        }
        levels = music_levels(nat, drops)
        quiet_nat(project, segs, nat, fps, warnings)
        if levels:
            music_props["levels"] = levels
        if loop:  # no fade either end, so the replay doesn't dip
            music_props.update(fadeOutFrames=0, fadeInFrames=0)
        if (baked.get("meta") or {}).get("instagram") and levels:
            share = covered_share(levels, total)
            if share > RECOGNITION_MAX:
                warnings.append(
                    f"nat windows and drops take the song down for {share:.0%} of the reel — keep it under {RECOGNITION_MAX:.0%} "
                    "so Instagram still recognizes the trending song"
                )

    if not str(post.get("caption", "")).strip():
        warnings.append("plan has no post.caption yet — add one before queueing")

    props = {
        "width": int(project.cfg("render", "width")),
        "height": int(project.cfg("render", "height")),
        "fps": fps,
        "totalFrames": int(total),
        "brand": {
            "handle": brand.get("handle", ""),
            "accent": brand.get("accent", "#FFE14D"),
            "captionStyle": brand.get("captionStyle") if brand.get("captionStyle") in ("native", "highlight") else "native",
            "textStyle": default_style,
        },
        "segments": seg_props,
        "hook": hook_props,
        "captions": pages,
        "beats": beats,
        "stats": stats,
        **({"routes": routes} if routes else {}),
        "music": music_props,
        **({"sfx": cue_props(cues)} if cues else {}),
        "endCard": end_props,
        "safeArea": False,
    }
    sound = {"nat": nat, "drops": drops, "cues": cues, "cleanNat": bool(acfg["cleanNat"]), "music": bool(music_props)}
    loop_info = {"swap": swap_halves(segs, fps), "bars": (music or {}).get("loop")} if loop else None  # as retimed
    return {"segs": segs, "props": props, "warnings": warnings, "music": music, "sound": sound, "total": int(total), "fps": fps,
            "loop": loop_info, "post": extras}  # fmt: skip


# --------------------------------------------------------------------------
# Staging (ffmpeg pre-cuts) and rendering
# --------------------------------------------------------------------------


def stage_video(
    project: Project, sg: Seg, out: Path, fps: int, *, clean: bool = False, scale: float = 1.0, edges: tuple[bool, bool] = (True, True),
    audio: bool = True,
) -> None:
    """`scale` > 1 stages the clip larger than the reel (punch-ins stay sharp; the template scales it down);
    `edges` = (fade in, fade out) the audio — a loop's seam leaves them off; `audio=False` = picture only (cutaways)."""
    probe, spec = sg.asset["probe"], sg.spec
    src = project.abs(sg.asset["media"])
    tw, th = int(project.cfg("render", "width")), int(project.cfg("render", "height"))
    if scale != 1:
        tw, th = even(tw * scale), even(th * scale)
    w, h = probe["width"], probe["height"]
    speed = spec["speed"]
    pre = hdr_filters(probe) + [f"setpts=(PTS-STARTPTS)/{speed:g}", f"fps={fps}"]
    tail = "setsar=1,format=yuv420p,tpad=stop_mode=clone:stop_duration=2[v]"

    if spec.get("sphere"):
        # 360: rebuild the sphere, then point a virtual camera (fixed, or moving along a path)
        src_graph, pad, inproj = source_graph(probe)
        cam = camera_filter(spec["sphere"], inproj, tw, th, fps, sg.frames, out.with_name(f"{out.stem}.cam.cmd"))
        graph = f"{src_graph};[{pad}]setpts=(PTS-STARTPTS)/{speed:g},fps={fps},{cam},{tail}"
    elif spec["frame"] == "crop":
        zoom = spec["zoom"]
        if w / h > tw / th:
            sh = even(th * zoom)
            sw = even(w * sh / h)
        else:
            sw = even(tw * zoom)
            sh = even(h * sw / w)
        sw, sh = max(sw, tw), max(sh, th)
        x = int(clamp(round(spec["focusX"] * sw - tw / 2), 0, sw - tw))
        y = int(clamp(round(spec["focusY"] * sh - th / 2), 0, sh - th))
        graph = f"[0:v]{','.join(pre + [f'scale={sw}:{sh}:flags=lanczos', f'crop={tw}:{th}:{x}:{y}'])},{tail}"
    elif spec["frame"] == "blurfill":
        bw, bh = tw // 4, th // 4
        graph = (
            f"[0:v]{','.join(pre)},split=2[a][b];"
            f"[a]scale={bw}:{bh}:force_original_aspect_ratio=increase,crop={bw}:{bh},boxblur=10:2,"
            f"eq=brightness=-0.08:saturation=1.15,scale={tw}:{th}[bg];"
            f"[b]scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,setsar=1[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,{tail}"
        )
    else:
        graph = (
            f"[0:v]{','.join(pre)},scale={tw}:{th}:force_original_aspect_ratio=decrease:flags=lanczos,"
            f"pad={tw}:{th}:(ow-iw)/2:(oh-ih)/2:color=black,{tail}"
        )

    use_audio = audio and bool(probe.get("hasAudio")) and 0.5 <= speed <= 2.0
    if use_audio:
        # short fades at both edges so hard cuts don't pop; `clean` = gentle highpass + denoise against wind
        dur = sg.frames / fps
        mute = mute_filter(spec)  # bleeps: silence the source words before retiming
        af = ["asetpts=PTS-STARTPTS"] + ([mute] if mute else []) + [f"atempo={speed:g},aresample=48000"] + ([CLEAN_AF] if clean else [])
        fades = [f"afade=t=in:st=0:d={EDGE_FADE:g}"] if edges[0] else []
        fades += ["apad"] + ([f"afade=t=out:st={max(0.0, dur - EDGE_FADE):.4f}:d={EDGE_FADE:g}"] if edges[1] else [])
        tone = tone_graph(spec, dur)
        if tone:
            af.append("aformat=sample_rates=48000:channel_layouts=stereo")
            graph += f";[0:a:0]{','.join(af)}[am];{tone};[am][bt]amix=inputs=2:duration=first:normalize=0,{','.join(fades)}[a]"
        else:
            graph += f";[0:a:0]{','.join(af + fades)}[a]"
    cmd: list[Any] = ["ffmpeg", "-v", "error", "-y", "-ss", f"{spec['in']:.3f}", "-t", f"{spec['out'] - spec['in']:.3f}", "-i", src]
    cmd += ["-filter_complex", graph, "-map", "[v]"]
    cmd += ["-map", "[a]", "-c:a", "aac", "-b:a", "192k", "-ac", "2"] if use_audio else ["-an"]
    cmd += ["-frames:v", sg.frames, "-t", f"{sg.frames / fps:.4f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16"]
    cmd += ["-g", fps, "-pix_fmt", "yuv420p", "-movflags", "+faststart", out]
    run(cmd, cwd=out.parent)


def stage_photo_pan(project: Project, sg: Seg, out: Path, fps: int) -> None:
    """A 360 photo with a camera path becomes a short silent clip."""
    tw, th = int(project.cfg("render", "width")), int(project.cfg("render", "height"))
    src = project.abs(sg.asset["media"])
    cam = camera_filter(sg.spec["sphere"], "e", tw, th, fps, sg.frames, out.with_name(f"{out.stem}.cam.cmd"))
    graph = f"[0:v]format=yuv420p,fps={fps},{cam},setsar=1,format=yuv420p[v]"
    run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", fps, "-t", f"{sg.frames / fps + 0.5:.3f}", "-i", src,
         "-filter_complex", graph, "-map", "[v]", "-an", "-frames:v", sg.frames, "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "16", "-g", fps, "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], cwd=out.parent)  # fmt: skip


def stage_photo(project: Project, sg: Seg, out: Path) -> None:
    tw, th = int(project.cfg("render", "width")), int(project.cfg("render", "height"))
    if sg.spec.get("sphere"):
        cam = sg.spec["sphere"]
        # 20% headroom so the template's slow zoom/pan never shows an edge
        photo_view(project.abs(sg.asset["media"]), out, cam["view"], even(tw * 1.2), even(th * 1.2), cam["projection"])
        return
    im = open_image(project.abs(sg.asset["media"]))
    w, h = im.size
    spec = sg.spec
    scale = (max(tw / w, th / h) if spec["fit"] == "cover" else min(tw / w, th / h)) * 1.2
    nw, nh = max(2, round(w * scale)), max(2, round(h * scale))
    cap = 2600 / max(nw, nh)
    if cap < 1:
        nw, nh = round(nw * cap), round(nh * cap)
    im.resize((nw, nh), Image.LANCZOS).save(out, quality=90)
    if spec["fit"] == "blurfill":
        small = ImageOps.fit(im, (tw // 4, th // 4), Image.LANCZOS, centering=(spec["focusX"], spec["focusY"]))
        bg = ImageEnhance.Color(ImageEnhance.Brightness(small.filter(ImageFilter.GaussianBlur(9))).enhance(0.6)).enhance(1.15)
        bg.resize((tw, th), Image.BICUBIC).save(out.with_name(out.stem + ".bg.jpg"), quality=88)


def stage(project: Project, built: dict, staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    clean_nat = built["sound"]["cleanNat"]
    tw, th = int(project.cfg("render", "width")), int(project.cfg("render", "height"))
    most = float(project.cfg("render", "punchStageScale") or 1)
    segs = built["segs"]
    seam = bool(built.get("loop") and built["loop"]["swap"][0])  # ride audio runs on across the seam: no edge fades there
    for sg in segs:
        if sg.kind == "video":
            clean = bool(sg.spec.get("clean") or (clean_nat and sg.spec.get("nat")))
            edges = (not (seam and sg.index == 0), not (seam and sg.index == len(segs) - 1))
            k = stage_scale(sg, tw, th, most)
            stage_video(project, sg, staging / f"seg{sg.index}.mp4", built["fps"], clean=clean, scale=k, edges=edges)
        elif (sg.spec.get("sphere") or {}).get("path"):
            stage_photo_pan(project, sg, staging / f"seg{sg.index}.mp4", built["fps"])
        else:
            stage_photo(project, sg, staging / f"seg{sg.index}.jpg")
        for k, c in enumerate(sg.spec.get("_cutaways") or []):  # silent B-roll over the segment
            cs, name = c["seg"], cutaway_name(sg, k)
            if cs.kind == "video":
                stage_video(project, cs, staging / f"{name}.mp4", built["fps"], audio=False)
            elif (cs.spec.get("sphere") or {}).get("path"):
                stage_photo_pan(project, cs, staging / f"{name}.mp4", built["fps"])
            else:
                stage_photo(project, cs, staging / f"{name}.jpg")
    if built["props"]["music"]:
        mp: Path = built["music"]["path"]
        shutil.copy2(mp, staging / f"music{mp.suffix.lower()}")
    for cue in built["sound"]["cues"]:
        dest = staging / "sfx" / cue["path"].name
        if not dest.exists():
            dest.parent.mkdir(exist_ok=True)
            shutil.copy2(cue["path"], dest)


def run_remotion(project: Project, props_path: Path, public_dir: Path, out_tmp: Path, log_path: Path) -> None:
    rdir = project.remotion_dir
    binary = rdir / "node_modules" / ".bin" / "remotion"
    if not binary.exists():
        raise ReelsError("Remotion isn't installed yet — run ./setup.sh (or: cd remotion && npm install)")
    cmd = [
        str(binary), "render", "src/index.ts", "CyclingReel", str(out_tmp),
        f"--props={props_path}", f"--public-dir={public_dir}",
        "--codec=h264", "--crf=16", "--audio-bitrate=192k", "--pixel-format=yuv420p",
        "--color-space=bt709", "--enforce-audio-track", "--log=info",
    ]  # fmt: skip
    if project.cfg("render", "concurrency"):
        cmd.append(f"--concurrency={project.cfg('render', 'concurrency')}")
    browser = os.environ.get("REELS_BROWSER") or project.cfg("render", "browserExecutable")
    if browser:
        cmd.append(f"--browser-executable={browser}")
    with open(log_path, "w", encoding="utf-8") as lf:
        proc = subprocess.run(cmd, cwd=rdir, stdout=lf, stderr=subprocess.STDOUT)
    if proc.returncode != 0 or not out_tmp.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        raise ReelsError(f"Remotion render failed (full log: {project.rel(log_path)}):\n{tail}")


def _double_rate(rate: str) -> str:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([kKmM]?)", str(rate))
    if not m:
        return "40M"
    return f"{float(m.group(1)) * 2:g}{m.group(2)}"


def measure_loudness(src: Path, target: float) -> dict | None:
    """First pass of ffmpeg's loudnorm. None when the track is silent or unreadable."""
    proc = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-i", src, "-vn", "-af", f"loudnorm=I={target}:TP=-1.5:LRA=11:print_format=json", "-f", "null", "-"],
        check=False,
    )
    m = re.search(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        if float(data["input_i"]) < -70:
            return None
    except (ValueError, KeyError):
        return None
    return data


RIDE_ONLY_LIMIT_DB = 6.0  # ride-only mixes: at most this much peak limiting to get louder


def ride_only_args(project: Project, src: Path, max_boost: float) -> list[str]:
    """Reels with no music: one linear gain toward the platform loudness plus a peak limiter,
    instead of loudnorm's dynamic mode (which flattens the ride sounds and pumps wind noise up).
    The gain is capped at `max_boost` dB and at RIDE_ONLY_LIMIT_DB of limiting on the peaks."""
    target = project.config["render"].get("loudness")
    m = measure_loudness(src, float(target)) if target is not None else None
    if not m:
        return []
    i, tp = float(m["input_i"]), float(m["input_tp"])
    gain = min(float(target) - i, max_boost, -1.5 - tp + RIDE_ONLY_LIMIT_DB)
    log(f"  ride-only mix at {i:.1f} LUFS: {gain:+.1f} dB linear gain, peaks limited at -2 dBFS (no dynamic loudnorm)")
    return ["-af", f"volume={gain:.2f}dB,alimiter=limit=0.794:attack=2:release=60:level=false:latency=true"]


def loudnorm_args(project: Project, src: Path) -> list[str]:
    """Second loudnorm pass, aimed at the platform loudness (-14 LUFS by default)."""
    target = project.config["render"].get("loudness")
    m = measure_loudness(src, float(target)) if target is not None else None
    if not m:
        return []
    return [
        "-af",
        f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={m['input_i']}:measured_TP={m['input_tp']}"
        f":measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true",
    ]


def final_encode(project: Project, src: Path, out: Path, max_boost: float | None = None) -> None:
    """Instagram Reels spec: H.264 High, yuv420p BT.709, AAC 48 kHz <=128 kbps, faststart,
    with audio normalised to the platform loudness (-14 LUFS by default); `max_boost` = a ride-only mix."""
    rc = project.config["render"]
    af = ride_only_args(project, src, max_boost) if max_boost is not None else loudnorm_args(project, src)
    cmd: list[Any] = ["ffmpeg", "-v", "error", "-y", "-i", src, *af]
    cmd += [
        "-c:v", "libx264", "-preset", "slow", "-crf", str(rc["crf"]),
        "-maxrate", rc["maxBitrate"], "-bufsize", _double_rate(rc["maxBitrate"]),
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-g", "60",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709", "-color_range", "tv",
        "-c:a", "aac", "-b:a", rc["audioBitrate"], "-ar", "48000", "-ac", "2",
        "-movflags", "+faststart", out,
    ]  # fmt: skip
    run(cmd)


def mix_preview(project: Project, reel: Path, track: Path, ig: dict, duration: float, out: Path, *, fade: bool = True) -> None:
    """The reel as it should sound once Instagram attaches the song: the upload's own audio at
    videoVolume plus the song from `start` at audioVolume, constant levels (Instagram doesn't duck)."""
    v, a = ig["videoVolume"] / 100, ig["audioVolume"] / 100
    tail = f",afade=t=out:st={max(0.0, duration - 0.3):.3f}:d=0.3" if fade else ""
    graph = (
        f"[0:a]volume={v:.3f}[v];[1:a]atrim=start={ig['start']:.3f},asetpts=PTS-STARTPTS,volume={a:.3f}"
        f"{tail}[m];[v][m]amix=inputs=2:duration=first:normalize=0[a]"
    )
    mixed = out.with_name(out.stem + ".mix.mp4")
    run(["ffmpeg", "-v", "error", "-y", "-i", reel, "-i", track, "-filter_complex", graph, "-map", "0:v", "-map", "[a]",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000", mixed])  # fmt: skip
    rc = project.config["render"]
    run(["ffmpeg", "-v", "error", "-y", "-i", mixed, *loudnorm_args(project, mixed), "-c:v", "copy",
         "-c:a", "aac", "-b:a", rc["audioBitrate"], "-ar", "48000", "-ac", "2", "-movflags", "+faststart", out])  # fmt: skip
    mixed.unlink(missing_ok=True)


def make_preview(reel: Path, duration: float, out: Path, title: str) -> None:
    with tempfile.TemporaryDirectory() as td:
        tiles = []
        for i, f in enumerate((0.04, 0.2, 0.4, 0.6, 0.8, 0.97)):
            t = duration * f
            fp = Path(td) / f"p{i}.jpg"
            extract_frame(reel, t, fp, width=720)
            with Image.open(fp) as im:
                tiles.append((fmt_ts(t), im.convert("RGB")))
        build_sheet(tiles, out, title, cols=3, tile_w=360)


def mark_usage(project: Project, slug: str, segs: list[Seg]) -> None:
    """Records which footage this reel shows (segments and cutaways), so other reels don't repeat it."""
    by_asset: dict[str, list[Seg]] = {}
    for _, sg, _ in visuals(segs):
        by_asset.setdefault(sg.asset["id"], []).append(sg)
    for a in all_assets(project):
        used = a.get("used") or []
        if a["id"] not in by_asset and not any(u.get("plan") == slug for u in used):
            continue
        used = [u for u in used if u.get("plan") != slug]
        for sg in by_asset.get(a["id"], []):
            entry: dict[str, Any] = {"plan": slug, "at": now_iso()}
            if sg.kind == "video":
                entry.update({"in": sg.spec["in"], "out": sg.spec["out"]})
            used.append(entry)
        a["used"] = used
        save_asset(project, a)


def render_plan(project: Project, plan_path: str | Path, *, keep_staging: bool = False) -> dict:
    require("ffmpeg")
    require("ffprobe")
    plan = load_plan(project, plan_path)
    built = build(project, plan)
    for w in built["warnings"]:
        warn(w)
    slug, fps, total = plan["slug"], built["fps"], built["total"]
    staging = project.staging_dir / slug
    out_dir = project.out_dir / slug
    out_dir.mkdir(parents=True, exist_ok=True)

    log(f"rendering {slug}: {total / fps:.1f}s from {len(built['segs'])} segment(s)…")
    stage(project, built, staging)
    props_path = staging / "props.json"
    write_json(props_path, built["props"])
    tmp = staging / "render.mp4"
    run_remotion(project, props_path, staging, tmp, out_dir / "render.log")
    seam = None
    if built["loop"]:
        seam = seam_score(staged_seam(staging, built["segs"]))

    reel = out_dir / "reel.mp4"
    boost = project.cfg("audio", "rideOnlyMaxBoost")  # no song at all: don't drag wind noise up to full loudness
    final_encode(project, tmp, reel, float(boost) if boost is not None and built["music"] is None else None)
    info = ffprobe_json(reel)
    duration = float((info.get("format") or {}).get("duration") or total / fps)
    music = built["music"]
    ig = (music or {}).get("instagram")
    preview_video = out_dir / "preview.mp4"
    preview_video.unlink(missing_ok=True)
    if ig:  # reel.mp4 is the clean upload; preview.mp4 is what it'll sound like on Instagram
        mix_preview(project, reel, music["path"], ig, duration, preview_video, fade=not built["loop"])

    hook = built["props"]["hook"]
    cover_t = plan.get("cover")
    if cover_t is None:
        cover_t = (hook["endFrame"] - 4) / fps if hook else min(1.0, duration / 2)
    cover_t = float(clamp(float(cover_t), 0.0, max(0.0, duration - 1 / fps)))
    extract_frame(reel, cover_t, out_dir / "cover.jpg")
    make_preview(reel, duration, out_dir / "preview.jpg", f"{slug}  ·  {duration:.1f}s")

    plan_copy = {k: v for k, v in plan.items() if not k.startswith("_")}
    write_json(out_dir / "plan.json", plan_copy)

    post_path = out_dir / "post.json"
    prev = read_json(post_path) if post_path.exists() else {}
    if prev.get("status") in ("scheduled", "posted"):
        mid = f" (Metricool post {prev['metricoolId']}, kept in post.json — update that post rather than scheduling a second one)" if prev.get("metricoolId") else ""
        warn(f"{slug} was already {prev['status']} — it needs approval again, then re-schedule it so the new render is the one that posts{mid}")
    p = plan.get("post") or {}
    hashtags = [h if str(h).startswith("#") else f"#{h}" for h in (p.get("hashtags") or [])]
    post = {
        "slug": slug,
        "status": "rendered",
        "renderedAt": now_iso(),
        "file": project.rel(reel),
        "cover": project.rel(out_dir / "cover.jpg"),
        "coverMs": int(round(cover_t * 1000)),  # the same frame, for the scheduler's cover setting (Metricool videoCoverMilliseconds)
        "preview": project.rel(out_dir / "preview.jpg"),
        "duration": round(duration, 2),
        "sizeMB": round(reel.stat().st_size / 1e6, 1),
        "caption": str(p.get("caption", "")).strip(),
        "hashtags": hashtags,
        "altText": str(p.get("altText", "")).strip(),
        "firstComment": str(p.get("firstComment", "")).strip(),
        **{k: v for k, v in (built.get("post") or {}).items() if v},  # collaborators, peopleTags
        "postDate": plan.get("postDate"),
        "scheduledFor": prev.get("scheduledFor"),
        "timezone": prev.get("timezone"),
        "mediaUrl": None,
        "metricoolId": None,
        "segments": [
            {"asset": v.asset["id"], **({"in": v.spec["in"], "out": v.spec["out"]} if v.kind == "video" else {}),
             **({"cutaway": True} if is_cut else {})}
            for _, v, is_cut in visuals(built["segs"])
        ],  # fmt: skip
    }
    carry_over(prev, post)  # history, the Metricool post id and the live reel's id survive a re-render
    if built["loop"]:
        post["loop"] = {"swapHalves": built["loop"]["swap"][0], **({"seamSsim": seam["seam"], "stepSsim": seam["step"]} if seam else {})}
    if music:
        meta = music.get("meta") or {}
        post["music"] = {"track": meta.get("slug") or music["path"].stem, "title": meta.get("title"), "url": meta.get("url")}
        if ig:
            post["instagramAudio"] = {**ig, "title": meta.get("title"), "artist": meta.get("artist")}
            post["previewVideo"] = project.rel(preview_video)
        else:
            post["musicCredit"] = meta.get("credit")
    write_json(post_path, post)
    (out_dir / "caption.txt").write_text(caption_block(post).strip() + "\n", encoding="utf-8")
    mark_usage(project, slug, built["segs"])
    release_usage(project, slug)  # a re-render may have changed the song
    if music:
        record_usage(project, music.get("meta"), slug)

    if not (keep_staging or project.cfg("render", "keepStaging")):
        shutil.rmtree(staging, ignore_errors=True)
    log(f"✓ {project.rel(reel)}  ({duration:.1f}s, {post['sizeMB']} MB)  preview: {post['preview']}")
    if built["loop"]:
        line, advice = describe_seam(seam, built["loop"]["swap"])
        log(f"  loop seam: {line}")
        for a in advice:
            warn(a)
    if ig:
        log(f"  watch {post['previewVideo']} — reel.mp4 is the upload without the song; Instagram adds it when it's scheduled")
    return post


def check_plan(project: Project, plan_path: str | Path) -> dict:
    plan = load_plan(project, plan_path)
    built = build(project, plan)
    fps, total = built["fps"], built["total"]
    props = built["props"]
    music = built["music"]
    music_txt = "none"
    if music:
        music_txt = f"{(music.get('meta') or {}).get('slug') or music['path'].name} from {fmt_ts(music['start'])[:-2]}"
        ig = music.get("instagram")
        if ig:
            how = "posted by hand" if ig["manual"] else "attached when scheduled"
            music_txt += f" (Instagram audio, {how}: song {ig['audioVolume']} / ride audio {ig['videoVolume']})"
    log(f"{plan['slug']}  ·  {total / fps:.1f}s ({total} frames)  ·  {len(built['segs'])} segments  ·  music: {music_txt}")
    sync = (music or {}).get("beatSync")
    if sync:
        log(f"  cuts on the beat ({sync['bpm']:g} bpm): " + ", ".join(f"{c:.2f}s" for c in sync["cuts"]))
    for sg in built["segs"]:
        cam = sg.spec.get("sphere")
        cam_txt = ""
        if cam:
            v = cam["view"]
            if cam["projection"] == "tiny-planet":
                cam_txt = f"360 tiny planet (yaw {v['yaw']:g})"
            else:
                cam_txt = f"360 yaw {v['yaw']:g} pitch {v['pitch']:g} fov {v['fov']:g}"
            if cam["path"]:
                cam_txt += " → path " + " → ".join(f"{k['at']:g}s:{k['yaw']:g}°" for k in cam["path"])
        if sg.kind == "video":
            src = f"{fmt_ts(sg.spec['in'])}-{fmt_ts(sg.spec['out'])}"
            frame = cam_txt or (sg.spec["frame"] + (f"@{sg.spec['focusX']:.2f}" if sg.spec["frame"] == "crop" else ""))
            extra = (f" x{sg.spec['speed']:g}" if sg.spec["speed"] != 1 else "") + (" captions" if sg.spec["captions"] == "transcript" else "")
            if sg.spec.get("_bleeps"):
                extra += f" bleep({sg.spec['bleep']['sound']}): " + ", ".join(f"{s:.2f}-{e:.2f}s" for s, e in sg.spec["_bleeps"])
        else:
            src, frame, extra = "photo", cam_txt or f"{sg.spec['fit']}/{sg.spec['motion']}", ""
        log(f"  {sg.index + 1:>2}  {fmt_ts(sg.start / fps):>7}  {sg.frames / fps:4.1f}s  {sg.asset['id']:<34} {src:<17} {frame}{extra}")
        for c in sg.spec.get("_cutaways") or []:
            cs = c["seg"]
            a, b = (sg.start + c["start"]) / fps, (sg.start + c["start"] + cs.frames) / fps
            what = f"{fmt_ts(cs.spec['in'])}-{fmt_ts(cs.spec['out'])}" if cs.kind == "video" else "photo"
            log(f"        cutaway {a:5.2f}-{b:.2f}s  {cs.asset['id']:<34} {what:<17} (picture only — the segment's audio keeps going)")
    if props["hook"]:
        log(f"  hook: \"{props['hook']['text']}\" until {props['hook']['endFrame'] / fps:.1f}s")
    log(f"  captions: {len(props['captions'])} pages  ·  text beats: {len(props['beats'])}  ·  stats: {len(props['stats'])}")
    for st in props["stats"]:
        if st.get("unit") is not None and (st.get("live") or st.get("source") or st["value"].isdigit()):  # data pill: say what it will show
            if st.get("live"):
                vals = [int(v) for v in st["live"]["values"]]
                shown = f"{min(vals)}–{max(vals)}" if min(vals) != max(vals) else str(vals[0])
            else:
                shown = st["value"]
            src = "strava" if st.get("source") == "strava" else "gps"
            log(f"  {src}: \"{st['label']}\" {shown} {st['unit']}{' (live)' if st.get('live') else ''}  {st['startFrame'] / fps:.1f}-{st['endFrame'] / fps:.1f}s")
    for r in props.get("routes") or []:
        log(f"  route map {r['startFrame'] / fps:.1f}-{r['endFrame'] / fps:.1f}s ({len(r['paths'])} line(s))")
    for sg in built["segs"]:
        for p in sg.spec.get("_punches") or []:
            until = "the cut" if p["end"] is None else f"{p['end'] / fps:.2f}s"
            log(f"  punch   {p['start'] / fps:6.2f}s  {p['scale']:g}× until {until}  (segment {sg.index + 1}, {p['why']})")
    lp = built["loop"]
    if lp:
        bars = lp["bars"]
        if bars:
            log(f"  loop: {bars['seconds']:.2f}s = {bars['bars']} bar{'s' if bars['bars'] > 1 else ''} at {bars['bpm']:g} bpm — the song restarts on a bar; no fades, no end card")
        else:
            log("  loop: no fades, no end card (length not snapped to bars)")
        score, swap = check_seam(project, built["segs"], fps)
        line, advice = describe_seam(score, swap)
        log(f"  seam: {line}")
        built["warnings"] += advice
    if props["endCard"]:
        log(f"  end card: {props['endCard']['handle']} from {props['endCard']['startFrame'] / fps:.1f}s")
    sheet = cue_sheet(built["sound"], fps, built["segs"])
    if sheet:
        log("  sound:")
        for line in sheet:
            log(line)
    for w in built["warnings"]:
        warn(w)
    return built


def discard(project: Project, slug: str) -> None:
    out_dir = project.out_dir / slug
    post_path = out_dir / "post.json"
    if post_path.exists():
        post = read_json(post_path)
        if post.get("status") in ("scheduled", "posted"):
            warn(f"{slug} is {post['status']} — delete it in Metricool/Instagram too")
    for a in all_assets(project):
        used = a.get("used") or []
        if any(u.get("plan") == slug for u in used):
            a["used"] = [u for u in used if u.get("plan") != slug]
            save_asset(project, a)
    release_usage(project, slug)
    if out_dir.exists():
        shutil.rmtree(out_dir)
    log(f"discarded {slug}; its clips are available again")
