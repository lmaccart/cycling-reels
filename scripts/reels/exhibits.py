"""Exhibits: a study screenshot, diagram or chart shown over the footage while the rider talks
and points at it.

The one exception to "every visual is the user's own footage": images the USER drops into
exhibits/ (never fetched or generated here), shown only as an overlay — never as a segment,
cutaway or background. Charts are drawn by the template from numbers the user gave or from
their own Strava rides.

Three ways to place one, all on the same plan entry:
  A  placed      "x"/"y"/"width" — a spot on the frame (film yourself pointing at that spot)
  B  anchored    "anchor": "hand" — found from the pointing fingertip in the clip (vision.py)
  C  depth       "depth": "behind" — the rider is cut out and laid back over it, so the hand
                 and head pass in front (vision.py)
Hook, text beats and captions move out of an exhibit's way (make_room)."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps

from . import vision
from .config import Project
from .images import open_image
from .util import ReelsError, clamp, fmt_ts, log

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".heic", ".heif", ".gif", ".tif", ".tiff", ".bmp")
STYLES = ("card", "cutout")
DEPTHS = ("front", "behind")
MOVES = ("pop", "fade", "slide", "none")
SIDES = ("auto", "left", "right", "above", "below")
KEYS = {"at", "for", "image", "chart", "style", "width", "x", "y", "tilt", "crop", "anchor", "side", "gap", "point", "follow",
        "depth", "enter", "exit", "shadow", "background", "padding", "safeArea"}  # fmt: skip
# Where an exhibit may sit on the 1080x1920 frame (px): under Instagram's top bar, above its
# caption/buttons. Text keeps PAD clear of it.
SAFE_LEFT, SAFE_RIGHT, SAFE_TOP, SAFE_BOTTOM = 40, 1040, 230, 1500
# The like/comment/share column on the right (Instagram and TikTok): an exhibit reaching below
# BUTTONS_TOP stays left of it. Same numbers as the Studio guide in Overlays.tsx.
BUTTONS_TOP, BUTTONS_LEFT = 1000, 950
PAD = 28
MIN_FRAMES = 15
TRACK_SCALE = 0.5  # the clip is looked at at half size: plenty for a hand, and quick
# Default places of on-screen text (px), matching the template's components.
TOP_TEXT, LOWER_TEXT_BOTTOM, CAPTION_BOTTOM = 420, 1320, 1360
LINE = 78  # one line of on-screen text incl. its box
CHARS_PER_LINE = 26

STRAVA_SERIES = {  # chart "data" -> (stream, default smoothing in s, title)
    "strava:power": ("watts", 30, "power"),
    "strava:hr": ("heart_rate", 5, "heart rate"),
    "strava:speed": ("velocity_smooth", 10, "speed"),
    "strava:cadence": ("cadence", 10, "cadence"),
}
CHART_TYPES = ("line", "bar")
CHART_THEMES = ("light", "dark")
CHART_POINTS = 160  # a line on a phone needs no more


# --------------------------------------------------------------------------
# The exhibits/ folder
# --------------------------------------------------------------------------


def files(project: Project) -> list[Path]:
    d = project.exhibits_dir
    if not d.is_dir():
        return []
    return sorted(p for p in d.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith("."))


def resolve(project: Project, name: Any) -> Path:
    """An image in exhibits/ by file name, name without extension, or a prefix only one file has."""
    name = str(name or "").strip()
    if not name:
        raise ReelsError('needs "image": "<file in exhibits/>" or a "chart"')
    root = project.exhibits_dir.resolve()
    direct = (root / name).resolve()
    if root in direct.parents and direct.is_file() and direct.suffix.lower() in IMAGE_EXTS:
        return direct
    have = files(project)
    low = name.lower()
    for test in (lambda p: p.name.lower() == low, lambda p: p.stem.lower() == low, lambda p: p.name.lower().startswith(low)):
        hits = [p for p in have if test(p)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise ReelsError(f"{name!r} matches several exhibits ({', '.join(p.name for p in hits[:4])}) — use the full file name")
    raise ReelsError(f"no image {name!r} in exhibits/ — drop the screenshot or diagram there (./reels exhibits lists what's in it)")


def load(path: Path) -> Image.Image:
    """Upright image, RGBA when it has transparency (a cut-out), else RGB."""
    if path.suffix.lower() in (".heic", ".heif"):
        return open_image(path)
    try:
        with Image.open(path) as im:
            im = ImageOps.exif_transpose(im)
            alpha = im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info
            return im.convert("RGBA" if alpha else "RGB")
    except Exception as e:  # noqa: BLE001 - Pillow raises many types
        raise ReelsError(f"{path.name}: can't open image ({e})") from e


def see_through(im: Image.Image) -> bool:
    return im.mode == "RGBA" and im.getchannel("A").getextrema()[0] < 250


def add(project: Project, src: str | Path, name: str | None = None) -> Path:
    """Copies an image the user has (a screenshot, a diagram) into exhibits/."""
    src = Path(src).expanduser()
    if not src.is_file():
        raise ReelsError(f"no such file: {src}")
    if src.suffix.lower() not in IMAGE_EXTS:
        raise ReelsError(f"{src.name} isn't an image ({', '.join(IMAGE_EXTS)})")
    load(src)  # opens, or says why not
    stem = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (name or src.stem).strip().lower()).strip("-") or "exhibit"
    dest = project.exhibits_dir / f"{stem}{src.suffix.lower()}"
    if dest.exists():
        raise ReelsError(f"exhibits/{dest.name} already exists — pick another --name")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def print_list(project: Project) -> None:
    from .util import read_json

    have = files(project)
    if not have:
        log("exhibits/ is empty — drop study screenshots, diagrams or cut-out PNGs there (or ./reels exhibits add <file>)")
        return
    used: dict[str, list[str]] = {}
    for plan_path in sorted(project.plans_dir.glob("*.json")):
        try:
            plan = read_json(plan_path)
        except Exception:  # noqa: BLE001
            continue
        for ex in _raw(plan):
            if isinstance(ex[1], dict) and ex[1].get("image"):
                try:
                    used.setdefault(resolve(project, ex[1]["image"]).name, []).append(plan_path.stem)
                except ReelsError:
                    pass
    for p in have:
        try:
            im = load(p)
            what = f"{im.width}x{im.height}{'  transparent (cut-out)' if see_through(im) else ''}"
        except ReelsError as e:
            what = str(e)
        plans = sorted(set(used.get(p.name, [])))
        log(f"  {p.relative_to(project.exhibits_dir)!s:<40} {what}" + (f"   in: {', '.join(plans)}" if plans else ""))


def _raw(plan: dict) -> list[tuple[int | None, Any]]:
    """(segment index or None for plan-level, raw entry) for every exhibit in a plan."""
    out: list[tuple[int | None, Any]] = []
    for i, s in enumerate(plan.get("segments") or []):
        if isinstance(s, dict):
            out += [(i, e) for e in (s.get("exhibits") or [] if isinstance(s.get("exhibits"), list) else [s.get("exhibits")]) if e is not None]
    top = plan.get("exhibits")
    out += [(None, e) for e in (top or [] if isinstance(top, list) else [top]) if e is not None]
    return out


# --------------------------------------------------------------------------
# Charts (drawn by the template)
# --------------------------------------------------------------------------


def _ticks(lo: float, hi: float, want: int = 4) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / want
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    out = []
    v = first
    while v <= hi + step * 1e-6:
        out.append(round(v, 10))
        v += step
    return out


def _fmt(v: float) -> str:
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-6 else f"{v:g}"


def _strava_series(project: Project, c: dict, sg: Any, errors: list[str], where: str) -> tuple[list[float], list[float], str, str] | None:
    """(minutes into the ride, values, unit, default title) from the user's own imported ride."""
    from .strava import LIVE_UNITS, Streams, linked_activity, load_activity
    from .telemetry import speed_in, unit_names

    kind = str(c["data"]).strip().lower()
    if kind not in STRAVA_SERIES:
        errors.append(f"{where}: chart data {kind!r} unknown — use one of {', '.join(STRAVA_SERIES)} or typed \"points\"")
        return None
    if c.get("ride"):
        act = load_activity(project, str(c["ride"]))
        if act is None:
            errors.append(f"{where}: Strava ride {c['ride']} isn't imported (./reels strava list)")
            return None
    else:
        if sg is None or not (sg.asset.get("strava") or {}).get("activity"):
            errors.append(f"{where}: the clip under this chart has no Strava ride linked — give \"ride\": \"<activity id>\" (./reels strava list)")
            return None
        act = linked_activity(project, sg.asset)
        if act is None:
            errors.append(f"{where}: Strava ride {sg.asset['strava']['activity']} isn't in strava/activities/ — import it again")
            return None
    stream, smooth_default, title = STRAVA_SERIES[kind]
    st = Streams(act.get("streams") or {})
    if not st.has(stream):
        errors.append(f"{where}: ride {act['id']} has no {stream} stream — import get_activity_streams with --activity {act['id']}")
        return None
    a = float(c.get("from", 0)) * 60
    b = float(c["to"]) * 60 if c.get("to") is not None else st.t[-1]
    if b - a < 10:
        errors.append(f"{where}: chart \"from\"/\"to\" are minutes into the ride and need at least 10 s between them")
        return None
    smooth = float(c.get("smooth", smooth_default))
    n = int(min(CHART_POINTS, max(2, (b - a) // 1)))
    step = (b - a) / n
    half = max(smooth, step) / 2
    xs, ys = [], []
    for i in range(n + 1):
        t = a + i * step
        v = st.mean(stream, t - half, t + half)
        if v is None:
            continue
        xs.append(round((t - a) / 60 if c.get("from") is not None else t / 60, 3))
        ys.append(round(speed_in(project, v) if kind == "strava:speed" else v, 1))
    if len(xs) < 2:
        errors.append(f"{where}: no {title} data between {fmt_ts(a)} and {fmt_ts(b)} into ride {act['id']}")
        return None
    unit = unit_names(project)["speed"] if kind == "strava:speed" else LIVE_UNITS[kind]
    return xs, ys, unit, title


def chart_props(project: Project, c: Any, sg: Any, errors: list[str], where: str) -> dict | None:
    """Template props for a chart exhibit. Typed numbers are the user's (like typed stat pills);
    "data": "strava:…" reads the user's own ride."""
    if not isinstance(c, dict):
        errors.append(f'{where}: chart must be an object like {{"type": "line", "points": [[0, 100], [3, 40]]}} or {{"data": "strava:power"}}')
        return None
    kind = c.get("type") or "line"
    if kind not in CHART_TYPES:
        errors.append(f"{where}: chart type must be one of {', '.join(CHART_TYPES)}")
        return None
    theme = c.get("theme") or "light"
    if theme not in CHART_THEMES:
        errors.append(f"{where}: chart theme must be light or dark")
        return None
    labels: list[str] | None = None
    unit = str(c.get("unit") or "")
    title = str(c.get("title") or "")
    x_label = str(c.get("xLabel") or "")
    if c.get("data"):
        got = _strava_series(project, c, sg, errors, where)
        if got is None:
            return None
        xs, ys, s_unit, s_title = got
        unit = unit or s_unit
        title = title or s_title
        x_label = x_label or "min"
        source = "strava"
    else:
        source = "typed"
        try:
            if c.get("points"):
                pts = sorted((float(p[0]), float(p[1])) for p in c["points"])
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            elif c.get("values"):
                ys = [float(v) for v in c["values"]]
                xs = [float(i) for i in range(len(ys))]
                if c.get("labels"):
                    labels = [str(x) for x in c["labels"]]
                    if len(labels) != len(ys):
                        errors.append(f"{where}: chart labels and values must be the same length")
                        return None
            else:
                errors.append(f'{where}: chart needs "points" [[x, y], …], "values" (with "labels"), or "data": "strava:power"')
                return None
        except (TypeError, ValueError, IndexError):
            errors.append(f"{where}: chart points/values must be numbers")
            return None
        if len(ys) < 2:
            errors.append(f"{where}: a chart needs at least two points")
            return None
    if kind == "bar" and len(ys) > 12:
        errors.append(f"{where}: {len(ys)} bars won't read on a phone — use a line chart, or 12 bars at most")
        return None

    lo, hi = min(ys), max(ys)
    base = 0.0 if (kind == "bar" or c.get("zero") or lo >= 0 and lo <= 0.35 * hi) else lo
    y_ticks = _ticks(min(base, lo), hi)
    y_min, y_max = min(base, lo, y_ticks[0]), max(hi, y_ticks[-1])
    out: dict[str, Any] = {
        "type": kind, "theme": theme, "title": title, "unit": unit, "xLabel": x_label, "source": source,
        "xs": xs, "ys": ys, "yMin": y_min, "yMax": y_max,
        "yTicks": [{"v": v, "label": _fmt(v)} for v in y_ticks if y_min <= v <= y_max],
        "draw": bool(c.get("draw", True)),
    }  # fmt: skip
    if labels:
        out["xTicks"] = [{"v": x, "label": lb} for x, lb in zip(xs, labels)]
    elif kind == "bar":
        out["xTicks"] = [{"v": x, "label": _fmt(x)} for x in xs] if c.get("points") else []
    else:
        out["xTicks"] = [{"v": v, "label": _fmt(v)} for v in _ticks(xs[0], xs[-1]) if xs[0] <= v <= xs[-1]]
    if c.get("color"):
        out["color"] = str(c["color"])
    mark = c.get("mark")
    if mark is not None:
        if not isinstance(mark, dict) or mark.get("x") is None:
            errors.append(f'{where}: chart mark looks like {{"x": 3, "label": "0:03"}}')
            return None
        mx = float(mark["x"])
        if labels and isinstance(mark["x"], str):
            errors.append(f"{where}: chart mark x is the position (0 = first bar)")
            return None
        if not xs[0] <= mx <= xs[-1]:
            errors.append(f"{where}: chart mark x={mx:g} is outside the data ({xs[0]:g}–{xs[-1]:g})")
            return None
        j = max(i for i, x in enumerate(xs) if x <= mx)
        k = min(j + 1, len(xs) - 1)
        f = 0.0 if k == j or xs[k] == xs[j] else (mx - xs[j]) / (xs[k] - xs[j])
        my = ys[j] + (ys[k] - ys[j]) * f
        out["mark"] = {"x": mx, "y": round(my, 3), "label": str(mark.get("label") or f"{_fmt(round(my))}{(' ' + unit) if unit else ''}")}
    return out


# --------------------------------------------------------------------------
# Plan -> placed exhibits
# --------------------------------------------------------------------------


def _number(spec: dict, key: str, default: float, lo: float, hi: float, errors: list[str], where: str) -> float:
    v = spec.get(key, default)
    try:
        v = float(default if v is None else v)
    except (TypeError, ValueError):
        errors.append(f"{where}: {key} must be a number")
        return default
    if not lo <= v <= hi:
        errors.append(f"{where}: {key}={v:g} is outside {lo:g}–{hi:g}")
        return default
    return v


def _choice(spec: dict, key: str, default: str, allowed: tuple[str, ...], errors: list[str], where: str) -> str:
    v = spec.get(key) or default
    if v not in allowed:
        errors.append(f"{where}: {key} must be one of {', '.join(allowed)} (got {v!r})")
        return default
    return v


def punch_at(keys: list[list[float]] | None, f: float) -> tuple[float, float, float]:
    """(scale, originX, originY) of a segment's punch-in at a frame from its start."""
    if not keys:
        return 1.0, 0.5, 0.5
    if f <= keys[0][0]:
        return keys[0][1], keys[0][2], keys[0][3]
    for a, b in zip(keys, keys[1:]):
        if f < b[0]:
            t = (f - a[0]) / max(1, b[0] - a[0])
            return a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t, a[3] + (b[3] - a[3]) * t
    return keys[-1][1], keys[-1][2], keys[-1][3]


def track_hand(project: Project, sg: Any, a: int, b: int, fps: int) -> list[dict | None]:
    """The pointing fingertip in frames [a, b) of a video segment (frames from its start), as it
    appears on the reel: looked for in the clip framed the way the reel frames it, then moved with
    the segment's punch-ins. One entry per frame, None where no hand points."""
    from .punch import punch_keys
    from .render import stage_video

    spec = sg.spec
    moving = bool((spec.get("sphere") or {}).get("path"))  # a 360 camera move is timed from the segment start
    lo, hi = (0, sg.frames) if moving else (a, b)
    sub = replace(sg, frames=hi - lo, spec=dict(spec, **{"in": spec["in"] + lo / fps * spec["speed"],
                                                          "out": min(spec["out"], spec["in"] + hi / fps * spec["speed"])}))  # fmt: skip
    ident = {k: sub.spec.get(k) for k in ("in", "out", "speed", "frame", "zoom", "focusX", "focusY", "sphere")}
    key = hashlib.sha1(json.dumps([sg.asset.get("hash"), ident, fps, sub.frames, TRACK_SCALE], sort_keys=True, default=str).encode()).hexdigest()[:20]
    cache = project.root / ".cache" / "vision" / f"hands-{key}.json"
    if cache.exists():
        rows = vision.hand_rows(project, cache, key)
    else:
        with tempfile.TemporaryDirectory(prefix="reels-hand-") as td:
            clip = Path(td) / "look.mp4"
            stage_video(project, sub, clip, fps, scale=TRACK_SCALE, audio=False)
            rows = vision.hand_rows(project, clip, key)
    tw, th = int(project.cfg("render", "width")), int(project.cfg("render", "height"))
    tips = vision.fingertips(rows, tw / th)
    tips = (tips + [None] * (hi - lo))[: hi - lo][a - lo : b - lo]
    keys = punch_keys(sg, fps)
    if keys:
        for i, t in enumerate(tips):
            if t:
                s, ox, oy = punch_at(keys, a + i)
                t["x"], t["y"] = ox + (t["x"] - ox) * s, oy + (t["y"] - oy) * s
    return tips


def _centre(tip: dict, w: float, h: float, side: str, gap: float, point: list[float] | None, tw: int, th: int) -> tuple[float, float]:
    """Exhibit centre (px) for a fingertip: `point` puts that spot of the image under the finger;
    otherwise the exhibit sits just beyond the fingertip, on the side it points to."""
    tx, ty = tip["x"] * tw, tip["y"] * th
    if point:
        cx, cy = tx - (point[0] - 0.5) * w, ty - (point[1] - 0.5) * h
    else:
        dx, dy = {"left": (-1.0, 0.0), "right": (1.0, 0.0), "above": (0.0, -1.0), "below": (0.0, 1.0)}.get(side, (tip["dx"], tip["dy"]))
        reach = min(w / 2 / abs(dx) if abs(dx) > 1e-6 else math.inf, h / 2 / abs(dy) if abs(dy) > 1e-6 else math.inf)
        cx, cy = tx + dx * (gap * tw + reach), ty + dy * (gap * tw + reach)
    return _inside(cx, cy, w, h)


def _inside(cx: float, cy: float, w: float, h: float) -> tuple[float, float]:
    cy = clamp(cy, SAFE_TOP + h / 2, SAFE_BOTTOM - h / 2) if h <= SAFE_BOTTOM - SAFE_TOP else (SAFE_TOP + SAFE_BOTTOM) / 2
    right = BUTTONS_LEFT if cy + h / 2 > BUTTONS_TOP else SAFE_RIGHT
    cx = clamp(cx, SAFE_LEFT + w / 2, right - w / 2) if w <= right - SAFE_LEFT else (SAFE_LEFT + right) / 2
    return cx, cy


def build_exhibits(project: Project, plan: dict, segs: list, total: int, fps: int, errors: list[str], warnings: list[str]) -> list[dict]:
    """Every exhibit in the plan, timed and placed. Each: {"props": template props, "source": image
    path or None, "crop", "where", "host": segment index, "note": how it was placed}."""
    tw, th = int(project.cfg("render", "width")), int(project.cfg("render", "height"))
    out: list[dict] = []
    n = {None: 0}
    from .timeline import suspended_event_paths
    suspended = suspended_event_paths(plan)
    for seg_i, raw in _raw(plan):
        sg = next((s for s in segs if s.index == seg_i), None) if seg_i is not None else None
        n[seg_i] = n.get(seg_i, 0) + 1
        where = f"segment {seg_i + 1} exhibit {n[seg_i]}" if seg_i is not None else f"exhibit {n[None]}"
        if seg_i is None and f"/exhibits/{n[None]-1}" in suspended:
            continue
        if not isinstance(raw, dict):
            errors.append(f'{where}: use {{"image": "<file in exhibits/>", "at": 1.0, "for": 4.0}} (or "chart")')
            continue
        if seg_i is not None and sg is None:
            continue  # its segment had a problem of its own
        unknown = sorted(set(raw) - KEYS)
        if unknown:
            errors.append(f"{where}: unknown field(s) {', '.join(unknown)} — exhibits take {', '.join(sorted(KEYS))}")
            continue
        if bool(raw.get("image")) == bool(raw.get("chart")):
            errors.append(f'{where}: needs either "image" (a file in exhibits/) or "chart", not {"both" if raw.get("image") else "neither"}')
            continue

        at = _number(raw, "at", 0.3 if sg else 0.0, 0, 600, errors, where)
        start = (sg.start if sg else 0) + round(at * fps)
        active = [s for s in segs if s.start <= start < s.end]
        if sg:
            host = sg
        elif plan.get("timeline") and active:
            from .timeline import video_track
            visible = [s for s in active if not s.trans or start > s.start]
            host = max(visible or active, key=lambda s: (video_track(s.spec["timeline"]["track"]), s.start, s.index))
        else:
            host = next(iter(active), None)
        if host is None and seg_i is None and plan.get("timeline"):
            from types import SimpleNamespace
            host = SimpleNamespace(index=-1, kind="gap", start=0, end=total, frames=total, spec={}, asset={})
        if host is None or start >= total:
            errors.append(f"{where}: starts at {start / fps:.2f}s, after the reel ends ({total / fps:.2f}s)")
            continue
        if not host.start <= start < host.end:
            errors.append(f"{where}: at={at:g}s is past the end of its segment ({host.frames / fps:.2f}s)")
            continue
        if raw.get("for") is None:
            end = host.end - round(0.1 * fps)
        else:
            end = start + round(_number(raw, "for", 3.0, 1 / fps if plan.get("timeline") else 0.3, 600, errors, where) * fps)
        end = min(end, total, sg.end if sg and plan.get("timeline") else total)
        if end - start < (1 if plan.get("timeline") else MIN_FRAMES):
            errors.append(f"{where}: on screen for {(end - start) / fps:.2f}s — too short to read, give it at least {MIN_FRAMES / fps:.1f}s")
            continue

        style = _choice(raw, "style", "card", STYLES, errors, where)
        depth = _choice(raw, "depth", "front", DEPTHS, errors, where)
        props: dict[str, Any] = {
            "startFrame": int(start), "endFrame": int(end), "style": style, "depth": depth,
            "tilt": _number(raw, "tilt", 0, -20, 20, errors, where),
            "enter": _choice(raw, "enter", "pop", MOVES, errors, where),
            "exit": _choice(raw, "exit", "fade", MOVES, errors, where),
            "shadow": bool(raw.get("shadow", True)),
        }  # fmt: skip
        source, crop = None, None
        if raw.get("image"):
            try:
                source = resolve(project, raw["image"])
                im = load(source)
            except ReelsError as e:
                errors.append(f"{where}: {e}")
                continue
            iw, ih = im.size
            if raw.get("crop") is not None:
                c = raw["crop"]
                ok = isinstance(c, list) and len(c) == 4 and all(isinstance(v, (int, float)) for v in c)
                if not ok or not (0 <= c[0] < c[2] <= 1 and 0 <= c[1] < c[3] <= 1):
                    errors.append(f"{where}: crop is [left, top, right, bottom] as 0–1 fractions of the image")
                    continue
                crop = [float(v) for v in c]
                iw, ih = iw * (crop[2] - crop[0]), ih * (crop[3] - crop[1])
            aspect = iw / max(1.0, ih)
            props.update(kind="image")
            if style == "card":
                props["background"] = str(raw.get("background") or "#ffffff") if see_through(im) or raw.get("background") else None
            label = source.name
        else:
            chart = chart_props(project, raw["chart"], host, errors, where)
            if chart is None:
                continue
            aspect = _number(raw["chart"], "aspect", 1.25, 0.6, 2.5, errors, where)
            props.update(kind="chart", chart=chart, style="card")
            label = f"chart ({chart['type']}, {chart['source']})"
        props["padding"] = int(_number(raw, "padding", 0, 0, 80, errors, where)) if style == "card" else 0

        w = _number(raw, "width", 0.6, 0.12, 1.0, errors, where) * tw
        h = w / aspect
        most_h = 0.62 * th
        if h > most_h:
            w, h = w * most_h / h, most_h
            warnings.append(f"{where}: {label} is tall — shown {w / tw:.0%} of the frame wide so it fits (use \"crop\" to show part of it)")
        safe = raw.get("safeArea", True)
        if not isinstance(safe, bool):
            errors.append(f"{where}: safeArea is true or false")
            continue
        cx, cy = _number(raw, "x", 0.5, 0, 1, errors, where) * tw, _number(raw, "y", 0.36, 0, 1, errors, where) * th
        # decorative overlays (a pile of stickers) may run to the frame's edges and under the app's buttons
        cx, cy = _inside(cx, cy, w, h) if safe else (cx, cy)
        note = "placed" if safe else "placed (whole frame)"
        path = None

        anchor = raw.get("anchor")
        if anchor is not None and not safe:
            errors.append(f"{where}: safeArea: false is for placed exhibits only (not anchor)")
            continue
        if anchor not in (None, "hand"):
            errors.append(f'{where}: anchor can only be "hand" (leave it out to place the exhibit with x/y)')
            continue
        point = raw.get("point")
        if point is not None and not (isinstance(point, list) and len(point) == 2 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in point)):
            errors.append(f"{where}: point is [x, y] on the image as 0–1 fractions — the spot your finger touches")
            continue
        if anchor == "hand":
            side = _choice(raw, "side", "auto", SIDES, errors, where)
            gap = _number(raw, "gap", 0.03, 0, 0.3, errors, where)
            fallback = f"placed with x/y instead ({cx / tw:.2f}, {cy / th:.2f})"
            why = vision.available()
            if host.kind != "video":
                warnings.append(f"{where}: anchor=hand needs a video segment under it — {fallback}")
            elif why:
                warnings.append(f"{where}: {why} — {fallback}")
            else:
                a, b = start - host.start, min(end, host.end) - host.start
                try:
                    tips = track_hand(project, host, a, b, fps)
                except ReelsError as e:
                    tips = []
                    warnings.append(f"{where}: couldn't look for the hand ({e}) — {fallback}")
                seen = sum(1 for t in tips if t)
                if raw.get("follow"):
                    track = vision.smooth_track(tips)
                    if track:
                        pts = [_centre(t, w, h, side, gap, point, tw, th) for t in track]
                        path = [[i, round(p[0], 1), round(p[1], 1)] for i, p in enumerate(pts) if i % 2 == 0 or i == len(pts) - 1]
                        cx, cy = pts[0]
                        note = f"follows the hand (seen in {seen / len(tips):.0%} of frames)"
                else:
                    tip = vision.steady_point(tips)
                    if tip:
                        cx, cy = _centre(tip, w, h, side, gap, point, tw, th)
                        note = f"at the fingertip ({tip['x']:.2f}, {tip['y']:.2f}; hand seen in {tip['share']:.0%} of frames)"
                if tips and note == "placed":
                    warnings.append(f"{where}: no pointing hand found in {host.asset['id']} between {a / fps:.2f}s and {b / fps:.2f}s of the "
                                    f"segment (seen in {seen}/{len(tips)} frames) — {fallback}")  # fmt: skip
        elif raw.get("follow") or point is not None or raw.get("side") or raw.get("gap") is not None:
            errors.append(f'{where}: follow, point, side and gap only apply with "anchor": "hand"')
            continue

        props.update(x=round(cx, 1), y=round(cy, 1), width=round(w, 1), height=round(h, 1))
        if sg and plan.get("timeline"):
            props["ownerSegmentIndex"] = sg.index
        if path:
            props["path"] = path
        if source is not None:
            props["src"] = f"exhibit{len(out)}.png"
        out.append({"props": props, "source": source, "crop": crop, "where": where, "host": host.index, "note": note, "label": label})

    out.sort(key=lambda e: e["props"]["startFrame"])
    for k, e in enumerate(out):  # staged names follow the final order
        if e["source"] is not None:
            e["props"]["src"] = f"exhibit{k}.png"
    _check_depth(out, segs, fps, errors, positioned=bool(plan.get("timeline")))
    return out


def _check_depth(exhibits: list[dict], segs: list, fps: int, errors: list[str], *, positioned: bool = False) -> None:
    """depth=behind lays the rider back over the exhibit: that needs the clip itself on screen."""
    for e in exhibits:
        p = e["props"]
        if p["depth"] != "behind":
            continue
        global_gap = positioned and e["host"] < 0 and p.get("ownerSegmentIndex") is None
        if e["host"] < 0 and not global_gap:
            errors.append(f"{e['where']}: depth=behind needs footage, not an empty timeline gap")
            continue
        if global_gap and not any(sg.start < p["endFrame"] and p["startFrame"] < sg.end for sg in segs):
            continue  # No subject to cut out while the global exhibit sits over black.
        why = vision.available()
        if why:
            errors.append(f"{e['where']}: {why} — use \"depth\": \"front\"")
            continue
        for sg in segs:
            if p.get("ownerSegmentIndex") is not None and sg.index != p["ownerSegmentIndex"]:
                continue
            if not (sg.start < p["endFrame"] and p["startFrame"] < sg.end):
                continue
            if sg.kind != "video":
                errors.append(f"{e['where']}: depth=behind needs video under it, and segment {sg.index + 1} is a photo — end the exhibit "
                              "before it or use \"depth\": \"front\"")  # fmt: skip
                break
            for c in sg.spec.get("_cutaways") or []:
                a, b = sg.start + c["start"], sg.start + c["start"] + c["seg"].frames
                if a < p["endFrame"] and p["startFrame"] < b:
                    errors.append(f"{e['where']}: depth=behind overlaps {c['where']} ({a / fps:.2f}–{b / fps:.2f}s) — under a cutaway "
                                  "you're not on screen to stand in front of it; move one of them or use \"depth\": \"front\"")  # fmt: skip
                    break


def bounds(p: dict) -> tuple[float, float, float, float]:
    """(left, top, right, bottom) px an exhibit covers over its whole time on screen."""
    pts = [(p["x"], p["y"])] + [(q[1], q[2]) for q in p.get("path") or []]
    w, h = p["width"] / 2, p["height"] / 2
    return min(x for x, _ in pts) - w, min(y for _, y in pts) - h, max(x for x, _ in pts) + w, max(y for _, y in pts) + h


def foregrounds(exhibits: list[dict], segs: list, fps: int) -> list[dict]:
    """Template props for the rider-in-front layers: one stretch per segment that has an exhibit
    behind the rider. {"src", "startFrame" (reel), "durationInFrames", "offset" (frames into the
    segment), "seg", "punch"}."""
    from .punch import punch_keys

    out = []
    for sg in segs:
        owned_spans, global_spans = [], []
        for e in exhibits:
            p = e["props"]
            owner = p.get("ownerSegmentIndex")
            if p["depth"] != "behind" or owner is not None and owner != sg.index:
                continue
            a, b = max(p["startFrame"], sg.start), min(p["endFrame"], sg.end)
            if b > a:
                (global_spans if owner is None else owned_spans).append((a, b))
        spans = owned_spans + global_spans
        if not spans or sg.kind != "video":
            continue
        a, b = min(s[0] for s in spans), max(s[1] for s in spans)
        fg: dict[str, Any] = {"src": f"fg{sg.index}.webm", "startFrame": int(a), "durationInFrames": int(b - a),
                               "offset": int(a - sg.start), "seg": sg.index}  # fmt: skip
        if sg.spec.get("timeline"):
            fg.update(ownerSegmentIndex=sg.index, ownedBehindRanges=owned_spans, globalBehindRanges=global_spans)
        keys = punch_keys(sg, fps)
        if keys:
            fg["punch"] = keys
        out.append(fg)
    return out


# --------------------------------------------------------------------------
# Text gets out of the way
# --------------------------------------------------------------------------


def _lines(text: str) -> int:
    return max(1, math.ceil(len(text) / CHARS_PER_LINE))


def _band(item: dict, kind: str) -> tuple[float, float]:
    """(top, bottom) px of a hook / text beat / caption page where the template puts it."""
    if kind == "caption":
        text = " ".join(w["text"] for w in item["words"])
        bottom = 1920 - item.get("offsetBottom", 1920 - CAPTION_BOTTOM)
        return bottom - _lines(text) * LINE, bottom
    hgt = _lines(item["text"]) * LINE + 12
    pos = item.get("position")
    if pos == "center":
        return 960 - hgt / 2, 960 + hgt / 2
    if pos == "lower":
        return LOWER_TEXT_BOTTOM - hgt, LOWER_TEXT_BOTTOM
    top = item.get("offsetTop", TOP_TEXT)
    return top, top + hgt


def make_room(exhibits: list[dict], hook: dict | None, beats: list[dict], pages: list[dict], warnings: list[str], fps: int,
              *, positioned: bool = False) -> None:
    """Moves captions, the hook and text beats (in place) above or below any exhibit they would
    sit on while it's up. Text is centred and nearly frame-wide, so only height matters."""
    if not exhibits:
        return
    obstacles = [(e["props"]["startFrame"], e["props"]["endFrame"], *bounds(e["props"])[1::2],
                  e["props"].get("ownerSegmentIndex")) for e in exhibits]

    def shares_layer(item: dict, owner: int | None) -> bool:
        item_owner = item.get("ownerSegmentIndex")
        return not positioned or owner is None or item_owner is None or owner == item_owner

    def place(item: dict, kind: str, name: str) -> None:
        top, bottom = _band(item, kind)
        hgt = bottom - top
        busy = sorted((t - PAD, b + PAD) for s, e, t, b, owner in obstacles
                      if shares_layer(item, owner) and s < item["endFrame"] and item["startFrame"] < e)
        if not any(t < bottom and top < b for t, b in busy):
            return
        # the free stretches of the frame's height while this text is up; take the nearest one it fits in
        lo, spots = float(SAFE_TOP), []
        for t, b in busy + [(SAFE_BOTTOM + 60.0, math.inf)]:
            if t - lo >= hgt:
                spots.append(clamp(top, lo, t - hgt))
            lo = max(lo, b)
        if not spots:
            warnings.append(f"{name} sits on an exhibit and there's no room above or below it — make the exhibit smaller, move it, "
                            "or shorten the text")  # fmt: skip
            return
        top = min(spots, key=lambda t: abs(t - top))
        bottom = top + hgt
        if kind == "caption":
            item["offsetBottom"] = int(round(1920 - bottom))
        else:
            item["position"] = "top"
            item["offsetTop"] = int(round(top))
        obstacles.append((item["startFrame"], item["endFrame"], top, bottom, item.get("ownerSegmentIndex")))

    for pg in pages:
        place(pg, "caption", f"captions at {pg['startFrame'] / fps:.1f}s")
    # Captions that hop for a page or two read as a glitch: neighbours within a moved run share its place.
    for prev, pg in zip(pages, pages[1:]):
        if (shares_layer(pg, prev.get("ownerSegmentIndex")) and "offsetBottom" in prev
                and "offsetBottom" not in pg and pg["startFrame"] - prev["endFrame"] <= 8):
            if any(shares_layer(pg, owner) and s < pg["endFrame"] + fps and pg["startFrame"] - fps < e
                   for s, e, _, _, owner in obstacles[: len(exhibits)]):
                pg["offsetBottom"] = prev["offsetBottom"]
    if hook and hook.get("size") != "big":  # a big hook is the statement on top of the pile: it stays put
        place(hook, "hook", "the hook")
    for b in beats:
        place(b, "beat", f"text '{b['text']}'")


# --------------------------------------------------------------------------
# Staging, check lines
# --------------------------------------------------------------------------


def stage(project: Project, built: dict, staging: Path) -> None:
    """Copies each exhibit image into the render's public dir (cropped, sized for the frame) and
    cuts the rider out of the staged clips wherever an exhibit sits behind them."""
    for e in built.get("exhibits") or []:
        if e["source"] is None:
            continue
        im = load(e["source"])
        if e["crop"]:
            l, t, r, b = e["crop"]
            im = im.crop((round(l * im.width), round(t * im.height), round(r * im.width), round(b * im.height)))
        want = round(e["props"]["width"] * 2)  # 2x: stays sharp through the pop and any tilt
        if im.width > want:
            im = im.resize((want, max(1, round(im.height * want / im.width))), Image.LANCZOS)
        im.save(staging / e["props"]["src"], optimize=False)
    for fg in built["props"].get("foregrounds") or []:
        clip = staging / f"seg{fg['seg']}.mp4"
        log(f"  cutting you out in front of the exhibit: segment {fg['seg'] + 1}, {fg['durationInFrames'] / built['fps']:.1f}s…")
        vision.foreground(project, clip, staging / fg["src"], start=fg["offset"], frames=fg["durationInFrames"], fps=built["fps"])


def check_lines(built: dict) -> list[str]:
    fps = built["fps"]
    out = []
    for e in built.get("exhibits") or []:
        p = e["props"]
        out.append(f"  exhibit {p['startFrame'] / fps:5.2f}-{p['endFrame'] / fps:.2f}s  {e['label']}  {p['style']} "
                   f"{p['width']:.0f}x{p['height']:.0f} centred ({p['x'] / 1080:.2f}, {p['y'] / 1920:.2f})  "
                   f"{'behind you' if p['depth'] == 'behind' else 'in front'}  · {e['note']}")  # fmt: skip
    return out


def preview(project: Project, plan_path: str | Path, at: float | None = None) -> list[Path]:
    """Renders single frames of the reel with its exhibits up (one per exhibit, or the one at `at`
    seconds) into out/<slug>/exhibits/ — for nudging x/y/width before a full render."""
    import os
    import subprocess

    from .media import require
    from .render import build, load_plan, stage
    from .util import warn, write_json

    require("ffmpeg")
    plan = load_plan(project, plan_path)
    built = build(project, plan)
    for w in built["warnings"]:
        if "exhibit" in w or "captions at" in w or "sits on" in w:
            warn(w)
    fps, total = built["fps"], built["total"]
    if at is not None:
        frames = [int(clamp(round(at * fps), 0, total - 1))]
    else:
        frames = [min(e["props"]["endFrame"] - 1, e["props"]["startFrame"] + max(12, (e["props"]["endFrame"] - e["props"]["startFrame"]) // 2))
                  for e in built["exhibits"]]  # fmt: skip
    if not frames:
        raise ReelsError(f"{plan['slug']} has no exhibits — add \"exhibits\" to a segment (reference/exhibits.md)")
    binary = project.remotion_dir / "node_modules" / ".bin" / "remotion"
    if not binary.exists():
        raise ReelsError("Remotion isn't installed yet — run ./setup.sh (or: cd remotion && npm install)")
    staging = project.staging_dir / f"{plan['slug']}-exhibits"
    out_dir = project.out_dir / plan["slug"] / "exhibits"
    out_dir.mkdir(parents=True, exist_ok=True)
    log(f"staging {plan['slug']} for {len(frames)} preview frame(s)…")
    made = []
    try:
        stage(project, built, staging)
        write_json(staging / "props.json", built["props"])
        for f in frames:
            out = out_dir / f"frame-{f / fps:06.2f}s.jpg"
            cmd = [str(binary), "still", "src/index.ts", "CyclingReel", str(out), f"--props={staging / 'props.json'}",
                   f"--public-dir={staging}", f"--frame={f}", "--image-format=jpeg", "--jpeg-quality=88", "--log=error"]  # fmt: skip
            browser = os.environ.get("REELS_BROWSER") or project.cfg("render", "browserExecutable")
            if browser:
                cmd.append(f"--browser-executable={browser}")
            proc = subprocess.run(cmd, cwd=project.remotion_dir, capture_output=True, text=True)
            if proc.returncode != 0 or not out.exists():
                raise ReelsError(f"preview frame failed:\n{(proc.stdout + proc.stderr)[-2000:]}")
            made.append(out)
            log(f"  ✓ {project.rel(out)}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    for line in check_lines(built):
        log(line)
    return made
