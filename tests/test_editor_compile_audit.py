"""Regressions found during the compiler's pre-integration safety audit."""
from __future__ import annotations

import json
import platform
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import TempProject
from reels.editor.compile_runtime import InputMirror
from reels.editor.compile_worker import WorkerRejected, WorkerUnavailable
from reels.editor.transactions import DocumentError, DocumentStore, _worker_compile


class ScratchAuditTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="compile-audit-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.source = root / "source"
        self.source.mkdir()
        self.asset = self.source / "library/assets/a.json"
        self.asset.parent.mkdir(parents=True)
        self.asset.write_text('{"id":"a"}')
        self.private = root / "worker/private"
        self.mirror = InputMirror(self.source, self.private)
        self.mirror.sync()

    def test_renderer_created_inputs_and_caches_are_pruned_before_reuse(self):
        extra = self.private / "library/assets/rogue.json"
        extra.write_text('{"id":"rogue"}')
        cache = self.private / ".cache/renderer"
        cache.mkdir(parents=True)
        (cache / "stale.json").write_text("{}")
        self.mirror.sync()
        self.assertFalse(extra.exists())
        self.assertFalse(cache.exists())
        self.assertEqual((self.private / "library/assets/a.json").read_bytes(), self.asset.read_bytes())
        self.assertFalse((self.source / "library/assets/rogue.json").exists())

    def test_hardlinked_destination_is_rejected_without_modifying_source(self):
        target = self.private / "library/assets/a.json"
        target.unlink()
        target.hardlink_to(self.asset)
        before = self.asset.read_bytes()
        with self.assertRaisesRegex(ValueError, "hardlink"):
            self.mirror.sync()
        with self.assertRaisesRegex(ValueError, "unsafe"):
            self.mirror.apply({"library/assets/a.json": {"id": "changed"}})
        self.assertEqual(self.asset.read_bytes(), before)

    def test_symlink_destination_is_rejected_without_following_it(self):
        target = self.private / "library/assets/a.json"
        target.unlink()
        target.symlink_to(self.asset)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.mirror.sync()
        self.assertEqual(self.asset.read_text(), '{"id":"a"}')

    def test_replaced_scratch_root_is_never_traversed_or_pruned(self):
        precious = self.source / "precious.txt"
        precious.write_text("keep")
        shutil.rmtree(self.private)
        self.private.symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "scratch root"):
            self.mirror.sync()
        with self.assertRaisesRegex(ValueError, "scratch root"):
            self.mirror.verify()
        self.assertEqual(precious.read_text(), "keep")
        self.assertEqual(self.asset.read_text(), '{"id":"a"}')

    def test_post_build_verification_detects_overwrites_and_added_inputs(self):
        self.mirror.verify()
        self.asset.write_text('{"id":"changed"}')
        with self.assertRaisesRegex(ValueError, "changed during validation"):
            self.mirror.verify()
        self.mirror.sync()
        (self.asset.parent / "new.json").write_text("{}")
        with self.assertRaisesRegex(ValueError, "changed during validation"):
            self.mirror.verify()


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("sandbox-exec"), "macOS sandbox required")
class TransactionAuditTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.fixture.asset("a")
        self.fixture.asset("b")
        self.path = self.fixture.plan(
            "audit", [{"asset": "a", "in": 0, "out": 5},
                      {"asset": "b", "in": 1, "out": 6, "transition": "fade"}],
            text=[{"text": "follow second", "at": 8, "for": 1}],
        )
        self.workspace = SimpleNamespace(project=self.fixture.project, root=self.fixture.root)

    def store(self):
        store = DocumentStore(self.workspace, "plans/audit.json")
        self.addCleanup(store.close)
        return store

    def test_cancellation_racing_failure_or_rejection_never_falls_back_or_publishes(self):
        store = self.store()
        before = store.snapshot()
        nodes = [node for node in before["nodes"] if node["kind"] == "segment"]
        operations = [
            {"op": "reorder_segments", "ids": [nodes[1]["id"]], "beforeId": nodes[0]["id"]},
            {"op": "set_field", "id": nodes[0]["id"], "path": "/volume", "value": 0.4},
        ]
        for operation in operations:
            for error in (WorkerUnavailable("crashed"), WorkerRejected("rejected")):
                def fail(*args, **kwargs):
                    store.cancel_pending("manual")
                    raise error
                head = store.head_path.read_bytes()
                with (self.subTest(operation=operation["op"], error=type(error).__name__),
                      patch.object(store._warm_pool, "compile", side_effect=fail),
                      patch("reels.editor.transactions._worker_compile",
                            side_effect=AssertionError("cancelled validation fell back")) as cold):
                    with self.assertRaises(DocumentError) as caught:
                        store.apply({"expected": before["revisions"], "origin": "manual",
                                     "operations": [operation]})
                    self.assertEqual(caught.exception.code, "cancelled")
                    self.assertEqual(cold.call_count, 0)
                    self.assertEqual(store.head_path.read_bytes(), head)

    def test_generated_cue_after_timing_uses_fully_validated_cold_fallback(self):
        root = self.fixture.root
        (root / "sfx").mkdir()
        (root / "sfx/click.wav").write_bytes(b"fixture sound")
        (root / "sfx/click.json").write_text('{"slug":"click","duration":0.1}')
        (root / "config.json").write_text(json.dumps({
            "sfx": {"autoRules": {"transition": "click", "textBeat": "click"}},
        }))
        raw = json.loads(self.path.read_text())
        raw["autoSfx"] = True
        self.path.write_text(json.dumps(raw))
        store = self.store()
        before = store.snapshot()
        segment = [node for node in before["nodes"] if node["kind"] == "segment"][1]
        cue = next(node for node in before["nodes"] if node["kind"] == "generated_sfx"
                   and node["raw"]["source"] == "auto: transition")
        head = store.head_path.read_bytes()
        def validate(*args, **kwargs):
            self.assertEqual(store.head_path.read_bytes(), head)
            return _worker_compile(*args, **kwargs)
        with patch("reels.editor.transactions._worker_compile", side_effect=validate):
            result = store.apply({
                "expected": before["revisions"], "origin": "manual",
                "operations": [
                    {"op": "trim_segment", "id": segment["id"], "sourceIn": 1, "sourceOut": 4},
                    {"op": "move_overlay", "id": cue["id"], "at": 1},
                ],
            })
        self.assertFalse(result["plan"]["autoSfx"])
        self.assertTrue(any(item["at"] == 1 for item in result["plan"]["sfx"]))
