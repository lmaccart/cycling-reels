from __future__ import annotations

import contextlib
import io
import unittest
from types import SimpleNamespace

from helpers import TempProject, words
from reels import cli
from reels.lineage import from_built
from reels.render import Seg, build, load_plan
from reels.retention import batch_warnings, caption_cuts, fades, opener_warnings, opening, spine_warnings, still_stretches
from reels.util import ReelsError

FPS = 30


def seg(i: int, start: int, frames: int, *, kind: str = "video", trans: int = 0, **spec) -> Seg:
    return Seg(index=i, kind=kind, asset={"id": f"a{i}"}, spec={"in": 0.0, "out": frames / FPS, "speed": 1.0, **spec},
               frames=frames, trans=trans, start=start)  # fmt: skip


class OpeningTests(unittest.TestCase):
    def test_photo_first_is_flagged_but_a_360_camera_move_is_not(self):
        self.assertIn("opens on a photo", opening([seg(0, 0, 60, kind="photo"), seg(1, 60, 60)], FPS)[0])
        moving = seg(0, 0, 60, kind="photo", sphere={"path": [{"at": 0}, {"at": 2}]})
        self.assertEqual(opening([moving, seg(1, 60, 60)], FPS), [])

    def test_only_an_early_crossfade_is_flagged(self):
        early = [seg(0, 0, 40), seg(1, 32, 60, trans=8)]
        self.assertIn("crossfade at 1.1s", opening(early, FPS)[0])
        late = [seg(0, 0, 90), seg(1, 82, 60, trans=8)]
        self.assertEqual(opening(late, FPS), [])

    def test_mostly_fades_reads_as_a_slideshow(self):
        segs = [seg(0, 0, 60)] + [seg(i, 60 * i, 60, trans=8) for i in range(1, 5)] + [seg(5, 300, 60)]
        self.assertIn("4 of 5 cuts are crossfades", fades(segs)[0])
        self.assertEqual(fades([seg(0, 0, 60), seg(1, 52, 60, trans=8), seg(2, 112, 60)]), [])  # one deliberate fade


class StillStretchTests(unittest.TestCase):
    def test_long_unchanging_shot_is_flagged(self):
        segs = [seg(0, 0, 60), seg(1, 60, 240)]  # 2 s, then 8 s of one shot with nothing on it
        got = still_stretches(segs, [], [], [], None, FPS, 300)
        self.assertEqual(len(got), 1)
        self.assertIn("8.0s (2.0–10.0s)", got[0])

    def test_text_captions_punches_cutaways_and_nat_count_as_change(self):
        segs = [seg(0, 0, 60), seg(1, 60, 240)]
        beat = [{"startFrame": 150, "endFrame": 200}]
        self.assertEqual(still_stretches(segs, [], beat, [], None, FPS, 300), [])
        self.assertEqual(still_stretches(segs, [{"startFrame": 130}, {"startFrame": 200}], [], [], None, FPS, 300), [])
        self.assertEqual(still_stretches(segs, [], [], [{"startFrame": 180}], None, FPS, 300), [])
        punched = [seg(0, 0, 60), seg(1, 60, 240, _punches=[{"start": 150, "end": 200}])]
        self.assertEqual(still_stretches(punched, [], [], [], None, FPS, 300), [])
        cut = {"start": 60, "seg": seg(9, 0, 60)}
        covered = [seg(0, 0, 60), seg(1, 60, 240, _cutaways=[cut])]
        self.assertEqual(still_stretches(covered, [], [], [], None, FPS, 300), [])


class CaptionCutTests(unittest.TestCase):
    WORDS = words(("on", 2.70, 3.10), ("my", 3.20, 3.40), ("way", 3.40, 3.80), ("left", 5.70, 6.30))

    def cuts(self, a: float, b: float, w=None) -> list[str]:
        return caption_cuts(seg(0, 0, 90, **{"in": a, "out": b}), w or self.WORDS)

    def test_in_point_inside_a_word_is_a_stray_caption(self):
        got = self.cuts(3.0, 5.0)
        self.assertEqual(len(got), 1)
        self.assertIn("in-point 3.00s cuts into 'on'", got[0])
        self.assertIn("start at 3.10s", got[0])

    def test_out_point_inside_a_word_clips_it(self):
        got = self.cuts(3.15, 6.0)
        self.assertEqual(len(got), 1)
        self.assertIn("clips 'left'", got[0])

    def test_clean_boundaries_and_tiny_overlaps_pass(self):
        self.assertEqual(self.cuts(3.12, 5.0), [])
        self.assertEqual(self.cuts(2.68, 3.85), [])  # starts 20 ms before 'on': within Whisper's timing noise

    def test_low_confidence_words_are_listed(self):
        shaky = words(("wind", 1.0, 1.3), ("noise", 1.3, 1.6), p=0.2) + words(("hi", 2.0, 2.2))
        got = self.cuts(0.5, 3.0, shaky)
        self.assertEqual(len(got), 1)
        self.assertIn("'wind' (0.20), 'noise' (0.20)", got[0])


