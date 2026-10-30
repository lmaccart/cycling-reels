from __future__ import annotations

import tempfile
from math import ceil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import Project
from .images import open_image
from .media import extract_frame
from .sphere import describe as describe_sphere
from .sphere import extract_equirect
from .util import clamp, fmt_ts

_FONTS_BOLD = [
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
]
_FONTS_REGULAR = [
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
]


def load_font(size: int, bold: bool = True) -> ImageFont.ImageFont:
    for cand in _FONTS_BOLD if bold else _FONTS_REGULAR:
        if Path(cand).exists():
            try:
                return ImageFont.truetype(cand, size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def build_sheet(
    tiles: list[tuple[str, Image.Image]],
    out: Path,
    title: str,
    subtitle: str = "",
    cols: int = 4,
    tile_w: int = 390,
    guide_aspect: float | None = None,
    yaw_ticks: bool = False,
) -> Path:
    """Grid of labelled frames. guide_aspect draws a centred crop outline (e.g. 9/16);
    yaw_ticks labels unwrapped 360 frames with yaw angles along the bottom."""
    first = tiles[0][1]
    tile_h = round(tile_w * first.height / first.width)
    gutter, header = 10, 72 if subtitle else 50
    cols = max(1, min(cols, len(tiles)))
    rows = ceil(len(tiles) / cols)
    f_title, f_sub, f_label = load_font(26), load_font(18, bold=False), load_font(20)
    probe_draw = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    text_w = max(probe_draw.textlength(title, font=f_title), probe_draw.textlength(subtitle, font=f_sub) if subtitle else 0)
    width = max(cols * tile_w + (cols + 1) * gutter, int(text_w) + 2 * gutter)
    height = header + rows * (tile_h + gutter) + gutter
    sheet = Image.new("RGB", (width, height), (16, 16, 16))
    d = ImageDraw.Draw(sheet)
    d.text((gutter, 12), title, fill=(240, 240, 240), font=f_title)
    if subtitle:
        d.text((gutter, 44), subtitle, fill=(175, 175, 175), font=f_sub)

    for i, (label, im) in enumerate(tiles):
        r, c = divmod(i, cols)
        x = gutter + c * (tile_w + gutter)
        y = header + r * (tile_h + gutter)
        sheet.paste(im.resize((tile_w, tile_h), Image.LANCZOS), (x, y))
        if guide_aspect and tile_w / tile_h > guide_aspect + 0.02:
            gw = tile_h * guide_aspect
            gx = x + (tile_w - gw) / 2
            for lx in (gx, gx + gw):
                d.line([(lx, y), (lx, y + tile_h)], fill=(0, 0, 0), width=3)
                d.line([(lx, y), (lx, y + tile_h)], fill=(228, 255, 26), width=1)
        if yaw_ticks:
            f_tick = load_font(16)
            for yaw in (-180, -90, 0, 90, 180):
                tx = x + (yaw + 180) / 360 * (tile_w - 1)
                d.line([(tx, y + tile_h - 14), (tx, y + tile_h)], fill=(255, 255, 255), width=2)
                txt = "±180" if abs(yaw) == 180 else f"{yaw:+d}" if yaw else "0"
                tw_ = d.textlength(txt, font=f_tick)
                lx = min(max(tx - tw_ / 2, x + 2), x + tile_w - tw_ - 2)
                d.rectangle([lx - 3, y + tile_h - 36, lx + tw_ + 3, y + tile_h - 16], fill=(0, 0, 0))
                d.text((lx, y + tile_h - 35), txt, fill=(255, 225, 77), font=f_tick)
        tw = d.textlength(label, font=f_label)
        d.rectangle([x, y, x + tw + 14, y + 28], fill=(0, 0, 0))
        d.text((x + 7, y + 3), label, fill=(255, 255, 255), font=f_label)

    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, quality=86)
    return out


def sheet_times(duration: float, start: float = 0.0, end: float | None = None, count: int | None = None) -> list[float]:
    end = duration if end is None else min(end, duration)
    start = clamp(start, 0, max(0.0, end - 0.1))
    span = max(0.1, end - start)
    if count is None:
        count = int(clamp(round(span / 4), 6, 16)) if span <= 120 else int(clamp(round(span / 8), 16, 48))
    count = int(clamp(count, 1, 64))
    return [round(start + (i + 0.5) * span / count, 2) for i in range(count)]


