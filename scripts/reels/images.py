from __future__ import annotations

import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

from .media import which
from .util import ReelsError

_heif_ready: bool | None = None


def _enable_heif() -> bool:
    global _heif_ready
    if _heif_ready is None:
        try:
            import pillow_heif  # type: ignore

            pillow_heif.register_heif_opener()
            _heif_ready = True
        except ImportError:
            _heif_ready = False
    return _heif_ready


def open_image(path: Path) -> Image.Image:
    """Open any supported photo upright (EXIF orientation applied) as RGB."""
    path = Path(path)
    if path.suffix.lower() in (".heic", ".heif") and not _enable_heif():
        if which("sips"):  # macOS built-in converter
            with tempfile.TemporaryDirectory() as td:
                tmp = Path(td) / "converted.jpg"
                subprocess.run(["sips", "-s", "format", "jpeg", str(path), "--out", str(tmp)], capture_output=True, check=True)
                with Image.open(tmp) as im:
                    return ImageOps.exif_transpose(im).convert("RGB")
        raise ReelsError("HEIC photos need pillow-heif: .venv/bin/pip install pillow-heif")
    try:
        with Image.open(path) as im:
            return ImageOps.exif_transpose(im).convert("RGB")
    except Exception as e:  # noqa: BLE001 - Pillow raises many types
        raise ReelsError(f"{path.name}: can't open image ({e})") from e


def _exif_date(path: Path) -> str | None:
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            raw = exif.get_ifd(0x8769).get(36867) or exif.get(306)
    except Exception:  # noqa: BLE001
        return None
    return str(raw) if raw else None


def photo_info(path: Path) -> dict:
    im = open_image(path)
    info = {"width": im.width, "height": im.height, "created": _exif_date(path)}
    from .sphere import detect_photo

    sph = detect_photo(path, im.width, im.height)
    if sph:
        info["sphere"] = sph
    return info


def parse_date(value: str | None) -> datetime | None:
    """Accepts ISO, QuickTime and EXIF ("2026:10:19 14:03:22") style stamps."""
    if not value:
        return None
    m = re.match(r"(\d{4})[-:](\d{2})[-:](\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(value))
    if not m:
        m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", str(value))
        if not m2:
            return None
        y, mo, d = map(int, m2.groups())
        return datetime(y, mo, d)
    y, mo, d, h, mi, s = map(int, m.groups())
    try:
        return datetime(y, mo, d, h, mi, s)
    except ValueError:
        return None
