"""Content-keyed cache of staged clips for `./reels render`, and the bounded pool that stages them.

Re-rendering a reel after a caption tweak used to re-stage (and re-grade) every clip. A staged clip is a
pure function of: its source file (path + size + mtime), the clip's pixel/audio-relevant spec, the stage
options (fps, target size, scale, clean, edges, audio), the resolved grade settings and the code that
makes it. All of that goes into the key; anything that doesn't change the clip (captions, hooks, text) is
left out so those edits stay warm. Entries live in `.cache/render/staged/<key>/` (never in library/), hold
the clip's files plus `meta.json` (the grade summary, so `built["graded"]` is identical on a hit), and are
trimmed least-recently-used past `render.stageCacheGB`. The editor's own preview cache
(editor/preview_cache.py) is a separate, manifest-based scheme sandboxed for the editor; the stage-spec
allowlist below is the same one its worker uses.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Sequence

KEY_VERSION = 1
DEFAULT_GB = 10
# Spec fields that change a staged clip's pixels or audio (same set as editor/worker.py's _STAGE_SPEC_FIELDS).
SPEC_FIELDS = (
    "in", "out", "speed", "frame", "zoom", "focusX", "focusY", "fit", "motion",
    "sphere", "punch", "blur", "clean", "nat", "bleep", "tone", "volume", "_bleeps", "grade",
)
# Modules whose code decides a staged clip (their sources are hashed into every key).
CODE_MODULES = ("render", "grade", "media", "sphere", "sphere_metal", "stabilize", "bleep", "mix", "punch", "images", "util")
META = "meta.json"


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------


def default_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def worker_count(value: Any) -> int:
    """`render.stageWorkers`: a positive integer, else the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 1:
        return default_workers()
    return int(value)


def run_jobs(jobs: Sequence[Callable[[], Any]], workers: int) -> list[Any]:
    """Run independent jobs on up to `workers` threads; results come back in job order. The first error
    (by completion) cancels everything not started yet, waits for the running ones, and is raised."""
    if workers <= 1 or len(jobs) <= 1:
        return [job() for job in jobs]
    results: list[Any] = [None] * len(jobs)
    pool = ThreadPoolExecutor(max_workers=min(workers, len(jobs)), thread_name_prefix="stage")
    futures = {pool.submit(job): i for i, job in enumerate(jobs)}
    pending = set(futures)
    error: BaseException | None = None
    try:
        while pending and error is None:
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                if f.exception() is not None:
                    error = error or f.exception()
                else:
                    results[futures[f]] = f.result()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    if error is not None:
        raise error
    return results


# ---------------------------------------------------------------------------
# Keys
# ---------------------------------------------------------------------------


def _file_id(path: Path) -> dict:
    try:
        st = path.stat()
    except OSError:
        return {"path": str(path), "missing": True}
    return {"path": str(path), "size": st.st_size, "mtime": st.st_mtime_ns}


@lru_cache(maxsize=8)
def _code_revision(code_dir: str, tools: tuple) -> str:
    h = hashlib.sha256(str(KEY_VERSION).encode())
    for name in CODE_MODULES:
        p = Path(code_dir) / f"{name}.py"
        h.update(name.encode())
        h.update(p.read_bytes() if p.is_file() else b"-")
    h.update(json.dumps(tools).encode())
    return h.hexdigest()


def code_revision(project: Any) -> str:
    """Hash of the staging code, the ffmpeg build and (on macOS) the Metal sphere binary."""
    from .media import BIN

    tools: list[Any] = [os.environ.get("REELS_SPHERE_BACKEND", "auto"), platform.machine()]
    for name in ("ffmpeg", "ffprobe"):
        exe = shutil.which(BIN.get(name, name))
        tools.append(_file_id(Path(exe).resolve()) if exe else None)
    if platform.system() == "Darwin":
        try:
            from .sphere_metal import executable

            exe = executable(project.code_root)
            tools += [_file_id(exe), _file_id(exe.with_name("sphere.metallib"))] if exe else [None]
        except Exception:  # noqa: BLE001 - unknown state: key on that fact
            tools.append("sphere-unknown")
    return _code_revision(str(Path(__file__).resolve().parent), tuple(json.dumps(t, sort_keys=True) for t in tools))


