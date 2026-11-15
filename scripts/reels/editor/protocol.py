"""Versioned transport shared by the native app and the private MCP relay."""
from __future__ import annotations

import dataclasses
import json
import math
import errno
import threading
from pathlib import Path
from typing import Any, Callable, TextIO

from ..util import ReelsError

VERSION = 1
MAX_MESSAGE = 8 * 1024 * 1024


class ProtocolError(ReelsError):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self) -> dict:
        result = {"code": self.code, "message": self.message}
        if self.details is not None:
            result["details"] = self.details
        return result


def json_default(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, (set, tuple)):
        return list(value)
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=json_default, separators=(",", ":"))


def decode(line: str | bytes) -> dict:
    if len(line) > MAX_MESSAGE:
        raise ProtocolError("message_too_large", "Request exceeds the 8 MiB protocol limit.")
    try:
        result = json.loads(line, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ProtocolError("invalid_json", "Request must be finite UTF-8 JSON.") from exc
    if not isinstance(result, dict):
        raise ProtocolError("invalid_request", "A request must be a JSON object.")
    return result


def validate_request(request: dict) -> tuple[Any, str, dict]:
    if request.get("version") != VERSION:
        raise ProtocolError("protocol_version", f"Expected protocol version {VERSION}.")
    rid = request.get("id")
    if not isinstance(rid, (str, int)) or isinstance(rid, bool):
        raise ProtocolError("invalid_request", "A string or integer request ID is required.")
    method, params = request.get("method"), request.get("params", {})
    if not isinstance(method, str) or not method or not isinstance(params, dict):
        raise ProtocolError("invalid_request", "Expected a method name and parameters object.")
    return rid, method, params


def error_value(exc: Exception) -> dict:
    if isinstance(exc, ProtocolError):
        return exc.as_dict()
    if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
        return {"code": "disk_full", "message": "Not enough free disk space. The last committed project remains available."}
    code = getattr(exc, "code", None)
    details = getattr(exc, "details", None)
    if hasattr(exc, "as_dict"):
        candidate = exc.as_dict()
        if isinstance(candidate, dict) and "message" in candidate:
            return candidate
    # Expected document/workspace failures stay structured, not learning events.
    return {"code": str(code or "validation_error"), "message": str(exc), **({"details": details} if details is not None else {})}


def respond(request: dict, handler: Callable[[str, dict], Any]) -> dict:
    rid = request.get("id")
    try:
        rid, method, params = validate_request(request)
        return {"version": VERSION, "id": rid, "result": handler(method, params)}
    except Exception as exc:
        return {"version": VERSION, "id": rid, "error": error_value(exc)}


class Writer:
    """One lock prevents interleaved responses/events from concurrent jobs."""
    def __init__(self, stream: TextIO):
        self.stream = stream
        self.lock = threading.RLock()
        self.sequence = 0
        self.closed = False

    def send(self, message: dict) -> None:
        text = encode(message)
        with self.lock:
            if not self.closed:
                try:
                    self.stream.write(text + "\n")
                    self.stream.flush()
                except (BrokenPipeError, OSError):
                    self.closed = True

    def event(self, name: str, payload: Any, revisions: dict | None = None, **identity: Any) -> None:
        with self.lock:
            self.sequence += 1
            self.send({"version": VERSION, "event": name, "sequence": self.sequence,
                       "revisions": revisions or {}, "payload": payload, **identity})
