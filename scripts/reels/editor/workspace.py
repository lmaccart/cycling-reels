"""Live editor workspace: the real repo folders plus a private state folder.

The editor works directly on the repository: plans/, library/ metadata, music,
exhibits and config are read from (and plan edits are written to) the repo. The
original footage under library/media is registered read-only. Everything the
editor itself owns (history, jobs, previews, chat state, lock, context.json)
lives under <repo>/.cache/editor/live/.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any

from ..config import Project
from ..util import ReelsError, quick_hash

# Render inputs. Worker clones copy these (never library/media) and the library
# revision fingerprints them. library/sheets and library/thumbs are not render
# inputs, so they are left out of both.
INPUT_DIRS = (
    "library/assets",
    "library/transcripts",
    "library/sounds",
    "library/telemetry",
    "music",
    "sfx",
    "exhibits",
    "strava",
)
INPUT_FILES = ("config.json", "progress.json", "library/progress.json")
CLONE_EXTRA_DIRS = ("plans", "learning")
_SENSITIVE_NAMES = {
    ".env", ".claude", ".credentials", "credentials", "secrets", "keychain",
    "node_modules", ".venv", ".cache", "__pycache__",
}
_SENSITIVE_KEYS = {
    "token", "access_token", "refresh_token", "api_key", "apikey", "secret",
    "password", "authorization", "credential", "credentials", "client_secret",
    "private_key", "auth",
}
LIVE_STATE = Path(".cache/editor/live")
_MANIFEST_VERSION = 1


def _within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _signature(path: Path) -> dict[str, int]:
    info = path.stat(follow_symlinks=False)
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
    }


def _safe_name(path: Path) -> bool:
    for part in path.parts:
        name = part.lower()
        if (
            name in _SENSITIVE_NAMES
            or name.startswith(".")
            or any(marker in name for marker in ("credential", "secret", "token", "oauth", "keychain"))
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        ):
            return False
    return path.name != ".DS_Store"


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or any(
        marker in normalized for marker in ("token", "secret", "password", "credential", "api_key", "private_key")
    )


def _plan_media_refs(plan: dict[str, Any]):
    """Renderer schema media references, not arbitrary extension field names."""
    for segment in plan.get("segments", []):
        if not isinstance(segment, dict):
            continue
        asset = segment.get("asset")
        if isinstance(asset, str) and asset.strip():
            yield asset
        cutaways = segment.get("cutaways")
        if isinstance(cutaways, list):
            for cutaway in cutaways:
                if isinstance(cutaway, dict):
                    asset = cutaway.get("asset")
                    if isinstance(asset, str) and asset.strip():
                        yield asset


def _identity(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise ReelsError(f"registered original is not a regular file: {path.name}")
    before = _signature(path)
    digest = quick_hash(path)
    after = _signature(path)
    if before != after:
        raise ReelsError(f"original changed while its identity was recorded: {path.name}")
    return {**after, "quick_hash": digest}


def _regular_files(root: Path, *, reject_symlinks: bool = False) -> list[Path]:
    result: list[Path] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        kept = []
        for name in dirs:
            path = base / name
            if path.is_symlink():
                if reject_symlinks:
                    raise ReelsError(f"editor input contains a symlink: {path.relative_to(root)}")
            else:
                kept.append(name)
        dirs[:] = kept
        for name in files:
            path = base / name
            if path.is_symlink():
                if reject_symlinks:
                    raise ReelsError(f"editor input contains a symlink: {path.relative_to(root)}")
            elif path.is_file():
                result.append(path)
    return result


def _read_stable(path: Path, expected: dict[str, int]) -> bytes:
    if _signature(path) != expected:
        raise ReelsError(f"editor input changed before it could be read: {path.name}")
    data = path.read_bytes()
    if _signature(path) != expected or len(data) != expected["size"]:
        raise ReelsError(f"editor input changed while it was being read: {path.name}")
    return data


class Workspace:
    """Facade binding the live repo, the editor's private state folder and originals."""

    def __init__(self, root: Path, data_root: Path, code_root: Path, project: Project,
                 originals: dict[str, dict[str, Any]] | None = None):
        self.root = root
        self.data_root = data_root
        self.code_root = code_root
        self.project = project
        self._originals: dict[str, dict[str, Any]] = originals if originals is not None else {}
        self._assets_stamp: tuple | None = None
        self._renderer_cache: tuple[float, str] | None = None
        self._library_revision_lock = threading.RLock()
        self._library_revision_value: str | None = None
        self._library_revision_dirty = True
        self._library_generation = 0
        self._library_signatures: dict[str, tuple[int, ...]] = {}
        self._library_watch_stop = threading.Event()
        self._library_watch_thread: threading.Thread | None = None
        self._library_watch_failed = False
        self._library_watch_generation = 0
        self.live_mode = True

    @classmethod
    def live(cls, repo_root: Path | str, code_root: Path | str | None = None) -> "Workspace":
        """Live mode on a repo. ``code_root`` is only for tests with throwaway repos."""
        repo = Path(repo_root).expanduser().resolve()
        if not repo.is_dir() or not (repo / "remotion").is_dir():
            raise ReelsError(f"not a cycling-reels folder: {repo}")
        state = repo / LIVE_STATE
        for parent in (repo / ".cache", repo / ".cache/editor"):
            parent.mkdir(parents=True, exist_ok=True)
        state.mkdir(parents=True, exist_ok=True)
        os.chmod(state, 0o700)
        for private_dir in ("history", "code", "jobs", "preview", "chat"):
            (state / private_dir).mkdir(exist_ok=True, mode=0o700)
        state_path = state / "state.json"
        if not state_path.exists():
            state_path.write_text(json.dumps({"version": _MANIFEST_VERSION, "status": "active", "mode": "live"}) + "\n")
        code = Path(code_root).expanduser().resolve() if code_root is not None else repo
        project = Project(repo, code_root=code, media_read_only=True)
        workspace = cls(root=state.resolve(), data_root=repo, code_root=code, project=project)
        workspace.sync_originals()
        return workspace

    # ---- renderer revision --------------------------------------------

    def renderer_revision(self) -> str:
        """Digest of the repo's current renderer source (python pipeline + remotion/src)."""
        now = time.monotonic()
        if self._renderer_cache and now - self._renderer_cache[0] < 2.0:
            return self._renderer_cache[1]
        digest = hashlib.sha256()
        files: list[Path] = sorted((self.code_root / "scripts/reels").glob("*.py"))
        src = self.code_root / "remotion/src"
        if src.is_dir():
            files += sorted(path for path in src.rglob("*") if path.is_file() and not path.is_symlink())
        for relative in ("remotion/package.json", "remotion/editor-preview.tsx", "remotion/editorStaticFile.ts"):
            if (self.code_root / relative).is_file():
                files.append(self.code_root / relative)
        for path in files:
            digest.update(path.relative_to(self.code_root).as_posix().encode())
            digest.update(b"\0")
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(b"unreadable")
        value = digest.hexdigest()[:24]
        self._renderer_cache = (now, value)
        return value

    # ---- originals registry -------------------------------------------

    def _asset_stamp(self) -> tuple:
        directory = self.project.assets_dir
        if not directory.is_dir():
            return ()
        stamp = []
        for path in sorted(directory.glob("*.json")):
            try:
                info = path.stat()
            except OSError:
                continue
            stamp.append((path.name, info.st_mtime_ns, info.st_size))
        return tuple(stamp)

    def sync_originals(self, force: bool = False) -> bool:
        """Rebuild the originals registry from library/assets records when they changed."""
        stamp = self._asset_stamp()
        if not force and stamp == self._assets_stamp:
            return False
        media_root = self.project.media_dir
        previous = self._originals
        originals: dict[str, dict[str, Any]] = {}
        for path in sorted(self.project.assets_dir.glob("*.json")) if self.project.assets_dir.is_dir() else []:
            try:
                asset = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            media = asset.get("media") if isinstance(asset, dict) else None
            if not isinstance(media, str) or not isinstance(asset.get("id"), str):
                continue
            rel = PurePosixPath(media)
            if rel.is_absolute() or ".." in rel.parts or len(rel.parts) < 3 or rel.parts[:2] != ("library", "media"):
                continue
            original = media_root.joinpath(*rel.parts[2:])
            if original.is_symlink():
                continue
            old = previous.get(media)
            identity = None
            if original.is_file():
                if old and old.get("identity") and old["path"] == str(original):
                    try:
                        now = _signature(original)
                    except OSError:
                        now = None
                    if now and all(old["identity"].get(k) == v for k, v in now.items()):
                        identity = old["identity"]
                if identity is None:
                    try:
                        identity = _identity(original)
                    except (OSError, ReelsError):
                        identity = None
            originals[media] = {"path": str(original), "asset_id": asset["id"], "identity": identity}
        self._originals = originals
        self._assets_stamp = stamp
        return True

    def media_status(self) -> dict[str, dict[str, Any]]:
        self.sync_originals()
        status: dict[str, dict[str, Any]] = {}
        for identifier, record in self._originals.items():
            path = Path(record["path"])
            expected = record.get("identity")
            if not path.is_file():
                status[identifier] = {"status": "offline", "assetId": record.get("asset_id")}
                continue
            if expected is None:
                status[identifier] = {"status": "changed", "assetId": record.get("asset_id")}
                continue
            try:
                now = _signature(path)
                same = all(expected.get(k) == v for k, v in now.items())
            except OSError:
                same = False
            status[identifier] = {"status": "online" if same else "changed", "assetId": record.get("asset_id")}
        return status

    # ---- library revision ---------------------------------------------

    def input_paths(self) -> list[Path]:
        paths: list[Path] = []
        for relative in INPUT_DIRS:
            base = self.data_root / relative
            if base.is_symlink():
                continue
            if base.is_dir():
                paths.extend(_regular_files(base))
        for relative in INPUT_FILES:
            path = self.data_root / relative
            if path.is_file() and not path.is_symlink():
                paths.append(path)
        return paths

    def _fresh_library_revision(self) -> tuple[str, dict[str, tuple[int, ...]]]:
        """Recompute render-input identity and a cheap watcher signature."""
        self.sync_originals()
        digest = hashlib.sha256()
        signatures: dict[str, tuple[int, ...]] = {}
        for path in sorted(self.input_paths()):
            relative = path.relative_to(self.data_root).as_posix()
            if not _safe_name(Path(relative)):
                continue
            try:
                identity = _signature(path)
            except OSError:
                continue
            signatures[relative] = tuple(identity[key] for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns"))
            if path.suffix.lower() == ".json":
                # Explicit refreshes really read JSON, even if a caller restored
                # its timestamp and size after an external edit.
                try:
                    content = hashlib.sha256(_read_stable(path, identity)).hexdigest()
                except (OSError, ReelsError):
                    content = "unstable"
            else:
                content = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(content.encode())
        digest.update(json.dumps(self._originals, sort_keys=True).encode())
        return digest.hexdigest(), signatures

    def refresh_library_revision(self) -> str:
        """Force a fresh content fingerprint and reset the watcher's baseline."""
        with self._library_revision_lock:
            revision, signatures = self._fresh_library_revision()
            if revision != self._library_revision_value:
                self._library_generation += 1
            self._library_revision_value = revision
            self._library_signatures = signatures
            self._library_revision_dirty = False
            return revision

    def invalidate_library_revision(self) -> None:
        """Mark render inputs dirty after an in-process write or materialization."""
        with self._library_revision_lock:
            self._library_revision_dirty = True

    def library_revision(self) -> str:
        """Return the stable cached revision; refresh synchronously if needed.

        When the polling watcher is unavailable or stopped, every call forces
        a fresh read so external CLI edits remain immediately visible.
        """
        with self._library_revision_lock:
            watcher = self._library_watch_thread
            watching = bool(
                watcher is not None and watcher.is_alive() and not self._library_watch_failed
            )
            if watching and not self._library_revision_dirty and self._library_revision_value is not None:
                return self._library_revision_value
        return self.refresh_library_revision()

    def library_generation(self) -> int:
        """Monotonic generation for consumers that mirror inputs incrementally."""
        self.library_revision()
        with self._library_revision_lock:
            return self._library_generation

    def library_signatures(self) -> dict[str, tuple[int, ...]]:
        """Return cached relative-path stat signatures for worker input syncing."""
        self.library_revision()
        with self._library_revision_lock:
            return dict(self._library_signatures)

    def _input_watch_signature(self) -> dict[str, tuple[int, ...]]:
        signatures: dict[str, tuple[int, ...]] = {}
        for path in self.input_paths():
            relative = path.relative_to(self.data_root).as_posix()
            if not _safe_name(Path(relative)):
                continue
            try:
                info = _signature(path)
            except OSError:
                continue
            signatures[relative] = tuple(
                info[key] for key in ("dev", "ino", "size", "mtime_ns", "ctime_ns")
            )
        return signatures

    def _watch_library_inputs(self, interval: float, stop_event: threading.Event,
                              generation: int) -> None:
        try:
            while not stop_event.wait(interval):
                signatures = self._input_watch_signature()
                with self._library_revision_lock:
                    if generation != self._library_watch_generation or stop_event.is_set():
                        return
                    if signatures != self._library_signatures:
                        self._library_signatures = signatures
                        self._library_revision_dirty = True
        except Exception:  # noqa: BLE001 - fail closed to fresh synchronous checks
            with self._library_revision_lock:
                if generation == self._library_watch_generation:
                    self._library_watch_failed = True

    def start_library_watcher(self, interval: float = 1.0) -> bool:
        """Start stdlib polling for additions, deletions, renames and overwrites."""
        if interval <= 0:
            raise ValueError("watch interval must be positive")
        with self._library_revision_lock:
            watcher = self._library_watch_thread
            if (watcher is not None and watcher.is_alive()
                    and not self._library_watch_stop.is_set() and not self._library_watch_failed):
                return True
        self.stop_library_watcher()
        self.refresh_library_revision()
        with self._library_revision_lock:
            self._library_watch_failed = False
            self._library_watch_generation += 1
            generation = self._library_watch_generation
            self._library_watch_stop = threading.Event()
            stop_event = self._library_watch_stop
            watcher = threading.Thread(
                target=self._watch_library_inputs,
                args=(float(interval), stop_event, generation),
                name="reels-library-revision-watcher",
                daemon=True,
            )
            self._library_watch_thread = watcher
            try:
                watcher.start()
            except RuntimeError:
                self._library_watch_failed = True
                self._library_watch_thread = None
                return False
            return watcher.is_alive()

    def stop_library_watcher(self, join_timeout: float = 2.0) -> None:
        with self._library_revision_lock:
            self._library_watch_generation += 1
            watcher = self._library_watch_thread
            stop_event = self._library_watch_stop
            stop_event.set()
        if watcher is not None and watcher is not threading.current_thread():
            watcher.join(timeout=max(0.0, join_timeout))
        with self._library_revision_lock:
            if watcher is self._library_watch_thread:
                self._library_watch_thread = None
