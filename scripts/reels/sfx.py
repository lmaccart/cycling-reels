from __future__ import annotations

import difflib
import re
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

from .config import Project
from .library import load_asset
from .media import BIN, ffprobe_json, run
from .util import ReelsError, log, now_iso, read_json, slugify, warn, write_json

CATEGORIES = ["whoosh", "swish", "riser", "hit", "pop", "click", "shutter", "bell", "scratch", "stop", "nat", "other"]
SR = 48000
TARGET_LUFS = -20.0  # integrated loudness for sounds long enough to measure it
CEILING_DBTP = -1.5  # true-peak ceiling for those (gain is reduced rather than clipped)
PEAK_DBFS = -6.0  # shorter sounds: peak-normalized instead
LUFS_MIN_SECONDS = 1.5
SILENCE_DB = -50.0  # relative to the sound's own peak, when trimming leading/trailing silence
MAX_BYTES = 60 << 20
BANNED = re.compile(r"ai[- ]generated|elevenlabs|eleven labs|generated (?:by|with) ai|text[- ]to[- ]sound", re.I)


def sfx_dir(project: Project) -> Path:
    return project.root / "sfx"


def list_sfx(project: Project) -> list[dict]:
    d = sfx_dir(project)
    out = []
    for p in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            out.append(read_json(p))
        except Exception:  # noqa: BLE001
            warn(f"skipping unreadable {p.name}")
    return sorted(out, key=lambda m: (CATEGORIES.index(m["category"]) if m.get("category") in CATEGORIES else 99, m["slug"]))


def resolve_sfx(project: Project, name: str) -> tuple[Path, dict]:
    """(wav path, record) for a sound slug. Unknown names list the close matches."""
    d = sfx_dir(project)
    slug = slugify(name or "")
    meta_path = d / f"{slug}.json"
    if slug and meta_path.exists() and (d / f"{slug}.wav").exists():
        return d / f"{slug}.wav", read_json(meta_path)
    slugs = [m["slug"] for m in list_sfx(project)]
    close = difflib.get_close_matches(slug, slugs, n=5, cutoff=0.4) or [s for s in slugs if slug and slug.split("-")[0] in s][:5]
    hint = f" — close: {', '.join(close)}" if close else f" — available: {', '.join(slugs) or 'none yet (./reels sfx add …)'}"
    raise ReelsError(f"no sound effect '{name}'{hint}")


def _decode(path: Path, *, ss: float | None = None, t: float | None = None, af: str | None = None, ch: int | None = None):
    """Float32 samples (n, ch) at 48 kHz from the first audio stream."""
    import numpy as np

    if ch is None:
        info = ffprobe_json(path)
        a = [s for s in info.get("streams") or [] if s.get("codec_type") == "audio"]
        if not a:
            raise ReelsError(f"{path.name} has no audio")
        ch = 1 if int(a[0].get("channels") or 1) == 1 else 2
    cmd = [BIN["ffmpeg"], "-nostdin", "-v", "error"]
    if ss is not None:
        cmd += ["-ss", f"{ss:.3f}"]
    if t is not None:
        cmd += ["-t", f"{t:.3f}"]
    cmd += ["-i", str(path), "-map", "0:a:0", "-vn", "-ac", str(ch), "-ar", str(SR)]
    if af:
        cmd += ["-af", af]
    proc = subprocess.run(cmd + ["-f", "f32le", "-"], capture_output=True)
    if proc.returncode != 0:
        raise ReelsError(f"couldn't read audio from {path.name}:\n{proc.stderr.decode(errors='replace')[-600:]}")
    x = np.frombuffer(proc.stdout, dtype=np.float32)
    return x.reshape(-1, ch).copy(), ch


def _write_wav(path: Path, x, ch: int) -> None:
    import numpy as np

    pcm = (np.clip(x, -1, 1) * 32767).round().astype("<i2")
    proc = subprocess.run(
        [BIN["ffmpeg"], "-nostdin", "-v", "error", "-y", "-f", "s16le", "-ar", str(SR), "-ac", str(ch), "-i", "-", "-c:a", "pcm_s16le", str(path)],
        input=pcm.tobytes(), capture_output=True,
    )  # fmt: skip
    if proc.returncode != 0:
        raise ReelsError(f"couldn't write {path.name}:\n{proc.stderr.decode(errors='replace')[-600:]}")


