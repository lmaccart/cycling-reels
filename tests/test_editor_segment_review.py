"""Regression coverage for derived disabled rendering and segment-edit edge cases."""
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
from reels.editor.document import editing_snapshot, ensure_node_ids
from reels.editor.preview import PreviewManager
from reels.editor.service import EditorService
from reels.editor.transactions import DocumentStore, _worker_compile
from reels.render import build, check_plan, load_plan
from reels.util import write_json


def render_timeline(project, plan, *, aligned=False):
    compiled = serialize(build(project, deepcopy(plan), aligned=aligned))
    return {key: compiled[key] for key in ("fps", "total", "segs", "props", "sound")}


def assert_playback_bounds(case, project, snapshot):
    """Compare stable node IDs with retained identities from the real compiler."""
    built = build(project, deepcopy(snapshot["plan"]), aligned=True)
    ids = {int(entry["locator"].split("/")[2]): node_id
           for node_id, entry in snapshot["plan"]["_editor"]["nodes"].items()
           if entry["kind"] == "segment"}
    expected = {ids[segment.index]: (segment.start, segment.start + segment.frames)
                for segment in built["segs"]}
    nodes = {node["id"]: node for node in snapshot["nodes"] if node["kind"] == "segment"}
    case.assertEqual(set(nodes), set(ids.values()))
    actual = {}
    for node_id, node in nodes.items():
        if node_id in expected:
            actual[node_id] = (node["playbackStartFrame"], node["playbackEndFrame"])
        else:
            case.assertNotIn("playbackStartFrame", node)
            case.assertNotIn("playbackEndFrame", node)
    case.assertEqual(actual, expected)
    editing = built.get("editing", built)
    case.assertEqual(
        {node_id: (node["startFrame"], node["endFrame"]) for node_id, node in nodes.items()},
        {ids[segment.index]: (segment.start, segment.start + segment.frames) for segment in editing["segs"]},
    )


class ReviewPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        for name in ("a", "b", "c"):
            self.temp.asset(name, duration=30)

    def snapshot(self, segments, **extra):
        plan = load_plan(self.temp.project, self.temp.plan("playback", segments, **extra))
        ensure_node_ids(plan)
        compiled = serialize(build(self.temp.project, deepcopy(plan), aligned=True))
        return editing_snapshot(plan, compiled, {"document": "d", "library": "l", "renderer": "r"})

    def test_playback_bounds_follow_retained_indexes_across_disabled_gaps_and_tails(self):
        for disabled in ((), (0,), (1,), (2,), (0, 1), (1, 2), (0, 2), (0, 1, 2)):
            with self.subTest(disabled=disabled):
                segments = [{"asset": name, "in": 1, "out": 3, **({"enabled": False} if i in disabled else {})}
                            for i, name in enumerate(("a", "b", "c"))]
                snapshot = self.snapshot(segments)
                assert_playback_bounds(self, self.temp.project, snapshot)
                if not disabled:
                    for node in snapshot["nodes"]:
                        if node["kind"] == "segment":
                            self.assertEqual((node["playbackStartFrame"], node["playbackEndFrame"]),
                                             (node["startFrame"], node["endFrame"]))

    def test_playback_bounds_use_actual_fade_trim_and_speed_timing(self):
        for disabled in ((), (1,), (0,)):
            with self.subTest(disabled=disabled):
                segments = [{"asset": "a", "in": 2, "out": 6, "speed": 2},
                            {"asset": "b", "in": 3, "out": 6, "transition": "fade"},
                            {"asset": "c", "in": 4, "out": 10, "speed": 1.5, "transition": "fade"}]
                for i in disabled:
                    segments[i]["enabled"] = False
                assert_playback_bounds(self, self.temp.project, self.snapshot(segments))

    def test_positioned_playback_bounds_are_identity_with_disabled_placements(self):
        for disabled in ((), (1,), (2,), (0, 1, 2)):
            with self.subTest(disabled=disabled):
                segments = [placed("a", 0, 60), placed("b", 90, 60), placed("c", 180, 90)]
                for i in disabled:
                    segments[i]["enabled"] = False
                snapshot = self.snapshot(segments, timeline=header(270))
                assert_playback_bounds(self, self.temp.project, snapshot)
                for node in snapshot["nodes"]:
                    if node["kind"] == "segment" and node.get("enabled") is not False:
                        self.assertEqual((node["playbackStartFrame"], node["playbackEndFrame"]),
                                         (node["startFrame"], node["endFrame"]))

    def test_unsupported_correspondence_is_absent_instead_of_guessed(self):
        plan = load_plan(self.temp.project, self.temp.plan("compact", [
            {"asset": "a", "in": 0, "out": 2},
            {"asset": "b", "in": 0, "out": 2, "enabled": False},
            {"asset": "c", "in": 0, "out": 2},
        ]))
        ensure_node_ids(plan)
        compiled = serialize(build(self.temp.project, deepcopy(plan)))  # output indexes are compacted
        for segment in compiled["segs"]:
            self.assertNotIn("playbackStartFrame", segment)
            self.assertNotIn("playbackEndFrame", segment)
        snapshot = editing_snapshot(plan, compiled, {"document": "d", "library": "l", "renderer": "r"})
        for node in snapshot["nodes"]:
            self.assertNotIn("playbackStartFrame", node)
            self.assertNotIn("playbackEndFrame", node)

    def test_renderer_output_order_does_not_determine_node_correspondence(self):
        plan = load_plan(self.temp.project, self.temp.plan("reordered", [
            {"asset": "a", "in": 0, "out": 2},
            {"asset": "a", "in": 2, "out": 4, "enabled": False},
            {"asset": "a", "in": 4, "out": 6},
        ]))
        ensure_node_ids(plan)
        built = build(self.temp.project, deepcopy(plan), aligned=True)
        built["segs"].reverse()
        snapshot = editing_snapshot(plan, serialize(built), {"document": "d", "library": "l", "renderer": "r"})
        assert_playback_bounds(self, self.temp.project, snapshot)