class BuildWarnsTests(unittest.TestCase):
    """The checks run inside build(), i.e. in ./reels check and ./reels render."""

    def setUp(self):
        self.t = TempProject(self)

    def test_stray_caption_word_and_photo_opening_show_up_in_check(self):
        self.t.asset("talk", words=words(("left", 2.85, 3.10), ("this", 3.2, 3.5), ("is", 3.5, 3.6), ("fine", 3.6, 4.0)))
        self.t.asset("pic", kind="photo")
        plan = self.t.plan("w", [{"asset": "pic", "duration": 1.5}, {"asset": "talk", "in": 3.0, "out": 4.5, "captions": "transcript"}])
        warnings = build(self.t.project, load_plan(self.t.project, plan))["warnings"]
        self.assertTrue(any("cuts into 'left'" in w for w in warnings), warnings)
        self.assertTrue(any("opens on a photo" in w for w in warnings), warnings)


def spined(n: int, spine: str | None, fmt: str | None = None) -> list[dict]:
    return [{"slug": f"{spine}-{fmt}-{i}", **({"spine": spine} if spine else {}), **({"format": fmt} if fmt else {})} for i in range(n)]


class SpineWarningTests(unittest.TestCase):
    def test_missing_warns_and_set_does_not(self):
        self.assertIn("not set", spine_warnings({}, None)[0])
        self.assertIn("not set", spine_warnings({"spine": None}, None)[0])
        for spine in ("story", "sound", "trend"):
            self.assertEqual(spine_warnings({"spine": spine}, None), [])

    def test_voice_share_floor(self):
        self.assertIn("29%", spine_warnings({"spine": "voice"}, 0.29)[0])
        self.assertEqual(spine_warnings({"spine": "voice"}, 0.30), [])

    def test_other_spines_ignore_voice_share(self):
        self.assertEqual(spine_warnings({"spine": "sound"}, 0.0), [])


class BatchWarningTests(unittest.TestCase):
    def test_a_good_week_is_quiet(self):
        week = spined(4, "voice") + spined(1, "trend", "beat-edit") + spined(1, "story") + spined(1, "sound", "just-the-bike")
        self.assertEqual(batch_warnings(week), [])

    def test_too_few_voice_reels(self):
        got = batch_warnings(spined(3, "voice") + spined(4, "story"))
        self.assertEqual(len(got), 1)
        self.assertIn("3 of 7", got[0])

    def test_more_than_one_reel_without_a_spine(self):
        got = batch_warnings(spined(4, "voice") + spined(1, "story") + spined(2, None))
        self.assertEqual(len(got), 1)
        self.assertIn("2 of 7", got[0])

    def test_more_than_one_montage_however_it_is_written(self):
        got = batch_warnings(spined(4, "voice") + spined(1, "story") + spined(1, "trend", "Beat Edit") + spined(1, "trend", "ride_montage"))
        self.assertEqual(len(got), 1)
        self.assertIn("pure montage", got[0])

    def test_a_missing_spine_and_a_montage_add_up(self):
        self.assertEqual(len(batch_warnings(spined(4, "voice") + spined(1, "story") + spined(1, None) + spined(1, "trend", "ride-montage"))), 1)

    def test_only_a_batch_of_seven(self):
        for n in (1, 6, 8):
            self.assertEqual(batch_warnings(spined(n, None)), [])


def first_shot(*, face=None, role=None, match="moment", captions="none") -> dict:
    return {"segments": [{"position": "first", "match": match, "captions": captions, "tags": {"face": face, "role": role}},
                         {"position": "last", "match": "moment", "captions": "none", "tags": {"face": False, "role": "texture"}}]}  # fmt: skip