def measure(path: Path) -> tuple[float | None, float]:
    """(integrated LUFS or None if too short/quiet to measure, true peak dBTP)."""
    proc = run(["ffmpeg", "-nostdin", "-hide_banner", "-i", path, "-af", "ebur128=peak=true", "-f", "null", "-"], check=False)
    tail = (proc.stderr or "").rsplit("Summary:", 1)[-1]
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", tail)
    p = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", tail)
    lufs = float(i.group(1)) if i and float(i.group(1)) > -69 else None
    peak = float(p.group(1)) if p and p.group(1) != "-inf" else -120.0
    return lufs, peak


def _trim_silence(x, pad: float = 0.005):
    import numpy as np

    mono = np.abs(x).max(axis=1)
    peak = float(mono.max()) if len(mono) else 0.0
    if peak <= 0:
        raise ReelsError("that sound is silent")
    idx = np.flatnonzero(mono >= peak * 10 ** (SILENCE_DB / 20))
    p = int(pad * SR)
    return x[max(0, idx[0] - p) : idx[-1] + 1 + p]


def _fade(x, ms_in: float = 2, ms_out: float = 15):
    import numpy as np

    n = len(x)
    a, b = min(n // 2, int(ms_in * SR / 1000)), min(n // 2, int(ms_out * SR / 1000))
    if a:
        x[:a] *= np.linspace(0, 1, a, dtype=np.float32)[:, None]
    if b:
        x[-b:] *= np.linspace(1, 0, b, dtype=np.float32)[:, None]
    return x


def _normalize(x, tmp: Path, ch: int):
    """Scales x so it sits at a predictable level. Returns (samples, mode)."""
    import numpy as np

    dur = len(x) / SR
    _write_wav(tmp, x, ch)
    lufs, tp = measure(tmp)
    if lufs is not None and dur >= LUFS_MIN_SECONDS:
        db = min(TARGET_LUFS - lufs, CEILING_DBTP - tp)
        mode = "lufs"
    else:
        peak = float(np.abs(x).max())
        db = PEAK_DBFS - 20 * np.log10(max(peak, 1e-9))
        mode = "peak"
    return x * np.float32(10 ** (db / 20)), mode


def _fetch(url: str, dest: Path) -> Path:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (reels sfx)"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            n = 0
            while chunk := r.read(1 << 16):
                n += len(chunk)
                if n > MAX_BYTES:
                    raise ReelsError("download is over 60 MB — not a sound effect?")
                f.write(chunk)
    except OSError as e:
        raise ReelsError(f"couldn't download {url}: {e}") from e
    return dest


def _parse_pair(s: str | None) -> tuple[float | None, float | None]:
    if not s:
        return None, None
    try:
        a, b = (v.strip() for v in s.split(","))
        return (float(a) if a else None), (float(b) if b else None)
    except ValueError as e:
        raise ReelsError("--trim wants START,END in seconds, e.g. 0.1,1.4 (leave one empty to keep that side)") from e


def add(
    project: Project,
    source: str | None,
    *,
    name: str,
    category: str,
    license: str | None = None,
    source_page: str | None = None,
    author: str | None = None,
    trim: str | None = None,
    tags: list[str] | None = None,
    from_asset: str | None = None,
    at: float | None = None,
    length: float | None = None,
    clean: bool = False,
) -> dict:
    """Adds a sound to sfx/: converted to 48 kHz WAV, silence trimmed, level-normalized, with a record of where it came from."""
    if category not in CATEGORIES:
        raise ReelsError(f"category must be one of: {', '.join(CATEGORIES)}")
    slug = slugify(name)
    if not slug:
        raise ReelsError("--name needs a slug like whoosh-short")
    d = sfx_dir(project)
    if (d / f"{slug}.wav").exists() or (d / f"{slug}.json").exists():
        raise ReelsError(f"sfx/{slug} already exists — pick another --name (or delete the old files first)")
    extra: dict[str, Any] = {}
    tag_list = [t.strip() for t in (tags or []) if t.strip()]
    d.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        if from_asset:
            if at is None or length is None or length <= 0:
                raise ReelsError("--from needs --at <seconds> and --for <seconds>")
            asset = load_asset(project, from_asset)
            media = project.abs(asset.get("media") or "")
            if not media.exists():
                raise ReelsError(f"{asset['id']} has no media file to cut from")
            if category == "other":
                category = "nat"
            af = "highpass=f=90,afftdn=nr=10:nf=-45" if clean else None
            x, ch = _decode(media, ss=at, t=length, af=af)
            if not len(x):
                raise ReelsError(f"nothing to cut at {at}s in {asset['id']}")
            license, source_page, author = license or "own footage", source_page or asset["id"], author or "own footage"
            extra = {"sourceAsset": asset["id"], "in": round(at, 3), "out": round(at + len(x) / SR, 3)}
            if clean:
                extra["clean"] = True
            x = _fade(x)  # exact window as asked: no silence trimming
        else:
            if not source:
                raise ReelsError("give a file or URL to add (or --from <asset-id> --at S --for S)")
            if not license or not source_page:
                raise ReelsError("--license and --source (the sound's page URL) are required — only CC0 / Pixabay-licence sounds belong here")
            if not author:
                warn("no --author given — record who made it if the page says")
            blob = f"{name} {source_page} {author or ''} {' '.join(tag_list)}"
            if BANNED.search(blob):
                raise ReelsError("AI-generated sounds aren't allowed in the library")
            if re.match(r"(?i)^https?://", source):
                path = _fetch(source, tdp / "download")
            else:
                path = Path(source).expanduser()
                if not path.exists():
                    raise ReelsError(f"no such file: {source}")
            t0, t1 = _parse_pair(trim)
            x, ch = _decode(path, ss=t0, t=(t1 - (t0 or 0)) if t1 is not None else None)
            if not len(x):
                raise ReelsError("nothing left after --trim")
            x = _fade(_trim_silence(x))
        x, mode = _normalize(x, tdp / "n.wav", ch)
        wav = d / f"{slug}.wav"
        _write_wav(wav, x, ch)
    lufs, tp = measure(wav)
    meta: dict[str, Any] = {
        "slug": slug,
        "name": name,
        "category": category,
        "file": f"sfx/{slug}.wav",
        "duration": round(len(x) / SR, 3),
        "channels": ch,
        "loudness": lufs,
        "peak": round(tp, 1),
        "normalized": "-20 LUFS / -1.5 dBTP ceiling" if mode == "lufs" else "peak -6 dBFS",
        "source": source_page,
        "author": author,
        "license": license,
        "addedAt": now_iso(),
        "tags": tag_list,
        **extra,
    }
    write_json(d / f"{slug}.json", meta)
    write_readme(project)
    return meta


def write_readme(project: Project) -> None:
    items = list_sfx(project)
    lines = [
        "Free sound effects for reels, used with `./reels sfx`. Human-made only, CC0 or Pixabay Content License,",
        "with source page, author and license recorded in each <slug>.json. Nothing here is AI-generated.",
        "",
        "    ./reels sfx list [--category whoosh]",
        "    ./reels sfx add <file-or-url> --name whoosh-short --category whoosh --license CC0-1.0 --source <page-url> --author \"<name>\" [--trim 0.1,1.4] [--tags a,b]",
        "    ./reels sfx add --from <asset-id> --at 12.5 --for 1.2 --name bike-chain [--clean]   (cut from your own footage; --clean = highpass + denoise for wind)",
        "",
        "Every sound is 48 kHz 16-bit WAV with leading/trailing silence trimmed and a short fade, and sits at a predictable",
        "level so they mix evenly: sounds of 1.5 s or more are set to -20 LUFS integrated (gain reduced if that would pass",
        "a -1.5 dBTP true-peak ceiling); shorter ones can't be measured reliably, so they are peak-normalized to -6 dBFS.",
        "Sounds cut from your own footage (category nat) are normalized the same way but not silence-trimmed.",
        "Reels mix them in at config `sfx.gain` (default 0.7).",
        "",
        "| slug | category | length | LUFS | peak | license | author | source |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for m in items:
        lufs = "" if m.get("loudness") is None else f"{m['loudness']:.1f}"
        lines.append(
            f"| {m['slug']} | {m['category']} | {m['duration']:.2f}s | {lufs} | {m['peak']} | {m['license']} | {m.get('author') or ''} | {m['source']} |"
        )
    (sfx_dir(project) / "README.md").write_text("\n".join(lines) + "\n")


def print_sfx(items: list[dict]) -> None:
    if not items:
        log("no sound effects yet — ./reels sfx add …")
        return
    for m in items:
        lufs = "" if m.get("loudness") is None else f"{m['loudness']:.1f} LUFS  "
        log(f"{m['slug']:<20} {m['category']:<8} {m['duration']:>5.2f}s  {lufs}{m['license']}  ·  {m.get('author') or ''}")
