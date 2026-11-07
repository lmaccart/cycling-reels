"""Road to 5 W/kg progress bar, and Strava-first GPS (GoPro GPS only where Strava has no data)."""

from __future__ import annotations

import unittest

from reels import progress
from reels.library import load_asset
from reels.render import Seg, _routes, _stats, build, top_offset
from reels.util import ReelsError

from helpers import TempProject
from test_strava import StravaTests, ride


class ProgressTests(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(self)
        self.project = self.tp.project

    def seg(self, spec, start=0, frames=150):
        self.tp.asset("clip", duration=10)
        sp = {"in": 0.0, "out": 5.0, "speed": 1.0, **spec}
        return Seg(0, "video", load_asset(self.project, "clip"), sp, frames, start=start)

    def test_entries_and_numbers(self):
        progress.set_entry(self.project, ftp=335, weight=78, date="2026-11-07")
        progress.set_entry(self.project, ftp=340, weight=78, date="2026-11-14")
        progress.set_entry(self.project, ftp=345, weight=78, date="2026-11-14")  # same day replaces
        entries = progress.load(self.project)["wkg"]["entries"]
        self.assertEqual([e["ftp"] for e in entries], [335, 345])
        with self.assertRaises(ReelsError):
            progress.set_entry(self.project, ftp=335, weight=None)
        with self.assertRaises(ReelsError):
            progress.set_entry(self.project, ftp=78, weight=335)  # swapped

    def test_bar_props(self):
        progress.set_entry(self.project, ftp=335, weight=78, date="2026-11-07")
        warnings = []
        [b] = progress.bars(self.project, [self.seg({"progress": {"asOf": "2026-11-07"}})], 150, 30, 150, warnings)
        self.assertEqual((b["value"], b["fill"], b["fillFrames"]), (4.29, 0.0, 0))  # series start: empty bar
        self.assertEqual((b["label"], b["goalText"], b["note"]), ("road to 5 w/kg", "goal 5.0", "55 W to go"))
        self.assertEqual(warnings, [])
        progress.set_entry(self.project, ftp=351, weight=78, date="2026-11-14")
        [b] = progress.bars(self.project, [self.seg({"progress": True})], 150, 30, 150, warnings)
        self.assertEqual((b["value"], b["prevValue"], b["asOf"]), (4.5, 4.29, "2026-11-14"))
        self.assertAlmostEqual(b["fill"], (4.5 - 335 / 78) / (5 - 335 / 78), places=3)
        self.assertGreater(b["fillFrames"], 0)
        self.assertIn("no asOf", warnings[0])
        [b] = progress.bars(self.project, [self.seg({"progress": {"asOf": "2026-11-10"}})], 150, 30, 150, [])
        self.assertEqual(b["asOf"], "2026-11-07")  # pinned: later entries don't leak into an old episode

    def test_errors(self):
        with self.assertRaises(ReelsError) as e:
            progress.bars(self.project, [self.seg({"progress": True})], 150, 30, 150, [])
        self.assertIn("no FTP/weight logged", str(e.exception))
        progress.set_entry(self.project, ftp=335, weight=78, date="2026-11-07")
        with self.assertRaises(ReelsError):
            progress.bars(self.project, [self.seg({"progress": {"asOf": "2026-11-01"}})], 150, 30, 150, [])

    def test_render_props_layout_and_clash(self):
        progress.set_entry(self.project, ftp=335, weight=78, date="2026-11-07")
        self.tp.asset("clip", duration=10)
        plan = {"slug": "ep1", "hook": {"text": "road to 5", "position": "top"}, "autoSfx": False, "endCard": False,
                "segments": [{"asset": "clip", "in": 0, "out": 5, "progress": {"asOf": "2026-11-07", "at": 0}}]}
        built = build(self.project, plan)
        [b] = built["props"]["progress"]
        self.assertEqual(b["value"], 4.29)
        self.assertGreaterEqual(built["props"]["hook"]["offsetTop"], progress.BAR_TOP + progress.BAR_HEIGHT)
        self.assertEqual(top_offset({"startFrame": 0, "endFrame": 10}, [], [], [b]), progress.BAR_TOP + progress.BAR_HEIGHT + 24)
        plan["segments"][0]["stats"] = [{"label": "grade", "value": "12%", "at": 0}]
        with self.assertRaises(ReelsError) as e:
            build(self.project, plan)
        self.assertIn("overlaps stat pills", str(e.exception))


class StravaFirstTests(unittest.TestCase):
    """A GoPro clip linked to a Strava ride uses Strava's GPS; GoPro's only fills Strava gaps."""

    setUp, write, asset, gopro_track, import_ride, seg = (StravaTests.setUp, StravaTests.write, StravaTests.asset,
                                                          StravaTests.gopro_track, StravaTests.import_ride, StravaTests.seg)

    def gopro(self):
        self.asset("gp", "2026-11-04T07:02:00", name="GS010001.MP4", duration=30, telemetry=self.gopro_track(120, 30))

    def test_gopro_speed_and_route_refused_when_strava_covers(self):
        self.gopro()
        self.import_ride()
        sg = self.seg("gp", {"stats": [{"value": "gps"}]})
        with self.assertRaises(ReelsError) as e:
            _stats(self.project, [sg], 90, 30, [])
        self.assertIn('use "strava:speed"', str(e.exception))
        with self.assertRaises(ReelsError) as e:
            _routes(self.project, [self.seg("gp", {"route": True})], 90, 30, 90)
        self.assertIn('"route": "strava"', str(e.exception))
        # clip-level top/avg speed has no Strava equivalent yet, so GoPro is still allowed there
        self.assertEqual(len(_stats(self.project, [self.seg("gp", {"stats": [{"value": "gps:max"}]})], 90, 30, [])), 1)

    def test_gopro_allowed_where_strava_has_no_data(self):
        self.gopro()
        st = ride()
        for k in ("velocity_smooth", "location"):
            st.pop(k)
        self.import_ride(st)
        self.assertEqual(len(_stats(self.project, [self.seg("gp", {"stats": [{"value": "gps"}]})], 90, 30, [])), 1)
        self.assertEqual(len(_routes(self.project, [self.seg("gp", {"route": True})], 90, 30, 90)), 1)

    def test_unlinked_gopro_clip_still_uses_its_gps(self):
        self.asset("gp2", "2026-11-09T07:02:00", name="GS010002.MP4", duration=30, telemetry=self.gopro_track(0, 30))
        self.import_ride()
        self.assertEqual(len(_stats(self.project, [self.seg("gp2", {"stats": [{"value": "gps"}]})], 90, 30, [])), 1)


del StravaTests  # imported for its helpers only; its own tests run from test_strava

if __name__ == "__main__":
    unittest.main()
