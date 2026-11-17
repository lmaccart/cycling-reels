"""Trusted local broker. Every native or AI mutation uses this dispatcher."""
from __future__ import annotations

import contextlib
import copy
import inspect
import json
import math
import os
import selectors
import signal
import sys
import threading
import time
import uuid
import hashlib
import fcntl
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .claude import ClaudeCLI
from .delta import snapshot_delta as _snapshot_delta, snapshot_token
from .http import CapabilityServer
from . import live, music_service
from .jobs import JobManager, atomic_json
from .protocol import MAX_MESSAGE, ProtocolError, Writer, decode, error_value, respond
from .relay import Relay


class EditorService:
    def __init__(self, workspace, emit):
        self.workspace, self.project, self.emit = workspace, workspace.project, emit
        self.music_search = music_service.BackgroundSearch(self.project, lambda *a: self.emit(*a))
        lock_path = Path(workspace.root) / "service.lock"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            info = os.fstat(fd)
            if info.st_nlink != 1 or info.st_uid != os.getuid():
                raise ProtocolError("unsafe_workspace_lock", "Workspace lock is not a private owned file.")
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise ProtocolError("workspace_in_use", "This private workspace already has an editing service. Reuse its app window.") from exc
        except Exception:
            os.close(fd)
            raise
        try:
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            os.write(fd, json.dumps({"pid": os.getpid(), "workspace": str(workspace.root)}).encode())
        except Exception:
            os.close(fd)
            raise
        self._workspace_lock_fd = fd
        self._service_closed = False
        self.document = None
        self.document_path: str | None = None
        self.lock = threading.RLock()
        self._preview_start_lock = threading.Lock()
        self.shutdown_requested = threading.Event()
        self.selection = {"ids": [], "playhead": 0}
        self.preview: dict = {"state": "empty"}
        self.permissions: dict[str, dict] = {}
        permission_path = Path(workspace.root) / "chat" / "permissions.json"
        if permission_path.is_file() and not permission_path.is_symlink():
            try:
                value = json.loads(permission_path.read_text())
                if isinstance(value, dict):
                    self.permissions = value
            except (OSError, ValueError):
                pass
        self._ai_turn: dict | None = None
        self._manual_during_ai: set[str] = set()
        self._index = None
        self._search = None
        self._preview_manager = None
        self._renderer_manager = None
        self._preview_timer: threading.Timer | None = None
        self._preview_sequence = 0
        self._announced_document_state: dict | None = None
        self._announced_snapshot: dict | None = None
        self._observed_library_revision: str | None = None
        self._timeline_leases: dict[str, float] = {}
        self._manual_apply_inflight = 0
        self.watcher: threading.Thread | None = None
        self._plans_stamp: dict | None = live.plans_stamp(self.project)
        self._ingest_job: str | None = None
        self.state_path = Path(workspace.root) / "state.json"
        try:
            self.state = json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            self.state = {}
        try:
            from .compile_worker import begin_compile_session

            self._compile_session = begin_compile_session(workspace)
            self.http = CapabilityServer(workspace)
            self.relay = Relay(self._relay_dispatch)
            self.claude = ClaudeCLI(workspace, self.relay, emit)
            self.jobs = JobManager(Path(workspace.root) / "jobs", self._job_event, self.current_revisions)
            remove_partial_proxies(Path(workspace.root))
            start_library_watcher = getattr(self.workspace, "start_library_watcher", None)
            if callable(start_library_watcher):
                try:
                    start_library_watcher()
                except Exception:  # noqa: BLE001 - fresh synchronous checks remain available
                    pass
            try:
                self._observed_library_revision = str(self.workspace.library_revision())
            except Exception:
                self._observed_library_revision = None
            self.watcher = self._new_service_watcher()
            self.watcher.start()
        except Exception:
            self.shutdown_requested.set()
            try:
                if self.watcher is not None and self.watcher.is_alive():
                    self.watcher.join(timeout=2)
                stop_library_watcher = getattr(self.workspace, "stop_library_watcher", None)
                if callable(stop_library_watcher):
                    try:
                        stop_library_watcher()
                    except Exception:
                        pass
                if self.document is not None and hasattr(self.document, "close"):
                    try:
                        self.document.close()
                    except Exception:
                        pass
                for name in ("jobs", "claude", "relay", "http"):
                    resource = getattr(self, name, None)
                    if resource is not None:
                        try:
                            resource.close()
                        except Exception:
                            pass
            finally:
                try:
                    compile_session = getattr(self, "_compile_session", None)
                    if compile_session is not None:
                        compile_session.close()
                finally:
                    os.close(self._workspace_lock_fd)
            raise

    def _new_service_watcher(self) -> threading.Thread:
        return threading.Thread(target=self._watch, name="reels-private-file-watcher", daemon=True)

    def current_revisions(self) -> dict:
        document = self.document
        if document is None:
            return {}
        try:
            return dict(document.snapshot().get("revisions", {}))
        except Exception:
            if self.shutdown_requested.is_set():
                return {}
            raise

    def _doc(self):
        if self.document is None:
            raise ProtocolError("document_required", "Open a private reel plan first.")
        return self.document

    @staticmethod
    def _operation_targets(operations) -> set[str]:
        targets = set()
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            name = operation.get("op", "")
            if name in ("insert_segment", "remove_segments", "reorder_segments", "split_segment",
                        "duplicate_segments", "ripple_remove", "paste_segments", "set_enabled"):
                targets.add("spine")
            for key in ("id", "parentId", "assetId"):
                if operation.get(key):
                    targets.add(str(operation[key]))
            targets.update(str(item) for item in (operation.get("ids") or []) if isinstance(item, str))
            if name == "set_field" and operation.get("id", "document") == "document":
                targets.add("document:" + str(operation.get("path", "")).lstrip("/").split("/", 1)[0])
        return targets or {"document"}

    def _guard_ai(self, params, *, renderer=False):
        if self._ai_turn is None:
            return
        current = self._doc().snapshot()
        if self._ai_turn.get("documentId") != current.get("documentId"):
            raise ProtocolError("manual_conflict", "The person opened a different document. Wait for a new editing request.")
        targets = self._operation_targets(params.get("operations", []))
        conflicts = self._manual_during_ai & targets
        if renderer and self._manual_during_ai:
            conflicts = set(self._manual_during_ai)
        if "document" in self._manual_during_ai:
            conflicts = set(self._manual_during_ai)
        if conflicts:
            raise ProtocolError("manual_conflict", "Manual edits made during this turn take precedence. Do not overwrite them.",
                                {"affectedIds": sorted(conflicts), "current": current.get("revisions")})

    def snapshot(self):
        result = dict(self._doc().snapshot())
        result["preview"] = dict(self.preview)
        result["selection"] = dict(self.selection)
        result["permissions"] = copy.deepcopy(list(self.permissions.values()))
        result.pop("snapshotToken", None)
        result["snapshotToken"] = snapshot_token(result)
        with self.lock:
            self._announced_snapshot = copy.deepcopy(result)
        return result

    def _project_config(self):
        from ..config import DEFAULTS, deep_merge
        path = self.project.root / "config.json"
        value = json.loads(path.read_text()) if path.is_file() else {}
        return deep_merge(copy.deepcopy(DEFAULTS), value)

    def _changed(self, snapshot: dict, *, prepare_preview=True, delta_base=None,
                 delta_snapshot_token=None):
        snapshot = {
            **snapshot,
            "preview": dict(self.preview),
            "selection": dict(self.selection),
            "permissions": copy.deepcopy(list(self.permissions.values())),
        }
        snapshot.pop("snapshotToken", None)
        snapshot["snapshotToken"] = snapshot_token(snapshot)
        with self.lock:
            previous = self._announced_snapshot
            delta = (
                _snapshot_delta(previous, snapshot, delta_base, delta_snapshot_token)
                if delta_base is not None and delta_snapshot_token is not None
                else None
            )
            self._announced_snapshot = copy.deepcopy(snapshot)
            revision = (snapshot.get("revisions") or {}).get("library")
            if revision:
                self._observed_library_revision = str(revision)
        self._announced_document_state = self._document_state(snapshot)
        self.jobs.cancel_kind("preview")
        if self._index is not None and self._index.library_revision != snapshot.get("revisions", {}).get("library"):
            self._invalidate_index()
        if delta is None:
            self.emit("document.changed", snapshot, snapshot.get("revisions", {}),
                      documentId=snapshot.get("documentId"))
        # One committed gesture/batch, not each pointer/key event, prepares media.
        with self.lock:
            if self._preview_timer:
                self._preview_timer.cancel()
            if prepare_preview and self.document is not None and not self.shutdown_requested.is_set():
                self._preview_timer = threading.Timer(0.15, self._auto_preview)
                self._preview_timer.daemon = True
                self._preview_timer.start()
        return delta if delta is not None else snapshot

    def _refresh_library_inputs(self):
        invalidate = getattr(self.workspace, "invalidate_library_revision", None)
        if callable(invalidate):
            invalidate()
        refresh = getattr(self.workspace, "refresh_library_revision", None)
        revision = refresh() if callable(refresh) else self.workspace.library_revision()
        revision = str(revision)
        with self.lock:
            self._observed_library_revision = revision
        self._invalidate_index()
        self.emit("library.changed", {"libraryRevision": revision}, {"library": revision})
        return revision

    def _check_library_revision(self):
        revision = str(self.workspace.library_revision())
        with self.lock:
            previous = self._observed_library_revision
            if previous is None:
                self._observed_library_revision = revision
                return
            if previous == revision:
                return
            self._observed_library_revision = revision
        self._invalidate_index()
        self.emit("library.changed", {"libraryRevision": revision}, {"library": revision})

    @staticmethod
    def _document_state(snapshot):
        return copy.deepcopy({
            key: snapshot.get(key)
            for key in ("documentId", "revisions", "viewOnly", "offlineMedia", "pendingExternalEdits")
        })

    def _auto_preview(self):
        if self.shutdown_requested.is_set() or self.document is None:
            return
        with self.lock:
            now = time.monotonic()
            self._timeline_leases = {
                token: expires for token, expires in self._timeline_leases.items() if expires > now
            }
            if self._timeline_leases or self._manual_apply_inflight:
                timer = threading.Timer(0.15, self._auto_preview)
                timer.daemon = True
                self._preview_timer = timer
                timer.start()
                return
        if self.preview.get("state") == "ready" and self.preview.get("revisions") == self.current_revisions():
            return
        try:
            self._start_preview("interactive")
        except Exception as exc:
            self.emit("preview.error", error_value(exc), self.current_revisions())

    def _invalidate_index(self):
        if self._search is not None:
            self._search.close()
        self._index = self._search = None

    def index(self):
        revision = self.workspace.library_revision()
        with self.lock:
            if self._index is None or getattr(self._index, "library_revision", None) != revision:
                self._invalidate_index()
                from .search import SearchIndex
                self._index = SearchIndex(self.project, revision)
            return self._index

    def search_coordinator(self):
        index = self.index()
        if self._search is None:
            from .search import SearchCoordinator
            self._search = SearchCoordinator(index, semantic_runner=None, emit=self._search_event)
        return self._search

    def _search_event(self, *args, **kwargs):
        if len(args) >= 2 and isinstance(args[0], str):
            name, payload = args[:2]
        else:
            name, payload = "search.results", args[0] if args else kwargs
        if isinstance(payload, dict):
            if isinstance(payload.get("error"), dict):
                payload = {**payload, "message": payload["error"].get("message", ""),
                           "code": payload["error"].get("code", "semantic_error")}
            current = self.workspace.library_revision()
            revision = payload.get("libraryRevision", current)
            if revision != current:
                return
            self.emit(name, payload, {"library": revision})

    def _job_event(self, name, payload, revisions=None):
        self.emit(name, payload, revisions or {})
        if (name == "job.changed" and payload.get("kind") == "preview"
                and payload.get("state") in ("failed", "cancelled", "superseded")
                and revisions == self.current_revisions()
                and payload.get("id") == self.preview.get("jobId")
                and self.preview.get("requestedRevisions") == revisions):
            error = payload.get("error") or {"code": payload["state"], "message": "Preview preparation was stopped."}
            self.preview = {**self.preview, "state": "error" if payload["state"] == "failed" else "stale",
                            "error": error}
            self.emit("preview.error", {**error, "jobId": payload["id"], "keepingLastGood": bool(self.preview.get("url"))},
                      revisions)

    def preview_manager(self):
        if self._preview_manager is None:
            from .preview import PreviewManager
            self._preview_manager = PreviewManager(self.workspace)
        return self._preview_manager

    def renderer_manager(self):
        if self._renderer_manager is None:
            from .renderer import RendererManager
            self._renderer_manager = RendererManager(self.workspace)
        return self._renderer_manager

    def _relay_dispatch(self, method: str, params: dict, scope: str):
        search_methods = {"index.catalog", "index.search", "index.get"}
        editing_methods = {"document.snapshot", "document.read", "selection.get", "library.list", "library.asset", "library.query",
                           "document.apply", "renderer.read", "renderer.submit", "job.status", "permission.request"}
        if method not in (search_methods if scope == "search" else editing_methods):
            raise ProtocolError("tool_denied", "This MCP session has no authority for that method.")
        result = self.dispatch(method, params, source="search" if scope == "search" else "ai")
        from .ai_views import (
            MAX_RESULT_CHARS, _size, data_page, job_summary,
            library_page, project_summary, read_node, text_page,
        )
        if scope == "search":
            if method in {"index.catalog", "index.search"}:
                return data_page(result)
            if method == "index.get" and _size(result) > MAX_RESULT_CHARS:
                raise ProtocolError("metadata_too_large", "Retrieve fewer passage IDs per call; evidence is never truncated.")
            return result
        if method in {"document.snapshot", "document.apply"}:
            return project_summary(result, params.get("offset", 0), params.get("limit", 20))
        if method == "library.list":
            return library_page(result, params.get("offset", 0))
        if method == "library.query":
            result = {key: value for key, value in result.items() if key != "query"}
            if result.get("catalog"):
                result = {**result, "catalog": data_page(result["catalog"], max_chars=8_000)}
            return data_page(result, "matches", params.get("offset", 0), params.get("limit", 20))
        if method == "library.asset":
            # Reuse the safe pointer reader without exposing original files.
            return {"assetId": result.get("id"),
                    **read_node({"plan": result, "revisions": self.current_revisions()},
                                "document", params.get("path", ""), params.get("offset", 0), params.get("limit", 20))}
        if method == "renderer.read":
            return {"path": result["path"], "rendererRevision": result["rendererRevision"],
                    **text_page(result["content"], params.get("offset", 0), params.get("limit", 4_000))}
        if method == "job.status":
            return job_summary(result, params.get("offset", 0), params.get("limit", 2_000))
        return result

    def _library(self, offset=0, limit=50):
        from ..library import all_assets
        assets = sorted(all_assets(self.project), key=lambda asset: str(asset.get("id", "")))
        assets = [asset for asset in assets if not asset.get("deleted")]
        offset, limit = max(0, int(offset)), min(200, max(1, int(limit)))
        page = [self._asset_summary(asset) for asset in assets[offset:offset + limit]]
        return {"assets": page, "items": page, "offset": offset, "total": len(assets),
                "next": offset + limit if offset + limit < len(assets) else None,
                "libraryRevision": self.workspace.library_revision()}

    def _asset_summary(self, asset):
        result = copy.deepcopy(asset)
        result.pop("hash", None)
        for key in ("sourceURL", "url", "previewURL", "thumbnailURL", "imageURL", "proxyURL", "sourceState", "sourceReason"):
            result.pop(key, None)
        return result

    def _flags(self, method: str, params: dict) -> dict:
        """Flagged clips (flags.json): list, add, change the note, remove."""
        from .. import flags
        from ..util import ReelsError

        try:
            if method == "flags.add":
                aid = self._require_asset_id(params.get("asset"))
                def seconds(key):
                    value = params.get(key)
                    if value is None:
                        return None
                    value = float(value)
                    if not math.isfinite(value):
                        raise ReelsError(f"{key} must be a number of seconds")
                    return value
                changed = flags.add(self.project, aid, seconds("in"), seconds("out"), str(params.get("note") or ""))
            elif method == "flags.update":
                changed = flags.update(self.project, str(params.get("id") or ""), params.get("note"))
            elif method == "flags.remove":
                changed = flags.unflag(self.project, str(params.get("id") or ""))
            else:
                return {"flags": flags.load(self.project)}
        except (ReelsError, TypeError, ValueError) as exc:
            raise ProtocolError("invalid_flag", str(exc))
        listing = flags.load(self.project)
        self.emit("flags.changed", {"flags": listing}, self.current_revisions() if self.document is not None else {})
        return {"flag": changed, "flags": listing}

    def _sphere_camera(self, node_id: str) -> dict:
        """A 360 clip's camera resolved to numbers: named angles, the moment's view and a
        split segment's camera window become plain yaw/pitch/fov/roll keyframes in
        segment seconds — what the live 360 viewer edits."""
        from ..library import load_asset, resolve_id

        snapshot = self._doc().snapshot()
        node = next((n for n in snapshot["nodes"] if n["id"] == node_id), None)
        if node is None:
            raise ProtocolError("node_not_found", "That clip is no longer in the reel.")
        target = node["raw"]
        spec = node.get("resolved", {})
        if not isinstance(target, dict) or not target.get("asset"):
            raise ProtocolError("not_360", "Select a 360 clip on the timeline.")
        try:
            asset = load_asset(self.project, resolve_id(self.project, str(spec["asset"])))
        except Exception as exc:
            raise ProtocolError("asset_not_found", str(exc))
        if asset.get("kind") != "video" or not (asset.get("probe") or {}).get("sphere"):
            raise ProtocolError("not_360", "This clip isn't 360 video.")
        tags = asset.get("tags") or {}
        # Raw settings, resolved bounds/camera and asset identity belong to the
        # same snapshot, including when snapshot() reloads an external edit.
        try:
            start, end = float(spec["in"]), float(spec["out"])
            speed = float(spec.get("speed") or 1.0)
        except (KeyError, TypeError, ValueError):
            raise ProtocolError("invalid_clip", "This clip has no in/out range.")
        seconds = max(0.0, (end - start) / speed)
        window = spec.get("cameraWindow") if isinstance(spec.get("cameraWindow"), dict) else None
        camera = spec["sphere"]
        path = camera.get("path") or []
        playback_offset = float(window.get("offsetSeconds") or 0) if window else 0.0
        playback_path = [{**key, "at": key["at"] + playback_offset} for key in path]
        if window or camera["durationSeconds"] != seconds:
            from .timing import window_piece_keys
            path = window_piece_keys(playback_path, playback_offset, seconds, projection=camera["projection"])
        return {
            "id": node_id, "assetId": asset["id"], "in": start, "out": end, "speed": speed, "seconds": seconds,
            "projection": camera["projection"], "view": camera["view"], "path": path,
            "playbackPath": playback_path, "playbackOffsetSeconds": playback_offset,
            "stabilize": target.get("stabilize"), "stabilizeSetting": camera.get("stabilize"),
            "angles": tags.get("angles") or {}, "errors": [],
        }

    def _require_asset_id(self, value):
        if (not isinstance(value, str) or not value or value in (".", "..")
                or "/" in value or "\\" in value or "\x00" in value):
            raise ProtocolError("invalid_asset_id", "Use an exact library asset ID, not a path.")
        path = self.project.assets_dir / f"{value}.json"
        if path.is_symlink() or not path.is_file():
            raise ProtocolError("asset_not_found", "That asset is not in the private library.")
        return value

    def _asset(self, aid, source):
        from ..library import load_asset
        asset = load_asset(self.project, str(aid))
        result = self._asset_summary(asset)
        transcript = asset.get("transcript")
        if transcript:
            path = self.project.abs(transcript)
            if path.is_file():
                result["transcriptData"] = json.loads(path.read_text())
        if source == "native":
            try:
                from .preview import source_proxy_reason
                path = self.project.abs(asset["media"])
                reason = source_proxy_reason(asset, path)
                if asset.get("kind") not in ("video", "photo"):
                    result["sourceState"] = "unsupported"
                    result["sourceReason"] = "The registered asset has an unsupported media kind."
                elif reason:
                    result["sourceState"] = "proxy_required"
                    result["sourceReason"] = reason
                else:
                    result["sourceURL"] = self.http.register_file(f"originals/{asset['id']}/{path.name}", path)
                    result["sourceState"] = "ready"
            except Exception as exc:
                result.update(sourceState="offline", sourceReason=str(exc))
        return result

    def _register_preview(self, manifest):
        manifest = dict(manifest)
        if manifest.get("state") == "empty":
            return manifest
        revisions = manifest.get("revisions", self.current_revisions())
        key = manifest.get("httpKey") or ("previews/" + str(revisions.get("document", uuid.uuid4().hex))
                                           + "/" + str(manifest.get("mode", "interactive")) + "/" + uuid.uuid4().hex)
        manifest["httpKey"] = key
        bundle = manifest.get("bundleDir") or manifest.get("bundle_dir")
        static = manifest.get("staticDir") or manifest.get("static_dir") or manifest.get("stagingDir")
        for directory in (bundle, static):
            if directory and not Path(directory).resolve().is_relative_to(Path(self.workspace.root).resolve()):
                raise ProtocolError("invalid_preview_path", "Preview bundles and staged media must be private revision artifacts.")
        if bundle:
            manifest["url"] = self.http.register_tree(key, Path(bundle))
        if static:
            manifest["staticURL"] = self.http.register_tree(key + "/public", Path(static)).rsplit("/", 1)[0] + "/"
            manifest["staticBaseUrl"] = manifest["staticURL"]
        for field in ("movie", "review", "reel", "path"):
            path = manifest.get(field)
            if isinstance(path, (str, Path)) and Path(path).is_file() and Path(path).suffix.lower() in (".mp4", ".mov"):
                manifest[field + "URL"] = self.http.register_file(key + "/" + Path(path).name, Path(path))
                if manifest.get("mode") == "exact":
                    manifest["url"] = manifest[field + "URL"]
        manifest.update(state="ready", revisions=revisions, revision=str(revisions.get("document", "")))
        return manifest

    def _preview_record_path(self, document_id):
        key = hashlib.sha256(str(document_id).encode()).hexdigest()
        return Path(self.workspace.root) / "preview" / "documents" / f"{key}.json"

    def _restore_preview(self, snapshot):
        from .preview import empty_preview
        empty = empty_preview(snapshot, "interactive")
        if empty is not None:
            self.preview = empty
            return
        path = self._preview_record_path(snapshot.get("documentId"))
        if path.is_symlink() or not path.is_file():
            return
        try:
            saved = json.loads(path.read_text())
            if saved.get("documentId") != snapshot.get("documentId"):
                return
            restored = self._register_preview(saved)
            if restored["revisions"] != snapshot.get("revisions"):
                restored["state"] = "stale"
                restored["requestedRevisions"] = snapshot.get("revisions")
            self.preview = {**restored, "restored": True}
        except Exception as exc:
            self.preview = {"state": "unavailable", "reason": str(exc)}

    def _publish_preview(self, manifest):
        manifest = dict(manifest)
        revisions = manifest.get("revisions", self.current_revisions())
        if revisions != self.current_revisions():
            raise ProtocolError("stale_job", "Prepared preview belongs to an older document revision.")
        manifest = self._register_preview(manifest)
        def publish():
            self.preview = manifest
            atomic_json(Path(self.workspace.root) / "preview" / "last-ready.json", manifest)
            if manifest.get("documentId"):
                atomic_json(self._preview_record_path(manifest["documentId"]), manifest)
            self.emit("preview.ready", manifest, revisions)
            return manifest
        document = self._doc()
        if hasattr(document, "publish_if_current"):
            return document.publish_if_current(revisions, publish)
        if revisions != self.current_revisions():
            raise ProtocolError("stale_job", "Preview preparation lost a race to a manual edit.")
        return publish()

    def _start_preview(self, mode):
        # Serialize reservation through job submission. Otherwise a native
        # request can arrive while the automatic request has jobId=None, and
        # the two requests can cancel both workers before either ID is stored.
        with self._preview_start_lock:
            return self._start_preview_locked(mode)

    def _start_preview_locked(self, mode):
        if mode not in ("interactive", "exact"):
            raise ProtocolError("invalid_preview_mode", "Choose interactive or exact preview.")
        snapshot = copy.deepcopy(self.snapshot())
        if snapshot.get("viewOnly"):
            raise ProtocolError("offline_media", "This is an offline, read-only snapshot. The last good preview is retained; reconnect registered originals to prepare another.")
        from .preview import empty_preview
        empty = empty_preview(snapshot, mode)
        if empty is not None:
            with self.lock:
                self._preview_sequence += 1
                self.preview = empty
            self.jobs.cancel_kind("preview")
            return self._publish_preview(empty)
        with self.lock:
            existing_id = (self.preview.get("jobId") if self.preview.get("state") == "preparing"
                           and self.preview.get("requestedRevisions") == snapshot["revisions"]
                           and self.preview.get("requestedMode") == mode else None)
        if existing_id:
            existing = self.jobs.status(existing_id)
            if existing.get("state") in ("queued", "running"):
                return existing
        with self.lock:
            self._preview_sequence += 1
            generation = self._preview_sequence
            self.preview = {**self.preview, "state": "preparing", "requestedRevisions": snapshot["revisions"],
                            "requestedMode": mode, "jobId": None}
        self.jobs.cancel_kind("preview")
        self.emit("preview.pending", self.preview, snapshot["revisions"])
        def prepare(context):
            result = self.preview_manager().prepare(snapshot, mode=mode, context=context)
            return {**result, "documentId": snapshot.get("documentId"), "revisions": snapshot["revisions"]}
        def publish(result):
            if generation != self._preview_sequence:
                raise ProtocolError("stale_job", "A newer preview request superseded this preparation.")
            return self._publish_preview(result)
        job = self.jobs.submit("preview", snapshot["revisions"], prepare, publish)
        if generation == self._preview_sequence:
            self.preview["jobId"] = job["id"]
            latest = self.jobs.status(job["id"])
            if latest.get("state") in ("failed", "cancelled", "superseded"):
                self._job_event("job.changed", latest, snapshot["revisions"])
        else:
            self.jobs.cancel_job(job["id"])
        return job

    def dispatch(self, method: str, params: dict, source="native"):
        if method == "workspace.info":
            return {"workspace": str(self.workspace.root), "dataRoot": str(self.workspace.data_root),
                    "codeRoot": str(self.workspace.code_root), "readOnlyOriginals": str(self.project.media_dir),
                    "productionRoot": str(self.project.media_dir.parent.parent),
                    "offline": self.workspace.media_status(), "protocolVersion": 1,
                    "plan": self.document_path, "chatSessionId": self.claude.state["id"],
                    "previewOrigin": self.http.origin, "state": self.state,
                    "projectConfig": self._project_config(), "config": self._project_config(),
                    "permissions": copy.deepcopy(list(self.permissions.values())),
                    "live": True, "repoRoot": str(self.workspace.data_root)}
        if method == "plans.list":
            return live.list_plans(self.project, self.http)
        if method == "projects.create":
            return live.create_plan(self.project, params)
        if method == "music.list":
            return music_service.list_tracks(self.project)
        if method == "music.search":
            return self.music_search.start(params)
        if method == "music.searchCancel":
            self.music_search.cancel()
            return {"cancelled": True}
        if method == "music.add":
            args = music_service.validate_add(params)
            job = self.jobs.submit("music", {}, music_service.add_work(self.project, args), self._music_added, independent=True)
            return {"jobId": job["id"]}
        if method == "library.inbox":
            names = [str(path.relative_to(self.project.inbox)) for path in live.inbox_files(self.project)]
            return {"count": len(names), "files": names}
        if method == "library.import":
            return self._start_import(params)
        if method == "library.ingest":
            return self._start_ingest()
        if method == "document.open":
            from .transactions import DocumentStore
            path = str(params.get("path", ""))
            resolved = self.project.abs(path).resolve()
            if not resolved.is_relative_to(self.project.plans_dir.resolve()) or resolved.suffix != ".json":
                raise ProtocolError("invalid_plan_path", "Open a plan inside this workspace's private plans directory.")
            store = DocumentStore(self.workspace, path)
            with self.lock:
                self.jobs.cancel_kind("preview", "export", "renderer")
                previous = self.document
                self.document, self.document_path = store, path
                self.selection = {"ids": [], "playhead": 0}
                self.state["plan"] = path
                atomic_json(self.state_path, self.state)
                self.preview = {"state": "empty"}
                self.claude.stop()
                if previous is not None and hasattr(previous, "close"):
                    previous.close()
                self._restore_preview(store.snapshot())
            self._write_context()
            return self._changed(self.snapshot())
        if method == "document.snapshot":
            return self.snapshot()
        if method == "audio.visual":
            from .audio_visuals import audio_visual
            return audio_visual(self.project, self.snapshot() if self.document is not None else None, params)
        if method == "document.read":
            from .ai_views import read_node
            return read_node(self.snapshot(), params.get("id", "document"), params.get("path", ""),
                             params.get("offset", 0), params.get("limit", 20))
        if method == "document.apply":
            # The native app sends {"batch": {...}}; MCP/AI callers send the batch itself.
            wrapped = params.get("batch") if isinstance(params, dict) else None
            batch = copy.deepcopy(wrapped if isinstance(wrapped, dict) and "operations" in wrapped else params)
            delta_base = (
                batch.get("baseRevisions")
                if (source == "native" and batch.get("responseMode") == "delta"
                    and isinstance(batch.get("baseSnapshotToken"), str))
                else None
            )
            delta_snapshot_token = (
                batch.get("baseSnapshotToken") if delta_base is not None else None
            )
            batch["origin"] = "ai" if source == "ai" else "manual"
            if source == "ai":
                batch.pop("lease", None)
                self._guard_ai(batch)
            for operation in batch.get("operations", []):
                if isinstance(operation, dict) and operation.get("op") == "correct_words":
                    self._require_asset_id(operation.get("assetId"))
            manual_request = source != "ai"
            if manual_request:
                with self.lock:
                    self._manual_apply_inflight += 1
            try:
                result = self._doc().apply(batch)
                if manual_request and self._ai_turn is not None:
                    self._manual_during_ai.update(self._operation_targets(batch.get("operations", [])))
                return self._changed(
                    result, delta_base=delta_base, delta_snapshot_token=delta_snapshot_token
                )
            finally:
                if manual_request:
                    with self.lock:
                        self._manual_apply_inflight = max(0, self._manual_apply_inflight - 1)
        if method in ("document.undo", "document.redo"):
            if self._ai_turn is not None:
                self._manual_during_ai.add("document")
            if hasattr(self._doc(), "cancel_pending"):
                self._doc().cancel_pending(origin="ai")
            return self._changed(getattr(self._doc(), method.split(".")[1])(params.get("expected")))
        if method == "document.reload":
            result = self._doc().reload_external()
            return self._changed(result) if result else self.snapshot()
        if method == "lease.acquire":
            owner = str(params.get("owner", "native"))
            with self.lock:
                lease = self._doc().acquire_lease(
                    owner, ttl=min(60, max(1, float(params.get("ttl", 6))))
                )
                if owner.startswith("timeline-") and isinstance(lease, dict) and lease.get("token"):
                    # DocumentStore replaces the prior timeline lease with this
                    # one; mirror that single-current-lease invariant here too.
                    self._timeline_leases.clear()
                    self._timeline_leases[str(lease["token"])] = (
                        time.monotonic() + float(lease.get("expiresIn", 6))
                    )
            return lease
        if method == "lease.release":
            token = str(params.get("token", ""))
            with self.lock:
                self._doc().release_lease(token)
                self._timeline_leases.pop(token, None)
            return {"released": True}
        if method == "selection.get":
            return dict(self.selection)
        if method == "selection.set":
            self.selection = {"ids": list(params.get("ids", [])), "playhead": max(0, int(params.get("playhead", 0)))}
            self._write_context()
            return dict(self.selection)
        if method == "state.save":
            allowed = {"panels", "collapsed", "monitorMode", "zoom", "scroll", "safeArea"}
            self.state.update({key: value for key, value in params.items() if key in allowed})
            atomic_json(self.state_path, self.state)
            return self.state
        if method == "library.list":
            return self._library(params.get("offset", 0), params.get("limit", 50))
        if method == "library.asset":
            return self._asset(params.get("id"), source)
        if method == "library.query":
            return self.index().regex(params.get("query", ""), params.get("filters", {}))
        if method == "index.catalog":
            return self.index().catalog(**params)
        if method == "index.search":
            return self.index().search_passages(**params)
        if method == "index.get":
            ids = params.get("ids", [])
            if not isinstance(ids, list) or len(ids) > 200 or any(not isinstance(item, str) for item in ids):
                raise ProtocolError("invalid_passage_ids", "Request at most 200 passage IDs.")
            return self.index().get_passages(ids)
        if method == "search.query":
            submitted_at = time.monotonic()
            if self._search is not None:
                self._search.cancel()
            generation = self.search_coordinator().query(params.get("query", ""), params.get("filters", {}),
                                                          generation=params.get("generation"), submitted_at=submitted_at)
            return {"generation": generation}
        if method == "search.cancel":
            if self._search:
                self._search.cancel()
            return {"cancelled": True}
        if method == "preview.prepare":
            return self._start_preview(params.get("mode", "interactive"))
        if method == "source.prepare":
            aid = self._require_asset_id(params.get("id"))
            revisions = self.current_revisions()
            manager = self.preview_manager()
            if not hasattr(manager, "prepare_source"):
                raise ProtocolError("source_proxy_required", "This source requires a view-specific proxy not available in this build.")
            view = params.get("view")
            def publish_source(result):
                result = dict(result)
                if result.get("status", "ready") != "ready":
                    self.emit("source.unavailable", result, revisions)
                    return result
                path = result.get("path") or result.get("mediaPath")
                if not isinstance(path, (str, Path)):
                    raise ProtocolError("invalid_source_result", "Source preparation returned no registered media.")
                result["url"] = self.http.register_file(f"source/{uuid.uuid4().hex}{Path(path).suffix}", path)
                result["sourceURL"] = result["url"]
                self.emit("source.ready", result, revisions)
                return result
            def source_stopped(record):
                error = record.get("error") or {}
                message = {
                    "cancelled": "Preparing this source view was stopped.",
                    "timed_out": "Preparing this source view took longer than "
                                 f"{record.get('timeoutSeconds', SOURCE_JOB_TIMEOUT) / 60:g} minutes and was stopped.",
                }.get(record["state"], error.get("message") or "This source view couldn't be prepared.")
                self.emit("source.error", {"assetId": aid, "jobId": record["id"], "state": record["state"],
                                           "code": error.get("code") or record["state"], "message": message}, revisions)
            # One job per asset and view: a repeat request joins it and other assets never stop it.
            # The proxy belongs to the library asset, so document edits don't supersede it.
            key = f"{aid}:{json.dumps(view or {}, sort_keys=True)}"
            return self.jobs.submit("source", revisions, lambda ctx: manager.prepare_source(aid, view=view, context=ctx),
                                    publish_source, independent=True, key=key, timeout=SOURCE_JOB_TIMEOUT,
                                    on_error=source_stopped)
        if method in ("flags.list", "flags.add", "flags.update", "flags.remove"):
            return self._flags(method, params)
        if method == "sphere.camera":
            return self._sphere_camera(str(params.get("id") or ""))
        if method == "sphere.prepare":
            aid = self._require_asset_id(params.get("id"))
            try:
                start, end = float(params.get("start")), float(params.get("end"))
            except (TypeError, ValueError):
                raise ProtocolError("invalid_params", "sphere.prepare needs start and end source seconds.")
            if not (math.isfinite(start) and math.isfinite(end) and end > start):
                raise ProtocolError("invalid_params", "sphere.prepare needs end after start.")
            revisions = self.current_revisions()
            manager = self.preview_manager()
            request = str(params.get("requestId") or "")
            def publish_sphere(result):
                result = dict(result)
                result["requestId"] = request
                if result.get("status", "ready") != "ready":
                    self.emit("sphere.unavailable", result, revisions)
                    return result
                path = result.get("path")
                if not isinstance(path, (str, Path)):
                    raise ProtocolError("invalid_sphere_result", "360 preparation returned no proxy.")
                result["url"] = self.http.register_file(f"sphere/{uuid.uuid4().hex}.mp4", path)
                result.pop("path", None)
                self.emit("sphere.ready", result, revisions)
                return result
            def sphere_stopped(record):
                error = record.get("error") or {}
                self.emit("sphere.unavailable", {"requestId": request, "assetId": aid, "jobId": record["id"],
                                                 "status": record["state"],
                                                 "reason": error.get("message") or "The 360 view couldn't be prepared."}, revisions)
            self.jobs.cancel_kind("sphere")
            return self.jobs.submit("sphere", revisions, lambda ctx: manager.prepare_sphere(
                aid, start, end, stabilize=params.get("stabilize"), context=ctx), publish_sphere,
                independent=True, on_error=sphere_stopped)
        if method == "job.status":
            return self.jobs.status(str(params.get("id", "")))
        if method == "job.cancel":
            return self.jobs.cancel_job(str(params.get("id", "")))
        if method == "export.start":
            snapshot = copy.deepcopy(self.snapshot())
            if snapshot.get("viewOnly"):
                raise ProtocolError("offline_media", "Reconnect the recorded originals before exporting this read-only snapshot.")
            if not (snapshot.get("plan") or {}).get("segments"):
                raise ProtocolError("empty_plan", "Add footage to this reel before exporting.")
            return self.jobs.submit("export", snapshot["revisions"],
                                    lambda ctx: self.preview_manager().export(snapshot, context=ctx),
                                    lambda receipt: self._publish_export(receipt))
        if method == "export.save":
            from .exporting import save_artifact
            job = self.jobs.status(str(params.get("id", "")))
            if job.get("kind") != "export" or job.get("state") != "completed":
                raise ProtocolError("export_incomplete", "Only a completed export can be saved.")
            return save_artifact(self.workspace, job["result"], str(params.get("destination", "")),
                                 artifact=params.get("artifact", "reel"), overwrite=params.get("overwrite") is True)
        if method == "chat.send":
            with self.lock:
                previous_turn, previous_manual = self._ai_turn, self._manual_during_ai
                self._ai_turn = {"documentId": self._doc().snapshot().get("documentId"), "id": uuid.uuid4().hex}
                self._manual_during_ai = set()
                try:
                    return self.claude.send(params.get("text", ""), params.get("model"))
                except Exception:
                    self._ai_turn, self._manual_during_ai = previous_turn, previous_manual
                    raise
        if method == "chat.history":
            return {"sessionId": self.claude.state["id"], "records": self.claude.history()}
        if method == "chat.stop":
            self.claude.stop()
            if self.document is not None and hasattr(self.document, "cancel_pending"):
                self.document.cancel_pending(origin="ai")
            self.jobs.cancel_kind("renderer")
            return {"state": "stopping", "committedEditsRemainUndoable": True}
        if method == "permission.request":
            kind = str(params.get("kind", "")).lower().replace("-", "_")
            if kind not in ("visual", "audio", "generated_visual", "generated_audio", "title_card", "titlecard"):
                raise ProtocolError("permission_kind_unavailable", "Only scoped visual/audio consent is supported. Shell, uploads, publishing and pruning remain unavailable.")
            scope = params.get("scope")
            if not isinstance(scope, dict) or not scope:
                raise ProtocolError("scoped_permission_required", "Describe the exact generated material and where it would be used.")
            pid = uuid.uuid4().hex
            request = {"id": pid, "kind": kind, "reason": str(params.get("reason", "")),
                       "scope": scope, "state": "pending",
                       "documentId": self.document.snapshot().get("documentId") if self.document is not None else None}
            self.permissions[pid] = request
            atomic_json(Path(self.workspace.root) / "chat" / "permissions.json", self.permissions)
            self.emit("permission.requested", request, self.current_revisions())
            return request
        if method == "permission.respond":
            request = self.permissions.get(params.get("id"))
            if request is None:
                raise ProtocolError("permission_not_found", "No pending permission with that ID.")
            request["state"] = "granted" if params.get("granted") is True else "denied"
            request["decidedBy"] = "native-user"
            request["decidedAt"] = time.time()
            atomic_json(Path(self.workspace.root) / "chat" / "permissions.json", self.permissions)
            return dict(request)
        if method == "renderer.read":
            return self.renderer_manager().read(params.get("path", ""), revision=self.current_revisions().get("renderer"))
        if method == "inspector.schema":
            from .schema import inspector_schema
            extras = []
            revision = self.current_revisions().get("renderer")
            if revision and "/" not in revision and "\\" not in revision:
                manifest = Path(self.workspace.root) / "code" / "renderers" / revision / "manifest.json"
                if manifest.is_file() and not manifest.is_symlink():
                    extras = json.loads(manifest.read_text()).get("descriptors", [])
            return inspector_schema(extras)
        if method == "renderer.submit":
            raise ProtocolError("renderer_changes_unavailable",
                                "Ask Claude Code in the terminal to change renderer code.")
        if method in ("schedule.prepare", "schedule.submit"):
            from .scheduling import Scheduler
            scheduler = Scheduler(self)
            return scheduler.prepare() if method == "schedule.prepare" else scheduler.submit(params)
        if method == "shutdown":
            self.begin_close()
            return {"stopping": True}
        raise ProtocolError("method_not_found", f"Unknown editor method: {method}")

    def _publish_export(self, receipt):
        receipt = dict(receipt)
        from .http import identity
        receipt["artifactIdentities"] = {
            field: list(identity(Path(receipt[field])))
            for field in ("reelPath", "previewVideoPath")
            if isinstance(receipt.get(field), str) and Path(receipt[field]).is_file()
        }
        if receipt.get("sideEffects") != "private-snapshot-only":
            raise ProtocolError("invalid_export_receipt", "Export bookkeeping did not stay inside its private snapshot.")
        if receipt.get("usageUpdates"):
            snapshot = self._doc().commit_export_usage(receipt["usageUpdates"], receipt["revisions"])
            receipt["bookkeepingRevisions"] = snapshot.get("revisions", {})
            self._changed(snapshot)
        return receipt

    def _check_external_changes(self):
        document = self.document
        if document is None:
            return
        # reload_external returns a snapshot even when no files changed.
        # Treating every return as a change cancels preparation once a second.
        result = document.reload_external()
        if (result is not None and document is self.document
                and self._document_state(result) != self._announced_document_state):
            self._changed(result)

    def _watch(self):
        last_error = None
        ticks = 0
        while not self.shutdown_requested.wait(1):
            ticks += 1
            if ticks % 2 == 0:
                try:
                    self._poll_plans()
                except Exception:  # noqa: BLE001
                    pass
            document = self.document
            try:
                self._check_library_revision()
            except Exception as exc:
                message = str(exc)
                if message != last_error:
                    self.emit("library.error", error_value(exc), {})
                    last_error = message
            if document is None:
                continue
            try:
                self._check_external_changes()
                last_error = None
            except Exception as exc:
                message = str(exc)
                if message != last_error:
                    self.emit("document.external_error", error_value(exc), self.current_revisions())
                    last_error = message

    def _poll_plans(self):
        stamp = live.plans_stamp(self.project)
        previous, self._plans_stamp = self._plans_stamp, stamp
        if previous is None or previous == stamp:
            return
        changed = sorted(name for name in set(stamp) | set(previous) if stamp.get(name) != previous.get(name))
        self.emit("plans.changed", {"paths": [f"plans/{name}" for name in changed],
                                    "added": [f"plans/{n}" for n in changed if n not in previous],
                                    "removed": [f"plans/{n}" for n in changed if n not in stamp]}, {})

    def _write_context(self):
        try:
            snapshot = self.document.snapshot() if self.document is not None else {}
            ids = [str(item) for item in self.selection.get("ids", [])]
            fps = 30
            try:
                fps = int((snapshot.get("fps") or snapshot.get("config", {}).get("render", {}).get("fps") or 30))
            except (TypeError, ValueError):
                pass
            live.write_context(self.workspace, self.document_path, live.describe_nodes(snapshot, ids),
                               round(float(self.selection.get("playhead", 0)) / max(1, fps), 3))
        except Exception:  # noqa: BLE001 - context is a convenience, never a failure
            pass

    def _env_for_commands(self) -> dict:
        env = dict(os.environ)
        env["REELS_HOME"] = str(self.workspace.data_root)
        return env

    def _music_added(self, result):
        self.emit("music.changed", {"track": result.get("track")}, {})
        self._refresh_library_inputs()
        return result

    def _start_import(self, params):
        paths = params.get("paths")
        if not isinstance(paths, list) or not paths:
            raise ProtocolError("invalid_paths", "Pass the files or folders to import as absolute paths.")
        files, skipped = live.plan_import(self.project, paths)
        ingest = params.get("ingest", True) is not False
        if ingest and self._ingest_job and self.jobs.status(self._ingest_job).get("state") in ("queued", "running"):
            raise ProtocolError("ingest_running", "An ingest is already running.")
        job = self._submit_ingest(files, ingest)
        return {"jobId": job["id"], "copied": len(files), "skipped": skipped}

    def _start_ingest(self):
        if self._ingest_job and self.jobs.status(self._ingest_job).get("state") in ("queued", "running"):
            raise ProtocolError("ingest_running", "An ingest is already running.")
        job = self._submit_ingest([], True)
        return {"jobId": job["id"]}

    def _submit_ingest(self, files, ingest):
        repo = Path(self.workspace.data_root)

        def work(context):
            copied = live.copy_into_inbox(self.project, files, context.log, context.check_cancelled) if files else 0
            if ingest:
                context.log("Running ./reels ingest")
                context.stream([str(repo / "reels"), "ingest"], cwd=repo, env=self._env_for_commands())
            return {"copied": copied, "ingested": ingest}

        def publish(result):
            if result.get("ingested"):
                self._refresh_library_inputs()
            return result

        job = self.jobs.submit("ingest", {}, work, publish, independent=True)
        self._ingest_job = job["id"]
        return job

    def begin_close(self):
        self.shutdown_requested.set()
        try:
            if self._preview_timer:
                self._preview_timer.cancel()
            if self.document is not None and hasattr(self.document, "cancel_pending"):
                self.document.cancel_pending(origin="ai")
            self.claude.stop()
            self.jobs.cancel_kind("preview", "export", "source", "renderer")
        finally:
            try:
                if self.document is not None and hasattr(self.document, "close"):
                    self.document.close()
            finally:
                self._compile_session.close()

    def close(self):
        if self._service_closed:
            return
        self._service_closed = True
        try:
            try:
                self.begin_close()
            except Exception as exc:
                self.emit("service.cleanup_error", error_value(exc), {})
            if self.watcher is not None and self.watcher.is_alive():
                self.watcher.join(timeout=2)
            stop_library_watcher = getattr(self.workspace, "stop_library_watcher", None)
            if callable(stop_library_watcher):
                stop_library_watcher()
            for resource in (self._search, self.claude, self.jobs, self.http, self.relay):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception as exc:
                        self.emit("service.cleanup_error", error_value(exc), {})
        finally:
            fcntl.flock(self._workspace_lock_fd, fcntl.LOCK_UN)
            os.close(self._workspace_lock_fd)


