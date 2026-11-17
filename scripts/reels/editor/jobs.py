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
        self.on_log: Callable[[str], None] | None = None
        self.cancel = threading.Event()
        self.timed_out = False
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

    def expire(self) -> None:
        """The job ran past its deadline: stop it, and report it as timed out rather than cancelled."""
        self.timed_out = True
        self.stop()

    def log(self, line: str) -> None:
        if self.on_log is not None and line:
            self.on_log(line)

    def stream(self, args: list[str], *, cwd: str | Path, env: dict | None = None, timeout: float = 6 * 3600) -> int:
        """Run a command, forwarding each stdout/stderr line to the job log. Cancelling kills its process group."""
        self.check_cancelled()
        process = subprocess.Popen([str(a) for a in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                   errors="replace", start_new_session=True)
        self.own(process)
        tail: list[str] = []
        timer = threading.Timer(timeout, self._terminate, args=(process,))
        timer.daemon = True
        timer.start()
        try:
            for raw in process.stdout:
                line = raw.rstrip()
                if line:
                    tail = (tail + [line])[-40:]
                    self.log(line)
            process.wait()
            self.check_cancelled()
            if process.returncode:
                raise ProtocolError("command_failed", f"Command exited with status {process.returncode}.",
                                    {"log": "\n".join(tail)})
            return process.returncode
        finally:
            timer.cancel()
            self.disown(process)
            process.stdout.close()

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
               publish: Callable[[Any], Any] | None = None, *, independent: bool = False,
               key: str | None = None, timeout: float | None = None,
               on_error: Callable[[dict], Any] | None = None) -> dict:
        """independent jobs (footage ingest, source and 360 proxies) are not tied to the open document's revisions.

        A keyed request joins the queued or running job of the same kind and key instead of starting another.
        timeout (seconds, counted from the start of the work) ends the job as "timed_out".
        on_error gets the final record of a job that ends unpublished: failed, cancelled, timed out or superseded."""
        revisions = dict(revisions)
        with self.lock:
            if key is not None:
                for live, live_context in self.contexts.items():
                    existing = self.jobs[live]
                    if (existing["kind"] == kind and existing.get("key") == key
                            and existing["state"] in ("queued", "running") and not live_context.cancel.is_set()):
                        return {**existing, "joined": True}
            jid = uuid.uuid4().hex
            record = {"id": jid, "kind": kind, "revisions": dict(revisions), "state": "queued", "created": time.time()}
            if key is not None:
                record["key"] = key
            if timeout is not None:
                record["timeoutSeconds"] = timeout
            context = JobContext()
            self.jobs[jid], self.contexts[jid] = record, context
            self._save(record)

        def on_log(line: str) -> None:
            record["log"] = line
            self.emit("job.changed", dict(record), revisions)
        context.on_log = on_log

        def perform():
            deadline = None
            try:
                with self.lock:
                    context.check_cancelled()
                    record.update(state="running", started=time.time())
                    self._save(record)
                if timeout is not None:
                    deadline = threading.Timer(timeout, context.expire)
                    deadline.daemon = True
                    deadline.start()
                self.emit("job.changed", dict(record), revisions)
                result = work(context)
                context.check_cancelled()
                if isinstance(result, dict):
                    result = {**result, "jobId": jid}
                with self.lock:
                    # Recheck immediately before publication, not just before work.
                    stale = (not independent) and dict(revisions) != self.current()
                    if not stale and publish is not None:
                        result = publish(result)
                    record.update(state="superseded" if stale else "completed", result=result, finished=time.time())
                    self._save(record)
            except Exception as exc:
                error = error_value(exc)
                if context.timed_out:
                    state, error = "timed_out", {"code": "timed_out", "message": f"The job took longer than {timeout:g} seconds."}
                elif timeout is not None and error.get("code") == "worker_timeout":
                    state = "timed_out"
                else:
                    state = "cancelled" if context.cancel.is_set() else "failed"
                with self.lock:
                    record.update(state=state, error=error, finished=time.time())
                    self._save(record)
            finally:
                if deadline is not None:
                    deadline.cancel()
                with self.lock:
                    # No longer joinable before anyone hears that it ended.
                    self.contexts.pop(jid, None)
                    self.futures.pop(jid, None)
                self.emit("job.changed", dict(record), revisions)
                if kind == "export" and record.get("state") == "completed":
                    self.emit("export.ready", record.get("result", {}), revisions)
                if on_error is not None and record.get("state") != "completed":
                    try:
                        on_error(dict(record))
                    except Exception:  # noqa: BLE001 - the job record already holds the outcome
                        pass

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
