"""Colour for the footage: every clip converted to BT.709 from what it really is, then graded.

Two steps, both done while staging a clip (so the fast path and the Remotion render show the
same pixels):

1. Colour management (always on). iPhone copies and shared clips are often Display P3 with a
   BT.601 matrix and full range; staged as if they were BT.709 they lose about 15% of their colour.
   `to_bt709` converts from the real tags. HDR clips are already tone-mapped to BT.709 by
   `media.hdr_filters`.
2. The grade (config `render.grade`, plan `grade`, segment `grade`). Measured once per segment
   on frames of the staged clip, so nothing flickers:
     - a per-shot technical fix: colour cast from neutral surfaces below the sky band, black and
       white points and a light mid-tone nudge, each bounded. It doesn't try to make shots match;
     - the house look, the same on every reel so they read as one feed: a gentle contrast curve,
       warm highlights / cool shadows and mild vibrance that spares skin and whites;
   both folded into one 3D LUT for the segment, plus
     - a grey-day rescue on overcast shots: local contrast and a pull on bright sky, scaled by
       how overcast the shot is and eased off on low-resolution sources.

Only footage is graded: exhibits, charts, maps, title cards and text never go through here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np

from .media import has_filter, run
from .util import ReelsError

VERSION = 1
LUT_SIZE = 33
LOOKS = ("house", "moody", "none")
DEFAULTS: dict[str, Any] = {"look": "none", "strength": 1.0, "rescue": "auto"}
SAMPLES = 8  # frames measured per segment
SAMPLE_W = 270

# ffprobe tag -> zscale / colorspace-filter names
_Z_MATRIX = {"bt709": "709", "smpte170m": "170m", "bt470bg": "470bg", "bt2020nc": "2020_ncl", "bt2020c": "2020_cl", "fcc": "fcc", "smpte240m": "240m"}
_Z_PRIM = {"bt709": "709", "smpte170m": "170m", "bt470bg": "470bg", "smpte240m": "240m", "smpte431": "smpte431", "smpte432": "smpte432", "bt2020": "2020", "film": "film"}
_CS_SPACE = {"bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470bg", "bt2020nc": "bt2020ncl", "fcc": "fcc", "smpte240m": "smpte240m"}
_CS_PRIM = {"bt709": "bt709", "smpte170m": "smpte170m", "bt470bg": "bt470bg", "smpte240m": "smpte240m", "smpte431": "smpte431", "smpte432": "smpte432", "bt2020": "bt2020", "film": "film"}
BT709 = {"matrix": "bt709", "primaries": "bt709", "range": "tv"}


# --------------------------------------------------------------------------
# 1. Colour management
# --------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _tags(path: str) -> dict:
    out = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
               "stream=color_space,color_primaries,color_range,pix_fmt,width,height", "-of", "json", path], check=False).stdout  # fmt: skip
    import json

    try:
        return (json.loads(out or "{}").get("streams") or [{}])[0]
    except ValueError:
        return {}


def source_colour(probe: dict, path: Path | str) -> dict:
    """What the frames are after `hdr_filters`: {matrix, primaries, range} in ffprobe's names.
    Missing tags get the usual guesses (HD = BT.709, `yuvj` = full range)."""
    if probe.get("hdr"):
        return dict(BT709)  # tone-mapped to BT.709 limited by media.TONEMAP / HLG_FALLBACK
    t = _tags(str(path))
    hd = int(t.get("width") or probe.get("width") or 0) * int(t.get("height") or probe.get("height") or 0) > 720 * 576
    matrix = t.get("color_space") if t.get("color_space") in _Z_MATRIX else ("bt709" if hd else "smpte170m")
    prim = t.get("color_primaries") if t.get("color_primaries") in _Z_PRIM else "bt709"
    rng = t.get("color_range")
    if rng not in ("tv", "pc"):
        rng = "pc" if str(t.get("pix_fmt") or probe.get("pixFmt") or "").startswith("yuvj") else "tv"
    return {"matrix": matrix, "primaries": prim, "range": rng}


def use_zscale() -> bool:
    """zscale (zimg) is the accurate converter; Homebrew's ffmpeg-full has it. Switches to it if installed."""
    from .media import can_tonemap

    return can_tonemap() or has_filter("zscale")


def _z_in(c: dict) -> str:
    return f"min={_Z_MATRIX[c['matrix']]}:pin={_Z_PRIM[c['primaries']]}:tin=709:rin={'full' if c['range'] == 'pc' else 'limited'}"


