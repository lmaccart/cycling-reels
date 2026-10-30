from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .config import Project
from .media import BIN, ffprobe_json, run, which
from .util import ReelsError, fmt_ts, log, now_iso, read_json, slugify, write_json

AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus"}
MAX_SECONDS = 20 * 60

# Signals in a video's title/description/licence that a track is free to use.
FREE_SIGNALS = [
    (re.compile(r"\bNCS\b|NoCopyrightSounds", re.I), "NCS release"),
    (re.compile(r"creative commons|\bcc[- ]?by\b", re.I), "Creative Commons"),
    (re.compile(r"youtube audio library", re.I), "YouTube Audio Library"),
    (re.compile(r"royalty[- ]free|copyright[- ]free", re.I), "says royalty/copyright free"),
    (re.compile(r"\bfree (?:to|for) (?:commercial )?use\b", re.I), "says free to use"),
    (re.compile(r"\bno ?copyright\b", re.I), "says no copyright"),
]
# Classic disclaimers on re-uploads of copyrighted songs — the opposite of free.
DISCLAIMERS = re.compile(r"infringement intended|all rights (?:go|belong|reserved) to|i do not own|i don'?t own", re.I)


def ytdlp_cmd() -> list[str]:
    try:
        import yt_dlp  # type: ignore  # noqa: F401
    except ImportError as e:
        raise ReelsError("yt-dlp isn't installed — run ./setup.sh (or: .venv/bin/pip install 'yt-dlp[default]')") from e
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress"]
    if which("node"):  # YouTube needs a JavaScript runtime; Node is already here for Remotion
        cmd += ["--js-runtimes", "node"]
    ff = which("ffmpeg")
    if ff:
        cmd += ["--ffmpeg-location", str(Path(ff).resolve().parent)]
    return cmd


def ytdlp_error(proc: subprocess.CompletedProcess, source: str = "youtube") -> str:
    tail = "\n".join((proc.stderr or proc.stdout or "").strip().splitlines()[-6:])
    msg = f"download failed:\n{tail}"
    if re.search(r"youtu\.?be|ytsearch", source, re.I):
        msg += "\nYouTube changes often — updating yt-dlp usually fixes it: .venv/bin/pip install -U 'yt-dlp[default]'"
    return msg


def rights(info: dict) -> tuple[list[str], str | None]:
    """(free-use signals, caution) from yt-dlp metadata."""
    text = " ".join(str(info.get(k) or "") for k in ("title", "description", "channel", "uploader", "license"))
    signals: list[str] = []
    lic = str(info.get("license") or "")
    if "creative commons" in lic.lower():
        signals.append("Creative Commons licence on YouTube")
    for rx, label in FREE_SIGNALS:
        if rx.search(text) and label not in signals and not (label == "Creative Commons" and signals):
            signals.append(label)
    caution = None
    if DISCLAIMERS.search(text):
        caution = "uploader disclaimer ('no infringement intended' / 'I don't own this') — almost certainly copyrighted"
        signals = [s for s in signals if s != "says no copyright"]
    return signals, caution


def detect_credit(info: dict) -> str | None:
    """Pulls the attribution line free-music channels ask for (e.g. NCS's 'Song: …')."""
    desc = str(info.get("description") or "")
    song = provider = None
    for line in (ln.strip() for ln in desc.splitlines()):
        if not line:
            continue
        m = re.match(r"(?i)^(?:song|track|music)\s*[:\-–—]\s*(.+)$", line)
        if m and not song and "http" not in m.group(1):
            song = m.group(1).strip()
        m2 = re.match(r"(?i)^music (?:provided|promoted|supported) by\s+(.+)$", line)
        if m2 and not provider:
            provider = re.sub(r"https?://\S+", "", m2.group(1)).strip(" :-–—")
    if song:
        return f"Music: {song}" + (f" — provided by {provider}" if provider else "")
    if "creative commons" in str(info.get("license") or "").lower():
        who = info.get("artist") or info.get("channel") or info.get("uploader") or "unknown"
        return f"Music: {info.get('track') or info.get('title')} by {who} (CC BY)"
    return None


