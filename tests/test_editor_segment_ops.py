"""duplicate_segments, ripple_remove, paste_segments and set_enabled, on sequential and positioned plans,
plus the renderer contract that a disabled segment contributes no frames, video or audio."""
from __future__ import annotations

import platform
import shutil
import unittest
from copy import deepcopy
from types import SimpleNamespace

from helpers import TempProject, words
from reels.editor.operations import OperationBatch, operation_schema
from reels.editor.transactions import DocumentError, DocumentStore
from reels.render import build, check_plan, load_plan
from reels.util import ReelsError, write_json

SANDBOX = platform.system() == "Darwin" and shutil.which("sandbox-exec")


def placed(asset, start, frames, track="V1", out=None, **extra):
    """A positioned (explicit timeline) segment: 30 fps, 1 source second = 30 frames."""
    return {"asset": asset, "in": 0, "out": out if out is not None else frames / 30,
            "timeline": {"track": track, "startFrame": start, "durationFrames": frames, "transitionInFrames": 0},
            **extra}


def header(frames):
    return {"version": 1, "fps": 30, "durationFrames": frames}


class SchemaTests(unittest.TestCase):
    def test_batch_schema_accepts_the_four_ops_and_rejects_bad_shapes(self):
        good = [
            {"op": "duplicate_segments", "ids": ["a"]},
            {"op": "ripple_remove", "ids": ["a", "b"]},
            {"op": "paste_segments", "values": [{"asset": "x"}], "atFrame": 0},
            {"op": "paste_segments", "values": [{"asset": "x"}], "atFrame": 12, "track": "V2"},
            {"op": "set_enabled", "ids": ["a"], "enabled": False},
        ]
        tokens = {"document": "d", "library": "l", "renderer": "r"}
        batch = OperationBatch.model_validate({"expected": tokens, "reason": "x", "operations": good})
        wire = batch.wire()["operations"]
        self.assertNotIn("track", wire[2])  # null track is omitted: "first video track"
        self.assertEqual(wire[4], {"op": "set_enabled", "ids": ["a"], "enabled": False})
        for bad in ({"op": "duplicate_segments", "ids": []},
                    {"op": "ripple_remove"},
                    {"op": "paste_segments", "values": [], "atFrame": 0},
                    {"op": "paste_segments", "values": [{"asset": "x"}], "atFrame": -1},
                    {"op": "paste_segments", "values": [{"asset": "x"}], "atFrame": 0, "track": "A1"},
                    {"op": "set_enabled", "ids": ["a"], "enabled": "yes"},
                    {"op": "set_enabled", "ids": ["a"]}):
            with self.assertRaises(ValueError, msg=bad):
                OperationBatch.model_validate({"expected": tokens, "reason": "x", "operations": [bad]})
        names = {item["properties"]["op"]["const"] for item in operation_schema()["$defs"].values()
                 if "op" in item.get("properties", {}) and "const" in item["properties"]["op"]}
        self.assertTrue({"duplicate_segments", "ripple_remove", "paste_segments", "set_enabled"} <= names)


