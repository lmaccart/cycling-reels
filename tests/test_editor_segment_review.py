"""Regression coverage for persisted disable state and segment-edit edge cases."""
from __future__ import annotations

import threading
import unittest
from copy import deepcopy
from itertools import permutations
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import TempProject, words
from test_editor_segment_ops import OpsBase, SEQUENTIAL, header, placed
from reels.editor.compile_runtime import serialize
from reels.editor.preview import PreviewManager
from reels.editor.service import EditorService
from reels.editor.transactions import DocumentStore
from reels.render import build, check_plan, load_plan
from reels.util import write_json


def render_timeline(project, plan, *, aligned=False):
    compiled = serialize(build(project, deepcopy(plan), aligned=aligned))
    return {key: compiled[key] for key in ("fps", "total", "segs", "props", "sound")}


class ReviewOpsTests(OpsBase):
    def test_saved_disabled_events_match_editor_render_and_check(self):
        store = self.store(SEQUENTIAL, **self.global_events(3.0))
        middle = self.clips(store.snapshot())[1]
        off = self.apply(store, [{"op": "set_enabled", "ids": [middle["id"]], "enabled": False}])
        disk = load_plan(self.temp.project, self.path)
        self.assertNotIn("_editor", disk)
        self.assertEqual(render_timeline(self.temp.project, disk), render_timeline(self.temp.project, off["plan"]))
        checked = []
        def compile_check(*args, **kwargs):
            result = build(*args, **kwargs)
            checked.append(serialize(result))
            return result
        with patch("reels.render.build", side_effect=compile_check), patch("reels.render.log"), patch("reels.render.warn"):
            check_plan(self.temp.project, self.path)
        self.assertEqual(checked[0]["props"], render_timeline(self.temp.project, off["plan"], aligned=True)["props"])
        # No sidecar/history: the saved plan alone must retain restoration information.
        store.close()
        fresh_workspace = SimpleNamespace(project=self.temp.project, root=self.temp.root / "fresh-history")
        fresh = DocumentStore(fresh_workspace, self.path)
        self.addCleanup(fresh.close)
        owner = self.clips(fresh.snapshot())[1]
        on = self.apply(fresh, [{"op": "set_enabled", "ids": [owner["id"]], "enabled": True}])
        self.assertEqual(on["plan"]["text"], [{"text": "global words", "at": 3.0, "for": 0.5}])

    def test_suspended_events_restore_source_time_after_speed_and_trim(self):
        for change in ("speed", "trim", "both"):
            with self.subTest(change=change):
                events = self.global_events(5.0)
                events["exhibits"][0]["for"] = 1.0
                path = self.temp.plan(f"source-{change}", [
                    {"asset": "a", "in": 0, "out": 2}, {"asset": "b", "in": 4, "out": 12},
                    {"asset": "c", "in": 0, "out": 2}], **events)
                store = DocumentStore(self.workspace, path)
                self.addCleanup(store.close)
                owner = self.clips(store.snapshot())[1]
                self.apply(store, [{"op": "set_enabled", "ids": [owner["id"]], "enabled": False}])
                if change in ("trim", "both"):
                    self.apply(store, [{"op": "trim_segment", "id": owner["id"], "sourceIn": 6, "sourceOut": 12}])
                if change in ("speed", "both"):
                    self.apply(store, [{"op": "set_field", "id": owner["id"], "path": "/speed", "value": 2}])
                on = self.apply(store, [{"op": "set_enabled", "ids": [owner["id"]], "enabled": True}])
                expected_at = {"speed": 3.5, "trim": 3.0, "both": 2.5}[change]
                for collection in ("text", "sfx", "musicDrops", "exhibits"):
                    self.assertEqual(on["plan"][collection][0]["at"], expected_at)
                self.assertEqual(on["plan"]["text"][0]["for"], 0.25 if change != "trim" else 0.5)
                self.assertEqual(on["plan"]["sfx"][1]["endAt"], expected_at + (0.25 if change != "trim" else 0.5))
                store.close()

    def test_ripple_does_not_treat_disabled_overlaps_as_fade_partners(self):
        for gone_disabled in (False, True):
            with self.subTest(gone_disabled=gone_disabled):
                store = self.store([placed("a", 0, 60, enabled=not gone_disabled),
                                    placed("b", 30, 60, enabled=gone_disabled), placed("c", 120, 30)],
                                   timeline=header(150))
                owner = self.clips(store.snapshot())[0]
                self.assertRejected(store, {"op": "ripple_remove", "ids": [owner["id"]]}, "crosses the removed range")
                store.close()

    def test_positioned_paste_uses_actual_moment_durations(self):
        self.temp.asset("b", duration=30, tags={"moments": [{"id": "m-long", "in": 4, "out": 10}]})
        store = self.store([placed("a", 0, 30)], timeline=header(30))
        for first, expected in (({"asset": "b", "moment": "m-long"}, [(30, 210), (210, 240)]),
                                ({"asset": "b", "moment": "m-long", "speed": 2}, [(30, 120), (120, 150)]),
                                ({"asset": "b", "moment": "m-long", "in": 6, "out": 10}, [(30, 150), (150, 180)])):
            with self.subTest(value=first):
                before = store.snapshot()
                after = self.apply(store, [{"op": "paste_segments", "values": [first, {"asset": "c", "in": 0, "out": 1}],
                                           "atFrame": 30}])
                self.assertEqual([(n["startFrame"], n["endFrame"]) for n in self.clips(after)[1:]], expected)
                self.assert_round_trip(store, before, after)
                store.undo(store.snapshot()["revisions"])

    def test_duplicate_keeps_scalar_child_tracks_by_locator(self):
        store = self.store(SEQUENTIAL)
        before = store.snapshot()
        owner = self.clips(before)[1]
        text = next(n for n in self.children(before, owner) if n["kind"] == "text")
        self.apply(store, [{"op": "add_track", "kind": "video"},
                           {"op": "set_node_track", "id": text["id"], "track": "V4"}])
        before = store.snapshot()
        after = self.apply(store, [{"op": "duplicate_segments", "ids": [owner["id"]]}])
        copied = self.clips(after)[2]
        child = next(n for n in self.children(after, copied) if n["kind"] == "text")
        self.assertEqual(child["track"], "V4")
        self.assertNotEqual(child["id"], text["id"])
        self.assert_round_trip(store, before, after)

    def test_disable_edit_sequences_preserve_never_disabled_render_invariant(self):
        # Every ordering of these actions with disable before enable, with undo/redo at each edit.
        # Reload starts a fresh history from disk: no in-memory or sidecar suspension may be required.
        actions = ("disable", "trim", "speed", "reload", "enable")
        orders = [order for order in permutations(actions) if order.index("disable") < order.index("enable")]
        events = self.global_events(5.0)
        events["exhibits"][0]["for"] = 1.0
        segments = [{"asset": "a", "in": 0, "out": 2},
                    {"asset": "b", "in": 4, "out": 12,
                     "text": [{"text": "owned", "at": 3.0, "for": 1.0}],
                     "sfx": [{"sound": "click", "at": 3.0}],
                     "cutaways": [{"asset": "d", "in": 2, "out": 3, "at": 3.0}]},
                    {"asset": "c", "in": 0, "out": 2}]
        for case, order in enumerate(orders):
            with self.subTest(order=order):
                paths = [self.temp.plan(f"invariant-{case}-{side}", segments, **events) for side in ("control", "disabled")]
                stores = [DocumentStore(self.workspace, path) for path in paths]
                for store in stores:
                    self.addCleanup(store.close)
                try:
                    for action in order:
                        for side in range(2):
                            store = stores[side]
                            if action == "reload":
                                saved = load_plan(self.temp.project, paths[side])
                                self.assertNotIn("_editor", saved)
                                self.assertEqual(render_timeline(self.temp.project, saved),
                                                 render_timeline(self.temp.project, store.snapshot()["plan"]))
                                store.close()
                                root = self.temp.root / f"reloaded-{case}-{side}"
                                stores[side] = DocumentStore(SimpleNamespace(project=self.temp.project, root=root), paths[side])
                                self.addCleanup(stores[side].close)
                                continue
                            if side == 0 and action in ("disable", "enable"):
                                continue
                            before = store.snapshot()
                            owner = next(n for n in self.clips(before) if n["assetId"] == "b")
                            if action in ("disable", "enable"):
                                operation = {"op": "set_enabled", "ids": [owner["id"]], "enabled": action == "enable"}
                            elif action == "trim":
                                operation = {"op": "trim_segment", "id": owner["id"], "sourceIn": 6, "sourceOut": 12}
                            else:
                                operation = {"op": "set_field", "id": owner["id"], "path": "/speed", "value": 2}
                            after = self.apply(store, [operation], before)
                            self.assert_round_trip(store, before, after)
                    expected = render_timeline(self.temp.project, stores[0].snapshot()["plan"])
                    actual = render_timeline(self.temp.project, stores[1].snapshot()["plan"])
                    self.assertEqual(actual, expected)
                    self.assertEqual(render_timeline(self.temp.project, load_plan(self.temp.project, paths[1])), expected)
                finally:
                    for store in stores:
                        store.close()


class ReviewPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.events = []
        self.workspace = SimpleNamespace(root=self.temp.root, data_root=self.temp.root,
                                         code_root=Path(__file__).resolve().parents[1], project=self.temp.project,
                                         media_status=lambda: [], library_revision=lambda: "l")
        self.service = EditorService(self.workspace, lambda *a, **k: self.events.append((a, k)))
        self.addCleanup(self.service.close)

    def test_all_disabled_preview_clears_previous_urls_and_cancels_pending_generation(self):
        snapshot = {"documentId": "d", "revisions": {"document": "d", "library": "l", "renderer": "r"},
                    "plan": {"segments": [{"asset": "a", "enabled": False}]}}
        self.service.document = SimpleNamespace(snapshot=lambda: snapshot)
        self.service.preview = {"state": "ready", "url": "http://old-preview", "movieURL": "http://old-movie"}
        # A real queued job is held until after the empty request; it must never publish old footage.
        entered, release = threading.Event(), threading.Event()
        published = []
        def pending(context):
            entered.set()
            release.wait(5)
            return {"url": "http://late-preview"}
        job = self.service.jobs.submit("preview", snapshot["revisions"], pending, published.append)
        try:
            self.assertTrue(entered.wait(2))
            for mode in ("interactive", "exact"):
                result = self.service.dispatch("preview.prepare", {"mode": mode})
                self.assertEqual(result["state"], "empty")
                self.assertEqual(self.service.snapshot()["preview"]["state"], "empty")
                self.assertNotIn("url", self.service.preview)
                self.assertNotIn("movieURL", self.service.preview)
            self.assertTrue(self.service.jobs.contexts[job["id"]].cancel.is_set())
        finally:
            release.set()
        self.service.close()  # join the cancelled worker before asserting it never published
        self.assertEqual(published, [])
        self.assertTrue(any(a[0] == "preview.ready" and a[1].get("state") == "empty" for a, _ in self.events))

    def test_preview_manager_all_disabled_returns_empty_without_staging(self):
        snapshot = {"revisions": {"document": "d", "library": "l", "renderer": "r"},
                    "plan": {"segments": [{"asset": "a", "enabled": False}]}}
        manager = PreviewManager(self.workspace)
        for mode in ("interactive", "exact"):
            result = manager.prepare(snapshot, mode=mode)
            self.assertEqual(result["state"], "empty")
            self.assertNotIn("url", result)


class ReviewCueTests(unittest.TestCase):
    def test_transition_cues_use_enabled_adjacency_in_aligned_and_compact_builds(self):
        temp = TempProject(self)
        temp.asset("a", words=words(("hello", 0.1, 0.4)))
        temp.asset("b")
        temp.asset("c")
        sounds = temp.root / "sfx"
        sounds.mkdir(exist_ok=True)
        (sounds / "swish.wav").write_bytes(b"sound fixture")
        write_json(sounds / "swish.json", {"slug": "swish", "duration": 0.2})
        temp.project.config["sfx"]["autoRules"] = {"transition": "swish"}
        path = temp.plan("adjacency", [{"asset": "a", "in": 0, "out": 3, "captions": "transcript"},
                                       {"asset": "b", "in": 0, "out": 1, "enabled": False},
                                       {"asset": "c", "in": 0, "out": 3, "transition": "fade"}], autoSfx=True)
        plan = load_plan(temp.project, path)
        compact = build(temp.project, deepcopy(plan))
        aligned = build(temp.project, deepcopy(plan), aligned=True)
        self.assertEqual(compact["sound"]["cues"], [])
        self.assertEqual(aligned["sound"]["cues"], compact["sound"]["cues"])