SOURCE_JOB_TIMEOUT = 1800.0  # s of work per source job; the app waits a little longer (EditorStore+Source.swift)
PARTIAL_PROXY_GRACE = 600.0  # s; a younger partial may be an encode still running outside this service


def remove_partial_proxies(workspace_root: Path, now: float | None = None) -> list[Path]:
    """Delete the .source-*/.sphere-* temporaries that killed proxy workers left behind.

    Only in this workspace's private preview cache (<root>/preview/sources|spheres/<key>/);
    finished proxies, symlinks and anything else are left alone."""
    from .proxy_cache import cache_lock

    now = time.time() if now is None else now
    preview = Path(workspace_root) / "preview"
    removed: list[Path] = []
    for folder, prefix in (("sources", ".source-"), ("spheres", ".sphere-")):
        root = preview / folder
        try:
            if preview.is_symlink() or root.is_symlink() or not root.is_dir():
                continue
            for entry in root.iterdir():
                if entry.is_symlink() or not entry.is_dir():
                    continue
                # A CLI prewarm has no JobManager record. Its cross-process key
                # lock, not the partial's age, proves whether it is still writing.
                with cache_lock(entry, blocking=False) as acquired:
                    if not acquired:
                        continue
                    directories = [entry, *(p for p in entry.iterdir() if p.is_dir() and not p.is_symlink())]
                    for directory in directories:
                        for path in directory.iterdir():
                            if (not path.name.startswith(prefix) or path.is_symlink() or not path.is_file()
                                    or now - path.stat().st_mtime < PARTIAL_PROXY_GRACE):
                                continue
                            path.unlink(missing_ok=True)
                            removed.append(path)
        except OSError:
            continue  # cache housekeeping never blocks the service; the next start tries again
    return removed


