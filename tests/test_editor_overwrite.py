from __future__ import annotations

import json
import math
import platform
import shutil
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from helpers import TempProject, words
from reels.editor.compiled_cache import cache_record, read_cache, valid_result
from reels.editor.document import canonical_json, node_location
from reels.editor.operations import MoveSegments
from reels.editor.transactions import DocumentStore, DocumentError
from reels.timeline import deterministic_id

EDIT = "A0000000-0000-4000-8000-000000000001"
EDIT2 = "b0000000-0000-4000-8000-000000000002"
SANDBOX = platform.system() == "Darwin" and shutil.which("sandbox-exec")


class DeterministicOverwriteIDs(unittest.TestCase):
    def test_shared_python_swift_vectors(self):
        self.assertEqual(deterministic_id("frag-", EDIT, "segment-victim", 180, 240),
            "frag-ef0b2abac24c30522a26f0ffc5bb7e3f544b58ebd80d4c8deaef496d27806876")
        self.assertEqual(deterministic_id("clip-", EDIT, "insert:0", 90, 180),
            "clip-f376c9832eba1e75ada37f5cf94f4e25aa9f1276967c17426652af5922d9e9a6")
        self.assertEqual(deterministic_id("node-", EDIT, "caption-café", 180, 240),
            "node-84a5c489ae125a1439d2a0fe33d2f33d5b91f83d655278cf09fe55f58c7dd5ce")

    def test_schema_rejects_ambiguous_sources_and_invalid_edit_ids(self):
        with self.assertRaises(ValueError):
            MoveSegments.model_validate({"op": "move_segments", "mode": "overwrite", "editId": EDIT,
                "placements": [{"id": "x", "insert": {"asset": "a"}, "track": "V1", "atFrame": 0}]})
        with self.assertRaises(ValueError):
            MoveSegments.model_validate({"op": "move_segments", "mode": "overwrite", "editId": "not-a-uuid",
                "placements": [{"id": "x", "track": "V1", "atFrame": 0}]})

    def test_positioned_full_length_fades_allow_only_exact_overlaps(self):
        from reels.timeline import place
        plan = {"timeline": {"version": 1, "fps": 30, "durationFrames": 100}}
        # place() reads end dynamically in the real Seg type.
        class Segment:
            def __init__(self, start, duration, transition):
                self.start, self.frames, self.trans, self.index = 0, duration, 0, start
                self.spec = {"timeline": {"track": "V1", "startFrame": start,
                             "durationFrames": duration, "transitionInFrames": transition}}
            @property
            def end(self):
                return self.start + self.frames
        for frames in (1, 2):
            place(plan, [Segment(0, 90, 0), Segment(90-frames, frames, frames)], 30)
            with self.assertRaisesRegex(ValueError, "overlap"):
                place(plan, [Segment(0, 90, 0), Segment(90-frames, frames, 0)], 30)
            with self.assertRaisesRegex(ValueError, "transition"):
                place(plan, [Segment(0, 90, 0), Segment(90-frames, frames, frames+1)], 30)


