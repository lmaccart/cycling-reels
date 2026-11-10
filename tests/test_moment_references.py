from __future__ import annotations

import unittest

from helpers import TempProject
from reels.library import load_asset, save_asset
from reels.render import build
from reels.tags import merge_tags
from reels.util import ReelsError


class MomentReferenceTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.asset = self.t.asset("clip", tags={
            "type": "solo-ride", "shot": "pov", "quality": 4, "summary": "ride",
            "moments": [{"id": "m-aaaaaa", "in": 5, "out": 8, "label": "keep this shot", "score": 4}],
        })

    def segment(self, reference):
        return {"asset": "clip", "moment": reference}

    def built(self, reference):
        return build(self.t.project, {"slug": "r", "autoSfx": False, "segments": [self.segment(reference)]})

    def test_adding_an_earlier_moment_preserves_both_stable_and_legacy_references(self):
        self.asset["tags"] = merge_tags(self.asset, {"addMoments": [{"in": 0, "out": 3, "label": "new shot", "score": 3}]})
        save_asset(self.t.project, self.asset)
        for reference in ("m-aaaaaa", 0):
            seg = self.built(reference)["segs"][0]
            self.assertEqual((seg.spec["in"], seg.spec["out"]), (5, 8))

    def test_stable_reference_survives_reordering_and_works_in_cutaways(self):
        self.asset["tags"] = merge_tags(self.asset, {"addMoments": [{"in": 0, "out": 3, "label": "new shot", "score": 3}]})
        self.asset["tags"]["moments"].reverse()
        save_asset(self.t.project, self.asset)
        seg = self.built("m-aaaaaa")["segs"][0]
        self.assertEqual((seg.spec["in"], seg.spec["out"]), (5, 8))
        self.t.asset("talk")
        built = build(self.t.project, {"slug": "cut", "autoSfx": False, "segments": [
            {"asset": "talk", "in": 0, "out": 8, "cutaways": [{"at": 1, **self.segment("m-aaaaaa")}]}]})
        cut = built["segs"][0].spec["_cutaways"][0]["seg"]
        self.assertEqual((cut.spec["in"], cut.spec["out"]), (5, 8))

    def test_missing_or_ambiguous_stable_reference_is_an_error(self):
        for reference in ("m-bbbbbb", -1, True, []):
            with self.subTest(reference=reference), self.assertRaises(ReelsError):
                self.built(reference)
        asset = load_asset(self.t.project, "clip")
        asset["tags"]["moments"] *= 2
        save_asset(self.t.project, asset)
        with self.assertRaises(ReelsError):
            self.built("m-aaaaaa")


if __name__ == "__main__":
    unittest.main()
