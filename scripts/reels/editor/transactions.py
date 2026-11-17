"""Transactional, revisioned editing of authoritative reel-plan JSON."""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import stat
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from copy import copy, deepcopy
from pathlib import Path
from typing import Any, Callable

from .. import grade as grade_mod
from ..config import DEFAULTS, Project, deep_merge
from ..util import ReelsError, read_json
from .document import (
    canonical_json,
    editing_snapshot,
    ensure_node_ids,
    fingerprint,
    node_location,
    normalize_compiled,
    pointer,
    pointer_get,
    pointer_parent,
)
from .timing import seconds_to_frames, source_duration, split_segment, trim_segment, trim_local_source

_WORKSPACE_LOCKS: dict[str, threading.RLock] = {}
_WORKSPACE_LOCKS_GUARD = threading.Lock()


class DocumentError(Exception):
    """Expected editor failure suitable for a structured protocol response."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details or {}

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".editor-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:  # mkstemp makes 0600 files; keep an existing file's mode, else the normal umask default
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
        try:
            dfd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            pass
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _json_bytes(value: Any, indent: int = 2) -> bytes:
    return (json.dumps(value, indent=indent, ensure_ascii=False) + "\n").encode("utf-8")


def _existing_indent(path: Path, default: int = 2) -> int:
    """Indent width of an existing JSON file, so edits don't reformat the whole plan."""
    try:
        with path.open("r", encoding="utf-8") as stream:
            stream.readline()
            second = stream.readline()
    except (OSError, UnicodeDecodeError):
        return default
    width = len(second) - len(second.lstrip(" "))
    return width if 1 <= width <= 8 and second.strip() else default


EDITOR_KEY = "_editor"
SIDECAR_VERSION = 1


def _is_plan_rel(rel: str) -> bool:
    parts = Path(rel).parts
    return len(parts) == 2 and parts[0] == "plans" and rel.endswith(".json")


def _without_editor(rel: str, value: Any) -> Any:
    """The plan as it exists on disk: real plan content only, never ``_editor`` metadata."""
    if _is_plan_rel(rel) and isinstance(value, dict) and EDITOR_KEY in value:
        return {key: child for key, child in value.items() if key != EDITOR_KEY}
    return value


def _same_json(left: Any, right: Any) -> bool:
    return _json_bytes(left) == _json_bytes(right)


def _read_object(path: Path, default: Any = None) -> Any:
    try:
        return read_json(path)
    except FileNotFoundError:
        return default