class OpenerTests(unittest.TestCase):
    def test_scenery_first_is_flagged_with_its_role(self):
        got = opener_warnings({"spine": "story"}, first_shot(face=False, role="texture"))
        self.assertEqual(len(got), 1)
        self.assertIn("no face and nobody talking (role: texture)", got[0])
        self.assertIn("aud-person-01", got[0])

    def test_a_face_or_a_voice_first_is_quiet(self):
        self.assertEqual(opener_warnings({}, first_shot(face=True, role="reaction")), [])
        self.assertEqual(opener_warnings({}, first_shot(face=False, role="voice")), [])  # a teammate talking counts
        self.assertEqual(opener_warnings({}, first_shot(face=False, role="crew")), [])  # the group is the subject, even from behind
        self.assertEqual(opener_warnings({}, first_shot(face=False, role="effort", captions="transcript")), [])

    def test_unknown_is_not_scenery(self):
        self.assertEqual(opener_warnings({}, first_shot(face=None, role="texture")), [])  # moment never got a face tag
        self.assertEqual(opener_warnings({}, first_shot(face=False, match="none")), [])  # untagged span
        self.assertEqual(opener_warnings({}, first_shot(face=False, match="photo")), [])  # photo openings have their own warning
        self.assertEqual(opener_warnings({}, None), [])

    def test_a_sound_reel_opens_on_its_sound(self):
        self.assertEqual(opener_warnings({"spine": "sound"}, first_shot(face=False, role="texture")), [])


class OpenerInPlanTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        moment = {"id": "m-bbbbbb", "in": 0, "out": 10, "label": "empty road", "score": 4, "role": "texture", "face": False}
        self.t.asset("road", tags={"type": "solo-ride", "shot": "pov", "quality": 4, "summary": "road", "moments": [moment]})
        self.t.asset("selfie", tags={"type": "solo-ride", "shot": "selfie", "quality": 4, "summary": "grin",
                                     "moments": [{**moment, "id": "m-cccccc", "label": "grin", "role": "reaction", "face": True}]})  # fmt: skip

    def lines(self, order: list[str]) -> list[str]:
        plan = self.t.plan("reel", [{"asset": a, "in": 0.0, "out": 4.0} for a in order], spine="story")
        return [w for w in build(self.t.project, load_plan(self.t.project, plan))["warnings"] if "opens on a shot" in w]

    def test_check_flags_a_road_opening_and_not_a_face_opening(self):
        self.assertEqual(len(self.lines(["road", "selfie"])), 1)
        self.assertEqual(self.lines(["selfie", "road"]), [])


class SpineInPlanTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.t.asset("talk", words=words(("hello", 1.1, 1.5), ("there", 3.0, 3.4)),
                     tags={"type": "talking", "shot": "selfie", "quality": 4, "summary": "the rider speaking",
                           "moments": [{"id": "m-aaaaaa", "in": 0, "out": 10, "label": "talk", "score": 4, "role": "voice", "speaker": "me"}]})
        self.t.asset("road")

    def built(self, voice_out: float, **extra) -> dict:
        plan = self.t.plan("reel", [{"asset": "talk", "in": 1.0, "out": voice_out, "captions": "transcript"}, {"asset": "road", "in": 0.0, "out": 8.0}], **extra)
        return build(self.t.project, load_plan(self.t.project, plan))

    def spine_lines(self, got: dict) -> list[str]:
        return [w for w in got["warnings"] if w.startswith("spine:")]

    def test_unknown_spine_is_an_error(self):
        with self.assertRaises(ReelsError) as caught:
            self.built(3.0, spine="vibes")
        self.assertIn("spine must be one of", str(caught.exception))

    def test_missing_spine_is_only_a_warning(self):
        self.assertIn("not set", self.spine_lines(self.built(3.0))[0])

    def test_low_voice_share_matches_lineage(self):
        got = self.built(3.0, spine="voice")  # 2 s of voice in 10 s
        self.assertEqual(from_built("reel", got)["voiceShare"], 0.2)
        self.assertIn("20%", self.spine_lines(got)[0])

    def test_enough_voice_is_quiet(self):
        self.assertEqual(self.spine_lines(self.built(4.5, spine="voice")), [])  # 3.5 s of 11.5 s

    def test_cutaways_keep_the_voice_going(self):
        plan = self.t.plan("over", [{"asset": "talk", "in": 1.0, "out": 4.0, "captions": "transcript", "cutaways": [{"at": 0.5, "asset": "road", "in": 1.0, "out": 3.0}]}], spine="voice")
        self.assertEqual(self.spine_lines(build(self.t.project, load_plan(self.t.project, plan))), [])

    def test_check_warns_about_a_seven_reel_batch(self):
        plans = [str(self.t.plan(f"reel-{i}", [{"asset": "road", "in": 0.0, "out": 5.0}], spine="story")) for i in range(7)]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(cli.cmd_check(self.t.project, SimpleNamespace(plans=plans)), 0)
        self.assertIn("batch: 0 of 7 reels are spine: voice", err.getvalue())

    def test_check_says_nothing_about_a_smaller_batch(self):
        plans = [str(self.t.plan(f"reel-{i}", [{"asset": "road", "in": 0.0, "out": 5.0}], spine="story")) for i in range(2)]
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            cli.cmd_check(self.t.project, SimpleNamespace(plans=plans))
        self.assertNotIn("batch:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
