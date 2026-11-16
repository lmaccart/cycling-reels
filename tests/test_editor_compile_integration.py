from __future__ import annotations

import json
import platform
import shutil
import subprocess
import sys
import threading
import unittest
from copy import copy, deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import TempProject, live_workspace
from reels.editor.compile_worker import WarmCompilePool, WorkerCancelled, WorkerUnavailable
from reels.editor.transactions import DocumentStore, DocumentError, _worker_compile


SANDBOX = platform.system() == "Darwin" and shutil.which("sandbox-exec")


@unittest.skipUnless(SANDBOX, "trusted macOS sandbox required")
class WarmCompileIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.fixture.asset("a")
        self.fixture.asset("b")
        self.path = self.fixture.plan("case", [
            {"asset": "a", "in": 0, "out": 5},
            {"asset": "b", "in": 1, "out": 6, "transition": "fade"},
        ], text=[{"text": "follow shot two", "at": 8, "for": 1}],
            musicDrops=[{"at": 9, "for": 0.5}])
        self.workspace = SimpleNamespace(project=self.fixture.project, root=self.fixture.root)

    def store(self, workspace=None):
        result = DocumentStore(workspace or self.workspace, "plans/case.json")
        self.addCleanup(result.close)
        return result

    def pool(self, **options):
        pool = WarmCompilePool(self.workspace, **options)
        self.addCleanup(pool.close)
        project = copy(self.fixture.project)
        project.code_root = Path(__file__).resolve().parents[1]
        return pool, project

    def apply(self, store, operations, origin="manual"):
        return store.apply({"expected": store.snapshot()["revisions"],
                            "operations": operations, "origin": origin})

    def test_requests_reuse_one_process_and_restore_override_state(self):
        pool, project = self.pool()
        plan = json.loads(self.path.read_text())
        first = pool.compile(project, plan, {}, "A", "library-A")
        process = pool.workers[0].process
        changed = pool.compile(project, plan, {"config.json": {"render": {"fps": 60}}},
                               "A", "library-A")
        restored = pool.compile(project, plan, {}, "A", "library-A")
        self.assertIs(pool.workers[0].process, process)
        self.assertEqual(first["fps"], restored["fps"])
        self.assertEqual(changed["fps"], 60)
        self.assertEqual(first["total"], restored["total"])

    def test_renderer_switch_and_request_cap_retire_workers(self):
        pool, project = self.pool(max_workers=1, max_requests=1)
        plan = json.loads(self.path.read_text())
        pool.compile(project, plan, {}, "A", "library")
        first = pool.workers[0].process
        pool.compile(project, plan, {}, "A", "library")
        second = pool.workers[0].process
        self.assertNotEqual(first.pid, second.pid)
        self.assertIsNotNone(first.poll())
        pool.compile(project, plan, {}, "B", "library")
        self.assertEqual(pool.workers[0].revision, "B")
        self.assertNotEqual(second.pid, pool.workers[0].process.pid)

    def test_root_set_change_restarts_worker(self):
        pool, project = self.pool(max_workers=1)
        plan = json.loads(self.path.read_text())
        pool.compile(project, plan, {}, "A", "library")
        first = pool.workers[0].process
        (self.fixture.root / "models").mkdir()
        pool.compile(project, plan, {}, "A", "library")
        self.assertNotEqual(first.pid, pool.workers[0].process.pid)

    def test_changed_profile_is_not_hidden_by_profile_serialization_cache(self):
        from reels.editor import preview
        pool, project = self.pool(max_workers=1)
        plan = json.loads(self.path.read_text())
        pool.compile(project, plan, {}, "A", "library")
        first = pool.workers[0].process
        original = preview._profile_text
        with patch.object(preview, "_profile_text", side_effect=lambda *args, **kwargs: (
                original(*args, **kwargs) + "\n")):
            pool.compile(project, plan, {}, "A", "library")
        self.assertNotEqual(first.pid, pool.workers[0].process.pid)
        self.assertIsNotNone(first.poll())

    def test_reorder_and_trim_each_make_one_rpc_and_keep_head_before_validation(self):
        store = self.store()
        before = store.snapshot()
        segments = [node for node in before["nodes"] if node["kind"] == "segment"]
        pool = store._warm_pool
        head = store.head_path.read_bytes()
        original = pool.compile

        def checked(*args, **kwargs):
            self.assertEqual(store.head_path.read_bytes(), head)
            return original(*args, **kwargs)

        with patch.object(pool, "compile", side_effect=checked) as calls:
            result = self.apply(store, [{"op": "reorder_segments", "ids": [segments[1]["id"]],
                                         "beforeId": segments[0]["id"]}])
        self.assertEqual(calls.call_count, 1)
        head = store.head_path.read_bytes()
        with patch.object(pool, "compile", side_effect=checked) as calls:
            self.apply(store, [{"op": "trim_segment", "id": segments[0]["id"],
                                "sourceIn": 1, "sourceOut": 3}])
        self.assertEqual(calls.call_count, 1)
        self.assertNotEqual(before["revisions"]["document"], result["revisions"]["document"])

    def test_mid_request_crash_uses_cold_fallback_and_next_edit_recovers(self):
        store = self.store()
        snapshot = store.snapshot()
        segments = [node for node in snapshot["nodes"] if node["kind"] == "segment"]
        worker = store._warm_pool.workers[0]
        write = worker.process.stdin.write

        def crash(data):
            result = write(data)
            worker.process.kill()
            return result

        with (patch.object(worker.process.stdin, "write", side_effect=crash),
              patch("reels.editor.transactions._worker_compile", wraps=_worker_compile) as cold):
            self.apply(store, [{"op": "reorder_segments", "ids": [segments[1]["id"]],
                                "beforeId": segments[0]["id"]}])
        self.assertGreaterEqual(cold.call_count, 1)
        result = self.apply(store, [{"op": "set_field", "id": segments[0]["id"],
                                    "path": "/volume", "value": 0.4}])
        self.assertEqual(result["plan"]["segments"][1]["volume"], 0.4)
        self.assertTrue(any(not item.closed for item in store._warm_pool.workers))

    def test_cancelled_request_discards_reply_without_killing_the_worker(self):
        pool, project = self.pool()
        plan = json.loads(self.path.read_text())
        pool.compile(project, plan, {}, "A", "library")
        worker = pool.workers[0]
        entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
        original = worker.responses.get
        outcome = []

        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)

        def validate():
            try:
                pool.compile(project, plan, {}, "A", "library", cancelled=cancelled.is_set)
            except Exception as exc:
                outcome.append(exc)

        with patch.object(worker.responses, "get", side_effect=delayed):
            thread = threading.Thread(target=validate)
            thread.start()
            self.assertTrue(entered.wait(2))
            cancelled.set()
            thread.join(2)
            release.set()
            self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome[0], WorkerCancelled)
        self.assertIsNone(worker.process.poll())
        pool.compile(project, plan, {}, "A", "library")
        self.assertIs(pool.workers[0], worker)

    def test_ai_compilation_stays_cold(self):
        store = self.store()
        segment = next(node for node in store.snapshot()["nodes"] if node["kind"] == "segment")
        with (patch.object(store._warm_pool, "compile", side_effect=AssertionError("AI used warm worker")),
              patch("reels.editor.transactions._worker_compile", wraps=_worker_compile) as cold):
            self.apply(store, [{"op": "set_field", "id": segment["id"],
                                "path": "/volume", "value": 0.6}], origin="ai")
        self.assertEqual(cold.call_count, 1)

    def test_stale_library_manifest_is_rejected_and_worker_is_retired(self):
        workspace = live_workspace(self.fixture)
        self.addCleanup(workspace.stop_library_watcher)
        workspace.start_library_watcher(interval=60)
        pool = WarmCompilePool(workspace)
        self.addCleanup(pool.close)
        project = copy(workspace.project)
        project.code_root = workspace.code_root
        revision = workspace.library_revision()
        plan = json.loads(self.path.read_text())
        pool.compile(project, plan, {}, "A", revision)
        # Keep the watcher deliberately asleep to exercise the worker's check,
        # not eventual detection by the broker.
        asset = self.fixture.project.assets_dir / "a.json"
        value = json.loads(asset.read_text())
        value["tags"]["summary"] = "external modification"
        asset.write_text(json.dumps(value))
        with self.assertRaisesRegex(WorkerUnavailable, "expected revision"):
            pool.compile(project, plan, {}, "A", revision)
        self.assertTrue(pool.workers[0].closed)

    def test_timeout_retires_worker_then_next_request_recovers(self):
        pool, project = self.pool(timeout=0.1)
        plan = json.loads(self.path.read_text())
        # Warm imports before using a short fault-injection deadline.
        pool.timeout = 5
        pool.compile(project, plan, {}, "A", "library")
        worker = pool.workers[0]
        worker.timeout = 0.05
        with patch.object(worker.process.stdin, "write", return_value=0):
            with self.assertRaisesRegex(WorkerUnavailable, "deadline"):
                pool.compile(project, plan, {}, "A", "library")
        self.assertTrue(worker.closed)
        self.assertEqual(pool.compile(project, plan, {}, "A", "library")["fps"], 30)

    def test_rejected_candidate_never_changes_head(self):
        store = self.store()
        segment = next(node for node in store.snapshot()["nodes"] if node["kind"] == "segment")
        head = store.head_path.read_bytes()
        with self.assertRaises(DocumentError):
            self.apply(store, [{"op": "set_field", "id": segment["id"],
                                "path": "/speed", "value": -1}])
        self.assertEqual(store.head_path.read_bytes(), head)

    def test_config_materialization_undo_and_redo_invalidate_cached_library(self):
        workspace = live_workspace(self.fixture)
        self.addCleanup(workspace.stop_library_watcher)
        workspace.start_library_watcher(interval=60)
        store = self.store(workspace)
        before = store.snapshot()
        changed = self.apply(store, [{"op": "set_field", "id": "project",
                                     "path": "/render/fps", "value": 60}])
        self.assertEqual(changed["fps"], 60)
        self.assertNotEqual(changed["revisions"]["library"], before["revisions"]["library"])
        self.assertEqual(workspace.library_revision(), changed["revisions"]["library"])
        undone = store.undo(changed["revisions"])
        self.assertEqual(undone["fps"], before["fps"])
        self.assertEqual(workspace.library_revision(), undone["revisions"]["library"])
        redone = store.redo(undone["revisions"])
        self.assertEqual(redone["fps"], 60)
        self.assertEqual(workspace.library_revision(), redone["revisions"]["library"])

    def test_warm_profile_denies_source_writes_and_unregistered_original_reads(self):
        workspace = live_workspace(self.fixture)
        pool = WarmCompilePool(workspace)
        self.addCleanup(pool.close)
        project = copy(workspace.project)
        project.code_root = workspace.code_root
        pool.prewarm(project, "A")
        worker = pool.workers[0]
        unregistered = self.fixture.project.media_dir / "unregistered.mp4"
        unregistered.write_bytes(b"not registered")
        original = self.fixture.project.media_dir / "a.mp4"
        before = original.read_bytes()
        probe = """
import pathlib,sys
for filename in sys.argv[1:3]:
    try:
        pathlib.Path(filename).write_bytes(b'forbidden')
    except PermissionError:
        pass
    else:
        raise AssertionError('source write allowed')
try:
    pathlib.Path(sys.argv[3]).read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError('unregistered read allowed')
pathlib.Path(sys.argv[4]).write_bytes(b'private output')
"""
        result = subprocess.run(
            ["sandbox-exec", "-f", str(worker.scratch / "compile.sb"), sys.executable,
             "-I", "-c", probe, str(self.fixture.root / "config.json"), str(original),
             str(unregistered), str(worker.scratch / "allowed.bin")],
            cwd=worker.scratch, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(original.read_bytes(), before)
        self.assertEqual((worker.scratch / "allowed.bin").read_bytes(), b"private output")

    def test_cache_reopen_hit_corruption_and_library_invalidation(self):
        store = self.store()
        expected = store.snapshot()
        cache = store.history / "COMPILED.json"
        self.assertTrue(cache.is_file())
        store.close()
        with patch.object(WarmCompilePool, "compile", side_effect=AssertionError("unchanged reopen compiled")):
            reopened = self.store()
            self.assertEqual(reopened.snapshot()["durationFrames"], expected["durationFrames"])
            reopened.close()
        cache.write_text("{corrupt")
        with patch.object(WarmCompilePool, "compile", autospec=True,
                          side_effect=WorkerUnavailable("test cold fallback")) as compile_call:
            repaired = self.store()
            self.assertEqual(repaired.snapshot()["durationFrames"], expected["durationFrames"])
            self.assertEqual(compile_call.call_count, 1)
            repaired.close()
        asset = self.fixture.project.assets_dir / "a.json"
        value = json.loads(asset.read_text())
        value["tags"]["summary"] = "changed input"
        asset.write_text(json.dumps(value))
        with patch.object(WarmCompilePool, "compile", autospec=True,
                          side_effect=WorkerUnavailable("test cold fallback")) as compile_call:
            changed = self.store()
            changed.snapshot()
            self.assertEqual(compile_call.call_count, 1)

    def test_document_normalization_abi_change_invalidates_unchanged_disk_cache(self):
        store = self.store()
        before = store.snapshot()
        old_key = json.loads((store.history / "COMPILED.json").read_text())["key"]
        store.close()
        source = Path(__file__).resolve().parents[1] / "scripts/reels/editor/document.py"
        original_read = Path.read_bytes
        original_compile = WarmCompilePool.compile
        def changed_abi(path):
            value = original_read(path)
            return value + b"\n# changed normalization ABI\n" if path == source else value
        with (patch.object(Path, "read_bytes", new=changed_abi),
              patch.object(WarmCompilePool, "compile", autospec=True,
                           side_effect=original_compile) as compiled):
            reopened = self.store()
            after = reopened.snapshot()
            new_key = json.loads((reopened.history / "COMPILED.json").read_text())["key"]
        self.assertEqual(before["revisions"], after["revisions"])
        self.assertEqual(old_key["planHash"], new_key["planHash"])
        self.assertNotEqual(old_key["runner"], new_key["runner"])
        self.assertEqual(compiled.call_count, 1, "ABI-only changes must not promote the old compiled cache")

    def test_offline_cache_is_view_only_and_never_promoted(self):
        workspace = live_workspace(self.fixture)
        self.addCleanup(workspace.stop_library_watcher)
        store = self.store(workspace)
        store.snapshot()
        store.close()
        (self.fixture.project.media_dir / "a.mp4").unlink()
        with patch.object(WarmCompilePool, "compile", side_effect=AssertionError("offline compile")):
            reopened = self.store(workspace)
            result = reopened.snapshot()
            self.assertTrue(result["viewOnly"])
            self.assertIsNone(reopened._compiled_cache)
            with self.assertRaises(DocumentError) as caught:
                self.apply(reopened, [{"op": "set_field", "path": "/hook", "value": "no"}])
            self.assertEqual(caught.exception.code, "media_offline")

    def test_sequential_timing_remap_matches_cold_renderer(self):
        initial = deepcopy(json.loads(self.path.read_text()))
        initial["autoSfx"] = True
        self.path.write_text(json.dumps(initial))
        other = TempProject(self)
        other.asset("a")
        other.asset("b")
        (other.project.plans_dir / "case.json").write_text(json.dumps(initial))
        for root in (self.fixture.root, other.root):
            (root / "sfx").mkdir()
            (root / "sfx/click.wav").write_bytes(b"fixture sound")
            (root / "sfx/click.json").write_text(json.dumps({"slug": "click", "duration": 0.1}))
            (root / "config.json").write_text(json.dumps({
                "sfx": {"autoRules": {"transition": "click", "textBeat": "click"}},
            }))
        fast = self.store()
        cold = DocumentStore(SimpleNamespace(project=other.project, root=other.root), "plans/case.json")
        self.addCleanup(cold.close)

        def operations(snapshot):
            nodes = [node for node in snapshot["nodes"] if node["kind"] == "segment"]
            return [
                {"op": "reorder_segments", "ids": [nodes[1]["id"]], "beforeId": nodes[0]["id"]},
                {"op": "trim_segment", "id": nodes[0]["id"], "sourceIn": 1, "sourceOut": 4},
                {"op": "split_segment", "id": nodes[1]["id"], "atFrame": 60},
                {"op": "set_field", "id": nodes[0]["id"], "path": "/speed", "value": 1.25},
            ]
        result = self.apply(fast, operations(fast.snapshot()))
        reference = self.apply(cold, operations(cold.snapshot()), origin="ai")
        self.assertEqual(json.loads(self.path.read_text()),
                         json.loads((other.project.plans_dir / "case.json").read_text()))
        self.assertEqual(result["durationFrames"], reference["durationFrames"])
        self.assertEqual(result["plan"]["text"], reference["plan"]["text"])
        self.assertEqual(fast._current_compiled["props"], cold._current_compiled["props"])
        self.assertTrue(any(cue["source"].startswith("auto:") for cue in fast._current_compiled["sound"]["cues"]))
        self.assertEqual(fast._current_compiled["sound"]["cues"], cold._current_compiled["sound"]["cues"])
