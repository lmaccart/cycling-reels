from __future__ import annotations

import unittest
from unittest import mock

from PIL import Image

from helpers import TempProject, words
from reels import exhibits, vision
from reels.render import build, load_plan
from reels.util import ReelsError, write_json

TALK = words(("so", 0.2, 0.4), ("this", 0.5, 0.7), ("study", 0.7, 1.0), ("says", 1.0, 1.3), ("ride", 2.0, 2.3), ("easy", 2.3, 2.8),
             ("more", 3.2, 3.5), ("often", 3.5, 4.0))  # fmt: skip


def tip(x: float, y: float, dx: float = 1.0, dy: float = 0.0) -> dict:
    return {"x": x, "y": y, "dx": dx, "dy": dy, "score": 1.0}


def hand(tip_xy, mcp, wrist, conf=0.9, curled=True) -> dict:
    """A hand as the Vision helper reports it: index out from the knuckle, other fingers curled or open."""
    h = {"side": "right", "tip": [*tip_xy, conf], "mcp": [*mcp, conf], "wrist": [*wrist, conf],
         "pip": [(tip_xy[0] + mcp[0]) / 2, (tip_xy[1] + mcp[1]) / 2, conf]}  # fmt: skip
    for f in ("middle", "ring", "little"):
        h[f"{f}Pip"] = [mcp[0], mcp[1] + 0.02, conf]
        h[f"{f}Tip"] = [wrist[0], wrist[1] - 0.01, conf] if curled else [tip_xy[0], tip_xy[1] + 0.03, conf]
    return h


class ExhibitTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.p = self.t.project
        self.t.asset("talk", duration=12, words=TALK)
        self.t.asset("broll", duration=20)
        self.t.asset("pic", kind="photo")
        Image.new("RGB", (1200, 800), "white").save(self.p.exhibits_dir / "vo2-study.png")
        Image.new("RGB", (800, 1000), "white").save(self.p.exhibits_dir / "zones.jpg")
        cut = Image.new("RGBA", (600, 600), (0, 0, 0, 0))
        cut.paste((200, 30, 30, 255), (100, 100, 500, 500))
        cut.save(self.p.exhibits_dir / "bottle.png")

    def built(self, exhibits_, *, plan_level=None, **seg):
        segment = {"asset": "talk", "in": 0.0, "out": 6.0, "exhibits": exhibits_, **seg}
        extra = {"exhibits": plan_level} if plan_level else {}
        plan = self.t.plan("study-reel", [segment, {"asset": "broll", "in": 10.0, "out": 12.0}], **extra, **self.extra)
        return build(self.p, load_plan(self.p, plan))

    extra: dict = {}

    def problems(self, exhibits_, **seg) -> str:
        with self.assertRaises(ReelsError) as cm:
            self.built(exhibits_, **seg)
        return str(cm.exception)

    # --- A: placed ---------------------------------------------------------

    def test_a_placed_card_gets_its_spot_size_and_time(self):
        b = self.built([{"image": "vo2-study", "at": 1.0, "for": 3.0, "x": 0.6, "y": 0.4, "width": 0.5}])
        (e,) = b["props"]["exhibits"]
        self.assertEqual((e["startFrame"], e["endFrame"], e["kind"], e["src"]), (30, 120, "image", "exhibit0.png"))
        self.assertEqual((e["x"], e["y"], e["width"], e["height"]), (648.0, 768.0, 540.0, 360.0))
        self.assertEqual((e["style"], e["depth"], e["shadow"], e["tilt"], e["background"]), ("card", "front", True, 0, None))
        self.assertNotIn("foregrounds", b["props"])
        self.assertNotIn("exhibits", build(self.p, load_plan(self.p, self.t.plan("plain", [{"asset": "broll", "in": 0, "out": 3}])))["props"])

    def test_exhibits_reaching_the_lower_half_stay_left_of_the_button_column(self):
        # Upper half: may run to the right safe edge. Reaching below the buttons' top: kept left of them.
        cx, cy = exhibits._inside(1080, 500, 400, 300)
        self.assertEqual((cx + 200, cy), (exhibits.SAFE_RIGHT, 500))
        cx, cy = exhibits._inside(1080, 1200, 400, 300)
        self.assertEqual((cx + 200, cy), (exhibits.BUTTONS_LEFT, 1200))
        cx, _ = exhibits._inside(1080, 900, 400, 300)  # bottom edge at 1050 dips into the column
        self.assertEqual(cx + 200, exhibits.BUTTONS_LEFT)
        self.assertEqual(exhibits._inside(600, 1200, 400, 300), (600, 1200))  # already clear: untouched
        cx, _ = exhibits._inside(900, 1200, 1000, 300)  # too wide to fit beside the column: centred in what's left
        self.assertEqual(cx, (exhibits.SAFE_LEFT + exhibits.BUTTONS_LEFT) / 2)

    def test_defaults_run_to_the_end_of_the_segment_and_stay_in_the_safe_area(self):
        b = self.built([{"image": "zones.jpg", "x": 0.95, "y": 0.05, "width": 0.5}])
        e = b["props"]["exhibits"][0]
        self.assertEqual((e["startFrame"], e["endFrame"]), (9, 177))
        left, top, right, bottom = exhibits.bounds(e)
        self.assertGreaterEqual(top, exhibits.SAFE_TOP)
        self.assertLessEqual(right, exhibits.SAFE_RIGHT)

    def test_cutouts_keep_their_shape_and_see_through_cards_get_a_backing(self):
        b = self.built([{"image": "bottle.png", "style": "cutout", "at": 0.5, "for": 2, "tilt": -6},
                        {"image": "bottle.png", "at": 3, "for": 2}])  # fmt: skip
        cut, card = b["props"]["exhibits"]
        self.assertEqual((cut["style"], cut["tilt"], cut.get("background")), ("cutout", -6, None))
        self.assertEqual((card["style"], card["background"]), ("card", "#ffffff"))

    def test_plan_level_exhibits_use_reel_time_and_may_span_a_cut(self):
        b = self.built([], plan_level=[{"image": "vo2-study", "at": 5.0, "for": 2.5}])
        e = b["props"]["exhibits"][0]
        self.assertEqual((e["startFrame"], e["endFrame"]), (150, 225))

    def test_images_come_only_from_the_exhibits_folder(self):
        self.assertEqual(exhibits.resolve(self.p, "vo2").name, "vo2-study.png")
        (self.p.root / "outside.png").write_bytes((self.p.exhibits_dir / "vo2-study.png").read_bytes())
        self.assertIn("no image", self.problems([{"image": "../outside.png"}]))
        self.assertIn("no image", self.problems([{"image": "missing"}]))
        Image.new("RGB", (10, 10)).save(self.p.exhibits_dir / "vo2-other.png")
        self.assertIn("matches several", self.problems([{"image": "vo2"}]))
        # an exhibit is never footage: it can't be a segment or a cutaway
        plan = self.t.plan("p", [{"asset": "vo2-study.png", "duration": 3}])
        with self.assertRaises(ReelsError) as cm:
            build(self.p, load_plan(self.p, plan))
        self.assertIn("no asset matches", str(cm.exception))

    def test_problems_are_explained(self):
        self.assertIn("not both", self.problems([{"image": "vo2-study", "chart": {"points": [[0, 1], [1, 2]]}}]))
        self.assertIn("not neither", self.problems([{"at": 1}]))
        self.assertIn("unknown field(s) zoom", self.problems([{"image": "vo2-study", "zoom": 2}]))
        self.assertIn("too short to read", self.problems([{"image": "vo2-study", "at": 5.8}]))
        self.assertIn("past the end of its segment", self.problems([{"image": "vo2-study", "at": 7}]))
        self.assertIn("only apply with", self.problems([{"image": "vo2-study", "follow": True}]))
        self.assertIn("crop is", self.problems([{"image": "vo2-study", "crop": [0.5, 0, 0.2, 1]}]))
        self.assertIn("style must be one of", self.problems([{"image": "vo2-study", "style": "polaroid"}]))

    def test_crop_shows_part_of_the_image_and_staging_sizes_it(self):
        b = self.built([{"image": "vo2-study", "at": 1, "for": 2, "crop": [0, 0, 0.5, 1], "width": 0.25}])
        e = b["props"]["exhibits"][0]
        self.assertEqual((e["width"], e["height"]), (270.0, 360.0))  # 600x800 of the image
        b["props"].pop("foregrounds", None)
        exhibits.stage(self.p, b, self.p.root)
        with Image.open(self.p.root / "exhibit0.png") as im:
            self.assertEqual(im.size, (540, 720))

    # --- text gets out of the way ------------------------------------------

    def test_hook_and_captions_move_clear_of_an_exhibit(self):
        self.extra = {"hook": "why easy rides make you faster"}
        self.addCleanup(setattr, self, "extra", {})
        b = self.built([{"image": "vo2-study", "at": 0.2, "for": 3.0, "x": 0.5, "y": 0.26, "width": 0.7},
                        {"image": "zones.jpg", "at": 3.4, "for": 2.0, "x": 0.5, "y": 0.62, "width": 0.4}], captions="transcript")  # fmt: skip
        first, second = b["props"]["exhibits"]
        hook = b["props"]["hook"]
        _, top, _, bottom = exhibits.bounds(first)
        self.assertLess(top, exhibits.TOP_TEXT)  # it covers where the hook normally sits
        self.assertGreaterEqual(hook["offsetTop"], bottom + exhibits.PAD)
        pages = b["props"]["captions"]
        early = [p for p in pages if p["endFrame"] <= second["startFrame"]]
        late = [p for p in pages if p["startFrame"] < second["endFrame"] and second["startFrame"] < p["endFrame"]]
        self.assertTrue(early and late)
        self.assertTrue(all("offsetBottom" not in p for p in early))  # the first exhibit is nowhere near the captions
        _, top2, _, bottom2 = exhibits.bounds(second)
        for p in late:
            cap_bottom = 1920 - p["offsetBottom"]
            self.assertTrue(cap_bottom <= top2 - exhibits.PAD or cap_bottom - exhibits.LINE >= bottom2 + exhibits.PAD)

    def test_a_big_hook_keeps_its_lines_and_stays_on_top_of_exhibits(self):
        self.extra = {"hook": {"text": "THIS\nIS  MY\n\nART", "size": "big", "position": "center", "style": "plain"}}
        self.addCleanup(setattr, self, "extra", {})
        b = self.built([{"image": "vo2-study", "at": 0.0, "for": 5.0, "x": 0.5, "y": 0.5, "width": 0.9}])
        hook = b["props"]["hook"]
        self.assertEqual((hook["text"], hook["size"], hook["position"]), ("THIS\nIS MY\nART", "big", "center"))
        self.assertNotIn("offsetTop", hook)
        self.assertFalse(any("hook sits on an exhibit" in w for w in b["warnings"]))

    def test_safe_area_off_lets_a_sticker_sit_under_the_buttons(self):
        spot = {"image": "bottle", "style": "cutout", "x": 0.95, "y": 0.9, "width": 0.3}
        (kept,) = self.built([spot])["props"]["exhibits"]
        (free,) = self.built([{**spot, "safeArea": False}])["props"]["exhibits"]
        self.assertLessEqual(kept["x"] + kept["width"] / 2, exhibits.BUTTONS_LEFT)
        self.assertEqual((free["x"], free["y"]), (0.95 * 1080, 0.9 * 1920))
        self.assertIn("safeArea", self.problems([{**spot, "safeArea": "no"}]))
        self.assertIn("placed exhibits only", self.problems([{**spot, "safeArea": False, "anchor": "hand"}]))

    def test_normal_hooks_fold_line_breaks_and_unknown_sizes_warn(self):
        self.extra = {"hook": {"text": "legs\nsaid no", "size": "huge"}}
        self.addCleanup(setattr, self, "extra", {})
        b = self.built([{"image": "vo2-study", "at": 4.0, "for": 1.0, "x": 0.5, "y": 0.5, "width": 0.4}])
        hook = b["props"]["hook"]
        self.assertEqual(hook["text"], "legs said no")
        self.assertNotIn("size", hook)
        self.assertTrue(any("size 'huge' unknown" in w for w in b["warnings"]))

    def test_text_with_nowhere_to_go_is_reported(self):
        beats = [{"text": "the whole frame is taken", "startFrame": 0, "endFrame": 60, "position": "center"}]
        full = [{"props": {"startFrame": 0, "endFrame": 90, "x": 540, "y": 880, "width": 900, "height": 1250}}]
        warnings: list[str] = []
        exhibits.make_room(full, None, beats, [], warnings, 30)
        self.assertIn("no room above or below", warnings[0])
        self.assertEqual(beats[0]["position"], "center")
        # and text that isn't near an exhibit, or isn't up at the same time, stays put
        later = [{"text": "after", "startFrame": 100, "endFrame": 160, "position": "center"}]
        exhibits.make_room(full, None, later, [], warnings, 30)
        self.assertEqual((later[0]["position"], len(warnings)), ("center", 1))

    # --- B: anchored to the pointing hand ----------------------------------

    def anchored(self, tips, **ex):
        with mock.patch.object(vision, "available", return_value=None), \
                mock.patch.object(exhibits, "track_hand", return_value=tips) as tracked:
            b = self.built([{"image": "vo2-study", "at": 1.0, "for": 2.0, "width": 0.4, "anchor": "hand", **ex}])
        self.assertEqual(tracked.call_args.args[2:4], (30, 90))  # the frames the exhibit is up, from the segment start
        return b

    def test_b_exhibit_sits_just_beyond_the_fingertip(self):
        b = self.anchored([None] * 10 + [tip(0.4, 0.5)] * 50)
        e = b["props"]["exhibits"][0]
        # pointing right from x=432: a 0.03-frame gap, then half the card's width
        self.assertAlmostEqual(e["x"], 432 + 0.03 * 1080 + 216, places=0)
        self.assertAlmostEqual(e["y"], 960, places=0)
        self.assertNotIn("path", e)
        self.assertIn("at the fingertip", b["exhibits"][0]["note"])

    def test_b_side_and_point_override_where_it_lands(self):
        e = self.anchored([tip(0.5, 0.6)] * 60, side="above")["props"]["exhibits"][0]
        self.assertAlmostEqual(e["x"], 540, places=0)
        self.assertAlmostEqual(e["y"], 0.6 * 1920 - 0.03 * 1080 - e["height"] / 2, places=0)
        e = self.anchored([tip(0.5, 0.5)] * 60, point=[0.25, 0.75])["props"]["exhibits"][0]
        # that spot of the image is under the finger
        self.assertAlmostEqual(e["x"] + (0.25 - 0.5) * e["width"], 540, places=0)
        self.assertAlmostEqual(e["y"] + (0.75 - 0.5) * e["height"], 960, places=0)

    def test_b_follow_tracks_the_hand_smoothly(self):
        moving = [tip(0.3 + 0.004 * i, 0.5) for i in range(60)]
        moving[20] = moving[21] = None  # the hand drops out for two frames
        b = self.anchored(moving, follow=True, side="above")
        path = b["props"]["exhibits"][0]["path"]
        self.assertEqual((path[0][0], path[-1][0]), (0, 59))
        xs = [k[1] for k in path]
        self.assertEqual(xs, sorted(xs))
        self.assertLess(max(b_ - a for a, b_ in zip(xs, xs[1:])), 15)  # glides, no jump over the gap
        self.assertIn("follows the hand", b["exhibits"][0]["note"])

    def test_b_falls_back_to_the_typed_spot_when_no_hand_points(self):
        b = self.anchored([None] * 58 + [tip(0.4, 0.5)] * 2, x=0.7, y=0.3)
        e = b["props"]["exhibits"][0]
        self.assertEqual((e["x"], e["y"]), (756.0, 576.0))
        self.assertTrue(any("no pointing hand found" in w and "placed with x/y instead" in w for w in b["warnings"]), b["warnings"])
        with mock.patch.object(vision, "available", return_value="hand anchoring and depth need macOS"):
            b = self.built([{"image": "vo2-study", "at": 1, "for": 2, "anchor": "hand"}])
        self.assertTrue(any("need macOS" in w for w in b["warnings"]))

    def test_pointing_needs_an_extended_index_finger(self):
        out = hand((0.60, 0.40), (0.52, 0.42), (0.45, 0.45))
        p = vision.pointing(out)
        self.assertGreater(p["dx"], 0.9)
        self.assertEqual((p["x"], p["y"]), (0.60, 0.40))
        self.assertIsNone(vision.pointing(hand((0.50, 0.44), (0.52, 0.42), (0.45, 0.45))))  # curled back to the palm
        self.assertIsNone(vision.pointing(hand((0.60, 0.40), (0.52, 0.42), (0.45, 0.45), conf=0.1)))
        open_palm = hand((0.60, 0.40), (0.52, 0.42), (0.45, 0.45), curled=False)
        tips = vision.fingertips([{"i": 0, "hands": [open_palm, hand((0.30, 0.30), (0.24, 0.33), (0.18, 0.36))]}, {"i": 1, "hands": []}])
        self.assertEqual((tips[0]["x"], tips[1]), (0.30, None))  # the hand that points beats the open one

    def test_steady_point_is_a_median_and_needs_enough_frames(self):
        tips = [tip(0.4, 0.5)] * 20 + [tip(0.9, 0.1)] * 3 + [None] * 7
        got = vision.steady_point(tips)
        self.assertEqual((got["x"], got["y"]), (0.4, 0.5))
        self.assertIsNone(vision.steady_point([None] * 40 + [tip(0.4, 0.5)] * 2))
        self.assertIsNone(vision.smooth_track([None] * 40 + [tip(0.4, 0.5)] * 2))

    def test_punch_ins_move_the_anchor_with_the_picture(self):
        keys = [[0, 1.0, 0.5, 0.4], [10, 1.0, 0.5, 0.4], [14, 1.2, 0.5, 0.4]]
        self.assertEqual(exhibits.punch_at(keys, 5), (1.0, 0.5, 0.4))
        self.assertAlmostEqual(exhibits.punch_at(keys, 12)[0], 1.1)
        self.assertEqual(exhibits.punch_at(keys, 99), (1.2, 0.5, 0.4))
        self.assertEqual(exhibits.punch_at(None, 3), (1.0, 0.5, 0.5))

    # --- C: depth -----------------------------------------------------------

    def test_c_behind_adds_a_cutout_of_the_rider_over_the_same_frames(self):
        with mock.patch.object(vision, "available", return_value=None):
            b = self.built([{"image": "vo2-study", "at": 1.0, "for": 2.0, "depth": "behind"},
                            {"image": "zones.jpg", "at": 3.5, "for": 1.5, "depth": "behind"},
                            {"image": "bottle.png", "at": 5.0, "style": "cutout"}], punch=[{"at": 1.5, "scale": 1.15}])  # fmt: skip
        (fg,) = b["props"]["foregrounds"]
        self.assertEqual((fg["src"], fg["seg"], fg["startFrame"], fg["durationInFrames"], fg["offset"]), ("fg0.webm", 0, 30, 120, 30))
        self.assertEqual(fg["punch"], b["props"]["segments"][0]["punch"])  # the cut-out zooms with the footage
        self.assertEqual([e["depth"] for e in b["props"]["exhibits"]], ["behind", "behind", "front"])

    def test_c_needs_the_rider_on_screen(self):
        with mock.patch.object(vision, "available", return_value=None):
            msg = self.problems([{"image": "vo2-study", "at": 1.0, "for": 3.0, "depth": "behind"}],
                                cutaways=[{"at": 2.0, "asset": "broll", "in": 0, "out": 1.5}])  # fmt: skip
            self.assertIn("under a cutaway you're not on screen", msg)
            plan = self.t.plan("p", [{"asset": "pic", "duration": 3, "exhibits": [{"image": "vo2-study", "depth": "behind"}]}])
            with self.assertRaises(ReelsError) as cm:
                build(self.p, load_plan(self.p, plan))
            self.assertIn("needs video under it", str(cm.exception))
        with mock.patch.object(vision, "available", return_value="hand anchoring and depth need macOS (Apple's Vision framework)"):
            self.assertIn("need macOS", self.problems([{"image": "vo2-study", "depth": "behind"}]))

    # --- charts ---------------------------------------------------------------

    def test_chart_of_typed_numbers(self):
        b = self.built([{"chart": {"title": "retention", "unit": "%", "points": [[3, 40], [0, 100], [30, 25]],
                                   "mark": {"x": 3, "label": "0:03"}}, "at": 1, "for": 3, "width": 0.5}])  # fmt: skip
        e = b["props"]["exhibits"][0]
        c = e["chart"]
        self.assertEqual((e["kind"], e["width"], e["height"], "src" in e), ("chart", 540.0, 432.0, False))
        self.assertEqual((c["xs"], c["ys"], c["source"]), ([0.0, 3.0, 30.0], [100.0, 40.0, 25.0], "typed"))
        self.assertEqual(c["mark"], {"x": 3.0, "y": 40.0, "label": "0:03"})
        self.assertEqual((c["yMin"], [t["label"] for t in c["yTicks"]]), (0.0, ["0", "25", "50", "75", "100"]))
        self.assertIn("at least two points", self.problems([{"chart": {"points": [[0, 1]]}}]))
        self.assertIn("outside the data", self.problems([{"chart": {"points": [[0, 1], [5, 2]], "mark": {"x": 9}}}]))
        self.assertIn("won't read on a phone", self.problems([{"chart": {"type": "bar", "values": list(range(20))}}]))

    def test_bar_chart_with_labels_starts_at_zero(self):
        b = self.built([{"chart": {"type": "bar", "values": [310, 322, 335], "labels": ["sep", "oct", "nov"], "unit": "W"}, "for": 3}])
        c = b["props"]["exhibits"][0]["chart"]
        self.assertEqual((c["yMin"], [t["label"] for t in c["xTicks"]]), (0.0, ["sep", "oct", "nov"]))

    def test_chart_of_the_users_own_strava_ride(self):
        from reels.library import load_asset, save_asset

        t = list(range(0, 1201))
        write_json(self.p.root / "strava" / "activities" / "111.json",
                   {"id": "111", "streams": {"time": t, "watts": [150 if s < 600 else 300 for s in t]}})  # fmt: skip
        self.assertIn("no Strava ride linked", self.problems([{"chart": {"data": "strava:power"}}]))
        a = load_asset(self.p, "talk")
        a["strava"] = {"activity": "111", "offset": 30.0}
        save_asset(self.p, a)
        b = self.built([{"chart": {"data": "strava:power"}, "for": 3}])
        c = b["props"]["exhibits"][0]["chart"]
        self.assertEqual((c["source"], c["unit"], c["title"], c["xLabel"]), ("strava", "W", "power", "min"))
        self.assertEqual((c["xs"][0], c["xs"][-1], c["ys"][2], c["ys"][-3]), (0.0, 20.0, 150.0, 300.0))
        self.assertLessEqual(len(c["xs"]), exhibits.CHART_POINTS + 1)
        b = self.built([{"chart": {"data": "strava:power", "from": 12, "to": 18, "ride": "111"}, "for": 3}])
        self.assertEqual(set(b["props"]["exhibits"][0]["chart"]["ys"]), {300.0})
        self.assertIn("has no heart_rate stream", self.problems([{"chart": {"data": "strava:hr"}}]))
        self.assertIn("unknown", self.problems([{"chart": {"data": "strava:kudos"}}]))

    # --- bookkeeping ----------------------------------------------------------

    def test_exhibit_changes_count_for_retention_and_the_end_card_takes_over(self):
        long_talk = [{"asset": "talk", "in": 0.0, "out": 11.5}]
        plain = build(self.p, load_plan(self.p, self.t.plan("held", long_talk)))
        self.assertTrue(any("nothing changes on screen" in w for w in plain["warnings"]))
        long_talk[0]["exhibits"] = [{"image": "vo2-study", "at": 3.5, "for": 4.0}]
        shown = build(self.p, load_plan(self.p, self.t.plan("held", long_talk)))
        self.assertFalse(any("nothing changes on screen" in w for w in shown["warnings"]))
        long_talk[0]["exhibits"] = [{"image": "vo2-study", "at": 8.0}]
        carded = build(self.p, load_plan(self.p, self.t.plan("held", long_talk, endCard={"seconds": 2.0, "handle": "@me"})))
        self.assertEqual(carded["props"]["exhibits"][0]["endFrame"], carded["props"]["endCard"]["startFrame"])

    def test_add_copies_an_image_in_and_refuses_other_files(self):
        src = self.p.root / "Screen Shot 2026-11-11.png"
        Image.new("RGB", (40, 30)).save(src)
        dest = exhibits.add(self.p, src)
        self.assertEqual(dest, self.p.exhibits_dir / "screen-shot-2026-11-11.png")
        with self.assertRaises(ReelsError):
            exhibits.add(self.p, src)  # already there
        notes = self.p.root / "notes.txt"
        notes.write_text("x")
        with self.assertRaises(ReelsError):
            exhibits.add(self.p, notes)


if __name__ == "__main__":
    unittest.main()
