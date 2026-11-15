"""Private editor workspace snapshots and read-only original-media registry."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import uuid
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any

from ..config import DEFAULTS, Project
from ..util import ReelsError, quick_hash

_SNAPSHOT_DIRS = (
    "plans",
    "library/assets",
    "library/transcripts",
    "library/sheets",
    "library/thumbs",
    "library/sounds",
    "library/telemetry",
    "music",
    "sfx",
    "exhibits",
    "analytics",
    "learning",
    "strava",
)
_SNAPSHOT_FILES = ("config.json", "progress.json", "library/progress.json")
_SENSITIVE_KEYS = {
    "token", "access_token", "refresh_token", "api_key", "apikey", "secret",
    "password", "authorization", "credential", "credentials", "client_secret",
    "private_key", "auth",
}
_SENSITIVE_NAMES = {
    ".env", ".claude", ".credentials", "credentials", "secrets", "keychain",
    "node_modules", ".venv", ".cache", "__pycache__",
}
_IMPLEMENTATION_EXTENSIONS = {
    ".py", ".pyi", ".pyc", ".pyo", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs",
    ".sh", ".bash", ".zsh", ".command", ".swift", ".h", ".c", ".cc", ".cpp",
    ".rs", ".go", ".rb", ".php", ".css", ".html",
}
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


def _walk_snapshot(source: Path) -> dict[str, Path]:
    """Enumerate only approved data; symlinks in included trees fail closed."""
    found: dict[str, Path] = {}
    candidates = [source / name for name in _SNAPSHOT_FILES]
    for name in _SNAPSHOT_DIRS:
        candidates.append(source / name)
    for candidate in candidates:
        if not candidate.exists() and not candidate.is_symlink():
            continue
        components = [candidate]
        parent = candidate.parent
        while parent != source and _within(parent, source):
            components.append(parent)
            parent = parent.parent
        if any(component.is_symlink() for component in components):
            raise ReelsError(f"snapshot source contains a symlink: {candidate.relative_to(source)}")
        if candidate.is_file():
            rel = candidate.relative_to(source).as_posix()
            if _safe_name(Path(rel)):
                found[rel] = candidate
            continue
        if not candidate.is_dir():
            raise ReelsError(f"snapshot source entry is not a regular file or directory: {candidate}")
        for current, dirs, files in os.walk(candidate, topdown=True, followlinks=False):
            current_path = Path(current)
            kept_dirs = []
            for name in sorted(dirs):
                entry = current_path / name
                if entry.is_symlink():
                    raise ReelsError(f"snapshot source contains a symlink: {entry.relative_to(source)}")
                if _safe_name(entry.relative_to(source)):
                    kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in sorted(files):
                entry = current_path / name
                rel_path = entry.relative_to(source)
                if not _safe_name(rel_path):
                    continue
                if entry.is_symlink():
                    raise ReelsError(f"snapshot source contains a symlink: {rel_path.as_posix()}")
                if not entry.is_file():
                    raise ReelsError(f"snapshot source entry is not a regular file: {rel_path.as_posix()}")
                if entry.suffix.lower() in _IMPLEMENTATION_EXTENSIONS:
                    raise ReelsError(f"implementation file is not snapshot data: {rel_path.as_posix()}")
                found[rel_path.as_posix()] = entry
    return found


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _sanitize(item)
            for key, item in value.items()
            if not _sensitive_key(str(key))
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or any(
        marker in normalized for marker in ("token", "secret", "password", "credential", "api_key", "private_key")
    )


def _read_stable(path: Path, expected: dict[str, int]) -> bytes:
    before = _signature(path)
    if before != expected:
        raise ReelsError(f"snapshot source changed before it could be copied: {path.name}")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before["dev"], before["ino"]):
            raise ReelsError(f"snapshot source was replaced while opening: {path.name}")
        data = stream.read()
        after_open = os.fstat(stream.fileno())
    after = _signature(path)
    stable_open = (
        after_open.st_dev == opened.st_dev
        and after_open.st_ino == opened.st_ino
        and after_open.st_size == opened.st_size
        and after_open.st_mtime_ns == opened.st_mtime_ns
        and after_open.st_ctime_ns == opened.st_ctime_ns
    )
    if not stable_open or after != before or len(data) != before["size"]:
        raise ReelsError(f"snapshot source changed while it was being copied: {path.name}")
    return data


def _write_private(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


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


def _assert_no_symlink_components(path: Path, parent: Path) -> None:
    if any(component.is_symlink() for component in (parent, *parent.parents)):
        raise ReelsError("original media root must not be reached through a symlink")
    try:
        relative = path.relative_to(parent)
    except ValueError as exc:
        raise ReelsError("original media path escaped its registered root") from exc
    current = parent
    for part in relative.parts[:-1]:
        current = current / part
        if current.is_symlink():
            raise ReelsError(f"registered original uses a symlinked directory: {part}")


def _store_state(path: Path, state: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _write_private(temporary, (json.dumps(state, indent=2) + "\n").encode())
    os.replace(temporary, path)


def _publish_without_replace(source: Path, destination: Path) -> None:
    """Atomically activate the staged directory without replacing any target."""
    import ctypes
    import errno

    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        rename = getattr(libc, "renamex_np", None)
        if rename is None:
            raise ReelsError("filesystem lacks atomic no-replace workspace activation")
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        result = rename(os.fsencode(source), os.fsencode(destination), 0x00000004)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = getattr(libc, "renameat2", None)
        if rename is None:
            raise ReelsError("filesystem lacks atomic no-replace workspace activation")
        rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        result = rename(-100, os.fsencode(source), -100, os.fsencode(destination), 0x00000001)  # RENAME_NOREPLACE
    else:
        raise ReelsError("platform lacks atomic no-replace workspace activation")
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise ReelsError(f"editor workspace appeared during initialization: {destination}")
        raise OSError(error, os.strerror(error), str(destination))


def _regular_files(root: Path, *, reject_symlinks: bool) -> list[Path]:
    result: list[Path] = []
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        kept_dirs = []
        for name in dirs:
            path = base / name
            if path.is_symlink():
                if reject_symlinks:
                    raise ReelsError(f"editor authoritative data contains a symlink: {path.relative_to(root)}")
            else:
                kept_dirs.append(name)
        dirs[:] = kept_dirs
        for name in files:
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                if reject_symlinks:
                    raise ReelsError(f"editor authoritative data contains a symlink: {path.relative_to(root)}")
            elif stat.S_ISREG(info.st_mode):
                result.append(path)
    return result


def _check_no_links(root: Path, *, internal_scope: Path | None = None) -> None:
    """Reject authoritative symlink escapes; permit private internal hardlinks."""
    files = _regular_files(root, reject_symlinks=True)
    scope_files = _regular_files(internal_scope or root, reject_symlinks=False)
    counts: dict[tuple[int, int], int] = {}
    for path in scope_files:
        info = path.stat(follow_symlinks=False)
        key = (info.st_dev, info.st_ino)
        counts[key] = counts.get(key, 0) + 1
    for path in files:
        info = path.stat(follow_symlinks=False)
        if info.st_nlink > counts.get((info.st_dev, info.st_ino), 0):
            raise ReelsError(f"editor authoritative file has a hard link outside the private workspace: {path.name}")


def _assert_path_no_symlink(path: Path, root: Path, *, required: bool = True) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ReelsError("authoritative workspace path escaped its root") from exc
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ReelsError(f"authoritative workspace path contains a symlink: {relative.as_posix()}")
    if not current.exists():
        if required:
            raise ReelsError(f"authoritative workspace file is missing: {relative.as_posix()}")
        return False
    if not current.is_file():
        raise ReelsError(f"authoritative workspace path is not a file: {relative.as_posix()}")
    return True


def _committed_history_paths(root: Path, history: Path | None = None) -> list[Path]:
    """Return HEAD-reachable immutable history; ignore uncommitted job scratch."""
    if history is None:
        paths = _committed_history_paths(root, root / "history")
        documents = root / "history/documents"
        if documents.is_symlink():
            raise ReelsError("editor document histories must not be a symlink")
        if documents.is_dir():
            for directory in documents.iterdir():
                if directory.is_symlink():
                    raise ReelsError("editor document history must not be a symlink")
                if directory.is_dir():
                    paths.extend(_committed_history_paths(root, directory))
        return paths
    head_path = history / "HEAD"
    if not _assert_path_no_symlink(head_path, root, required=False):
        return []
    paths = [head_path]
    try:
        head = json.loads(head_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ReelsError(f"editor history HEAD is invalid: {exc}") from exc
    revision = head.get("revision") if isinstance(head, dict) else None
    seen: set[str] = set()
    while revision:
        if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", revision):
            raise ReelsError("editor history HEAD contains an invalid revision identifier")
        if revision in seen:
            raise ReelsError("editor history contains a commit cycle")
        seen.add(revision)
        manifest_path = history / "commits" / f"{revision}.json"
        _assert_path_no_symlink(manifest_path, root)
        paths.append(manifest_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReelsError(f"editor commit manifest {revision} is invalid: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("revision") != revision:
            raise ReelsError(f"editor commit manifest {revision} does not match HEAD history")
        objects = manifest.get("files", {})
        if not isinstance(objects, dict):
            raise ReelsError(f"editor commit manifest {revision} has invalid object references")
        for digest in objects.values():
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ReelsError(f"editor commit manifest {revision} has an invalid object identity")
            object_path = history / "objects" / f"{digest}.json"
            _assert_path_no_symlink(object_path, root)
            paths.append(object_path)
        revision = manifest.get("parent")
    materialized = history / "MATERIALIZED"
    if _assert_path_no_symlink(materialized, root, required=False):
        paths.append(materialized)
    return paths


def _check_paths_no_links(paths: list[Path], *, internal_scope: Path) -> None:
    """Ensure authoritative files have no links outside the private workspace."""
    scope_files = _regular_files(internal_scope, reject_symlinks=False)
    counts: dict[tuple[int, int], int] = {}
    for path in scope_files:
        info = path.stat(follow_symlinks=False)
        key = (info.st_dev, info.st_ino)
        counts[key] = counts.get(key, 0) + 1
    for path in paths:
        info = path.stat(follow_symlinks=False)
        if info.st_nlink > counts.get((info.st_dev, info.st_ino), 0):
            raise ReelsError(f"authoritative file has a hard link outside the private workspace: {path.name}")


def _validate_data(data_root: Path, originals_root: Path) -> dict[str, dict[str, Any]]:
    """Validate copied JSON and references; return original media identities."""
    assets: dict[str, dict[str, Any]] = {}
    originals: dict[str, dict[str, Any]] = {}
    assets_dir = data_root / "library/assets"
    if assets_dir.exists():
        for path in sorted(assets_dir.glob("*.json")):
            try:
                asset = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReelsError(f"invalid asset JSON {path.name}: {exc}") from exc
            if not isinstance(asset, dict) or not isinstance(asset.get("id"), str):
                raise ReelsError(f"asset record {path.name} must be an object with a string id")
            if asset["id"] != path.stem or asset["id"] in assets:
                raise ReelsError(f"asset id must be unique and match {path.name}")
            assets[asset["id"]] = asset

    for asset_id, asset in assets.items():
        media = asset.get("media")
        if not isinstance(media, str):
            raise ReelsError(f"asset {asset_id} is missing its media identifier")
        rel = PurePosixPath(media)
        if rel.is_absolute() or ".." in rel.parts or len(rel.parts) < 3 or rel.parts[:2] != ("library", "media"):
            raise ReelsError(f"asset {asset_id} has an unsafe original-media identifier")
        original = originals_root.joinpath(*rel.parts[2:])
        if not _within(original.resolve(strict=False), originals_root.resolve()):
            raise ReelsError(f"asset {asset_id} original-media identifier escapes library/media")
        _assert_no_symlink_components(original, originals_root)
        identity = _identity(original)
        originals[media] = {
            "path": str(original.resolve(strict=False)),
            "asset_id": asset_id,
            "identity": identity,
        }
        for field in ("transcript", "telemetry"):
            reference = asset.get(field)
            if reference:
                if not isinstance(reference, str):
                    raise ReelsError(f"asset {asset_id} has an invalid {field} reference")
                p = Path(reference)
                if p.is_absolute() or ".." in p.parts or not (data_root / p).is_file():
                    raise ReelsError(f"asset {asset_id} has a missing or unsafe {field} reference")
        sheets = asset.get("sheets") or []
        if not isinstance(sheets, list):
            raise ReelsError(f"asset {asset_id} sheets must be a list")
        for reference in sheets:
            if not isinstance(reference, str):
                raise ReelsError(f"asset {asset_id} has an invalid sheet reference")
            p = Path(reference)
            if p.is_absolute() or ".." in p.parts or not (data_root / p).is_file():
                raise ReelsError(f"asset {asset_id} has a missing or unsafe sheet reference")

    for path in sorted(data_root.rglob("*.json")):
        if path.is_symlink():
            raise ReelsError(f"workspace JSON must not be a symlink: {path.name}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReelsError(f"invalid JSON in snapshot ({path.relative_to(data_root)}): {exc}") from exc
        if path == data_root / "config.json" and not isinstance(value, dict):
            raise ReelsError("snapshot config.json must be an object")
        if path == data_root / "config.json":
            for key, default in DEFAULTS.items():
                if isinstance(default, dict) and key in value and not isinstance(value[key], dict):
                    raise ReelsError(f"snapshot config.json section {key!r} must be an object")
        if data_root / "plans" in path.parents:
            if not isinstance(value, dict):
                raise ReelsError(f"plan {path.name} must be a JSON object")
            if not isinstance(value.get("segments"), list):
                raise ReelsError(f"plan {path.name} must contain a segments list")
            from ..library import resolve_id

            asset_project = SimpleNamespace(assets_dir=assets_dir)
            for ref in _plan_media_refs(value):
                try:
                    resolve_id(asset_project, ref)
                except ReelsError as exc:
                    raise ReelsError(f"plan {path.name} refers to an unresolved asset {ref!r}: {exc}") from exc
    return originals


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


def init_workspace(
    source: Path | str,
    workspace: Path | str,
    code_root: Path | str | None = None,
) -> "Workspace":
    """Create an inactive staged snapshot, then atomically publish it as active."""
    source_root = Path(source).expanduser().resolve()
    if not source_root.is_dir():
        raise ReelsError(f"snapshot source is not a directory: {source_root}")
    resolved_code = (
        Path(code_root).expanduser().resolve()
        if code_root is not None
        else Path(__file__).resolve().parents[3]
    )
    if not resolved_code.is_dir():
        raise ReelsError(f"editor code root is not a directory: {resolved_code}")
    target = Path(workspace).expanduser().resolve()
    if target == source_root or _within(target, source_root):
        raise ReelsError("editor workspace must be outside the snapshot source")
    if target.exists() or target.is_symlink():
        raise ReelsError(f"editor workspace already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    stage = target.parent / f".{target.name}.incomplete-{uuid.uuid4().hex}"
    stage.mkdir(mode=0o700)
    for private_dir in ("history", "code", "jobs", "preview", "chat"):
        (stage / private_dir).mkdir(mode=0o700)
    state_path = stage / "state.json"
    try:
        files = _walk_snapshot(source_root)
        baseline = {name: _signature(path) for name, path in files.items()}
        data_root = stage / "data"
        data_root.mkdir(mode=0o700)
        for name, source_path in sorted(files.items()):
            payload = _read_stable(source_path, baseline[name])
            if source_path.suffix.lower() == ".json":
                try:
                    parsed = json.loads(payload.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise ReelsError(f"invalid JSON in snapshot source {name}: {exc}") from exc
                payload = (json.dumps(_sanitize(parsed), ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            destination = data_root / name
            _write_private(destination, payload)
            source_info = source_path.stat(follow_symlinks=False)
            dest_info = destination.stat(follow_symlinks=False)
            if (source_info.st_dev, source_info.st_ino) == (dest_info.st_dev, dest_info.st_ino):
                raise ReelsError(f"snapshot copy unexpectedly shares an inode: {name}")

        originals_root = source_root / "library/media"
        if originals_root.is_symlink():
            raise ReelsError("original media root must not be a symlink")
        originals = _validate_data(data_root, originals_root)
        # Re-enumerate and compare every selected source entry. This catches
        # additions/removals and changes after their individual read completed.
        final_files = _walk_snapshot(source_root)
        if set(final_files) != set(files):
            raise ReelsError("snapshot source data changed during initialization; retry the snapshot")
        for name, path in final_files.items():
            if _signature(path) != baseline[name]:
                raise ReelsError(f"snapshot source data changed during initialization ({name}); retry the snapshot")
        for record in originals.values():
            identity = _identity(Path(record["path"]))
            if identity != record["identity"]:
                raise ReelsError("original media changed during initialization; retry the snapshot")
        _check_no_links(stage)
        manifest = {
            "version": _MANIFEST_VERSION,
            "source": str(source_root),
            "originals": originals,
        }
        _write_private(stage / "originals.json", (json.dumps(manifest, indent=2) + "\n").encode())
        _write_private(
            state_path,
            (json.dumps({"version": _MANIFEST_VERSION, "status": "active"}, indent=2) + "\n").encode(),
        )
        _check_no_links(stage)
        if target.exists():
            raise ReelsError(f"editor workspace appeared during initialization: {target}")
        _publish_without_replace(stage, target)
    except Exception as exc:
        try:
            failed_state = {
                "version": _MANIFEST_VERSION,
                "status": "failed",
                "error": str(exc),
                "retry": True,
            }
            if state_path.exists():
                _store_state(state_path, failed_state)
            else:
                _write_private(state_path, (json.dumps(failed_state, indent=2) + "\n").encode())
        except OSError:
            pass
        if isinstance(exc, ReelsError):
            raise ReelsError(
                f"editor snapshot failed; workspace remains inactive at {stage}; retry initialization: {exc}"
            ) from exc
        raise ReelsError(
            f"editor snapshot failed; workspace remains inactive at {stage}; retry initialization: {exc}"
        ) from exc
    return Workspace.load(target, code_root=resolved_code)


class Workspace:
    """Facade binding private project data, local code and registered originals."""

    def __init__(self, root: Path, data_root: Path, code_root: Path, project: Project,
                 originals: dict[str, dict[str, Any]]):
        self.root = root
        self.data_root = data_root
        self.code_root = code_root
        self.project = project
        self._originals = originals
        self._revision_cache: dict[str, tuple[dict[str, int], str]] = {}

    @classmethod
    def load(cls, path: Path | str, code_root: Path | str | None = None) -> "Workspace":
        supplied = Path(path).expanduser()
        if supplied.is_symlink() or not supplied.is_dir():
            raise ReelsError(f"not an editor workspace directory: {supplied}")
        root = supplied.resolve()
        state_path = root / "state.json"
        manifest_path = root / "originals.json"
        _assert_path_no_symlink(state_path, root)
        _check_paths_no_links([state_path], internal_scope=root)
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReelsError(f"editor workspace has no valid activation state: {exc}") from exc
        if not isinstance(state, dict) or state.get("version") != _MANIFEST_VERSION or state.get("status") != "active":
            raise ReelsError("editor workspace snapshot is not active")
        _assert_path_no_symlink(manifest_path, root)
        _check_paths_no_links([manifest_path], internal_scope=root)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReelsError(f"active editor workspace has an invalid original-media manifest: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("version") != _MANIFEST_VERSION or not isinstance(manifest.get("originals"), dict):
            raise ReelsError("editor workspace original-media manifest is invalid")
        data_root = root / "data"
        if not data_root.is_dir() or data_root.is_symlink():
            raise ReelsError("editor workspace has no private data root")
        _check_no_links(data_root, internal_scope=root)
        history_paths = _committed_history_paths(root)
        _check_paths_no_links(history_paths, internal_scope=root)
        originals = manifest["originals"]
        media_root = Path(manifest.get("source", "")).resolve() / "library/media"
        source_root = media_root.parent.parent
        if _within(root, source_root):
            raise ReelsError("editor workspace must remain outside the snapshot source")
        for identifier, record in originals.items():
            if not isinstance(identifier, str) or not isinstance(record, dict):
                raise ReelsError("editor workspace original-media record is malformed")
            rel = PurePosixPath(identifier)
            if rel.is_absolute() or ".." in rel.parts or len(rel.parts) < 3 or rel.parts[:2] != ("library", "media"):
                raise ReelsError("editor workspace contains an unsafe original-media identifier")
            # Validate the recorded name, not a replacement symlink's target.
            # Changed originals remain loadable/offline so the last preview survives.
            resolved = Path(record.get("path", ""))
            if not _within(resolved, media_root):
                raise ReelsError("editor workspace original-media path escapes its registered root")
            expected_path = media_root.joinpath(*rel.parts[2:])
            if resolved != expected_path:
                raise ReelsError("editor workspace original-media identity does not match its identifier")
            if record.get("identity") is not None and not isinstance(record["identity"], dict):
                raise ReelsError("editor workspace original-media identity is malformed")
        if code_root is None:
            code = Path(__file__).resolve().parents[3]
        else:
            code = Path(code_root).expanduser().resolve()
        if not code.is_dir():
            raise ReelsError(f"editor code root is not a directory: {code}")
        project = Project(
            data_root,
            code_root=code,
            media_root=media_root,
            registered_media=originals,
        )
        return cls(root=root, data_root=data_root, code_root=code, project=project, originals=originals)

    def media_status(self) -> dict[str, dict[str, Any]]:
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
                _assert_no_symlink_components(path, self.project.media_dir)
                actual = _identity(path)
            except (OSError, ReelsError):
                actual = None
            state = "online" if actual == expected else "changed"
            status[identifier] = {"status": state, "assetId": record.get("asset_id")}
        return status

    def library_revision(self) -> str:
        """Fingerprint render inputs without rereading unchanged large metadata.

        JSON is content-addressed and cached by full stat identity. Ancillary
        media uses its file identity; original footage has a separate registered
        identity guard. Plans and worker output/cache trees are intentionally
        outside this library revision.
        """
        digest = hashlib.sha256()
        paths: list[Path] = []
        for relative in _SNAPSHOT_DIRS:
            if relative == "plans":
                continue
            base = self.data_root / relative
            components = [base]
            parent = base.parent
            while parent != self.data_root and _within(parent, self.data_root):
                components.append(parent)
                parent = parent.parent
            if any(component.is_symlink() for component in components):
                raise ReelsError(f"private render input contains a symlink: {relative}")
            if base.is_dir():
                paths.extend(_regular_files(base, reject_symlinks=True))
        for relative in _SNAPSHOT_FILES:
            path = self.data_root / relative
            if path.is_symlink():
                raise ReelsError(f"private render input contains a symlink: {relative}")
            if path.is_file():
                paths.append(path)
        live: set[str] = set()
        for path in sorted(paths):
            relative = path.relative_to(self.data_root).as_posix()
            if not _safe_name(Path(relative)):
                continue
            live.add(relative)
            identity = _signature(path)
            if path.suffix.lower() == ".json":
                cached = self._revision_cache.get(relative)
                if cached is None or cached[0] != identity:
                    payload = _read_stable(path, identity)
                    content = hashlib.sha256(payload).hexdigest()
                    self._revision_cache[relative] = (identity, content)
                else:
                    content = cached[1]
            else:
                # Ancillary media can be large; replacement and in-place edits
                # change inode/ctime/mtime even when byte length is unchanged.
                content = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            digest.update(relative.encode())
            digest.update(b"\0")
            digest.update(content.encode())
        self._revision_cache = {key: value for key, value in self._revision_cache.items() if key in live}
        digest.update(json.dumps(self._originals, sort_keys=True).encode())
        return digest.hexdigest()
