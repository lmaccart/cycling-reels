from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from helpers import TempProject
from reels import cli
from reels.library import load_asset
from reels.tags import LINES, ROLES, backfill_moment_ids, merge_tags, validate_tags
from reels.util import ReelsError, write_json

ASSET = {"id": "clip", "kind": "video", "probe": {"duration": 60.0}}


def base(*moments, **extra) -> dict:
    return {"type": "solo-ride", "shot": "pov", "quality": 4, "summary": "a ride", "moments": list(moments), **extra}


def moment(start: float = 1.0, end: float = 5.0, **extra) -> dict:
    return {"in": start, "out": end, "label": "something happens", "score": 3, **extra}


class ShelfValidationTests(unittest.TestCase):
    def problems(self, **fields) -> str:
        with self.assertRaises(ReelsError) as caught:
            validate_tags(ASSET, base(moment(**fields)))
        return str(caught.exception)

    def test_shelves_are_optional_for_old_data(self):
        saved = validate_tags(ASSET, base(moment()))["moments"][0]
        for key in ("role", "speaker", "face", "mood", "opener", "line", "thread", "beat"):
            self.assertNotIn(key, saved)

    def test_valid_shelves_are_kept(self):
        fields = {"role": "voice", "speaker": "me", "face": True, "mood": "honest", "opener": False,
                  "line": "confession", "thread": "road-to-5-ep1", "beat": "turn"}  # fmt: skip
        saved = validate_tags(ASSET, base(moment(**fields)))["moments"][0]
        self.assertEqual({k: saved[k] for k in fields}, fields)

    def test_every_listed_value_is_accepted(self):
        for role in ROLES:
            validate_tags(ASSET, base(moment(role=role)))
        for line in LINES:
            validate_tags(ASSET, base(moment(role="voice", line=line)))

    def test_all_problems_are_listed_at_once(self):
        text = self.problems(role="hero", speaker="dog", mood="meh", face="yes", opener=1)
        for field in ("role", "speaker", "mood", "face", "opener"):
            self.assertIn(f"moments[0].{field}", text)

    def test_line_only_with_voice(self):
        self.assertIn("line only applies when role is voice", self.problems(role="crew", line="joke"))
        self.assertIn("line only applies when role is voice", self.problems(line="joke"))

    def test_beat_needs_thread(self):
        self.assertIn("beat needs a thread", self.problems(beat="turn"))

    def test_bad_beat_and_thread_are_rejected(self):
        self.assertIn("beat must be one of", self.problems(thread="a-story", beat="climax"))
        for thread in ("Road To 5", "road_to_5", "-road", "road--5", ""):
            self.assertIn("kebab-case", self.problems(thread=thread))