class ReviewOpsTests(OpsBase):
    def test_playback_bounds_follow_disabled_source_edits_split_and_history(self):
        store = self.store([{"asset": "a", "in": 0, "out": 2},
                            {"asset": "b", "in": 4, "out": 12, "transition": "fade"},
                            {"asset": "c", "in": 0, "out": 2, "transition": "fade"}])
        before = store.snapshot()
        assert_playback_bounds(self, self.temp.project, before)
        owner = self.clips(before)[1]

        def apply_checked(operations):
            before = store.snapshot()
            after = self.apply(store, operations, before)
            assert_playback_bounds(self, self.temp.project, after)
            undone = store.undo(after["revisions"])
            assert_playback_bounds(self, self.temp.project, undone)
            self.assertEqual(self.clips(undone), self.clips(before))
            redone = store.redo(undone["revisions"])
            assert_playback_bounds(self, self.temp.project, redone)
            self.assertEqual(self.clips(redone), self.clips(after))
            return redone

        apply_checked([{"op": "set_enabled", "ids": [owner["id"]], "enabled": False}])
        apply_checked([{"op": "trim_segment", "id": owner["id"], "sourceIn": 6, "sourceOut": 12}])
        sped = apply_checked([{"op": "set_field", "id": owner["id"], "path": "/speed", "value": 2}])
        current = next(node for node in self.clips(sped) if node["id"] == owner["id"])
        split = apply_checked([{"op": "split_segment", "id": owner["id"], "atFrame": current["startFrame"] + 30}])
        left, right = self.clips(split)[1:3]
        self.assertNotEqual(left["id"], right["id"])
        apply_checked([{"op": "set_enabled", "ids": [right["id"]], "enabled": True}])
        apply_checked([{"op": "set_enabled", "ids": [left["id"]], "enabled": True}])

    def test_warm_and_cold_serializers_emit_identical_playback_correspondence(self):
        cases = [(disabled, False) for disabled in ((), (0,), (1,), (2,), (0, 1, 2))]
        cases += [((1,), True), ((0, 1, 2), True)]
        for disabled, positioned in cases:
            with self.subTest(disabled=disabled, positioned=positioned):
                segments = [{"asset": "a", "in": 2, "out": 6, "speed": 2},
                            {"asset": "b", "in": 3, "out": 6, "transition": "fade"},
                            {"asset": "c", "in": 4, "out": 10, "speed": 1.5, "transition": "fade"}]
                extra = {}
                if positioned:
                    segments = [placed("a", 0, 60), placed("b", 90, 90), placed("c", 210, 120)]
                    extra["timeline"] = header(330)
                for i in disabled:
                    segments[i]["enabled"] = False
                plan = load_plan(self.temp.project, self.temp.plan("serializer-parity", segments, **extra))
                ensure_node_ids(plan)
                warm = serialize(build(self.temp.project, deepcopy(plan), aligned=True))
                cold = _worker_compile(self.temp.project, deepcopy(plan), {})
                self.assertEqual(warm["segs"], cold["segs"])
                for compiled in (warm, cold):
                    snapshot = editing_snapshot(plan, compiled, {"document": "d", "library": "l", "renderer": "r"})
                    assert_playback_bounds(self, self.temp.project, snapshot)

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

    def test_disabled_events_follow_the_same_source_edits_as_enabled_events(self):
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

    def test_split_while_disabled_keeps_events_with_the_correct_source_half(self):
        store = self.store([{"asset": "a", "in": 0, "out": 2},
                            {"asset": "b", "in": 4, "out": 12},
                            {"asset": "c", "in": 0, "out": 2}], **self.global_events(7.0))
        before = store.snapshot()
        owner = self.clips(before)[1]
        off = self.apply(store, [{"op": "set_enabled", "ids": [owner["id"]], "enabled": False}])
        # The flag is the only public plan mutation; no hidden ownership state exists.
        expected = deepcopy(before["plan"])
        expected["segments"][1]["enabled"] = False
        self.assertEqual({k: v for k, v in off["plan"].items() if k != "_editor"},
                         {k: v for k, v in expected.items() if k != "_editor"})
        self.assertEqual(self.layout(off), self.layout(before))
        split = self.apply(store, [{"op": "split_segment", "id": owner["id"], "atFrame": 180}])
        self.assert_round_trip(store, off, split)
        left, right = self.clips(split)[1:3]
        self.assertNotEqual(left["id"], right["id"])
        self.assertEqual((left["sourceOut"], right["sourceIn"]), (8, 8))
        first_on = self.apply(store, [{"op": "set_enabled", "ids": [left["id"]], "enabled": True}])
        self.assertFalse(store._current_compiled["props"].get("exhibits"))
        self.assertEqual(first_on["plan"]["exhibits"][0]["at"], 7.0)
        # The event lies in the right half, which becomes reel time 3 after dropping the left.
        self.apply(store, [{"op": "set_enabled", "ids": [left["id"]], "enabled": False},
                           {"op": "set_enabled", "ids": [right["id"]], "enabled": True}])
        shown = store._current_compiled["props"]["exhibits"][0]
        self.assertEqual(shown["startFrame"], 90)
        on = self.apply(store, [{"op": "set_enabled", "ids": [left["id"]], "enabled": True}])
        self.assertEqual(on["plan"]["exhibits"], before["plan"]["exhibits"])
        self.assertEqual(store._current_compiled["props"]["exhibits"][0]["startFrame"], 210)

    def test_positioned_conversion_and_speed_are_identical_while_disabled(self):
        results = []
        for disabled in (False, True):
            store = self.store([{"asset": "a", "in": 0, "out": 2},
                                {"asset": "b", "in": 4, "out": 12},
                                {"asset": "c", "in": 0, "out": 2}], **self.global_events(5.0))
            before = store.snapshot()
            owner, last = self.clips(before)[1:]
            if disabled:
                self.apply(store, [{"op": "set_enabled", "ids": [owner["id"]], "enabled": False}])
            converted = self.apply(store, [{"op": "move_segments", "mode": "overwrite",
                                           "editId": "a0000000-0000-4000-8000-000000000001",
                                           "placements": [{"id": last["id"], "track": "V2", "atFrame": 300}]}])
            sped = self.apply(store, [{"op": "set_field", "id": owner["id"], "path": "/speed", "value": 2}])
            self.assert_round_trip(store, converted, sped)
            if disabled:
                self.apply(store, [{"op": "set_enabled", "ids": [owner["id"]], "enabled": True}])
            results.append(render_timeline(self.temp.project, store.snapshot()["plan"]))
            store.close()
        self.assertEqual(results[0], results[1])

    def test_cold_ai_disable_retimes_a_late_exhibit_only_in_the_render_plan(self):
        store = self.store(SEQUENTIAL, **self.global_events(5.0))
        before = store.snapshot()
        result = store.apply({"expected": before["revisions"], "origin": "ai",
                              "operations": [{"op": "set_enabled", "ids": [self.clips(before)[0]["id"]],
                                              "enabled": False}]})
        self.assertEqual(result["plan"]["exhibits"][0]["at"], 5.0)
        self.assertEqual(result["durationFrames"], 120)
        self.assertEqual(result["editingDurationFrames"], 180)
        self.assertEqual(store._current_compiled["props"]["exhibits"][0]["startFrame"], 90)
        self.assert_round_trip(store, before, result)
        # A shortening edit also needs timing-only compilation before global-event validation.
        current = store.snapshot()
        owner = self.clips(current)[1]
        trimmed = store.apply({"expected": current["revisions"], "origin": "ai",
                               "operations": [{"op": "trim_segment", "id": owner["id"],
                                               "sourceIn": 0, "sourceOut": 1}]})
        self.assertEqual(trimmed["plan"]["exhibits"][0]["at"], 4.0)
        self.assertEqual(store._current_compiled["props"]["exhibits"][0]["startFrame"], 60)

    def test_track_and_fps_changes_do_not_extend_render_to_disabled_endpoints(self):
        store = self.store([placed("a", 0, 60), placed("b", 120, 90, enabled=False)], timeline=header(60))
        before = store.snapshot()
        moved = self.apply(store, [{"op": "set_node_track", "id": self.clips(before)[0]["id"], "track": "V2"}])
        self.assertEqual((moved["durationFrames"], moved["editingDurationFrames"]), (60, 210))
        self.assertEqual(moved["plan"]["timeline"]["durationFrames"], 60)
        self.assert_round_trip(store, before, moved)
        changed = self.apply(store, [{"op": "set_field", "id": "project", "path": "/render/fps", "value": 60}])
        self.assertEqual((changed["fps"], changed["durationFrames"], changed["editingDurationFrames"]), (60, 120, 420))
        self.assertEqual(changed["plan"]["timeline"]["durationFrames"], 120)
        disk = load_plan(self.temp.project, self.path)
        from reels.config import Project
        compact = build(Project(self.temp.root), disk)
        self.assertEqual(compact["total"], 120)
        self.assert_round_trip(store, moved, changed)
        # Enabling an edited clip beyond the render header changes only the flag.
        before_enable = store.snapshot()
        owner = next(n for n in self.clips(before_enable) if n["assetId"] == "b")
        enabled = self.apply(store, [{"op": "set_enabled", "ids": [owner["id"]], "enabled": True}])
        self.assertEqual(enabled["plan"]["timeline"], before_enable["plan"]["timeline"])
        self.assertEqual(enabled["durationFrames"], 420)
        self.assert_round_trip(store, before_enable, enabled)

    def test_all_disabled_cutaways_fit_identically_in_full_and_timing_compilation(self):
        import reels.render as renderer
        from reels.editor.compile_runtime import timing_build
        path = self.temp.plan("all-off-cuts", [{"asset": "a", "in": 0, "out": 2, "enabled": False,
                                               "cutaways": [{"asset": "d", "in": 0, "out": 0.4, "at": 49 / 30}]}])
        plan = load_plan(self.temp.project, path)
        full = serialize(build(self.temp.project, deepcopy(plan), aligned=True))
        timing = timing_build(renderer, self.temp.project, deepcopy(plan))
        for result in (full, timing):
            self.assertEqual((result["total"], result["editingDurationFrames"]), (0, 60))
            self.assertEqual(result["segs"][0]["cutaways"], [])


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