def serve(workspace) -> int:
    protocol_stdout = sys.stdout
    writer = Writer(protocol_stdout)
    # Existing helpers may log to stdout. Protocol retains its own original stream.
    with contextlib.redirect_stdout(sys.stderr):
        service = EditorService(workspace, writer.event)
        previous_signals = {}
        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGTERM, signal.SIGINT):
                previous_signals[signum] = signal.signal(signum, lambda *_args: service.shutdown_requested.set())
        pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="reels-request")
        writer.event("service.ready", service.dispatch("workspace.info", {}), {})
        def handle(line):
            try:
                request = decode(line)
                writer.send(respond(request, service.dispatch))
            except Exception as exc:
                writer.send({"version": 1, "id": None, "error": error_value(exc)})
        try:
            selector = selectors.DefaultSelector()
            selector.register(sys.stdin.fileno(), selectors.EVENT_READ)
            pending = b""
            while not service.shutdown_requested.is_set() and not writer.closed:
                if not selector.select(timeout=0.25):
                    continue
                chunk = os.read(sys.stdin.fileno(), 65536)
                if not chunk:
                    break
                pending += chunk
                while b"\n" in pending:
                    line, pending = pending.split(b"\n", 1)
                    if line.strip():
                        pool.submit(handle, line)
                if len(pending) > MAX_MESSAGE:
                    raise ProtocolError("message_too_large", "Unterminated request exceeds protocol limit.")
            selector.close()
        except (KeyboardInterrupt, BrokenPipeError):
            pass
        finally:
            service.begin_close()
            pool.shutdown(wait=True, cancel_futures=True)
            service.close()
            for signum, handler in previous_signals.items():
                signal.signal(signum, handler)
    return 0