def clip_key(project: Any, seg: Any, kind: str, fps: int, options: dict, grade: dict | None) -> str:
    """Key for one staged clip. `options` = everything the caller passes beyond the segment itself
    (clean, scale, edges, audio ...); `grade` = the resolved grade settings."""
    spec = {k: seg.spec[k] for k in SPEC_FIELDS if k in seg.spec}
    asset = seg.asset
    ident = {
        "v": KEY_VERSION,
        "code": code_revision(project),
        "kind": kind,
        "source": _file_id(project.abs(asset["media"])),
        "probe": asset.get("probe"),
        "spec": spec,
        "frames": int(seg.frames),
        "fps": fps,
        "size": [int(project.cfg("render", "width")), int(project.cfg("render", "height"))],
        "options": options,
        "grade": grade,
    }
    blob = json.dumps(ident, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


# ---------------------------------------------------------------------------
# The cache
# ---------------------------------------------------------------------------


def _copy(src: Path, dst: Path) -> None:
    """APFS clone where possible (instant, no extra space), else a plain copy."""
    if platform.system() == "Darwin":
        r = subprocess.run(["cp", "-c", str(src), str(dst)], capture_output=True, stdin=subprocess.DEVNULL)
        if r.returncode == 0:
            return
        dst.unlink(missing_ok=True)
    shutil.copyfile(src, dst)


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class StageCache:
    def __init__(self, root: Path, max_bytes: int):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @classmethod
    def for_project(cls, project: Any) -> "StageCache | None":
        """None when bypassed (`render.stageCache: false`) or sized to zero."""
        if project.cfg("render", "stageCache") is False:
            return None
        gb = project.cfg("render", "stageCacheGB")
        if isinstance(gb, bool) or not isinstance(gb, (int, float)) or gb < 0:
            gb = DEFAULT_GB
        if gb == 0:
            return None
        return cls(project.root / ".cache" / "render" / "staged", int(gb * 1_000_000_000))

    def fetch(self, key: str, name: str, dest_dir: Path) -> dict | None:
        """Copy a cached clip's files into `dest_dir` as `name...`; returns {"grade": summary-or-None}, None on a miss."""
        entry = self.root / key
        copied: list[Path] = []
        try:
            meta = json.loads((entry / META).read_text())
            files = meta["files"]
            if not files or not all((entry / "files" / rel).is_file() for rel in files):
                return None
            for rel in files:
                dst = dest_dir / (name + rel)
                copied.append(dst)
                _copy(entry / "files" / rel, dst)
            os.utime(entry / META)  # LRU stamp
            return {"grade": meta.get("grade")}
        except (OSError, ValueError, KeyError, TypeError):
            for p in copied:
                p.unlink(missing_ok=True)
            return None

    def store(self, key: str, name: str, src_dir: Path, grade: dict | None) -> None:
        """Keep the clip `name` (every `name.*` file in `src_dir`) under `key`. Best effort."""
        try:
            files = sorted(p.name[len(name):] for p in src_dir.glob(glob.escape(name) + ".*") if p.is_file())
            if not files:
                return
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = Path(tempfile.mkdtemp(prefix=".tmp-", dir=self.root))
            try:
                (tmp / "files").mkdir()
                for rel in files:
                    _copy(src_dir / (name + rel), tmp / "files" / rel)
                (tmp / META).write_text(json.dumps({"files": files, "grade": grade, "key": key}))
                final = self.root / key
                with self._lock:
                    if final.exists():
                        shutil.rmtree(final, ignore_errors=True)
                    os.replace(tmp, final)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            self.trim(keep={key})
        except OSError:
            pass

    def trim(self, keep: set[str] | frozenset = frozenset()) -> int:
        """Drop least-recently-used entries until the cache fits its budget; returns entries removed.
        Half-written `.tmp-*` leftovers older than an hour go too."""
        with self._lock:
            if not self.root.is_dir():
                return 0
            entries = []
            for e in self.root.iterdir():
                if not e.is_dir():
                    continue
                if e.name.startswith(".tmp-"):
                    if time.time() - e.stat().st_mtime > 3600:
                        shutil.rmtree(e, ignore_errors=True)
                    continue
                try:
                    stamp = (e / META).stat().st_mtime
                except OSError:
                    stamp = 0.0
                entries.append((stamp, e, _size(e)))
            total = sum(s for _, _, s in entries)
            removed = 0
            for _, e, size in sorted(entries, key=lambda t: t[0]):
                if total <= self.max_bytes:
                    break
                if e.name in keep:
                    continue
                shutil.rmtree(e, ignore_errors=True)
                total -= size
                removed += 1
            return removed