class RenderTimelineTests(unittest.TestCase):
    """The renderer drops disabled segments everywhere a plan becomes a timeline."""

    def setUp(self):
        self.temp = TempProject(self)
        self.project = self.temp.project
        self.temp.asset("a", duration=30, words=words(("hello", 0.5, 0.9), ("there", 1.0, 1.4)))
        for name in ("b", "c"):
            self.temp.asset(name, duration=30)

    def build(self, segments, **extra):
        path = self.temp.plan("disabled", segments, **extra)
        return build(self.project, load_plan(self.project, path))

    def three(self, **middle):
        return [{"asset": "a", "in": 0, "out": 2}, {"asset": "b", "in": 0, "out": 3, **middle},
                {"asset": "c", "in": 0, "out": 1}]

    def test_disabled_segment_adds_no_frames_and_later_clips_close_up(self):
        full = self.build(self.three())
        off = self.build(self.three(enabled=False))
        self.assertEqual(full["total"], 180)
        self.assertEqual(off["total"], 90)
        self.assertEqual([(s["startFrame"] if "startFrame" in s else None, s["durationInFrames"])
                          for s in off["props"]["segments"]], [(None, 60), (None, 30)])
        self.assertEqual(len(off["segs"]), 2)
        self.assertEqual([sg.asset["id"] for sg in off["segs"]], ["a", "c"])
        self.assertEqual([sg.start for sg in off["segs"]], [0, 60])
        # indexes are positions in what renders, so staged files seg0/seg1 match the props
        self.assertEqual([s["src"] for s in off["props"]["segments"]], ["seg0.mp4", "seg1.mp4"])

    def test_disabled_segment_contributes_no_audio_captions_text_or_cues(self):
        noisy = {"captions": "transcript", "nat": True, "text": ["gone"]}
        plan = [{"asset": "a", "in": 0, "out": 2, **noisy}, {"asset": "b", "in": 0, "out": 2}]
        live = self.build(plan)
        self.assertTrue(live["props"]["captions"] and live["props"]["beats"] and live["sound"]["nat"])
        plan[0]["enabled"] = False
        off = self.build(plan)
        self.assertEqual(off["props"]["captions"], [])
        self.assertEqual(off["props"]["beats"], [])
        self.assertEqual(off["sound"]["nat"], [])
        self.assertEqual(off["sound"]["cues"], [])
        self.assertEqual(off["total"], 60)

    def test_aligned_build_keeps_plan_indexes_and_lists_the_ghost(self):
        path = self.temp.plan("aligned", self.three(enabled=False))
        built = build(self.project, load_plan(self.project, path), aligned=True)
        self.assertEqual(built["total"], 90)
        self.assertEqual([sg.index for sg in built["segs"]], [0, 2])
        (ghost,) = built["disabled"]
        self.assertEqual((ghost.index, ghost.start, ghost.frames, ghost.disabled), (1, 60, 90, True))
        self.assertEqual([s["src"] for s in built["props"]["segments"]], ["seg0.mp4", "seg2.mp4"])

    def test_all_disabled_is_empty_for_the_editor_and_an_error_for_render(self):
        segments = [dict(s, enabled=False) for s in self.three()]
        path = self.temp.plan("none", segments)
        plan = load_plan(self.project, path)
        built = build(self.project, plan, aligned=True)
        self.assertEqual((built["total"], len(built["segs"]), len(built["disabled"])), (0, 0, 3))
        with self.assertRaisesRegex(ReelsError, "every segment is disabled"):
            build(self.project, plan)

    def test_enabled_must_be_a_boolean_and_true_is_normal(self):
        with self.assertRaisesRegex(ReelsError, "enabled must be true or false"):
            self.build(self.three(enabled="no"))
        self.assertEqual(self.build(self.three(enabled=True))["total"], 180)

    def test_positioned_disabled_clip_is_skipped_and_cannot_collide(self):
        a = placed("a", 0, 60)
        b = placed("b", 30, 60, enabled=False)  # overlaps a on V1: only legal because it is disabled
        c = placed("c", 90, 30)
        built = self.build([a, b, c], timeline=header(120))
        self.assertEqual(built["total"], 120)
        self.assertEqual([(s["startFrame"], s["durationInFrames"]) for s in built["props"]["segments"]],
                         [(0, 60), (90, 30)])
        with self.assertRaisesRegex(ReelsError, "overlap"):
            self.build([a, dict(b, enabled=True), c], timeline=header(120))

    def test_check_shows_disabled_segments_instead_of_erroring(self):
        path = self.temp.plan("shown", self.three(enabled=False))
        lines: list[str] = []
        import reels.render as render
        original = render.log
        render.log = lines.append
        try:
            check_plan(self.project, path)
        finally:
            render.log = original
        text = "\n".join(lines)
        self.assertIn("2 segments (+1 disabled)", text)
        self.assertRegex(text, r"\n\s+2\s+—\s+3\.0s\s+b\s.*disabled")

    def test_ghost_starts_follow_beat_snapping_in_full_and_timing_compilation(self):
        import reels.render as renderer
        from reels.editor.compile_runtime import serialize, timing_build

        (self.project.music_dir / "grid.mp3").write_bytes(b"music fixture")
        write_json(self.project.music_dir / "grid.json",
                   {"slug": "grid", "file": "music/grid.mp3", "rightsOk": True,
                    "bpm": 60, "beats": list(range(12)), "duration": 30})
        path = self.temp.plan("snapped", [
            {"asset": "a", "in": 0, "out": 2.4},
            {"asset": "b", "in": 0, "out": 1, "enabled": False},
            {"asset": "c", "in": 0, "out": 2.4},
        ], music={"track": "grid", "cutOnBeat": True})
        plan = load_plan(self.project, path)
        full = serialize(build(self.project, deepcopy(plan), aligned=True))
        timing = timing_build(renderer, self.project, deepcopy(plan))
        for compiled in (full, timing):
            with self.subTest(mode="full" if compiled is full else "timing"):
                a, ghost, c = compiled["segs"]
                self.assertEqual(a["frames"], 60)
                self.assertEqual(ghost["start"], a["start"] + a["frames"])
                self.assertEqual(c["start"], ghost["start"])

    def test_editing_extent_covers_tail_ghosts_without_extending_render(self):
        import reels.render as renderer
        from reels.editor.compile_runtime import serialize, timing_build
        from reels.editor.document import editing_snapshot

        for segments, extra, total, extent in (
            ([{"asset": "a", "in": 0, "out": 2}, {"asset": "b", "in": 0, "out": 3, "enabled": False}],
             {}, 60, 150),
            ([dict(s, enabled=False) for s in self.three()], {}, 0, 90),
            ([placed("a", 0, 60), placed("b", 120, 90, enabled=False)], {"timeline": header(60)}, 60, 210),
        ):
            with self.subTest(total=total, extent=extent):
                path = self.temp.plan("extent", segments, **extra)
                plan = load_plan(self.project, path)
                for compiled in (serialize(build(self.project, deepcopy(plan), aligned=True)),
                                 timing_build(renderer, self.project, deepcopy(plan))):
                    self.assertEqual(compiled["total"], total)
                    self.assertEqual(compiled["editingDurationFrames"], extent)
                    snap = editing_snapshot(deepcopy(plan), compiled, {"document": "d", "library": "l", "renderer": "r"})
                    self.assertEqual(snap["durationFrames"], total)
                    self.assertEqual(snap["editingDurationFrames"], extent)



