"""Explicit, fail-closed reconciliation into a new independent workspace.

Legacy snapshots have no merge base: every differing file requires a choice.
The old workspace remains the history archive; only current data and referenced
renderer snapshots cross the boundary. No candidate renderer code is executed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from ..util import ReelsError
from . import workspace as ws
from .document import ensure_node_ids
from .workspace import Workspace, init_workspace


class RefreshConflict(ReelsError):
    """Unresolved choices; ``report_path`` names the inactive candidate's report."""

    def __init__(self, report_path: Path):
        self.report_path = report_path
        super().__init__(f"workspace refresh needs explicit resolutions: {report_path}")


def _path(value: Path | str) -> Path:
    path = Path(value).expanduser().absolute()
    if ".." in path.parts:
        raise ReelsError("refresh paths cannot traverse directories")
    # Resolve the parent only after checking it: resolving first hides escapes.
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ReelsError(f"refresh path contains a symlink: {path}")
    return path.resolve()


def _regular(path: Path) -> dict[str, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ReelsError(f"refresh refuses symlinks, hardlinks and special files: {path}")
    return ws._signature(path)


def _inventory(root: Path) -> dict[str, dict[str, int]]:
    return {name: _regular(path) for name, path in ws._walk_snapshot(root).items()}


def _private_inventory(old: Workspace) -> dict[str, dict[str, int]]:
    files = {f"data/{name}": sig for name, sig in _inventory(old.data_root).items()}
    for path in [old.root / "state.json", old.root / "originals.json",
                 *ws._committed_history_paths(old.root)]:
        files[path.relative_to(old.root).as_posix()] = _regular(path)
    return files


def _payload(path: Path, signature: dict[str, int]) -> bytes:
    payload = ws._read_stable(path, signature)
    if path.suffix.lower() == ".json":
        try:
            value = ws._sanitize(json.loads(payload))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReelsError(f"invalid refresh JSON: {path}") from exc
        return _json(value)
    return payload


def _json(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, indent=2) + "\n").encode()


def _file_identity(payload: bytes | None) -> dict[str, Any] | None:
    return None if payload is None else {
        "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload),
    }


def _asset_original(name: str, payload: bytes | None, registry: dict) -> dict | None:
    if payload is not None and name.startswith("library/assets/") and name.endswith(".json"):
        value = json.loads(payload)
        if isinstance(value, dict):
            return registry.get(value.get("media"))
    return None


def _resolutions(value: Mapping[str, str] | None) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ReelsError("refresh resolutions must be a relative-data-path mapping")
    result = {}
    for name, choice in value.items():
        if (not isinstance(name, str) or not name or "\\" in name or "\0" in name
                or Path(name).is_absolute() or any(p in ("", ".", "..") for p in name.split("/"))
                or choice not in ("private", "source")):
            raise ReelsError("refresh resolutions require safe relative paths and private/source choices")
        result[name] = choice
    return result


def _selected_revisions(old: Workspace) -> dict[str, str]:
    """Read HEADs without invoking recovery/materialization on the old workspace."""
    selected = {}
    for path in ws._committed_history_paths(old.root):
        if path.name != "HEAD":
            continue
        head = json.loads(path.read_bytes())
        manifest = json.loads((path.parent / "commits" / f"{head['revision']}.json").read_bytes())
        revision = head.get("rendererRevision") or manifest.get("rendererRevision")
        if revision:
            for name in manifest.get("files", {}):
                if name.startswith("plans/") and name.endswith(".json"):
                    selected[name] = revision
    return selected


