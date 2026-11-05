from __future__ import annotations

import unittest

from helpers import TempProject, words
from reels.library import load_asset
from reels.render import Seg, build, fit_cutaways, load_plan, mark_usage, reuse_warnings, visuals
from reels.util import ReelsError

TALK = words(("so", 0.2, 0.4), ("this", 0.5, 0.7), ("is", 0.7, 0.8), ("my", 0.8, 1.0), ("week", 1.0, 1.4), ("of", 3.0, 3.1),
             ("training", 3.1, 3.6))  # fmt: skip


class CutawayTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.t.asset("talk", duration=12, words=TALK)
        self.t.asset("broll", duration=20)
        self.t.asset("pic", kind="photo")

    def built(self, cutaways, **seg):
        segment = {"asset": "talk", "in": 0.0, "out": 6.0, "captions": "transcript", "cutaways": cutaways, **seg}
        plan = self.t.plan("talk-reel", [segment, {"asset": "broll", "in": 10.0, "out": 12.0}])
        return build(self.t.project, load_plan(self.t.project, plan))

    def problems(self, cutaways, **seg) -> str:
        with self.assertRaises(ReelsError) as cm:
            self.built(cutaways, **seg)
        return str(cm.exception)

    def test_props_place_silent_broll_inside_the_segment(self):
        b = self.built([{"at": 1.5, "asset": "broll", "in": 2.0, "out": 4.0}, {"at": 4.0, "asset": "pic", "duration": 1.2, "motion": "zoom-out"}])
        cuts = b["props"]["segments"][0]["cutaways"]
        self.assertEqual(cuts[0], {"startFrame": 45, "durationInFrames": 60, "kind": "video", "src": "cut0-0.mp4"})
        self.assertEqual((cuts[1]["kind"], cuts[1]["src"], cuts[1]["startFrame"], cuts[1]["durationInFrames"], cuts[1]["motion"]),
                         ("photo", "cut0-1.jpg", 120, 36, "zoom-out"))  # fmt: skip
        self.assertNotIn("cutaways", b["props"]["segments"][1])
        # The talking clip's audio and captions are untouched: the reel is as long as its segments.
        self.assertEqual(b["total"], 240)
        self.assertGreater(b["props"]["segments"][0]["volume"], 0)
        self.assertTrue(b["props"]["captions"])
        self.assertEqual([(w, v.asset["id"], cut) for w, v, cut in visuals(b["segs"])],
                         [("segment 1", "talk", False), ("segment 1 cutaway 1", "broll", True),
                          ("segment 1 cutaway 2", "pic", True), ("segment 2", "broll", False)])  # fmt: skip

    def test_a_moment_or_a_360_view_works_like_on_a_segment(self):
        self.t.asset("cam", duration=30, width=5376, height=2688,
                     tags={"type": "group-ride", "angles": {"rider": {"yaw": 180, "pitch": -15, "fov": 85, "roll": 0}},
                           "moments": [{"in": 5.0, "out": 7.0, "label": "pull", "score": 4, "focusX": 0.5, "focusY": 0.5}]})  # fmt: skip
        sphere = load_asset(self.t.project, "cam")
        sphere["probe"]["sphere"] = {"projection": "equirect", "width": 5376, "height": 2688, "source": "test"}
        from reels.library import save_asset

        save_asset(self.t.project, sphere)
        b = self.built([{"at": 0.5, "asset": "cam", "moment": 0, "view": "rider"}])
        cs = b["segs"][0].spec["_cutaways"][0]["seg"]
        self.assertEqual((cs.spec["in"], cs.spec["out"], cs.spec["sphere"]["view"]["yaw"]), (5.0, 7.0, 180.0))

    def test_cutaway_problems_are_explained(self):
        self.assertIn("belong on the segment", self.problems([{"at": 1, "asset": "broll", "in": 0, "out": 2, "captions": "transcript"}]))
        self.assertIn("past the end of its segment", self.problems([{"at": 5, "asset": "broll", "in": 0, "out": 3}]))
        self.assertIn("too short", self.problems([{"at": 1, "asset": "broll", "in": 0, "out": 0.3}]))
        self.assertIn("overlaps", self.problems([{"at": 1, "asset": "broll", "in": 0, "out": 2}, {"at": 2, "asset": "pic", "duration": 1}]))
        self.assertIn("use {", self.problems([{"asset": "broll", "in": 0, "out": 2}]))
        self.assertIn("no asset matches", self.problems([{"at": 1, "asset": "nope", "in": 0, "out": 2}]))
        self.assertIn("must be a list", self.problems({"at": 1}))

    def test_cutaways_only_on_video_and_not_under_live_numbers(self):
        plan = self.t.plan("p", [{"asset": "pic", "duration": 3, "cutaways": [{"at": 0.5, "asset": "broll", "in": 0, "out": 1}]}])
        with self.assertRaises(ReelsError) as cm:
            build(self.t.project, load_plan(self.t.project, plan))
        self.assertIn("cutaways go on video segments", str(cm.exception))
        msg = self.problems([{"at": 1, "asset": "broll", "in": 0, "out": 2}], stats=[{"value": "gps"}])
        self.assertIn("would describe the clip under the cutaways", msg)

    def test_footage_guard_applies_to_cutaways(self):
        media = self.t.project.abs(load_asset(self.t.project, "broll")["media"])
        media.write_bytes(b"something else")
        self.assertIn("changed after it was ingested", self.problems([{"at": 1, "asset": "broll", "in": 0, "out": 2}]))

    def test_retiming_trims_or_drops_cutaways_that_no_longer_fit(self):
        b = self.built([{"at": 1.0, "asset": "broll", "in": 0, "out": 2}, {"at": 4.0, "asset": "broll", "in": 5, "out": 7}])
        sg = b["segs"][0]
        sg.frames = 160  # the segment came out ~0.7 s shorter on the beat
        warnings: list[str] = []
        fit_cutaways([sg], 30, warnings)
        last = sg.spec["_cutaways"][-1]["seg"]
        self.assertEqual((last.frames, last.spec["out"]), (40, round(5 + 40 / 30, 3)))
        sg.frames = 125
        fit_cutaways([sg], 30, warnings)
        self.assertEqual(len(sg.spec["_cutaways"]), 1)
        self.assertIn("no longer fits", warnings[-1])

    def test_usage_records_cutaways_and_reuse_is_flagged_for_other_reels(self):
        b = self.built([{"at": 1.0, "asset": "broll", "in": 2.0, "out": 4.0}])
        mark_usage(self.t.project, "talk-reel", b["segs"])
        used = load_asset(self.t.project, "broll")["used"]
        self.assertEqual(sorted((u["in"], u["out"]) for u in used), [(2.0, 4.0), (10.0, 12.0)])
        # the same reel re-checked: its own usage isn't a repeat
        again = self.built([{"at": 1.0, "asset": "broll", "in": 2.0, "out": 4.0}])
        self.assertEqual(reuse_warnings("talk-reel", again["segs"]), [])
        other = reuse_warnings("next-reel", again["segs"])
        self.assertTrue(any("cutaway 1: broll 0:02.0-0:04.0 is already in talk-reel" in w for w in other), other)

    def test_reuse_of_photos_and_partial_overlaps(self):
        segs = [Seg(index=0, kind="video", asset={"id": "v", "used": [{"plan": "old", "in": 3.0, "out": 5.0}]}, spec={"in": 4.5, "out": 8.0}, frames=105),
                Seg(index=1, kind="photo", asset={"id": "p", "used": [{"plan": "old", "at": "x"}]}, spec={}, frames=60),
                Seg(index=2, kind="video", asset={"id": "w", "used": [{"plan": "old", "in": 0.0, "out": 1.0}]}, spec={"in": 1.0, "out": 2.0}, frames=30)]  # fmt: skip
        got = reuse_warnings("new", segs)
        self.assertEqual(len(got), 2)
        self.assertIn("v 0:04.5-0:05.0 is already in old", got[0])
        self.assertIn("photo p is already in old", got[1])


if __name__ == "__main__":
    unittest.main()
