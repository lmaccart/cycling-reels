from __future__ import annotations

import json
import threading
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from helpers import TempProject, words
from reels.editor.transactions import DocumentError, DocumentStore
from reels.editor.document import editing_snapshot, ensure_node_ids


class Workspace:
    def __init__(self, project):
        self.project = project
        self.root = project.root
        self.data_root = project.root


def fake_compile(project, plan, overrides=None, renderer_revision=None):
    fps = int(project.cfg("render", "fps"))
    segments = []
    start = 0
    prev_frames = 0
    for i, raw in enumerate(plan.get("segments") or []):
        frames = round(((float(raw.get("out", 1)) - float(raw.get("in", 0))) /
                        float(raw.get("speed", 1))) * fps)
        frames = max(1, frames)
        trans = min(int(project.cfg("render", "transitionFrames")), prev_frames - 1, frames - 1) \
            if i and raw.get("transition") == "fade" else 0
        if i:
            start = segments[-1]["start"] + prev_frames - trans
        segments.append({"index": i, "kind": "video", "asset": {"id": raw.get("asset")},
                         "spec": {**raw, "speed": raw.get("speed", 1)}, "frames": frames,
                         "trans": trans, "start": start, "cutaways": []})
        prev_frames = frames
    total = segments[-1]["start"] + segments[-1]["frames"] if segments else 0
    return {"fps": fps, "total": total, "segs": segments, "warnings": [],
            "props": {}, "sound": {"cues": []}, "loop": None}


class EditorTransactionsTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.workspace = Workspace(self.temp.project)

    def make_store(self, *, compiler=None):
        self.temp.asset("asset-a", words=words(("hello", 1.0, 1.3)))
        self.temp.asset("asset-b")
        path = self.temp.plan("2026-11-14-test", [
            {"asset": "asset-a", "in": 0, "out": 5, "customUnknown": {"keep": True}},
            {"asset": "asset-b", "in": 1, "out": 6, "transition": "fade"},
        ], text=[{"text": "global beat", "at": 8, "for": 1}], musicDrops=[{"at": 9, "for": 0.5}])
        return DocumentStore(self.workspace, path, compiler=compiler), path

    def test_cutaway_source_edits_are_local_and_photos_use_duration(self):
        self.temp.asset("voice", words=words(("talk", 1, 1.2), ("more", 4, 4.4)))
        self.temp.asset("broll", duration=20)
        self.temp.asset("photo", kind="photo")
        path = self.temp.plan("broll-edits", [{
            "asset": "voice", "in": 0, "out": 9, "captions": "transcript",
            "bleep": {"windows": [{"at": 4, "for": .3}]},
            # Deliberately unsorted: compiled cuts must retain raw provenance.
            "cutaways": [{"asset": "photo", "at": 6},
                         {"asset": "broll", "at": 1, "in": 2, "out": 8, "speed": 2, "custom": {"keep": 1}}],
        }], text=[{"text": "fixed", "at": 7, "for": 1}])
        store = DocumentStore(self.workspace, path)
        self.addCleanup(store.close)
        before = store.snapshot()
        video = next(n for n in before["nodes"] if n["kind"] == "cutaway" and n.get("assetId") == "broll")
        photo = next(n for n in before["nodes"] if n["kind"] == "cutaway" and n.get("assetId") == "photo")
        self.assertEqual(photo["assetKind"], "photo")
        self.assertNotIn("duration", photo["raw"])
        self.assertEqual(video["assetKind"], "video")
        self.assertEqual(next(n for n in before["nodes"] if n["kind"] == "segment")["assetKind"], "video")
        parent = before["plan"]["segments"][0]
        result = store.apply({"expected": before["revisions"], "operations": [
            {"op": "trim_cutaway", "id": video["id"], "sourceIn": 3, "sourceOut": 7},
            {"op": "move_overlay", "id": video["id"], "at": 2},
            {"op": "resize_overlay", "id": video["id"], "for": 1.5},
            {"op": "resize_overlay", "id": photo["id"], "for": 1.2},
        ]})
        raw = result["plan"]["segments"][0]
        self.assertEqual(raw["cutaways"][1]["in"], 3)
        self.assertEqual(raw["cutaways"][1]["out"], 6)
        self.assertEqual(raw["cutaways"][1]["at"], 2)
        self.assertEqual(raw["cutaways"][1]["custom"], {"keep": 1})
        self.assertEqual(raw["cutaways"][0]["duration"], 1.2)
        self.assertNotIn("for", raw["cutaways"][0])
        for key in ("in", "out", "captions", "bleep"):
            self.assertEqual(raw[key], parent[key])
        self.assertEqual(result["plan"]["text"], before["plan"]["text"])
        self.assertEqual(result["durationFrames"], before["durationFrames"])
        self.assertEqual({n["id"] for n in result["nodes"] if n["kind"] == "cutaway"},
                         {video["id"], photo["id"]})
        with self.assertRaises(DocumentError):
            store.apply({"expected": result["revisions"], "operations": [
                {"op": "trim_segment", "id": video["id"], "sourceIn": 3, "sourceOut": 21},
            ]})
        self.assertEqual(store.snapshot()["revisions"], result["revisions"])

    def test_remove_overlay_and_reserved_insert_collections(self):
        store, path = self.make_store(compiler=fake_compile)
        before = store.snapshot()
        segment = next(n for n in before["nodes"] if n["kind"] == "segment")
        text = next(n for n in before["nodes"] if n["kind"] == "text")
        for collection in ("segments", "_editor", "history", "../segments", ""):
            with self.subTest(collection=collection), self.assertRaises(DocumentError):
                store.apply({"expected": before["revisions"], "operations": [
                    {"op": "insert_overlay", "collection": collection, "value": {"asset": "asset-a"}},
                ]})
        with self.assertRaises(DocumentError):
            store.apply({"expected": before["revisions"], "operations": [
                {"op": "remove_overlay", "id": segment["id"]},
            ]})
        result = store.apply({"expected": before["revisions"], "operations": [
            {"op": "remove_overlay", "id": text["id"]},
        ]})
        self.assertEqual(json.loads(path.read_text())["text"], [])
        self.assertEqual(result["durationFrames"], before["durationFrames"])

    def test_generated_cue_multi_move_and_remove_materializes_all_siblings_once(self):
        def compile_cues(project, plan, overrides):
            result = fake_compile(project, plan, overrides)
            if plan.get("autoSfx"):
                result["sound"]["cues"] = [
                    {"source": f"auto: event-{i}", "sound": f"sound-{i}", "startFrame": i * 30,
                     "durationInFrames": 5, "gain": .5} for i in range(1, 5)]
            return result
        store, path = self.make_store(compiler=compile_cues)
        plan = json.loads(path.read_text())
        plan["autoSfx"] = True
        path.write_text(json.dumps(plan))
        before = store.reload_external()
        cues = [n for n in before["nodes"] if n["kind"] == "generated_sfx"]
        result = store.apply({"expected": before["revisions"], "operations": [
            {"op": "move_overlay", "id": cues[0]["id"], "at": .5},
            {"op": "move_overlay", "id": cues[1]["id"], "at": 1.5},
            {"op": "remove_overlay", "id": cues[2]["id"]},
        ]})
        self.assertFalse(result["plan"]["autoSfx"])
        self.assertEqual([(c["sound"], c["at"]) for c in result["plan"]["sfx"]],
                         [("sound-1", .5), ("sound-2", 1.5), ("sound-4", 4)])
        self.assertFalse(any(n["kind"] == "generated_sfx" for n in result["nodes"]))
        # Removing an automatic cue as the first operation also preserves peers.
        undone = store.undo(result["revisions"])
        target = next(n for n in undone["nodes"] if n["kind"] == "generated_sfx")
        removed = store.apply({"expected": undone["revisions"], "operations": [
            {"op": "remove_overlay", "id": target["id"]},
        ]})
        self.assertEqual(len(removed["plan"]["sfx"]), 3)
        self.assertFalse(removed["plan"]["autoSfx"])

    def test_ai_cannot_borrow_manual_lease_token(self):
        store, _ = self.make_store(compiler=fake_compile)
        before = store.snapshot()
        lease = store.acquire_lease("manual")
        batch = {"expected": before["revisions"], "origin": "ai", "lease": lease["token"],
                 "operations": [{"op": "set_field", "path": "/note", "value": "changed"}]}
        for action in (store.apply, store.candidate_snapshot):
            with self.assertRaises(DocumentError) as caught:
                action(batch)
            self.assertEqual(caught.exception.code, "lease_conflict")
        result = store.apply({**batch, "origin": "manual"})
        self.assertEqual(result["plan"]["note"], "changed")
        store.release_lease(lease["token"])

    def test_actual_renderer_collision_timing_and_dropped_duplicate_text(self):
        self.temp.asset("talk", words=words(
            ("one", .2, .7), ("two", .8, 1.3), ("three", 1.4, 2),
            ("four", 2.1, 2.6), ("five", 2.7, 3.2)))
        path = self.temp.plan("collisions", [{"asset": "talk", "in": 0, "out": 8, "captions": "transcript"}],
                              hook={"text": "hook", "seconds": 2}, endCard={"seconds": 1},
                              text=[{"text": "identical", "at": .2, "for": .5, "position": "top"},
                                    {"text": "identical", "at": .2, "for": 3.5, "position": "top"}])
        store = DocumentStore(self.workspace, path)
        self.addCleanup(store.close)
        result = store.snapshot()
        text = [n for n in result["nodes"] if n["kind"] == "text"]
        self.assertEqual(text[0]["timingState"], "dropped")
        self.assertEqual(text[1]["timingState"], "resolved")
        self.assertEqual(text[1]["startFrame"], 60)
        self.assertEqual(text[1]["raw"]["at"], .2)
        self.assertEqual(text[1]["resolved"], store._current_compiled["props"]["beats"][0])
        hook = next(n for n in result["nodes"] if n["kind"] == "hook")
        end_card = next(n for n in result["nodes"] if n["kind"] == "endCard")
        self.assertEqual(hook["endFrame"], store._current_compiled["props"]["hook"]["endFrame"])
        self.assertEqual(end_card["startFrame"], store._current_compiled["props"]["endCard"]["startFrame"])
        self.assertTrue(any(n["kind"] == "caption_page" and n["generated"] for n in result["nodes"]))
        self.assertTrue(any(n["kind"] == "caption_word" and n["generated"] for n in result["nodes"]))
        captions = [n for n in result["nodes"] if n["kind"] in ("caption_page", "caption_word")]
        self.assertTrue(all(n["readOnly"] and n["assetId"] == "talk" for n in captions))
        with self.assertRaises(DocumentError) as caught:
            store.apply({"expected": result["revisions"], "operations": [
                {"op": "move_overlay", "id": captions[0]["id"], "at": 3},
            ]})
        self.assertEqual(caught.exception.code, "read_only_node")
        self.assertEqual(store.snapshot()["plan"], result["plan"])

    def test_primary_photo_kind_comes_from_compiler_when_raw_duration_is_omitted(self):
        self.temp.asset("still", kind="photo")
        path = self.temp.plan("photo-default", [{"asset": "still"}])
        store = DocumentStore(self.workspace, path)
        self.addCleanup(store.close)
        node = next(n for n in store.snapshot()["nodes"] if n["kind"] == "segment")
        self.assertEqual(node["assetKind"], "photo")
        self.assertNotIn("duration", node["raw"])

    def test_actual_renderer_exhibit_bleep_nat_and_music_projection(self):
        self.temp.asset("talk", words=words(("word", .5, .8), ("hello", 2, 2.5)))
        path = self.temp.plan("rich-lanes", [{
            "asset": "talk", "in": 0, "out": 8, "captions": "transcript",
            "nat": {"at": 3, "for": 1, "volume": .8},
            "bleep": {"windows": [{"at": .5, "for": .3}], "sound": "mute"},
            "punch": {"words": ["hello"], "for": .5},
            "blur": [{"keys": [{"at": 1, "x": .2, "y": .2, "w": .1, "h": .1},
                              {"at": 3, "x": .3, "y": .3, "w": .1, "h": .1}]}],
            "exhibits": [{"chart": {"type": "line", "points": [[0, 0], [1, 2]]}, "at": 4, "for": 1}],
        }], music=False)
        store = DocumentStore(self.workspace, path)
        self.addCleanup(store.close)
        result = store.snapshot()
        kinds = {n["kind"] for n in result["nodes"]}
        self.assertTrue({"exhibit", "bleep", "bleep_window", "nat", "music", "caption_page",
                         "caption_word", "punch_word", "generated_punch", "blur_keyframe"} <= kinds)
        exhibit = next(n for n in result["nodes"] if n["kind"] == "exhibit")
        nat = next(n for n in result["nodes"] if n["kind"] == "nat")
        self.assertEqual(exhibit["startFrame"], store._current_compiled["props"]["exhibits"][0]["startFrame"])
        self.assertEqual(nat["resolved"], store._current_compiled["sound"]["nat"][0])
        bleep = next(n for n in result["nodes"] if n["kind"] == "bleep_window")
        self.assertEqual(bleep["timingState"], "resolved")
        self.assertLess(bleep["startFrame"], 15)  # renderer padding, not raw at
        # The baked music lane covers renderer total, not summed source seconds.
        compiled = deepcopy(store._current_compiled)
        compiled["props"]["music"] = {"src": "music.wav", "trimBefore": 90, "volume": .5}
        raw = deepcopy(result["plan"])
        raw["music"] = {"file": "song.wav", "start": 3}
        music = next(n for n in editing_snapshot(raw, compiled, result["revisions"])["nodes"] if n["kind"] == "music")
        self.assertEqual((music["startFrame"], music["endFrame"]), (0, compiled["total"]))
        self.assertEqual(music["raw"], raw["music"])

    def test_nested_ids_survive_external_parent_edit_and_child_reorder(self):
        plan = {"segments": [{"asset": "a", "custom": {"keep": 1},
                              "bleep": {"windows": [{"at": 1, "for": .2, "unknown": "a"},
                                                     {"at": 2, "for": .2, "unknown": "b"}]},
                              "progress": {"at": .3}, "route": True,
                              "sphere": {"path": [{"at": 0, "yaw": 0}]}}],
                "hook": "plain string", "titleCard": {"text": "title"}, "music": {"file": "song.wav"}}
        ids = deepcopy(ensure_node_ids(plan))
        windows = {entry["fingerprint"]: node for node, entry in ids.items() if entry["kind"] == "bleep_window"}
        plan["segments"][0]["custom"]["keep"] = 2
        plan["segments"][0]["bleep"]["windows"].reverse()
        after = ensure_node_ids(plan)
        self.assertEqual(windows, {entry["fingerprint"]: node for node, entry in after.items()
                                  if entry["kind"] == "bleep_window"})
        self.assertTrue({"progress", "route", "camera_keyframe", "hook", "titleCard", "music"}
                        <= {entry["kind"] for entry in after.values()})

    def test_offline_reopen_uses_cached_committed_view_and_blocks_edits(self):
        state = {"offline": False, "renderer": True}
        self.workspace.media_status = lambda: {
            "library/media/asset-a.mp4": {"status": "offline" if state["offline"] else "online", "assetId": "asset-a"}}
        self.workspace.renderer_revision_available = lambda revision: state["renderer"]
        store, path = self.make_store(compiler=fake_compile)
        before = store.snapshot()
        store.close()
        state["offline"] = True
        # Uncommitted private edits are not materialized away or silently blessed.
        raw = json.loads(path.read_text())
        raw["uncommitted"] = "keep on disk"
        path.write_text(json.dumps(raw))
        reopened = DocumentStore(self.workspace, path, compiler=fake_compile)
        self.addCleanup(reopened.close)
        offline = reopened.snapshot()
        self.assertTrue(offline["viewOnly"])
        self.assertTrue(offline["pendingExternalEdits"])
        self.assertEqual(offline["timingState"], "cached_offline")
        self.assertEqual(offline["durationFrames"], before["durationFrames"])
        self.assertNotIn("uncommitted", offline["plan"])
        self.assertEqual(json.loads(path.read_text())["uncommitted"], "keep on disk")
        with self.assertRaises(DocumentError) as caught:
            reopened.apply({"expected": offline["revisions"], "operations": [
                {"op": "set_field", "path": "/note", "value": "must not commit"},
            ]})
        self.assertIn(caught.exception.code, ("media_offline", "external_changes"))
        with self.assertRaises(DocumentError) as caught:
            reopened._compile(offline["plan"], {})
        self.assertEqual(caught.exception.code, "media_offline")
        state["renderer"] = False
        with self.assertRaises(DocumentError) as caught:
            reopened.snapshot()
        self.assertEqual(caught.exception.code, "renderer_unavailable")
        state["renderer"] = True
        (reopened.history / "VIEW.json").unlink()
        uncached = reopened.snapshot()
        self.assertTrue(uncached["viewOnly"])
        self.assertEqual(uncached["timingState"], "unavailable")
        self.assertEqual(uncached["durationFrames"], 0)  # never invent timing from raw durations
        self.assertTrue(all(not node["editable"] for node in uncached["nodes"]))

    def test_scalar_settings_and_nested_selector_removal_keep_stable_ids(self):
        store, path = self.make_store(compiler=fake_compile)
        raw = json.loads(path.read_text())
        raw["hook"] = "plain hook"
        raw["segments"][0]["bleep"] = {"words": ["first", "second"], "custom": {"keep": 1}}
        path.write_text(json.dumps(raw))
        before = store.reload_external()
        hook = next(n for n in before["nodes"] if n["kind"] == "hook")
        selectors = [n for n in before["nodes"] if n["kind"] == "bleep_word"]
        result = store.apply({"expected": before["revisions"], "operations": [
            {"op": "set_field", "id": hook["id"], "path": "/value", "value": "edited hook"},
            {"op": "remove_overlay", "id": selectors[0]["id"]},
        ]})
        self.assertEqual(next(n for n in result["nodes"] if n["kind"] == "hook")["id"], hook["id"])
        self.assertEqual(next(n for n in result["nodes"] if n["kind"] == "bleep_word")["id"], selectors[1]["id"])
        self.assertEqual(result["plan"]["segments"][0]["bleep"],
                         {"words": ["second"], "custom": {"keep": 1}})

    def test_renderer_snapshot_uses_real_fade_frames_and_keeps_raw_plan(self):
        store, path = self.make_store()
        snapshot = store.snapshot()
        primary = [node for node in snapshot["nodes"] if node["kind"] == "segment"]
        self.assertEqual(primary[0]["startFrame"], 0)
        self.assertGreater(primary[1]["transitionInFrames"], 0)
        self.assertEqual(primary[1]["startFrame"], primary[0]["endFrame"] - primary[1]["transitionInFrames"])
        self.assertEqual(snapshot["durationFrames"], primary[-1]["endFrame"])
        self.assertIn("customUnknown", snapshot["plan"]["segments"][0])
        self.assertNotIn("_cutaways", snapshot["plan"]["segments"][0])
        self.assertEqual(json.loads(path.read_text())["segments"][0]["customUnknown"], {"keep": True})

    def test_ids_survive_reorder_and_undo_redo_are_new_revisions(self):
        store, _ = self.make_store()
        before = store.snapshot()
        ids = [node["id"] for node in before["nodes"] if node["kind"] == "segment"]
        result = store.apply({
            "expected": before["revisions"], "origin": "manual", "reason": "move second before first",
            "operations": [{"op": "reorder_segments", "ids": [ids[1]], "beforeId": ids[0]}],
        })
        self.assertEqual([n["id"] for n in result["nodes"] if n["kind"] == "segment"], [ids[1], ids[0]])
        moved_revision = result["revisions"]["document"]
        undone = store.undo(result["revisions"])
        self.assertNotEqual(undone["revisions"]["document"], moved_revision)
        self.assertEqual([n["id"] for n in undone["nodes"] if n["kind"] == "segment"], ids)
        redone = store.redo(undone["revisions"])
        self.assertNotEqual(redone["revisions"]["document"], moved_revision)
        self.assertEqual([n["id"] for n in redone["nodes"] if n["kind"] == "segment"], [ids[1], ids[0]])
        self.assertEqual(redone["change"]["origin"], "redo")

    def test_trim_ripples_global_events_and_preserves_unknown_fields(self):
        store, path = self.make_store()
        before = store.snapshot()
        segment = next(node for node in before["nodes"] if node["kind"] == "segment")
        result = store.apply({
            "expected": before["revisions"], "origin": "manual", "reason": "shorten first shot",
            "operations": [{"op": "trim_segment", "id": segment["id"], "sourceIn": 1, "sourceOut": 3}],
        })
        raw = json.loads(path.read_text())
        self.assertEqual(raw["segments"][0]["customUnknown"], {"keep": True})
        self.assertEqual(raw["segments"][0]["in"], 1)
        self.assertAlmostEqual(raw["text"][0]["at"], 5)
        self.assertAlmostEqual(raw["musicDrops"][0]["at"], 6)
        self.assertEqual(result["durationFrames"], 202)

    def test_split_rebases_segment_local_events_and_preserves_transcript_times(self):
        store, path = self.make_store()
        plan = json.loads(path.read_text())
        plan["segments"][0].update({
            "bleep": [{"at": 1.5, "for": 1.0}, {"at": 3.8, "for": 0.7}],
            "text": [{"text": "first", "at": 0.5, "for": 1.5},
                     {"text": "second", "at": 3.0, "for": 1.5}],
        })
        path.write_text(json.dumps(plan))
        # Reload treats this raw edit as its own revision.
        store.reload_external()
        before = store.snapshot()
        segment = next(node for node in before["nodes"] if node["kind"] == "segment")
        store.apply({
            "expected": before["revisions"], "origin": "manual", "reason": "split the take",
            "operations": [{"op": "split_segment", "id": segment["id"], "atFrame": 75}],
        })
        raw = json.loads(path.read_text())
        self.assertEqual(len(raw["segments"]), 3)
        self.assertEqual(raw["segments"][0]["bleep"], [{"at": 1.5, "for": 1.0}])
        self.assertEqual(raw["segments"][1]["bleep"], [{"at": 1.3, "for": 0.7}])
        transcript = json.loads((self.temp.project.transcripts_dir / "asset-a.json").read_text())
        self.assertEqual(transcript["words"][0], {"w": "hello", "s": 1.0, "e": 1.3, "p": 0.95})

    def test_correct_words_journal_changes_only_word_text_and_undo_restores(self):
        store, _ = self.make_store()
        before = store.snapshot()
        transcript_path = self.temp.project.transcripts_dir / "asset-a.json"
        original = json.loads(transcript_path.read_text())
        result = store.apply({
            "expected": before["revisions"], "origin": "ai", "reason": "fix transcript spelling",
            "operations": [{"op": "correct_words", "assetId": "asset-a",
                            "corrections": [{"index": 0, "word": "hullo"}]}],
        })
        changed = json.loads(transcript_path.read_text())
        self.assertEqual(changed["words"][0]["w"], "hullo")
        self.assertEqual(changed["words"][0]["s"], original["words"][0]["s"])
        self.assertEqual(changed["words"][0]["e"], original["words"][0]["e"])
        self.assertEqual(changed["segments"], original["segments"])
        undone = store.undo(result["revisions"])
        self.assertEqual(json.loads(transcript_path.read_text()), original)
        self.assertEqual(undone["change"]["origin"], "undo")

    def test_multi_file_head_recovery_finishes_materialization(self):
        store, path = self.make_store()
        before = store.snapshot()
        with patch.object(store, "_materialize", side_effect=OSError("simulated crash after HEAD")):
            with self.assertRaises(OSError):
                store.apply({
                    "expected": before["revisions"], "origin": "ai", "reason": "combined edit",
                    "operations": [
                        {"op": "set_field", "id": next(n["id"] for n in before["nodes"] if n["kind"] == "segment"),
                         "path": "/customUnknown/keep", "value": False},
                        {"op": "correct_words", "assetId": "asset-a", "corrections": [{"index": 0, "word": "fixed"}]},
                    ],
                })
        transcript_path = self.temp.project.transcripts_dir / "asset-a.json"
        # The new HEAD is authoritative, and startup completes the plan+transcript materialization.
        reopened = DocumentStore(self.workspace, path, compiler=fake_compile)
        self.assertFalse(json.loads(path.read_text())["segments"][0]["customUnknown"]["keep"])
        self.assertEqual(json.loads(transcript_path.read_text())["words"][0]["w"], "fixed")
        self.assertEqual(reopened.snapshot()["change"]["reason"], "combined edit")

    def test_stale_revision_and_manual_lease_block_ai_commit(self):
        store, _ = self.make_store(compiler=fake_compile)
        before = store.snapshot()
        lease = store.acquire_lease("timeline drag")
        node = next(n for n in before["nodes"] if n["kind"] == "segment")
        batch = {"expected": before["revisions"], "origin": "ai", "reason": "AI change",
                 "operations": [{"op": "set_field", "id": node["id"], "path": "/speed", "value": 2}]}
        with self.assertRaises(DocumentError) as caught:
            store.apply(batch)
        self.assertEqual(caught.exception.code, "lease_conflict")
        store.release_lease(lease["token"])
        newer = store.apply({
            "expected": before["revisions"], "origin": "manual", "reason": "manual wins",
            "operations": [{"op": "set_field", "id": node["id"], "path": "/speed", "value": 1.5}],
        })
        with self.assertRaises(DocumentError) as caught:
            store.apply(batch)
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(store.snapshot()["revisions"], newer["revisions"])

    def test_expected_revisions_rechecked_after_validation(self):
        calls = 0

        def changing_compiler(project, plan, overrides=None):
            nonlocal calls
            calls += 1
            result = fake_compile(project, plan, overrides)
            if calls == 3:
                record = project.assets_dir / "asset-a.json"
                asset = json.loads(record.read_text())
                asset["testRevisionBump"] = True
                record.write_text(json.dumps(asset))
            return result

        store, _ = self.make_store(compiler=changing_compiler)
        before = store.snapshot()
        node = next(n for n in before["nodes"] if n["kind"] == "segment")
        with self.assertRaises(DocumentError) as caught:
            store.apply({
                "expected": before["revisions"], "origin": "ai", "reason": "slow validation",
                "operations": [{"op": "set_field", "id": node["id"], "path": "/speed", "value": 2}],
            })
        self.assertEqual(caught.exception.code, "conflict")
        self.assertEqual(store.snapshot()["revisions"]["document"], before["revisions"]["document"])

    def test_cancel_pending_ai_invalidates_in_flight_commit_without_revision_change(self):
        started = threading.Event()
        release = threading.Event()
        calls = 0

        def slow_compiler(project, plan, overrides=None):
            nonlocal calls
            calls += 1
            if calls > 2:
                started.set()
                release.wait(5)
            return fake_compile(project, plan, overrides)

        store, _ = self.make_store(compiler=slow_compiler)
        before = store.snapshot()
        started.clear()
        node = next(n for n in before["nodes"] if n["kind"] == "segment")
        outcome = {}

        def edit():
            try:
                outcome["snapshot"] = store.apply({
                    "expected": before["revisions"], "origin": "ai", "reason": "slow Claude edit",
                    "operations": [{"op": "set_field", "id": node["id"], "path": "/speed", "value": 2}],
                })
            except DocumentError as exc:
                outcome["error"] = exc

        original_head = store.head_path.read_bytes()
        thread = threading.Thread(target=edit)
        thread.start()
        self.assertTrue(started.wait(2), "candidate compiler did not start")
        store.cancel_pending(origin="ai")
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(outcome["error"].code, "cancelled")
        self.assertEqual(store.head_path.read_bytes(), original_head)
        self.assertEqual(store.snapshot()["revisions"]["document"], before["revisions"]["document"])

    def test_external_private_plan_edit_becomes_revision(self):
        store, path = self.make_store(compiler=fake_compile)
        before = store.snapshot()
        raw = json.loads(path.read_text())
        raw["segments"][0]["customUnknown"]["keep"] = "external"
        path.write_text(json.dumps(raw))
        result = store.reload_external()
        self.assertNotEqual(result["revisions"]["document"], before["revisions"]["document"])
        self.assertEqual(result["change"]["origin"], "external")
        self.assertEqual(result["plan"]["segments"][0]["customUnknown"]["keep"], "external")

    def test_renderer_activation_checkpoints_code_plan_and_matching_preview(self):
        store, _ = self.make_store(compiler=fake_compile)
        before = store.snapshot()
        node = next(n for n in before["nodes"] if n["kind"] == "segment")
        candidate_revision = "candidate-renderer-v2"

        def candidate_compile(project, plan, overrides=None, renderer_revision=None):
            self.assertEqual(renderer_revision, candidate_revision)
            return fake_compile(project, plan, overrides)

        result = store.activate_renderer(
            candidate_revision, before["revisions"], "change renderer and effect",
            operations=[{"op": "set_field", "id": node["id"], "path": "/zoom", "value": 1.2}],
            preview={"ref": "preview/rev-2", "rendererRevision": candidate_revision,
                     "inputDocumentRevision": before["revisions"]["document"]},
            candidate_compiler=candidate_compile, source_ref="code/revisions/renderer-v2",
        )
        self.assertEqual(result["revisions"]["renderer"], candidate_revision)
        self.assertEqual(result["preview"]["state"], "ready")
        self.assertEqual(result["change"]["affectedIds"], [node["id"]])
        activated_manifest = store._manifest()
        self.assertEqual(activated_manifest["sourceRef"], "code/revisions/renderer-v2")
        self.assertEqual(activated_manifest["preview"]["ref"], "preview/rev-2")
        undone = store.undo(result["revisions"])
        self.assertNotEqual(undone["revisions"]["document"], result["revisions"]["document"])
        self.assertNotEqual(undone["revisions"]["renderer"], candidate_revision)
        redone = store.redo(undone["revisions"])
        self.assertEqual(redone["revisions"]["renderer"], candidate_revision)
        self.assertEqual(redone["preview"]["state"], "ready")

    def test_project_config_is_journaled_and_denied_permission_fields_stay_separate(self):
        store, _ = self.make_store()
        before = store.snapshot()
        result = store.apply({
            "expected": before["revisions"], "origin": "manual", "reason": "set project frame rate",
            "operations": [{"op": "set_field", "id": "project", "path": "/render/fps", "value": 60}],
        })
        config_path = self.temp.project.root / "config.json"
        self.assertEqual(json.loads(config_path.read_text())["render"]["fps"], 60)
        self.assertEqual(result["fps"], 60)
        self.assertEqual(result["projectConfig"]["render"]["fps"], 60)
        self.assertEqual(result["config"]["render"]["fps"], 60)
        undone = store.undo(result["revisions"])
        self.assertFalse(config_path.exists())
        self.assertEqual(undone["fps"], 30)
        with self.assertRaises(DocumentError) as caught:
            store.apply({
                "expected": undone["revisions"], "origin": "ai", "reason": "self-grant title permission",
                "operations": [{"op": "set_field", "id": "project",
                                "path": "/titleCards/allowed", "value": ["new-show"]}],
            })
        self.assertEqual(caught.exception.code, "config_permission")
        with self.assertRaises(DocumentError):
            store.apply({
                "expected": undone["revisions"], "origin": "ai", "reason": "write ignored plan render field",
                "operations": [{"op": "set_field", "id": "document", "path": "/render/fps", "value": 60}],
            })

    def test_candidate_snapshot_does_not_commit_and_activation_keeps_inserted_ids(self):
        store, path = self.make_store(compiler=fake_compile)
        before = store.snapshot()
        head, disk = store.head_path.read_bytes(), path.read_bytes()
        operations = [{"op": "insert_segment", "value": {"asset": "asset-a", "in": 2, "out": 3}}]
        candidate = store.candidate_snapshot(operations, before["revisions"])
        self.assertEqual(len(candidate["plan"]["segments"]), 3)
        self.assertEqual(store.head_path.read_bytes(), head)
        self.assertEqual(path.read_bytes(), disk)
        candidate_id = [n["id"] for n in candidate["nodes"] if n["kind"] == "segment"][-1]
        activated = store.activate_renderer("new-renderer", before["revisions"], "candidate",
                                            operations=operations, candidate_compiler=fake_compile)
        self.assertEqual([n["id"] for n in activated["nodes"] if n["kind"] == "segment"][-1], candidate_id)

    def test_export_usage_merges_old_revision_receipt_without_replacing_current_plan(self):
        store, path = self.make_store(compiler=fake_compile)
        before = store.snapshot()
        node = next(n for n in before["nodes"] if n["kind"] == "segment")
        latest = store.apply({"expected": before["revisions"], "origin": "manual", "operations": [
            {"op": "set_field", "id": node["id"], "path": "/customUnknown/keep", "value": "newer"},
        ]})
        asset_path = self.temp.project.assets_dir / "asset-a.json"
        asset = json.loads(asset_path.read_text())
        asset["used"] = [{"slug": "another-export"}]
        asset["newMetadata"] = {"keep": True}
        asset_path.write_text(json.dumps(asset))
        receipt = {"assets": {"asset-a": [{"slug": "old-export"}]}, "music": {}}
        recorded = store.commit_export_usage(receipt, before["revisions"])
        self.assertEqual(recorded["plan"], latest["plan"])
        value = json.loads(asset_path.read_text())
        self.assertEqual(value["used"], [{"slug": "another-export"}, {"slug": "old-export"}])
        self.assertEqual(value["newMetadata"], {"keep": True})
        self.assertNotEqual(recorded["revisions"]["library"], latest["revisions"]["library"])
        again = store.commit_export_usage(receipt, before["revisions"])
        self.assertEqual(again["revisions"], recorded["revisions"])
        undone = store.undo(again["revisions"])
        self.assertTrue(undone["plan"]["segments"][0]["customUnknown"]["keep"])
        self.assertEqual(json.loads(asset_path.read_text()), value)
        redone = store.redo(undone["revisions"])
        self.assertEqual(redone["plan"], latest["plan"])
        self.assertEqual(json.loads(asset_path.read_text()), value)

    def test_two_documents_reopen_preserves_newer_shared_transcript_and_config(self):
        first, path = self.make_store(compiler=fake_compile)
        initial = first.snapshot()
        first.apply({"expected": initial["revisions"], "origin": "manual", "operations": [
            {"op": "correct_words", "assetId": "asset-a", "corrections": [{"index": 0, "word": "first"}]},
            {"op": "set_field", "id": "project", "path": "/render/fps", "value": 24},
        ]})
        other_path = self.temp.plan("second-plan", [{"asset": "asset-a", "in": 0, "out": 2}])
        second = DocumentStore(self.workspace, other_path, compiler=fake_compile)
        second.apply({"expected": second.snapshot()["revisions"], "origin": "manual", "operations": [
            {"op": "correct_words", "assetId": "asset-a", "corrections": [{"index": 0, "word": "second"}]},
            {"op": "set_field", "id": "project", "path": "/render/fps", "value": 60},
        ]})
        reopened = DocumentStore(self.workspace, path, compiler=fake_compile)
        self.assertNotEqual(first.history, second.history)
        self.assertEqual(reopened.snapshot()["config"]["render"]["fps"], 60)
        transcript = json.loads((self.temp.project.transcripts_dir / "asset-a.json").read_text())
        self.assertEqual(transcript["words"][0]["w"], "second")
        current = reopened.snapshot()
        node = next(n for n in current["nodes"] if n["kind"] == "segment")
        reopened.apply({"expected": current["revisions"], "origin": "manual", "operations": [
            {"op": "set_field", "id": node["id"], "path": "/customUnknown/keep", "value": "later"},
        ]})
        self.assertEqual(json.loads((self.temp.project.transcripts_dir / "asset-a.json").read_text()), transcript)

    def test_real_seg_objects_are_supported_without_serializing_normalized_build(self):
        from reels.render import build
        def real_compile(project, plan, overrides):
            return build(project, plan)
        store, path = self.make_store(compiler=real_compile)
        snapshot = store.snapshot()
        node = next(n for n in snapshot["nodes"] if n["kind"] == "segment")
        result = store.apply({"expected": snapshot["revisions"], "origin": "manual", "operations": [
            {"op": "trim_segment", "id": node["id"], "sourceIn": 1, "sourceOut": 3},
        ]})
        self.assertEqual(result["durationFrames"], 202)
        self.assertNotIn("_cutaways", json.loads(path.read_text())["segments"][0])

    def test_duplicate_segment_ids_survive_reordering_and_following_operation(self):
        store, path = self.make_store(compiler=fake_compile)
        raw = json.loads(path.read_text())
        raw["segments"] = [deepcopy(raw["segments"][0]), deepcopy(raw["segments"][0])]
        path.write_text(json.dumps(raw))
        before = store.reload_external()
        ids = [n["id"] for n in before["nodes"] if n["kind"] == "segment"]
        result = store.apply({"expected": before["revisions"], "origin": "manual", "operations": [
            {"op": "reorder_segments", "ids": [ids[1]], "beforeId": ids[0]},
            {"op": "set_field", "id": ids[1], "path": "/customUnknown/keep", "value": "moved"},
        ]})
        self.assertEqual([n["id"] for n in result["nodes"] if n["kind"] == "segment"], list(reversed(ids)))
        self.assertEqual(result["plan"]["segments"][0]["customUnknown"]["keep"], "moved")

    def test_generated_cue_move_materializes_and_suppresses_automatic_cues(self):
        def generated_compile(project, plan, overrides):
            result = fake_compile(project, plan, overrides)
            if plan.get("autoSfx", True):
                result["sound"]["cues"] = [
                    {"sound": "click", "source": "auto:cut", "startFrame": 30, "endFrame": 33},
                    {"sound": "whoosh", "source": "auto:text", "startFrame": 90, "endFrame": 96},
                ]
            return result
        store, path = self.make_store(compiler=generated_compile)
        raw = json.loads(path.read_text())
        raw["autoSfx"] = True
        path.write_text(json.dumps(raw))
        before = store.reload_external()
        node = next(n for n in before["nodes"] if n.get("generated"))
        result = store.apply({"expected": before["revisions"], "origin": "manual", "operations": [
            {"op": "move_overlay", "id": node["id"], "at": 2},
        ]})
        self.assertFalse(result["plan"]["autoSfx"])
        self.assertEqual([cue["at"] for cue in result["plan"]["sfx"]], [2, 3])
        self.assertFalse(any(n.get("generated") for n in result["nodes"]))

    def test_manual_lease_can_interrupt_ai_validation_before_commit(self):
        started, release = threading.Event(), threading.Event()
        def slow_compile(project, plan, overrides):
            if plan.get("aiPending"):
                started.set()
                release.wait(5)
            return fake_compile(project, plan, overrides)
        store, _ = self.make_store(compiler=slow_compile)
        before = store.snapshot()
        result = {}
        def edit():
            try:
                store.apply({"expected": before["revisions"], "origin": "ai", "operations": [
                    {"op": "set_field", "id": "document", "path": "/aiPending", "value": True},
                ]})
            except DocumentError as exc:
                result["error"] = exc.code
        thread = threading.Thread(target=edit)
        thread.start()
        self.assertTrue(started.wait(2))
        lease = store.acquire_lease("manual drag")
        release.set()
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get("error"), "lease_conflict")
        self.assertEqual(store.snapshot()["revisions"], before["revisions"])
        store.release_lease(lease["token"])

    def test_multiple_undo_redo_steps_stop_at_initial_revision(self):
        store, _ = self.make_store(compiler=fake_compile)
        initial = store.snapshot()
        node = next(n for n in initial["nodes"] if n["kind"] == "segment")
        current = initial
        for value in ("one", "two"):
            current = store.apply({"expected": current["revisions"], "origin": "manual", "operations": [
                {"op": "set_field", "id": node["id"], "path": "/customUnknown/keep", "value": value},
            ]})
        current = store.undo(current["revisions"])
        self.assertEqual(current["plan"]["segments"][0]["customUnknown"]["keep"], "one")
        current = store.undo(current["revisions"])
        self.assertTrue(current["plan"]["segments"][0]["customUnknown"]["keep"])
        with self.assertRaises(DocumentError) as caught:
            store.undo(current["revisions"])
        self.assertEqual(caught.exception.code, "nothing_to_undo")
        current = store.redo(current["revisions"])
        self.assertEqual(current["plan"]["segments"][0]["customUnknown"]["keep"], "one")
        current = store.redo(current["revisions"])
        self.assertEqual(current["plan"]["segments"][0]["customUnknown"]["keep"], "two")

    def test_renderer_source_persists_and_missing_recorded_source_fails_closed(self):
        import tempfile
        import shutil
        from copy import copy
        from reels.editor.transactions import _worker_compile
        from reels.editor.workspace import init_workspace
        self.temp.asset("asset-a")
        self.temp.plan("renderer-fixture", [{"asset": "asset-a", "in": 0, "out": 1}])
        with tempfile.TemporaryDirectory() as directory:
            workspace = init_workspace(self.temp.root, Path(directory) / "workspace")
            store = DocumentStore(workspace, "plans/renderer-fixture.json")
            before = store.snapshot()
            source = workspace.root / "code/renderers" / before["revisions"]["renderer"] / "source"
            self.assertTrue((source / "scripts/reels/render.py").is_file())
            with patch.object(store, "_working_renderer_revision", return_value="unrelated-service-edit"):
                store._current_compiled = None
                self.assertEqual(store.snapshot()["revisions"], before["revisions"])
            candidate = workspace.root / "code/test-denied-writes"
            shutil.copytree(source, candidate, symlinks=True)
            renderer = candidate / "scripts/reels/render.py"
            renderer.chmod(0o600)
            asset = json.loads((self.temp.project.assets_dir / "asset-a.json").read_text())
            original = self.temp.project.abs(asset["media"])
            private_plan = workspace.data_root / "plans/renderer-fixture.json"
            original_bytes, plan_bytes = original.read_bytes(), private_plan.read_bytes()
            renderer.write_text(renderer.read_text() + "\n_trusted_build = build\n"
                "def build(*args, **kwargs):\n"
                f"    for target in {repr([str(original), str(private_plan)])}:\n"
                "        try:\n"
                "            Path(target).write_bytes(b'candidate-write')\n"
                "        except PermissionError:\n"
                "            pass\n"
                "        else:\n"
                "            raise RuntimeError('sandbox allowed protected write')\n"
                "    return _trusted_build(*args, **kwargs)\n")
            project = copy(workspace.project)
            project.code_root = candidate
            _worker_compile(project, before["plan"], {})
            self.assertEqual(original.read_bytes(), original_bytes)
            self.assertEqual(private_plan.read_bytes(), plan_bytes)
            source.rename(source.with_name("missing-source"))
            store._current_compiled = None
            with self.assertRaises(DocumentError) as caught:
                store.snapshot()
            self.assertEqual(caught.exception.code, "renderer_unavailable")


if __name__ == "__main__":
    unittest.main()
