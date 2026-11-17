from __future__ import annotations

import tempfile
import json
import os
import queue
import signal
import shlex
import shutil
import subprocess
import sys
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from helpers import TempProject
from reels.editor.delta import snapshot_token
from reels.editor.protocol import ProtocolError
from reels.editor.service import EditorService


class ServiceBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.workspace = SimpleNamespace(root=self.fixture.root, data_root=self.fixture.root,
                                         code_root=Path(__file__).resolve().parents[1],
                                         project=self.fixture.project,
                                         media_status=lambda: [], library_revision=lambda: "library-fixture")
        self.events = []
        self.service = EditorService(self.workspace, lambda *args, **kwargs: self.events.append((args, kwargs)))
        self.addCleanup(self.service.close)

    def test_search_never_wires_a_semantic_runner(self):
        coordinator = self.service.search_coordinator()
        self.assertIsNone(coordinator.semantic_runner)

    def test_search_query_makes_no_semantic_call_or_event(self):
        calls = []
        self.service.claude.search = lambda *a, **k: calls.append((a, k))
        self.service.dispatch("search.query", {"query": "bridge", "filters": {}, "generation": 1})
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any(
                a and a[0] == "search.results" for a, _ in self.events):
            time.sleep(0.02)
        time.sleep(0.8)  # past the old 600 ms semantic delay
        names = [(a[1] or {}).get("type") for a, _ in self.events if a and a[0] == "search.results"]
        self.assertIn("regex", names)
        self.assertNotIn("semantic", names)
        self.assertEqual(calls, [])

    def test_workspace_roots_are_explicit(self):
        info = self.service.dispatch("workspace.info", {})
        self.assertEqual(info["dataRoot"], str(self.fixture.root))
        self.assertEqual(info["protocolVersion"], 1)
        self.assertTrue(info["previewOrigin"].startswith("http://127.0.0.1:"))

    def test_one_mutation_broker_per_private_workspace(self):
        with self.assertRaises(ProtocolError) as error:
            EditorService(self.workspace, lambda *args, **kwargs: None)
        self.assertEqual(error.exception.code, "workspace_in_use")
        self.service.close()
        reopened = EditorService(self.workspace, lambda *args, **kwargs: None)
        reopened.close()

    def test_compile_session_closes_after_document_and_repeated_service_close_is_safe(self):
        session = self.service._compile_session
        events = []
        original_close = session.close
        session.close = lambda: (events.append("session"), original_close())
        self.service.document = SimpleNamespace(
            close=lambda: events.append("document"),
            cancel_pending=lambda **_kwargs: None,
        )

        self.service.close()
        self.service.close()

        self.assertEqual(events, ["document", "session"])
        self.assertTrue(session.closed)

    def test_constructor_failure_releases_compile_session_and_workspace_lock(self):
        from unittest.mock import Mock, patch

        self.service.close()
        session = Mock()
        with (
            patch("reels.editor.compile_worker.begin_compile_session", return_value=session),
            patch("reels.editor.service.JobManager", side_effect=RuntimeError("job manager failed")),
            self.assertRaisesRegex(RuntimeError, "job manager failed"),
        ):
            EditorService(self.workspace, lambda *args, **kwargs: None)

        session.close.assert_called_once()
        reopened = EditorService(self.workspace, lambda *args, **kwargs: None)
        reopened.close()

    def test_service_watcher_start_failure_retires_session_and_releases_lock(self):
        from reels.editor.compile_worker import begin_compile_session

        self.service.close()
        sessions = []

        def create_session(workspace):
            session = begin_compile_session(workspace)
            sessions.append(session)
            return session

        class FailingThread(threading.Thread):
            def start(self):
                raise RuntimeError("watcher thread start failed")

        watcher = FailingThread()
        with (
            patch("reels.editor.compile_worker.begin_compile_session", side_effect=create_session),
            patch.object(EditorService, "_new_service_watcher", return_value=watcher),
            self.assertRaisesRegex(RuntimeError, "watcher thread start failed"),
        ):
            EditorService(self.workspace, lambda *args, **kwargs: None)

        self.assertFalse(watcher.is_alive(), "cleanup must tolerate the unstarted thread")
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0].closed)
        self.assertTrue(sessions[0].owner.closed)
        reopened = EditorService(self.workspace, lambda *args, **kwargs: None)
        reopened.close()

    def test_search_mcp_cannot_read_document_or_mutate(self):
        for method in ("document.snapshot", "document.apply", "renderer.read", "permission.respond", "shutdown"):
            with self.assertRaises(ProtocolError) as error:
                self.service._relay_dispatch(method, {}, "search")
            self.assertEqual(error.exception.code, "tool_denied")

    def test_editing_mcp_has_no_permission_self_grant(self):
        for method in ("permission.respond", "export.start", "state.save", "shutdown", "chat.send"):
            with self.assertRaises(ProtocolError):
                self.service._relay_dispatch(method, {}, "editing")

    def test_native_library_gets_registered_source_but_ai_does_not(self):
        asset = self.fixture.asset("a")
        asset["probe"]["vcodec"] = "h264"
        from reels.library import save_asset
        save_asset(self.fixture.project, asset)
        native = self.service.dispatch("library.asset", {"id": "a"})
        ai = self.service._relay_dispatch("library.asset", {"id": "a"}, "editing")
        self.assertIn("sourceURL", native)
        self.assertNotIn("sourceURL", ai)
        self.assertNotIn("hash", native)
        listing = self.service.dispatch("library.list", {})
        self.assertEqual(listing["total"], 1)

    def test_metadata_cannot_forge_a_transport_url(self):
        asset = self.fixture.asset("a")
        asset["sourceURL"] = "http://localhost:1234/not-registered"
        asset["previewURL"] = "https://untrusted.invalid/footage"
        from reels.library import save_asset
        save_asset(self.fixture.project, asset)
        ai = self.service._relay_dispatch("library.asset", {"id": "a"}, "editing")
        self.assertNotIn("sourceURL", ai)
        self.assertNotIn("previewURL", ai)

    def test_transcript_and_source_asset_ids_cannot_be_paths(self):
        with self.assertRaises(ProtocolError):
            self.service.dispatch("source.prepare", {"id": "../../../config"})
        self.service.document = SimpleNamespace(apply=lambda batch: self.fail("must reject before mutation"))
        with self.assertRaises(ProtocolError):
            self.service.dispatch("document.apply", {"operations": [{"op": "correct_words",
                                                                     "assetId": "../../../config", "corrections": []}]})
        self.service.document = None

    def test_360_source_is_not_pretended_playable(self):
        asset = self.fixture.asset("sphere")
        asset["probe"]["sphere"] = {"projection": "equirectangular"}
        from reels.library import save_asset
        save_asset(self.fixture.project, asset)
        result = self.service.dispatch("library.asset", {"id": "sphere"})
        self.assertEqual(result["sourceState"], "proxy_required")
        self.assertNotIn("sourceURL", result)

    def test_unsupported_source_codec_and_photo_format_request_real_proxies(self):
        from reels.library import save_asset
        for aid, kind, codec, extension in (
            ("vp9", "video", "vp9", ".webm"),
            ("av1", "video", "av1", ".mp4"),
            ("unknown", "video", None, ".mov"),
            ("tiff", "photo", None, ".tiff"),
        ):
            with self.subTest(aid=aid):
                asset = self.fixture.asset(aid, kind=kind)
                old = self.fixture.project.abs(asset["media"])
                new = old.with_suffix(extension)
                old.rename(new)
                asset["media"] = self.fixture.project.rel(new)
                asset["probe"]["vcodec"] = codec
                save_asset(self.fixture.project, asset)
                result = self.service.dispatch("library.asset", {"id": aid})
                self.assertEqual(result["sourceState"], "proxy_required")
                self.assertNotIn("sourceURL", result)

    def test_ai_origin_cannot_impersonate_manual(self):
        captured = []
        self.service.document = SimpleNamespace(apply=lambda batch: captured.append(batch) or {"revisions": {}})
        self.service.dispatch("document.apply", {"origin": "manual", "expected": {}, "operations": [], "reason": "test"}, source="ai")
        self.assertEqual(captured[0]["origin"], "ai")
        self.service.document = None

    def test_manual_node_changes_win_even_if_ai_rereads(self):
        snapshot = {"documentId": "doc", "revisions": {"document": "new"}}
        self.service.document = SimpleNamespace(snapshot=lambda: snapshot, apply=lambda batch: snapshot)
        self.service._ai_turn = {"documentId": "doc"}
        self.service._manual_during_ai.add("clip-a")
        with self.assertRaises(ProtocolError) as error:
            self.service.dispatch("document.apply", {"expected": snapshot["revisions"], "reason": "overwrite",
                                                     "operations": [{"op": "set_field", "id": "clip-a",
                                                                     "path": "/volume", "value": 1}]}, source="ai")
        self.assertEqual(error.exception.code, "manual_conflict")
        self.service.document = None

    def test_failed_chat_start_does_not_clear_manual_conflicts(self):
        from unittest.mock import patch
        self.service.document = SimpleNamespace(snapshot=lambda: {"documentId": "doc"})
        self.service._ai_turn = {"documentId": "doc", "id": "original"}
        self.service._manual_during_ai = {"clip-a"}
        with patch.object(self.service.claude, "send", side_effect=ProtocolError("chat_busy", "busy")):
            with self.assertRaises(ProtocolError):
                self.service.dispatch("chat.send", {"text": "another turn"})
        self.assertEqual(self.service._manual_during_ai, {"clip-a"})
        self.assertEqual(self.service._ai_turn["id"], "original")
        self.service.document = None

    def test_deleted_assets_are_not_shown(self):
        asset = self.fixture.asset("gone")
        asset["deleted"] = {"at": "earlier"}
        from reels.library import save_asset
        save_asset(self.fixture.project, asset)
        self.assertEqual(self.service.dispatch("library.list", {})["assets"], [])

    def test_permission_request_is_not_a_grant(self):
        request = self.service._relay_dispatch("permission.request",
                                              {"kind": "visual", "reason": "new effect", "scope": {"title": "a"}},
                                              "editing")
        self.assertEqual(request["state"], "pending")
        result = self.service.dispatch("permission.respond", {"id": request["id"], "granted": False})
        self.assertEqual(result["state"], "denied")
        self.assertNotIn("titleCards", self.fixture.project.config)

    def test_selection_and_panel_state_are_private(self):
        self.service.dispatch("selection.set", {"ids": ["node-a"], "playhead": 45})
        self.assertEqual(self.service.dispatch("selection.get", {})["playhead"], 45)
        self.service.dispatch("state.save", {"panels": {"library": 230}, "upload": {"bucket": "must-not-save"}})
        self.assertEqual(self.service.state["panels"], {"library": 230})
        self.assertNotIn("upload", self.service.state)

    def _wait_trim(self):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.service._trim_thread is not None:
            time.sleep(0.01)
        self.assertIsNone(self.service._trim_thread, "the cache trim did not finish")

    def test_publishing_a_preview_trims_the_preview_cache_but_never_what_it_points_at(self):
        preview = Path(self.workspace.root) / "preview"
        self.fixture.project.config["editor"] = {"previewCacheGB": 0}
        old = time.time() - 7200
        stale = []
        for seed in range(5):
            folder = preview / "revisions" / f"{seed:020x}-{seed:08x}"
            (folder / "public").mkdir(parents=True)
            (folder / "public/seg0.mp4").write_bytes(b"x" * 100)
            os.utime(folder / "public/seg0.mp4", (old, old))
            os.utime(folder / "public", (old, old))
            os.utime(folder, (old + seed, old + seed))  # seed 0 is the oldest
            stale.append(folder)
        proxy = preview / "sources/abc/proxy.mp4"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"proxy")
        os.utime(proxy.parent, (old, old))
        revisions = {"document": "d1", "library": "l", "renderer": "r"}
        self.service.document = SimpleNamespace(
            snapshot=lambda: {"revisions": revisions},
            publish_if_current=lambda expected, callback: callback())
        current = preview / "revisions" / f"{9:020x}-{9:08x}"
        (current / "public").mkdir(parents=True)
        (current / "public/seg0.mp4").write_bytes(b"x" * 100)
        (current / "public/props.json").write_text("{}")
        os.utime(current, (old, old))  # old enough to rely on the manifest, not the grace period
        try:
            self.service._publish_preview({
                "documentId": "doc", "revisions": revisions, "mode": "interactive",
                "staticDir": str(current / "public"), "propsPath": str(current / "public/props.json")})
            self._wait_trim()
        finally:
            self.service.document = None
        self.assertTrue(current.is_dir(), "the published preview's folder is referenced by last-ready.json")
        self.assertEqual(sorted(p.name for p in stale if p.exists()), sorted(p.name for p in stale[2:]),
                         "unreferenced folders go oldest first, the newest three unowned ones stay")
        self.assertEqual(proxy.read_bytes(), b"proxy")

    def test_a_documents_preview_urls_beyond_the_newest_three_are_released(self):
        root = Path(self.workspace.root)
        keys = []
        for number in range(5):
            movie = root / f"preview/revisions/rev{number}/exact.mp4"
            movie.parent.mkdir(parents=True)
            movie.write_bytes(b"movie")
            key = f"previews/d{number}/exact/u{number}"
            self.service.http.register_file(f"{key}/exact.mp4", movie)
            self.service._retire_superseded_previews({"httpKey": key, "documentId": "doc"})
            keys.append(key)
        registered = set(self.service.http.files)
        self.assertEqual(registered, {f"{key}/exact.mp4" for key in keys[2:]})

    def test_automatic_and_native_preview_requests_coalesce_instead_of_cancelling_each_other(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        snapshot = {"documentId": "d", "revisions": {"document": "d", "library": "l", "renderer": "r"},
                    "plan": {"slug": "fixture", "segments": [{"asset": "a"}]}, "preview": {}}
        self.service.document = SimpleNamespace(snapshot=lambda: snapshot)
        def prepare(*args, **kwargs):
            calls.append(1)
            entered.set()
            release.wait(5)
            return {"mode": "interactive"}
        self.service._preview_manager = SimpleNamespace(prepare=prepare)
        try:
            first = self.service.dispatch("preview.prepare", {"mode": "interactive"})
            self.assertTrue(entered.wait(2))
            second = self.service.dispatch("preview.prepare", {"mode": "interactive"})
            self.service._auto_preview()
            self.assertEqual(first["id"], second["id"])
            self.assertEqual(calls, [1])
            self.assertFalse(self.service.jobs.contexts[first["id"]].cancel.is_set())
        finally:
            release.set()
            self.service.document = None

    def test_timeline_burst_defers_preview_until_gesture_lease_releases(self):
        timers = []

        class FakeTimer:
            def __init__(self, interval, function):
                self.interval, self.function = interval, function
                self.cancelled = False
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                self.cancelled = True

        revisions = {"document": "d1", "library": "l", "renderer": "r"}
        snapshot = {"documentId": "doc", "revisions": revisions, "plan": {"segments": []}, "nodes": []}
        self.service.document = SimpleNamespace(snapshot=lambda: snapshot)
        self.service._timeline_leases["gesture"] = time.monotonic() + 5
        self.service._start_preview = Mock(return_value={"id": "one"})
        try:
            with patch("reels.editor.service.threading.Timer", FakeTimer):
                self.service._changed(snapshot)
                first = timers[-1]
                snapshot["revisions"] = {**revisions, "document": "d2"}
                self.service._changed(snapshot)
                second = timers[-1]
                self.assertTrue(first.cancelled, "the burst replaces the earlier debounce timer")

                second.function()
                self.assertEqual(self.service._start_preview.call_count, 0)
                self.service._timeline_leases.clear()
                self.service._manual_apply_inflight = 1  # the next queued apply is now validating
                deferred = timers[-1]
                deferred.function()
                self.assertEqual(self.service._start_preview.call_count, 0)
                self.service._manual_apply_inflight = 0
                timers[-1].function()
                self.assertEqual(self.service._start_preview.call_count, 1)
        finally:
            self.service.document = None

    def test_replaced_timeline_lease_release_unblocks_one_preview(self):
        from concurrent.futures import ThreadPoolExecutor

        timers = []
        active_lease = {"token": None}
        first_entered, release_first, second_entered = threading.Event(), threading.Event(), threading.Event()
        lease_number = [0]

        class FakeTimer:
            def __init__(self, interval, function):
                self.interval, self.function = interval, function
                timers.append(self)

            def start(self):
                pass

            def cancel(self):
                pass

        def acquire_lease(_owner, ttl):
            lease_number[0] += 1
            token = f"timeline-{lease_number[0]}"
            active_lease["token"] = token
            if token == "timeline-1":
                first_entered.set()
                release_first.wait(3)
            else:
                second_entered.set()
            return {"token": token, "expiresIn": ttl}

        def release_lease(token):
            if active_lease["token"] == token:
                active_lease["token"] = None
                return True
            return False

        snapshot = {
            "documentId": "doc",
            "revisions": {"document": "d1", "library": "l", "renderer": "r"},
            "plan": {"segments": []}, "nodes": [],
        }
        self.service.document = SimpleNamespace(
            snapshot=lambda: snapshot, acquire_lease=acquire_lease, release_lease=release_lease,
        )
        self.service._start_preview = Mock(return_value={"id": "preview"})
        try:
            with patch("reels.editor.service.threading.Timer", FakeTimer):
                with ThreadPoolExecutor(max_workers=2) as callers:
                    first_call = callers.submit(
                        self.service.dispatch, "lease.acquire", {"owner": "timeline-trim"}
                    )
                    self.assertTrue(first_entered.wait(2))
                    second_call = callers.submit(
                        self.service.dispatch, "lease.acquire", {"owner": "timeline-reorder"}
                    )
                    self.assertFalse(
                        second_entered.wait(0.05),
                        "the later acquire must not pass an earlier acquire before tracking it",
                    )
                    release_first.set()
                    first = first_call.result(timeout=2)
                    second = second_call.result(timeout=2)
                self.assertTrue(second_entered.is_set())
                self.assertEqual(active_lease["token"], second["token"])
                self.assertEqual(set(self.service._timeline_leases), {second["token"]})
                self.service._changed(snapshot)
                self.service.dispatch("lease.release", {"token": first["token"]})
                self.assertEqual(set(self.service._timeline_leases), {second["token"]})
                self.service.dispatch("lease.release", {"token": second["token"]})
                self.assertFalse(self.service._timeline_leases)
                timers[-1].function()
                self.assertEqual(self.service._start_preview.call_count, 1)
        finally:
            release_first.set()
            self.service.document = None

    def test_preview_reservation_and_submission_are_one_coalescing_step(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        submitting, release_submit, duplicate = threading.Event(), threading.Event(), threading.Event()
        release_worker = threading.Event()
        snapshot = {"documentId": "d", "revisions": {"document": "d", "library": "l", "renderer": "r"},
                    "plan": {"slug": "fixture", "segments": [{"asset": "a"}]}}
        self.service.document = SimpleNamespace(snapshot=lambda: snapshot)
        def prepare(*args, **kwargs):
            release_worker.wait(3)
            return {"mode": "interactive"}
        self.service._preview_manager = SimpleNamespace(prepare=prepare)
        real_submit = self.service.jobs.submit
        submissions = []

        def delayed_submit(*args, **kwargs):
            submissions.append(1)
            if len(submissions) > 1:
                duplicate.set()
            submitting.set()
            release_submit.wait(3)
            return real_submit(*args, **kwargs)

        try:
            with patch.object(self.service.jobs, "submit", side_effect=delayed_submit):
                with ThreadPoolExecutor(max_workers=2) as callers:
                    first = callers.submit(self.service._start_preview, "interactive")
                    self.assertTrue(submitting.wait(2))
                    second = callers.submit(self.service._start_preview, "interactive")
                    try:
                        self.assertFalse(duplicate.wait(0.1))
                    finally:
                        release_submit.set()
                    one, two = first.result(timeout=3), second.result(timeout=3)
                    self.assertEqual(one["id"], two["id"])
                    self.assertEqual(submissions, [1])
        finally:
            release_submit.set()
            release_worker.set()
            self.service.document = None

    def test_unchanged_watcher_snapshots_do_not_restart_preview(self):
        from unittest.mock import patch
        snapshot = {"documentId": "d", "revisions": {"document": "d", "library": "l", "renderer": "r"},
                    "plan": {"slug": "fixture", "segments": [{"asset": "a"}]}}
        self.service.document = SimpleNamespace(
            snapshot=lambda: snapshot, reload_external=lambda: snapshot)
        self.service._announced_document_state = self.service._document_state(snapshot)
        try:
            with patch.object(self.service, "_changed", wraps=self.service._changed) as changed:
                for _ in range(3):
                    self.service._check_external_changes()
                changed.assert_not_called()
                snapshot["revisions"]["document"] = "external-edit"
                self.service._check_external_changes()
                changed.assert_called_once()
                self.service._check_external_changes()
                changed.assert_called_once()
                snapshot["viewOnly"] = True
                snapshot["offlineMedia"] = {"original": {"status": "missing"}}
                self.service._check_external_changes()
                self.assertEqual(changed.call_count, 2)
                self.service._check_external_changes()
                self.assertEqual(changed.call_count, 2)
        finally:
            self.service.document = None

    def test_renderer_ready_change_does_not_start_a_replacement_interactive_job(self):
        from unittest.mock import patch
        snapshot = {"documentId": "d", "revisions": {"document": "new", "library": "l", "renderer": "candidate"}}
        self.service.document = SimpleNamespace(snapshot=lambda: snapshot)
        try:
            with patch("reels.editor.service.threading.Timer") as timer:
                self.service._changed(snapshot, prepare_preview=False)
                timer.assert_not_called()
            self.assertEqual(self.service._announced_document_state, self.service._document_state(snapshot))
        finally:
            self.service.document = None

    def test_editing_project_and_node_tools_never_return_preview_props(self):
        snapshot = {
            "documentId": "d", "revisions": {"document": "d", "library": "l", "renderer": "r"},
            "plan": {"segments": [{"asset": "a", "volume": .85}]},
            "nodes": [{"id": "stable", "kind": "segment", "lane": "primary",
                       "raw": {"asset": "a", "volume": .85}}],
            "preview": {"state": "ready", "props": {"large": "x" * 100_000}},
        }
        self.service.document = SimpleNamespace(snapshot=lambda: snapshot)
        try:
            summary = self.service._relay_dispatch("document.snapshot", {}, "editing")
            self.assertEqual(summary["nodes"][0]["id"], "stable")
            self.assertNotIn("props", summary["preview"])
            self.assertLess(len(json.dumps(summary)), 12_000)
            field = self.service._relay_dispatch("document.read", {"id": "stable", "path": "/volume"}, "editing")
            self.assertEqual(field["value"], .85)
            with self.assertRaises(ProtocolError):
                self.service._relay_dispatch("document.read", {"id": "stable"}, "search")
        finally:
            self.service.document = None

    def test_apply_delta_is_complete_small_and_falls_back_on_stale_base(self):
        revisions = {"document": "d1", "library": "l1", "renderer": "r1"}
        nodes = [
            {"id": f"node-{index}", "kind": "text", "lane": "text", "startFrame": index,
             "endFrame": index + 10, "frames": 10, "raw": {"text": str(index)}}
            for index in range(42)
        ]
        before = {
            "documentId": "doc", "revisions": revisions,
            "plan": {"segments": [{"asset": f"asset-{index}"} for index in range(42)]},
            "nodes": nodes, "fps": 30, "durationFrames": 420, "warnings": [],
            "preview": {"state": "ready"}, "tracks": [], "config": {}, "projectConfig": {},
            "change": {"origin": "system"}, "selection": {"ids": []}, "permissions": [],
        }
        before["snapshotToken"] = snapshot_token(before)
        after = deepcopy(before)
        after["revisions"]["document"] = "d2"
        after["plan"]["segments"][7]["volume"] = .5
        after["plan"]["segments"].pop()
        after["nodes"][7]["raw"]["volume"] = .5
        after["nodes"].pop()
        after["durationFrames"] = 390
        after["warnings"] = ["fixture warning"]
        after["preview"] = {"state": "stale"}
        after["change"] = {"origin": "manual", "affectedIds": ["node-7"]}
        self.service.preview = {"state": "stale"}
        self.service._announced_snapshot = before
        self.service.document = SimpleNamespace(snapshot=lambda: after)
        delta = self.service._changed(
            after, prepare_preview=False, delta_base=revisions,
            delta_snapshot_token=before["snapshotToken"],
        )

        self.assertEqual(delta["type"], "document.delta")
        self.assertEqual(delta["baseSnapshotToken"], before["snapshotToken"])
        wire_after = {
            **after, "preview": dict(self.service.preview),
            "selection": dict(self.service.selection),
            "permissions": list(self.service.permissions.values()),
        }
        self.assertEqual(delta["snapshotToken"], snapshot_token(wire_after))
        self.assertEqual(delta["removedIds"], ["node-41"])
        self.assertEqual(delta["nodeOrder"], [f"node-{index}" for index in range(41)])
        self.assertEqual([node["id"] for node in delta["changedNodes"]], ["node-7"])
        self.assertEqual(delta["revisions"]["document"], "d2")
        self.assertEqual(delta["fields"]["durationFrames"], 390)
        self.assertEqual(delta["fields"]["warnings"], ["fixture warning"])
        self.assertEqual(delta["fields"]["preview"], {"state": "stale"})
        self.assertTrue(any(item["path"] == "/segments" for item in delta["planPatch"]))
        self.assertLess(len(json.dumps(delta, separators=(",", ":")).encode()), 5_000)
        self.assertFalse(any(args[0] == "document.changed" for args, _ in self.events))

        stale = deepcopy(after)
        stale["revisions"]["document"] = "d3"
        self.events.clear()
        full = self.service._changed(
            stale, prepare_preview=False, delta_base=revisions,
            delta_snapshot_token=before["snapshotToken"],
        )
        self.assertNotIn("type", full)
        wire_stale = {
            **stale, "preview": dict(self.service.preview),
            "selection": dict(self.service.selection),
            "permissions": list(self.service.permissions.values()),
        }
        self.assertEqual(full["snapshotToken"], snapshot_token(wire_stale))
        self.assertTrue(any(args[0] == "document.changed" for args, _ in self.events))
        self.service.document = None

    def test_same_revision_metadata_mismatch_forces_full_snapshot(self):
        revisions = {"document": "d1", "library": "l1", "renderer": "r1"}
        announced = {
            "documentId": "doc", "revisions": revisions, "plan": {"segments": []}, "nodes": [],
            "fps": 30, "durationFrames": 0, "warnings": [], "preview": {"state": "empty"},
            "tracks": [], "selection": {"ids": [], "playhead": 0}, "permissions": [],
        }
        announced["snapshotToken"] = snapshot_token(announced)
        fetched_at_b = deepcopy(announced)
        fetched_at_b["selection"] = {"ids": ["b"], "playhead": 0}
        fetched_at_b["snapshotToken"] = snapshot_token(fetched_at_b)
        after_selection_returns_to_a = deepcopy(announced)
        after_selection_returns_to_a["revisions"] = {**revisions, "document": "d2"}
        self.service._announced_snapshot = announced
        self.service.document = SimpleNamespace(snapshot=lambda: after_selection_returns_to_a)
        self.events.clear()

        response = self.service._changed(
            after_selection_returns_to_a,
            prepare_preview=False,
            delta_base=revisions,
            delta_snapshot_token=fetched_at_b["snapshotToken"],
        )

        self.assertNotIn("type", response)
        self.assertEqual(response["selection"]["ids"], [])
        self.assertTrue(any(args[0] == "document.changed" for args, _ in self.events))
        self.service.document = None

    def test_delta_response_is_opt_in_to_native_manual_apply_only(self):
        before = {
            "documentId": "doc", "revisions": {"document": "d1", "library": "l1", "renderer": "r1"},
            "plan": {"segments": []}, "nodes": [], "fps": 30, "durationFrames": 0,
            "warnings": [], "preview": {"state": "empty"}, "tracks": [],
        }
        before["selection"] = dict(self.service.selection)
        before["permissions"] = list(self.service.permissions.values())
        before["snapshotToken"] = snapshot_token(before)
        after = deepcopy(before)
        after["revisions"]["document"] = "d2"
        after["plan"]["note"] = "saved"
        self.service._announced_snapshot = before
        self.service.document = SimpleNamespace(apply=lambda _batch: after)
        self.service.shutdown_requested.set()  # avoid the delayed preview in this protocol test
        params = {
            "batch": {
                "expected": before["revisions"],
                "responseMode": "delta",
                "baseRevisions": before["revisions"],
                "baseSnapshotToken": before["snapshotToken"],
                "operations": [{"op": "set_field", "path": "/note", "value": "saved"}],
            }
        }

        result = self.service.dispatch("document.apply", params, source="native")
        self.assertEqual(result["type"], "document.delta")
        self.assertFalse(any(args[0] == "document.changed" for args, _ in self.events))

        self.events.clear()
        full = self.service.dispatch("document.apply", {
            "batch": {
                "expected": after["revisions"],
                "operations": [{"op": "set_field", "path": "/note", "value": "saved"}],
            }
        }, source="native")
        self.assertNotIn("type", full)
        self.assertTrue(any(args[0] == "document.changed" for args, _ in self.events))
        self.service.document = None

    def test_library_revision_change_invalidates_search_and_emits_fresh_revision(self):
        current = ["library-before"]
        self.workspace.library_revision = lambda: current[0]
        self.service._observed_library_revision = current[0]
        self.events.clear()

        current[0] = "library-after"
        self.service._check_library_revision()

        self.assertEqual(self.service._observed_library_revision, "library-after")
        self.assertTrue(any(
            args[0] == "library.changed" and args[1]["libraryRevision"] == "library-after"
            for args, _ in self.events
        ))
        self.assertIsNone(self.service._index)

    def test_library_producer_refreshes_after_its_write(self):
        calls = []
        self.workspace.invalidate_library_revision = lambda: calls.append("invalidate")
        self.workspace.refresh_library_revision = lambda: calls.append("refresh") or "fresh-library"
        self.events.clear()

        revision = self.service._refresh_library_inputs()

        self.assertEqual(revision, "fresh-library")
        self.assertEqual(calls, ["invalidate", "refresh"])
        self.assertTrue(any(
            args[0] == "library.changed" and args[1]["libraryRevision"] == "fresh-library"
            for args, _ in self.events
        ))

    def test_invalid_plan_symlink_not_listed(self):
        external = self.fixture.root / "outside.json"
        external.write_text("{}")
        (self.fixture.project.plans_dir / "escape.json").symlink_to(external)
        self.assertEqual(self.service.dispatch("plans.list", {})["plans"], [])


class StdioServiceTests(unittest.TestCase):
    def test_actual_launcher_keeps_stdout_protocol_only_and_reopens_workspace(self):
        fixture = TempProject(self)
        fixture.asset("fixture")
        fixture.plan("fixture", [{"asset": "fixture", "in": 0, "out": 2}])
        root = Path(__file__).resolve().parents[1]
        # Exercise the real shell launcher on a throwaway repo (the repo folder IS
        # the workspace now): `./reels editor serve` with no workspace option.
        launch_root = fixture.root
        (launch_root / "remotion").mkdir()
        (launch_root / ".venv/bin").mkdir(parents=True)
        shutil.copyfile(root / "reels", launch_root / "reels")
        (launch_root / "reels").chmod(0o700)
        (launch_root / "scripts").symlink_to(root / "scripts", target_is_directory=True)
        python = launch_root / ".venv/bin/python"
        python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
        python.chmod(0o700)
        process = subprocess.Popen([str(launch_root / "reels"), "editor", "serve"],
                                   cwd=launch_root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        messages = queue.Queue()
        def read():
            for line in process.stdout:
                try:
                    messages.put(json.loads(line))
                except ValueError:
                    messages.put({"invalidProtocol": line})
        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        def failure_details():
            # A service that crashed at startup looks like a silent timeout; show why.
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            return f"exit={process.returncode}\nstderr:\n{process.stderr.read()[-4000:]}"
        def request(rid, method, params, timeout=60):
            process.stdin.write(json.dumps({"version": 1, "id": rid, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            for _ in range(100):
                try:
                    message = messages.get(timeout=timeout)
                except queue.Empty:
                    self.fail(f"No response to {method} within {timeout}s; {failure_details()}")
                self.assertNotIn("invalidProtocol", message)
                if message.get("id") == rid:
                    return message
            self.fail("No response to request")
        try:
            info = request("info", "workspace.info", {})["result"]
            self.assertEqual(info["dataRoot"], str(launch_root.resolve()))
            self.assertTrue(info["live"])
            plans = request("plans", "plans.list", {})["result"]["plans"]
            self.assertEqual(plans[0]["path"], "plans/fixture.json")
            request("state", "state.save", {"panels": {"library": 250}})
            request("stop", "shutdown", {})
            process.stdin.close()
            process.wait(timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr.read())
            state = json.loads((launch_root / ".cache/editor/live/state.json").read_text())
            self.assertEqual(state["panels"], {"library": 250})
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
            for stream in (process.stdin, process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
            reader.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