class MomentIdTests(unittest.TestCase):
    def retag(self, previous: dict, *moments) -> list[dict]:
        return validate_tags({**ASSET, "tags": previous}, base(*moments))["moments"]

    def test_new_moments_get_unique_ids(self):
        moments = validate_tags(ASSET, base(*[moment(i * 2.0, i * 2.0 + 1) for i in range(20)]))["moments"]
        ids = [m["id"] for m in moments]
        self.assertEqual(len(set(ids)), 20)
        for i in ids:
            self.assertRegex(i, r"^m-[0-9a-f]{6}$")

    def test_full_retag_keeps_ids_of_the_same_spans(self):
        first = validate_tags(ASSET, base(moment(1, 5), moment(10, 14), moment(20, 24)))
        ids = [m["id"] for m in first["moments"]]
        again = self.retag(first, moment(10, 14, label="new label"), moment(20.2, 23.9), moment(30, 34))
        self.assertEqual(again[0]["id"], ids[1])  # exact
        self.assertEqual(again[1]["id"], ids[2])  # tweaked trim
        self.assertNotIn(again[2]["id"], ids)  # new span, never a dropped moment's id

    def test_exact_span_beats_a_near_one(self):
        first = validate_tags(ASSET, base(moment(10, 14)))
        mid = first["moments"][0]["id"]
        again = self.retag(first, moment(10.2, 14), moment(10, 14))
        self.assertEqual([m["id"] == mid for m in again], [True, False])  # sorted by start: the exact span is first

    def test_different_span_gets_a_new_id(self):
        first = validate_tags(ASSET, base(moment(10, 14)))
        again = self.retag(first, moment(12, 16))
        self.assertNotEqual(again[0]["id"], first["moments"][0]["id"])

    def test_given_id_keeps_a_moment_through_a_big_retrim(self):
        first = validate_tags(ASSET, base(moment(10, 14)))
        mid = first["moments"][0]["id"]
        self.assertEqual(self.retag(first, moment(20, 28, id=mid))[0]["id"], mid)

    def test_bad_ids_are_rejected(self):
        first = validate_tags(ASSET, base(moment(10, 14)))
        mid = first["moments"][0]["id"]
        cases = [(base(moment(1, 2, id="m-000000")), "not a moment on this clip"),
                 (base(moment(1, 2, id="nope")), "id must look like"),
                 (base(moment(1, 2, id=mid), moment(3, 4, id=mid)), "same id is on two moments")]  # fmt: skip
        for raw, expected in cases:
            with self.assertRaises(ReelsError) as caught:
                validate_tags({**ASSET, "tags": first}, raw)
            self.assertIn(expected, str(caught.exception))

    def test_backfill_only_fills_gaps(self):
        tags = base(moment(1, 2), moment(3, 4, id="m-aaaaaa"))
        self.assertEqual(backfill_moment_ids(tags), 1)
        self.assertEqual(tags["moments"][1]["id"], "m-aaaaaa")
        self.assertRegex(tags["moments"][0]["id"], r"^m-[0-9a-f]{6}$")
        self.assertEqual(backfill_moment_ids(tags), 0)


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.sounds = [{"at": 2.0, "for": 1.0, "kind": "freehub", "score": 4, "note": "freehub, no wind"}]
        self.tags = validate_tags(ASSET, base(moment(1, 5, focusX=0.3), moment(10, 14), hooks=["wait for it"], sounds=self.sounds))
        self.ids = [m["id"] for m in self.tags["moments"]]
        self.asset = {**ASSET, "tags": self.tags}

    def test_partial_moment_update_changes_only_supplied_fields(self):
        merged = merge_tags(self.asset, {"moments": [{"id": self.ids[0], "role": "effort", "face": False}]})
        expected = {**self.tags["moments"][0], "role": "effort", "face": False}
        self.assertEqual(merged["moments"], [expected, self.tags["moments"][1]])

    def test_merge_does_not_touch_sounds_angles_or_hooks(self):
        sphere = {"id": "c", "kind": "video", "probe": {"duration": 60.0, "sphere": True}}
        raw = base(moment(1, 5, view="road"))
        raw.update(sounds=self.sounds, angles={"road": {"yaw": 0, "pitch": -8}}, hooks=["hook"], notes="keep me")
        tags = validate_tags(sphere, raw)
        merged = merge_tags({**sphere, "tags": tags}, {"moments": [{"id": tags["moments"][0]["id"], "role": "texture"}]})
        for key in ("sounds", "angles", "hooks", "notes", "summary"):
            self.assertEqual(merged[key], tags[key], key)

    def test_merge_keeps_what_validation_would_have_normalised(self):
        long_label, long_notes = "x" * 200, "y" * 400
        old = self.tags | {"notes": long_notes}
        old.pop("sounds")
        old["moments"] = [{**old["moments"][0], "label": long_label}, old["moments"][1]]
        merged = merge_tags({**ASSET, "tags": old}, {"moments": [{"id": old["moments"][0]["id"], "mood": "calm"}]})
        self.assertEqual(merged["notes"], long_notes)
        self.assertEqual(merged["moments"][0]["label"], long_label)
        self.assertNotIn("sounds", merged)

    def test_supplied_top_level_fields_replace(self):
        merged = merge_tags(self.asset, {"summary": "new summary", "sounds": []})
        self.assertEqual((merged["summary"], merged["sounds"]), ("new summary", []))
        self.assertEqual(merged["moments"], self.tags["moments"])

    def test_null_removes_an_optional_field(self):
        tagged = merge_tags(self.asset, {"moments": [{"id": self.ids[0], "role": "voice", "line": "joke"}]})
        with self.assertRaises(ReelsError):
            merge_tags({**ASSET, "tags": tagged}, {"moments": [{"id": self.ids[0], "role": "crew"}]})
        cleared = merge_tags({**ASSET, "tags": tagged}, {"moments": [{"id": self.ids[0], "role": "crew", "line": None}]})
        self.assertEqual(cleared["moments"][0]["role"], "crew")
        self.assertNotIn("line", cleared["moments"][0])

    def test_merged_result_is_validated_against_existing_fields(self):
        tagged = merge_tags(self.asset, {"moments": [{"id": self.ids[0], "role": "effort"}]})
        with self.assertRaises(ReelsError) as caught:
            merge_tags({**ASSET, "tags": tagged}, {"moments": [{"id": self.ids[0], "line": "joke"}]})
        self.assertIn("line only applies", str(caught.exception))

    def test_all_problems_are_listed_at_once(self):
        patch = {"moment": [], "skip": True, "moments": [{"role": "effort"}, {"id": "m-000000"}, {"id": self.ids[0], "rol": "x"}, {"id": self.ids[1], "mood": "meh"}]}
        with self.assertRaises(ReelsError) as caught:
            merge_tags(self.asset, patch)
        text = str(caught.exception)
        for expected in ("'moment'", "use ./reels skip", "needs an id", "no moment 'm-000000'", "unknown field 'rol'", "mood must be one of"):
            self.assertIn(expected, text)

    def test_needs_existing_tags(self):
        for tags in (None, {"skip": True, "reason": "junk"}):
            with self.assertRaises(ReelsError):
                merge_tags({**ASSET, "tags": tags}, {"summary": "x"})

    def test_moment_order_only_changes_when_a_start_moves(self):
        patch = {"moments": [{"id": self.ids[0], "in": 20.0, "out": 24.0}]}
        self.assertEqual([m["id"] for m in merge_tags(self.asset, patch)["moments"]], [self.ids[1], self.ids[0]])


