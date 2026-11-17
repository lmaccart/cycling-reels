"""Read-only capability HTTP, serving registered immutable files, never arbitrary paths."""
from __future__ import annotations

import mimetypes
import os
import re
import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import quote, unquote, urlsplit

from .protocol import ProtocolError


def identity(path: Path) -> tuple[int, int, int, int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@dataclass(frozen=True)
class RegisteredFile:
    path: Path
    identity: tuple


class CapabilityServer:
    def __init__(self, workspace=None):
        self.workspace = workspace
        self.capability = secrets.token_urlsafe(24)
        self.prefix = f"/cap/{self.capability}/"
        self.files: dict[str, RegisteredFile] = {}
        self.lock = threading.RLock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_HEAD(self):
                self._serve(False)

            def do_GET(self):
                self._serve(True)

            def do_POST(self):
                self.send_error(405)
                self.close_connection = True

            do_PUT = do_PATCH = do_DELETE = do_POST

            def _serve(self, body):
                raw = urlsplit(self.path).path
                decoded = unquote(raw)
                if not decoded.startswith(owner.prefix):
                    self.send_error(404)
                    return
                key = decoded[len(owner.prefix):]
                if not owner.valid_key(key):
                    self.send_error(404)
                    return
                with owner.lock:
                    item = owner.files.get(key)
                if item is None:
                    self.send_error(404)
                    return
                try:
                    # Open without following a last-component replacement symlink.
                    fd = os.open(item.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                    stream = os.fdopen(fd, "rb")
                    stat = os.fstat(stream.fileno())
                    observed = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                    if observed != item.identity or identity(item.path) != item.identity:
                        stream.close()
                        self.send_error(409, "Registered media is offline or changed")
                        return
                except OSError:
                    self.send_error(410, "Registered media is offline")
                    return
                with stream:
                    size = stat.st_size
                    start, end, partial = 0, size - 1, False
                    request_range = self.headers.get("Range")
                    if request_range:
                        match = re.fullmatch(r"bytes=(\d*)-(\d*)", request_range.strip())
                        if not match or not any(match.groups()):
                            self._bad_range(size)
                            return
                        left, right = match.groups()
                        if not left:
                            length = int(right)
                            if length <= 0:
                                self._bad_range(size)
                                return
                            start = max(0, size - length)
                        else:
                            start = int(left)
                            end = min(end, int(right)) if right else end
                        if start >= size or start > end:
                            self._bad_range(size)
                            return
                        partial = True
                    self.send_response(206 if partial else 200)
                    self.send_header("Content-Type", mimetypes.guess_type(item.path.name)[0] or "application/octet-stream")
                    self.send_header("Content-Length", str(max(0, end - start + 1)))
                    self.send_header("Accept-Ranges", "bytes")
                    if partial:
                        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
                    self.send_header("Cache-Control", "private, no-store")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Content-Security-Policy", "default-src 'self' blob:; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self' blob:; frame-ancestors 'none'")
                    self.end_headers()
                    if body:
                        stream.seek(start)
                        remaining = end - start + 1
                        try:
                            while remaining > 0:
                                chunk = stream.read(min(256 * 1024, remaining))
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                                remaining -= len(chunk)
                        except (ConnectionError, OSError):
                            pass

            def _bad_range(self, size):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, name="reels-preview-http", daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def valid_key(key: str) -> bool:
        return bool(key and not key.startswith("/") and "\\" not in key and "\x00" not in key
                    and all(part not in ("", ".", "..") for part in key.split("/")))

    def register_file(self, key: str, path: str | Path, identity=None) -> str:
        if not self.valid_key(key):
            raise ProtocolError("invalid_media_key", "Invalid registered media name.")
        path = Path(path)
        if path.is_symlink():
            raise ProtocolError("symlink_media", "Media registration cannot follow a symlink.")
        resolved = path.resolve(strict=True)
        if self.workspace is not None:
            root = Path(self.workspace.root).resolve()
            if not resolved.is_relative_to(root):
                originals = Path(self.workspace.project.media_dir).resolve()
                project = self.workspace.project
                static = [Path(project.thumbs_dir).resolve(), Path(project.sheets_dir).resolve(),
                          Path(project.out_dir).resolve()]
                if any(resolved.is_relative_to(base) for base in static):
                    pass  # thumbnails, contact sheets and rendered covers: read-only images for the plan picker
                elif not resolved.is_relative_to(originals):
                    raise ProtocolError("unregistered_media", "Media is outside this workspace and its registered originals.")
                else:
                    if hasattr(self.workspace, "sync_originals"):
                        self.workspace.sync_originals()
                    registered = getattr(self.workspace, "_originals", None) or {}
                    if not any(Path(record["path"]).resolve() == resolved for record in registered.values()):
                        raise ProtocolError("unregistered_media", "Original media is not registered.")
        observed = globals()["identity"](resolved)
        if identity is not None and tuple(identity) != observed:
            raise ProtocolError("offline_media", "Original identity changed.")
        with self.lock:
            previous = self.files.get(key)
            if previous is not None and previous != RegisteredFile(resolved, observed):
                raise ProtocolError("immutable_media", "A registered revision URL cannot be replaced.")
            self.files[key] = RegisteredFile(resolved, observed)
        return self.url(key)

    def register_tree(self, prefix: str, directory: str | Path) -> str:
        directory = Path(directory)
        if directory.is_symlink():
            raise ProtocolError("symlink_media", "Bundle directory cannot be a symlink.")
        directory = directory.resolve(strict=True)
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ProtocolError("symlink_media", "Bundle contains a symlink.")
            if path.is_file():
                self.register_file(f"{prefix}/{path.relative_to(directory).as_posix()}", path)
        return self.url(prefix + "/index.html")

    def retire(self, prefix: str) -> None:
        """Forget every file registered at `prefix` or below it (a superseded preview): later requests get 404."""
        with self.lock:
            for key in [key for key in self.files if key == prefix or key.startswith(prefix + "/")]:
                del self.files[key]

    def url(self, key: str) -> str:
        return self.origin + self.prefix + quote(key, safe="/")

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