def _renderer_inventory(old: Workspace, revision: str, code_root: Path) -> dict[str, Any]:
    if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", revision):
        raise ReelsError("refresh selected an invalid renderer revision")
    root = old.root / "code/renderers" / revision
    ws._assert_path_no_symlink(root / "manifest.json", old.root)
    manifest = json.loads(ws._read_stable(root / "manifest.json", _regular(root / "manifest.json")))
    if not isinstance(manifest, dict) or manifest.get("rendererRevision") != revision:
        raise ReelsError(f"renderer manifest does not match selected revision: {revision}")
    source = root / "source"
    if source.is_symlink() or not source.is_dir():
        raise ReelsError(f"selected renderer source is unavailable: {revision}")
    found: dict[str, Any] = {"manifest.json": _regular(root / "manifest.json")}
    for current, dirs, files in os.walk(source, followlinks=False):
        base = Path(current)
        for name in sorted(dirs + files):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                if relative != "source/remotion/node_modules":
                    raise ReelsError(f"renderer snapshot contains a symlink: {relative}")
                dependencies = code_root / "remotion/node_modules"
                if (not dependencies.is_dir() or dependencies.is_symlink()
                        or path.resolve() != dependencies.resolve()):
                    raise ReelsError("renderer dependency binding must point to isolated code_root/remotion/node_modules")
                _path(dependencies)
                found[relative] = {"link": str(dependencies), **ws._signature(path)}
                if name in dirs:
                    dirs.remove(name)
                continue
            if not ws._safe_name(path.relative_to(source)):
                raise ReelsError(f"renderer snapshot contains a non-source/private file: {relative}")
            if path.is_dir():
                continue
            found[relative] = _regular(path)
    return found


def _copy_renderer(old: Workspace, stage: Path, revision: str, code_root: Path) -> dict:
    inventory = _renderer_inventory(old, revision, code_root)
    source = old.root / "code/renderers" / revision
    target = stage / "code/renderers" / revision
    for name, identity in inventory.items():
        destination = target / name
        if "link" in identity:
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            destination.symlink_to(identity["link"], target_is_directory=True)
        else:
            ws._write_private(destination, ws._read_stable(source / name, identity))
            destination.chmod(0o400)
    # Match persisted snapshot permissions without following dependency bindings.
    from .renderer import RendererManager
    RendererManager._make_source_readonly(target / "source")
    return inventory


def _initialize_history(stage: Path, name: str, plan: dict, revision: str, origin: dict) -> None:
    """Seed a new metadata-only root commit, including its materialization marker.

    This is the DocumentStore on-disk format; compiling here would execute the
    selected candidate code and prevent offline snapshots from being refreshed.
    """
    from .transactions import _json_bytes
    key = hashlib.sha256(name.encode()).hexdigest()[:24]
    history = stage / "history/documents" / key
    commit = str(uuid.uuid4())
    values = {name: plan}
    config = stage / "data/config.json"
    if config.is_file():
        values["config.json"] = json.loads(config.read_bytes())
    refs = {}
    for relative, value in values.items():
        payload = _json_bytes(value)
        digest = hashlib.sha256(payload).hexdigest()
        refs[relative] = digest
        destination = history / "objects" / f"{digest}.json"
        if not destination.exists():
            ws._write_private(destination, payload)
    manifest = {
        "revision": commit, "parent": None, "created": time.time(),
        "origin": "system", "reason": "initialize refreshed document",
        "affectedIds": [], "files": refs, "undoTarget": None, "redoTarget": None,
        "redoStack": [], "deleted": [], "previousFiles": dict(refs), "restoredFrom": None,
        "rendererRevision": revision, "preview": None,
        "sourceRef": f"code/renderers/{revision}/source", "refreshOrigin": origin,
    }
    ws._write_private(history / "commits" / f"{commit}.json", _json_bytes(manifest))
    ws._write_private(history / "HEAD", _json_bytes({
        "revision": commit, "rendererRevision": revision, "preview": None,
    }))
    ws._write_private(history / "MATERIALIZED", _json_bytes({"revision": commit}))


