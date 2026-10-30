from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import Project
from .media import can_tonemap, has_filter, run, which
from .util import log

OK, BAD, NOTE = "✓", "✗", "!"


def _model_repo(name: str) -> str:
    try:
        from faster_whisper.utils import _MODELS  # type: ignore

        return _MODELS.get(name, name)
    except Exception:  # noqa: BLE001
        return name if "/" in name else f"Systran/faster-whisper-{name}"


def whisper_model_cached(name: str) -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    hit = try_to_load_from_cache(_model_repo(name), "model.bin")
    return isinstance(hit, str) and Path(hit).exists()


def doctor(project: Project, *, fix: bool = False) -> bool:
    ok = True

    def line(mark: str, msg: str) -> None:
        log(f"  {mark} {msg}")

    log(f"cycling-reels project: {project.root}")
    v = sys.version_info
    if v >= (3, 10):
        line(OK, f"python {v.major}.{v.minor}.{v.micro}")
    else:
        line(BAD, f"python {v.major}.{v.minor} — need 3.10+ (brew install python@3.12, then re-run ./setup.sh)")
        ok = False

    if which("ffmpeg") and which("ffprobe"):
        hdr_ok = can_tonemap()  # may switch to Homebrew's ffmpeg-full
        first = run(["ffmpeg", "-version"], check=False).stdout.splitlines()[:1]
        line(OK, (first[0] if first else "ffmpeg") + f"  ({which('ffmpeg')})")
        if not has_filter("libx264") and "libx264" not in run(["ffmpeg", "-hide_banner", "-encoders"], check=False).stdout:
            line(BAD, "this ffmpeg has no libx264 encoder — brew reinstall ffmpeg")
            ok = False
        if hdr_ok:
            line(OK, "HDR footage (iPhone/GoPro HDR) will be tone-mapped to SDR")
        else:
            line(NOTE, "ffmpeg can't tone-map HDR (no zimg) — HLG clips get an approximation. For accurate colour: brew install ffmpeg-full")
        if all(has_filter(f) for f in ("v360", "blend", "sendcmd")):
            line(OK, "360 video support (GoPro .360, 360 MP4s and photos)")
        else:
            line(BAD, "this ffmpeg lacks the v360/blend/sendcmd filters needed for 360 footage — brew reinstall ffmpeg")
            ok = False
    else:
        line(BAD, "ffmpeg/ffprobe missing — brew install ffmpeg")
        ok = False

    node = which("node")
    if node:
        ver = run(["node", "--version"], check=False).stdout.strip()
        major = int(ver.lstrip("v").split(".")[0] or 0) if ver else 0
        if major >= 18:
            line(OK, f"node {ver}")
        else:
            line(BAD, f"node {ver} — need 18+ (brew install node)")
            ok = False
    else:
        line(BAD, "node missing — brew install node")
        ok = False

    rdir = project.remotion_dir
    if (rdir / "node_modules" / ".bin" / "remotion").exists():
        line(OK, "Remotion template dependencies installed")
    elif fix and node:
        log("    installing Remotion dependencies…")
        subprocess.run(["npm", "install", "--no-audit", "--no-fund"], cwd=rdir, check=False)
        ok = (rdir / "node_modules" / ".bin" / "remotion").exists() and ok
    else:
        line(BAD, "Remotion dependencies missing — cd remotion && npm install")
        ok = False

    browser = os.environ.get("REELS_BROWSER") or project.cfg("render", "browserExecutable")
    if browser:
        line(OK if Path(browser).exists() else BAD, f"render browser: {browser}")
        ok = ok and Path(browser).exists()
    elif (rdir / "node_modules" / ".remotion").exists():
        line(OK, "Chrome Headless Shell downloaded for rendering")
    elif fix and (rdir / "node_modules" / ".bin" / "remotion").exists():
        log("    downloading Chrome Headless Shell for Remotion…")
        subprocess.run([str(rdir / "node_modules" / ".bin" / "remotion"), "browser", "ensure"], cwd=rdir, check=False)
    else:
        line(NOTE, "Remotion will download Chrome Headless Shell on the first render (or run: ./reels doctor --fix)")

    try:
        import PIL

        line(OK, f"Pillow {PIL.__version__}")
    except ImportError:
        line(BAD, "Pillow missing — re-run ./setup.sh")
        ok = False
    try:
        import pillow_heif  # type: ignore  # noqa: F401

        line(OK, "HEIC photo support")
    except ImportError:
        line(NOTE, "no HEIC support (optional) — .venv/bin/pip install pillow-heif" + (" (macOS sips works as a fallback)" if which("sips") else ""))

    model = project.cfg("transcription", "model")
    try:
        import faster_whisper

        line(OK, f"faster-whisper {faster_whisper.__version__}")
        if whisper_model_cached(model):
            line(OK, f"Whisper model '{model}' downloaded")
        elif fix:
            from .transcribe import load_model

            load_model(project)
            line(OK, f"Whisper model '{model}' downloaded")
        else:
            line(NOTE, f"Whisper model '{model}' downloads on first transcribe (or run: ./reels doctor --fix)")
    except ImportError:
        line(BAD, "faster-whisper missing — re-run ./setup.sh")
        ok = False

    # yt-dlp (YouTube music). YouTube changes often, so --fix always updates it.
    if fix:
        log("    updating yt-dlp…")
        subprocess.run([sys.executable, "-m", "pip", "install", "--quiet", "--upgrade", "yt-dlp[default]"], check=False)
    ver = run([sys.executable, "-m", "yt_dlp", "--version"], check=False)
    if ver.returncode == 0:
        v = ver.stdout.strip()
        line(OK, f"yt-dlp {v} (YouTube music)")
        try:
            from datetime import date

            age = (date.today() - date(*map(int, v.split(".")[:3]))).days
            if age > 60:
                line(NOTE, f"yt-dlp is {age} days old — YouTube downloads may break; ./reels doctor --fix updates it")
        except (ValueError, TypeError):
            pass
        if not which("node"):
            line(NOTE, "YouTube downloads need a JavaScript runtime — install Node (brew install node)")
    else:
        line(BAD, "yt-dlp missing — re-run ./setup.sh (or: .venv/bin/pip install 'yt-dlp[default]')")
        ok = False

    handle = project.cfg("brand", "handle")
    if handle in ("", "@yourhandle"):
        line(NOTE, "set brand.handle in config.json")
    else:
        line(OK, f"brand handle {handle}")
    from .music import list_tracks

    tracks = list_tracks(project)
    line(
        OK if tracks else NOTE,
        f"{len(tracks)} music track(s) in music/" + ("" if tracks else " — find some with ./reels music search \"<mood>\" --free"),
    )
    up = project.config.get("upload") or {}
    if up.get("bucket"):
        try:
            import boto3  # type: ignore  # noqa: F401

            line(OK, f"uploads to bucket '{up['bucket']}'")
        except ImportError:
            line(BAD, "upload is configured but boto3 is missing — .venv/bin/pip install boto3")
            ok = False

    log("\nall good ✓" if ok else "\nfix the ✗ items above, then run ./reels doctor again")
    return ok
