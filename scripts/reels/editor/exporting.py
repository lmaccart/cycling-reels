"""Explicit native Save actions; AI tools have no access to this operation."""
from __future__ import annotations

import ctypes
import errno
import os
import shutil
import tempfile
from pathlib import Path

from .http import identity
from .protocol import ProtocolError


def save_artifact(workspace, receipt: dict, destination: str, *, artifact="reel", overwrite=False) -> dict:
    field = {"reel": "reelPath", "review": "previewVideoPath"}.get(artifact)
    if field is None:
        raise ProtocolError("invalid_artifact", "Choose the reel or audible review artifact.")
    source_value = receipt.get(field) or (receipt.get("reelPath") if artifact == "review" else None)
    if not isinstance(source_value, str):
        raise ProtocolError("artifact_unavailable", "That completed export has no such artifact.")
    source = Path(source_value)
    if source.is_symlink() or not source.is_file():
        raise ProtocolError("artifact_unavailable", "The completed export is unavailable.")
    source = source.resolve(strict=True)
    if not source.is_relative_to(Path(workspace.root).resolve()):
        raise ProtocolError("invalid_artifact", "Export source is outside this private workspace.")
    destination_path = Path(destination).expanduser()
    if not destination_path.is_absolute() or destination_path.suffix.lower() not in (".mp4", ".mov"):
        raise ProtocolError("invalid_destination", "Choose an absolute MP4 or MOV save destination.")
    if destination_path.is_symlink():
        raise ProtocolError("invalid_destination", "Do not replace a symlink with an export.")
    parent = destination_path.parent.resolve(strict=True)
    target = parent / destination_path.name
    production = Path(workspace.project.media_dir).resolve().parent.parent
    if target.is_relative_to(production):
        raise ProtocolError("production_write_denied", "Exports cannot be saved into the production checkout.")
    if target == source:
        raise ProtocolError("same_artifact", "Choose a different destination for the finished artifact.")
    if target.exists() and not overwrite:
        raise ProtocolError("destination_exists", "Confirm replacement in the native save dialog first.")
    recorded = (receipt.get("artifactIdentities") or {}).get(field)
    observed = identity(source)
    if recorded is not None and tuple(recorded) != observed:
        raise ProtocolError("artifact_changed", "The export changed after completion; it was not copied.")
    fd, temp_name = tempfile.mkstemp(prefix=".reels-save-", suffix=".tmp", dir=parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as dst:
            read_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(read_fd, "rb") as src:
                opened = os.fstat(src.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != observed:
                    raise ProtocolError("artifact_changed", "The selected export was replaced before copying.")
                shutil.copyfileobj(src, dst, length=1024 * 1024)
                dst.flush()
                os.fsync(dst.fileno())
                after = os.fstat(src.fileno())
                if (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != observed:
                    raise ProtocolError("artifact_changed", "The selected export changed while copying.")
        if identity(source) != observed or temporary.stat().st_size != observed[2]:
            raise ProtocolError("artifact_changed", "The selected export changed while copying.")
        if overwrite:
            os.replace(temporary, target)
        elif os.uname().sysname == "Darwin":
            libc = ctypes.CDLL(None, use_errno=True)
            rename = libc.renamex_np
            rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            if rename(os.fsencode(temporary), os.fsencode(target), 0x00000004) != 0:
                code = ctypes.get_errno()
                if code == errno.EEXIST:
                    raise ProtocolError("destination_exists", "Destination appeared while saving; no file was replaced.")
                raise OSError(code, os.strerror(code))
        else:
            # Atomic no-replace publication on test platforms; both links are
            # only to this newly copied temporary, never to shared source data.
            os.link(temporary, target)
            temporary.unlink()
        return {"path": str(target), "revisions": receipt.get("revisions", {}), "artifact": artifact}
    finally:
        temporary.unlink(missing_ok=True)
