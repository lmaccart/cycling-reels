"""Broker-side persistent sandbox compiler.

``WarmCompilePool.compile`` returns the cold compiler's result shape. The caller
owns revision checks before/after validation, offline gating and cold fallback.
Only ``WorkerUnavailable`` is eligible for fallback; cancellation and renderer
rejections must propagate. No workspace or transaction import is needed here.
"""

from __future__ import annotations

import hashlib
import json
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import uuid
from collections import deque
from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, TimeoutError
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .compile_runtime import INPUT_DIRS, INPUT_FILES, PROTOCOL_VERSION


class WorkerUnavailable(Exception):
    """Transport/startup/deadline failure: a cold sandbox may retry."""


class WorkerRejected(Exception):
    """Renderer rejected the candidate: retry must not mask validation failure."""

    def __init__(self, message, code="validation_failed", details=None):
        super().__init__(message)
        self.code, self.details = code, details or {}


class WorkerCancelled(Exception):
    """The caller invalidated a request. Never retry it."""


def boundary_key(renderer_revision: str, reads: list[Path], profile: str,
                 scratch: Path, *, data_root: Path, media_root: Path | None,
                 canonical: bool = False) -> str:
    """Scratch names do not affect reuse; every capability/identity input does."""
    scratch_text = str(scratch.resolve())
    payload = {
        "version": PROTOCOL_VERSION,
        "runner": _runner_digest(),
        "renderer": renderer_revision,
        "dataRoot": str(data_root.resolve()),
        "mediaRoot": str(media_root.resolve()) if media_root else None,
        "reads": sorted(str(path if canonical else path.resolve()).replace(scratch_text, "$SCRATCH")
                        for path in reads),
        "profile": profile.replace(scratch_text, "$SCRATCH"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _runner_digest():
    digest = hashlib.sha256()
    for name in ("compile_runtime.py", "transactions.py", "overwrite.py", "timing.py",
                 "tracks.py", "document.py"):
        digest.update(Path(__file__).with_name(name).read_bytes())
    digest.update((Path(__file__).parent.parent / "timeline.py").read_bytes())
    return digest.hexdigest()


def _boundary(workspace, project, code_root: Path, scratch: Path, *, make_env=True):
    from .preview import _path_roots, _profile_text, _safe_env, _executables

    context = SimpleNamespace(
        project=project, root=workspace.root, data_root=project.root,
        code_root=code_root, _originals=getattr(workspace, "_originals", {}),
    )
    reads, _ = _path_roots(context, code_root, scratch, scratch, scratch, scratch)
    for relative in (*INPUT_DIRS, *INPUT_FILES):
        path = project.root / relative
        if path.exists():
            if path.is_symlink():
                raise WorkerUnavailable(f"compiler input root is a symlink: {relative}")
            reads.append(path.resolve())
    if not project.read_only_media and project.media_dir.is_dir():
        reads.append(project.media_dir.resolve())
    executables = _executables(context, code_root)
    # Resolve capabilities on every call, but profile serialization is pure for
    # those exact canonical paths. Do not resolve the same ancestors hundreds
    # of times for every compile and cache write.
    key = (tuple(map(str, reads)), tuple(sorted(map(str, executables))), str(scratch),
           id(_profile_text))
    with _OWNERS_GUARD:
        cache = getattr(workspace, "_editor_compile_boundaries", None)
        if cache is None:
            cache = workspace._editor_compile_boundaries = {}
        profile = cache.get(key)
    if profile is None:
        profile = _profile_text(reads, [scratch.resolve()], executables)
        with _OWNERS_GUARD:
            if len(cache) >= 8:
                cache.pop(next(iter(cache)))
            cache[key] = profile
    return reads, profile, _safe_env(context, scratch, code_root) if make_env else None


class _Worker:
    def __init__(self, workspace, project, revision: str, *, timeout: float, max_requests: int):
        self.timeout, self.max_requests = timeout, max_requests
        self.revision, self.requests = revision, 0
        self.pending = 0
        self.closed = False
        self._close_lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="editor-compile")
        self.responses: queue.Queue = queue.Queue()
        self.stderr: deque[str] = deque(maxlen=40)
        self.tools = tuple(project.config.get("tools", {}).get(key) for key in ("ffmpeg", "ffprobe"))
        sandbox = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
        if sandbox is None:
            self.executor.shutdown(wait=False)
            raise WorkerUnavailable("renderer compilation requires the trusted macOS sandbox")
        parent = project.root / ".cache/editor-compile"
        parent.mkdir(parents=True, exist_ok=True)
        self.directory = tempfile.TemporaryDirectory(prefix="warm-", dir=parent)
        scratch = Path(self.directory.name).resolve()
        self.scratch = scratch
        try:
            reads, profile, env = _boundary(workspace, project, project.code_root, scratch)
            self.media_root = project.media_dir if project.read_only_media else None
            self.key = boundary_key(revision, reads, profile, scratch,
                                    data_root=project.root, media_root=self.media_root, canonical=True)
            profile_path = scratch / "compile.sb"
            profile_path.write_text(profile, encoding="utf-8")
            self.process = subprocess.Popen(
                [sandbox, "-f", str(profile_path), sys.executable,
                 str(Path(__file__).with_name("compile_runtime.py").resolve()),
                 str(project.code_root / "scripts"), str(project.root),
                 str(scratch / "private"), revision,
                 str(self.media_root) if self.media_root else ""],
                cwd=scratch, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, start_new_session=True, bufsize=1,
            )
        except Exception:
            self.executor.shutdown(wait=False)
            self.directory.cleanup()
            raise
        threading.Thread(target=self._read, daemon=True, name="editor-compile-output").start()
        threading.Thread(target=self._read_errors, daemon=True, name="editor-compile-errors").start()

    def _read(self) -> None:
        try:
            while True:
                line = self.process.stdout.readline(16 * 1024 * 1024)
                if not line:
                    break
                if not line.endswith("\n"):
                    raise ValueError("compiler response exceeds protocol limit")
                self.responses.put(json.loads(line))
        except Exception as exc:
            self.responses.put(WorkerUnavailable(f"invalid compiler response: {exc}"))
        finally:
            self.responses.put(WorkerUnavailable("compiler worker exited"))
            self.process.stdout.close()

    def _read_errors(self) -> None:
        try:
            while line := self.process.stderr.readline(4096):
                self.stderr.append(line[-2000:])
        finally:
            self.process.stderr.close()

    def matches(self, workspace, project, revision: str) -> bool:
        if self.closed or self.process.poll() is not None or self.requests >= self.max_requests:
            return False
        if tuple(project.config.get("tools", {}).get(key) for key in ("ffmpeg", "ffprobe")) != self.tools:
            return False
        reads, profile, _ = _boundary(workspace, project, project.code_root, self.scratch, make_env=False)
        key = boundary_key(revision, reads, profile, self.scratch, data_root=project.root,
                            media_root=project.media_dir if project.read_only_media else None, canonical=True)
        return key == self.key

    def _exchange(self, request: dict, cancelled: Callable[[], bool]) -> dict:
        if cancelled():
            raise WorkerCancelled("compiler request was cancelled before validation")
        if self.closed or self.process.poll() is not None:
            raise WorkerUnavailable("compiler worker is no longer running")
        if self.requests >= self.max_requests:
            self.close()
            raise WorkerUnavailable("compiler request cap reached")
        self.requests += 1
        def expire():
            # Covers a blocked stdin write/import as well as response reading.
            self.responses.put(WorkerUnavailable("compiler worker exceeded validation deadline"))
            self.close()
        deadline = threading.Timer(self.timeout, expire)
        deadline.daemon = True
        deadline.start()
        try:
            self.process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self.process.stdin.flush()
            response = self.responses.get(timeout=self.timeout)
            if isinstance(response, Exception):
                raise response
            if not isinstance(response, dict):
                raise WorkerUnavailable("compiler response is not an object")
            if (response.get("requestId") != request["requestId"]
                    or response.get("version") != PROTOCOL_VERSION):
                raise WorkerUnavailable("compiler response identity mismatch")
            if "error" in response:
                if (not isinstance(response["error"], str)
                        or not isinstance(response.get("errorCode", ""), str)
                        or not isinstance(response.get("errorDetails", {}), dict)):
                    raise WorkerUnavailable("invalid compiler error response")
                if response.get("errorKind") == "unavailable":
                    raise WorkerUnavailable(response["error"])
                raise WorkerRejected(response["error"], response.get("errorCode", "validation_failed"),
                                     response.get("errorDetails"))
            if (response.get("rendererRevision") != request["rendererRevision"]
                    or response.get("libraryRevision") != request["libraryRevision"]):
                raise WorkerUnavailable("compiler response revision mismatch")
            result = response.get("result")
            from .compiled_cache import valid_result
            if not valid_result(result):
                raise WorkerUnavailable("compiler response is missing required fields")
            if request.get("batch") is not None and (
                not isinstance(response.get("timingResults"), list)
                or not all(valid_result(value) for value in response["timingResults"])
                or not isinstance(response.get("candidateHash"), str)
            ):
                raise WorkerUnavailable("compiler response has invalid timing evidence")
            return response if request.get("batch") is not None else result
        except queue.Empty as exc:
            self.close()
            raise WorkerUnavailable("compiler worker exceeded validation deadline") from exc
        except (OSError, ValueError, WorkerUnavailable, WorkerRejected) as exc:
            self.close()
            if isinstance(exc, (WorkerUnavailable, WorkerRejected)):
                raise
            raise WorkerUnavailable(f"compiler transport failed: {exc}") from exc
        finally:
            deadline.cancel()

    def submit(self, request: dict, cancelled: Callable[[], bool]) -> Future:
        if self.closed:
            raise WorkerUnavailable("compiler worker is closed")
        try:
            self.pending += 1
            future = self.executor.submit(self._exchange, request, cancelled)
            def completed(_):
                self.pending -= 1
            future.add_done_callback(completed)
            return future
        except RuntimeError as exc:
            self.pending -= 1
            raise WorkerUnavailable("compiler worker is closing") from exc

    def close(self) -> None:
        with self._close_lock:
            if self.closed:
                return
            self.closed = True
            from .jobs import JobContext
            if self.process.poll() is None:
                try:
                    JobContext._terminate(self.process)
                except ProcessLookupError:
                    pass
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
            for stream in (self.process.stdin,):
                try:
                    stream.close()
                except OSError:
                    pass
            self.executor.shutdown(wait=False, cancel_futures=True)
            # Reader threads consume EOF without joining a compiler thread here.
            self.directory.cleanup()


class WarmCompilePool:
    """Small capability-keyed pool, owned by a workspace compiler session.

    Cancellation abandons only its request ID; the exchange continues draining
    privately until completion/deadline so another request cannot steal a reply.
    No cancellation ever kills a different document's validation.
    """

    def __init__(self, workspace, *, max_workers: int = 2, timeout: float = 180,
                 max_requests: int = 500):
        self.workspace = workspace
        self.max_workers = max(1, max_workers)
        self.timeout, self.max_requests = timeout, max_requests
        self.workers: list[_Worker] = []
        self.lock = threading.Lock()
        self.closed = False

    def compile(self, project, plan: dict, overrides: dict, renderer_revision: str,
                library_revision: str, *, request_id: str | None = None,
                cancelled: Callable[[], bool] | None = None, batch: dict | None = None) -> dict:
        cancelled = cancelled or (lambda: False)
        if cancelled():
            raise WorkerCancelled("compiler request was cancelled")
        with self.lock:
            try:
                worker = self._worker(project, renderer_revision)
            except (WorkerUnavailable, WorkerCancelled):
                raise
            except Exception as exc:
                raise WorkerUnavailable(f"cannot verify compiler boundary: {exc}") from exc
            request = {
                "version": PROTOCOL_VERSION, "requestId": request_id or str(uuid.uuid4()),
                "rendererRevision": renderer_revision, "libraryRevision": library_revision,
                "plan": plan, "overrides": overrides,
            }
            signatures = getattr(self.workspace, "library_signatures", None)
            if callable(signatures):
                request["inputSignatures"] = signatures()
            if batch is not None:
                request["batch"] = batch
            future = worker.submit(request, cancelled)
        while True:
            if cancelled() or self.closed:
                future.cancel()
                raise WorkerCancelled(f"compiler request {request['requestId']} was cancelled")
            try:
                result = future.result(timeout=0.05)
                if cancelled():
                    raise WorkerCancelled("compiler request was cancelled after validation")
                return result
            except TimeoutError:
                continue
            except CancelledError as exc:
                if cancelled() or self.closed:
                    raise WorkerCancelled("compiler request was cancelled") from exc
                raise WorkerUnavailable("compiler worker was retired") from exc
            except (WorkerUnavailable, WorkerRejected) as exc:
                if cancelled() or self.closed:
                    raise WorkerCancelled("compiler request was cancelled during worker failure") from exc
                raise

    def _worker(self, project, revision):
        """Called under the pool lock; never retire someone else's active RPC."""
        if self.closed:
            raise WorkerCancelled("compiler pool is closed")
        for item in list(self.workers):
            if item.pending == 0 and (
                item.closed or item.process.poll() is not None or item.requests >= item.max_requests
            ):
                self.workers.remove(item)
                item.close()
        worker = next((item for item in self.workers if item.matches(self.workspace, project, revision)), None)
        if worker is not None:
            return worker
        while len(self.workers) >= self.max_workers:
            idle = next((item for item in self.workers if item.pending == 0), None)
            if idle is None:
                raise WorkerUnavailable("warm compiler capacity is busy")
            self.workers.remove(idle)
            idle.close()
        try:
            worker = _Worker(self.workspace, project, revision,
                             timeout=self.timeout, max_requests=self.max_requests)
        except WorkerUnavailable:
            raise
        except Exception as exc:
            raise WorkerUnavailable(f"cannot start warm compiler: {exc}") from exc
        self.workers.append(worker)
        return worker

    def prewarm(self, project, renderer_revision: str) -> None:
        """Start imports without compiling a document or changing any authority."""
        with self.lock:
            self._worker(project, renderer_revision)

    def close(self) -> None:
        with self.lock:
            self.closed = True
            for worker in self.workers:
                worker.close()
            self.workers.clear()


_OWNERS_GUARD = threading.RLock()


class _PoolLease:
    def __init__(self, owner, *, session=False):
        self.owner, self.session, self.closed = owner, session, False

    @property
    def pool(self):
        if self.closed or self.owner.closed:
            raise WorkerCancelled("workspace compiler session is closed")
        return self.owner.pool

    def close(self):
        with _OWNERS_GUARD:
            if self.closed:
                return
            self.closed = True
            if self.session:
                self.owner.sessions -= 1
                # Explicit broker shutdown cancels all requests on this owner.
                retire = self.owner.sessions == 0
            else:
                self.owner.documents -= 1
                retire = self.owner.sessions == 0 and self.owner.documents == 0
            if retire:
                self.owner.closed = True
        if retire:
            self.owner.pool.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _PoolOwner:
    def __init__(self, workspace):
        self.pool = WarmCompilePool(workspace)
        self.sessions = self.documents = 0
        self.closed = False


def _lease(workspace, *, session):
    with _OWNERS_GUARD:
        owner = getattr(workspace, "_editor_compile_owner", None)
        if owner is None or owner.closed:
            owner = _PoolOwner(workspace)
            workspace._editor_compile_owner = owner
        if session:
            owner.sessions += 1
        else:
            owner.documents += 1
        return _PoolLease(owner, session=session)


def begin_compile_session(workspace):
    """Pin one bounded pool across document opens; close at broker shutdown.

    Old documents retain their original owner generation. Restarting a service
    on the same Workspace cannot reconnect old work to its new pool.
    """
    return _lease(workspace, session=True)


def acquire_document_compiler(workspace):
    """Standalone stores share while overlapping, but leave no idle subprocesses."""
    return _lease(workspace, session=False)
