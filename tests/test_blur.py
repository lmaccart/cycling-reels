from __future__ import annotations

import unittest

from helpers import TempProject
from reels.blur import blur_props
from reels.render import build, load_plan
from reels.util import ReelsError


class BlurTests(unittest.TestCase):
    def test_keys_become_frames_sorted_and_clamped_to_the_segment(self):
        out = blur_props([{"keys": [{"at": 1.0, "x": 0.3, "y": 0.6, "w": 0.2, "h": 0.1}, {"at": 0.0, "x": 0.2, "y": 0.5, "w": 0.2, "h": 0.1},
                                    {"at": 9.0, "x": 0.4, "y": 0.6, "w": 0.2, "h": 0.1}]}], 60, 30, "segment 1")  # fmt: skip
        self.assertEqual(out, [{"strength": 28, "keys": [[0, 0.2, 0.5, 0.2, 0.1], [30, 0.3, 0.6, 0.2, 0.1], [60, 0.4, 0.6, 0.2, 0.1]]}])

    def test_a_single_key_holds_to_the_end_of_the_segment(self):
        out = blur_props([{"keys": [{"at": 0.5, "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3}], "strength": 40}], 90, 30, "segment 1")
        self.assertEqual(out[0], {"strength": 40, "keys": [[15, 0.5, 0.5, 0.3, 0.3], [90, 0.5, 0.5, 0.3, 0.3]]})

    def test_bad_patches_are_plan_errors(self):
        for raw in ({"keys": []}, [{"keys": [{"at": 0, "x": 0.5}]}], [{"keys": [{"at": 0, "x": 5, "y": 0.5, "w": 0.2, "h": 0.2}]}],
                    [{"keys": [{"at": 4, "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}]}],
                    [{"keys": [{"at": 0, "x": 0.5, "y": 0.5, "w": 0.2, "h": 0.2}], "strength": 500}]):  # fmt: skip
            with self.assertRaises(ReelsError):
                blur_props(raw, 60, 30, "segment 1")
        self.assertEqual(blur_props(None, 60, 30, "segment 1"), [])

    def test_build_passes_patches_to_the_template_and_refuses_them_on_cutaways(self):
        t = TempProject(self)
        t.asset("talk", duration=12)
        t.asset("broll", duration=20)
        key = {"at": 0.0, "x": 0.3, "y": 0.6, "w": 0.2, "h": 0.1}
        plan = t.plan("blurred", [{"asset": "talk", "in": 0.0, "out": 4.0, "blur": [{"keys": [key]}]}, {"asset": "broll", "in": 1.0, "out": 3.0}])
        props = build(t.project, load_plan(t.project, plan))["props"]["segments"]
        self.assertEqual(props[0]["blur"], [{"strength": 28, "keys": [[0, 0.3, 0.6, 0.2, 0.1], [120, 0.3, 0.6, 0.2, 0.1]]}])
        self.assertNotIn("blur", props[1])
        bad = t.plan("bad", [{"asset": "talk", "in": 0.0, "out": 4.0,
                              "cutaways": [{"at": 1.0, "asset": "broll", "in": 1.0, "out": 3.0, "blur": [{"keys": [key]}]}]}])  # fmt: skip
        with self.assertRaises(ReelsError):
            build(t.project, load_plan(t.project, bad))


if __name__ == "__main__":
    unittest.main()
