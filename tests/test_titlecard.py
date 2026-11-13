"""Series title board: only for series titles the user OK'd, timed props, its sounds, and the hook moving out of its way."""

from __future__ import annotations

import unittest

from helpers import TempProject
from reels.render import build, load_plan
from reels.titlecard import HIT, SWING, card_sfx, push_hook
from reels.util import ReelsError

QUIET = {"theme": False, "nail": False, "creak": False}  # the temp project has no sfx/ files


class TitleCardTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.t.asset("ride", duration=20)
        self.t.project.config["titleCards"] = {"allowed": ["The Down Week"]}

    def built(self, card, **extra):
        plan = self.t.plan("dw-ep1", [{"asset": "ride", "in": 0.0, "out": 8.0}], titleCard=card, **extra)
        return build(self.t.project, load_plan(self.t.project, plan))

    def test_props(self):
        b = self.built({"title": "the down week", "episode": 2, "subtitle": "rusty legs", **QUIET})
        c = b["props"]["titleCard"]
        fps = b["fps"]
        self.assertEqual((c["title"], c["episode"], c["subtitle"]), ("the down week", "Ep 2.", "rusty legs"))
        self.assertEqual(c["startFrame"], 0)  # whole board on screen in the feed's first frame
        self.assertEqual(c["endFrame"], 3 * fps)
        self.assertLess(c["nailFrame"], c["hitFrame"])
        self.assertLess(c["hitFrame"], c["swingFrame"])
        self.assertEqual(c["hitFrame"], round(HIT * fps))

    def test_needs_permission_for_the_series(self):
        with self.assertRaises(ReelsError) as cm:
            self.built({"title": "Some Other Series", **QUIET})
        self.assertIn("titleCards.allowed", str(cm.exception))
        self.t.project.config["titleCards"] = {}
        with self.assertRaises(ReelsError):
            self.built({"title": "The Down Week", **QUIET})

    def test_bad_fields(self):
        for card in ({"episode": 1}, {"title": "The Down Week", "episode": "one"}, {"title": "The Down Week", "for": 10}):
            with self.assertRaises(ReelsError):
                self.built({**card, **QUIET})

    def test_no_card_no_props(self):
        plan = self.t.plan("plain", [{"asset": "ride", "in": 0.0, "out": 4.0}])
        self.assertNotIn("titleCard", build(self.t.project, load_plan(self.t.project, plan))["props"])

    def test_hook_waits_for_the_board(self):
        b = self.built({"title": "The Down Week", "for": 2.5, **QUIET}, hook="legs feel like wood")
        hook, card = b["props"]["hook"], b["props"]["titleCard"]
        self.assertEqual(hook["startFrame"], card["endFrame"])
        self.assertTrue(any("after the title card" in w for w in b["warnings"]))
        hook = {"startFrame": 0, "endFrame": 60}
        push_hook(hook, {"endFrame": 90}, 100, [])
        self.assertEqual((hook["startFrame"], hook["endFrame"]), (90, 100))

    def test_sounds(self):
        cues = card_sfx({"titleCard": {"title": "The Down Week"}})
        self.assertEqual([c["sound"] for c in cues], ["sting-showdown", "hit-nail", "creak-wood"])
        nail = next(c for c in cues if c["sound"] == "hit-nail")
        self.assertAlmostEqual(nail["at"], HIT, delta=0.05)
        self.assertGreater(next(c for c in cues if c["sound"] == "creak-wood")["at"], SWING - 0.01)
        cues = card_sfx({"titleCard": {"title": "The Down Week", "theme": "sting-slide-down", "creak": False}})
        self.assertEqual([c["sound"] for c in cues], ["sting-slide-down", "hit-nail"])
        self.assertEqual(card_sfx({}), [])

    def test_missing_sound_is_an_error(self):
        with self.assertRaises(ReelsError) as cm:
            self.built({"title": "The Down Week", "theme": False, "creak": False})
        self.assertIn("hit-nail", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