def summarize(info: dict) -> dict:
    signals, caution = rights(info)
    vid = info.get("id")
    return {
        "title": info.get("title") or "",
        "channel": info.get("channel") or info.get("uploader") or "",
        "duration": info.get("duration"),
        "views": info.get("view_count"),
        "url": info.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid}" if vid else None),
        "license": info.get("license"),
        "freeUse": signals,
        "caution": caution,
        "credit": detect_credit(info),
    }


def search(project: Project, query: str, *, limit: int = 8, free_only: bool = False) -> list[dict]:
    limit = max(1, min(int(limit), 20))
    cmd = ytdlp_cmd() + ["--skip-download", "--dump-json", "--ignore-errors", f"ytsearch{limit}:{query}"]
    proc = run(cmd, check=False, timeout=300)
    results = []
    for line in (proc.stdout or "").splitlines():
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue
        results.append(summarize(info))
    if not results and proc.returncode != 0:
        raise ReelsError(ytdlp_error(proc))
    if free_only:
        results = [r for r in results if r["freeUse"] and not r["caution"]]
    return results


def _views(n: Any) -> str:
    if not isinstance(n, (int, float)):
        return ""
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"{n / div:.1f}".rstrip("0").rstrip(".") + f"{suffix} views"
    return f"{int(n)} views"


def print_results(results: list[dict]) -> None:
    if not results:
        log("no results")
        return
    for i, r in enumerate(results, 1):
        meta = "  ·  ".join(x for x in (r["channel"], fmt_ts(r["duration"])[:-2] if r["duration"] else "", _views(r["views"])) if x)
        flag = f"   free: {', '.join(r['freeUse'])}" if r["freeUse"] else "   rights: unknown"
        log(f"{i:>2}  {r['title']}  ·  {meta}{flag}")
        if r["caution"]:
            log(f"    ⚠ {r['caution']}")
        log(f"    {r['url']}")


def _unique_slug(project: Project, base: str) -> str:
    slug, n = base or "track", 2
    while (project.music_dir / f"{slug}.mp3").exists() or (project.music_dir / f"{slug}.json").exists():
        slug, n = f"{base}-{n}", n + 1
    return slug


def suggest_start(path: Path, reel_seconds: float = 15.0) -> float:
    """Skips quiet intros: start of the loudest ~15 s stretch, nudged back to where it kicks in."""
    import numpy as np

    proc = subprocess.run(
        [BIN["ffmpeg"], "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"],
        capture_output=True,
    )
    x = np.frombuffer(proc.stdout, dtype=np.float32)
    hop = 4000  # 0.5 s at 8 kHz
    n = len(x) // hop
    win = 30  # 15 s
    if proc.returncode != 0 or n <= win + 4:
        return 0.0
    rms = np.sqrt(np.mean(x[: n * hop].reshape(n, hop) ** 2, axis=1) + 1e-12)
    csum = np.concatenate([[0.0], np.cumsum(rms)])
    scores = csum[win:] - csum[:-win]
    # Earliest window that's nearly as loud as the loudest: where the big section begins.
    i = int(np.argmax(scores >= 0.95 * scores.max()))
    lo, hi = max(1, i - 8), min(n - 1, i + 2)
    k = lo + int(np.argmax(rms[lo : hi + 1] - rms[lo - 1 : hi]))
    start = k * 0.5
    duration = n * 0.5
    if start + reel_seconds > duration:
        start = max(0.0, duration - reel_seconds)
    return 0.0 if start < 2 else round(start * 2) / 2


BEAT_SR, BEAT_FFT, BEAT_HOP = 11025, 1024, 128  # onset frames every ~11.6 ms
BEAT_LAG = 0.074  # frame start → onset time, calibrated on synthetic click tracks (selftest re-checks it)