class AddMomentsTests(unittest.TestCase):
    def setUp(self):
        self.tags = validate_tags(ASSET, base(moment(10, 14), moment(20, 24), hooks=["hook"], landmarks=[{"at": 12, "label": "bridge"}]))
        self.asset = {**ASSET, "tags": self.tags}

    def full(self, start: float = 30, end: float = 34, **extra) -> dict:
        return moment(start, end, role="proof", speaker="none", face=False, mood="calm", opener=False, focusX=0.4, **extra)

    def refused(self, patch: dict, asset: dict | None = None) -> str:
        with self.assertRaises(ReelsError) as caught:
            merge_tags(asset or self.asset, patch)
        return str(caught.exception)

    def test_adds_a_validated_moment_with_a_new_id_in_order(self):
        merged = merge_tags(self.asset, {"addMoments": [self.full(15, 18)]})
        self.assertEqual([m["in"] for m in merged["moments"]], [10, 15, 20])
        new = merged["moments"][1]
        self.assertRegex(new["id"], r"^m-[0-9a-f]{6}$")
        self.assertNotIn(new["id"], [m["id"] for m in self.tags["moments"]])
        self.assertEqual((new["role"], new["focusX"], new["speech"]), ("proof", 0.4, False))

    def test_nothing_else_changes(self):
        merged = merge_tags(self.asset, {"addMoments": [self.full()]})
        for key in ("hooks", "landmarks", "summary", "sounds"):
            self.assertEqual(merged[key], self.tags[key], key)
        self.assertEqual(merged["moments"][:2], self.tags["moments"])

    def test_keeps_records_validation_would_have_normalised(self):
        old = {**self.tags, "notes": "y" * 400, "moments": [{**self.tags["moments"][0], "label": "x" * 200}]}
        merged = merge_tags({**ASSET, "tags": old}, {"addMoments": [self.full()]})
        self.assertEqual(merged["notes"], "y" * 400)
        self.assertEqual(merged["moments"][0]["label"], "x" * 200)

    def test_overlap_with_an_existing_moment_is_refused(self):
        mid = self.tags["moments"][0]["id"]
        self.assertIn(f"overlaps {mid}", self.refused({"addMoments": [self.full(13, 16)]}))

    def test_overlap_between_new_moments_is_refused(self):
        self.assertIn("overlaps", self.refused({"addMoments": [self.full(30, 34), self.full(33, 36)]}))

    def test_touching_moments_are_fine(self):
        merged = merge_tags(self.asset, {"addMoments": [self.full(14, 20)]})
        self.assertEqual(len(merged["moments"]), 3)

    def test_overlap_is_checked_against_a_moved_moment(self):
        mid = self.tags["moments"][0]["id"]
        patch = {"moments": [{"id": mid, "in": 40, "out": 44}], "addMoments": [self.full(10, 14)]}
        self.assertEqual(len(merge_tags(self.asset, patch)["moments"]), 3)
        self.assertIn("overlaps", self.refused({"moments": [{"id": mid, "in": 40, "out": 44}], "addMoments": [self.full(41, 43)]}))

    def test_same_validation_as_a_full_tag(self):
        text = self.refused({"addMoments": [{**self.full(), "role": "hero", "score": 9}, {"in": 40, "out": 41}, "nope"]})
        for expected in ("addMoments[0].role", "addMoments[0].score", "addMoments[1].label is required", "addMoments[2] must be an object"):
            self.assertIn(expected, text)

    def test_ids_and_unknown_fields_are_refused(self):
        text = self.refused({"addMoments": [{**self.full(), "id": "m-abcdef", "rol": "x"}]})
        self.assertIn("leave it out", text)
        self.assertIn("unknown field 'rol'", text)

    def test_must_be_a_list_and_a_video(self):
        self.assertIn("must be a list", self.refused({"addMoments": {"in": 1}}))
        photo = {"id": "p", "kind": "photo", "probe": {}, "tags": {"type": "scenery", "shot": "photo", "quality": 3, "summary": "s", "score": 3}}
        self.assertIn("only apply to video", self.refused({"addMoments": [self.full()]}, photo))

    def test_landmarks_merge_alongside(self):
        merged = merge_tags(self.asset, {"addMoments": [self.full()], "landmarks": [{"at": 30, "label": "summit"}]})
        self.assertEqual(merged["landmarks"], [{"at": 30.0, "label": "summit"}])
        self.assertEqual(len(merged["moments"]), 3)

    def test_empty_list_changes_nothing(self):
        self.assertEqual(merge_tags(self.asset, {"addMoments": []}), self.tags)


class TagCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.project = self.temp.project
        tags = validate_tags({"kind": "video", "probe": {"duration": 10.0}}, base(moment(1, 5), moment(6, 9), sounds=[{"at": 2.0, "for": 1.0, "kind": "freehub", "score": 4, "note": "ok"}]))
        self.temp.asset("clip", tags=tags)
        self.temp.asset("other", tags=tags)

    def args(self, **kw) -> SimpleNamespace:
        return SimpleNamespace(**{"asset": "clip", "json": None, "file": None, "merge": False, **kw})

    def set_tagged_at(self, value: str):
        from reels.library import save_asset

        asset = load_asset(self.project, "clip")
        asset["taggedAt"] = value
        save_asset(self.project, asset)

    def test_merge_keeps_status_and_tagged_at(self):
        self.set_tagged_at("2026-01-01T00:00:00")
        mid = load_asset(self.project, "clip")["tags"]["moments"][0]["id"]
        cli.cmd_tag(self.project, self.args(merge=True, json=json.dumps({"moments": [{"id": mid, "role": "effort"}]})))
        saved = load_asset(self.project, "clip")
        self.assertEqual(saved["tags"]["moments"][0]["role"], "effort")
        self.assertEqual((saved["status"], saved["taggedAt"]), ("tagged", "2026-01-01T00:00:00"))

    def test_full_tag_keeps_ids_through_the_command(self):
        before = [m["id"] for m in load_asset(self.project, "clip")["tags"]["moments"]]
        raw = base(moment(1, 5, label="again"), moment(6, 9))
        cli.cmd_tag(self.project, self.args(json=json.dumps(raw)))
        self.assertEqual([m["id"] for m in load_asset(self.project, "clip")["tags"]["moments"]], before)

    def test_batch_saves_nothing_when_any_asset_is_invalid(self):
        mid = load_asset(self.project, "clip")["tags"]["moments"][0]["id"]
        path = self.temp.root / "batch.json"
        write_json(path, {"clip": {"moments": [{"id": mid, "role": "effort"}]}, "other": {"quality": 9}, "ghost": {"summary": "x"}})
        with self.assertRaises(ReelsError) as caught:
            cli.cmd_tag_batch(self.project, SimpleNamespace(file=str(path)))
        self.assertIn("quality", str(caught.exception))
        self.assertIn("ghost", str(caught.exception))
        self.assertNotIn("role", load_asset(self.project, "clip")["tags"]["moments"][0])

    def test_merge_adds_moments_through_the_command(self):
        self.set_tagged_at("2026-01-01T00:00:00")
        new = {"in": 5.0, "out": 5.9, "label": "new", "score": 3, "role": "texture", "speaker": "none", "face": False, "mood": "calm", "opener": False}
        cli.cmd_tag(self.project, self.args(merge=True, json=json.dumps({"addMoments": [new]})))
        saved = load_asset(self.project, "clip")
        self.assertEqual([m["in"] for m in saved["tags"]["moments"]], [1, 5.0, 6])
        self.assertEqual((saved["status"], saved["taggedAt"]), ("tagged", "2026-01-01T00:00:00"))

    def test_batch_adds_moments_and_saves_nothing_when_one_overlaps(self):
        good = {"in": 5.0, "out": 5.9, "label": "new", "score": 3}
        bad = {"in": 2, "out": 4, "label": "overlaps", "score": 3}
        path = self.temp.root / "batch.json"
        write_json(path, {"clip": {"addMoments": [good]}, "other": {"addMoments": [bad]}})
        with self.assertRaises(ReelsError) as caught:
            cli.cmd_tag_batch(self.project, SimpleNamespace(file=str(path)))
        self.assertIn("overlaps", str(caught.exception))
        self.assertEqual(len(load_asset(self.project, "clip")["tags"]["moments"]), 2)
        write_json(path, {"clip": {"addMoments": [good]}, "other": {"addMoments": [good]}})
        cli.cmd_tag_batch(self.project, SimpleNamespace(file=str(path)))
        self.assertEqual(len(load_asset(self.project, "other")["tags"]["moments"]), 3)

    def test_batch_merges_every_asset(self):
        patch = {"clip": {"summary": "one"}, "other": {"summary": "two"}}
        path = self.temp.root / "batch.json"
        write_json(path, patch)
        cli.cmd_tag_batch(self.project, SimpleNamespace(file=str(path)))
        self.assertEqual(load_asset(self.project, "clip")["tags"]["summary"], "one")
        self.assertEqual(load_asset(self.project, "other")["tags"]["summary"], "two")
        self.assertEqual(len(load_asset(self.project, "other")["tags"]["sounds"]), 1)


if __name__ == "__main__":
    unittest.main()
