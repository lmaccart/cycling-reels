"""Owned, cancellable work. Results are publishable only at their input revision."""
from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .protocol import ProtocolError, error_value


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temp.unlink(missing_ok=True)


class JobContext:
    def __init__(self):
        self.cancel = threading.Event()
        self._processes: set[subprocess.Popen] = set()
        self._lock = threading.RLock()

    def check_cancelled(self) -> None:
        if self.cancel.is_set():
            raise ProtocolError("cancelled", "The operation was cancelled.")

    def own(self, process: subprocess.Popen) -> None:
        with self._lock:
            self._processes.add(process)
            if self.cancel.is_set():
                self._terminate(process)

    def disown(self, process: subprocess.Popen) -> None:
        with self._lock:
            # A completed launcher may still have renderer/MCP descendants.
            # It started a private session, so its process group is ours too.
            self._terminate(process)
            self._processes.discard(process)

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        # Do not check only the launcher: it may already have exited while a
        # grandchild still owns its pipes or is ignoring SIGTERM.
        timer = threading.Timer(2.0, JobContext._kill, args=(process,))
        timer.daemon = True
        timer.start()

    @staticmethod
    def _kill(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    def stop(self) -> None:
        self.cancel.set()
        with self._lock:
            for process in tuple(self._processes):
                self._terminate(process)

    def run(self, args: list[str], *, cwd: str | Path, env: dict | None = None,
            timeout: float = 600, input: str | None = None) -> subprocess.CompletedProcess:
        self.check_cancelled()
        process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env,
                                   stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        self.own(process)
        try:
            stdout, stderr = process.communicate(input, timeout=timeout)
            self.check_cancelled()
            if process.returncode:
                raise ProtocolError("worker_failed", f"Worker exited with status {process.returncode}.",
                                    {"stderr": stderr[-12000:], "stdout": stdout[-2000:]})
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired as exc:
            self._terminate(process)
            process.communicate()
            raise ProtocolError("worker_timeout", f"Worker exceeded {timeout:g} seconds.") from exc
        finally:
            self.disown(process)


class JobManager:
    def __init__(self, root: Path, emit: Callable, current: Callable[[], dict], max_workers: int = 2):
        self.root, self.emit, self.current = Path(root), emit, current
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="reels-job")
        self.lock = threading.RLock()
        self.jobs: dict[str, dict] = {}
        self.contexts: dict[str, JobContext] = {}
        self.futures: dict[str, Any] = {}
        # Interrupted workers are not completed exports and never resume implicitly.
        for path in self.root.glob("*.json"):
            try:
                record = json.loads(path.read_text())
                if record.get("state") in ("queued", "running"):
                    record.update(state="interrupted", error={"code": "interrupted", "message": "Service stopped before completion."})
                    atomic_json(path, record)
                self.jobs[record["id"]] = record
            except (ValueError, KeyError, OSError):
                continue

    def _save(self, job: dict) -> None:
        atomic_json(self.root / f"{job['id']}.json", job)

    def submit(self, kind: str, revisions: dict, work: Callable[[JobContext], Any],
               publish: Callable[[Any], Any] | None = None) -> dict:
        revisions = dict(revisions)
        jid = uuid.uuid4().hex
        record = {"id": jid, "kind": kind, "revisions": dict(revisions), "state": "queued", "created": time.time()}
        context = JobContext()
        with self.lock:
            self.jobs[jid], self.contexts[jid] = record, context
            self._save(record)

        def perform():
            try:
                with self.lock:
                    context.check_cancelled()
                    record.update(state="running", started=time.time())
                    self._save(record)
                self.emit("job.changed", dict(record), revisions)
                result = work(context)
                context.check_cancelled()
                if isinstance(result, dict):
                    result = {**result, "jobId": jid}
                with self.lock:
                    # Recheck immediately before publication, not just before work.
                    stale = dict(revisions) != self.current()
                    if not stale and publish is not None:
                        result = publish(result)
                    record.update(state="superseded" if stale else "completed", result=result, finished=time.time())
                    self._save(record)
            except Exception as exc:
                with self.lock:
                    record.update(state="cancelled" if context.cancel.is_set() else "failed",
                                  error=error_value(exc), finished=time.time())
                    self._save(record)
            finally:
                self.emit("job.changed", dict(record), revisions)
                if kind == "export" and record.get("state") == "completed":
                    self.emit("export.ready", record.get("result", {}), revisions)
                with self.lock:
                    self.contexts.pop(jid, None)
                    self.futures.pop(jid, None)

        with self.lock:
            self.futures[jid] = self.pool.submit(perform)
        return dict(record)

    def status(self, jid: str) -> dict:
        with self.lock:
            if jid not in self.jobs:
                raise ProtocolError("job_not_found", "No such editor job.")
            return dict(self.jobs[jid])

    def cancel_job(self, jid: str) -> dict:
        with self.lock:
            self.status(jid)
            if jid in self.contexts:
                self.contexts[jid].stop()
            return dict(self.jobs[jid])

    def cancel_kind(self, *kinds: str) -> None:
        with self.lock:
            for jid, context in tuple(self.contexts.items()):
                if self.jobs[jid]["kind"] in kinds:
                    context.stop()

    def close(self) -> None:
        with self.lock:
            for context in tuple(self.contexts.values()):
                context.stop()
        self.pool.shutdown(wait=True, cancel_futures=False)