def _onset_envelope(path: Path):
    """Spectral flux of log-compressed magnitudes: spikes where drums and notes hit."""
    import numpy as np

    proc = subprocess.run(
        [BIN["ffmpeg"], "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(BEAT_SR), "-f", "f32le", "-"],
        capture_output=True,
    )
    x = np.frombuffer(proc.stdout, dtype=np.float32)
    if proc.returncode != 0 or len(x) < BEAT_SR * 4:
        return np.zeros(0, dtype=np.float32)
    view = np.lib.stride_tricks.sliding_window_view(x, BEAT_FFT)[::BEAT_HOP]
    win = np.hanning(BEAT_FFT).astype(np.float32)
    flux = np.zeros(len(view), dtype=np.float32)
    prev = None
    for s in range(0, len(view), 2048):
        mag = np.log1p(100.0 * np.abs(np.fft.rfft(view[s : s + 2048] * win, axis=1))).astype(np.float32)
        stacked = mag if prev is None else np.vstack([prev, mag])
        d = np.maximum(np.diff(stacked, axis=0), 0).mean(axis=1)
        flux[s + len(mag) - len(d) : s + len(mag)] = d  # the very first frame has nothing before it
        prev = mag[-1:]
    # remove the slow loudness trend so only the hits stand out
    k = int(0.5 * BEAT_SR / BEAT_HOP)
    trend = np.convolve(flux, np.ones(k) / k, mode="same")
    onset = np.maximum(flux - trend, 0)
    sd = onset.std()
    return onset / sd if sd > 0 else onset


def detect_beats(path: Path) -> dict | None:
    """Tempo and beat times (seconds into the track). Autocorrelation picks the tempo,
    then dynamic programming lays a steady beat grid over the onsets (Ellis 2007)."""
    import numpy as np

    onset = _onset_envelope(path)
    n = len(onset)
    fr = BEAT_SR / BEAT_HOP
    if n < fr * 4 or not onset.any():
        return None
    g = np.exp(-0.5 * (np.arange(-4, 5) / 1.5) ** 2)  # widen the hits a little so whole-frame lags don't miss them
    o = np.convolve(onset, g / g.sum(), mode="same")
    o -= o.mean()
    spec = np.fft.rfft(o, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:n]
    lo, hi = int(np.ceil(fr * 60 / 180)), int(fr * 60 / 60)  # 60–180 bpm, leaning towards ~115
    lags = np.arange(lo, hi + 1)
    bpms = 60 * fr / lags
    score = ac[lags] * np.exp(-0.5 * np.log2(bpms / 115.0) ** 2)
    i = int(np.argmax(score))
    period = float(lags[i])
    if 0 < i < len(lags) - 1:  # parabolic refinement
        a, b, c = score[i - 1], score[i], score[i + 1]
        den = a - 2 * b + c
        if den != 0:
            period += 0.5 * (a - c) / den

    w = np.arange(-int(period), int(period) + 1)
    local = np.convolve(onset, np.exp(-0.5 * (w * 32.0 / period) ** 2), mode="same")
    offsets = np.arange(-int(round(2 * period)), -int(round(period / 2)) + 1)
    cost = -100.0 * np.log(-offsets / period) ** 2
    cum = local.astype(np.float64).copy()
    back = np.full(n, -1)
    for t in range(n):
        z = t + offsets
        ok = z >= 0
        if not ok.any():
            continue
        cand = cum[z[ok]] + cost[ok]
        j = int(np.argmax(cand))
        if cand[j] > 0:
            cum[t] = local[t] + cand[j]
            back[t] = z[ok][j]
    peaks = np.flatnonzero((cum[1:-1] >= cum[:-2]) & (cum[1:-1] > cum[2:])) + 1
    if not len(peaks):
        return None
    last = int(peaks[cum[peaks] >= 0.5 * np.median(cum[peaks])].max())
    beats = [last]
    while back[beats[-1]] >= 0:
        beats.append(int(back[beats[-1]]))
    beats = np.array(beats[::-1])
    strong = local[beats] >= 0.5 * np.sqrt(np.mean(local[beats] ** 2))
    if strong.any():
        beats = beats[np.argmax(strong) : len(strong) - np.argmax(strong[::-1])]
    times = beats / fr + BEAT_LAG
    return {"bpm": round(60 * fr / period, 1), "beats": [round(float(t), 3) for t in times if t >= 0]}


def ensure_beats(project: Project, path: Path, meta: dict | None) -> dict | None:
    """Beat grid for a track, cached in its music/<slug>.json record."""
    if meta and meta.get("beats"):
        return {"bpm": meta.get("bpm"), "beats": meta["beats"]}
    found = detect_beats(path)
    if found and meta and meta.get("slug") and (project.music_dir / f"{meta['slug']}.json").exists():
        current = read_json(project.music_dir / f"{meta['slug']}.json")
        current.update(found)
        write_json(project.music_dir / f"{meta['slug']}.json", current)
        meta.update(found)
    return found


def _probe_audio(path: Path) -> tuple[float, dict]:
    data = ffprobe_json(path)
    fmt = data.get("format") or {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    if not any(s.get("codec_type") == "audio" for s in data.get("streams") or []):
        raise ReelsError(f"{path.name} has no audio")
    return float(fmt.get("duration") or 0), tags


def add(
    project: Project,
    source: str,
    *,
    name: str | None = None,
    start: float | None = None,
    credit: str | None = None,
    allow_long: bool = False,
    instagram: str | None = None,
) -> dict:
    """Downloads a track from YouTube (or another site yt-dlp supports) as MP3 into
    music/, or imports a local audio file, and records where it came from.
    `instagram` marks a trending song and says how to find it in Instagram's library (title +
    artist, or an Instagram audio id). It's mixed into reels like any track, or with config
    audio.attachSongs, left out of the upload and attached from Instagram's library when scheduled."""
    project.music_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        audio = tdp / "track.mp3"
        if re.match(r"(?i)^https?://", source):
            cmd = ytdlp_cmd() + [
                "--no-playlist", "-f", "bestaudio/best", "-x", "--audio-format", "mp3", "--audio-quality", "192K",
                "--write-info-json", "-o", str(tdp / "track.%(ext)s"),
            ]  # fmt: skip
            if not allow_long:
                cmd += ["--match-filters", f"duration<=?{MAX_SECONDS}"]
            proc = run(cmd + [source], check=False, timeout=900)
            if not audio.exists():
                if "does not pass filter" in (proc.stdout or "") + (proc.stderr or ""):
                    raise ReelsError(f"that's longer than {MAX_SECONDS // 60} minutes (a mix?) — add --allow-long if you really want it")
                raise ReelsError(ytdlp_error(proc, source))
            info_path = tdp / "track.info.json"
            info = read_json(info_path) if info_path.exists() else {}
            s = summarize(info)
            meta: dict[str, Any] = {
                "title": info.get("track") or s["title"] or "track",
                "artist": info.get("artist") or info.get("creator") or s["channel"],
                "channel": s["channel"],
                "source": str(info.get("extractor_key") or "web").lower(),
                "url": s["url"] or source,
                "license": s["license"],
                "freeUse": s["freeUse"],
                "caution": s["caution"],
                "credit": s["credit"],
                "description": str(info.get("description") or "")[:800],
            }
        else:
            src = Path(source).expanduser()
            if not src.is_file():
                raise ReelsError(f"not a URL or an audio file: {source}")
            _, tags = _probe_audio(src)
            run([BIN["ffmpeg"], "-v", "error", "-y", "-i", src, "-vn", "-map", "0:a:0", "-c:a", "libmp3lame", "-b:a", "192k", audio])
            meta = {
                "title": tags.get("title") or src.stem,
                "artist": tags.get("artist") or tags.get("album_artist"),
                "channel": None,
                "source": "file",
                "url": None,
                "license": None,
                "freeUse": [],
                "caution": None,
                "credit": None,
                "originalFile": src.name,
            }
        slug = _unique_slug(project, slugify(name or meta["title"], 40))
        dest = project.music_dir / f"{slug}.mp3"
        shutil.move(str(audio), dest)

    duration, _ = _probe_audio(dest)
    meta.update(
        {
            "slug": slug,
            "file": project.rel(dest),
            "duration": round(duration, 2),
            "suggestedStart": suggest_start(dest),
            "addedAt": now_iso(),
            "usedIn": [],
        }
    )
    if start is not None:
        meta["defaultStart"] = float(start)
    if credit:
        meta["credit"] = credit.strip()
    if instagram:
        meta["instagram"] = {"audio": instagram.strip()}
        meta["credit"] = None  # Instagram shows the song on the reel itself
    beats = detect_beats(dest)
    if beats:
        meta.update(beats)
    write_json(project.music_dir / f"{slug}.json", meta)
    return meta


def describe_track(meta: dict) -> str:
    bits = [f"{meta['slug']}", fmt_ts(meta.get("duration") or 0)[:-2]]
    title = " — ".join(x for x in (meta.get("artist"), meta.get("title")) if x)
    if title:
        bits.append(title)
    return "  ·  ".join(bits)


def list_tracks(project: Project) -> list[dict]:
    if not project.music_dir.exists():
        return []
    tracks, known = [], set()
    for mp in sorted(project.music_dir.glob("*.json")):
        try:
            meta = read_json(mp)
        except Exception:  # noqa: BLE001
            continue
        if not project.abs(meta.get("file", "")).is_file():
            continue
        known.add(Path(meta["file"]).name)
        tracks.append(meta)
    for f in sorted(project.music_dir.iterdir()):
        if f.suffix.lower() in AUDIO_EXT and f.name not in known:
            tracks.append({"slug": f.stem, "file": project.rel(f), "title": f.stem, "untracked": True})
    return tracks


def print_tracks(tracks: list[dict], attach: bool = False) -> None:
    if not tracks:
        log("no music yet — find some with ./reels music search \"<mood>\" --free, then ./reels music add <url>")
        return
    for t in tracks:
        if t.get("untracked"):
            log(f"  {t['slug']}  ·  {t['file']}  (dropped in by hand — no source/credit recorded)")
            continue
        ig = t.get("instagram")
        start = 0 if ig and attach else t.get("defaultStart", t.get("suggestedStart") or 0)
        if ig:
            rights_txt = "Instagram audio (licensed, attached when scheduled)" if attach else "trending song (mixed in)"
        elif t.get("freeUse"):
            rights_txt = f"free: {', '.join(t['freeUse'])}"
        else:
            rights_txt = "rights: confirmed" if t.get("rightsOk") else "rights: unknown"
        used = f"used {len(t.get('usedIn') or [])}×" if t.get("usedIn") else "unused"
        bpm = f"  ·  {t['bpm']:g} bpm" if t.get("bpm") else ""
        log(f"  {describe_track(t)}{bpm}  ·  starts {fmt_ts(start)[:-2]}  ·  {rights_txt}  ·  {used}")
        if ig:
            log(f"      instagram: {ig.get('id') or ig.get('audio')}")
        if t.get("caution"):
            log(f"      ⚠ {t['caution']}")
        if t.get("credit"):
            log(f"      credit: {t['credit']}")
        if t.get("url"):
            log(f"      {t['url']}")


def resolve_track(project: Project, ref: Any) -> tuple[Path, dict | None]:
    """Track by slug ("night-drive"), file name, or path."""
    ref = str(ref or "").strip()
    if not ref:
        raise ReelsError("music needs a track (slug from ./reels music list)")
    path = None
    for cand in (project.abs(ref), project.music_dir / ref):
        if cand.is_file():
            path = cand
            break
    if path is None:
        meta_path = project.music_dir / f"{ref}.json"
        if meta_path.exists():
            path = project.abs(read_json(meta_path)["file"])
        else:
            hits = [f for f in project.music_dir.glob(f"{ref}.*") if f.suffix.lower() in AUDIO_EXT] if project.music_dir.exists() else []
            path = hits[0] if len(hits) == 1 else None
    if path is None or not path.is_file():
        raise ReelsError(f"music track '{ref}' not found — see ./reels music list, or add one with ./reels music add <youtube-url>")
    meta_path = project.music_dir / f"{path.stem}.json"
    return path, (read_json(meta_path) if meta_path.exists() else None)


def record_usage(project: Project, meta: dict | None, plan_slug: str) -> None:
    if not meta or not meta.get("slug"):
        return
    path = project.music_dir / f"{meta['slug']}.json"
    if not path.exists():
        return
    current = read_json(path)
    used = [u for u in current.get("usedIn") or [] if u != plan_slug] + [plan_slug]
    current["usedIn"] = used
    write_json(path, current)


def release_usage(project: Project, plan_slug: str) -> None:
    """Drops a reel from every track's usedIn (discard, or a re-render with another song)."""
    for path in project.music_dir.glob("*.json") if project.music_dir.exists() else []:
        try:
            current = read_json(path)
        except ValueError:
            continue
        if isinstance(current, dict) and plan_slug in (current.get("usedIn") or []):
            current["usedIn"] = [u for u in current["usedIn"] if u != plan_slug]
            write_json(path, current)


def parse_time(value: str | None) -> float | None:
    if value in (None, ""):
        return None
    m = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", str(value).strip())
    if not m:
        raise ReelsError(f"bad time {value!r} — use seconds (83) or m:ss (1:23)")
    return int(m.group(1) or 0) * 60 + float(m.group(2))