def _worker_compile(
    project: Project,
    plan: dict[str, Any],
    transcript_overrides: dict[str, Any],
    *,
    process_started: Callable[[subprocess.Popen], None] | None = None,
    process_finished: Callable[[subprocess.Popen], None] | None = None,
    originals: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run renderer build outside the broker and against private worker data.

    All writes are confined by the trusted macOS profile to disposable worker
    data. Registered footage is resolved explicitly, never via a writable link.
    """
    code_root = project.code_root
    if not (code_root / "scripts/reels/render.py").is_file():
        if project.read_only_media or code_root != project.root:
            raise DocumentError("renderer_unavailable", "recorded renderer source is incomplete")
        code_root = Path(__file__).resolve().parents[3]
    package_root = code_root / "scripts"
    worker = r'''
import json, os, re, shutil, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from reels.config import Project
from reels.render import build
import reels.render as renderer

# Trusted worker-only instrumentation. A str subclass carries the helper's
# explicit raw locator through normalization; copied/retimed/dropped beat dicts
# then keep exact provenance even when two raw captions have identical text.
class LocatedText(str):
    pass
def traced_text_helper(original, field):
    def wrapped(*args, **kwargs):
        screen = renderer.screen_text
        def located(value, warnings, where):
            text = LocatedText(screen(value, warnings, where))
            m = re.fullmatch(r"segment (\d+) (text|stats)\[(\d+)\]", where)
            root = re.fullmatch(r"text\[(\d+)\]", where)
            text.locator = (f"/segments/{int(m[1])-1}/{m[2]}/{m[3]}" if m else
                            f"/text/{root[1]}" if root else None)
            return text
        renderer.screen_text = located
        try:
            values = original(*args, **kwargs)
            for value in values:
                locator = getattr(value.get(field), "locator", None)
                if locator:
                    value["_editorPath"] = locator
            return values
        finally:
            renderer.screen_text = screen
    return wrapped
for helper, field in (("_beats", "text"), ("_stats", "label")):
    original = getattr(renderer, helper, None)
    if callable(original) and callable(getattr(renderer, "screen_text", None)):
        setattr(renderer, helper, traced_text_helper(original, field))
original_exhibits = getattr(renderer, "build_exhibits", None)
if callable(original_exhibits):
    def located_exhibits(*args, **kwargs):
        values = original_exhibits(*args, **kwargs)
        for value in values:
            m = re.fullmatch(r"(?:segment (\d+) )?exhibit (\d+)", value.get("where", ""))
            if m:
                value["props"]["_editorPath"] = (f"/segments/{int(m[1])-1}" if m[1] else "") + f"/exhibits/{int(m[2])-1}"
        return values
    renderer.build_exhibits = located_exhibits

source, scratch, plan_path, overrides_path, result_path = map(Path, sys.argv[2:])
def copy_json_tree(src, dst):
    if not src.exists(): return
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.glob("*.json"):
        if item.is_file(): shutil.copy2(item, dst / item.name)
def link_dir(src, dst):
    if src.exists(): dst.parent.mkdir(parents=True, exist_ok=True); dst.symlink_to(src.resolve(), target_is_directory=True)

(scratch / "library").mkdir(parents=True, exist_ok=True)
copy_json_tree(source / "library" / "assets", scratch / "library" / "assets")
copy_json_tree(source / "library" / "transcripts", scratch / "library" / "transcripts")
link_dir(source / "library" / "media", scratch / "library" / "media")
copy_json_tree(source / "music", scratch / "music")
for item in (source / "music").glob("*") if (source / "music").exists() else []:
    if item.is_file() and item.suffix.lower() != ".json":
        target = scratch / "music" / item.name
        if not target.exists(): shutil.copyfile(item, target)
for name in ("exhibits", "sfx", "library/sounds", "library/telemetry", "strava", "learning", "models"):
    if (source / name).is_dir(): shutil.copytree(source / name, scratch / name)
if (source / "config.json").exists(): shutil.copy2(source / "config.json", scratch / "config.json")
for name in ("library/progress.json", "progress.json"):
    if (source / name).is_file():
        (scratch / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / name, scratch / name)
for rel, value in json.loads(overrides_path.read_text(encoding="utf-8")).items():
    if rel.startswith("__editor_"): continue
    target = (scratch / rel).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
for rel in json.loads(overrides_path.read_text(encoding="utf-8")).get("__editor_deleted_files__", []):
    target = (scratch / rel).resolve()
    try:
        target.unlink()
    except FileNotFoundError:
        pass

media = json.loads(overrides_path.read_text(encoding="utf-8")).get("__editor_media__", {})
p = Project(scratch, code_root=Path(sys.argv[1]).parent, **media)
plan = json.loads(plan_path.read_text(encoding="utf-8"))
if not plan.get("segments") and not plan.get("timeline"):
    result_path.write_text(json.dumps({"fps": int(p.cfg("render", "fps") or 30), "total": 0,
        "warnings": ["No footage yet: add a clip from the library to start."],
        "segs": [], "props": {}, "sound": {"cues": []}, "loop": None,
        "timelineCapabilities": getattr(renderer, "TIMELINE_CAPABILITIES", {})}))
    sys.exit(0)
built = build(p, plan, **({"aligned": True,
    "timing_only": bool(json.loads(overrides_path.read_text()).get("__editor_timing_only__"))}
    if hasattr(renderer, "derive_render_plan") else {}))
editing = built.get("editing", built)
# Match retained renderer identities, never enabled-list positions: frozen or
# compact renderer output may renumber segments instead of preserving indexes.
playback = {sg.index: sg for sg in built["segs"] if not getattr(sg, "disabled", False)}
retained = {sg.index for sg in editing["segs"] if not getattr(sg, "disabled", False)}
if playback.keys() != retained or len(playback) != len(built["segs"]):
    playback = {}
segs = []
for sg in editing["segs"]:
    rendered = playback.get(sg.index) if not getattr(sg, "disabled", False) else None
    spec = {k:v for k,v in sg.spec.items() if k != "_cutaways"}
    cuts = []
    for cut in sg.spec.get("_cutaways") or []:
        cs = cut["seg"]
        match = re.search(r"cutaway (\d+)$", cut.get("where", ""))
        cuts.append({"start": cut["start"], "frames": cs.frames, "assetId": cs.asset["id"],
                     "kind": cs.kind, "rawIndex": int(match[1])-1 if match else None,
                     "sourceDuration": cs.asset.get("probe", {}).get("duration"),
                     "spec": {k:v for k,v in cs.spec.items() if not k.startswith("_")}})
    segs.append({"index":sg.index, "kind":sg.kind, "asset":{"id":sg.asset["id"]},
                 "sourceDuration":sg.asset.get("probe", {}).get("duration"),
                 "hasAudio":bool(sg.asset.get("probe", {}).get("hasAudio")),
                 "spec":spec, "frames":sg.frames, "trans":sg.trans, "start":sg.start,
                 "cutaways":cuts, **({"disabled": True} if getattr(sg, "disabled", False) else {}),
                 **({"playbackStartFrame": rendered.start,
                     "playbackEndFrame": rendered.start + rendered.frames} if rendered is not None else {})})
sound = dict(built.get("sound") or {})
sound["cues"] = [{k:v for k,v in cue.items() if k != "path"} for cue in sound.get("cues") or []]
result = {"fps":built["fps"], "total":built["total"], "warnings":built["warnings"],
           "editingDurationFrames": editing["total"],
           "segs":segs, "props":built["props"], "sound":sound, "loop":built.get("loop")}
if editing is not built:
    result.update(editingProps=editing["props"], editingSound=editing["sound"])
if built.get("timelineCapabilities"):
    result["timelineCapabilities"] = built["timelineCapabilities"]
result_path.write_text(json.dumps(result, ensure_ascii=False, default=lambda value: sorted(value) if isinstance(value, set) else str(value)), encoding="utf-8")
'''
    scratch_parent = project.root / ".cache/editor-compile"
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="build-", dir=scratch_parent) as temp:
        worker_dir = Path(temp)
        plan_path = worker_dir / "plan.json"
        override_path = worker_dir / "overrides.json"
        result_path = worker_dir / "result.json"
        _atomic_bytes(plan_path, _json_bytes(plan))
        overrides = deepcopy(transcript_overrides)
        if project.read_only_media:
            overrides["__editor_media__"] = {"media_root": str(project.media_dir)}
        _atomic_bytes(override_path, _json_bytes(overrides))
        cmd = [
            sys.executable, "-c", worker, str(package_root), str(project.root),
            str(worker_dir / "private"), str(plan_path), str(override_path), str(result_path),
        ]
        from types import SimpleNamespace
        from .preview import _path_roots, _profile_text, _safe_env, _executables
        sandbox = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
        if not sandbox:
            raise DocumentError("sandbox_unavailable", "renderer compilation requires the trusted macOS sandbox")
        workspace = SimpleNamespace(project=project, root=project.root, data_root=project.root, code_root=code_root,
                                    _originals=originals or {})
        reads, _ = _path_roots(workspace, code_root, worker_dir, worker_dir, worker_dir, worker_dir)
        # Compilation freezes these private inputs inside its sandbox. Unlike
        # preview/export, it has not received a pre-copied data snapshot yet.
        # Do not restore a checkout-wide or whole-workspace read grant.
        for relative in (
            "library/assets", "library/transcripts", "library/sounds", "library/telemetry",
            "music", "sfx", "exhibits", "strava", "learning", "models",
            "config.json", "library/progress.json", "progress.json",
        ):
            path = project.root / relative
            if path.exists():
                reads.append(path.resolve())
        if not project.read_only_media and project.media_dir.is_dir():
            # Ordinary throwaway/test Projects own their media locally.
            reads.append(project.media_dir.resolve())
        # Only this compile's scratch is writable, never source/private metadata.
        profile = worker_dir / "compile.sb"
        profile_text = _profile_text(reads, [worker_dir.resolve()], _executables(workspace, code_root))
        # dyld/Python realpath traverses ancestors, including "/" itself.
        # Literal directory grants do not expose their child file contents.
        ancestors = {parent for root in reads for parent in root.parents}
        profile_text += "\n".join(f"(allow file-read* (literal {json.dumps(str(path))}))"
                                  for path in sorted(ancestors, key=str)) + "\n"
        profile.write_text(profile_text, encoding="utf-8")
        env = _safe_env(workspace, worker_dir, code_root)
        process = subprocess.Popen([sandbox, "-f", str(profile), *cmd], cwd=worker_dir, env=env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        if process_started:
            process_started(process)
        try:
            try:
                stdout, stderr = process.communicate(timeout=180)
            except subprocess.TimeoutExpired:
                from .jobs import JobContext
                JobContext._terminate(process)
                stdout, stderr = process.communicate()
                raise DocumentError("validation_timeout", "renderer worker exceeded the 180 second validation limit")
            if process.returncode:
                message = stderr.strip() or stdout.strip() or "renderer worker failed"
                raise DocumentError("validation_failed", message[-4000:])
        finally:
            from .jobs import JobContext
            JobContext._terminate(process)
            if process_finished:
                process_finished(process)
        try:
            return read_json(result_path)
        except Exception as exc:  # noqa: BLE001
            raise DocumentError("worker_protocol", f"renderer worker returned invalid data: {exc}") from exc


class DocumentStore:
    """Facade for raw plan reads, typed operations, journal commits and recovery."""

    def __init__(
        self,
        workspace: Any,
        plan_path: str | Path,
        *,
        compiler: Callable[..., dict[str, Any]] | None = None,
        renderer_revision: str | None = None,
    ):
        self.workspace = workspace
        self.project: Project = workspace.project
        self.data_root = Path(getattr(workspace, "data_root", self.project.root)).resolve()
        self.root = Path(getattr(workspace, "root", self.data_root)).resolve()
        candidate = self.project.abs(plan_path)
        self.plan_path = candidate.resolve()
        try:
            self.plan_rel = self.plan_path.relative_to(self.data_root).as_posix()
        except ValueError as exc:
            raise DocumentError("path_denied", "plan must be inside the private editor data root") from exc
        # Independent undo chains, with a shared workspace lock for shared JSON.
        # A different renderer source starts a fresh history: the live editor
        # always previews and exports with the repo's current renderer code.
        key_source = self.plan_rel
        live_revision = getattr(workspace, "renderer_revision", None)
        if renderer_revision is None and callable(live_revision):
            key_source = f"{self.plan_rel}\0{live_revision()}"
        document_key = hashlib.sha256(key_source.encode()).hexdigest()[:24]
        self.history = self.root / "history" / "documents" / document_key
        self.objects = self.history / "objects"
        self.manifests = self.history / "commits"
        self.head_path = self.history / "HEAD"
        self.materialized_path = self.history / "MATERIALIZED"
        self.compiler = compiler or self._run_worker_compiler
        self._renderer_revision_override = renderer_revision
        with _WORKSPACE_LOCKS_GUARD:
            self._lock = _WORKSPACE_LOCKS.setdefault(str(self.root), threading.RLock())
        self._lease: dict[str, Any] | None = None
        self._redo_target: str | None = None
        self._current_compiled: dict[str, Any] | None = None
        self._transcript_cache: dict[str, dict[str, Any]] = {}
        self._revision_compilers: dict[str, Callable[..., dict[str, Any]]] = {}
        self._compile_state = threading.local()
        self._cancel_guard = threading.Lock()
        self._epochs: dict[str, int] = {"manual": 0, "ai": 0, "renderer": 0, "undo": 0, "redo": 0, "system": 0, "external": 0}
        self._active_processes: dict[str, list[subprocess.Popen]] = {}
        self._compile_token: tuple[str, int] | None = None
        self._closed = False
        self._prepared_candidates: dict[str, dict[str, Any]] = {}
        self._warm_pool = None
        self._compile_pool_lease = None
        if self._disk_compile_cache_enabled():
            from .compile_worker import acquire_document_compiler
            import weakref
            self._compile_pool_lease = acquire_document_compiler(workspace)
            weakref.finalize(self, self._compile_pool_lease.close)
        try:
            self.history.mkdir(parents=True, exist_ok=True)
            self.objects.mkdir(parents=True, exist_ok=True)
            self.manifests.mkdir(parents=True, exist_ok=True)
            with self._lock:
                self._recover_pending_histories()
                self._open_or_initialize()
        except BaseException:
            self.close()
            raise
        if self._disk_compile_cache_enabled() and self._warm_pool is None:
            self._start_warm_worker()

    def _run_worker_compiler(self, project: Project, plan: dict[str, Any], overrides: dict[str, Any],
                             renderer_revision: str | None = None) -> dict[str, Any]:
        project = self._project_for_renderer(renderer_revision, project)
        token = self._compile_token
        origin = token[0] if token else getattr(self._compile_state, "origin", "system")
        timing_only = getattr(self._compile_state, "timing_only", False)
        if (origin in ("manual", "system") and not timing_only
                and not getattr(self._compile_state, "force_cold", False)
                and (not self._head_revision() or renderer_revision == self._renderer_revision())):
            from .compile_worker import WorkerUnavailable, WorkerRejected, WorkerCancelled
            try:
                return self._warm_compiler().compile(
                    project, plan, overrides, str(renderer_revision), self._library_revision(),
                    cancelled=lambda: self._closed or (token is not None and self._epoch(origin) != token[1]),
                )
            except WorkerUnavailable:
                if self._closed or (token is not None and self._epoch(origin) != token[1]):
                    raise DocumentError("cancelled", "validation was cancelled before cold fallback")
                invalidate = getattr(self.workspace, "invalidate_library_revision", None)
                if callable(invalidate):
                    invalidate()
                pass  # The unchanged cold sandbox is the infrastructure fallback.
            except WorkerRejected as exc:
                if self._closed or (token is not None and self._epoch(origin) != token[1]):
                    raise DocumentError("cancelled", "validation was cancelled during renderer rejection") from exc
                raise DocumentError(exc.code, str(exc), exc.details) from exc
            except WorkerCancelled as exc:
                raise DocumentError("cancelled", str(exc)) from exc
        def started(process: subprocess.Popen) -> None:
            with self._cancel_guard:
                self._active_processes.setdefault(origin, []).append(process)
                if token and self._epochs.get(origin, 0) != token[1]:
                    from .jobs import JobContext
                    JobContext._terminate(process)
        def finished(process: subprocess.Popen) -> None:
            with self._cancel_guard:
                current = self._active_processes.get(origin, [])
                if process in current:
                    current.remove(process)
        if self._closed or (token is not None and self._epoch(origin) != token[1]):
            raise DocumentError("cancelled", "validation was cancelled before cold fallback")
        if timing_only:
            overrides = {**overrides, "__editor_timing_only__": True}
        return _worker_compile(project, plan, overrides, process_started=started, process_finished=finished,
                               originals=getattr(self.workspace, "_originals", None))

    def _warm_compiler(self):
        # A store lock is deliberately not acquired here: validation released it.
        with self._cancel_guard:
            if self._closed:
                raise DocumentError("cancelled", "document store is closed")
            if self._warm_pool is None:
                if self._compile_pool_lease is None:
                    from .compile_worker import acquire_document_compiler
                    import weakref
                    self._compile_pool_lease = acquire_document_compiler(self.workspace)
                    weakref.finalize(self, self._compile_pool_lease.close)
                self._warm_pool = self._compile_pool_lease.pool
            return self._warm_pool

    def _start_warm_worker(self):
        def start():
            try:
                with self._lock:
                    if self._closed or self._offline_media(self._current_raw_plan()):
                        return
                    revision = self._renderer_revision()
                    project = self._project_for_renderer(revision)
                self._warm_compiler().prewarm(project, revision)
            except Exception:
                # Startup is speculative. The real validation still has the
                # normal renderer availability checks and cold fallback.
                return
        threading.Thread(target=start, daemon=True, name="editor-compile-prewarm").start()

    @contextmanager
    def _compile_origin(self, origin):
        prior = getattr(self._compile_state, "origin", "system")
        prior_cold = getattr(self._compile_state, "force_cold", False)
        self._compile_state.origin = origin
        try:
            yield
        finally:
            self._compile_state.origin = prior
            self._compile_state.force_cold = prior_cold

    def _epoch(self, origin: str) -> int:
        with self._cancel_guard:
            return self._epochs.get(origin, 0)

    @property
    def _compile_token(self):
        return getattr(self._compile_state, "token", None)

    @_compile_token.setter
    def _compile_token(self, token):
        self._compile_state.token = token

    @property
    def _current_compiled(self):
        value = getattr(self, "_compiled_cache", None)
        if value is not None and getattr(self, "_compiled_revisions", None) == self._revisions():
            return value
        if self._disk_compile_cache_enabled() and self._head_revision():
            from .compiled_cache import read_cache
            from .compile_worker import WorkerUnavailable
            try:
                plan = self._current_raw_plan()
                if self._offline_media(plan):
                    return None
                self._assert_view_renderer(self._renderer_revision())
                key = self._compiled_disk_key(plan)
                value = read_cache(self.history / "COMPILED.json", key)
                if value is not None:
                    self._compiled_cache = value
                    self._compiled_revisions = key["revisions"]
                    return value
            except (OSError, TypeError, ValueError, DocumentError, WorkerUnavailable):
                pass
        return None

    @_current_compiled.setter
    def _current_compiled(self, value):
        self._compiled_cache = value
        self._compiled_revisions = self._revisions() if value is not None else None
        if value is not None and self._disk_compile_cache_enabled() and self._head_revision():
            from .compiled_cache import cache_record
            from .compile_worker import WorkerUnavailable
            try:
                key = self._compiled_disk_key(self._current_raw_plan())
                _atomic_bytes(self.history / "COMPILED.json", _json_bytes(cache_record(key, value)))
            except (OSError, TypeError, ValueError, DocumentError, WorkerUnavailable):
                pass

    def _disk_compile_cache_enabled(self):
        return (self.compiler == self._run_worker_compiler
                and not callable(getattr(self.workspace, "compiler_for_renderer", None))
                and not self._revision_compilers)

    def _compiled_disk_key(self, plan):
        # Include the actual capabilities as well as the three public revisions.
        # A cached view is never eligible; only this independently keyed cache is.
        from .compile_worker import _boundary, boundary_key
        revision = self._renderer_revision()
        project = self._project_for_renderer(revision)
        scratch = self.project.root / ".cache/editor-compile/cache-identity"
        reads, profile, _ = _boundary(self.workspace, project, project.code_root, scratch, make_env=False)
        sources = hashlib.sha256()
        for name in ("transactions.py", "compile_worker.py", "compile_runtime.py", "compiled_cache.py",
                     "document.py", "overwrite.py", "timing.py", "tracks.py"):
            sources.update(Path(__file__).with_name(name).read_bytes())
        sources.update((Path(__file__).parent.parent / "timeline.py").read_bytes())
        return {
            "revisions": self._revisions(), "planHash": fingerprint(plan),
            "boundary": boundary_key(revision, reads, profile, scratch, data_root=project.root,
                                     media_root=project.media_dir if project.read_only_media else None,
                                     canonical=True),
            "originals": fingerprint(getattr(self.workspace, "_originals", {})),
            "runner": sources.hexdigest(),
        }

    def _project_for_renderer(self, revision, project=None):
        project = copy(project or self.project)
        if hasattr(self.workspace, "code_root"):
            source = self.root / "code/renderers" / str(revision) / "source"
            if (not source.is_dir() or any(path.is_symlink() for path in
                    (source, source.parent, source.parent.parent, source.parent.parent.parent))):
                raise DocumentError("renderer_unavailable", f"recorded renderer source {revision} is missing")
            project.code_root = source
        elif not (project.code_root / "scripts/reels/render.py").is_file():
            if project.read_only_media or project.code_root != project.root:
                raise DocumentError("renderer_unavailable", "recorded renderer source is incomplete")
            project.code_root = Path(__file__).resolve().parents[3]
        return project

    @contextmanager
    def _validation_unlocked(self):
        # The candidate is private. Let a manual gesture/commit win while the
        # worker validates; callers recheck revisions and leases before HEAD.
        state = self._lock._release_save() if self._lock._is_owned() else None
        self._compile_state.library_memo = None
        try:
            yield
        finally:
            if state is not None:
                self._lock._acquire_restore(state)
            self._compile_state.library_memo = None

    def _ensure_open(self) -> None:
        if self._closed or (self._compile_pool_lease is not None and self._compile_pool_lease.owner.closed):
            raise DocumentError("closed", "document store is closed")

    def cancel_pending(self, origin: str = "ai") -> dict[str, Any]:
        """Invalidate validations and terminate their renderer worker if running."""
        if origin == "all":
            origins = list(self._epochs)
        elif origin == "ai":
            origins = ["ai", "renderer"]
        elif origin in self._epochs:
            origins = [origin]
        else:
            raise DocumentError("invalid_origin", f"cannot cancel work for origin {origin!r}")
        cancelled = 0
        with self._cancel_guard:
            for name in origins:
                self._epochs[name] = self._epochs.get(name, 0) + 1
                for process in list(self._active_processes.get(name, [])):
                    from .jobs import JobContext
                    JobContext._terminate(process)
                    cancelled += 1
            return {"origin": origin, "cancelledProcesses": cancelled,
                    "epochs": {name: self._epochs[name] for name in origins}}

    def close(self) -> None:
        self.cancel_pending("all")
        self._closed = True
        if self._compile_pool_lease is not None:
            self._compile_pool_lease.close()

    # ---- revisions, paths and journal ---------------------------------

    def _library_revision(self) -> str:
        # Inside one apply() the library revision is asked for many times (~65 ms of
        # folder walking each). While the lock is held it is reused; releasing the lock
        # for validation drops it, so the post-validation check is always fresh.
        memo = getattr(self._compile_state, "library_memo", None)
        if memo is not None:
            return memo
        value = self._read_library_revision()
        if getattr(self._compile_state, "library_scope", False):
            self._compile_state.library_memo = value
        return value

    @contextmanager
    def _library_revision_scope(self):
        state = self._compile_state
        outer = getattr(state, "library_scope", False)
        state.library_scope = True
        try:
            yield
        finally:
            state.library_scope = outer
            if not outer:
                state.library_memo = None

    def _read_library_revision(self) -> str:
        value = getattr(self.workspace, "library_revision", None)
        if callable(value):
            try:
                value = value()
            except Exception as exc:  # noqa: BLE001
                raise DocumentError("library_unavailable", f"cannot verify private library revision: {exc}") from exc
        if value:
            return str(value)
        digest = hashlib.sha256()
        for directory in (self.project.assets_dir, self.project.transcripts_dir, self.project.music_dir):
            for path in sorted(directory.glob("*.json")):
                digest.update(str(path.relative_to(self.data_root)).encode())
                try:
                    digest.update(path.read_bytes())
                except OSError:
                    digest.update(b"offline")
        config = self.data_root / "config.json"
        if config.is_file():
            digest.update(config.read_bytes())
        return digest.hexdigest()[:24]

    def _renderer_revision(self) -> str:
        head = _read_object(self.head_path, {})
        committed = head.get("rendererRevision")
        if committed:
            return str(committed)
        return self._working_renderer_revision()

    def _working_renderer_revision(self) -> str:
        if self._renderer_revision_override is not None:
            return self._renderer_revision_override
        value = getattr(self.workspace, "renderer_revision", None)
        if callable(value):
            value = value()
        if value:
            return str(value)
        code_root = Path(getattr(self.workspace, "code_root", Path(__file__).resolve().parents[3]))
        digest = hashlib.sha256()
        for name in ("render.py", "mix.py", "music.py", "punch.py", "sphere.py", "stabilize.py", "exhibits.py"):
            path = code_root / "scripts" / "reels" / name
            try:
                digest.update(path.read_bytes())
            except OSError:
                digest.update(name.encode())
        return digest.hexdigest()[:24]

    def _revisions(self, document: str | None = None) -> dict[str, str]:
        return {
            "document": document or self._head_revision(),
            "library": self._library_revision(),
            "renderer": self._renderer_revision(),
        }

    def _head_revision(self) -> str:
        head = _read_object(self.head_path, {})
        return str(head.get("revision") or "")

    def _head_renderer_revision(self) -> str | None:
        head = _read_object(self.head_path, {})
        value = head.get("rendererRevision")
        if value:
            return str(value)
        manifest = self._manifest(head.get("revision")) if head.get("revision") else {}
        return str(manifest.get("rendererRevision")) if manifest.get("rendererRevision") else None

    def _manifest(self, revision: str | None = None) -> dict[str, Any]:
        revision = revision or self._head_revision()
        if not revision:
            return {}
        manifest = _read_object(self.manifests / f"{revision}.json")
        if not isinstance(manifest, dict):
            raise DocumentError("history_corrupt", f"missing commit manifest {revision}")
        return manifest

    def _object_value(self, digest: str) -> Any:
        path = self.objects / f"{digest}.json"
        if not path.is_file():
            raise DocumentError("history_corrupt", f"missing immutable object {digest}")
        value = read_json(path)
        if hashlib.sha256(_json_bytes(value)).hexdigest() != digest:
            raise DocumentError("history_corrupt", f"immutable object {digest} failed its hash check")
        return value

    def _safe_data_path(self, relative: str) -> Path:
        path = (self.data_root / relative).resolve()
        try:
            path.relative_to(self.data_root)
        except ValueError as exc:
            raise DocumentError("path_denied", f"journal path escapes private data root: {relative}") from exc
        return path

    def _delete_private(self, relative: str) -> None:
        """Remove a journalled file. Never anything under library/ or plans/."""
        path = self._safe_data_path(relative)
        top = Path(relative).parts[:1]
        if top in (("library",), ("plans",), ("out",), ("inbox",)):
            return  # guarantee: the editor never deletes library/plans/out/inbox files
        path.unlink(missing_ok=True)
        self._invalidate_library_for(relative)

    def _invalidate_library_for(self, relative):
        from .compile_runtime import INPUT_DIRS, INPUT_FILES
        if relative in INPUT_FILES or any(relative.startswith(root + "/") for root in INPUT_DIRS):
            invalidate = getattr(getattr(self, "workspace", None), "invalidate_library_revision", None)
            if callable(invalidate):
                invalidate()
            state = getattr(self, "_compile_state", None)
            if state is not None:
                state.library_memo = None

    def _manifest_files(self, manifest: dict[str, Any]) -> dict[str, Any]:
        return {rel: self._object_value(digest) for rel, digest in manifest.get("files", {}).items()}

    # ---- plan files on disk: real plan content only; metadata in a sidecar ----

    def _sidecar_path(self, rel: str) -> Path:
        path = Path(rel)
        name = path.stem if path.parent.name == "plans" and len(path.parts) == 2 else rel.replace("/", "__")
        return self.root / "documents" / f"{name}.json"

    def _read_sidecar(self, rel: str) -> dict[str, Any] | None:
        """Private node-ID metadata for a plan. Missing, corrupt or foreign sidecars are ignored."""
        try:
            value = read_json(self._sidecar_path(rel))
        except Exception:  # noqa: BLE001
            return None
        editor = value.get("editor") if isinstance(value, dict) else None
        if not isinstance(editor, dict) or value.get("plan") != rel:
            return None
        return editor

    def _write_materialized(self, rel: str, value: Any) -> None:
        path = self._safe_data_path(rel)
        if not (_is_plan_rel(rel) and isinstance(value, dict)):
            try:
                if _same_json(read_json(path), value):
                    return
            except (OSError, ValueError):
                pass
            _atomic_bytes(path, _json_bytes(value))
            self._invalidate_library_for(rel)
            return
        meta = value.get(EDITOR_KEY)
        if isinstance(meta, dict):
            record = _json_bytes({"version": SIDECAR_VERSION, "plan": rel, "editor": meta})
            sidecar = self._sidecar_path(rel)
            try:
                unchanged = sidecar.read_bytes() == record
            except OSError:
                unchanged = False
            if not unchanged:
                _atomic_bytes(sidecar, record)
        clean = _without_editor(rel, value)
        try:
            # Same content as disk (ignoring formatting and any legacy _editor block): leave the file alone.
            if _same_json(_without_editor(rel, read_json(path)), clean):
                return
        except Exception:  # noqa: BLE001
            pass
        _atomic_bytes(path, _json_bytes(clean, _existing_indent(path)))

    def _read_disk(self, rel: str) -> Any:
        return _without_editor(rel, read_json(self._safe_data_path(rel)))

    def _disk_matches(self, rel: str, digest: str) -> bool:
        try:
            current = read_json(self._safe_data_path(rel))
        except Exception:  # noqa: BLE001
            return False
        if _is_plan_rel(rel):
            return _same_json(_without_editor(rel, current), _without_editor(rel, self._object_value(digest)))
        return hashlib.sha256(_json_bytes(current)).hexdigest() == digest

    def _materialize(self, manifest: dict[str, Any]) -> None:
        for rel, digest in manifest.get("files", {}).items():
            if rel not in manifest.get("previousFiles", manifest.get("files", {})):
                continue
            self._write_materialized(rel, self._object_value(digest))
        for rel in manifest.get("deleted", []):
            self._delete_private(rel)
        _atomic_bytes(self.materialized_path, _json_bytes({"revision": manifest["revision"]}))

    def _recover(self) -> None:
        manifest = self._manifest()
        if not manifest:
            return
        materialized = _read_object(self.materialized_path, {})
        if materialized.get("revision") != manifest["revision"]:
            # HEAD is the commit point. Complete an interrupted multi-file publish.
            self._materialize(manifest)

    def _recover_pending_histories(self) -> None:
        """Finish committed shared-file writes before another plan can edit them."""
        histories = self.root / "history/documents"
        for history in sorted(histories.iterdir()) if histories.is_dir() else []:
            if not history.is_dir() or history.is_symlink():
                continue
            head = _read_object(history / "HEAD", {})
            revision = head.get("revision")
            if not revision or _read_object(history / "MATERIALIZED", {}).get("revision") == revision:
                continue
            if not isinstance(revision, str) or not re.fullmatch(r"[A-Za-z0-9-]{1,128}", revision):
                raise DocumentError("history_corrupt", "invalid pending document revision")
            manifest = _read_object(history / "commits" / f"{revision}.json")
            if not isinstance(manifest, dict) or manifest.get("revision") != revision:
                raise DocumentError("history_corrupt", "missing pending commit manifest")
            for rel, digest in manifest.get("files", {}).items():
                if rel not in manifest.get("previousFiles", manifest["files"]):
                    continue
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise DocumentError("history_corrupt", "invalid pending object identity")
                value = read_json(history / "objects" / f"{digest}.json")
                if hashlib.sha256(_json_bytes(value)).hexdigest() != digest:
                    raise DocumentError("history_corrupt", "pending object failed hash verification")
                self._write_materialized(rel, value)
            for rel in manifest.get("deleted", []):
                self._delete_private(rel)
            _atomic_bytes(history / "MATERIALIZED", _json_bytes({"revision": revision}))

    def _disk_files_match(self, manifest: dict[str, Any]) -> bool:
        if "config.json" not in manifest.get("files", {}) and (self.data_root / "config.json").is_file():
            return False
        for rel, digest in manifest.get("files", {}).items():
            path = self._safe_data_path(rel)
            if not self._disk_matches(rel, digest):
                return False
        if any(self._safe_data_path(rel).exists() for rel in manifest.get("deleted", [])):
            return False
        return True

    def _commit_files(
        self,
        files: dict[str, Any],
        *,
        origin: str,
        reason: str,
        affected_ids: list[str],
        parent: str | None,
        undo_target: str | None,
        redo_target: str | None = None,
        redo_stack: list[str] | None = None,
        restored_from: str | None = None,
        renderer_revision: str | None = None,
        preview: dict[str, Any] | None = None,
        source_ref: str | None = None,
        rebind_preview: bool = False,
        clear_preview: bool = False,
        expected_revisions: dict[str, Any] | None = None,
        cancel_token: tuple[str, int] | None = None,
    ) -> str:
        # Committing may write library inputs (transcripts, config): no reuse from here on.
        state = self._compile_state
        state.library_memo, state.library_scope = None, False
        revision = str(uuid.uuid4())
        renderer_revision = renderer_revision or self._head_renderer_revision() or self._working_renderer_revision()
        old_manifest = self._manifest() if self._head_revision() else {}
        if clear_preview:
            preview_record = None
        elif preview is None:
            preview_record = deepcopy(old_manifest.get("preview"))
        else:
            preview_record = deepcopy(preview)
        if preview_record is not None and rebind_preview:
            preview_record = {**preview_record, "documentRevision": revision, "rendererRevision": renderer_revision}
            plan_value = files.get(self.plan_rel)
            if isinstance(plan_value, dict):
                plan_value.setdefault("_editor", {})["rendererPreview"] = deepcopy(preview_record)
        def write_object(value: Any) -> str:
            digest = hashlib.sha256(_json_bytes(value)).hexdigest()
            object_path = self.objects / f"{digest}.json"
            if not object_path.exists():
                _atomic_bytes(object_path, _json_bytes(value))
            return digest

        refs: dict[str, str] = {rel: write_object(value) for rel, value in files.items()}
        previous_refs: dict[str, str | None] = {}
        prior_refs = old_manifest.get("files", {})
        for rel in set(prior_refs) | set(refs):
            if prior_refs.get(rel) == refs.get(rel):
                continue
            if rel in prior_refs:
                previous_refs[rel] = prior_refs[rel]
                continue
            try:
                prior_path = self._safe_data_path(rel)
                prior_value = read_json(prior_path) if prior_path.is_file() else None
            except Exception:  # noqa: BLE001
                prior_value = None
            previous_refs[rel] = write_object(prior_value) if prior_value is not None else None
        deleted = sorted(set(old_manifest.get("files", {})) - set(refs))
        manifest = {
            "revision": revision, "parent": parent, "created": time.time(),
            "origin": origin, "reason": reason, "affectedIds": sorted(set(affected_ids)),
            "files": refs, "undoTarget": undo_target, "redoTarget": redo_target,
            "redoStack": list(redo_stack or []),
            "deleted": deleted, "previousFiles": previous_refs,
            "restoredFrom": restored_from, "rendererRevision": renderer_revision,
            "preview": deepcopy(preview_record),
            "sourceRef": source_ref or (
                old_manifest.get("sourceRef") if old_manifest.get("rendererRevision") == renderer_revision
                else f"code/renderers/{renderer_revision}/source"
            ),
        }
        _atomic_bytes(self.manifests / f"{revision}.json", _json_bytes(manifest))
        guard = self._cancel_guard if cancel_token else nullcontext()
        with guard:
            if self._closed:
                raise DocumentError("cancelled", "document store closed before the commit point")
            if cancel_token and self._epochs.get(cancel_token[0], 0) != cancel_token[1]:
                raise DocumentError("cancelled", f"pending {cancel_token[0]} edit was cancelled before commit",
                                    {"origin": cancel_token[0]})
            if expected_revisions is not None:
                self._check_expected(expected_revisions)
            if parent != (self._head_revision() or None):
                raise DocumentError("conflict", "document changed before the commit point",
                                    {"current": self._revisions()})
            # The only commit point. Recovery always materializes this entire set.
            _atomic_bytes(self.head_path, _json_bytes({"revision": revision, "rendererRevision": renderer_revision,
                                                       "preview": deepcopy(preview_record)}))
        self._materialize(manifest)
        self._current_compiled = None
        self._redo_target = redo_target
        return revision

    def _load_files(self) -> dict[str, Any]:
        return self._manifest_files(self._manifest())

    def _open_or_initialize(self) -> None:
        if self.head_path.exists():
            self._recover()
            manifest = self._manifest()
            if not self._disk_files_match(manifest):
                # This is an external edit only if the last committed materialization
                # was already complete; otherwise _recover has just repaired it.
                marker = _read_object(self.materialized_path, {})
                if marker.get("revision") == manifest.get("revision") and not self._offline_media(self._current_raw_plan()):
                    self.reload_external()
            return
        if not self.plan_path.is_file():
            raise DocumentError("not_found", f"plan not found: {self.plan_path}")
        plan = read_json(self.plan_path)
        if not isinstance(plan, dict):
            raise DocumentError("invalid_plan", "plan root must be a JSON object")
        # Node identity comes from the private sidecar, else a legacy embedded block, else is regenerated.
        meta = self._read_sidecar(self.plan_rel)
        if meta is None and isinstance(plan.get(EDITOR_KEY), dict):
            meta = plan[EDITOR_KEY]
        plan.pop(EDITOR_KEY, None)
        if meta is not None:
            plan[EDITOR_KEY] = {key: deepcopy(child) for key, child in meta.items() if key != "rendererPreview"}
        ensure_node_ids(plan)
        initial_renderer = self._working_renderer_revision()
        if hasattr(self.workspace, "code_root") and self.compiler == self._run_worker_compiler:
            from .renderer import RendererManager
            RendererManager(self.workspace).resolve_renderer(initial_renderer)
        plan.setdefault("_editor", {})["rendererRevision"] = initial_renderer
        compiled = self._compile(plan, {})
        self._current_compiled = compiled
        files = {self.plan_rel: plan}
        config_path = self.project.root / "config.json"
        if config_path.is_file():
            config_rel = config_path.resolve().relative_to(self.data_root).as_posix()
            files[config_rel] = read_json(config_path)
        self._commit_files(files, origin="system", reason="initialize document", affected_ids=[],
                           parent=None, undo_target=None, renderer_revision=initial_renderer)
        self._current_compiled = compiled

    # ---- raw plan compilation and snapshots ----------------------------

    def _compile(
        self,
        plan: dict[str, Any],
        transcript_overrides: dict[str, Any],
        renderer_revision: str | None = None,
        compiler: Callable[..., dict[str, Any]] | None = None,
        removed_files: list[str] | None = None,
    ) -> dict[str, Any]:
        try:
            renderer_revision = renderer_revision or self._renderer_revision()
            selected = compiler or self._revision_compilers.get(renderer_revision)
            if selected is None:
                provider = getattr(self.workspace, "compiler_for_renderer", None)
                if callable(provider):
                    selected = provider(renderer_revision)
                else:
                    available = getattr(self.workspace, "renderer_revision_available", None)
                    if callable(available) and not available(renderer_revision):
                        raise DocumentError("renderer_unavailable",
                                            f"renderer source revision {renderer_revision} is unavailable",
                                            {"rendererRevision": renderer_revision})
                    persisted = self.root / "code/renderers" / renderer_revision / "source"
                    working = self._working_renderer_revision()
                    if renderer_revision != working and not persisted.is_dir():
                        raise DocumentError("renderer_unavailable",
                                            f"renderer source revision {renderer_revision} is unavailable",
                                            {"rendererRevision": renderer_revision})
                    selected = self.compiler
            if selected is None:
                raise DocumentError("renderer_unavailable", f"no compiler is available for {renderer_revision}")
            offline = self._offline_media(plan)
            if offline:
                self._assert_view_renderer(renderer_revision)
                raise DocumentError("media_offline", "referenced originals are offline or changed; document is view-only",
                                    {"media": offline})
            # The compiler receives a deep copy and is expected to be a worker
            # interface. No normalized renderer data is ever written to a plan.
            params = inspect.signature(selected).parameters
            overrides = deepcopy(transcript_overrides)
            if removed_files:
                overrides["__editor_deleted_files__"] = list(removed_files)
            compile_project = copy(self.project)
            config_path = self.data_root / "config.json"
            config = overrides.get("config.json", _read_object(config_path, {}))
            if removed_files and "config.json" in removed_files:
                config = {}
            compile_project.config = deep_merge(deepcopy(DEFAULTS), deepcopy(config))
            with self._validation_unlocked():
                if len(params) >= 4:
                    result = selected(compile_project, deepcopy(plan), overrides, renderer_revision)
                elif len(params) == 3:
                    result = selected(compile_project, deepcopy(plan), overrides)
                elif len(params) == 2:
                    result = selected(compile_project, deepcopy(plan))
                else:
                    result = selected(deepcopy(plan))
            if not isinstance(result, dict) or not {"fps", "total", "segs"}.issubset(result):
                raise DocumentError("compiler_contract", "compiler must return fps, total and segs")
            return normalize_compiled(result)
        except DocumentError:
            raise
        except ReelsError as exc:
            raise DocumentError("validation_failed", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise DocumentError("validation_failed", str(exc)) from exc

    def _current_raw_plan(self) -> dict[str, Any]:
        files = self._load_files()
        plan = files.get(self.plan_rel)
        if not isinstance(plan, dict):
            raise DocumentError("history_corrupt", "committed plan is missing")
        return plan

    def _offline_media(self, plan: dict[str, Any]) -> dict[str, Any]:
        """Only referenced originals gate this document, not unrelated assets."""
        status = getattr(self.workspace, "media_status", None)
        if not callable(status):
            return {}
        from .workspace import _plan_media_refs
        from ..library import resolve_id
        all_status = status()
        offline = {}
        for reference in _plan_media_refs(plan):
            try:
                asset_id = resolve_id(self.project, reference)
                asset = read_json(self.project.assets_dir / f"{asset_id}.json")
            except ReelsError:
                continue  # ordinary validation still handles invalid references
            media = asset.get("media")
            state = all_status.get(media)
            if state and state.get("status") != "online":
                offline[media] = state
        return offline

    def _assert_view_renderer(self, revision: str) -> None:
        """Offline mode never makes a missing selected renderer acceptable."""
        available = getattr(self.workspace, "renderer_revision_available", None)
        if callable(available) and not available(revision):
            raise DocumentError("renderer_unavailable", f"renderer source revision {revision} is unavailable")
        persisted = self.root / "code/renderers" / revision / "source"
        requires_snapshot = hasattr(self.workspace, "code_root") and self.compiler == self._run_worker_compiler
        if (requires_snapshot or revision != self._working_renderer_revision()) and not persisted.is_dir():
            # Explicit test/integration compiler providers still own availability.
            if not callable(getattr(self.workspace, "compiler_for_renderer", None)):
                raise DocumentError("renderer_unavailable", f"renderer source revision {revision} is unavailable")
        if persisted.is_symlink():
            raise DocumentError("renderer_unavailable", "renderer source revision is unsafe")

    def _offline_snapshot(self, plan: dict, offline: dict) -> dict:
        revisions = self._revisions()
        self._assert_view_renderer(revisions["renderer"])
        cache_path = self.history / "VIEW.json"
        cache = {}
        if cache_path.is_file() and not cache_path.is_symlink() and cache_path.stat().st_nlink == 1:
            try:
                cache = _read_object(cache_path, {})
            except (ValueError, OSError):
                pass
        cached_revisions = cache.get("revisions") or {}
        compiled = cache.get("compiled")
        usable = (cache.get("planHash") == fingerprint(plan)
                  and all(cached_revisions.get(key) == revisions[key] for key in ("document", "renderer"))
                  and isinstance(compiled, dict) and {"fps", "total", "segs"}.issubset(compiled))
        if not usable:
            compiled = {"fps": int(self.project.cfg("render", "fps")), "total": 0, "segs": [],
                        "props": {}, "sound": {}, "warnings": []}
        result = self._snapshot_result(plan, compiled, revisions, cache_view=False)
        result.update(viewOnly=True, offline=True, offlineMedia=offline,
                      timingState="cached_offline" if usable else "unavailable",
                      compiledRevisions=cached_revisions if usable else None,
                      pendingExternalEdits=not self._disk_files_match(self._manifest()))
        result["warnings"].append("Referenced originals are offline or changed; viewing the last committed document only.")
        for node in result["nodes"]:
            node["editable"] = False
            node["readOnly"] = True
            if usable:
                node["cached"] = True
                if node["timingState"] == "resolved":
                    node["timingState"] = "cached_offline"
        return result

    def _snapshot_result(
        self,
        plan: dict[str, Any],
        compiled: dict[str, Any],
        revisions: dict[str, str],
        manifest: dict[str, Any] | None = None,
        *,
        cache_view: bool = True,
    ) -> dict[str, Any]:
        manifest = manifest or self._manifest()
        result = editing_snapshot(plan, compiled, revisions)
        raw_config = self._load_files().get("config.json", {})
        config = deep_merge(deepcopy(DEFAULTS), deepcopy(raw_config))
        result["config"] = config
        result["projectConfig"] = deepcopy(config)
        result["change"] = {
            "origin": manifest.get("origin"),
            "reason": manifest.get("reason"),
            "affectedIds": list(manifest.get("affectedIds") or []),
        }
        if (cache_view and revisions["document"] == self._head_revision()
                and fingerprint(plan) == fingerprint(self._current_raw_plan())):
            # A recoverable view cache, not history authority or export input.
            # It is never assigned to _current_compiled when originals are gone.
            try:
                _atomic_bytes(self.history / "VIEW.json", _json_bytes({
                    "revisions": revisions, "planHash": fingerprint(plan), "compiled": compiled,
                }))
            except (OSError, TypeError, ValueError):
                pass
        return result

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            self._recover_pending_histories()
            plan = self._current_raw_plan()
            offline = self._offline_media(plan)
            if offline:
                return self._offline_snapshot(plan, offline)
            if not self._disk_files_match(self._manifest()):
                return self.reload_external()
            expected = self._revisions()
            plan = self._current_raw_plan()
            ensure_node_ids(plan)
            if self._current_compiled is None:
                transcript_overrides = {
                    rel: value for rel, value in self._load_files().items() if rel != self.plan_rel
                }
                self._compile_token = ("system", self._epoch("system"))
                compile_epoch = self._compile_token[1]
                try:
                    compiled = self._compile(plan, transcript_overrides)
                finally:
                    self._compile_token = None
                if self._closed or self._epoch("system") != compile_epoch:
                    raise DocumentError("cancelled", "snapshot preparation was cancelled")
                self._check_expected(expected)
                self._current_compiled = compiled
            return self._snapshot_result(plan, self._current_compiled, self._revisions())

    @contextmanager
    def revision_guard(self, expected: dict[str, Any]):
        """Hold the document commit lock while publishing revision-bound state."""
        with self._lock:
            self._ensure_open()
            self._check_expected(expected)
            current = self.snapshot()
            if current.get("viewOnly"):
                raise DocumentError("media_offline", "offline snapshots cannot prepare new output")
            yield current

    def publish_if_current(self, expected: dict[str, Any], callback: Callable[..., Any]) -> Any:
        """Run a short preview-publication callback iff all input revisions remain current.

        The callback executes under the same lock used by ``apply`` and
        ``activate_renderer``; a manual commit cannot race between the check
        and publishing the displayed preview.
        """
        with self._lock:
            self._ensure_open()
            self._check_expected(expected)
            current = self.snapshot()
            if current.get("viewOnly"):
                raise DocumentError("media_offline", "offline snapshots cannot publish new output")
            try:
                parameters = inspect.signature(callback).parameters
            except (TypeError, ValueError):
                parameters = {"snapshot": None}
            return callback() if not parameters else callback(current)

    # ---- edit leases / expected revisions ------------------------------

    def _check_expected(self, expected: dict[str, Any] | None, *, strict: bool = True) -> None:
        self._recover_pending_histories()
        current = self._revisions()
        if expected is None:
            if strict:
                raise DocumentError("revision_required", "expected document, library and renderer revisions are required",
                                    {"current": current})
            return
        missing = [k for k in ("document", "library", "renderer") if k not in expected]
        if missing:
            raise DocumentError("revision_required", "all expected revisions are required",
                                {"missing": missing, "current": current})
        stale = {k: {"expected": str(expected[k]), "current": current[k]}
                 for k in current if str(expected[k]) != current[k]}
        if stale:
            raise DocumentError("conflict", "the document changed while this edit was being prepared",
                                 {"stale": stale, "current": current})
        if self._head_revision() and not self._disk_files_match(self._manifest()):
            raise DocumentError("external_changes", "private files changed; reload before editing",
                                {"current": current})

    def acquire_lease(self, owner: str, ttl: float = 6) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            if self._lease and self._lease["expires"] <= now:
                self._lease = None
            # The native timeline only ever has one gesture in flight; a lease left
            # behind by an earlier timeline gesture (e.g. a click that never became a
            # drag) is stale, so a new timeline gesture replaces it instead of failing.
            if (self._lease and str(owner).startswith("timeline-")
                    and str(self._lease["owner"]).startswith("timeline-")):
                self._lease = None
            if self._lease:
                raise DocumentError("lease_held", "another edit gesture holds the document lease",
                                    {"owner": self._lease["owner"]})
            token = str(uuid.uuid4())
            self._lease = {"token": token, "owner": str(owner), "expires": now + max(0.25, min(float(ttl), 120))}
            return {"token": token, "owner": str(owner), "expiresIn": max(0.25, min(float(ttl), 120))}

    def release_lease(self, token: str) -> bool:
        with self._lock:
            if self._lease and self._lease["token"] == token:
                self._lease = None
                return True
            return False

    def _check_lease(self, batch: dict[str, Any]) -> None:
        now = time.monotonic()
        if self._lease and self._lease["expires"] <= now:
            self._lease = None
        # A lease protects the user's gesture from AI/external edits. The user's own
        # edits (timeline, inspector) never conflict with it: back-to-back drags used to
        # fail here when a new drag's lease replaced the one the previous edit carried.
        # Their order is still guarded by the expected-revision check.
        if self._lease and batch.get("origin", "ai") != "manual":
            raise DocumentError("lease_conflict", "an active manual edit lease takes precedence",
                                {"owner": self._lease["owner"]})

    # ---- operation application -----------------------------------------

    def candidate_snapshot(self, batch_or_operations=None, expected=None, rendererRevision=None,
                           *, operations=None, batch=None, renderer_revision=None) -> dict[str, Any]:
        """Prepare an isolated batch before renderer validation; never write HEAD."""
        if batch is not None:
            batch_or_operations = batch
        elif operations is not None:
            batch_or_operations = operations
        rendererRevision = rendererRevision or renderer_revision
        batch = deepcopy(batch_or_operations) if isinstance(batch_or_operations, dict) else {
            "operations": deepcopy(batch_or_operations), "expected": expected, "origin": "ai",
        }
        expected = batch.get("expected", expected)
        operations = batch.get("operations") or []
        with self._lock, self._compile_origin("ai"):
            self._ensure_open()
            self._check_expected(expected)
            self._check_lease(batch)
            epoch = self._epoch("ai")
            files = self._load_files()
            plan = deepcopy(files[self.plan_rel])
            if self._offline_media(plan):
                raise DocumentError("media_offline", "offline document is view-only")
            aux = {rel: deepcopy(value) for rel, value in files.items() if rel != self.plan_rel}
            compiled = self._current_compiled or self._compile(plan, aux)
            ctx = {"plan": plan, "files": files, "aux": aux, "compiled": compiled,
                   "fps": int(compiled["fps"]), "affected": set(), "origin": batch.get("origin", "ai")}
            for index, operation in enumerate(operations):
                try:
                    self._apply_edit_operation(ctx, operation)
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise DocumentError("invalid_operation", str(exc), {"operation": index}) from exc
            ensure_node_ids(plan)
            # Candidate effects may be invalid under the old renderer. Their
            # final compile belongs to the new sandboxed renderer validator.
            result = self._snapshot_result(plan, ctx["compiled"], self._revisions())
            result["fileOverrides"] = deepcopy(aux)
            result["overrides"] = deepcopy(aux)
            result["candidate"] = True
            result["candidateHash"] = fingerprint({"plan": plan, "files": aux})
            result["projectConfig"] = deep_merge(deepcopy(DEFAULTS), deepcopy(aux.get("config.json", {})))
            result["config"] = deepcopy(result["projectConfig"])
            if rendererRevision is not None:
                result["revisions"]["renderer"] = str(rendererRevision)
            self._check_expected(expected)
            if self._epoch("ai") != epoch:
                raise DocumentError("cancelled", "candidate preparation was cancelled")
            key = fingerprint({"expected": expected, "operations": operations})
            self._prepared_candidates[key] = {"plan": deepcopy(plan), "aux": deepcopy(aux),
                                               "affected": set(ctx["affected"]), "aiEpoch": epoch}
            while len(self._prepared_candidates) > 16:
                self._prepared_candidates.pop(next(iter(self._prepared_candidates)))
            return result

    def commit_export_usage(self, updates: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
        """Append an export receipt's usage deltas to latest private metadata."""
        if not isinstance(expected, dict) or any(k not in expected for k in ("document", "library", "renderer")):
            raise DocumentError("revision_required", "export receipt requires all input revisions")
        if not isinstance(updates, dict) or set(updates) - {"assets", "music"}:
            raise DocumentError("invalid_usage", "usage updates require assets and music maps")
        with self._lock:
            self._ensure_open()
            self._recover()
            if not self._disk_files_match(self._manifest()):
                self.reload_external()
            files = self._load_files()
            affected = []
            for kind, directory in (("assets", self.project.assets_dir), ("music", self.project.music_dir)):
                records = updates.get(kind, {})
                if not isinstance(records, dict):
                    raise DocumentError("invalid_usage", f"{kind} updates must be a map")
                for identifier, additions in records.items():
                    if not isinstance(identifier, str) or not identifier or Path(identifier).name != identifier or identifier in (".", ".."):
                        raise DocumentError("invalid_usage", "usage identifier must be a stable asset ID or music slug")
                    if not isinstance(additions, list):
                        raise DocumentError("invalid_usage", "usage delta must be an array")
                    path = self._safe_data_path((directory / f"{identifier}.json").relative_to(self.data_root).as_posix())
                    if not path.is_file():
                        raise DocumentError("invalid_usage", f"usage metadata is missing for {identifier}")
                    value = read_json(path)
                    used = value.setdefault("used", [])
                    if not isinstance(used, list):
                        raise DocumentError("invalid_usage", f"used field is not an array for {identifier}")
                    changed = False
                    for entry in additions:
                        if entry not in used:
                            used.append(deepcopy(entry))
                            changed = True
                    if changed:
                        files[path.relative_to(self.data_root).as_posix()] = value
                        affected.append(identifier)
            if not affected:
                return self.snapshot()
            current = self._revisions()
            self._commit_files(files, origin="system", reason="record completed export usage",
                               affected_ids=affected, parent=current["document"],
                               undo_target=self._manifest().get("undoTarget"),
                               restored_from=self._manifest().get("restoredFrom") or current["document"],
                               expected_revisions=current)
            return self.snapshot()

    def apply(self, batch: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(batch, dict):
            raise DocumentError("invalid_batch", "batch must be an object")
        expected = batch.get("expected")
        with self._lock, self._library_revision_scope(), self._compile_origin(batch.get("origin", "ai")):
            self._ensure_open()
            self._check_expected(expected)
            self._check_lease(batch)
            origin = batch.get("origin", "ai")
            if origin not in ("manual", "ai", "external", "system"):
                raise DocumentError("invalid_origin", "origin must be manual, ai, external or system")
            epoch = self._epoch(origin)
            self._compile_token = (origin, epoch)
            base_revision = self._head_revision()
            files = self._load_files()
            plan = deepcopy(files[self.plan_rel])
            aux = {k: deepcopy(v) for k, v in files.items() if k != self.plan_rel}
            prior_nodes = ensure_node_ids(plan)
            current_renderer = self._renderer_revision()
            if self._offline_media(plan):
                self._compile_token = None
                raise DocumentError("media_offline", "offline document is view-only")
            try:
                compiled_before = self._current_compiled or self._compile(plan, aux, current_renderer)
            except DocumentError:
                if self._epoch(origin) != epoch:
                    self._compile_token = None
                    raise DocumentError("cancelled", f"pending {origin} edit was cancelled during validation")
                self._compile_token = None
                raise
            if self._epoch(origin) != epoch:
                self._compile_token = None
                raise DocumentError("cancelled", f"pending {origin} edit was cancelled during validation")
            self._compile_token = None
            context = {"plan": plan, "files": files, "aux": aux, "compiled": compiled_before,
                       "fps": int(compiled_before["fps"]), "affected": set(), "origin": origin,
                       "cancelToken": (origin, epoch)}
            operations = batch.get("operations")
            if not isinstance(operations, list) or not operations:
                raise DocumentError("invalid_batch", "operations must be a non-empty array")
            for index, operation in enumerate(operations):
                if not isinstance(operation, dict):
                    raise DocumentError("invalid_operation", f"operation {index} must be an object")
            prepared = None
            if (origin == "manual" and self._disk_compile_cache_enabled()
                    and (compiled_before.get("timelineCapabilities") or {}).get("version") == 1
                    and any(isinstance(op, dict) and (
                        op.get("op") in ("reorder_segments", "trim_segment", "split_segment",
                                         "insert_segment", "remove_segments", "move_segments", "set_node_track",
                                         "duplicate_segments", "ripple_remove", "paste_segments", "set_enabled")
                        or (op.get("op") == "set_field" and op.get("path") in (
                            "/speed", "/duration", "/in", "/out", "/transition", "/beatSnap",
                            "/music", "/render/fps",
                        ))) for op in operations)):
                prepared = self._prepare_warm_batch(plan, aux, operations, compiled_before,
                                                    current_renderer, expected, epoch)
                if prepared is not None:
                    context["timingResults"] = iter(prepared["timingResults"])
            for n, operation in enumerate(operations):
                if self._closed or self._epoch(origin) != epoch:
                    raise DocumentError("cancelled", f"pending {origin} edit was cancelled")
                try:
                    self._apply_edit_operation(context, operation)
                except DocumentError as exc:
                    exc.details.setdefault("operation", n)
                    raise
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise DocumentError("invalid_operation", str(exc), {"operation": n}) from exc
            ensure_node_ids(plan)
            # Compile and validate candidate entirely before touching HEAD.
            self._compile_token = (origin, epoch)
            try:
                if prepared is None:
                    candidate_compiled = self._compile(plan, aux, current_renderer)
                else:
                    from .compile_runtime import content_hash
                    if content_hash(plan, aux) != prepared.get("candidateHash"):
                        raise DocumentError("worker_protocol", "timing replay did not match validated candidate")
                    if next(context["timingResults"], None) is not None:
                        raise DocumentError("worker_protocol", "unused compiler timing evidence")
                    candidate_compiled = normalize_compiled(prepared["result"])
            except DocumentError:
                if self._epoch(origin) != epoch:
                    raise DocumentError("cancelled", f"pending {origin} edit was cancelled during validation",
                                        {"origin": origin})
                raise
            finally:
                self._compile_token = None
            if self._epoch(origin) != epoch:
                raise DocumentError("cancelled", f"pending {origin} edit was cancelled during validation",
                                    {"origin": origin})
            # Validation may have yielded while a manual gesture, library
            # refresh, or renderer activation changed the base revisions.
            self._check_expected(expected)
            if self._head_revision() != base_revision:
                raise DocumentError("conflict", "document changed during validation",
                                    {"current": self._revisions()})
            self._check_lease(batch)
            self._check_compiled_locks(files[self.plan_rel], compiled_before, plan, candidate_compiled, operations)
            files[self.plan_rel] = plan
            files.update(aux)
            revision = self._commit_files(
                files, origin=origin, reason=str(batch.get("reason") or "edit"),
                affected_ids=list(context["affected"]), parent=base_revision,
                undo_target=base_revision, renderer_revision=current_renderer,
                expected_revisions=expected,
                cancel_token=(origin, epoch),
            )
            self._current_compiled = candidate_compiled
            return self._snapshot_result(plan, candidate_compiled, self._revisions(revision))

    def _check_compiled_locks(self, before_plan, before_compiled, after_plan, after_compiled, operations):
        """Protect implicit linked audio and renderer-generated owned items too."""
        from .tracks import track_list
        old = {row["id"] for row in track_list(before_plan.get("_editor"), plan=before_plan) if row["locked"]}
        new = {row["id"] for row in track_list(after_plan.get("_editor"), plan=after_plan) if row["locked"]}
        unlocked = {op.get("id") for op in operations
                    if op.get("op") == "set_track_locked" and op.get("locked") is False}
        protected = old & new - unlocked
        if not protected:
            return
        before = editing_snapshot(before_plan, before_compiled, self._revisions())["nodes"]
        after = editing_snapshot(after_plan, after_compiled, self._revisions())["nodes"]
        def state(node):
            return {key: node.get(key) for key in ("kind", "parentId", "track", "startFrame", "endFrame", "raw")}
        left = {node["id"]: state(node) for node in before if node["track"] in protected}
        right = {node["id"]: state(node) for node in after if node["track"] in protected}
        if left != right:
            raise DocumentError("track_locked", "The edit changes a locked track or one of its linked/owned items",
                                {"tracks": sorted(protected)})

    def _prepare_warm_batch(self, plan, aux, operations, compiled_before, renderer, expected, epoch):
        from .compile_worker import WorkerUnavailable, WorkerRejected, WorkerCancelled
        try:
            project = self._project_for_renderer(renderer)
            with self._validation_unlocked():
                response = self._warm_compiler().compile(
                    project, deepcopy(plan), deepcopy(aux), renderer, expected["library"],
                    cancelled=lambda: self._closed or self._epoch("manual") != epoch,
                    batch={"operations": deepcopy(operations), "compiledBefore": deepcopy(compiled_before),
                           "revisions": deepcopy(expected)},
                )
            if not isinstance(response.get("timingResults"), list):
                raise DocumentError("worker_protocol", "compiler omitted timing evidence")
            return response
        except WorkerUnavailable:
            # Reapply in the broker using the existing isolated compiler path.
            # No remote candidate was committed, even if the worker died late.
            if self._closed or self._epoch("manual") != epoch:
                raise DocumentError("cancelled", "timing validation was cancelled before cold fallback")
            self._compile_state.force_cold = True
            invalidate = getattr(self.workspace, "invalidate_library_revision", None)
            if callable(invalidate):
                invalidate()
            return None
        except WorkerRejected as exc:
            if self._closed or self._epoch("manual") != epoch:
                raise DocumentError("cancelled", "timing validation was cancelled during rejection") from exc
            raise DocumentError(exc.code, str(exc), exc.details) from exc
        except WorkerCancelled as exc:
            raise DocumentError("cancelled", str(exc)) from exc

    def _apply_edit_operation(self, ctx: dict[str, Any], operation: dict[str, Any]) -> None:
        """Preserve IDs by object identity and ripple against renderer positions."""
        plan = ctx["plan"]
        from .tracks import TRACK_OPS, apply_track_operation, locked_target
        ensure_node_ids(plan)
        from ..timeline import positioned, requantize
        from .overwrite import apply_overwrite, positioned_source_edit, video_source_bounds
        op = operation.get("op")
        if (ctx.get("timingOnly") and op in ("move_overlay", "resize_overlay", "remove_overlay")
                and operation.get("id") not in plan["_editor"]["nodes"]
                and operation.get("id") not in ctx.get("generatedAliases", {})):
            # Generated IDs require overlays, after the preceding operation remapped global events.
            ctx["compiled"] = self._compile(plan, ctx["aux"])
            ctx["timingOnly"] = False
        capable = (ctx["compiled"].get("timelineCapabilities") or {}).get("version") == 1
        if positioned(plan):
            requantize(plan, ctx["fps"])
        transformed = operation
        entry = plan["_editor"]["nodes"].get(str(operation.get("id")), {})
        source_kind = entry.get("kind")
        if ((capable and op == "set_node_track" and entry.get("kind") in ("segment", "cutaway"))
                or (positioned(plan) and op in ("insert_segment", "reorder_segments", "paste_segments"))):
            edit = str(uuid.uuid5(uuid.NAMESPACE_URL, canonical_json({
                "operation": operation, "document": self._revisions()["document"]})))
            if op == "set_node_track":
                node = next(item for item in editing_snapshot(plan, ctx["compiled"], self._revisions())["nodes"]
                            if item["id"] == operation["id"])
                placements = [{"id": operation["id"], "track": operation["track"], "atFrame": node["startFrame"]}]
            elif op == "paste_segments":
                from .segment_ops import paste_placements
                placements = paste_placements(plan, operation, ctx["fps"], self.project)
            elif op == "insert_segment":
                value = deepcopy(operation["value"])
                value.pop("timeline", None)
                before = operation.get("beforeId")
                at = (node_location(plan, before)[1]["timeline"]["startFrame"] if before
                      else plan["timeline"]["durationFrames"])
                placements = [{"insert": value, "track": "V1", "atFrame": at}]
            else:
                ids = operation.get("ids") or []
                clips = [node_location(plan, key)[1] for key in ids]
                start = min(value["timeline"]["startFrame"] for value in clips)
                before = operation.get("beforeId")
                at = (node_location(plan, before)[1]["timeline"]["startFrame"] if before
                      else plan["timeline"]["durationFrames"])
                placements = [{"id": key, "track": value["timeline"]["track"],
                               "atFrame": at + value["timeline"]["startFrame"] - start}
                              for key, value in zip(ids, clips)]
            transformed = {"op": "move_segments", "editId": edit, "mode": "overwrite", "placements": placements}
        if transformed.get("op") == "move_segments":
            if not capable:
                raise DocumentError("unsupported_operation", "selected renderer does not support placed footage")
            if positioned(plan):
                requantize(plan, ctx["fps"])
            try:
                apply_overwrite(ctx, transformed, self.project)
            except ValueError as exc:
                if "locked" in str(exc).lower():
                    raise DocumentError("track_locked", str(exc)) from exc
                raise
            self._refresh_edit_timing(ctx)
            return
        if positioned(plan) and op in ("trim_segment", "split_segment"):
            try:
                handled = positioned_source_edit(ctx, operation, self.project, self._revisions())
            except ValueError as exc:
                if "locked" in str(exc).lower():
                    raise DocumentError("track_locked", str(exc)) from exc
                raise
            if handled:
                self._refresh_edit_timing(ctx)
                return
        if operation.get("op") in TRACK_OPS:
            ctx["affected"].update(apply_track_operation(plan, operation))
            return
        locked = locked_target(plan, operation)
        if locked:
            raise DocumentError("track_locked", locked)
        if operation.get("op") in ("remove_segments", "ripple_remove"):
            for source in operation.get("ids") or []:
                entry = plan["_editor"]["nodes"].get(source)
                if entry:
                    for child, record in plan["_editor"]["nodes"].items():
                        if record["locator"].startswith(entry["locator"] + "/"):
                            message = locked_target(plan, {"id": child})
                            if message:
                                raise DocumentError("track_locked", message)
        metadata = deepcopy(ensure_node_ids(plan))
        objects = {node_id: pointer_get(plan, entry["locator"]) for node_id, entry in metadata.items()}
        old_snapshot = editing_snapshot(plan, ctx["compiled"], self._revisions())
        self._apply_operation(ctx, operation)
        if positioned(plan) and op == "set_field" and operation.get("path") in (
                "/speed", "/duration", "/in", "/out", "/transition"):
            _, clip = node_location(plan, str(operation.get("id")))
            if isinstance(clip, dict) and isinstance(clip.get("timeline"), dict):
                if "in" in clip and "out" in clip:
                    from ..library import load_asset, resolve_id
                    asset = load_asset(self.project, resolve_id(self.project, clip["asset"]))
                    video_source_bounds(asset, clip["in"], clip["out"], clip.get("speed", 1))
                frames = max(1, round(source_duration(clip) * plan["timeline"]["fps"]))
                clip["timeline"].update(durationFrames=frames, transitionInFrames=0)
                if clip.get("enabled") is not False:
                    plan["timeline"]["durationFrames"] = max(plan["timeline"]["durationFrames"],
                                                            clip["timeline"]["startFrame"] + frames)
        # Moving identical clips must preserve identities, not just fingerprints.
        from .document import _descriptors
        descriptors = _descriptors(plan)
        replacement = ctx.pop("idReplacement", {})
        removed_ids = ctx.pop("removedNodeIds", set())
        used_paths = set()
        for node_id, value in objects.items():
            value = replacement.get(node_id, value)
            candidates = [item for item in descriptors if item["raw"] is value
                          and item["kind"] == metadata[node_id]["kind"]
                          and item["path"] not in used_paths] if node_id not in removed_ids else []
            if not isinstance(value, (dict, list)):
                parent = metadata.get(metadata[node_id].get("parentId"), {}).get("locator")
                candidates = [item for item in candidates if item["parentPath"] == parent]
            descriptor = candidates[0] if len(candidates) == 1 else next(
                (item for item in candidates if item["path"] == metadata[node_id]["locator"]), None)
            if descriptor is not None:
                used_paths.add(descriptor["path"])
                metadata[node_id]["locator"] = descriptor["path"]
                metadata[node_id]["fingerprint"] = fingerprint(descriptor["raw"])
            else:
                metadata.pop(node_id, None)
        fresh = {id(root) for root in ctx.pop("newObjects", ())}
        if fresh:
            # Brand-new segments (copies, pastes) get their own IDs now. Left to ensure_node_ids, a copy
            # that sits before an identical original would take over the original's ID.
            roots = [item["path"] for item in descriptors if item["kind"] == "segment" and id(item["raw"]) in fresh]
            created: dict[str, str] = {}
            for item in descriptors:
                if any(item["path"] == root or item["path"].startswith(root + "/") for root in roots):
                    created[item["path"]] = new_id = str(uuid.uuid4())
                    metadata[new_id] = {"kind": item["kind"], "parentId": created.get(item["parentPath"]),
                                        "locator": item["path"], "fingerprint": fingerprint(item["raw"])}
        for node_id, entry in ctx.pop("newNodeMetadata", {}).items():
            if node_id not in metadata:
                metadata[node_id] = entry
        plan["_editor"]["nodes"] = metadata
        ensure_node_ids(plan)
        copied = ctx.pop("copyPairs", None)
        if copied:
            from .segment_ops import inherit_tracks
            inherit_tracks(plan, copied)
        if source_kind == "cutaway":
            # Promotion and later cutaway edits resolve through compiled specs
            # and rawIndex. Refresh after removal too, before indexes can shift.
            # Warm batches record/replay this timing prefix within the same RPC.
            self._refresh_edit_timing(ctx)
            return
        op = operation.get("op")
        edited_kind = metadata.get(str(operation.get("id")), {}).get("kind")
        timing_changed = op in ("split_segment", "insert_segment", "remove_segments", "reorder_segments",
                                "duplicate_segments", "ripple_remove", "paste_segments")
        timing_changed |= op == "trim_segment" and edited_kind == "segment"
        timing_changed |= op == "set_field" and operation.get("path") in (
            "/speed", "/duration", "/in", "/out", "/transition", "/beatSnap", "/music", "/render/fps", "/render",
        )
        if edited_kind == "cutaway":
            timing_changed = False
        if not timing_changed:
            return
        self._refresh_edit_timing(ctx)
        compiled = ctx["compiled"]
        absolute = positioned(plan)
        intervals = ctx.pop("removedIntervals", [])
        if absolute and op != "ripple_remove":
            return  # absolute placements never ripple reel-global events
        new_snapshot = editing_snapshot(plan, compiled, self._revisions())
        old_nodes = [n for n in old_snapshot["nodes"] if n["kind"] == "segment"]
        new_nodes = {n["id"]: n for n in new_snapshot["nodes"] if n["kind"] == "segment"}
        old_fps, new_fps = old_snapshot["fps"], new_snapshot["fps"]
        old_total = old_snapshot.get("editingDurationFrames", old_snapshot["durationFrames"]) / old_fps
        new_total = new_snapshot.get("editingDurationFrames", new_snapshot["durationFrames"]) / new_fps
        def source_time(at, anchor, target):
            offset = at - anchor["startFrame"] / old_fps
            if anchor.get("sourceIn") is not None and target.get("sourceIn") is not None:
                offset = (offset * anchor.get("speed", 1) + anchor["sourceIn"] - target["sourceIn"]) / target.get("speed", 1)
            return target["startFrame"] / new_fps + max(0.0, offset)
        def remap(at, *, endpoint=False):
            if absolute:
                frame = at * old_fps
                if not endpoint and any(a <= frame < b for a, b in intervals):
                    return None
                return max(0.0, (frame - sum(max(0, min(frame, b) - a) for a, b in intervals)) / new_fps)
            if at >= old_total:
                return max(0.0, at + new_total - old_total)
            if op == "split_segment":
                return at
            anchor = next((node for node in reversed(old_nodes) if node["startFrame"] / old_fps <= at), None)
            if anchor is None:
                return at
            target = new_nodes.get(anchor["id"])
            if target is None:
                return None
            return source_time(at, anchor, target)
        for collection in ("text", "musicDrops", "sfx", "exhibits"):
            events = plan.get(collection)
            if not isinstance(events, list):
                continue
            kept = []
            for event in events:
                if not isinstance(event, dict) or (event.get("at") is None and event.get("endAt") is None):
                    kept.append(event)
                    continue
                time_field = "at" if event.get("at") is not None else "endAt"
                old_at = float(event[time_field])
                at = remap(old_at)
                if at is None:
                    continue
                if time_field == "endAt":
                    event["endAt"] = round(at, 6)
                    kept.append(event)
                    continue
                old_end = float(event.get("endAt", old_at + float(event.get("for", 0) or 0)))
                end = remap(old_end, endpoint=True)
                event["at"] = round(at, 6)
                if "for" in event and end is not None:
                    event["for"] = round(max(0.0, end - at), 6)
                if "endAt" in event and end is not None:
                    event["endAt"] = round(max(at, end), 6)
                kept.append(event)
            plan[collection] = kept
        ensure_node_ids(plan)

    def _refresh_edit_timing(self, ctx):
        if "timingResults" in ctx:
            try:
                compiled = next(ctx["timingResults"])
            except StopIteration as exc:
                raise DocumentError("worker_protocol", "compiler omitted an intermediate timing result") from exc
            if not isinstance(compiled, dict) or not {"fps", "total", "segs"}.issubset(compiled):
                raise DocumentError("worker_protocol", "invalid compiler timing evidence")
        else:
            prior_token = self._compile_token
            self._compile_token = ctx.get("cancelToken", prior_token)
            prior_timing = getattr(self._compile_state, "timing_only", False)
            self._compile_state.timing_only = True
            try:
                compiled = self._compile(ctx["plan"], ctx["aux"])
            finally:
                self._compile_token = prior_token
                self._compile_state.timing_only = prior_timing
        ctx["compiled"] = compiled
        ctx["fps"] = int(compiled["fps"])
        ctx["timingOnly"] = True

    def _apply_operation(self, ctx: dict[str, Any], operation: dict[str, Any]) -> None:
        plan: dict[str, Any] = ctx["plan"]
        op = operation.get("op")
        affected: set[str] = ctx["affected"]

        if op == "insert_segment":
            value = operation.get("value")
            if not isinstance(value, dict):
                raise ValueError("insert_segment.value must be an object")
            segments = plan.setdefault("segments", [])
            before = operation.get("beforeId")
            if before:
                _, target = node_location(plan, before)
                if not isinstance(target, dict) or not any(target is item for item in segments):
                    raise ValueError("beforeId must identify a primary segment")
                segments.insert(next(i for i, item in enumerate(segments) if item is target), deepcopy(value))
            else:
                segments.append(deepcopy(value))
            return

        if op == "remove_segments":
            ids = operation.get("ids") or []
            locations = [(node_id, node_location(plan, node_id)[1]) for node_id in ids]
            segments = plan.get("segments") or []
            for node_id, item in locations:
                if not any(item is candidate for candidate in segments):
                    raise ValueError(f"{node_id} is not a primary segment")
                affected.add(node_id)
                segments.pop(next(i for i, candidate in enumerate(segments) if candidate is item))
            return

        if op in ("duplicate_segments", "ripple_remove", "paste_segments", "set_enabled"):
            from . import segment_ops
            if op == "duplicate_segments":
                segment_ops.duplicate(plan, operation.get("ids") or [], ctx)
            elif op == "ripple_remove":
                ctx["removedIntervals"] = segment_ops.ripple_remove(plan, operation.get("ids") or [], affected)
            elif op == "paste_segments":
                ctx["newObjects"] = segment_ops.paste_sequential(plan, ctx["compiled"], operation)
            else:
                segment_ops.set_enabled(plan, operation.get("ids") or [], operation.get("enabled"))
                affected.update(str(item) for item in operation.get("ids") or [])
            return

        if op == "reorder_segments":
            ids = operation.get("ids") or []
            segments = plan.get("segments") or []
            resolved = []
            for node_id in ids:
                _, item = node_location(plan, node_id)
                if not any(item is candidate for candidate in segments):
                    raise ValueError(f"{node_id} is not a primary segment")
                if not any(item is candidate for candidate in resolved):
                    resolved.append(item)
                affected.add(node_id)
            remaining = [item for item in segments if not any(item is candidate for candidate in resolved)]
            before_id = operation.get("beforeId")
            if before_id:
                _, before = node_location(plan, before_id)
                if not any(before is candidate for candidate in remaining):
                    raise ValueError("beforeId must be a segment outside the moved set")
                at = next(i for i, candidate in enumerate(remaining) if candidate is before)
            else:
                at = len(remaining)
            plan["segments"] = remaining[:at] + resolved + remaining[at:]
            return

        if op in ("trim_segment", "trim_cutaway"):
            node_id = str(operation["id"])
            entry, segment = node_location(plan, node_id)
            if entry["kind"] == "cutaway":
                resolved = self._resolved_cutaway(ctx, node_id)
                if resolved.get("mediaKind") == "photo":
                    raise ValueError("photo cutaways use resize_overlay.for")
                for field, value in (("in", resolved.get("sourceIn")), ("out", resolved.get("sourceOut")),
                                     ("speed", resolved.get("speed", 1))):
                    if value is not None:
                        segment.setdefault(field, value)
                source_in, source_out = float(operation["sourceIn"]), float(operation["sourceOut"])
                if resolved.get("sourceDuration") is not None and source_out > resolved["sourceDuration"]:
                    raise ValueError("cutaway sourceOut exceeds the original source duration")
                trim_local_source(segment, source_in, source_out, precision=12 if plan.get("timeline") else 3,
                                  resolved={"spec": resolved["resolved"]})
                affected.add(node_id)
                return
            if entry["kind"] != "segment" or op == "trim_cutaway":
                raise ValueError("trim operation requires a video segment or cutaway")
            index = int(entry["locator"].split("/")[2])
            compiled_seg = ctx["compiled"]["segs"][index]
            for field in ("in", "out"):
                segment.setdefault(field, compiled_seg["spec"].get(field))
            trim_segment(
                plan, index, float(operation["sourceIn"]), float(operation["sourceOut"]),
                fps=ctx["fps"], start_frame=int(compiled_seg["start"]), old_frames=int(compiled_seg["frames"]),
                ripple=False, resolved=compiled_seg,
            )
            affected.add(node_id)
            return

        if op == "split_segment":
            node_id = str(operation["id"])
            entry, segment = node_location(plan, node_id)
            if entry["kind"] != "segment":
                raise ValueError("split_segment.id must identify a primary segment")
            index = int(entry["locator"].split("/")[2])
            compiled_seg = ctx["compiled"]["segs"][index]
            for field in ("in", "out"):
                segment.setdefault(field, compiled_seg["spec"].get(field))
            at = int(operation["atFrame"])
            local_frame = at - int(compiled_seg["start"])
            if not 0 < local_frame < int(compiled_seg["frames"]):
                raise ValueError("split frame must be inside the segment")
            left, right = split_segment(segment, local_frame, fps=ctx["fps"],
                                        total_frames=int(compiled_seg["frames"]), resolved=compiled_seg)
            plan["segments"][index:index + 1] = [left, right]
            ctx["idReplacement"] = {node_id: left}
            affected.add(node_id)
            return

        if op == "insert_overlay":
            parent_id = operation.get("parentId")
            collection = str(operation.get("collection") or "")
            value = operation.get("value")
            if not isinstance(value, dict):
                raise ValueError("insert_overlay.value must be an object")
            parent: dict[str, Any]
            if parent_id not in (None, "", "document", plan.get("_editor", {}).get("documentId")):
                entry, parent = node_location(plan, str(parent_id))
                if entry["kind"] != "segment":
                    raise ValueError("overlay parentId must identify a primary segment")
            else:
                parent = plan
            allowed = {"text", "stats", "nat", "sfx", "bleep", "punch", "blur", "path", "exhibits", "cutaways"}
            if parent is plan:
                allowed = {"text", "musicDrops", "sfx", "exhibits"}
            if collection not in allowed:
                raise ValueError("invalid overlay collection")
            parent.setdefault(collection, [])
            if not isinstance(parent[collection], list):
                raise ValueError(f"{collection} is not an array")
            parent[collection].append(deepcopy(value))
            if parent_id:
                affected.add(str(parent_id))
            return

        if op in ("move_overlay", "resize_overlay", "remove_overlay"):
            node_id = str(operation["id"])
            if node_id in ctx.get("generatedRemoved", set()):
                raise ValueError("generated cue was already removed in this batch")
            aliases = ctx.get("generatedAliases", {})
            if node_id in aliases:
                operation = {**operation, "id": aliases[node_id]}
                node_id = aliases[node_id]
            generated = {n["id"]: n for n in editing_snapshot(plan, ctx["compiled"], self._revisions())["nodes"]
                         if n.get("generated")}
            if node_id in generated:
                if generated[node_id]["kind"] != "generated_sfx":
                    raise DocumentError("read_only_node", "renderer-generated caption/effect nodes are read-only; edit their raw source settings")
                self._materialize_generated(ctx, node_id, operation)
                affected.add(node_id)
                return
            entry, item = node_location(plan, node_id)
            if entry["kind"] == "segment":
                raise ValueError("overlay operation requires a timed overlay node")
            if op == "remove_overlay":
                parent, key = pointer_parent(plan, entry["locator"])
                if isinstance(parent, list):
                    parent.pop(int(key))
                elif parent is plan and key == "endCard":
                    parent[key] = False
                else:
                    parent.pop(key)
                ctx.setdefault("removedNodeIds", set()).add(node_id)
                affected.add(node_id)
                return
            if entry["kind"] == "cutaway":
                resolved = self._resolved_cutaway(ctx, node_id)
                if "at" in operation:
                    at = float(operation["at"])
                    if not math.isfinite(at) or at < 0:
                        raise ValueError("cutaway at must be nonnegative segment-local seconds")
                    item["at"] = round(at, 6)
                if op == "resize_overlay" and "for" in operation:
                    duration = float(operation["for"])
                    if not math.isfinite(duration) or duration <= 0:
                        raise ValueError("cutaway duration must be positive and finite")
                    if resolved.get("mediaKind") == "photo":
                        item["duration"] = round(duration, 6)
                    else:
                        start = float(item.get("in", resolved["sourceIn"]))
                        speed = float(item.get("speed", resolved.get("speed", 1)))
                        self._apply_operation(ctx, {"op": "trim_cutaway", "id": node_id,
                                                   "sourceIn": start, "sourceOut": start + duration * speed})
                affected.add(node_id)
                return
            if entry["kind"] == "sfx" and op == "resize_overlay" and "for" in operation:
                raise DocumentError("unsupported_timing", "SFX duration is determined by the selected sound")
            if not isinstance(item, dict):
                raise ValueError("overlay must be an object")
            if op == "move_overlay":
                item.pop("endAt", None)
                item["at"] = round(float(operation["at"]), 6)
            else:
                if "at" in operation:
                    item["at"] = round(float(operation["at"]), 6)
                if "for" in operation:
                    value = float(operation["for"])
                    if value < 0:
                        raise ValueError("overlay duration cannot be negative")
                    item["for"] = round(value, 6)
            affected.add(node_id)
            return

        if op in ("set_field", "set_keyframes"):
            node_id = operation.get("id")
            path = str(operation.get("path") or "")
            if node_id == "project":
                if op != "set_field":
                    raise ValueError("project config supports set_field only")
                self._set_project_config(ctx, path, operation.get("value"))
                affected.add("project")
                return
            if node_id in (None, "", "document"):
                target = plan
                node_id = None
            else:
                entry, target = node_location(plan, str(node_id))
                if not isinstance(target, (dict, list)):
                    if op == "set_field" and path == "/value":
                        parent, key = pointer_parent(plan, entry["locator"])
                        value = deepcopy(operation.get("value"))
                        parent[int(key) if isinstance(parent, list) else key] = value
                        ctx.setdefault("idReplacement", {})[str(node_id)] = value
                        affected.add(str(node_id))
                        return
                    raise ValueError("target node cannot contain fields")
                if entry["kind"] == "generated_sfx":
                    raise ValueError("generated events must be moved/resized to materialize an override")
            if not path.startswith("/") or path == "/_editor" or path.startswith("/_editor/"):
                raise ValueError("path must be a JSON pointer outside reserved _editor metadata")
            if path == "/timeline" or path.startswith("/timeline/"):
                raise ValueError("timeline placement is edited atomically with move_segments")
            if node_id is None and (path == "/segments" or path.startswith("/segments/")):
                raise ValueError("primary segments must be edited through stable node IDs")
            if node_id is None and (path == "/render" or path.startswith("/render/")):
                raise ValueError("render defaults belong to project config: use id='project' and path='/render/…'")
            components = [c.replace("~1", "/").replace("~0", "~") for c in path.lstrip("/").split("/")]
            parent = target
            if (len(components) > 1 and components[0] == "grade" and components[1] != "look"
                    and isinstance(target, dict) and target.get("grade") is None):
                # A new grade object without `look` would render as "house": pin the look the clip shows now.
                inherited = grade_mod.settings(self.project.config.get("render", {}).get("grade"),
                                               None if target is plan else plan.get("grade"))
                target["grade"] = {"look": inherited["look"]}
            for component in components[:-1]:
                if isinstance(parent, list):
                    raise ValueError("array elements must be edited through stable node IDs")
                if parent.get(component) is None:  # a group's first field (e.g. /grade/strength) creates the group
                    parent[component] = {}
                parent = parent[component]
                if not isinstance(parent, (dict, list)):
                    raise ValueError(f"{component!r} holds a value, not an object, so {path} can't be set inside it")
            key = components[-1]
            value = deepcopy(operation.get("value"))
            camera_edit = isinstance(target, dict) and components[0] in ("path", "view", "yaw", "pitch", "roll", "fov", "projection")
            window = target.get("cameraWindow") if camera_edit else None
            tags, moment = {}, {}
            if camera_edit and (window or node_id in ctx.get("clearedCameraWindows", {})):
                from ..library import load_asset, resolve_moment
                from ..sphere import plan_camera

                asset = load_asset(self.project, target["asset"])
                tags = asset.get("tags") or {}
                moment = resolve_moment(asset, target["moment"]) if "moment" in target else {}
                if isinstance(window, dict):
                    # Freeze sparse exterior poses before clearing any base-view
                    # overrides in the aim batch, not after the last field write.
                    errors: list[str] = []
                    camera = plan_camera({**target, "path": window["path"]}, tags, moment.get("view"),
                                         window["durationSeconds"], errors, [], "camera window")
                    if errors:
                        raise ValueError("; ".join(errors))
                    window = {**window, "path": camera["path"]}
            if isinstance(parent, list):
                raise ValueError("array elements must be edited through stable node IDs")
            else:
                parent[key] = value
            if camera_edit:
                cleared = ctx.setdefault("clearedCameraWindows", {})
                if path in ("/yaw", "/pitch", "/roll", "/fov") and value is None:
                    if isinstance(window, dict):
                        cleared[node_id] = window
                elif path == "/path" and isinstance(value, list):
                    window = window or cleared.pop(node_id, None)
                else:
                    cleared.pop(node_id, None)
                kept = None
                if path == "/path" and isinstance(value, list) and isinstance(window, dict):
                    # an aim edit on a split piece: its keys go into the window too, so the
                    # renderer (which reads the window) shows them and trims keep the move
                    from .timing import window_with_piece_keys
                    kept = window_with_piece_keys(window, {**moment, **target}, value,
                                                  tags=tags, moment_view=moment.get("view"))
                if kept is not None:
                    target["cameraWindow"] = kept
                else:
                    target.pop("cameraWindow", None)
            if node_id:
                affected.add(str(node_id))
            return

        if op == "correct_words":
            asset_id = str(operation["assetId"])
            asset_path = self.project.assets_dir / f"{asset_id}.json"
            if not asset_path.is_file():
                raise ValueError(f"unknown asset id: {asset_id}")
            asset = read_json(asset_path)
            transcript_rel = asset.get("transcript")
            if not transcript_rel:
                raise ValueError(f"{asset_id} has no transcript")
            transcript_path = self.project.abs(transcript_rel).resolve()
            try:
                rel = transcript_path.relative_to(self.data_root).as_posix()
            except ValueError as exc:
                raise DocumentError("path_denied", "transcript is outside private data root") from exc
            transcript = deepcopy(ctx["aux"].get(rel))
            if transcript is None:
                transcript = read_json(transcript_path)
            words = transcript.get("words")
            if not isinstance(words, list):
                raise ValueError("transcript has no words array")
            for correction in operation.get("corrections") or []:
                index, word = int(correction["index"]), str(correction["word"])
                if not 0 <= index < len(words):
                    raise ValueError(f"word index {index} is out of range")
                words[index]["w"] = word
            ctx["aux"][rel] = transcript
            affected.add(asset_id)
            return

        raise ValueError(f"unsupported operation: {op!r}")

    def _resolved_cutaway(self, ctx: dict[str, Any], node_id: str) -> dict[str, Any]:
        node = next((n for n in editing_snapshot(ctx["plan"], ctx["compiled"], self._revisions())["nodes"]
                     if n["id"] == node_id and n["kind"] == "cutaway" and n.get("resolved")), None)
        if node is None:
            raise ValueError("cutaway has no resolved source bounds in the compiled snapshot")
        return node

    def _set_project_config(self, ctx: dict[str, Any], path: str, value: Any) -> None:
        allowed = {"render", "audio", "brand", "captions", "sfx", "units"}
        if not path.startswith("/"):
            raise ValueError("project config path must be a JSON pointer")
        section = path.lstrip("/").split("/", 1)[0].replace("~1", "/").replace("~0", "~")
        from .workspace import _sensitive_key
        def sensitive(item):
            if isinstance(item, dict):
                return any(_sensitive_key(str(key)) or sensitive(child) for key, child in item.items())
            return isinstance(item, list) and any(sensitive(child) for child in item)
        if any(_sensitive_key(component) for component in path.split("/")) or sensitive(value):
            raise DocumentError("config_permission", "credential fields cannot be edited through project operations")
        if section not in allowed:
            raise DocumentError(
                "config_permission",
                f"project config section {section!r} is not editable through this operation",
                {"allowedSections": sorted(allowed)},
            )
        config_rel = (self.project.root / "config.json").resolve().relative_to(self.data_root).as_posix()
        if config_rel in ctx["aux"]:
            config = deepcopy(ctx["aux"][config_rel])
        elif (self.project.root / "config.json").is_file():
            config = read_json(self.project.root / "config.json")
        else:
            config = {}
        if not isinstance(config, dict):
            raise DocumentError("invalid_config", "private config.json must contain an object")
        parts = [part.replace("~1", "/").replace("~0", "~") for part in path.lstrip("/").split("/")]
        cursor = config
        for component in parts[:-1]:
            next_value = cursor.get(component)
            if next_value is None:
                next_value = {}
                cursor[component] = next_value
            if not isinstance(next_value, dict):
                raise ValueError(f"config path component {component!r} is not an object")
            cursor = next_value
        cursor[parts[-1]] = deepcopy(value)
        from ..timeline import positioned, requantize
        if positioned(ctx["plan"]) and section == "render":
            fps = int(deep_merge(deepcopy(DEFAULTS), config)["render"]["fps"])
            if fps != ctx["plan"]["timeline"]["fps"]:
                from .tracks import track_list, locked_target
                locked_rows = {row["id"] for row in track_list(ctx["plan"].get("_editor")) if row["locked"]}
                for node_id, entry in ensure_node_ids(ctx["plan"]).items():
                    message = locked_target(ctx["plan"], {"id": node_id})
                    raw = pointer_get(ctx["plan"], entry["locator"])
                    if message or (isinstance(raw, dict) and raw.get("timeline", {}).get("track") in locked_rows):
                        raise DocumentError("track_locked", message or "FPS change affects a locked track")
                requantize(ctx["plan"], fps)
        if section == "units" and config.get("units") not in (None, "imperial", "metric"):
            raise DocumentError("invalid_config", "units must be 'imperial' or 'metric'")
        ctx["aux"][config_rel] = config

    def activate_renderer(
        self,
        renderer_revision: str,
        expected: dict[str, Any],
        reason: str,
        operations: list[dict[str, Any]] | None = None,
        preview: str | dict[str, Any] | None = None,
        *,
        candidate_compiler: Callable[..., dict[str, Any]] | None = None,
        source_ref: str | None = None,
    ) -> dict[str, Any]:
        """Atomically make a prepared renderer revision and its edit live.

        RendererManager owns source snapshotting and candidate validation. It
        calls this only after preparing an immutable candidate and matching
        preview. The revision/preview references, plan operations, history
        manifest and HEAD pointer then commit as one document revision.
        """
        renderer_revision = str(renderer_revision).strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", renderer_revision):
            raise DocumentError("invalid_renderer_revision", "renderer_revision is required")
        with self._lock:
            self._ensure_open()
            self._check_expected(expected)
            self._check_lease({"origin": "ai"})
            epoch = self._epoch("renderer")
            self._compile_token = ("renderer", epoch)
            base_revision = self._head_revision()
            files = self._load_files()
            plan = deepcopy(files[self.plan_rel])
            aux = {k: deepcopy(v) for k, v in files.items() if k != self.plan_rel}
            try:
                compiled_before = self._current_compiled or self._compile(plan, aux, self._renderer_revision())
            except DocumentError:
                self._compile_token = None
                if self._epoch("renderer") != epoch:
                    raise DocumentError("cancelled", "renderer activation was cancelled during validation")
                raise
            if self._epoch("renderer") != epoch:
                self._compile_token = None
                raise DocumentError("cancelled", "renderer activation was cancelled during validation")
            self._compile_token = None
            context = {
                "plan": plan, "files": files, "aux": aux, "compiled": compiled_before,
                "fps": int(compiled_before["fps"]), "affected": set(), "origin": "ai",
            }
            prepared = self._prepared_candidates.get(fingerprint({"expected": expected, "operations": operations or []}))
            if operations and preview is not None and hasattr(self.workspace, "code_root") and prepared is None:
                raise DocumentError("candidate_required",
                                    "prepare the frozen operation batch with candidate_snapshot before validating its preview")
            if prepared:
                if prepared["aiEpoch"] != self._epoch("ai"):
                    raise DocumentError("cancelled", "renderer candidate's AI turn was cancelled")
                plan = deepcopy(prepared["plan"])
                aux = deepcopy(prepared["aux"])
                context.update(plan=plan, aux=aux, affected=set(prepared["affected"]))
            for index, operation in enumerate([] if prepared else operations or []):
                if not isinstance(operation, dict):
                    raise DocumentError("invalid_operation", f"operation {index} must be an object")
                try:
                    self._apply_edit_operation(context, operation)
                except DocumentError as exc:
                    exc.details.setdefault("operation", index)
                    raise
                except (KeyError, IndexError, TypeError, ValueError) as exc:
                    raise DocumentError("invalid_operation", str(exc), {"operation": index}) from exc
            editor = plan.setdefault("_editor", {})
            editor["rendererRevision"] = renderer_revision
            preview_record: dict[str, Any] | None
            if isinstance(preview, str):
                preview_record = {"ref": preview}
            elif isinstance(preview, dict):
                preview_record = deepcopy(preview)
            elif preview is None:
                preview_record = None
            else:
                raise DocumentError("invalid_preview", "preview must be a reference string or object")
            if preview_record is not None:
                if not preview_record.get("ref"):
                    raise DocumentError("invalid_preview", "preview reference object requires a non-empty ref")
                preview_renderer = preview_record.get("rendererRevision", renderer_revision)
                if str(preview_renderer) != renderer_revision:
                    raise DocumentError("invalid_preview", "prepared preview renderer revision does not match candidate",
                                        {"previewRenderer": preview_renderer, "rendererRevision": renderer_revision})
                preview_input = preview_record.get("inputDocumentRevision", base_revision)
                if str(preview_input) != base_revision:
                    raise DocumentError("invalid_preview", "prepared preview was not built from the expected document revision",
                                        {"previewDocumentRevision": preview_input, "expectedDocument": base_revision})
                preview_record["rendererRevision"] = renderer_revision
                preview_record["inputDocumentRevision"] = base_revision
            elif "rendererPreview" in editor:
                # A code change cannot claim the prior renderer's preview.
                editor.pop("rendererPreview", None)

            ensure_node_ids(plan)
            files[self.plan_rel] = plan
            files.update(aux)
            provider = getattr(self.workspace, "compiler_for_renderer", None)
            if candidate_compiler is None and callable(provider):
                candidate_compiler = provider(renderer_revision)
            self._compile_token = ("renderer", epoch)
            try:
                candidate_compiled = self._compile(plan, aux, renderer_revision, candidate_compiler)
            except DocumentError:
                if self._epoch("renderer") != epoch:
                    raise DocumentError("cancelled", "renderer activation was cancelled during validation")
                raise
            finally:
                self._compile_token = None
            if self._epoch("renderer") != epoch:
                raise DocumentError("cancelled", "renderer activation was cancelled during validation")
            # This second check is deliberately adjacent to the commit point:
            # candidate builds may take minutes and manual edits always win.
            self._check_expected(expected)
            self._check_lease({"origin": "ai"})
            if self._head_revision() != base_revision:
                raise DocumentError("conflict", "document changed while renderer candidate was validating",
                                    {"current": self._revisions()})
            revision = self._commit_files(
                files, origin="renderer", reason=reason or "activate renderer revision",
                affected_ids=list(context["affected"]), parent=base_revision, undo_target=base_revision,
                renderer_revision=renderer_revision, preview=preview_record, source_ref=source_ref,
                rebind_preview=preview_record is not None,
                clear_preview=preview_record is None,
                expected_revisions=expected,
                cancel_token=("renderer", epoch),
            )
            if candidate_compiler is not None:
                self._revision_compilers[renderer_revision] = candidate_compiler
            self._current_compiled = candidate_compiled
            return self._snapshot_result(plan, candidate_compiled, self._revisions(revision))

    def _materialize_generated(self, ctx: dict[str, Any], node_id: str, operation: dict[str, Any]) -> None:
        nodes = editing_snapshot(ctx["plan"], ctx["compiled"], self._revisions())["nodes"]
        generated = next((n for n in nodes if n["id"] == node_id and n["kind"] == "generated_sfx"), None)
        if generated is None:
            raise ValueError("generated cue is no longer present in the current compiled revision")
        if operation.get("op") == "resize_overlay" and "for" in operation:
            raise DocumentError("unsupported_timing", "SFX duration is determined by the selected sound")
        plan = ctx["plan"]
        fps = ctx["fps"]
        # Disable automatic generation and materialize every currently visible
        # generated cue, so only the moved cue changes and no siblings vanish.
        explicit = list(plan.get("sfx") or [])
        materialized = {}
        for node in nodes:
            if node["kind"] != "generated_sfx":
                continue
            cue = node["raw"]
            if node["id"] == node_id and operation["op"] == "remove_overlay":
                ctx.setdefault("generatedRemoved", set()).add(node_id)
                continue
            at = round(int(cue.get("startFrame", 0)) / fps, 6)
            if node["id"] == node_id:
                if operation.get("op") == "move_overlay":
                    at = round(float(operation["at"]), 6)
                elif operation.get("at") is not None:
                    at = round(float(operation["at"]), 6)
            item = {"sound": cue["sound"], "at": at, "gain": cue.get("gain", 0.7),
                    "_editorGeneratedEvent": cue.get("source")}
            explicit.append(item)
            materialized[node["id"]] = item
        plan["sfx"] = explicit
        plan["autoSfx"] = False
        ids = ensure_node_ids(plan)
        aliases = ctx.setdefault("generatedAliases", {})
        for generated_id, item in materialized.items():
            aliases[generated_id] = next(node for node, entry in ids.items()
                                         if pointer_get(plan, entry["locator"]) is item)
        # Keep the freshly assigned IDs through _apply_edit_operation's identity
        # reconciliation, including any later operations in this same batch.
        ctx.setdefault("newNodeMetadata", {}).update(deepcopy(ids))

    # ---- history and external materialization --------------------------

    def _preserve_usage(self, files: dict[str, Any], current: dict[str, Any]) -> None:
        # Completed exports are facts, not editing gestures. Restoring a plan
        # checkpoint must not erase receipts or newer library annotations.
        for rel in set(files) | set(current.get("files", {})):
            if rel.startswith(("library/assets/", "music/")) and rel.endswith(".json"):
                path = self._safe_data_path(rel)
                if path.is_file():
                    files[rel] = read_json(path)

    def undo(self, expected: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            self._check_lease({})
            expected = expected or self._revisions()
            self._check_expected(expected, strict=expected is not None)
            current = self._manifest()
            logical = self._manifest(current["restoredFrom"]) if current.get("restoredFrom") else current
            target = current.get("undoTarget")
            if not target:
                raise DocumentError("nothing_to_undo", "there is no earlier document revision")
            target_manifest = self._manifest(target)
            files = self._manifest_files(target_manifest)
            for rel, digest in logical.get("previousFiles", {}).items():
                if digest is None:
                    files.pop(rel, None)
                else:
                    files[rel] = self._object_value(digest)
            self._preserve_usage(files, current)
            candidate_plan = files.get(self.plan_rel)
            aux = {k: v for k, v in files.items() if k != self.plan_rel}
            target_renderer = str(target_manifest.get("rendererRevision") or
                                  candidate_plan.get("_editor", {}).get("rendererRevision") or self._renderer_revision())
            removed = sorted(set(current.get("files", {})) - set(files))
            compiled = self._compile(candidate_plan, aux, target_renderer, removed_files=removed)
            self._check_lease({})
            redo_stack = list(current.get("redoStack") or []) + [logical["revision"]]
            revision = self._commit_files(
                files, origin="undo", reason=f"undo: {logical.get('reason', 'edit')}",
                affected_ids=current.get("affectedIds", []), parent=current["revision"],
                undo_target=target_manifest.get("undoTarget"), redo_target=redo_stack[-1], redo_stack=redo_stack,
                restored_from=target, renderer_revision=target_renderer,
                preview=target_manifest.get("preview"), rebind_preview=target_manifest.get("preview") is not None,
                clear_preview=target_manifest.get("preview") is None,
                expected_revisions=expected,
            )
            self._current_compiled = compiled
            return self._snapshot_result(candidate_plan, compiled, self._revisions(revision))

    def redo(self, expected: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            self._ensure_open()
            self._check_lease({})
            expected = expected or self._revisions()
            self._check_expected(expected, strict=expected is not None)
            current = self._manifest()
            redo_stack = list(current.get("redoStack") or [])
            target = redo_stack.pop() if redo_stack else current.get("redoTarget") or self._redo_target
            if not target:
                raise DocumentError("nothing_to_redo", "there is no edit to redo")
            target_manifest = self._manifest(target)
            files = self._manifest_files(target_manifest)
            self._preserve_usage(files, current)
            candidate_plan = files.get(self.plan_rel)
            aux = {k: v for k, v in files.items() if k != self.plan_rel}
            target_renderer = str(target_manifest.get("rendererRevision") or
                                  candidate_plan.get("_editor", {}).get("rendererRevision") or self._renderer_revision())
            removed = sorted(set(current.get("files", {})) - set(files))
            compiled = self._compile(candidate_plan, aux, target_renderer, removed_files=removed)
            self._check_lease({})
            revision = self._commit_files(
                files, origin="redo", reason=f"redo: {target_manifest.get('reason', 'edit')}",
                affected_ids=target_manifest.get("affectedIds", []), parent=current["revision"],
                undo_target=target_manifest.get("undoTarget"), redo_target=redo_stack[-1] if redo_stack else None,
                redo_stack=redo_stack, restored_from=target,
                renderer_revision=target_renderer, preview=target_manifest.get("preview"),
                rebind_preview=target_manifest.get("preview") is not None,
                clear_preview=target_manifest.get("preview") is None,
                expected_revisions=expected,
            )
            self._current_compiled = compiled
            return self._snapshot_result(candidate_plan, compiled, self._revisions(revision))

    def reload_external(self) -> dict[str, Any]:
        """Validate private materialized edits and commit them as new revisions."""
        with self._lock:
            self._ensure_open()
            current = self._manifest()
            if not current:
                raise DocumentError("not_found", "document has not been initialized")
            changed = {}
            removed: list[str] = []
            watched = set(current.get("files", {}))
            if (self.data_root / "config.json").is_file():
                watched.add("config.json")
            for rel in watched:
                path = self._safe_data_path(rel)
                if not path.exists():
                    if rel == self.plan_rel:
                        raise DocumentError("external_invalid", "the authoritative plan file was removed")
                    removed.append(rel)
                    continue
                try:
                    value = read_json(path)
                except Exception as exc:  # noqa: BLE001
                    raise DocumentError("external_invalid", f"cannot read externally edited {rel}: {exc}") from exc
                if not self._disk_matches(rel, current["files"].get(rel, "")):
                    changed[rel] = _without_editor(rel, value)
            if not changed and not removed:
                self._recover()
                return self.snapshot()
            files = self._manifest_files(current)
            for rel in removed:
                files.pop(rel, None)
            files.update(changed)
            plan = files.get(self.plan_rel)
            if not isinstance(plan, dict):
                raise DocumentError("external_invalid", "external plan root must remain a JSON object")
            if self.plan_rel in changed:
                # The file carries no metadata: identity comes from the committed head, then the sidecar.
                head_plan = self._object_value(current["files"][self.plan_rel]) if self.plan_rel in current["files"] else {}
                meta = head_plan.get(EDITOR_KEY) if isinstance(head_plan, dict) else None
                if not isinstance(meta, dict):
                    meta = self._read_sidecar(self.plan_rel)
                if isinstance(meta, dict):
                    plan[EDITOR_KEY] = deepcopy(meta)
            ensure_node_ids(plan)
            plan.setdefault("_editor", {})["rendererRevision"] = self._renderer_revision()
            aux = {k: v for k, v in files.items() if k != self.plan_rel}
            expected = self._revisions()
            disk_versions = {rel: fingerprint(self._read_disk(rel))
                             for rel in watched if rel not in removed}
            compiled = self._compile(plan, aux, removed_files=removed)
            if self._revisions() != expected or any(
                not self._safe_data_path(rel).is_file()
                or fingerprint(self._read_disk(rel)) != digest
                for rel, digest in disk_versions.items()
            ) or any(self._safe_data_path(rel).exists() for rel in removed):
                raise DocumentError("conflict", "private data changed again during external validation")
            self._check_lease({"origin": "external"})
            revision = self._commit_files(
                files, origin="external", reason="reload externally edited private files",
                affected_ids=list(ensure_node_ids(plan)), parent=current["revision"],
                undo_target=current["revision"],
            )
            self._current_compiled = compiled
            return self._snapshot_result(plan, compiled, self._revisions(revision))