def to_bt709(c: dict) -> str:
    """Filters that turn frames tagged `c` into BT.709 limited-range yuv420p."""
    if c == BT709:
        return "format=yuv420p"
    if use_zscale():
        return f"zscale={_z_in(c)}:m=709:p=709:t=709:r=limited,format=yuv420p"
    if has_filter("colorspace"):
        return (f"colorspace=space=bt709:trc=bt709:primaries=bt709:range=tv:ispace={_CS_SPACE[c['matrix']]}:itrc=bt709"
                f":iprimaries={_CS_PRIM[c['primaries']]}:irange={c['range']}:format=yuv420p")  # fmt: skip
    return "format=yuv420p"


# Tags for every staged/graded encode, so nothing downstream has to guess.
ENCODE_TAGS = ["-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709", "-color_range", "tv"]


# --------------------------------------------------------------------------
# Settings: config render.grade < plan grade < segment grade
# --------------------------------------------------------------------------


def settings(config: dict | None, plan_grade: Any = None, seg_grade: Any = None) -> dict:
    """The grade for one clip: project default (config render.grade) < the plan's `grade` < the segment's.
    `"none"` / false turns grading off at that level; `rescue: true/false` (the editor's toggle) = "auto"/"off"."""
    s = {**DEFAULTS, **(config or {})}
    chosen = False  # a level named the look ("none" or a dict with `look`): later dicts without one keep it
    for g in (plan_grade, seg_grade):
        if g is False or g == "none":
            s["look"] = "none"
            chosen = True
        elif isinstance(g, dict):
            if "look" in g:
                chosen = True
            elif not chosen and s["look"] == "none":
                s["look"] = "house"  # an explicit grade block asks for a grade
            s.update(g)
    if s.get("rescue") is True:
        s["rescue"] = "auto"
    elif s.get("rescue") is False:
        s["rescue"] = "off"
    return s


def check(where: str, g: Any, errors: list[str]) -> None:
    """Validation for a plan or segment `grade` value."""
    if g is None or g is False or g == "none":
        return
    if not isinstance(g, dict):
        errors.append(f"{where}: grade must be an object like {{\"look\": \"house\"}} or \"none\"")
        return
    for k in g:
        if k not in ("look", "strength", "rescue", "warmth", "exposure"):
            errors.append(f"{where}: grade.{k} isn't a grade setting (look, strength, rescue, warmth, exposure)")
    if "look" in g and g["look"] not in LOOKS:
        errors.append(f"{where}: grade.look must be one of {', '.join(LOOKS)}")
    for k, lo, hi in (("strength", 0, 1.5), ("warmth", -1, 1), ("exposure", -1, 1)):
        if k in g and not (isinstance(g[k], (int, float)) and not isinstance(g[k], bool) and lo <= g[k] <= hi):
            errors.append(f"{where}: grade.{k} must be a number {lo}–{hi}")
    r = g.get("rescue", "auto")
    if not (r in ("auto", "off") or isinstance(r, bool) or (isinstance(r, (int, float)) and 0 <= r <= 1)):
        errors.append(f"{where}: grade.rescue must be \"auto\", \"off\", true/false or 0–1")


def active(s: dict) -> bool:
    return s.get("look", "none") != "none"


# --------------------------------------------------------------------------
# Measuring a segment
# --------------------------------------------------------------------------


def _luma(x: np.ndarray) -> np.ndarray:
    return x[..., 0] * 0.2126 + x[..., 1] * 0.7152 + x[..., 2] * 0.0722


def _chroma(x: np.ndarray) -> np.ndarray:
    return x.max(-1) - x.min(-1)


def _lin(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0, 1) ** 2.2


def _enc(x: np.ndarray) -> np.ndarray:
    return np.clip(x, 0, 1) ** (1 / 2.2)


def _smooth(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0, 1)
    return t * t * (3 - 2 * t)


def _hue(x: np.ndarray) -> np.ndarray:
    r, g, b = x[..., 0], x[..., 1], x[..., 2]
    mx, mn = x.max(-1), x.min(-1)
    d = mx - mn + 1e-6
    h = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4))
    return h * 60


