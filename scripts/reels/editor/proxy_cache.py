"""Cross-process coordination shared by CLI prewarm and sandboxed editor workers."""
from __future__ import annotations

import fcntl
import os
import shutil
import uuid
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from functools import wraps
from pathlib import Path

from .proxy import proxy_reservation
from ..util import ReelsError

MIN_FREE_BYTES = 50_000_000_000
_encoder_fds: ContextVar[tuple[int, ...]] = ContextVar("proxy_encoder_fds", default=())


class ProxyDiskFull(ReelsError):
    pass


def _open_lock(path: Path):
    # Inherit only explicitly selected locks via pass_fds, never every open file.
    return os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600), "r+")


def encoder_fds() -> tuple[int, ...]:
    """Key locks and output leases the encoder must retain if Python dies."""
    return _encoder_fds.get()


@contextmanager
def _encoder_lock(stream):
    token = _encoder_fds.set((*_encoder_fds.get(), stream.fileno()))
    try:
        yield
    finally:
        _encoder_fds.reset(token)


@contextmanager
def cache_lock(root: Path, *, blocking: bool = True):
    """Lock one proxy key, not the editor's in-process job queue. Never unlink locks."""
    if root.is_symlink():
        raise ReelsError("proxy cache directory cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _open_lock(root / ".proxy.lock") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
        else:
            # Closing our descriptor releases only our reference. LOCK_UN would
            # also unlock a surviving encoder's inherited open-file description.
            with _encoder_lock(stream):
                yield True


def budget_root(workspace: Path) -> Path:
    root = workspace / "preview/proxy-reservations"
    if root.is_symlink() or not root.resolve().is_relative_to(workspace.resolve()):
        raise ReelsError("proxy reservations escaped the workspace")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    return root


@contextmanager
def reserve_disk(root: Path, amount: int, free_disk=None):
    """Atomically account for all in-flight proxy outputs, across processes.

    Live reservation files stay flocked by Python and its encoder; abandoned ones
    are reclaimed only after both exit, including after SIGKILL. Counting written
    bytes again is conservative. Unrelated disk writers are outside this budget.
    """
    free_disk = free_disk or (lambda path: shutil.disk_usage(path).free)
    lease_path = root / f"{uuid.uuid4().hex}.reserve"
    lease = None
    try:
        with cache_lock(root):
            reserved = 0
            for path in root.glob("*.reserve"):
                with _open_lock(path) as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        reserved += int(stream.read())
                    else:
                        path.unlink()  # abandoned lease, never media
            if free_disk(root) - reserved - amount < MIN_FREE_BYTES:
                raise ProxyDiskFull("keeping 50 GB free after active proxy reservations")
            lease = _open_lock(lease_path)
            fcntl.flock(lease, fcntl.LOCK_EX)
            lease.write(str(amount))
            lease.flush()
        with _encoder_lock(lease):
            yield
    finally:
        if lease is not None:
            lease.close()
            with cache_lock(root):
                # SIGINT can unwind this scope while an encoder is still alive.
                # Keep its lease until the last inherited descriptor is closed.
                with _open_lock(lease_path) as stream:
                    try:
                        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        lease_path.unlink(missing_ok=True)


def proxy_job(work):
    """Serialize lookup/encode/publication for a key, while other keys can run."""
    @wraps(work)
    def locked(job: dict):
        workspace = Path(job["workspaceRoot"]).resolve(strict=True)
        root = Path(job["revisionDir"])
        if root.is_symlink() or not root.resolve().is_relative_to(workspace):
            raise ReelsError("proxy output escaped the private editor workspace")
        with cache_lock(root), ExitStack() as reservations:
            reserved = False

            def reserve():
                nonlocal reserved
                if not reserved:
                    seconds = (float(job["end"]) - float(job["start"]) if "end" in job
                               else float((job.get("probe") or {}).get("duration") or 0))
                    reservations.enter_context(reserve_disk(
                        budget_root(workspace), proxy_reservation(seconds), job.get("freeDisk")))
                    reserved = True

            # Cache hits don't consume a reservation or stop playback on a full disk.
            # A miss retains its reservation until media, marker and CORI publish.
            return work({**job, "reserveOutput": reserve})
    return locked
