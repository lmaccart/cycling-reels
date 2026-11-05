from __future__ import annotations

import unittest

from helpers import TempProject, words
from reels.render import Seg, build, load_plan
from reels.retention import caption_cuts, fades, opening, still_stretches

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


if __name__ == "__main__":
    unittest.main()