def sample(staged: Path, frames: int, w: int, h: int) -> np.ndarray:
    """SAMPLES frames spread over the clip's own frames (not the padding), as RGB floats (n, h, w, 3)."""
    sh = int(round(SAMPLE_W * h / w / 2)) * 2
    step = max(1, frames // SAMPLES)
    conv = ("zscale=min=709:pin=709:tin=709:rin=limited:p=709:t=709:r=full,format=gbrp" if use_zscale()
            else "scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=gbrp")  # fmt: skip
    vf = f"select='lt(n\\,{frames})*not(mod(n\\,{step}))',scale={SAMPLE_W}:{sh}:flags=area,{conv}"
    import subprocess  # binary output: media.run decodes text

    raw = subprocess.run([*_ffmpeg_cmd(), "-v", "error", "-i", str(staged), "-vf", vf, "-fps_mode", "passthrough",
                          "-frames:v", str(SAMPLES), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                         capture_output=True, stdin=subprocess.DEVNULL).stdout  # fmt: skip
    n = len(raw) // (SAMPLE_W * sh * 3)
    if n == 0:
        raise ReelsError(f"grade: could not read frames from {staged.name}")
    return np.frombuffer(raw[: n * SAMPLE_W * sh * 3], np.uint8).reshape(n, sh, SAMPLE_W, 3).astype(np.float32) / 255


def _ffmpeg_cmd() -> list[str]:
    from .media import BIN

    return [BIN["ffmpeg"]]


def sky(x: np.ndarray) -> tuple[float, float]:
    """(overcast, blue sky) for frames `x`: overcast 0 = sun or no sky, 1 = a flat white/grey sky over
    the top of the frame; blue sky = share of the top 40% that is clear blue sky."""
    top = x[:, : int(x.shape[1] * 0.4)]
    y, c = _luma(top), _chroma(top)
    flat = ((y > 0.6) & (c < 0.10)).mean()
    h = _hue(top)
    blue = float(((h > 195) & (h < 245) & (c > 0.22) & (y > 0.3)).mean())
    return float(np.clip((flat - 0.1) / 0.3, 0, 1) * np.clip(1 - blue / 0.12, 0, 1)), blue


def overcast_score(x: np.ndarray) -> float:
    return sky(x)[0]


def measure(x: np.ndarray) -> dict:
    """Per-shot correction for frames `x` (RGB 0–1): every value bounded, so a strange shot can't run away."""
    overcast, blue = sky(x)
    p: dict[str, Any] = {"overcast": round(overcast, 3), "blueSky": round(blue, 3)}
    y, c = _luma(x), _chroma(x)
    below_sky = (np.arange(x.shape[1]) > x.shape[1] * 0.35)[None, :, None]
    sel = (c < 0.12) & (y > 0.15) & (y < 0.92) & below_sky
    gains = np.ones(3)
    if sel.mean() > 0.02:
        m = _lin(x[sel]).mean(0)
        cast = float(np.log(m[0] / max(m[2], 1e-4)))
        amount = 0.5 if cast < 0.15 else 0.2  # remove part of a cast; keep most of a golden-hour glow
        amount *= 1 - 0.8 * float(np.clip(blue / 0.08, 0, 1))  # under blue sky, blue shadows are real skylight
        target = m.mean() * np.array([1.02, 1.0, 0.97])  # neutral, a touch warm
        gains = np.clip(1 + amount * (target / m - 1), 0.9, 1.1)
    p["wb"] = [round(float(g), 4) for g in gains]
    x = _enc(_lin(x) * gains)
    y = _luma(x)
    lo, hi = float(np.percentile(y, 0.5)), float(np.percentile(y, 99.5))
    black = min(max(0.0, lo - 0.02), 0.05)  # only take out a small lift: a foggy or white shot must not turn black
    scale = float(np.clip(0.985 / max(hi - black, 1e-3), 1.0, 1.15))
    p["black"], p["scale"] = round(black, 4), round(scale, 4)
    x = np.clip((x - black) * scale, 0, 1)
    med = float(np.median(_luma(x)))
    g = float(np.clip(np.log(0.45) / np.log(min(max(med, 1e-3), 0.999)), 0.8, 1.25))
    p["gamma"] = round(1 + 0.25 * (g - 1), 4)  # a nudge: dark shots stay darker than bright ones
    return p


# --------------------------------------------------------------------------
# The LUT: correction + look
# --------------------------------------------------------------------------


def correct(x: np.ndarray, p: dict, s: dict) -> np.ndarray:
    lin = _lin(x) * np.array(p["wb"])
    warmth, exposure = float(s.get("warmth") or 0), float(s.get("exposure") or 0)
    if warmth:
        lin = lin * np.array([1 + 0.06 * warmth, 1.0, 1 - 0.08 * warmth])
    if exposure:
        lin = lin * 2**exposure
    x = np.clip((_enc(lin) - p["black"]) * p["scale"], 0, 1) ** (1 / p["gamma"])
    sat = 1 + 0.05 * rescue_amount(p, s)  # grey days get a little colour back; nothing else is boosted here
    y = _luma(x)
    sat = 1 + (sat - 1) * (1 - 0.7 * _smooth(0.8, 0.98, y))  # near-white stays white: no tint pulled out of clouds
    return np.clip(y[..., None] + (x - y[..., None]) * sat[..., None], 0, 1)


def look(x: np.ndarray, name: str) -> np.ndarray:
    y = _luma(x)
    white = _smooth(0.82, 0.98, y)  # clouds and other near-whites keep their colour: no warm tint, no extra saturation
    if name == "house":
        x = x + 0.3 * (_smooth(0, 1, x) - x)  # gentle S-curve
        sh, hl = 1 - _smooth(0.05, 0.4, y), _smooth(0.5, 0.75, y) * (1 - white)
        x = x + sh[..., None] * np.array([-0.012, 0.004, 0.012]) + hl[..., None] * np.array([0.018, 0.006, -0.016])
        h, c, yy = _hue(x), _chroma(x), _luma(x)[..., None]
        skin = ((h > 8) & (h < 50)).astype(np.float32)
        green = ((h > 65) & (h < 165)).astype(np.float32)
        blue = ((h > 195) & (h < 250)).astype(np.float32)
        vib = (1 + 0.12 * (1 - _smooth(0, 0.5, c)) * (1 - 0.55 * skin) * (1 - 0.6 * white)) * (1 + 0.04 * green)
        x = yy + (x - yy) * vib[..., None]
    elif name == "moody":
        x = x + 0.6 * (_smooth(0, 1, x) - x)
        sh, hl = 1 - _smooth(0.05, 0.45, y), _smooth(0.5, 0.75, y) * (1 - white)
        x = x + sh[..., None] * np.array([-0.02, 0.008, 0.022]) + hl[..., None] * np.array([0.02, 0.008, -0.018])
        h, yy = _hue(x), _luma(x)[..., None]
        skin = ((h > 8) & (h < 50)).astype(np.float32)
        blue = ((h > 195) & (h < 250)).astype(np.float32)
        green = ((h > 60) & (h < 170)).astype(np.float32)
        x = yy + (x - yy) * ((0.8 + 0.2 * skin + 0.25 * blue) * (1 - 0.3 * green))[..., None]
        x = 0.05 + x * 0.93
    return np.clip(x, 0, 1)


def grade_rgb(x: np.ndarray, p: dict, s: dict) -> np.ndarray:
    """The whole colour transform (no local contrast) on RGB 0–1 — what the segment's LUT holds."""
    out = look(correct(x, p, s), s.get("look", "house"))
    k = float(s.get("strength", 1.0))
    return np.clip(x + k * (out - x), 0, 1)


def write_lut(path: Path, p: dict, s: dict, n: int = LUT_SIZE) -> None:
    g = np.linspace(0, 1, n, dtype=np.float32)
    b, gg, r = np.meshgrid(g, g, g, indexing="ij")  # .cube order: red changes fastest
    grid = np.stack([r, gg, b], -1).reshape(-1, 1, 3)
    out = grade_rgb(grid, p, s).reshape(-1, 3)
    lines = [f'TITLE "reels grade v{VERSION}"', f"LUT_3D_SIZE {n}"] + [f"{v[0]:.6f} {v[1]:.6f} {v[2]:.6f}" for v in out]
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


# --------------------------------------------------------------------------
# Grey-day rescue: local contrast + a pull on bright sky (luma only)
# --------------------------------------------------------------------------


def rescue_amount(p: dict, s: dict) -> float:
    r = s.get("rescue", "auto")
    if r == "off" or s.get("look") == "none":
        return 0.0
    return float(p.get("overcast", 0)) if r == "auto" else float(r)


def source_quality(probe: dict) -> float:
    """Low-resolution sources (shared 720p clips) show compression blocks once contrast goes up."""
    short = min(int(probe.get("width") or 1080), int(probe.get("height") or 1920))
    return float(np.clip(short / 1080, 0.5, 1.0))


def rescue_graph(src: str, out: str, amount: float, quality: float, w: int, h: int, fps: int) -> str:
    """Filtergraph from [src] to [out] on BT.709 limited yuv420p. Fast filters only (tables, blends).
    Luma only: blend's chroma planes stay in "normal" mode at full opacity, which keeps the first
    (sharp) input's colour; opacity 0 there would hand over the blurred colour and smear it."""
    clarity = 0.45 * amount * quality**2
    pull = 0.20 * amount * quality
    sigma = max(2.0, w / 4 * 0.05)
    t = "clip(((val-16)/219-0.55)/0.30\\,0\\,1)"
    return ";".join([
        f"[{src}]split=3[gr_a][gr_b][gr_c]",
        f"[gr_b]scale={w // 8 * 2}:{h // 8 * 2}:flags=area,gblur=sigma={sigma:.2f},scale={w}:{h}:flags=bilinear[gr_blur]",
        f"[gr_a][gr_blur]blend=c0_mode=grainextract,lutyuv=y='clip((val-128)*{clarity:.4f}+128\\,0\\,255)'[gr_hp]",
        f"[gr_c][gr_hp]blend=c0_mode=grainmerge,split=3[gr_s0][gr_s1][gr_s2]",
        f"[gr_s1]lutyuv=y='255*{t}*{t}*(3-2*{t})':u=0:v=0[gr_bright]",
        f"color=c=black:s=2x{h}:r={fps},format=yuv420p,geq=lum='255*(1-clip((Y/H-0.35)/0.30\\,0\\,1))':cb=0:cr=0,"
        f"scale={w}:{h}:flags=bilinear[gr_ramp]",
        "[gr_bright][gr_ramp]blend=c0_mode=multiply:shortest=1[gr_mask]",
        f"[gr_s2]lutyuv=y='16+(val-16)*{1 - pull:.4f}'[gr_dark]",
        f"[gr_s0][gr_dark][gr_mask]maskedmerge[{out}]",
    ])  # fmt: skip


# --------------------------------------------------------------------------
# Applying it to a staged clip
# --------------------------------------------------------------------------


def apply(staged: Path, out: Path, *, frames: int, fps: int, probe: dict, s: dict) -> dict:
    """Measure `staged` (BT.709 limited, from stage_video), write its LUT next to `out`, and encode the graded clip."""
    probe_out = _tags(str(staged))
    w, h = int(probe_out.get("width") or 1080), int(probe_out.get("height") or 1920)
    p = measure(sample(staged, frames, w, h))
    lut = out.with_name(f"{out.stem}.grade.cube")
    write_lut(lut, p, s)
    amount = rescue_amount(p, s)
    quality = source_quality(probe)
    to_rgb = ("zscale=min=709:pin=709:tin=709:rin=limited:p=709:t=709:r=full,format=gbrp16le" if use_zscale()
              else "scale=in_color_matrix=bt709:in_range=tv:out_range=pc,format=gbrp16le")  # fmt: skip
    to_yuv = "zscale=m=709:p=709:t=709:r=limited,format=yuv420p" if use_zscale() else "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
    graph = f"[0:v]{to_rgb},lut3d=file='{lut.name}':interp=tetrahedral,{to_yuv}"
    if amount > 0.05:
        graph += "[gr_in];" + rescue_graph("gr_in", "v", amount, quality, w, h, fps)
    else:
        graph += "[v]"
    cmd: list[Any] = ["ffmpeg", "-v", "error", "-y", "-i", staged.name, "-filter_complex", graph, "-map", "[v]", "-map", "0:a?",
                      "-c:a", "copy", "-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-g", fps, "-pix_fmt", "yuv420p",
                      *ENCODE_TAGS, "-movflags", "+faststart", out.name]  # fmt: skip
    try:
        run(cmd, cwd=out.parent)
    finally:
        lut.unlink(missing_ok=True)
    return _summary(p, s, amount, quality)


def _summary(p: dict, s: dict, amount: float, quality: float) -> dict:
    out = {**p, "version": VERSION, "look": s.get("look"), "strength": s.get("strength"), "rescue": round(amount, 3), "quality": round(quality, 2)}
    out.update({k: s[k] for k in ("warmth", "exposure") if s.get(k)})
    return out


def grade_image(path: Path, s: dict, *, also: list[Path] | None = None) -> dict:
    """Grade a staged photo (JPEG, sRGB ≈ BT.709) in place with the same correction + look as video;
    `also` = files that get the same transform (the blurred background of a blurfill photo).
    No local contrast or sky pull on photos: they're on screen briefly and often already processed."""
    from PIL import Image

    with Image.open(path) as im:
        rgb = im.convert("RGB")
    small = rgb.copy()
    small.thumbnail((SAMPLE_W * 2, SAMPLE_W * 2))
    p = measure(np.asarray(small, np.float32)[None] / 255)
    for f in [path, *(also or [])]:
        with Image.open(f) as im:
            x = np.asarray(im.convert("RGB"), np.float32) / 255
        graded = grade_rgb(x, p, s)
        Image.fromarray((graded * 255 + 0.5).astype(np.uint8)).save(f, quality=90)
    return _summary(p, s, rescue_amount(p, s), 1.0)
