"""Permission-restricted Unix-socket relay to the *same* mutation dispatcher."""
from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import socketserver
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Callable

from .protocol import MAX_MESSAGE, ProtocolError, decode, encode, respond


class Relay:
    def __init__(self, dispatch: Callable):
        # The approved session temp path is too long for Darwin's 104-byte sun_path.
        self.root = Path(tempfile.mkdtemp(prefix=f"re-{os.getuid()}-", dir="/private/tmp"))
        os.chmod(self.root, 0o700)
        self.path = self.root / "s"
        self.tokens = {scope: secrets.token_urlsafe(32) for scope in ("editing", "search")}
        self.token_files: dict[str, Path] = {}
        for scope, token in self.tokens.items():
            file = self.root / f"{scope}.token"
            file.write_text(token)
            os.chmod(file, 0o600)
            self.token_files[scope] = file
        owner = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(300)
                try:
                    line = self.rfile.readline(MAX_MESSAGE + 1)
                    request = decode(line)
                    token = request.pop("capability", "")
                    scope = next((name for name, valid in owner.tokens.items()
                                  if isinstance(token, str) and hmac.compare_digest(token, valid)), None)
                    if scope is None:
                        raise ProtocolError("unauthorized", "Invalid editor relay capability.")
                    result = respond(request, lambda method, params: dispatch(method, params, scope))
                except Exception as exc:
                    from .protocol import error_value
                    result = {"version": 1, "id": None, "error": error_value(exc)}
                self.wfile.write((encode(result) + "\n").encode())

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True

        self.server = Server(str(self.path), Handler)
        os.chmod(self.path, 0o600)
        self.thread = threading.Thread(target=self.server.serve_forever, name="reels-mcp-relay", daemon=True)
        self.thread.start()

    def config(self, scope: str, workspace) -> Path:
        path = self.root / f"{scope}-mcp.json"
        python = Path(workspace.code_root) / ".venv/bin/python"
        payload = {"mcpServers": {"reels_editor": {
            "command": str(python), "args": ["-m", "reels", "--editor-workspace", str(workspace.root),
                                             "editor", "mcp", "--socket", str(self.path),
                                             "--token-file", str(self.token_files[scope]), "--scope", scope],
            "env": {"PYTHONPATH": str(Path(workspace.code_root) / "scripts"),
                    "REELS_HOME": str(workspace.data_root)},
        }}}
        path.write_text(json.dumps(payload))
        os.chmod(path, 0o600)
        return path

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        # Only remove this launch's known capability/config files.
        for scope in self.tokens:
            self.token_files[scope].unlink(missing_ok=True)
            (self.root / f"{scope}-mcp.json").unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)
        self.root.rmdir()


def call_relay(path: str | Path, token_file: str | Path, method: str, params: dict | None = None) -> dict:
    token_file = Path(token_file)
    stat = token_file.stat()
    if stat.st_uid != os.getuid() or stat.st_mode & 0o077 or token_file.is_symlink():
        raise ProtocolError("unsafe_capability", "Relay capability file must be private and owned by this user.")
    request = {"version": 1, "id": uuid.uuid4().hex, "capability": token_file.read_text().strip(),
               "method": method, "params": params or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(300)
        stream.connect(str(path))
        stream.sendall((encode(request) + "\n").encode())
        with stream.makefile("rb") as reader:
            result = decode(reader.readline(MAX_MESSAGE + 1))
    if "error" in result:
        error = result["error"]
        raise ProtocolError(error.get("code", "relay_error"), error.get("message", "Editor tool failed."), error.get("details"))
    return result["result"]