def refresh_workspace(
    source: Path | str,
    existing: Workspace | Path | str,
    target: Path | str,
    resolutions: Mapping[str, str] | None = None,
    keep_private: bool = False,
    code_root: Path | str | None = None,
) -> Workspace:
    """Fork a reconciled snapshot, publishing ``target`` only after validation.

    ``private`` retains a private-only file; ``source`` explicitly deletes it.
    ``keep_private`` supplies that choice for all differing/private-only files.
    New source paths are always added. Originals selected from the private side
    retain their recorded identity, even when offline. Failures leave scratch
    inactive and never write the existing workspace or the production source.
    """
    choices = _resolutions(resolutions)
    if not isinstance(keep_private, bool):
        raise ReelsError("keep_private must be a boolean")
    source_root = _path(source)
    old_root = _path(existing.root if isinstance(existing, Workspace) else existing)
    destination = _path(target)
    code = _path(code_root if code_root is not None else (
        existing.code_root if isinstance(existing, Workspace) else Path(__file__).resolve().parents[3]))
    if (ws._within(destination, source_root) or ws._within(destination, old_root)
            or ws._within(source_root, destination) or ws._within(old_root, destination)):
        raise ReelsError("refresh target must be independent of source and existing workspace")
    if destination.exists():
        raise ReelsError(f"refresh target already exists: {destination}")
    if (ws._within(code, source_root) or ws._within(code, old_root)
            or ws._within(code, destination)):
        raise ReelsError("refresh code_root must be isolated from source and workspace data")
    old = Workspace.load(old_root, code_root=code)
    manifest = json.loads((old.root / "originals.json").read_bytes())
    if Path(manifest["source"]).resolve() != source_root:
        raise ReelsError("refresh source must match the existing original-media registry")
    before_private = _private_inventory(old)
    before_source = _inventory(source_root)
    selected = _selected_revisions(old)
    # A revision name is immutable: capture referenced bytes before the source
    # snapshot starts, so a concurrent edit cannot be silently blessed as the
    # same selected code version.
    renderer_baselines = {}
    for name, signature in before_private.items():
        if name.startswith("data/plans/") and name.endswith(".json"):
            plan = json.loads(ws._read_stable(old.root / name, signature))
            editor = (plan.get("_editor") or {}) if isinstance(plan, dict) else {}
            if not isinstance(editor, dict):
                raise ReelsError(f"invalid private _editor metadata in {name}")
            revision = editor.get("rendererRevision") or selected.get(name[5:])
            if revision and revision not in renderer_baselines:
                renderer_baselines[revision] = _renderer_inventory(old, revision, code)
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.parent / f".{destination.name}.refresh-{uuid.uuid4().hex}"
    try:
        incoming = init_workspace(source_root, stage, code_root=code)
        ws._store_state(stage / "state.json", {"version": 1, "status": "refreshing"})
        source_registry = deepcopy(incoming._originals)
        before_originals = {}
        for identifier, record in {**old._originals, **source_registry}.items():
            original = Path(record["path"])
            ws._assert_no_symlink_components(original, source_root / "library/media")
            if original.exists() or original.is_symlink():
                _regular(original)
            before_originals[identifier] = ws._identity(original)
        private_names = {name[5:] for name in before_private if name.startswith("data/")}
        incoming_files = ws._walk_snapshot(incoming.data_root)
        unknown = set(choices) - (private_names | incoming_files.keys())
        if unknown:
            raise ReelsError(f"refresh resolutions name unknown data paths: {sorted(unknown)}")
        conflicts = []
        picked: dict[str, str] = {}
        for name in sorted(private_names | incoming_files.keys()):
            private = (_payload(old.data_root / name, before_private[f"data/{name}"])
                       if name in private_names else None)
            fresh = (_payload(incoming_files[name], _regular(incoming_files[name]))
                     if name in incoming_files else None)
            private_original = _asset_original(name, private, old._originals)
            fresh_original = _asset_original(name, fresh, source_registry)
            different = private is not None and (
                private != fresh or (private_original is not None and private_original != fresh_original))
            choice = choices.get(name) or ("private" if keep_private and private is not None else None)
            if different and choice is None:
                conflicts.append({
                    "path": name, "before": _file_identity(private), "incoming": _file_identity(fresh),
                    "beforeOriginal": private_original, "incomingOriginal": fresh_original,
                    "resolutionChoices": ["private", "source"],
                    "sourceDeletes": fresh is None,
                })
                continue
            choice = choice or ("source" if fresh is not None else "private")
            if choice == "private" and private is None:
                raise ReelsError(f"no private file exists for resolution: {name}")
            picked[name] = choice
            if choice == "private":
                path = incoming.data_root / name
                if path.exists():
                    path.unlink()
                ws._write_private(path, private)
            elif fresh is None:
                # The candidate has no such file; choosing source is deletion.
                continue
        if conflicts:
            report_path = stage / "refresh-conflicts.json"
            ws._write_private(report_path, _json({
                "version": 1, "status": "conflicted", "source": str(source_root),
                "existing": str(old.root), "target": str(destination), "conflicts": conflicts,
            }))
            raise RefreshConflict(report_path)

        origin = {"workspace": str(old.root), "source": str(source_root), "created": time.time()}
        renderer_inputs = {}
        plans = sorted((incoming.data_root / "plans").rglob("*.json"))
        for path in plans:
            name = path.relative_to(incoming.data_root).as_posix()
            plan = json.loads(path.read_bytes())
            editor = plan.setdefault("_editor", {})
            if not isinstance(editor, dict):
                raise ReelsError(f"invalid _editor metadata in {name}")
            # Source-selected plans keep private stable IDs/renderer selection,
            # never an incoming production-only renderer or preview binding.
            private_path = old.data_root / name
            private_editor = {}
            if name in private_names:
                prior = json.loads(ws._read_stable(private_path, before_private[f"data/{name}"]))
                private_editor = prior.get("_editor") or {}
                if not isinstance(private_editor, dict):
                    raise ReelsError(f"invalid private _editor metadata in {name}")
            if picked.get(name) == "source":
                for key in ("documentId", "nodes", "rendererRevision"):
                    editor.pop(key, None)
                    if key in private_editor:
                        editor[key] = deepcopy(private_editor[key])
            editor.pop("rendererPreview", None)
            revision = editor.get("rendererRevision") or selected.get(name)
            if revision:
                if revision not in renderer_inputs:
                    renderer_inputs[revision] = _copy_renderer(old, stage, revision, code)
                    if renderer_inputs[revision] != renderer_baselines.get(revision):
                        raise ReelsError("selected renderer changed during refresh; retry")
                editor["rendererRevision"] = revision
            ensure_node_ids(plan)
            ws._store_state(path, plan)
            if revision:
                _initialize_history(stage, name, plan, revision, origin)
            # Never-selected plans initialize normally on first DocumentStore
            # open, from these merged bytes and the isolated local code root.

        merged_registry = ws._validate_data(incoming.data_root, source_root / "library/media")
        for identifier, record in merged_registry.items():
            asset_name = f"library/assets/{record['asset_id']}.json"
            if picked.get(asset_name) == "private" and identifier in old._originals:
                previous = old._originals[identifier]
                if record["identity"] is not None and record["identity"] != previous["identity"]:
                    raise ReelsError(f"private original identity changed; refusing substitution: {identifier}")
                record["identity"] = deepcopy(previous["identity"])
        ws._store_state(stage / "originals.json", {
            "version": 1, "source": str(source_root), "originals": merged_registry,
        })
        ws._write_private(stage / "refresh-origin.json", _json({
            **origin, "resolutions": picked, "historyPolicy": "new roots; old workspace is archive",
        }))
        # Freeze checks include dropped incoming paths and all HEAD-reachable
        # old metadata, not only files eventually chosen for the merge.
        if _inventory(source_root) != before_source:
            raise ReelsError("source data changed during refresh; retry")
        if _private_inventory(old) != before_private:
            raise ReelsError("private data or HEAD changed during refresh; retry")
        for revision, inventory in renderer_inputs.items():
            if _renderer_inventory(old, revision, code) != inventory:
                raise ReelsError("selected renderer changed during refresh; retry")
        for identifier, identity in before_originals.items():
            original = source_root / identifier
            ws._assert_no_symlink_components(original, source_root / "library/media")
            if original.exists() or original.is_symlink():
                _regular(original)
            if ws._identity(original) != identity:
                raise ReelsError("original media changed during refresh; retry")
        # Verify the original identities captured by init, including source
        # assets that were later discarded by a private choice.
        for identifier, record in source_registry.items():
            if before_originals[identifier] != record["identity"]:
                raise ReelsError("source original changed after snapshot initialization; retry")
        ws._check_no_links(incoming.data_root)
        if _path(destination) != destination:
            raise ReelsError("refresh destination changed during staging")
        ws._store_state(stage / "state.json", {"version": 1, "status": "active"})
        Workspace.load(stage, code_root=code)
        ws._publish_without_replace(stage, destination)
    except Exception as exc:
        if stage.is_dir() and not stage.is_symlink():
            ws._store_state(stage / "state.json", {
                "version": 1, "status": "conflicted" if isinstance(exc, RefreshConflict) else "failed",
                "error": str(exc), "retry": True,
            })
        if isinstance(exc, RefreshConflict):
            raise
        raise ReelsError(f"refresh failed; candidate remains inactive at {stage}: {exc}") from exc
    return Workspace.load(destination, code_root=code)