@unittest.skipUnless(SANDBOX, "trusted macOS sandbox required")
class OpsBase(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.workspace = SimpleNamespace(project=self.temp.project, root=self.temp.root)
        for name in ("a", "b", "c", "d"):
            self.temp.asset(name, duration=30)

    def store(self, segments, **extra):
        self.path = self.temp.plan("ops", segments, **extra)
        store = DocumentStore(self.workspace, self.path)
        self.addCleanup(store.close)
        return store

    def apply(self, store, operations, snapshot=None):
        return store.apply({"expected": (snapshot or store.snapshot())["revisions"], "origin": "manual",
                            "operations": operations})

    @staticmethod
    def clips(snapshot):
        return [n for n in snapshot["nodes"] if n["kind"] == "segment"]

    @staticmethod
    def layout(snapshot):
        return [(n["assetId"], n["startFrame"], n["endFrame"]) for n in OpsBase.clips(snapshot)]

    def children(self, snapshot, parent):
        return [n for n in snapshot["nodes"] if n.get("parentId") == parent["id"] and not n.get("generated")]

    def assertRejected(self, store, operation, pattern=None):
        before = store.snapshot()
        with self.assertRaises(DocumentError) as caught:
            self.apply(store, [operation], before)
        if pattern:
            self.assertRegex(str(caught.exception), pattern)
        self.assertEqual(store.snapshot()["plan"]["segments"], before["plan"]["segments"])
        return caught.exception

    def global_events(self, at):
        sounds = self.temp.root / "sfx"
        sounds.mkdir(exist_ok=True)
        (sounds / "click.wav").write_bytes(b"sound fixture")
        write_json(sounds / "click.json", {"slug": "click", "duration": 0.2})
        return {
            "text": [{"text": "global words", "at": at, "for": 0.5}],
            "musicDrops": [{"at": at, "for": 0.5}],
            "sfx": [{"sound": "click", "at": at}, {"sound": "click", "endAt": at + 0.5}],
            "exhibits": [{"chart": {"type": "line", "points": [[0, 0], [1, 2]]}, "at": at, "for": 0.5}],
        }

    def assert_round_trip(self, store, before, after):
        undone = store.undo(after["revisions"])
        self.assertEqual(undone["plan"], before["plan"])
        self.assertEqual(self.layout(undone), self.layout(before))
        redone = store.redo(undone["revisions"])
        self.assertEqual(redone["plan"], after["plan"])
        self.assertEqual(self.layout(redone), self.layout(after))
        self.assertEqual([n["id"] for n in redone["nodes"]], [n["id"] for n in after["nodes"]])
        return redone


SEQUENTIAL = [
    {"asset": "a", "in": 0, "out": 2},                                       # 0-60
    {"asset": "b", "in": 0, "out": 2, "text": ["linked words"],              # 60-120, owns text + cutaway
     "cutaways": [{"asset": "d", "in": 0, "out": 1, "at": 0.5}]},
    {"asset": "c", "in": 0, "out": 2},                                       # 120-180
]


class SequentialOpsTests(OpsBase):
    def test_duplicate_inserts_a_copy_after_the_original_with_linked_items_and_new_ids(self):
        store = self.store(SEQUENTIAL)
        before = store.snapshot()
        original = self.clips(before)[1]
        after = self.apply(store, [{"op": "duplicate_segments", "ids": [original["id"]]}], before)
        self.assertEqual(self.layout(after), [("a", 0, 60), ("b", 60, 120), ("b", 120, 180), ("c", 180, 240)])
        _, copy, moved = self.clips(after)[1:]
        self.assertEqual(self.clips(after)[1]["id"], original["id"])
        self.assertNotEqual(copy["id"], original["id"])
        self.assertEqual(self.clips(after)[3]["id"], self.clips(before)[2]["id"])
        mine, theirs = self.children(after, self.clips(after)[1]), self.children(after, copy)
        self.assertEqual(sorted(n["kind"] for n in mine), sorted(n["kind"] for n in theirs))
        self.assertEqual({n["kind"] for n in theirs}, {"text", "cutaway"})
        self.assertFalse({n["id"] for n in mine} & {n["id"] for n in theirs})
        self.assertEqual(after["durationFrames"], 240)
        undone = store.undo(after["revisions"])
        self.assertEqual(self.layout(undone), self.layout(before))
        self.assertEqual([n["id"] for n in self.clips(undone)], [n["id"] for n in self.clips(before)])
        redone = store.redo(undone["revisions"])
        self.assertEqual(redone["plan"]["segments"], after["plan"]["segments"])

    def test_duplicate_several_lands_after_the_last_original_in_plan_order(self):
        store = self.store(SEQUENTIAL)
        before = store.snapshot()
        a, _, c = self.clips(before)
        after = self.apply(store, [{"op": "duplicate_segments", "ids": [c["id"], a["id"]]}], before)
        self.assertEqual([n[0] for n in self.layout(after)], ["a", "b", "c", "a", "c"])
        self.assertEqual(after["durationFrames"], 300)

    def test_ripple_remove_closes_the_gap_and_undoes(self):
        store = self.store(SEQUENTIAL)
        before = store.snapshot()
        first, middle, last = self.clips(before)
        after = self.apply(store, [{"op": "ripple_remove", "ids": [middle["id"]]}], before)
        self.assertEqual(self.layout(after), [("a", 0, 60), ("c", 60, 120)])
        self.assertEqual(self.clips(after)[1]["id"], last["id"])
        self.assertEqual([n for n in after["nodes"] if n["id"] == middle["id"]], [])
        # the linked text and cutaway went with their segment
        self.assertFalse([n for n in after["nodes"] if n["kind"] in ("text", "cutaway")])
        undone = store.undo(after["revisions"])
        self.assertEqual(self.layout(undone), self.layout(before))
        self.assertTrue([n for n in undone["nodes"] if n["kind"] == "cutaway"])
        redone = store.redo(undone["revisions"])
        self.assertEqual(redone["plan"], after["plan"])

    def test_ripple_remove_shifts_plan_level_events_like_a_delete(self):
        store = self.store(SEQUENTIAL, text=[{"text": "late", "at": 4.0, "for": 1.0}])
        before = store.snapshot()
        after = self.apply(store, [{"op": "ripple_remove", "ids": [self.clips(before)[1]["id"]]}], before)
        self.assertEqual(after["plan"]["text"][0]["at"], 2.0)

    def test_paste_inserts_at_the_nearest_cut_at_or_before_the_frame(self):
        store = self.store(SEQUENTIAL)
        value = {"asset": "d", "in": 0, "out": 1, "timeline": {"track": "V3", "startFrame": 9, "durationFrames": 9}}
        for at, expected in ((0, 0), (59, 0), (60, 1), (100, 1), (150, 2), (179, 2), (180, 3), (5000, 3)):
            after = self.apply(store, [{"op": "paste_segments", "values": [value], "atFrame": at}])
            self.assertEqual([n["assetId"] for n in self.clips(after)].index("d"), expected, at)
            self.assertNotIn("timeline", after["plan"]["segments"][expected])
            self.assertEqual(after["durationFrames"], 210)
            store.undo(after["revisions"])

    def test_paste_many_keeps_order_and_copies_linked_items_with_new_ids(self):
        store = self.store(SEQUENTIAL)
        before = store.snapshot()
        values = [self.clips(before)[1]["raw"], self.clips(before)[0]["raw"]]
        after = self.apply(store, [{"op": "paste_segments", "values": values, "atFrame": 0}], before)
        self.assert_round_trip(store, before, after)
        self.assertEqual([n[0] for n in self.layout(after)], ["b", "a", "a", "b", "c"])
        ids = [n["id"] for n in self.clips(after)]
        self.assertEqual(len(set(ids)), 5)
        self.assertEqual(ids[2:], [n["id"] for n in self.clips(before)])
        self.assertTrue(self.children(after, self.clips(after)[0]))
        # the pasted copy before an identical original must not steal the original's identity
        original_children = {n["id"] for n in self.children(before, self.clips(before)[1])}
        self.assertEqual({n["id"] for n in self.children(after, self.clips(after)[3])}, original_children)

    def test_set_enabled_keeps_full_length_in_the_editor_and_closes_up_the_reel(self):
        store = self.store(SEQUENTIAL, text=[{"text": "late", "at": 4.0, "for": 1.0}])
        before = store.snapshot()
        middle = self.clips(before)[1]
        off = self.apply(store, [{"op": "set_enabled", "ids": [middle["id"]], "enabled": False}], before)
        self.assertIs(off["plan"]["segments"][1]["enabled"], False)
        self.assertEqual(off["durationFrames"], 120)
        node = self.clips(off)[1]
        self.assertEqual((node["id"], node["frames"], node["enabled"]), (middle["id"], 60, False))
        self.assertEqual(self.clips(off)[2]["startFrame"], 60)
        self.assertEqual(off["plan"]["text"][0]["at"], 2.0)
        self.assertEqual({n["id"] for n in self.children(off, node)}, {n["id"] for n in self.children(before, middle)})
        self.assertFalse([n for n in off["nodes"] if n["kind"] == "source_audio" and n["parentId"] == middle["id"]])
        on = self.apply(store, [{"op": "set_enabled", "ids": [middle["id"]], "enabled": True}], off)
        self.assertNotIn("enabled", on["plan"]["segments"][1])
        self.assertEqual(self.layout(on), self.layout(before))
        self.assertEqual(on["plan"]["text"][0]["at"], 4.0)
        self.assert_round_trip(store, off, on)

    def test_disabling_everything_leaves_an_empty_but_editable_reel(self):
        store = self.store(SEQUENTIAL)
        before = store.snapshot()
        ids = [n["id"] for n in self.clips(before)]
        off = self.apply(store, [{"op": "set_enabled", "ids": ids, "enabled": False}], before)
        self.assertEqual(off["durationFrames"], 0)
        self.assertEqual(len(self.clips(off)), 3)
        back = self.apply(store, [{"op": "set_enabled", "ids": ids[:1], "enabled": True}], off)
        self.assertEqual(self.layout(back), [("a", 0, 60), ("b", 60, 120), ("c", 60, 120)])

    def test_disable_preserves_global_events_and_enable_restores_them_after_other_edits(self):
        events = self.global_events(3.0)
        store = self.store(SEQUENTIAL, **events)
        before = store.snapshot()
        middle = self.clips(before)[1]
        off = self.apply(store, [{"op": "set_enabled", "ids": [middle["id"]], "enabled": False}], before)
        for collection, values in events.items():
            self.assertEqual([{k: v for k, v in event.items() if k != "disabledTiming"}
                              for event in off["plan"][collection]], values)
        self.assertFalse(store._current_compiled["props"].get("exhibits"))
        self.assertFalse(store._current_compiled["props"]["beats"])
        self.assertFalse(store._current_compiled["sound"]["cues"])
        rendered = build(self.temp.project, deepcopy(off["plan"]))
        self.assertFalse(rendered["props"]["beats"])
        self.assertFalse(rendered["props"].get("exhibits"))
        self.assertFalse(rendered["sound"]["cues"])
        self.assert_round_trip(store, before, off)
        moved = self.apply(store, [{"op": "duplicate_segments", "ids": [self.clips(off)[0]["id"]]}])
        on = self.apply(store, [{"op": "set_enabled", "ids": [middle["id"]], "enabled": True}], moved)
        for collection, values in events.items():
            for original, event in zip(values, on["plan"][collection]):
                expected = deepcopy(original)
                for field in ("at", "endAt"):
                    if field in expected:
                        expected[field] += 2.0
                self.assertEqual(event, expected)
        original_ids = {n["id"] for n in before["nodes"] if n["kind"] in ("text", "sfx", "music_drop", "exhibit")
                        and n.get("parentId") is None and not n.get("generated")}
        self.assertTrue(original_ids <= {n["id"] for n in on["nodes"]})
        self.assert_round_trip(store, moved, on)

    def test_paste_uses_the_beat_snapped_ghost_cut(self):
        (self.temp.project.music_dir / "grid.mp3").write_bytes(b"music fixture")
        write_json(self.temp.project.music_dir / "grid.json",
                   {"slug": "grid", "file": "music/grid.mp3", "rightsOk": True,
                    "bpm": 60, "beats": list(range(12)), "duration": 30})
        store = self.store([{"asset": "a", "in": 0, "out": 1.6},
                            {"asset": "b", "in": 0, "out": 1, "enabled": False},
                            {"asset": "c", "in": 0, "out": 2.4}],
                           music={"track": "grid", "cutOnBeat": True})
        after = self.apply(store, [{"op": "paste_segments", "values": [{"asset": "d", "in": 0, "out": 1}],
                                   "atFrame": 55}])
        self.assertEqual([n["assetId"] for n in self.clips(after)], ["d", "a", "b", "c"])

    def test_a_disabled_clip_survives_the_first_free_placement_that_makes_the_plan_positioned(self):
        store = self.store(SEQUENTIAL)
        before = store.snapshot()
        a, b, c = self.clips(before)
        off = self.apply(store, [{"op": "set_enabled", "ids": [b["id"]], "enabled": False}], before)
        moved = self.apply(store, [{"op": "move_segments", "mode": "overwrite",
                                    "editId": "A0000000-0000-4000-8000-000000000001",
                                    "placements": [{"id": c["id"], "track": "V2", "atFrame": 10}]}], off)
        self.assertEqual(moved["plan"]["timeline"]["version"], 1)
        survivor = next(n for n in self.clips(moved) if n["id"] == b["id"])
        self.assertEqual((survivor["enabled"], survivor["frames"]), (False, 60))
        self.assertIs(next(s for s in moved["plan"]["segments"] if s["asset"] == "b")["enabled"], False)

    def test_rejected_inputs_leave_the_document_untouched(self):
        store = self.store(SEQUENTIAL)
        snap = store.snapshot()
        child = next(n for n in snap["nodes"] if n["kind"] == "text")
        for name in ("duplicate_segments", "ripple_remove"):
            self.assertRejected(store, {"op": name, "ids": ["no-such-node"]})
            self.assertRejected(store, {"op": name, "ids": [child["id"]]}, "primary segment")
        self.assertRejected(store, {"op": "set_enabled", "ids": [child["id"]], "enabled": False}, "primary segment")
        self.assertRejected(store, {"op": "paste_segments", "values": [{"in": 0, "out": 1}], "atFrame": 0}, "asset")
        self.assertRejected(store, {"op": "paste_segments", "values": [{"asset": "nope", "in": 0, "out": 1}],
                                    "atFrame": 0})
        self.assertRejected(store, {"op": "paste_segments", "values": ["a"], "atFrame": 0})
        self.assertRejected(store, {"op": "paste_segments", "values": [{"asset": "a", "in": 0, "out": 1}],
                                    "atFrame": -3})

    def test_locked_track_blocks_duplicate_ripple_and_disable_of_its_clips(self):
        store = self.store(SEQUENTIAL)
        snap = store.snapshot()
        first = self.clips(snap)[0]
        locked = self.apply(store, [{"op": "set_track_locked", "id": first["track"], "locked": True}], snap)
        for operation in ({"op": "duplicate_segments", "ids": [first["id"]]},
                          {"op": "ripple_remove", "ids": [first["id"]]},
                          {"op": "set_enabled", "ids": [first["id"]], "enabled": False}):
            with self.assertRaises(DocumentError) as caught:
                self.apply(store, [operation], locked)
            self.assertEqual(caught.exception.code, "track_locked")


class PositionedOpsTests(OpsBase):
    def test_duplicate_places_copies_after_the_last_original_and_never_moves_other_items(self):
        store = self.store([placed("a", 0, 60), placed("b", 60, 60, text=["owned"],
                           cutaways=[{"asset": "d", "in": 0, "out": 1, "at": 0.25}]), placed("c", 300, 30)],
                           timeline=header(330))
        before = store.snapshot()
        a, b, c = self.clips(before)
        after = self.apply(store, [{"op": "duplicate_segments", "ids": [b["id"]]}], before)
        self.assertEqual(self.layout(after), [("a", 0, 60), ("b", 60, 120), ("b", 120, 180), ("c", 300, 330)])
        self.assertEqual([n["id"] for n in self.clips(after)][:2] + [self.clips(after)[3]["id"]],
                         [a["id"], b["id"], c["id"]])
        copy = self.clips(after)[2]
        self.assertTrue({"text", "cutaway"} <= {n["kind"] for n in self.children(after, copy)})
        self.assertFalse({n["id"] for n in self.children(after, copy)}
                         & {n["id"] for n in self.children(after, self.clips(after)[1])})
        self.assertEqual(after["plan"]["timeline"]["durationFrames"], 330)
        undone = store.undo(after["revisions"])
        self.assertEqual(self.layout(undone), self.layout(before))
        self.assertEqual(store.redo(undone["revisions"])["plan"]["segments"], after["plan"]["segments"])

    def test_duplicate_selection_keeps_relative_layout_and_tracks_and_extends_the_reel(self):
        store = self.store([placed("a", 0, 60), placed("b", 30, 60, track="V2"), placed("c", 60, 30)],
                           timeline=header(90))
        before = store.snapshot()
        ids = [n["id"] for n in self.clips(before)]
        after = self.apply(store, [{"op": "duplicate_segments", "ids": ids}], before)
        added = [(n["assetId"], n["startFrame"], n["endFrame"], n["track"]) for n in self.clips(after)[3:]]
        self.assertEqual(sorted(added), [("a", 90, 150, "V1"), ("b", 120, 180, "V2"), ("c", 150, 180, "V1")])
        self.assertEqual(after["plan"]["timeline"]["durationFrames"], 180)

    def test_duplicate_can_use_a_disabled_clips_range(self):
        store = self.store([placed("a", 0, 60), placed("b", 60, 60, enabled=False)], timeline=header(120))
        before = store.snapshot()
        after = self.apply(store, [{"op": "duplicate_segments", "ids": [self.clips(before)[0]["id"]]}], before)
        self.assertEqual(self.layout(after), [("a", 0, 60), ("a", 60, 120), ("b", 60, 120)])
        self.assertIs(self.clips(after)[2]["enabled"], False)
        self.assert_round_trip(store, before, after)

    def test_duplicate_rejects_an_occupied_range_naming_the_frames(self):
        store = self.store([placed("a", 0, 60), placed("b", 70, 30)], timeline=header(100))
        first = self.clips(store.snapshot())[0]
        error = self.assertRejected(store, {"op": "duplicate_segments", "ids": [first["id"]]}, "No room to duplicate")
        self.assertIn("60-120 on V1", str(error))
        # another track at the same frames is not in the way
        store = self.store([placed("a", 0, 60), placed("b", 60, 30, track="V2")], timeline=header(100))
        after = self.apply(store, [{"op": "duplicate_segments", "ids": [self.clips(store.snapshot())[0]["id"]]}])
        self.assertEqual(len(self.clips(after)), 3)

    def test_duplicate_keeps_a_fade_only_when_its_predecessor_is_copied_too(self):
        faded = placed("b", 50, 60)
        faded["timeline"]["transitionInFrames"] = 10
        faded["transition"] = "fade"
        store = self.store([placed("a", 0, 60), faded], timeline=header(110))
        a, b = self.clips(store.snapshot())
        both = self.apply(store, [{"op": "duplicate_segments", "ids": [a["id"], b["id"]]}])
        stamps = [(n["startFrame"], n["transitionInFrames"]) for n in self.clips(both)]
        self.assertEqual(stamps, [(0, 0), (50, 10), (110, 0), (160, 10)])
        store.undo(both["revisions"])
        alone = self.apply(store, [{"op": "duplicate_segments", "ids": [b["id"]]}])
        self.assertEqual(self.clips(alone)[2]["transitionInFrames"], 0)
        self.assertEqual(alone["plan"]["segments"][2]["transition"], "cut")

    def test_ripple_remove_shifts_every_track_left_and_plain_remove_leaves_the_gap(self):
        def fresh():
            return self.store([placed("a", 0, 60), placed("b", 60, 60, text=["owned"]),
                               placed("c", 150, 30), placed("d", 130, 40, track="V2")], timeline=header(180))
        store = fresh()
        before = store.snapshot()
        a, b, c, d = self.clips(before)
        after = self.apply(store, [{"op": "ripple_remove", "ids": [b["id"]]}], before)
        self.assertEqual(self.layout(after), [("a", 0, 60), ("c", 90, 120), ("d", 70, 110)])
        self.assertEqual([n["id"] for n in self.clips(after)], [a["id"], c["id"], d["id"]])
        self.assertEqual(after["plan"]["timeline"]["durationFrames"], 120)
        self.assertEqual(after["durationFrames"], 120)
        self.assertFalse([n for n in after["nodes"] if n["kind"] == "text"])
        undone = store.undo(after["revisions"])
        self.assertEqual(self.layout(undone), self.layout(before))
        self.assertEqual(store.redo(undone["revisions"])["plan"]["segments"], after["plan"]["segments"])
        gap = fresh()
        snap = gap.snapshot()
        plain = self.apply(gap, [{"op": "remove_segments", "ids": [self.clips(snap)[1]["id"]]}], snap)
        self.assertEqual(self.layout(plain), [("a", 0, 60), ("c", 150, 180), ("d", 130, 170)])

    def test_ripple_remove_several_disjoint_clips_and_the_last_clip(self):
        store = self.store([placed("a", 0, 30), placed("b", 30, 30), placed("c", 60, 30), placed("d", 90, 30)],
                           timeline=header(120))
        a, b, c, d = self.clips(store.snapshot())
        after = self.apply(store, [{"op": "ripple_remove", "ids": [b["id"], d["id"]]}])
        self.assertEqual(self.layout(after), [("a", 0, 30), ("c", 30, 60)])
        self.assertEqual(after["plan"]["timeline"]["durationFrames"], 60)

    def test_ripple_remove_retimes_global_events_using_merged_disjoint_intervals(self):
        events = self.global_events(7.0)
        events["text"].append({"text": "removed", "at": 1.5, "for": 0.2})
        events["musicDrops"].append({"at": 0.5, "for": 2.5})
        store = self.store([placed("a", 0, 30), placed("b", 30, 30),
                            placed("b", 30, 30, track="V2"), placed("c", 90, 30), placed("d", 210, 30)],
                           timeline=header(240), **events)
        before = store.snapshot()
        ids = [n["id"] for n in self.clips(before)[1:4]]
        after = self.apply(store, [{"op": "ripple_remove", "ids": ids}], before)
        self.assertEqual(self.layout(after), [("a", 0, 30), ("d", 150, 180)])
        for collection in events:
            self.assertEqual(after["plan"][collection][0].get("at"), 5.0)
        self.assertEqual(after["plan"]["sfx"][1]["endAt"], 5.5)
        self.assertEqual(after["plan"]["text"], [{"text": "global words", "at": 5.0, "for": 0.5}])
        self.assertEqual(after["plan"]["musicDrops"][1], {"at": 0.5, "for": 1.5})
        self.assert_round_trip(store, before, after)

    def test_ripple_remove_rejects_a_clip_crossing_the_gap(self):
        store = self.store([placed("a", 0, 60), placed("b", 60, 60), placed("c", 40, 60, track="V2")],
                           timeline=header(120))
        b = self.clips(store.snapshot())[1]
        self.assertRejected(store, {"op": "ripple_remove", "ids": [b["id"]]}, "crosses the removed range")

    def test_ripple_remove_of_a_clip_that_fades_into_the_next_keeps_the_next_clips_frames_tidy(self):
        faded = placed("b", 50, 60)
        faded["timeline"]["transitionInFrames"] = 10
        faded["transition"] = "fade"
        store = self.store([placed("a", 0, 60), faded, placed("c", 110, 30)], timeline=header(140))
        a, b, c = self.clips(store.snapshot())
        after = self.apply(store, [{"op": "ripple_remove", "ids": [a["id"]]}])
        # a's fade overlap belongs to b: b keeps its start, loses the fade into nothing, and c closes up
        self.assertEqual(self.layout(after), [("b", 0, 60), ("c", 60, 90)])
        self.assertEqual(after["plan"]["segments"][0]["timeline"]["transitionInFrames"], 0)
        self.assertEqual(after["plan"]["segments"][0]["transition"], "cut")

    def test_paste_places_at_the_frame_on_a_track_and_defaults_to_the_first_video_track(self):
        store = self.store([placed("a", 0, 60), placed("b", 60, 60)], timeline=header(120))
        before = store.snapshot()
        source = deepcopy(self.clips(before)[0]["raw"])
        source["timeline"]["startFrame"] = 999  # relative layout only: the paste frame wins
        v2 = self.apply(store, [{"op": "paste_segments", "values": [source], "atFrame": 30, "track": "V2"}], before)
        pasted = [n for n in self.clips(v2) if n["track"] == "V2"]
        self.assertEqual([(n["assetId"], n["startFrame"], n["endFrame"]) for n in pasted], [("a", 30, 90)])
        self.assertEqual(self.layout(v2)[:2], self.layout(before)[:2])
        store.undo(v2["revisions"])
        v1 = self.apply(store, [{"op": "paste_segments", "values": [source], "atFrame": 90}])
        victims = [(n["assetId"], n["startFrame"], n["endFrame"]) for n in self.clips(v1) if n["track"] == "V1"]
        # existing overwrite rules: the paste cuts the clip underneath
        self.assertEqual(sorted(victims), [("a", 0, 60), ("a", 90, 150), ("b", 60, 90)])
        self.assertEqual(v1["plan"]["timeline"]["durationFrames"], 150)
        self.assertNotIn("_editor", v1["plan"]["segments"][-1])

    def test_paste_several_keeps_their_relative_offsets_and_copies_linked_items(self):
        store = self.store([placed("a", 0, 30), placed("b", 40, 30, text=["owned"])], timeline=header(70))
        before = store.snapshot()
        values = [n["raw"] for n in self.clips(before)]
        after = self.apply(store, [{"op": "paste_segments", "values": values, "atFrame": 200}], before)
        added = self.clips(after)[2:]
        self.assertEqual([(n["assetId"], n["startFrame"], n["endFrame"]) for n in added],
                         [("a", 200, 230), ("b", 240, 270)])
        self.assert_round_trip(store, before, after)
        self.assertTrue(self.children(after, added[1]))
        self.assertEqual(len({n["id"] for n in after["nodes"]}), len(after["nodes"]))

    def test_paste_without_timeline_info_lays_clips_back_to_back(self):
        store = self.store([placed("a", 0, 30)], timeline=header(30))
        values = [{"asset": "b", "in": 0, "out": 1}, {"asset": "c", "in": 0, "out": 2}]
        after = self.apply(store, [{"op": "paste_segments", "values": values, "atFrame": 30}])
        self.assertEqual(self.layout(after)[1:], [("b", 30, 60), ("c", 60, 120)])

    def test_paste_unordered_values_anchors_the_earliest_clip(self):
        store = self.store([placed("a", 0, 30)], timeline=header(30))
        before = store.snapshot()
        values = [placed("b", 90, 30), placed("c", 30, 30)]
        after = self.apply(store, [{"op": "paste_segments", "values": values, "atFrame": 120}], before)
        self.assertEqual(self.layout(after), [("a", 0, 30), ("b", 180, 210), ("c", 120, 150)])
        self.assert_round_trip(store, before, after)

    def test_set_enabled_leaves_everything_in_place_and_ghosts_do_not_block_edits(self):
        store = self.store([placed("a", 0, 60), placed("b", 60, 60), placed("c", 120, 60)], timeline=header(180))
        before = store.snapshot()
        b = self.clips(before)[1]
        off = self.apply(store, [{"op": "set_enabled", "ids": [b["id"]], "enabled": False}], before)
        self.assertEqual(self.layout(off), self.layout(before))
        self.assertEqual(self.clips(off)[1]["enabled"], False)
        self.assertEqual(off["durationFrames"], 180)
        self.assertEqual([c for c in off["plan"]["segments"] if c.get("enabled") is False][0]["asset"], "b")
        on = self.apply(store, [{"op": "set_enabled", "ids": [b["id"]], "enabled": True}], off)
        self.assertNotIn("enabled", on["plan"]["segments"][1])
        self.assert_round_trip(store, off, on)
        self.assertEqual(self.clips(on)[1]["id"], b["id"])

    def test_set_enabled_compiled_segs_and_nodes_flag_the_ghost(self):
        store = self.store([placed("a", 0, 60), placed("b", 30, 60, enabled=False)], timeline=header(90))
        snap = store.snapshot()
        compiled = store._current_compiled
        self.assertEqual([seg.get("disabled", False) for seg in compiled["segs"]], [False, True])
        self.assertEqual([n.get("enabled") for n in self.clips(snap)], [None, False])


class LinkedItemsTests(OpsBase):
    def test_audio_and_cutaways_follow_duplicate_paste_ripple_and_enable_round_trips(self):
        for absolute in (False, True):
            for op in ("duplicate_segments", "paste_segments", "ripple_remove", "set_enabled"):
                with self.subTest(positioned=absolute, operation=op):
                    linked = {"cutaways": [{"asset": "d", "in": 0, "out": 1, "at": 0.5}], "volume": 0.7}
                    segments = ([placed("a", 0, 60), placed("b", 60, 60, **linked), placed("c", 240, 60)]
                                if absolute else [dict(s, **(linked if i == 1 else {})) for i, s in enumerate(SEQUENTIAL)])
                    # Each scenario gets its own history.
                    slug = f"linked-{int(absolute)}-{op.replace('_', '-')}"
                    path = self.temp.plan(slug, segments, **({"timeline": header(300)} if absolute else {}))
                    store = DocumentStore(self.workspace, path)
                    self.addCleanup(store.close)
                    before = store.snapshot()
                    owner = self.clips(before)[1]
                    operation = ({"op": op, "values": [owner["raw"]], "atFrame": 360} if op == "paste_segments"
                                 else {"op": op, "ids": [owner["id"]], **({"enabled": False} if op == "set_enabled" else {})})
                    after = self.apply(store, [operation], before)
                    for snap in (before, after, self.assert_round_trip(store, before, after)):
                        for clip in self.clips(snap):
                            audio = [n for n in snap["nodes"] if n["kind"] == "source_audio" and n["parentId"] == clip["id"]]
                            if clip.get("enabled") is False:
                                self.assertEqual(audio, [])
                            else:
                                self.assertEqual([(n["startFrame"], n["endFrame"]) for n in audio],
                                                 [(clip["startFrame"], clip["endFrame"])])
                                self.assertEqual(audio[0]["raw"]["asset"], clip["assetId"])
                            if clip["assetId"] == "b":
                                cut = next(n for n in self.children(snap, clip) if n["kind"] == "cutaway")
                                self.assertEqual((cut["startFrame"], cut["endFrame"]),
                                                 (clip["startFrame"] + 15, clip["startFrame"] + 45))
                    if op == "set_enabled":
                        on = self.apply(store, [{"op": op, "ids": [owner["id"]], "enabled": True}])
                        self.assert_round_trip(store, after, on)
                        audio = next(n for n in on["nodes"] if n["kind"] == "source_audio" and n["parentId"] == owner["id"])
                        self.assertEqual((audio["startFrame"], audio["endFrame"]), (60, 120))
                    store.close()


if __name__ == "__main__":
    unittest.main()
