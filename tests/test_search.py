from __future__ import annotations

import contextlib
import io
import json
import unittest

from helpers import TempProject, words
from reels import cli
from reels.library import load_asset, save_asset
from reels.search import parse_query, search, shelf_counts, transcript_lines
from reels.tags import merge_tags, validate_tags
from reels.util import ReelsError

ASSET = {"id": "clip", "kind": "video", "probe": {"duration": 60.0}}


def moment(mid: str, start: float, end: float, label: str, score: int = 3, **extra) -> dict:
    return {"id": mid, "in": start, "out": end, "label": label, "score": score, **extra}


def tags(*moments, **extra) -> dict:
    return {"type": "solo-ride", "shot": "pov", "quality": 3, "summary": "a ride", "moments": list(moments), **extra}


class Base(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(self)
        self.project = self.tp.project

    def run_cli(self, *argv: str) -> str:
        args = cli.build_parser().parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(args.fn(self.project, args), 0)
        return out.getvalue()

    def hits(self, *argv: str) -> list[dict]:
        return json.loads(self.run_cli("search", *argv, "--json", "--limit", "100"))

    def delete(self, aid: str) -> None:
        asset = load_asset(self.project, aid)
        asset["deleted"] = {"at": "2026-11-09T10:00:00+00:00", "reason": "space"}
        save_asset(self.project, asset)


class QueryTests(Base):
    def test_parse_splits_loose_words_and_quoted_phrases(self):
        self.assertEqual(parse_query('Red "big Hill" bike'), (["red", "bike"], [["big", "hill"]]))

    def test_phrase_flag_makes_one_phrase(self):
        self.assertEqual(parse_query("big hill", phrase=True), ([], [["big", "hill"]]))

    def test_punctuation_only_query_is_an_error(self):
        with self.assertRaises(ReelsError):
            parse_query("?!")

    def test_case_insensitive_and_all_words_required(self):
        self.tp.asset("a", tags=tags(moment("m-000001", 1, 3, "Red bike on the bridge")))
        self.assertEqual(len(self.hits("RED", "bridge")), 1)
        self.assertEqual(self.hits("red", "tunnel"), [])

    def test_words_must_be_in_the_same_field(self):
        self.tp.asset("a", tags=tags(moment("m-000001", 1, 3, "red bike"), summary="a bridge"))
        self.assertEqual(self.hits("red", "bridge"), [])

    def test_word_prefix_matches_but_mid_word_does_not(self):
        self.tp.asset("a", tags=tags(moment("m-000001", 1, 3, "climbing the slow road")))
        self.assertEqual(len(self.hits("climb")), 1)
        self.assertEqual(self.hits("low"), [])

    def test_quoted_phrase_needs_those_words_in_order(self):
        self.tp.asset("a", tags=tags(moment("m-000001", 1, 3, "big red hill")))
        self.assertEqual(self.hits('"big hill"'), [])
        self.assertEqual(len(self.hits('"big red"')), 1)
        self.assertEqual(len(self.hits("red hill", "--phrase")), 1)

    def test_covers_summary_hooks_and_notes(self):
        self.tp.asset("a", tags=tags(summary="the summary word", hooks=["a hookish line"], notes="some remark"))
        self.assertEqual([h["where"] for h in self.hits("summary")], ["summary"])
        self.assertEqual([h["where"] for h in self.hits("hookish")], ["hook"])
        self.assertEqual([h["where"] for h in self.hits("remark")], ["notes"])

    def test_skipped_assets_are_not_searched(self):
        asset = self.tp.asset("a", tags=tags(moment("m-000001", 1, 3, "red bike")))
        asset.update(status="skip", tags={"skip": True, "reason": "junk"})
        save_asset(self.project, asset)
        self.assertEqual(self.hits("red"), [])


class TranscriptTests(Base):
    def test_hit_gives_line_span_and_matched_word_times(self):
        said = words(("well", 2.0, 2.3), ("my", 2.4, 2.5), ("legs", 2.6, 2.9), ("hurt.", 3.0, 3.4), ("anyway", 9.0, 9.5))
        self.tp.asset("a", words=said, tags=tags(moment("m-000001", 1, 5, "selfie", score=4, role="voice")))
        hit = self.hits("legs")[0]
        self.assertEqual((hit["in"], hit["out"]), (2.0, 3.4))
        self.assertEqual((hit["matchIn"], hit["matchOut"]), (2.6, 2.9))
        self.assertEqual(hit["text"], "well my legs hurt.")
        self.assertEqual((hit["momentId"], hit["role"], hit["score"]), ("m-000001", "voice", 4))
        self.assertFalse(hit["untagged"])

    def test_text_output_shows_line_and_moment(self):
        said = words(("my", 2.4, 2.5), ("legs", 2.6, 2.9))
        self.tp.asset("a", words=said, tags=tags(moment("m-000001", 1, 5, "selfie")))
        out = self.run_cli("search", "legs")
        self.assertIn('"my legs"', out)
        self.assertIn("0:02.40-0:02.90", out)
        self.assertIn("in m-000001: selfie", out)

    def test_outside_every_moment_is_shown_as_untagged(self):
        self.tp.asset("a", words=words(("legs", 20.0, 20.4)), tags=tags(moment("m-000001", 1, 5, "selfie")))
        hit = self.hits("legs")[0]
        self.assertTrue(hit["untagged"])
        self.assertIsNone(hit["momentId"])
        self.assertIn("untagged", self.run_cli("search", "legs"))

    def test_corrected_words_win_over_stale_segment_text(self):
        asset = self.tp.asset("a", words=words(("Zwift", 1.0, 1.4)))
        path = self.project.abs(asset["transcript"])
        data = json.loads(path.read_text())
        data["segments"] = [{"start": 1.0, "end": 1.4, "text": "swift"}]
        path.write_text(json.dumps(data))
        self.assertEqual(len(self.hits("zwift")), 1)
        self.assertEqual(self.hits("swift"), [])

    def test_a_long_silence_splits_a_line(self):
        lines = transcript_lines({"segments": [{"start": 0, "end": 300, "text": "one two"}], "words": words(("one", 0.0, 0.5), ("two", 290.0, 290.5))})
        self.assertEqual([(x["in"], x["out"]) for x in lines], [(0.0, 0.5), (290.0, 290.5)])

    def test_used_when_the_line_or_its_moment_is_used(self):
        said = words(("legs", 4.0, 4.5))
        used = [{"plan": "p", "in": 1.0, "out": 2.0}]
        self.tp.asset("a", words=said, used=used, tags=tags(moment("m-000001", 1, 5, "selfie")))
        self.assertTrue(self.hits("legs")[0]["used"])

    def test_gibberish_is_flagged_and_ranked_last(self):
        self.project.config["transcription"]["keyterms"] = ["KOM", "Strava", "Zwift"]
        self.tp.asset("a", words=words(("KOM", 1.0, 1.1), ("Strava", 1.1, 1.2), ("Zwift", 1.2, 1.3), p=0.9))
        self.tp.asset("b", words=words(("Strava", 1.0, 1.1), p=0.05))
        self.tp.asset("c", tags=tags(summary="strava screen"))
        args = cli.build_parser().parse_args(["search", "strava"])
        got = [(h["asset"], bool(h.get("lowConfidence"))) for h in search(self.project, "strava", args)]
        self.assertEqual(got[0], ("c", False))
        self.assertEqual({a for a, low in got if low}, {"a", "b"})


class FilterTests(Base):
    def setUp(self):
        super().setUp()
        self.tp.asset("a", tags=tags(
            moment("m-000001", 1, 3, "red one", 5, role="voice", speaker="me", mood="funny", face=True, opener=True, line="joke", thread="story-a", beat="setup"),
            moment("m-000002", 4, 6, "red two", 3, role="voice", speaker="teammate", mood="calm", face=False, opener=False),
            moment("m-000003", 7, 9, "red three", 4, role="effort", speaker="none", mood="fast", face=False, opener=False, type="sprint"),
            moment("m-000004", 10, 12, "red old"),
        ), used=[{"plan": "p", "in": 7.0, "out": 8.0}], words=words(("red", 30.0, 30.4)))  # fmt: skip
        self.tp.asset("photo", kind="photo", tags={"type": "scenery", "shot": "photo", "quality": 4, "score": 4, "summary": "red wall"})

    def ids(self, *argv: str) -> set:
        return {h["momentId"] for h in self.hits("red", *argv)}

    def library(self, *argv: str) -> list[dict]:
        return json.loads(self.run_cli("library", "--json", *argv))

    def test_each_filter(self):
        self.assertEqual(self.ids("--role", "voice"), {"m-000001", "m-000002"})
        self.assertEqual(self.ids("--mood", "fast"), {"m-000003"})
        self.assertEqual(self.ids("--speaker", "teammate"), {"m-000002"})
        self.assertEqual(self.ids("--face"), {"m-000001"})
        self.assertEqual(self.ids("--opener"), {"m-000001"})
        self.assertEqual(self.ids("--line", "joke"), {"m-000001"})
        self.assertEqual(self.ids("--thread", "story-a"), {"m-000001"})

    def test_no_face_skips_moments_that_never_had_the_tag(self):
        self.assertEqual(self.ids("--no-face"), {"m-000002", "m-000003"})

    def test_filters_combine_with_each_other_and_existing_ones(self):
        self.assertEqual(self.ids("--role", "voice", "--no-face"), {"m-000002"})
        self.assertEqual(self.ids("--no-face", "--min-score", "4"), {"m-000003"})
        self.assertEqual(self.ids("--no-face", "--unused"), {"m-000002"})
        self.assertEqual(self.ids("--no-face", "--type", "sprint"), {"m-000003"})

    def test_shelf_filter_drops_hits_that_have_no_moment(self):
        wheres = {h["where"] for h in self.hits("red", "--role", "voice")}
        self.assertEqual(wheres, {"label"})

    def test_min_score_drops_untagged_lines_which_have_no_score(self):
        self.assertFalse(any(h.get("untagged") for h in self.hits("red", "--min-score", "1")))

    def test_invalid_choice_is_rejected(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.build_parser().parse_args(["library", "--role", "hero"])

    def test_face_flags_are_exclusive(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.build_parser().parse_args(["search", "x", "--face", "--no-face"])

    def test_library_filters_combine(self):
        self.assertEqual({r["momentId"] for r in self.library("--role", "voice", "--min-score", "4")}, {"m-000001"})
        self.assertEqual({r["momentId"] for r in self.library("--no-face", "--unused")}, {"m-000002"})
        self.assertEqual({r["momentId"] for r in self.library("--no-face", "--type", "sprint")}, {"m-000003"})

    def test_library_shelf_filter_leaves_out_photos(self):
        self.assertFalse(any(r["kind"] == "photo" for r in self.library("--role", "voice")))
        self.assertTrue(any(r["kind"] == "photo" for r in self.library()))

    def test_shelf_filters_are_refused_with_sounds(self):
        args = cli.build_parser().parse_args(["library", "--sounds", "--role", "voice"])
        with self.assertRaises(ReelsError):
            args.fn(self.project, args)

    def test_library_recent_is_newest_first_and_in_clip_order(self):
        asset = self.tp.asset("newer", tags=tags(moment("m-00000a", 5, 7, "b", 1), moment("m-00000b", 1, 3, "a", 1)))
        asset["created"] = "2026-11-08T08:00:00"
        save_asset(self.project, asset)
        rows = self.library("--recent")
        self.assertEqual([r["momentId"] for r in rows[:2]], ["m-00000b", "m-00000a"])
        self.assertEqual(self.library()[0]["score"], 5)


class RankingTests(Base):
    def order(self, *argv: str) -> list:
        return [(h["asset"], h["where"]) for h in self.hits("red", *argv)]

    def test_strong_matches_then_unused_then_score(self):
        self.tp.asset("used-high", tags=tags(moment("m-000001", 1, 3, "red", 5)), used=[{"plan": "p", "in": 1.0, "out": 2.0}])
        self.tp.asset("free-low", tags=tags(moment("m-000002", 1, 3, "red", 2)))
        self.tp.asset("free-high", tags=tags(moment("m-000003", 1, 3, "red", 4)))
        self.tp.asset("summary", tags={**tags(), "summary": "red", "quality": 5})
        self.assertEqual(self.order(), [("free-high", "label"), ("free-low", "label"), ("used-high", "label"), ("summary", "summary")])

    def test_transcript_ranks_with_labels(self):
        self.tp.asset("spoken", words=words(("red", 1.0, 1.2)), tags=tags(moment("m-000001", 0, 3, "x", 5)))
        self.tp.asset("note", tags=tags(notes="red"))
        self.assertEqual(self.order()[0], ("spoken", "transcript"))

    def test_limit_applies_after_ranking(self):
        self.tp.asset("low", tags=tags(moment("m-000001", 1, 3, "red", 1)))
        self.tp.asset("high", tags=tags(moment("m-000002", 1, 3, "red", 5)))
        got = json.loads(self.run_cli("search", "red", "--limit", "1", "--json"))
        self.assertEqual([h["asset"] for h in got], ["high"])


class DeletedTests(Base):
    def setUp(self):
        super().setUp()
        self.tp.asset("live", tags=tags(moment("m-000001", 1, 3, "red live", 4, role="voice", opener=True)))
        self.tp.asset("gone", tags=tags(moment("m-000002", 1, 3, "red gone", 5, role="voice", opener=True, thread="story", beat="setup"),
                                        sounds=[{"at": 1.0, "for": 1.0, "kind": "bell", "score": 4, "note": "ding"}]),
                      words=words(("red", 1.5, 1.8)))  # fmt: skip
        self.delete("gone")

    def test_search_hides_then_flags(self):
        self.assertEqual({h["asset"] for h in self.hits("red")}, {"live"})
        shown = [h for h in self.hits("red", "--include-deleted") if h["asset"] == "gone"]
        self.assertEqual(len(shown), 2)
        self.assertTrue(all(h["deleted"] for h in shown))
        self.assertIn("[deleted]", self.run_cli("search", "red", "--include-deleted"))

    def test_library_hides_then_flags_without_counting(self):
        self.assertNotIn("gone", self.run_cli("library"))
        out = self.run_cli("library", "--include-deleted")
        self.assertIn("gone#0", out)
        self.assertIn("[deleted]", out)
        self.assertIn("\n1 unused moments scoring 3+", out)

    def test_library_sounds_hide_deleted(self):
        self.assertEqual(json.loads(self.run_cli("library", "--sounds", "--json")), [])

    def test_shelves_never_count_deleted(self):
        out = self.run_cli("shelves")
        self.assertRegex(out, r"voice\s+1\s+1\s+1")
        self.assertNotIn("gone", out)
        out = self.run_cli("shelves", "--include-deleted")
        self.assertRegex(out, r"voice\s+1\s+1\s+1")
        self.assertIn("not counted above: 1", out)
        self.assertNotIn("gone", out)

    def test_stories_do_not_fill_a_beat_from_deleted_footage(self):
        self.assertIn("no threads", self.run_cli("stories"))
        self.assertIn("setup ✗", self.run_cli("stories", "--include-deleted"))
        self.assertIn("[deleted]", self.run_cli("stories", "story", "--include-deleted"))


class ShelvesTests(Base):
    def test_counts_low_stock_and_strong(self):
        self.tp.asset("a", tags=tags(
            moment("m-000001", 1, 3, "free strong", 5, role="voice", speaker="me", opener=True),
            moment("m-000002", 4, 6, "free weak", 2, role="voice", speaker="teammate"),
            moment("m-000003", 7, 9, "taken", 5, role="voice", speaker="me", opener=True),
        ), used=[{"plan": "p", "in": 7.0, "out": 8.0}])  # fmt: skip
        counts = shelf_counts([load_asset(self.project, "a")])
        self.assertEqual(counts["roles"]["voice"], {"total": 3, "unused": 2, "good": 1})
        self.assertEqual((counts["openers"], counts["voiceMe"]), (1, 1))
        self.assertEqual([m["id"] for _, m in counts["strong"]], ["m-000001"])
        out = self.run_cli("shelves")
        self.assertRegex(out, r"voice\s+3\s+2\s+1\s+LOW")
        self.assertIn("free strong", out)
        self.assertNotIn("taken", out)

    def test_moments_without_a_role_get_their_own_row(self):
        self.tp.asset("a", tags=tags(moment("m-000001", 1, 3, "old")))
        self.assertIn("(no role)", self.run_cli("shelves"))


class StoriesTests(Base):
    def setUp(self):
        super().setUp()
        self.tp.asset("a", tags=tags(
            moment("m-000001", 5, 7, "the effort", thread="ep-1", beat="effort"),
            moment("m-000002", 1, 3, "the plan", thread="ep-1", beat="setup"),
            moment("m-000003", 8, 10, "loose", thread="ep-1"),
            moment("m-000004", 11, 13, "other story", thread="ep-2", beat="result"),
        ))  # fmt: skip

    def test_list_shows_missing_beats(self):
        out = self.run_cli("stories")
        self.assertIn("ep-1   setup ✓  effort ✓  turn ✗  result ✗", out)
        self.assertIn("ep-2   setup ✗  effort ✗  turn ✗  result ✓", out)

    def test_detail_groups_in_beat_order_and_names_what_is_missing(self):
        out = self.run_cli("stories", "ep-1")
        self.assertLess(out.index("the plan"), out.index("the effort"))
        self.assertLess(out.index("the effort"), out.index("(no beat)"))
        self.assertIn("missing: turn, result", out)
        self.assertNotIn("other story", out)

    def test_used_moment_still_fills_its_beat(self):
        asset = load_asset(self.project, "a")
        asset["used"] = [{"plan": "p", "in": 1.0, "out": 3.0}]
        save_asset(self.project, asset)
        out = self.run_cli("stories", "ep-1")
        self.assertIn("setup ✓", out)
        self.assertIn("[used]", out)

    def test_unknown_thread_names_the_known_ones(self):
        args = cli.build_parser().parse_args(["stories", "nope"])
        with self.assertRaises(ReelsError) as caught:
            args.fn(self.project, args)
        self.assertIn("ep-1", str(caught.exception))


class LandmarkTests(Base):
    def problems(self, landmarks) -> str:
        with self.assertRaises(ReelsError) as caught:
            validate_tags(ASSET, tags(landmarks=landmarks))
        return str(caught.exception)

    def saved(self) -> dict:
        return {**ASSET, "tags": tags(moment("m-000001", 1, 3, "x"), landmarks=[{"at": 30.0, "label": "gas station"}])}

    def test_valid_landmarks_are_normalised_and_sorted(self):
        got = validate_tags(ASSET, tags(landmarks=[{"at": 40.123, "label": " bridge "}, {"at": 5, "label": "start"}]))
        self.assertEqual(got["landmarks"], [{"at": 5.0, "label": "start"}, {"at": 40.12, "label": "bridge"}])

    def test_older_records_gain_no_empty_list(self):
        self.assertNotIn("landmarks", validate_tags(ASSET, tags()))

    def test_at_must_be_inside_the_clip(self):
        self.assertIn("landmarks[0].at", self.problems([{"at": 61, "label": "x"}]))
        self.assertIn("landmarks[0].at", self.problems([{"at": -1, "label": "x"}]))
        self.assertIn("landmarks[0].at must be a number", self.problems([{"label": "x"}]))

    def test_label_must_be_non_empty_text(self):
        for bad in ("", "   ", None, 7):
            self.assertIn("landmarks[0].label", self.problems([{"at": 1, "label": bad}]))

    def test_must_be_a_list_of_objects(self):
        self.assertIn("landmarks must be a list", self.problems({"at": 1}))
        self.assertIn("landmarks[0] must be an object", self.problems(["x"]))

    def test_all_problems_are_listed_at_once(self):
        text = self.problems([{"at": 99, "label": "x"}, {"at": 1, "label": ""}])
        self.assertIn("landmarks[0].at", text)
        self.assertIn("landmarks[1].label", text)

    def test_only_on_video(self):
        photo = {"id": "p", "kind": "photo", "probe": {}}
        with self.assertRaises(ReelsError) as caught:
            validate_tags(photo, {"type": "scenery", "summary": "s", "landmarks": [{"at": 1, "label": "x"}]})
        self.assertIn("only apply to video", str(caught.exception))

    def test_merge_replaces_landmarks_whole(self):
        merged = merge_tags(self.saved(), {"landmarks": [{"at": 12, "label": "bridge"}]})
        self.assertEqual(merged["landmarks"], [{"at": 12.0, "label": "bridge"}])

    def test_merge_of_something_else_leaves_them_alone(self):
        merged = merge_tags(self.saved(), {"moments": [{"id": "m-000001", "mood": "calm"}]})
        self.assertEqual(merged["landmarks"], [{"at": 30.0, "label": "gas station"}])

    def test_merge_can_clear_them(self):
        self.assertNotIn("landmarks", merge_tags(self.saved(), {"landmarks": None}))
        self.assertNotIn("landmarks", merge_tags(self.saved(), {"landmarks": []}))

    def test_merge_validates_them(self):
        with self.assertRaises(ReelsError):
            merge_tags(self.saved(), {"landmarks": [{"at": 999, "label": "x"}]})

    def test_merge_does_not_add_them_to_a_clip_without_any(self):
        asset = {**ASSET, "tags": tags(moment("m-000001", 1, 3, "x"))}
        self.assertNotIn("landmarks", merge_tags(asset, {"summary": "new"}))

    def test_search_marks_a_landmark_in_an_untagged_stretch(self):
        self.tp.asset("a", duration=60.0, tags=tags(moment("m-000001", 1, 3, "x"), landmarks=[{"at": 30.0, "label": "gas station stop"}]))
        hit = self.hits("gas")[0]
        self.assertEqual((hit["where"], hit["landmark"], hit["untagged"], hit["in"]), ("landmark", True, True, 30.0))
        out = self.run_cli("search", "gas")
        self.assertIn("[landmark]", out)
        self.assertIn("@0:30.00", out)

    def test_search_names_the_moment_around_a_landmark(self):
        self.tp.asset("a", duration=60.0, tags=tags(moment("m-000001", 28, 32, "x", 4, role="crew"), landmarks=[{"at": 30.0, "label": "gas station"}]))
        hit = self.hits("gas")[0]
        self.assertEqual((hit["momentId"], hit["role"], hit["score"]), ("m-000001", "crew", 4))
        self.assertTrue(hit["landmark"])


if __name__ == "__main__":
    unittest.main()