@unittest.skipUnless(SANDBOX, "trusted macOS sandbox required")
class OverwriteTransactionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.workspace = SimpleNamespace(project=self.temp.project, root=self.temp.root)
        for name in ("a", "b", "c", "d"):
            self.temp.asset(name, duration=30)

    def store(self, segments=None, **extra):
        self.path = self.temp.plan("overwrite", segments or [
            {"asset": "a", "in": 0, "out": 8}, {"asset": "b", "in": 0, "out": 2}],
            **extra)
        store = DocumentStore(self.workspace, self.path)
        self.addCleanup(store.close)
        return store

    def apply(self, store, operations):
        return store.apply({"expected": store.snapshot()["revisions"], "origin": "manual",
                            "operations": operations})

    def move(self, store, placements, edit=EDIT):
        return self.apply(store, [{"op": "move_segments", "mode": "overwrite",
                                  "editId": edit, "placements": placements}])

    def clips(self, snapshot):
        return [node for node in snapshot["nodes"] if node["kind"] == "segment"]

    def test_middle_overwrite_one_rpc_exact_source_slices_and_undo_redo(self):
        store = self.store()
        before = store.snapshot()
        victim, moving = self.clips(before)
        head = store.head_path.read_bytes()
        original = store._warm_pool.compile
        def validate(*args, **kwargs):
            self.assertEqual(store.head_path.read_bytes(), head)
            return original(*args, **kwargs)
        with patch.object(store._warm_pool, "compile", side_effect=validate) as calls:
            after = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 90}])
        self.assertEqual(calls.call_count, 1)
        survivors = [node for node in self.clips(after) if node["assetId"] == "a"]
        self.assertEqual([(n["startFrame"], n["endFrame"], n["sourceIn"], n["sourceOut"])
                          for n in survivors], [(0, 90, 0, 3), (150, 240, 5, 8)])
        self.assertEqual(survivors[0]["id"], victim["id"])
        self.assertEqual(survivors[1]["id"], deterministic_id("frag-", EDIT, victim["id"], 150, 240))
        self.assertEqual(after["durationFrames"], before["durationFrames"])
        undone = store.undo(after["revisions"])
        self.assertNotIn("timeline", undone["plan"])
        self.assertEqual([n["id"] for n in self.clips(undone)], [victim["id"], moving["id"]])
        redone = store.redo(undone["revisions"])
        self.assertEqual(redone["plan"]["segments"], after["plan"]["segments"])

    def test_head_overwrite_keeps_tail_id_and_source_offset(self):
        store = self.store()
        victim, moving = self.clips(store.snapshot())
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 0}])
        survivor = next(n for n in self.clips(result) if n["assetId"] == "a")
        self.assertEqual((survivor["id"], survivor["startFrame"], survivor["sourceIn"]),
                         (victim["id"], 60, 2))

    def test_tail_overwrite_does_not_shift_any_global_event(self):
        store = self.store(text=[{"text": "fixed", "at": 1, "for": 1}], musicDrops=[{"at": 4, "for": 1}])
        _, moving = self.clips(store.snapshot())
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 210}])
        survivor = next(n for n in self.clips(result) if n["assetId"] == "a")
        self.assertEqual((survivor["startFrame"], survivor["endFrame"], survivor["sourceOut"]), (0, 210, 7))
        self.assertEqual(result["plan"]["text"], [{"text": "fixed", "at": 1, "for": 1}])
        self.assertEqual(result["plan"]["musicDrops"], [{"at": 4, "for": 1}])

    def test_full_cover_and_multiple_victims(self):
        store = self.store([{"asset": name, "in": 0, "out": 3} for name in ("a", "b", "c")] +
                           [{"asset": "d", "in": 0, "out": 6}])
        nodes = self.clips(store.snapshot())
        result = self.move(store, [{"id": nodes[3]["id"], "track": "V1", "atFrame": 60}])
        self.assertNotIn(nodes[1]["id"], {n["id"] for n in result["nodes"]})
        self.assertEqual([(n["assetId"], n["startFrame"], n["endFrame"]) for n in self.clips(result)],
                         [("a", 0, 60), ("c", 240, 270), ("d", 60, 240)])
        # A second move can fully cover the last residual head without touching other tracks.
        result = self.move(store, [{"id": nodes[3]["id"], "track": "V1", "atFrame": 0}], EDIT2)
        self.assertNotIn(nodes[0]["id"], {n["id"] for n in result["nodes"]})

    def test_vertical_move_layers_preserves_lower_track_and_source_gap(self):
        store = self.store()
        before = store.snapshot()
        first, moving = self.clips(before)
        result = self.move(store, [{"id": moving["id"], "track": "V2", "atFrame": 90}])
        nodes = {n["id"]: n for n in self.clips(result)}
        self.assertEqual((nodes[first["id"]]["startFrame"], nodes[first["id"]]["endFrame"]), (0, 240))
        self.assertEqual((nodes[moving["id"]]["startFrame"], nodes[moving["id"]]["track"]), (90, "V2"))
        self.assertEqual(result["durationFrames"], 300, "the trailing source hole must remain")
        self.assertEqual(store._current_compiled["props"]["timeline"]["durationFrames"], 300)
        self.assertTrue(valid_result(store._current_compiled))
        record = deepcopy(store._current_compiled)
        cache = store.history / "TIMELINE-CACHE-TEST.json"
        cache.write_text(json.dumps(cache_record({"fixture": "explicit"}, record)))
        self.assertEqual(read_cache(cache, {"fixture": "explicit"}), record)
        record["props"].pop("timeline")
        self.assertFalse(valid_result(record), "legacy cache integrity must still require its exact last end")
        record = deepcopy(store._current_compiled)
        record["props"]["timeline"]["durationFrames"] -= 1
        self.assertFalse(valid_result(record))

    def test_speed_adjusted_source_ranges_and_one_frame_survivor(self):
        store = self.store([{"asset": "a", "in": 1, "out": 9, "speed": 2},
                            {"asset": "b", "in": 0, "out": 2}])
        victim, moving = self.clips(store.snapshot())
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 30}])
        slices = [n for n in self.clips(result) if n["assetId"] == "a"]
        self.assertEqual([(n["sourceIn"], n["sourceOut"]) for n in slices], [(1, 3), (7, 9)])
        result = self.move(store, [{"insert": {"asset": "c", "in": 0, "out": 29/30},
                                   "track": "V1", "atFrame": 1}], EDIT2)
        one = next(n for n in self.clips(result) if n["id"] == victim["id"])
        self.assertEqual(one["frames"], 1)
        self.assertAlmostEqual(one["sourceOut"], 1 + 2/30, places=12)

    def test_insert_video_and_photo_and_extending_past_end(self):
        self.temp.asset("photo", kind="photo")
        store = self.store()
        result = self.move(store, [
            {"insert": {"asset": "c", "in": 1, "out": 3, "speed": 2}, "track": "V2", "atFrame": 90},
            {"insert": {"asset": "photo", "duration": 2}, "track": "V3", "atFrame": 400},
        ])
        nodes = {n["assetId"]: n for n in self.clips(result)}
        self.assertEqual(nodes["c"]["id"], deterministic_id("clip-", EDIT, "insert:0", 90, 120))
        self.assertEqual(nodes["photo"]["id"], deterministic_id("clip-", EDIT, "insert:1", 400, 460))
        self.assertEqual(result["durationFrames"], 460)

    def test_simultaneous_swap_excludes_selected_sources_from_victims(self):
        store = self.store([{"asset": "a", "in": 0, "out": 2}, {"asset": "b", "in": 0, "out": 2}])
        first, second = self.clips(store.snapshot())
        result = self.move(store, [{"id": first["id"], "track": "V1", "atFrame": 60},
                                   {"id": second["id"], "track": "V1", "atFrame": 0}])
        self.assertEqual(len(self.clips(result)), 2)
        self.assertEqual({n["id"]: n["startFrame"] for n in self.clips(result)},
                         {first["id"]: 60, second["id"]: 0})

    def test_overlapping_incoming_selection_is_atomic_error(self):
        store = self.store()
        first, second = self.clips(store.snapshot())
        head = store.head_path.read_bytes()
        with self.assertRaises(DocumentError):
            self.move(store, [{"id": first["id"], "track": "V2", "atFrame": 0},
                              {"id": second["id"], "track": "V2", "atFrame": 0}])
        self.assertEqual(store.head_path.read_bytes(), head)

    def test_child_ids_survive_clipping_reindexing_and_queued_fragment_reference(self):
        store = self.store([
            {"asset": "a", "in": 0, "out": 8, "text": [
                {"text": "only left", "at": .1, "for": .5},
                {"text": "spans", "at": .2, "for": 7},
                {"text": "only right", "at": 6, "for": 1}]},
            {"asset": "b", "in": 0, "out": 2},
        ])
        before = store.snapshot()
        victim, moving = self.clips(before)
        spanning = next(n for n in before["nodes"] if n["kind"] == "text" and n["raw"].get("text") == "spans")
        right_child = next(n for n in before["nodes"] if n["kind"] == "text" and n["raw"].get("text") == "only right")
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 90}])
        fragment = deterministic_id("frag-", EDIT, victim["id"], 150, 240)
        child = deterministic_id("node-", EDIT, spanning["id"], 150, 240)
        self.assertIn(child, {n["id"] for n in result["nodes"]})
        self.assertEqual(next(n for n in result["nodes"] if n["id"] == right_child["id"])["parentId"], fragment)
        result = self.move(store, [{"id": fragment, "track": "V2", "atFrame": 300}], EDIT2)
        node = next(n for n in self.clips(result) if n["id"] == fragment)
        self.assertEqual((node["sourceIn"], node["sourceOut"]), (5, 8))
        self.assertIn(child, {n["id"] for n in result["nodes"]})

    def test_primitive_and_structured_children_keep_ids_tracks_and_replay_through_split(self):
        store = self.store([
            {"asset": "a", "in": 0, "out": 8, "captions": "none", "text": [
                "string child", {"text": "spans", "at": 0, "for": 8},
                {"text": "later", "at": 2.1, "for": 5}]},
            {"asset": "b", "in": 0, "out": 1},
        ])
        before = store.snapshot()
        victim, moving = self.clips(before)
        children = {("string child" if isinstance(n["raw"], str) and n["kind"] == "text"
                     else n["raw"].get("text") if isinstance(n["raw"], dict)
                     else n["kind"]): n
                    for n in before["nodes"] if n["parentId"] == victim["id"]
                    and n["kind"] in ("text", "caption_settings")}
        self.apply(store, [
            {"op": "add_track", "kind": "video"}, {"op": "add_track", "kind": "video"},
            {"op": "rename_track", "id": "V4", "name": "Owned children"},
            {"op": "rename_track", "id": "V5", "name": "Unused locked row"},
            {"op": "set_track_locked", "id": "V5", "locked": True},
            *({"op": "set_node_track", "id": child["id"], "track": "V4"}
              for child in children.values()),
        ])
        tracks = deepcopy(store.snapshot()["plan"]["_editor"]["tracks"])
        head = store.head_path.read_bytes()
        original = store._warm_pool.compile
        def validate(*args, **kwargs):
            self.assertEqual(store.head_path.read_bytes(), head)
            return original(*args, **kwargs)
        with patch.object(store._warm_pool, "compile", side_effect=validate) as calls:
            result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 30}])
        self.assertEqual(calls.call_count, 1)
        fragment = deterministic_id("frag-", EDIT, victim["id"], 60, 240)
        by_id = {n["id"]: n for n in result["nodes"]}
        for name, child in children.items():
            self.assertEqual(by_id[child["id"]]["parentId"], fragment if name == "later" else victim["id"])
            self.assertEqual(by_id[child["id"]]["track"], "V4")
            if name != "later":
                duplicate = deterministic_id("node-", EDIT, child["id"], 60, 240)
                self.assertEqual(by_id[duplicate]["parentId"], fragment)
                self.assertEqual(by_id[duplicate]["track"], "V4")
                self.assertEqual(result["plan"]["_editor"]["nodes"][duplicate]["track"], "V4")
        self.assertEqual(result["plan"]["_editor"]["tracks"], tracks)
        operation = {"op": "split_segment", "id": fragment, "atFrame": 90}
        split_edit = str(uuid5(NAMESPACE_URL, canonical_json({
            "document": result["revisions"]["document"], "operation": operation})))
        result = self.apply(store, [operation])
        split_fragment = deterministic_id("frag-", split_edit, fragment, 90, 240)
        by_id = {n["id"]: n for n in result["nodes"]}
        for name in ("spans", "caption_settings", "later"):
            original_child = children[name]["id"]
            retained = (original_child if name == "later"
                        else deterministic_id("node-", EDIT, original_child, 60, 240))
            duplicated = deterministic_id("node-", split_edit, retained, 90, 240)
            self.assertEqual(by_id[retained]["parentId"], fragment)
            self.assertEqual(by_id[duplicated]["parentId"], split_fragment)
            self.assertEqual((by_id[retained]["track"], by_id[duplicated]["track"]), ("V4", "V4"))
        self.assertEqual(result["plan"]["_editor"]["tracks"], tracks)
        self.assertNotIn("__timelineEditorId", canonical_json(result["plan"]))
        self.apply(store, [{"op": "set_track_locked", "id": "V4", "locked": True}])
        head = store.head_path.read_bytes()
        with self.assertRaises(DocumentError) as caught:
            self.move(store, [{"id": split_fragment, "track": "V2", "atFrame": 300}], EDIT2)
        self.assertEqual(caught.exception.code, "track_locked")
        self.assertEqual(store.head_path.read_bytes(), head)

    def test_earliest_surviving_string_child_keeps_original_id_after_left_copy_is_removed(self):
        store = self.store([
            {"asset": "a", "in": 0, "out": 8, "text": ["string child"]},
            {"asset": "b", "in": 0, "out": 1},
        ])
        before = store.snapshot()
        victim, moving = self.clips(before)
        child = next(n for n in before["nodes"] if n["kind"] == "text")
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 1}])
        fragment = deterministic_id("frag-", EDIT, victim["id"], 31, 240)
        survivors = [n for n in result["nodes"] if n["kind"] == "text"]
        self.assertEqual([(n["id"], n["parentId"]) for n in survivors], [(child["id"], fragment)])
        owner = next(n for n in self.clips(result) if n["id"] == fragment)
        self.assertEqual((owner["sourceIn"], owner["sourceOut"]), (31/30, 8))

    def test_split_duplicates_string_child_with_deterministic_ids_and_custom_assignment(self):
        store = self.store([{"asset": "a", "in": 2, "out": 6, "text": ["string child"]}])
        before = store.snapshot()
        owner = self.clips(before)[0]
        child = next(n for n in before["nodes"] if n["kind"] == "text")
        self.move(store, [{"id": owner["id"], "track": "V2", "atFrame": 0}])
        before = self.apply(store, [
            {"op": "add_track", "kind": "video"},
            {"op": "set_node_track", "id": child["id"], "track": "V4"},
        ])
        operation = {"op": "split_segment", "id": owner["id"], "atFrame": 30}
        split_edit = str(uuid5(NAMESPACE_URL, canonical_json({
            "document": before["revisions"]["document"], "operation": operation})))
        result = self.apply(store, [operation])
        fragment = deterministic_id("frag-", split_edit, owner["id"], 30, 120)
        duplicate = deterministic_id("node-", split_edit, child["id"], 30, 120)
        nodes = {n["id"]: n for n in result["nodes"]}
        self.assertEqual((nodes[child["id"]]["parentId"], nodes[duplicate]["parentId"]),
                         (owner["id"], fragment))
        for node_id in (child["id"], duplicate):
            self.assertEqual(nodes[node_id]["track"], "V4")
            self.assertEqual(nodes[node_id]["raw"]["text"], "string child")
        self.assertEqual((nodes[owner["id"]]["sourceIn"], nodes[owner["id"]]["sourceOut"]), (2, 3))
        self.assertEqual((nodes[fragment]["sourceIn"], nodes[fragment]["sourceOut"]), (3, 6))
        self.assertEqual(nodes[duplicate]["raw"]["at"], 0)
        self.assertAlmostEqual(nodes[duplicate]["raw"]["for"], 1.35)

    def test_one_frame_survivors_retain_owned_events_ids_and_custom_tracks(self):
        from reels.progress import set_entry
        set_entry(self.temp.project, ftp=300, weight=75, date="2026-11-01")
        store = self.store([
            {"asset": "a", "in": 1, "out": 9, "speed": 2,
             "text": [{"text": "spans", "at": 0, "for": 4}],
             "progress": {"asOf": "2026-11-01", "at": 0, "for": 4},
             "cutaways": [{"asset": "c", "in": 2, "out": 6, "at": 0}]},
            {"asset": "b", "in": 0, "out": 118/30},
        ])
        before = store.snapshot()
        victim, moving = self.clips(before)
        text = next(n for n in before["nodes"] if n["kind"] == "text")
        cut = next(n for n in before["nodes"] if n["kind"] == "cutaway")
        progress = next(n for n in before["nodes"] if n["kind"] == "progress")
        self.apply(store, [
            {"op": "add_track", "kind": "video"},
            {"op": "set_node_track", "id": text["id"], "track": "V4"},
        ])
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 1}])
        tail = deterministic_id("frag-", EDIT, victim["id"], 119, 120)
        roots = {n["id"]: n for n in self.clips(result)}
        self.assertEqual((roots[victim["id"]]["frames"], roots[tail]["frames"]), (1, 1))
        self.assertAlmostEqual(roots[victim["id"]]["sourceOut"], 1 + 2/30)
        self.assertAlmostEqual(roots[tail]["sourceIn"], 1 + 238/30)
        self.assertEqual(roots[tail]["sourceOut"], 9)
        by_id = {n["id"]: n for n in result["nodes"]}
        for child in (text, cut, progress):
            duplicated = deterministic_id("node-", EDIT, child["id"], 119, 120)
            for node_id, owner in ((child["id"], victim["id"]), (duplicated, tail)):
                node = by_id[node_id]
                self.assertEqual((node["parentId"], node["track"]),
                                 (owner, {"text": "V4", "cutaway": "V2", "progress": "V3"}[child["kind"]]))
                self.assertEqual((node["frames"], node["timingState"]), (1, "resolved"))
                self.assertEqual((node["startFrame"], node["endFrame"]),
                                 (roots[owner]["startFrame"], roots[owner]["endFrame"]))
                if child["kind"] == "cutaway":
                    self.assertAlmostEqual(node["raw"]["out"] - node["raw"]["in"], 1/30)
        self.assertAlmostEqual(by_id[deterministic_id("node-", EDIT, cut["id"], 119, 120)]["raw"]["in"],
                               2 + 119/30)
        self.assertAlmostEqual(by_id[cut["id"]]["sourceIn"], 2)
        self.assertAlmostEqual(by_id[cut["id"]]["sourceOut"], 2 + 1/30)
        self.assertEqual(by_id[deterministic_id("node-", EDIT, cut["id"], 119, 120)]["sourceOut"], 6)
        self.assertEqual([(p["startFrame"], p["endFrame"], p["ownerSegmentIndex"])
                          for p in store._current_compiled["props"]["progress"]], [(0, 1, 0), (119, 120, 1)])
        for segment in store._current_compiled["props"]["segments"][:2]:
            self.assertEqual([(c["startFrame"], c["durationInFrames"]) for c in segment["cutaways"]], [(0, 1)])

    def test_short_children_keep_legacy_minima_only_without_positioned_mode(self):
        from reels.progress import bars, set_entry
        from reels.render import fit_cutaways
        set_entry(self.temp.project, ftp=300, weight=75, date="2026-11-01")
        for frames in (1, 7, 11, 14, 15):
            for explicit in (False, True):
                with self.subTest(frames=frames, explicit=explicit):
                    cut = {"start": 0, "seg": SimpleNamespace(frames=frames), "where": "test cutaway"}
                    segment = SimpleNamespace(index=0, start=0, end=frames, frames=frames, spec={
                        "_cutaways": [cut],
                        "progress": {"asOf": "2026-11-01", "at": 0, "for": frames/30},
                        **({"timeline": {"track": "V1"}} if explicit else {}),
                    })
                    warnings = []
                    fit_cutaways([segment], 30, warnings)
                    self.assertEqual(bool(segment.spec["_cutaways"]), explicit or frames >= 12)
                    progress = bars(self.temp.project, [segment], frames, 30, frames, warnings)
                    self.assertEqual(bool(progress), explicit or frames >= 15)
                    if progress:
                        self.assertEqual((progress[0]["startFrame"], progress[0]["endFrame"]), (0, frames))
                        self.assertEqual(progress[0].get("ownerSegmentIndex"), 0 if explicit else None)

    def test_locks_on_sources_destinations_and_owned_children(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8, "text": [{"text": "owned", "at": 1, "for": 2}]},
                            {"asset": "b", "in": 0, "out": 2}])
        first, second = self.clips(store.snapshot())
        self.apply(store, [{"op": "set_track_locked", "id": "V3", "locked": True}])
        for placements in (
            [{"id": first["id"], "track": "V2", "atFrame": 0}],
            [{"id": second["id"], "track": "V1", "atFrame": 30}],
            [{"id": second["id"], "track": "V3", "atFrame": 0}],
        ):
            head = store.head_path.read_bytes()
            with self.subTest(placements=placements), self.assertRaises(DocumentError) as caught:
                self.move(store, placements)
            self.assertEqual(caught.exception.code, "track_locked")
            self.assertEqual(store.head_path.read_bytes(), head)
        with self.assertRaises(DocumentError) as caught:
            self.apply(store, [{"op": "remove_segments", "ids": [first["id"]]}])
        self.assertEqual(caught.exception.code, "track_locked")

    def test_explicit_fields_cannot_bypass_locked_destination_or_overwrite(self):
        store = self.store()
        first, second = self.clips(store.snapshot())
        self.move(store, [{"id": second["id"], "track": "V2", "atFrame": 0}])
        self.apply(store, [{"op": "set_track_locked", "id": "V3", "locked": True}])
        for path, value in (("/timeline/track", "V3"), ("/timeline/startFrame", 10)):
            with self.assertRaises(DocumentError):
                self.apply(store, [{"op": "set_field", "id": second["id"], "path": path, "value": value}])

    def test_source_audio_wire_metadata_is_read_only_and_stable_on_vertical_move(self):
        store = self.store()
        before = store.snapshot()
        first, second = self.clips(before)
        audio = [n for n in before["nodes"] if n["kind"] == "source_audio"]
        self.assertEqual(len(audio), 2)
        for node, owner in zip(audio, (first, second)):
            self.assertEqual((node["parentId"], node["track"], node["lane"]),
                             (owner["id"], "A1", "audio"))
            self.assertEqual((node["startFrame"], node["endFrame"]),
                             (owner["startFrame"], owner["endFrame"]))
            self.assertTrue(node["generated"])
            self.assertTrue(node["readOnly"])
            self.assertFalse(node["editable"])
            self.assertEqual(node["raw"], node["resolved"])
            self.assertNotIn(node["id"], before["plan"]["_editor"]["nodes"])
        with self.assertRaises(DocumentError) as caught:
            self.apply(store, [{"op": "remove_overlay", "id": audio[0]["id"]}])
        self.assertEqual(caught.exception.code, "read_only_node")
        self.apply(store, [{"op": "set_track_locked", "id": "A1", "locked": True}])
        original = store._warm_pool.compile
        head = store.head_path.read_bytes()
        def validate(*args, **kwargs):
            self.assertEqual(store.head_path.read_bytes(), head)
            return original(*args, **kwargs)
        with patch.object(store._warm_pool, "compile", side_effect=validate) as calls:
            after = self.move(store, [{"id": second["id"], "track": "V2",
                                       "atFrame": second["startFrame"]}])
        self.assertEqual(calls.call_count, 1)
        self.assertEqual([n for n in after["nodes"] if n["kind"] == "source_audio"], audio)

    def test_a1_lock_rejects_movement_cuts_insert_and_audio_settings_before_head(self):
        store = self.store()
        first, second = self.clips(store.snapshot())
        self.apply(store, [{"op": "set_track_locked", "id": "A1", "locked": True}])
        operations = [
            {"op": "move_segments", "mode": "overwrite", "editId": EDIT,
             "placements": [{"id": second["id"], "track": "V2", "atFrame": 30}]},
            {"op": "move_segments", "mode": "overwrite", "editId": EDIT,
             "placements": [{"insert": {"asset": "c", "in": 0, "out": 1},
                             "track": "V2", "atFrame": 30}]},
            {"op": "split_segment", "id": first["id"], "atFrame": 90},
            {"op": "trim_segment", "id": first["id"], "sourceIn": 1, "sourceOut": 7},
            {"op": "remove_segments", "ids": [first["id"]]},
            {"op": "set_field", "id": first["id"], "path": "/volume", "value": 0},
            {"op": "set_field", "id": first["id"], "path": "/volume", "value": .5},
            {"op": "set_field", "id": first["id"], "path": "/bleep",
             "value": [{"at": 1, "for": .5}]},
        ]
        for operation in operations:
            with self.subTest(operation=operation):
                head = store.head_path.read_bytes()
                with self.assertRaises(DocumentError) as caught:
                    self.apply(store, [operation])
                self.assertEqual(caught.exception.code, "track_locked")
                self.assertEqual(store.head_path.read_bytes(), head)

    def test_a1_lock_rejects_unmute_but_allows_silent_inserts_and_visual_edits(self):
        self.temp.asset("silent", audio=False)
        store = self.store([{"asset": "a", "in": 0, "out": 8, "volume": 0}])
        first = self.clips(store.snapshot())[0]
        self.apply(store, [{"op": "set_track_locked", "id": "A1", "locked": True}])
        with self.assertRaises(DocumentError) as caught:
            self.apply(store, [{"op": "set_field", "id": first["id"], "path": "/volume", "value": 1}])
        self.assertEqual(caught.exception.code, "track_locked")
        after = self.move(store, [
            {"insert": {"asset": "b", "in": 0, "out": 1, "volume": 0}, "track": "V2", "atFrame": 30},
            {"insert": {"asset": "silent", "in": 0, "out": 1}, "track": "V3", "atFrame": 30},
        ])
        self.assertFalse(any(n["kind"] == "source_audio" for n in after["nodes"]))
        self.apply(store, [{"op": "set_field", "id": first["id"], "path": "/focusX", "value": .8}])

    def test_a1_unlock_allows_audio_move_and_unused_locks_allow_unrelated_edits(self):
        store = self.store()
        first, second = self.clips(store.snapshot())
        self.apply(store, [
            {"op": "set_track_locked", "id": "A1", "locked": True},
            {"op": "set_track_locked", "id": "A3", "locked": True},
            {"op": "set_track_locked", "id": "V3", "locked": True},
        ])
        self.apply(store, [{"op": "set_field", "id": first["id"], "path": "/focusX", "value": .8}])
        after = self.apply(store, [
            {"op": "set_track_locked", "id": "A1", "locked": False},
            {"op": "move_segments", "mode": "overwrite", "editId": EDIT,
             "placements": [{"id": second["id"], "track": "V2", "atFrame": 30}]},
            {"op": "set_track_locked", "id": "A1", "locked": True},
        ])
        self.assertTrue(next(row for row in after["tracks"] if row["id"] == "A1")["locked"])
        self.assertEqual(next(n for n in after["nodes"] if n["kind"] == "source_audio"
                              and n["parentId"] == second["id"])["startFrame"], 30)

    def test_a1_lock_protects_overwrite_victim_even_when_incoming_clip_is_silent(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8},
                            {"asset": "b", "in": 0, "out": 2, "volume": 0}])
        _, second = self.clips(store.snapshot())
        self.apply(store, [{"op": "set_track_locked", "id": "A1", "locked": True}])
        head = store.head_path.read_bytes()
        with self.assertRaises(DocumentError) as caught:
            self.move(store, [{"id": second["id"], "track": "V1", "atFrame": 30}])
        self.assertEqual(caught.exception.code, "track_locked")
        self.assertEqual(store.head_path.read_bytes(), head)

    def test_generated_bleep_lock_protects_owner_without_locking_other_segments(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8, "bleep": [{"at": 1, "for": .5}]},
                            {"asset": "b", "in": 0, "out": 2}])
        before = store.snapshot()
        first, second = self.clips(before)
        generated = next(n for n in before["nodes"] if n["kind"] == "generated_bleep")
        self.assertEqual(generated["track"], "A3")
        self.apply(store, [{"op": "set_track_locked", "id": "A3", "locked": True}])
        head = store.head_path.read_bytes()
        with self.assertRaises(DocumentError) as caught:
            self.move(store, [{"id": first["id"], "track": "V2", "atFrame": 30}])
        self.assertEqual(caught.exception.code, "track_locked")
        self.assertEqual(store.head_path.read_bytes(), head)
        result = self.move(store, [{"id": second["id"], "track": "V2", "atFrame": 30}])
        self.assertIn(generated, result["nodes"])
        with self.assertRaises(DocumentError) as caught:
            self.apply(store, [{"op": "remove_overlay", "id": generated["id"]}])
        self.assertEqual(caught.exception.code, "read_only_node")

    def test_inserted_empty_project_has_capability_and_accepts_positioned_library_drop(self):
        path = self.temp.plan("empty", [])
        store = DocumentStore(self.workspace, path)
        self.addCleanup(store.close)
        self.assertTrue(store.snapshot()["timelineCapabilities"]["libraryInsert"])
        result = self.move(store, [{"insert": {"asset": "a", "in": 0, "out": 2},
                                   "track": "V2", "atFrame": 90}])
        self.assertEqual([(n["track"], n["startFrame"], n["endFrame"]) for n in self.clips(result)],
                         [("V2", 90, 150)])
        self.assertEqual(result["durationFrames"], 150)

    def test_fps_change_rejects_locked_owned_items_atomically(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8, "text": [{"text": "owned", "at": 1, "for": 2}]}])
        first = self.clips(store.snapshot())[0]
        self.move(store, [{"id": first["id"], "track": "V2", "atFrame": 0}])
        self.apply(store, [{"op": "set_track_locked", "id": "V3", "locked": True}])
        head = store.head_path.read_bytes()
        with self.assertRaises(DocumentError) as caught:
            self.apply(store, [{"op": "set_field", "id": "project", "path": "/render/fps", "value": 60}])
        self.assertEqual(caught.exception.code, "track_locked")
        self.assertEqual(store.head_path.read_bytes(), head)

    def test_caption_owner_is_not_inferred_from_first_overlapping_segment(self):
        self.temp.asset("a", duration=30, words=words(("first", 1, 1.5)))
        self.temp.asset("b", duration=30, words=words(("second", 1.2, 1.7)))
        store = self.store([{"asset": "a", "in": 0, "out": 8, "captions": "transcript"},
                            {"asset": "b", "in": 0, "out": 2, "captions": "transcript"}])
        first, second = self.clips(store.snapshot())
        result = self.move(store, [{"id": second["id"], "track": "V2", "atFrame": 0}])
        indexed = {n["id"]: n for n in result["nodes"]}
        pages = [n for n in result["nodes"] if n["kind"] == "caption_page"]
        self.assertEqual({page["assetId"] for page in pages}, {"a", "b"})
        for page in pages:
            parent = indexed[page["parentId"]]
            self.assertEqual(parent["parentId"], first["id"] if page["assetId"] == "a" else second["id"])

    def test_owned_sfx_tail_uses_original_audio_offset_after_split(self):
        sfx = self.temp.root / "sfx"
        sfx.mkdir()
        (sfx / "click.wav").write_bytes(b"fixture audio")
        (sfx / "click.json").write_text('{"slug":"click","duration":4}')
        store = self.store([{"asset": "a", "in": 0, "out": 8, "sfx": [{"sound": "click", "at": 1}]},
                            {"asset": "b", "in": 0, "out": 2}])
        first, second = self.clips(store.snapshot())
        self.move(store, [{"id": second["id"], "track": "V1", "atFrame": 60}])
        cues = store._current_compiled["props"]["sfx"]
        # Original sound runs 1..5 s. Kept pieces are 1..2 and 4..5;
        # the latter must begin three seconds into the same sound.
        self.assertEqual([(cue["startFrame"], cue["durationInFrames"], cue.get("trimBefore", 0))
                          for cue in cues], [(30, 30, 0), (120, 30, 90)])

    def test_actual_track_rows_are_recovered_without_cosmetic_metadata(self):
        store = self.store([{"asset": "a", "in": 0, "out": 2,
                             "timeline": {"track": "V7", "startFrame": 30, "durationFrames": 60,
                                          "transitionInFrames": 0}}],
                           timeline={"version": 1, "fps": 30, "durationFrames": 120})
        snapshot = store.snapshot()
        self.assertEqual(self.clips(snapshot)[0]["track"], "V7")
        row = next(row for row in snapshot["tracks"] if row["id"] == "V7")
        self.assertEqual(row["canonicalId"], "V7")

    def test_promoted_cutaway_stays_silent_and_keeps_its_framing(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8, "cutaways": [
            {"asset": "b", "in": 2, "out": 4, "at": 1, "focusX": .8, "zoom": 1.2},
        ]}])
        before = store.snapshot()
        cut = next(n for n in before["nodes"] if n["kind"] == "cutaway")
        self.apply(store, [{"op": "set_track_locked", "id": "A1", "locked": True}])
        result = self.move(store, [{"id": cut["id"], "track": "V2", "atFrame": 30}])
        promoted = next(n for n in self.clips(result) if n["id"] == cut["id"])
        self.assertEqual(promoted["raw"]["volume"], 0)
        self.assertEqual((promoted["raw"]["focusX"], promoted["raw"]["zoom"]), (.8, 1.2))
        self.assertEqual(store._current_compiled["props"]["segments"][1]["volume"], 0)
        self.assertEqual(result["plan"]["segments"][0]["cutaways"], [])

    def test_cutaway_trim_and_transform_then_promotion_uses_fresh_source_in_one_rpc(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8, "cutaways": [
            {"asset": "b", "in": 2, "out": 6, "at": 1, "focusX": .2, "zoom": 1.1},
        ]}])
        cut = next(n for n in store.snapshot()["nodes"] if n["kind"] == "cutaway")
        with patch.object(store._warm_pool, "compile", wraps=store._warm_pool.compile) as calls:
            result = self.apply(store, [
                {"op": "trim_cutaway", "id": cut["id"], "sourceIn": 3, "sourceOut": 5},
                {"op": "set_field", "id": cut["id"], "path": "/focusX", "value": .8},
                {"op": "set_field", "id": cut["id"], "path": "/zoom", "value": 1.4},
                {"op": "move_segments", "mode": "overwrite", "editId": EDIT,
                 "placements": [{"id": cut["id"], "track": "V2", "atFrame": 90}]},
            ])
        self.assertEqual(calls.call_count, 1)
        promoted = next(n for n in self.clips(result) if n["id"] == cut["id"])
        self.assertEqual((promoted["sourceIn"], promoted["sourceOut"], promoted["frames"]), (3, 5, 60))
        self.assertEqual((promoted["raw"]["focusX"], promoted["raw"]["zoom"]), (.8, 1.4))
        self.assertEqual(promoted["raw"]["volume"], 0)

    def test_cutaway_resize_then_track_promotion_uses_fresh_duration_and_start(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8, "cutaways": [
            {"asset": "b", "in": 2, "out": 6, "at": 1, "speed": 2},
        ]}])
        cut = next(n for n in store.snapshot()["nodes"] if n["kind"] == "cutaway")
        result = self.apply(store, [
            {"op": "resize_overlay", "id": cut["id"], "at": 2, "for": .75},
            {"op": "set_node_track", "id": cut["id"], "track": "V2"},
        ])
        promoted = next(n for n in self.clips(result) if n["id"] == cut["id"])
        self.assertEqual((promoted["sourceIn"], promoted["sourceOut"], promoted["speed"]), (2, 3.5, 2))
        self.assertEqual((promoted["startFrame"], promoted["frames"]), (60, 22))

    def test_remove_earlier_cutaway_then_promote_uses_current_raw_index(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8, "cutaways": [
            {"asset": "b", "in": 1, "out": 2, "at": .5, "focusX": .2},
            {"asset": "c", "in": 10, "out": 12, "at": 3, "focusX": .8},
        ]}])
        first, second = [n for n in store.snapshot()["nodes"] if n["kind"] == "cutaway"]
        result = self.apply(store, [
            {"op": "remove_overlay", "id": first["id"]},
            {"op": "move_segments", "mode": "overwrite", "editId": EDIT,
             "placements": [{"id": second["id"], "track": "V2", "atFrame": 90}]},
        ])
        promoted = next(n for n in self.clips(result) if n["id"] == second["id"])
        self.assertEqual((promoted["assetId"], promoted["sourceIn"], promoted["sourceOut"]), ("c", 10, 12))
        self.assertEqual(promoted["raw"]["focusX"], .8)
        self.assertNotIn(first["id"], {n["id"] for n in result["nodes"]})
        self.assertEqual(result["plan"]["segments"][0]["cutaways"], [])

    def test_positioned_insert_rejects_nonfinite_and_precisely_out_of_source_bounds(self):
        store = self.store()
        for fields in (
            {"in": 0, "out": 30 + 1/3000},
            {"in": 0, "out": math.nextafter(30, math.inf)},
            {"in": -1/3000, "out": 1},
            {"in": 1, "out": 1},
            {"in": 0, "out": math.inf},
            {"in": math.nan, "out": 1},
            {"in": 0, "out": 1, "speed": math.nan},
            {"in": 0, "out": 1, "speed": math.inf},
        ):
            with self.subTest(fields=fields):
                head = store.head_path.read_bytes()
                with self.assertRaises(DocumentError):
                    self.move(store, [{"insert": {"asset": "c", **fields}, "track": "V2", "atFrame": 0}])
                self.assertEqual(store.head_path.read_bytes(), head)

    def test_positioned_trim_rejects_nonfinite_and_precisely_out_of_source_bounds(self):
        store = self.store()
        first = self.clips(store.snapshot())[0]
        self.move(store, [{"id": first["id"], "track": "V2", "atFrame": 0}])
        for source_in, source_out in (
            (0, 30 + 1/3000), (0, math.nextafter(30, math.inf)), (-1/3000, 1),
            (1, 1), (0, math.inf), (math.nan, 1),
        ):
            with self.subTest(source_in=source_in, source_out=source_out):
                head = store.head_path.read_bytes()
                with self.assertRaises(DocumentError):
                    self.apply(store, [{"op": "trim_segment", "id": first["id"],
                                        "sourceIn": source_in, "sourceOut": source_out}])
                self.assertEqual(store.head_path.read_bytes(), head)
        for path, value in (("/out", 30 + 1/3000), ("/out", math.nextafter(30, math.inf)),
                            ("/in", -1/3000), ("/out", math.inf), ("/in", math.nan)):
            with self.subTest(path=path, value=value):
                head = store.head_path.read_bytes()
                with self.assertRaises(DocumentError):
                    self.apply(store, [{"op": "set_field", "id": first["id"], "path": path, "value": value}])
                self.assertEqual(store.head_path.read_bytes(), head)

    def test_positioned_video_source_bounds_preserve_subframe_precision_at_asset_end(self):
        duration = 30 + 1/3000
        self.temp.asset("c", duration=duration)
        store = self.store()
        source_in = duration - 1/30
        result = self.move(store, [{"insert": {"asset": "c", "in": source_in, "out": duration},
                                    "track": "V2", "atFrame": 0}])
        inserted = next(n for n in self.clips(result) if n["assetId"] == "c")
        self.assertEqual((inserted["sourceIn"], inserted["sourceOut"], inserted["frames"]),
                         (source_in, duration, 1))
        source_in = 1/3000
        source_out = math.nextafter(duration, -math.inf)
        result = self.apply(store, [{"op": "trim_segment", "id": inserted["id"],
                                    "sourceIn": source_in, "sourceOut": source_out}])
        trimmed = next(n for n in self.clips(result) if n["id"] == inserted["id"])
        self.assertEqual((trimmed["sourceIn"], trimmed["sourceOut"]), (source_in, source_out))

    def test_positioned_trim_split_delete_speed_fps_and_duplicate(self):
        store = self.store()
        first, moving = self.clips(store.snapshot())
        self.move(store, [{"id": moving["id"], "track": "V2", "atFrame": 300}])
        self.apply(store, [{"op": "trim_segment", "id": first["id"], "sourceIn": 1, "sourceOut": 7},
                           {"op": "split_segment", "id": first["id"], "atFrame": 90}])
        result = self.apply(store, [{"op": "set_field", "id": moving["id"], "path": "/speed", "value": 2},
                                    {"op": "set_field", "id": "project", "path": "/render/fps", "value": 60}])
        node = next(n for n in self.clips(result) if n["id"] == moving["id"])
        self.assertEqual((node["startFrame"], node["frames"]), (600, 60))
        self.assertEqual(result["plan"]["timeline"]["fps"], 60)
        duplicate = deepcopy(node["raw"])
        result = self.apply(store, [{"op": "insert_segment", "value": duplicate}])
        self.assertEqual(len([n for n in self.clips(result) if n["assetId"] == "b"]), 2)
        ids = [n["id"] for n in self.clips(result)]
        result = self.apply(store, [{"op": "remove_segments", "ids": ids}])
        self.assertEqual(self.clips(result), [])
        self.assertGreater(result["durationFrames"], 0)
        self.assertTrue(valid_result(store._current_compiled))

    def test_legacy_fades_keep_compiled_positions_until_placement(self):
        store = self.store([{"asset": "a", "in": 0, "out": 4},
                            {"asset": "b", "in": 0, "out": 4, "transition": "fade"},
                            {"asset": "c", "in": 0, "out": 2}])
        before = store.snapshot()
        self.assertNotIn("timeline", before["plan"])
        first, second, third = self.clips(before)
        result = self.move(store, [{"id": third["id"], "track": "V2", "atFrame": third["startFrame"]}])
        unchanged = {n["id"]: n for n in self.clips(result)}
        for node in (first, second):
            self.assertEqual((unchanged[node["id"]]["startFrame"], unchanged[node["id"]]["endFrame"]),
                             (node["startFrame"], node["endFrame"]))
        self.assertEqual(store._current_compiled["props"]["segments"][1]["transitionInFrames"],
                         second["transitionInFrames"])

    def test_middle_overwrite_preserves_victim_original_incoming_fade(self):
        store = self.store([{"asset": "a", "in": 0, "out": 4},
                            {"asset": "b", "in": 0, "out": 4, "transition": "fade"},
                            {"asset": "c", "in": 0, "out": 1}])
        before = store.snapshot()
        predecessor, victim, moving = self.clips(before)
        self.assertGreater(victim["transitionInFrames"], 0)
        at = victim["startFrame"] + 60
        head = store.head_path.read_bytes()
        original = store._warm_pool.compile
        def validate(*args, **kwargs):
            self.assertEqual(store.head_path.read_bytes(), head)
            return original(*args, **kwargs)
        with patch.object(store._warm_pool, "compile", side_effect=validate) as calls:
            result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": at}])
        self.assertEqual(calls.call_count, 1)
        nodes = {n["id"]: n for n in self.clips(result)}
        kept = nodes[victim["id"]]
        self.assertEqual((kept["startFrame"], kept["endFrame"]), (victim["startFrame"], at))
        self.assertEqual(kept["transitionInFrames"], victim["transitionInFrames"])
        self.assertEqual(kept["raw"]["transition"], "fade")
        self.assertEqual((kept["sourceIn"], kept["sourceOut"]), (0, 2))
        self.assertEqual(nodes[predecessor["id"]]["endFrame"], predecessor["endFrame"])
        tail = nodes[deterministic_id("frag-", EDIT, victim["id"], at + 30, victim["endFrame"])]
        self.assertEqual((tail["transitionInFrames"], tail["raw"]["transition"]), (0, "cut"))
        self.assertEqual((tail["sourceIn"], tail["sourceOut"]), (3, 4))

    def test_overwrite_inside_fade_preserves_full_short_head_overlap(self):
        for frames in (1, 2):
            with self.subTest(head_frames=frames):
                store = self.store([
                    {"asset": "a", "in": 0, "out": 100/30,
                     "timeline": {"track": "V1", "startFrame": 0, "durationFrames": 100, "transitionInFrames": 0}},
                    {"asset": "b", "in": 0, "out": 100/30, "transition": "fade",
                     "timeline": {"track": "V1", "startFrame": 88, "durationFrames": 100, "transitionInFrames": 12}},
                ], timeline={"version": 1, "fps": 30, "durationFrames": 188})
                first, second = self.clips(store.snapshot())
                at = 88 + frames
                result = self.move(store, [{"insert": {"asset": "c", "in": 0, "out": 20/30},
                                            "track": "V1", "atFrame": at}])
                nodes = {node["id"]: node for node in self.clips(result)}
                self.assertEqual(nodes[first["id"]]["endFrame"], at)
                head = nodes[second["id"]]
                self.assertEqual((head["startFrame"], head["endFrame"], head["frames"],
                                  head["transitionInFrames"]), (88, at, frames, frames))
                self.assertEqual(head["raw"]["transition"], "fade")
                self.assertAlmostEqual(head["sourceOut"], frames/30)
                tail = nodes[deterministic_id("frag-", EDIT, second["id"], at + 20, 188)]
                self.assertEqual((tail["startFrame"], tail["endFrame"], tail["transitionInFrames"]),
                                 (at + 20, 188, 0))
                self.assertTrue(valid_result(store._current_compiled))

    def test_full_length_short_fades_render_at_the_correct_frames(self):
        from test_editor_overwrite_render import PositionedRenderTests, ROOT
        if not shutil.which("ffmpeg") or not (ROOT / "remotion/node_modules/.bin/remotion").exists():
            self.skipTest("installed ffmpeg and Remotion required")
        fixture, project, built, public, _ = PositionedRenderTests._color_fixture(self)
        for duration in (1, 2):
            props = deepcopy(built["props"])
            red, blue = props["segments"]
            red.update(track="V1", startFrame=0, transitionInFrames=0)
            blue.update(track="V1", startFrame=8-duration, durationInFrames=duration,
                        transitionInFrames=duration)
            props.update(timeline={"version": 1, "fps": 10, "durationFrames": 8}, totalFrames=8)
            pixels = PositionedRenderTests._frames(self, fixture, project, public, props, f"short-fade-{duration}")
            red_channel, blue_channel = pixels[-1, 48, 48, 0], pixels[-1, 48, 48, 2]
            if duration == 1:
                self.assertGreater(red_channel, blue_channel + 100)
            else:
                self.assertGreater(red_channel, 70)
                self.assertGreater(blue_channel, 70)

    def test_fractional_frame_fast_source_tail_keeps_exact_outer_source_out(self):
        self.temp.asset("c", duration=1)
        self.temp.asset("d", duration=1.01)
        store = self.store([{"asset": "c", "in": 0, "out": 1, "speed": 4},
                            {"asset": "d", "in": 0, "out": 1.01, "speed": 4}])
        before = self.clips(store.snapshot())
        self.assertEqual([n["frames"] for n in before], [8, 8])
        result = self.move(store, [
            {"insert": {"asset": "b", "in": 0, "out": 1/30}, "track": "V1", "atFrame": 0},
            {"insert": {"asset": "b", "in": 0, "out": 1/30}, "track": "V1", "atFrame": 8},
        ])
        nodes = {n["id"]: n for n in self.clips(result)}
        for original in before:
            tail = nodes[original["id"]]
            self.assertEqual((tail["startFrame"], tail["endFrame"], tail["frames"]),
                             (original["startFrame"] + 1, original["endFrame"], 7))
            self.assertEqual(tail["sourceIn"], 4/30)
            self.assertEqual(tail["sourceOut"], original["sourceOut"])
            self.assertEqual(tail["raw"]["out"], original["raw"]["out"])
        self.assertTrue(valid_result(store._current_compiled))

    def test_default_text_intervals_are_resolved_before_clipping_and_not_restarted(self):
        store = self.store([{"asset": "a", "in": 0, "out": 8, "text": [
            "primitive", {"text": "implicit"}, {"text": "explicit start", "at": .3},
            {"text": "implicit start", "for": 4}]},
            {"asset": "b", "in": 0, "out": 2}])
        before = store.snapshot()
        victim, moving = self.clips(before)
        children = {n["raw"] if isinstance(n["raw"], str) else n["raw"]["text"]: n["id"]
                    for n in before["nodes"] if n["kind"] == "text"}
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 0}])
        nodes = {n["id"]: n for n in result["nodes"]}
        for name, duration in (("primitive", .35), ("implicit", .35),
                               ("explicit start", .5), ("implicit start", 2.15)):
            child = nodes[children[name]]
            self.assertEqual(child["parentId"], victim["id"])
            self.assertEqual(child["raw"]["at"], 0)
            self.assertAlmostEqual(child["raw"]["for"], duration)
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 75}], EDIT2)
        tail = deterministic_id("frag-", EDIT2, victim["id"], 135, 240)
        self.assertIn(tail, {n["id"] for n in self.clips(result)})
        self.assertFalse(any(n["kind"] == "text" and n["parentId"] == tail for n in result["nodes"]))
        self.assertTrue(set(children.values()).issubset({n["id"] for n in result["nodes"]}))

    def test_sliced_360_camera_keeps_the_original_easing_and_one_frame_is_valid(self):
        from reels.render import build
        from reels.sphere import path_value
        asset_path = self.temp.project.assets_dir / "a.json"
        asset = json.loads(asset_path.read_text())
        asset["probe"]["sphere"] = {"projection": "equirectangular"}
        asset_path.write_text(json.dumps(asset))
        store = self.store([
            {"asset": "a", "in": 0, "out": 8,
             "path": [{"at": 0, "yaw": 0}, {"at": 8, "yaw": 100}]},
            {"asset": "b", "in": 0, "out": 2},
        ])
        before = store.snapshot()
        first, second = self.clips(before)
        original = deepcopy(store._current_compiled["segs"][0]["spec"]["sphere"]["path"])
        result = self.move(store, [{"id": second["id"], "track": "V1", "atFrame": 90}])
        right_id = deterministic_id("frag-", EDIT, first["id"], 150, 240)
        right_index = next(index for index, node in enumerate(self.clips(result)) if node["id"] == right_id)
        right = store._current_compiled["segs"][right_index]["spec"]["sphere"]["path"]
        for at in (0, 1/30, .5, 1.7, 2.9):
            self.assertAlmostEqual(path_value(right, at)["yaw"], path_value(original, 5+at)["yaw"], places=10)
        result = self.move(store, [{"insert": {"asset": "c", "in": 0, "out": 89/30},
                                   "track": "V1", "atFrame": 1}], EDIT2)
        one = next(node for node in self.clips(result) if node["id"] == first["id"])
        self.assertEqual(one["frames"], 1)

    def test_photo_survivor_keeps_original_motion_phase(self):
        self.temp.asset("photo", kind="photo")
        store = self.store([{"asset": "photo", "duration": 8, "motion": "pan-right"},
                            {"asset": "b", "in": 0, "out": 2}])
        first, moving = self.clips(store.snapshot())
        result = self.move(store, [{"id": moving["id"], "track": "V1", "atFrame": 90}])
        piece = deterministic_id("frag-", EDIT, first["id"], 150, 240)
        index = next(index for index, node in enumerate(self.clips(result)) if node["id"] == piece)
        props = store._current_compiled["props"]["segments"][index]
        self.assertEqual((props["motionOffsetFrames"], props["motionDurationFrames"]), (150, 240))
