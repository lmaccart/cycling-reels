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
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from helpers import TempProject
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

    def test_automatic_and_native_preview_requests_coalesce_instead_of_cancelling_each_other(self):
        entered, release = threading.Event(), threading.Event()
        calls = []
        snapshot = {"documentId": "d", "revisions": {"document": "d", "library": "l", "renderer": "r"},
                    "plan": {"slug": "fixture"}, "preview": {}}
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

    def test_preview_reservation_and_submission_are_one_coalescing_step(self):
        from concurrent.futures import ThreadPoolExecutor
        from unittest.mock import patch

        submitting, release_submit, duplicate = threading.Event(), threading.Event(), threading.Event()
        release_worker = threading.Event()
        snapshot = {"documentId": "d", "revisions": {"document": "d", "library": "l", "renderer": "r"},
                    "plan": {"slug": "fixture"}}
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
                    "plan": {"slug": "fixture"}}
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

    def test_invalid_plan_symlink_not_listed(self):
        external = self.fixture.root / "outside.json"
        external.write_text("{}")
        (self.fixture.project.plans_dir / "escape.json").symlink_to(external)
        self.assertEqual(self.service.dispatch("plans.list", {})["plans"], [])


class StdioServiceTests(unittest.TestCase):
    def test_actual_launcher_keeps_stdout_protocol_only_and_reopens_workspace(self):
        from reels.editor.workspace import init_workspace
        fixture = TempProject(self)
        fixture.asset("fixture")
        fixture.plan("fixture", [{"asset": "fixture", "in": 0, "out": 2}])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(__file__).resolve().parents[1]
        workspace = init_workspace(fixture.root, Path(temp.name) / "workspace", code_root=root)
        # Exercise the real shell launcher without depending on an installed
        # desktop app or the production checkout's Python environment.
        launch_root = Path(temp.name) / "launcher"
        (launch_root / ".venv/bin").mkdir(parents=True)
        shutil.copyfile(root / "reels", launch_root / "reels")
        (launch_root / "reels").chmod(0o700)
        (launch_root / "scripts").symlink_to(root / "scripts", target_is_directory=True)
        python = launch_root / ".venv/bin/python"
        python.write_text(f"#!/bin/sh\nexec {shlex.quote(sys.executable)} \"$@\"\n")
        python.chmod(0o700)
        process = subprocess.Popen([str(launch_root / "reels"), "--editor-workspace", str(workspace.root), "editor", "serve"],
                                   cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
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
        def request(rid, method, params):
            process.stdin.write(json.dumps({"version": 1, "id": rid, "method": method, "params": params}) + "\n")
            process.stdin.flush()
            for _ in range(100):
                message = messages.get(timeout=10)
                self.assertNotIn("invalidProtocol", message)
                if message.get("id") == rid:
                    return message
            self.fail("No response to request")
        try:
            info = request("info", "workspace.info", {})["result"]
            self.assertEqual(info["dataRoot"], str(workspace.data_root))
            plans = request("plans", "plans.list", {})["result"]["plans"]
            self.assertEqual(plans[0]["path"], "plans/fixture.json")
            request("state", "state.save", {"panels": {"library": 250}})
            request("stop", "shutdown", {})
            process.stdin.close()
            process.wait(timeout=10)
            self.assertEqual(process.returncode, 0, process.stderr.read())
            from reels.editor.workspace import Workspace
            reopened = Workspace.load(workspace.root, code_root=root)
            self.assertEqual(reopened.data_root, workspace.data_root)
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
