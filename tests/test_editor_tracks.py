from __future__ import annotations

import json
import unittest
from copy import deepcopy

from pydantic import ValidationError

from helpers import TempProject
from reels.editor.document import editing_snapshot
from reels.editor.operations import OperationBatch
from reels.editor.tracks import default_track, track_list
from reels.editor.transactions import DocumentError, DocumentStore
from test_editor_transactions import Workspace, fake_compile


class EditorTrackTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.workspace = Workspace(self.temp.project)
        self.temp.asset("asset-a")
        self.temp.asset("asset-b")
        self.path = self.temp.plan("tracks", [
            {"asset": "asset-a", "in": 0, "out": 5},
            {"asset": "asset-b", "in": 1, "out": 6},
        ], text=[{"text": "global beat", "at": 8, "for": 1}], musicDrops=[{"at": 9, "for": 0.5}])
        self.store = DocumentStore(self.workspace, self.path, compiler=fake_compile)
        self.addCleanup(self.store.close)

    def apply(self, *ops, snap=None):
        snap = snap or self.store.snapshot()
        return self.store.apply({"expected": snap["revisions"], "operations": list(ops)})

    def node(self, snap, kind):
        return next(n for n in snap["nodes"] if n["kind"] == kind)

    def test_default_snapshot_tracks_and_node_mapping(self):
        snap = self.store.snapshot()
        self.assertEqual([t["id"] for t in snap["tracks"]], ["V1", "V2", "V3", "A1", "A2", "A3"])
        self.assertTrue(all(not t["locked"] and not t["userAdded"] for t in snap["tracks"]))
        self.assertEqual(self.node(snap, "segment")["track"], "V1")
        self.assertEqual(self.node(snap, "text")["track"], "V3")
        self.assertEqual(self.node(snap, "music_drop")["track"], "A2")
        self.assertEqual(default_track("sfx"), "A3")
        self.assertEqual(default_track("cutaway"), "V2")

    def test_add_rename_lock_remove_and_undo(self):
        snap = self.apply({"op": "add_track", "kind": "video"})
        self.assertIn("V4", [t["id"] for t in snap["tracks"]])
        snap = self.apply({"op": "add_track", "kind": "audio"}, {"op": "add_track", "kind": "audio"}, snap=snap)
        self.assertEqual([t["id"] for t in snap["tracks"] if t["kind"] == "audio"], ["A1", "A2", "A3", "A4", "A5"])
        snap = self.apply({"op": "rename_track", "id": "V4", "name": "Titles"},
                          {"op": "set_track_locked", "id": "V4", "locked": True}, snap=snap)
        v4 = next(t for t in snap["tracks"] if t["id"] == "V4")
        self.assertEqual((v4["name"], v4["locked"], v4["userAdded"]), ("Titles", True, True))
        # Built-in tracks can be renamed/locked but not removed.
        with self.assertRaises(DocumentError):
            self.apply({"op": "remove_track", "id": "V1"}, snap=snap)
        snap = self.apply({"op": "remove_track", "id": "V4"}, snap=snap)
        self.assertNotIn("V4", [t["id"] for t in snap["tracks"]])
        undone = self.store.undo(snap["revisions"])
        self.assertIn("V4", [t["id"] for t in undone["tracks"]])

    def test_set_node_track_kind_rules_and_remove_nonempty(self):
        snap = self.apply({"op": "add_track", "kind": "video"})
        text = self.node(snap, "text")
        snap = self.apply({"op": "set_node_track", "id": text["id"], "track": "V4"}, snap=snap)
        self.assertEqual(self.node(snap, "text")["track"], "V4")
        with self.assertRaises(DocumentError):
            self.apply({"op": "remove_track", "id": "V4"}, snap=snap)
        with self.assertRaises(DocumentError):
            self.apply({"op": "set_node_track", "id": text["id"], "track": "A1"}, snap=snap)
        drop = self.node(snap, "music_drop")
        with self.assertRaises(DocumentError):
            self.apply({"op": "set_node_track", "id": drop["id"], "track": "V2"}, snap=snap)
        snap = self.apply({"op": "set_node_track", "id": text["id"], "track": "V2"}, snap=snap)
        snap = self.apply({"op": "remove_track", "id": "V4"}, snap=snap)
        self.assertEqual(self.node(snap, "text")["track"], "V2")

    def test_locked_track_rejects_edits(self):
        snap = self.store.snapshot()
        text = self.node(snap, "text")
        snap = self.apply({"op": "set_track_locked", "id": "V3", "locked": True}, snap=snap)
        for op in ({"op": "set_field", "id": text["id"], "path": "/text", "value": "x"},
                   {"op": "move_overlay", "id": text["id"], "at": 1},
                   {"op": "remove_overlay", "id": text["id"]},
                   {"op": "set_node_track", "id": text["id"], "track": "V2"}):
            with self.subTest(op=op["op"]), self.assertRaises(DocumentError) as ctx:
                self.apply(op, snap=snap)
            self.assertIn("locked", str(ctx.exception))
        # Other tracks stay editable; unlocking restores edits.
        seg = self.node(snap, "segment")
        snap = self.apply({"op": "set_field", "id": seg["id"], "path": "/volume", "value": 0.5}, snap=snap)
        snap = self.apply({"op": "set_track_locked", "id": "V3", "locked": False}, snap=snap)
        self.apply({"op": "set_field", "id": text["id"], "path": "/text", "value": "x"}, snap=snap)

    def test_tracks_do_not_reach_plan_file_or_render_timeline(self):
        before = self.store.snapshot()
        on_disk = json.loads(self.path.read_text())
        snap = self.apply({"op": "add_track", "kind": "video"})
        text = self.node(snap, "text")
        snap = self.apply({"op": "set_node_track", "id": text["id"], "track": "V4"}, snap=snap)
        self.assertEqual(json.loads(self.path.read_text()), on_disk)
        self.assertNotIn("_editor", json.loads(self.path.read_text()))
        self.assertEqual(snap["durationFrames"], before["durationFrames"])
        strip = lambda n: {k: v for k, v in n.items() if k not in ("track", "id")}
        self.assertEqual([strip(n) for n in snap["nodes"]], [strip(n) for n in before["nodes"]])
        # Tracks survive a reopen via the sidecar.
        self.store.close()
        reopened = DocumentStore(self.workspace, self.path, compiler=fake_compile)
        self.addCleanup(reopened.close)
        again = reopened.snapshot()
        self.assertIn("V4", [t["id"] for t in again["tracks"]])
        self.assertEqual(self.node(again, "text")["track"], "V4")

    def test_plan_with_editor_tracks_loads_and_builds_the_same(self):
        from reels import render
        project = self.temp.project
        base = render.build(project, render.load_plan(project, self.path))
        plan = json.loads(self.path.read_text())
        plan["_editor"] = {"tracks": [{"id": "V4", "kind": "video", "name": "V4", "locked": True}],
                           "nodes": {"x": {"kind": "text", "track": "V4"}}}
        tracked_path = self.path.with_name("tracked.json")
        tracked_path.write_text(json.dumps(plan))
        tracked = render.build(project, render.load_plan(project, tracked_path))
        self.assertEqual((tracked["fps"], tracked["total"]), (base["fps"], base["total"]))
        self.assertEqual(tracked["props"], base["props"])

    def test_operation_schema(self):
        exp = {"document": "d", "library": "l", "renderer": "r"}
        def batch(op):
            return OperationBatch.model_validate({"expected": exp, "reason": "t", "operations": [op]})
        batch({"op": "add_track", "kind": "audio"})
        batch({"op": "rename_track", "id": "A4", "name": "Voice"})
        batch({"op": "set_track_locked", "id": "V1", "locked": True})
        batch({"op": "set_node_track", "id": "n", "track": "V2"})
        for bad in ({"op": "add_track", "kind": "text"}, {"op": "rename_track", "id": "A4"},
                    {"op": "set_node_track", "id": "n", "track": "Z9"}):
            with self.assertRaises(ValidationError):
                batch(bad)

    def test_track_list_ignores_junk_records(self):
        tracks = track_list({"tracks": [{"id": "X9", "kind": "video"}, {"id": "V2", "kind": "audio"}, "junk",
                                        {"id": "V5", "kind": "video", "name": "B"}]})
        self.assertEqual([t["id"] for t in tracks if t["userAdded"]], ["V5"])


if __name__ == "__main__":
    unittest.main()
