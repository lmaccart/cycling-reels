"""Allowlisted immutable renderer/effect source revisions."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from ..util import ReelsError
from .preview import PreviewManager, _atomic_json


_PYTHON_MODULES = {
    "scripts/reels/bleep.py",
    "scripts/reels/blur.py",
    "scripts/reels/exhibits.py",
    "scripts/reels/punch.py",
    "scripts/reels/progress.py",
    "scripts/reels/sphere.py",
}
_FIXED_TESTS = (
    "test_asset_updates.py",
    "test_search.py",
    "test_cutaways.py",
    "test_bleep.py",
    "test_exhibits.py",
    "test_blur.py",
    "test_titlecard.py",
    "test_moment_references.py",
    "test_kino.py",
    "test_mix_streams.py",
)
_COPY_OMIT = {".git", ".venv", "__pycache__", ".cache", "out", "node_modules"}
_MAX_SOURCE_BYTES = 2_000_000
_PROTECTED_REMOTION_SOURCES = {
    "remotion/src/editor-preview.tsx",
    "remotion/src/editor-preview.css",
    "remotion/src/editorStaticFile.ts",
}


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _source_path(path: str) -> PurePosixPath:
    if not isinstance(path, str) or not path or "\\" in path or "\x00" in path:
        raise ReelsError("renderer source path must be a non-empty relative POSIX path")
    parsed = PurePosixPath(path)
    if parsed.is_absolute() or any(part in ("", ".", "..") for part in parsed.parts):
        raise ReelsError("renderer source path cannot be absolute or traverse directories")
    normalized = parsed.as_posix()
    if normalized in _PROTECTED_REMOTION_SOURCES:
        raise ReelsError(f"renderer source path is protected: {normalized}")
    if normalized.startswith("remotion/src/"):
        return parsed
    if normalized in _PYTHON_MODULES:
        return parsed
    if normalized.startswith("scripts/reels/effects/") and normalized.endswith(".py"):
        return parsed
    if normalized.startswith("tests/renderer_effects/") and normalized.endswith(".py"):
        return parsed
    raise ReelsError(f"renderer source path is protected: {normalized}")


def _no_symlink_components(path: Path, root: Path) -> None:
    current = root
    if current.is_symlink():
        raise ReelsError("renderer source root cannot be a symlink")
    for part in path.relative_to(root).parts:
        current = current / part
        if current.is_symlink():
            raise ReelsError(f"renderer source path contains a symlink: {part}")


def _copy_tree(source: Path, destination: Path, *, extensions: set[str] | None = None) -> None:
    if source.is_symlink() or not source.is_dir():
        raise ReelsError(f"renderer snapshot source is missing: {source}")
    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        base = Path(current)
        rel = base.relative_to(source)
        dirs[:] = [name for name in sorted(dirs) if name not in _COPY_OMIT]
        for name in dirs + files:
            item = base / name
            if item.is_symlink():
                raise ReelsError(f"renderer source snapshot cannot follow a symlink: {item.relative_to(source)}")
        target_dir = destination / rel
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in sorted(files):
            item = base / name
            if extensions and item.suffix not in extensions:
                continue
            before = item.stat()
            target = target_dir / name
            shutil.copyfile(item, target)
            after = item.stat()
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
            ) or target.stat().st_size != before.st_size:
                raise ReelsError(f"renderer source changed while copying: {item.name}")
            target.chmod(0o600)


def _digest_changes(
    parent: str,
    normalized: list[tuple[str, str | None]],
    descriptors: list[dict] | None = None,
) -> str:
    digest = hashlib.sha256(parent.encode())
    for path, content in sorted(normalized):
        digest.update(path.encode())
        digest.update(b"\0")
        digest.update(b"<delete>" if content is None else content.encode("utf-8"))
        digest.update(b"\0")
    if descriptors:
        digest.update(json.dumps(descriptors, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
    return digest.hexdigest()


class RendererManager:
    """Read and validate renderer changes without importing candidate code in the broker.

    ``submit`` prepares a candidate revision and returns it to the transaction
    facade. Activation and any associated plan batch are intentionally left to
    that facade so both can become visible atomically.
    """

    def __init__(self, workspace):
        self.workspace = workspace
        self.root = Path(workspace.root) / "code/renderers"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.preview = PreviewManager(workspace)

    def _root_for_revision(self, revision: str | None) -> Path:
        if revision is None:
            revision = self._active_revision()
        if not isinstance(revision, str) or not revision or "/" in revision or "\\" in revision:
            raise ReelsError("invalid renderer revision")
        root = (self.root / revision / "source").resolve()
        head_path = Path(self.workspace.root) / "history/HEAD"
        has_committed_head = head_path.exists() or head_path.is_symlink()
        if not root.is_dir() and not has_committed_head and revision == self._code_revision():
            self._persist_baseline(revision)
            root = (self.root / revision / "source").resolve()
        if not _under(root, self.root.resolve()) or not root.is_dir() or root.is_symlink():
            raise ReelsError("renderer revision is unavailable; refusing to fall back to another code version")
        return root

    def _working_revision(self) -> str:
        """Match the document store's immutable initial renderer identity."""
        value = getattr(self.workspace, "renderer_revision", None)
        if callable(value):
            value = value()
        if value:
            return str(value)
        return self._code_revision()

    def _code_revision(self) -> str:
        digest = hashlib.sha256()
        code_root = Path(self.workspace.code_root).resolve()
        for name in ("render.py", "mix.py", "music.py", "punch.py", "sphere.py", "exhibits.py"):
            path = code_root / "scripts" / "reels" / name
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(name.encode())
        return digest.hexdigest()[:24]

    def _active_revision(self) -> str:
        head_path = Path(self.workspace.root) / "history/HEAD"
        if head_path.exists() or head_path.is_symlink():
            if head_path.is_symlink() or not head_path.is_file():
                raise ReelsError("active renderer revision metadata is unsafe")
            try:
                head = json.loads(head_path.read_text(encoding="utf-8"))
                revision = head.get("rendererRevision")
                if revision:
                    return str(revision)
                document = str(head.get("revision") or "")
                if not document:
                    raise ReelsError("active document HEAD has no renderer revision")
                manifest_path = Path(self.workspace.root) / "history/commits" / f"{document}.json"
                if manifest_path.is_symlink() or not manifest_path.is_file():
                    raise ReelsError("active document has no safe renderer revision manifest")
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                if not manifest.get("rendererRevision"):
                    raise ReelsError("active document manifest has no renderer revision")
                return str(manifest["rendererRevision"])
            except ReelsError:
                raise
            except (OSError, ValueError, AttributeError) as exc:
                raise ReelsError(f"active renderer revision metadata is invalid: {exc}") from exc
        return self._working_revision()

    def resolve_renderer(self, revision: str) -> Path:
        """Resolve an exact persisted renderer revision, creating only its initial baseline."""
        return self._root_for_revision(revision)

    def _persist_baseline(self, revision: str) -> None:
        candidate = self.root / revision
        if candidate.exists() or candidate.is_symlink():
            raise ReelsError("renderer baseline is incomplete; refusing to replace an immutable revision")
        source = candidate / "source"
        source.mkdir(parents=True, mode=0o700)
        try:
            self._copy_baseline(source, from_root=Path(self.workspace.code_root).resolve())
            manifest = {
                "rendererRevision": revision,
                "parentRendererRevision": None,
                "state": "baseline",
                "changedPaths": [],
            }
            _atomic_json(candidate / "manifest.json", manifest)
            self._make_source_readonly(source)
        except Exception:
            shutil.rmtree(candidate, ignore_errors=True)
            raise

    def read(self, path: str, revision: str | None = None) -> dict:
        relative = _source_path(path)
        root = self._root_for_revision(revision)
        target = root.joinpath(*relative.parts)
        _no_symlink_components(target, root)
        if not target.is_file():
            raise ReelsError(f"renderer source file does not exist: {relative.as_posix()}")
        if target.stat().st_size > _MAX_SOURCE_BYTES:
            raise ReelsError("renderer source file is too large to read through the editor")
        return {
            "path": relative.as_posix(),
            "content": target.read_text(encoding="utf-8"),
            "rendererRevision": revision,
        }

    @staticmethod
    def _changes(changes: Any) -> list[tuple[str, str | None]]:
        if isinstance(changes, dict):
            items = [{"path": key, "content": value} for key, value in changes.items()]
        elif isinstance(changes, list):
            items = changes
        else:
            raise ReelsError("renderer changes must be a path-to-content map or a list of edits")
        normalized: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for item in items:
            if not isinstance(item, dict) or set(item) - {"path", "content"} or "path" not in item:
                raise ReelsError("each renderer change must contain only path and content")
            path = _source_path(item["path"]).as_posix()
            if path in seen:
                raise ReelsError(f"renderer change repeats {path}")
            seen.add(path)
            content = item.get("content")
            if content is not None and (not isinstance(content, str) or len(content.encode("utf-8")) > _MAX_SOURCE_BYTES):
                raise ReelsError(f"renderer source content is invalid or too large: {path}")
            normalized.append((path, content))
        if not normalized:
            raise ReelsError("renderer submission must contain at least one source change")
        return normalized

    def _copy_baseline(self, destination: Path, *, from_root: Path | None = None) -> None:
        code_root = Path(from_root or self.workspace.code_root).resolve()
        scripts = code_root / "scripts"
        _copy_tree(scripts, destination / "scripts", extensions={".py", ".swift"})
        _copy_tree(code_root / "remotion/src", destination / "remotion/src")
        for relative in (
            "remotion/package.json",
            "remotion/package-lock.json",
            "remotion/tsconfig.json",
            "remotion/editor-preview.html",
            "remotion/editor-preview.tsx",
            "remotion/editor-preview.css",
            "remotion/editorStaticFile.ts",
            "remotion/scripts/build-editor-preview.mjs",
        ):
            source = code_root / relative
            if source.is_file() and not source.is_symlink():
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, target)
                target.chmod(0o600)
        test_root = code_root / "tests"
        copied_tests = destination / "tests"
        copied_tests.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in (*_FIXED_TESTS, "helpers.py"):
            source = test_root / name
            if source.is_file() and not source.is_symlink():
                target = copied_tests / name
                shutil.copyfile(source, target)
                target.chmod(0o600)
        dependencies = code_root / "remotion/node_modules"
        if dependencies.is_dir():
            target = destination / "remotion/node_modules"
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target.symlink_to(dependencies.resolve(), target_is_directory=True)

    def submit(
        self,
        changes: Any,
        expected: dict | str | None = None,
        plan_batch: dict | None = None,
        context=None,
        snapshot: dict | None = None,
        descriptors: list[dict] | None = None,
    ) -> dict:
        if snapshot is None and isinstance(context, dict):
            snapshot = context.get("snapshot")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("plan"), dict):
            raise ReelsError("renderer submission requires the frozen candidate plan snapshot for its draft render")
        revisions = snapshot.get("revisions") or {}
        parent_revision = str(revisions.get("renderer", ""))
        if not parent_revision:
            raise ReelsError("renderer submission is missing its expected renderer revision")
        expected_revision = expected.get("renderer") if isinstance(expected, dict) else expected
        if expected_revision is not None and str(expected_revision) != parent_revision:
            raise ReelsError("renderer revision conflict; submit against the current source revision")
        edits = self._changes(changes)
        candidate_id = _digest_changes(parent_revision, edits, descriptors)
        candidate_dir = self.root / candidate_id
        source_root = candidate_dir / "source"
        if candidate_dir.exists() or candidate_dir.is_symlink():
            raise ReelsError("renderer candidate already exists; revisions are immutable")
        source_root.mkdir(parents=True, mode=0o700)
        try:
            parent_root = self._root_for_revision(parent_revision)
            self._copy_baseline(source_root, from_root=parent_root)
            for relative, content in edits:
                target = source_root.joinpath(*PurePosixPath(relative).parts)
                if not _under(target.resolve(strict=False), source_root.resolve()):
                    raise ReelsError("renderer source change escaped its immutable snapshot")
                _no_symlink_components(target, source_root)
                if content is None:
                    if not target.is_file():
                        raise ReelsError(f"cannot remove missing renderer source file: {relative}")
                    target.unlink()
                    continue
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with target.open("w", encoding="utf-8", newline="\n") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                target.chmod(0o600)
            manifest = {
                "rendererRevision": candidate_id,
                "parentRendererRevision": parent_revision,
                "state": "candidate",
                "changedPaths": [path for path, _ in edits],
                "planBatch": plan_batch,
                "descriptors": descriptors or [],
            }
            _atomic_json(candidate_dir / "manifest.json", manifest)
            self._make_source_readonly(source_root)
        except Exception:
            # Only this newly-created candidate path is owned by this request.
            shutil.rmtree(candidate_dir, ignore_errors=True)
            raise

        job_root = self.preview.worker_root / f"renderer-{uuid.uuid4().hex}"
        job_root.mkdir(parents=True, mode=0o700)
        data_root = self.preview._copy_inputs(snapshot, job_root)
        # Validate directly from the immutable read-only candidate source.
        # Copying it under jobRoot would let candidate code rewrite trusted
        # validation scripts because that scratch tree is writable.
        code_root = source_root.resolve(strict=True)
        revision_dir = self.preview.preview_root / "revisions" / f"renderer-{candidate_id[:20]}-{uuid.uuid4().hex[:8]}"
        job_path, result_path = job_root / "validate.json", job_root / "result.json"
        preview_job = {
            "jobPath": str(job_path),
            "jobRoot": str(job_root),
            "dataRoot": str(data_root),
            "revisionDir": str(revision_dir),
            "resultPath": str(result_path),
            "mediaRoot": str(self.workspace.project.media_dir.resolve()),
            "originals": self.workspace._originals,
            "plan": snapshot["plan"],
            "revisions": {**revisions, "renderer": candidate_id},
            "mode": "exact",
            "action": "prepare",
        }
        worker_job = {
            "action": "validate",
            "jobPath": str(job_path),
            "jobRoot": str(job_root),
            "dataRoot": str(data_root),
            "revisionDir": str(revision_dir),
            "resultPath": str(result_path),
            "codeRoot": str(code_root),
            "previewJob": preview_job,
            "revisions": preview_job["revisions"],
        }
        try:
            validation = self.preview._run_worker(
                worker_job,
                context=context if not isinstance(context, dict) else context.get("jobContext"),
                code_root=code_root,
                timeout=1800,
            )
        except Exception as exc:
            manifest.update(state="failed", error=str(exc))
            _atomic_json(candidate_dir / "manifest.json", manifest)
            raise
        if isinstance(validation.get("render"), dict):
            try:
                self.preview._validate_preview_manifest(validation["render"])
            except Exception as exc:
                manifest.update(state="failed", error=str(exc))
                _atomic_json(candidate_dir / "manifest.json", manifest)
                raise
        manifest.update(state="validated", preview=validation.get("render"))
        _atomic_json(candidate_dir / "manifest.json", manifest)
        return {
            "candidateRevision": candidate_id,
            "parentRendererRevision": parent_revision,
            "sourceRoot": str(source_root),
            "changedPaths": manifest["changedPaths"],
            "checks": validation.get("checks", []),
            "preview": validation.get("render"),
            "planBatch": plan_batch,
            "activation": "transaction-facade-required",
            "message": "Candidate is validated but not active. Atomically activate this source, its plan batch, and matching preview through the document transaction facade.",
        }

    def validate_candidate(
        self,
        changes: Any,
        snapshot: dict,
        descriptors: list[dict] | None = None,
        context=None,
        operations: list[dict] | None = None,
    ) -> dict:
        """Validate a candidate source and the already-applied frozen edit plan.

        Operation application is owned by DocumentStore because timing and
        generated-overlay edits depend on its compiled timeline. The facade
        supplies ``candidateSnapshot`` after applying the operation batch to
        the expected snapshot; this method refuses to render the unedited plan.
        """
        if not isinstance(snapshot, dict):
            raise ReelsError("renderer candidate requires an immutable document snapshot")
        operations = operations or []
        candidate_snapshot = snapshot
        if operations:
            candidate_snapshot = snapshot.get("candidateSnapshot")
            if not isinstance(candidate_snapshot, dict) or not isinstance(candidate_snapshot.get("plan"), dict):
                raise ReelsError(
                    "renderer operations must be applied to a frozen candidateSnapshot before source validation"
                )
            base_revisions = snapshot.get("revisions") or {}
            candidate_revisions = candidate_snapshot.get("baseRevisions") or candidate_snapshot.get("revisions") or {}
            for key in ("document", "library", "renderer"):
                if str(candidate_revisions.get(key, "")) != str(base_revisions.get(key, "")):
                    raise ReelsError("candidateSnapshot does not match the frozen renderer submission revisions")
            candidate_snapshot = {
                **snapshot,
                **candidate_snapshot,
                "revisions": dict(base_revisions),
            }

        from .schema import validate_descriptors

        validated_descriptors = validate_descriptors(descriptors)
        context_value = context
        if isinstance(context_value, dict):
            context_value = context_value.get("jobContext")
        result = self.submit(
            changes,
            expected=(snapshot.get("revisions") or {}).get("renderer"),
            plan_batch=operations or None,
            context=context_value,
            snapshot=candidate_snapshot,
            descriptors=validated_descriptors,
        )
        render = result.get("preview")
        if not isinstance(render, dict):
            raise ReelsError("candidate validation did not return a prepared exact preview")
        static_dir = Path(str(render.get("staticDir", ""))).resolve()
        workspace_root = Path(self.workspace.root).resolve()
        if not _under(static_dir, workspace_root):
            raise ReelsError("candidate preview escaped the private editor workspace")
        input_revisions = snapshot.get("revisions") or {}
        preview = {
            **render,
            "ref": static_dir.relative_to(workspace_root).as_posix(),
            "rendererRevision": result["candidateRevision"],
            "documentRevision": str(input_revisions.get("document", "")),
            "inputDocumentRevision": str(input_revisions.get("document", "")),
        }
        return {
            **result,
            "rendererRevision": result["candidateRevision"],
            "preview": preview,
            "descriptors": validated_descriptors,
        }

    @staticmethod
    def _make_source_readonly(root: Path) -> None:
        for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
            base = Path(current)
            for name in files:
                path = base / name
                if path.is_symlink():
                    raise ReelsError("candidate source snapshots may not contain file symlinks")
                path.chmod(0o400)
            for name in dirs:
                path = base / name
                if path.is_symlink():
                    if path.name != "node_modules":
                        raise ReelsError("candidate source snapshots may not contain symlinks")
                    continue
                path.chmod(0o500)
        root.chmod(0o500)