def audio_line(asset: dict) -> str:
    probe = asset.get("probe") or {}
    if not probe.get("hasAudio"):
        return "audio: none"
    audio = asset.get("audio") or {}
    vol = (audio.get("volume") or {}).get("meanDb")
    vol_txt = f", mean {vol:.0f} dB" if isinstance(vol, (int, float)) else ""
    secs = audio.get("speechSeconds")
    if secs is None:
        return f"audio: present{vol_txt}"
    if secs < 1.5:
        return f"audio: no speech detected{vol_txt}"
    ranges = audio.get("speechRanges") or []
    shown = ", ".join(f"{fmt_ts(s)}-{fmt_ts(e)}" for s, e in ranges[:4]) + (" …" if len(ranges) > 4 else "")
    return f"audio: speech ~{secs:.0f}s ({shown}){vol_txt}"


def make_video_sheets(
    project: Project,
    asset: dict,
    *,
    start: float = 0.0,
    end: float | None = None,
    count: int | None = None,
    suffix: str = "",
) -> list[Path]:
    probe = asset["probe"]
    src = project.abs(asset["media"])
    times = sheet_times(probe["duration"], start, end, count)
    sph = probe.get("sphere")
    portrait = probe["height"] > probe["width"] and not sph
    if sph:
        cols, per_sheet, tile_w = 3, 12, 520
    else:
        cols, per_sheet, tile_w = (6, 12, 255) if portrait else (4, 16, 390)
    kind = f"  ·  {describe_sphere(sph)}" if sph else f"  ·  {probe['width']}x{probe['height']}"
    title = (
        f"{asset['id']}{kind} @ {probe.get('fps') or '?'}fps"
        f"  ·  {fmt_ts(probe['duration'])}" + ("  ·  HDR" if probe.get("hdr") else "")
    )
    if sph:
        subtitle = audio_line(asset) + "   ·   whole sphere unwrapped: yaw 0 = camera front, ±180 = behind, middle row = horizon"
    else:
        subtitle = audio_line(asset) + ("" if portrait else "   ·   yellow lines = default 9:16 crop")

    sheets: list[Path] = []
    with tempfile.TemporaryDirectory() as td:
        frames: list[tuple[float, Path]] = []
        for i, t in enumerate(times):
            fp = Path(td) / f"f{i:03d}.jpg"
            at = min(t, max(0.0, probe["duration"] - 0.05))
            if sph:
                extract_equirect(src, at, fp, probe, width=tile_w * 2)
            else:
                extract_frame(src, at, fp, width=tile_w * 2, probe=probe)
            frames.append((t, fp))
        chunks = [frames[i : i + per_sheet] for i in range(0, len(frames), per_sheet)]
        for ci, chunk in enumerate(chunks):
            name = f"{asset['id']}{suffix}" + ("" if len(chunks) == 1 else f"-{ci + 1}") + ".jpg"
            tiles = []
            for j, (t, fp) in enumerate(chunk):
                with Image.open(fp) as im:
                    tiles.append((f"#{ci * per_sheet + j + 1}  {fmt_ts(t)}", im.convert("RGB")))
            sheets.append(
                build_sheet(
                    tiles,
                    project.sheets_dir / name,
                    title,
                    subtitle,
                    cols=cols,
                    tile_w=tile_w,
                    guide_aspect=None if (portrait or sph) else 9 / 16,
                    yaw_ticks=bool(sph),
                )
            )
    return sheets


def make_photo_sheet(project: Project, asset: dict) -> Path:
    im = open_image(project.abs(asset["media"]))
    probe = asset["probe"]
    sph = probe.get("sphere")
    im.thumbnail((1560, 780) if sph else (1200, 1200))
    if sph:
        title = f"{asset['id']}  ·  {describe_sphere(sph)} photo"
        subtitle = "whole sphere unwrapped: yaw 0 = camera front, ±180 = behind, middle row = horizon"
        return build_sheet([("360 photo", im)], project.sheets_dir / f"{asset['id']}.jpg", title, subtitle,
                           cols=1, tile_w=im.width, yaw_ticks=True)  # fmt: skip
    title = f"{asset['id']}  ·  photo {probe['width']}x{probe['height']}"
    return build_sheet([("photo", im)], project.sheets_dir / f"{asset['id']}.jpg", title, cols=1, tile_w=im.width)
