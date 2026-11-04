from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reels import strava
from reels.config import Project
from reels.library import load_asset, save_asset
from reels.render import Seg, _routes, _stats
from reels.util import ReelsError, write_json

START = "2026-11-04T07:00:00"
LAT0, LON0 = 29.70, -95.40
V = 10.0  # m/s due north
DEG = V / 111_195  # degrees of latitude per second


def ride(n=600, lat0=LAT0, lon0=LON0):
    t = list(range(n))
    return {
        "time": t,
        "location": [[lat0 + i * DEG, lon0] for i in t],
        "distance": [i * V for i in t],
        "velocity_smooth": [V] * n,
        "watts": [100 + (i % 10) * 10 for i in t],  # 100..190 sawtooth
        "heart_rate": [150] * n,
        "temp": [20] * n,
    }


class StravaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.project.ensure_dirs()
        self.listing = {"activities": [{"id": "111", "name": "Morning Ride", "sport_type": "Ride", "start_local": START,
                                        "summary": {"distance": 6000.0, "moving_time": 590, "elapsed_time": 600, "elevation_gain": 10,
                                                    "avg_speed": 10.0, "max_speed": 12.0}}],
                        "value_units": {"distance": "meters"}}

    def write(self, name, data):
        p = self.root / name
        write_json(p, data)
        return p

    def asset(self, aid, created, *, name="IMG_1.mov", duration=10.0, telemetry=None, kind="video"):
        a = {"id": aid, "kind": kind, "originalName": name, "created": created, "probe": {"duration": duration},
             "status": "tagged", "tags": {"type": "group-ride"}, "used": []}
        if telemetry is not None:
            rel = f"library/telemetry/{aid}.json"
            write_json(self.root / rel, {"samples": telemetry})
            a["telemetry"] = rel
        save_asset(self.project, a)
        return a

    def gopro_track(self, ride_t0, duration, step=0.2):
        """GPS a GoPro would record from ride second ride_t0 on (5 Hz)."""
        out, t = [], 0.0
        while t <= duration:
            rt = ride_t0 + t
            out.append([round(t, 1), LAT0 + rt * DEG, LON0, 10.0, V, 3, 1.0])
            t += step
        return out

    def import_ride(self, streams=None):
        strava.import_file(self.project, self.write("list.json", self.listing), relink=False)
        # streams arrive wrapped the way MCP text content looks
        wrapped = [{"type": "text", "text": json.dumps(streams or ride())}]
        strava.import_file(self.project, self.write("streams.json", wrapped), "111")

    def test_phone_clip_linked_at_its_ride_time(self):
        self.asset("phone", "2026-11-04T07:02:00")
        self.asset("before", "2026-11-04T06:40:00")
        self.import_ride()
        lk = load_asset(self.project, "phone")["strava"]
        self.assertEqual((lk["activity"], lk["offset"], lk["clock"]), ("111", 120.0, "phone"))
        self.assertNotIn("strava", load_asset(self.project, "before"))

    def test_gopro_clock_fixed_from_its_gps_and_chapters_offset(self):
        # Camera clock runs 100 s fast: chapter 1 really starts at ride second 60, its clock says 160.
        self.asset("ch1", "2026-11-04T07:02:40", name="GX010042.MP4", duration=100.0, telemetry=self.gopro_track(60, 100))
        self.asset("ch2", "2026-11-04T07:02:40", name="GX020042.MP4", duration=50.0, telemetry=self.gopro_track(160, 50))
        self.asset("ch3", "2026-11-04T07:02:40", name="GX030042.MP4", duration=50.0)  # no GPS fix: borrows the shift
        self.import_ride()
        ch1, ch2, ch3 = (load_asset(self.project, x)["strava"] for x in ("ch1", "ch2", "ch3"))
        self.assertEqual(ch1["clock"], "gps")
        self.assertAlmostEqual(ch1["shift"], -100, delta=1)
        self.assertAlmostEqual(ch1["offset"], 60, delta=1)
        self.assertAlmostEqual(ch2["offset"], 160, delta=1)  # chapter 1's length added
        self.assertEqual(ch3["clock"], "gps-same-camera")
        self.assertAlmostEqual(ch3["offset"], 210, delta=1)

    def test_relink_drops_clip_outside_ride(self):
        self.asset("phone", "2026-11-04T07:02:00")
        self.import_ride()
        a = load_asset(self.project, "phone")
        a["created"] = "2026-11-04T09:00:00"
        save_asset(self.project, a)
        strava.link_assets(self.project)
        self.assertIsNone(load_asset(self.project, "phone")["strava"])

    def test_streams_need_activity_id_and_known_ride(self):
        p = self.write("s.json", ride())
        with self.assertRaises(ReelsError):
            strava.import_file(self.project, p)
        with self.assertRaises(ReelsError):
            strava.import_file(self.project, p, "999")

    def test_values_in_imperial(self):
        self.import_ride()
        act = strava.load_activity(self.project, "111")
        self.assertEqual(strava.ride_value(self.project, act, "strava:distance"), ("3.7", "mi"))
        self.assertEqual(strava.ride_value(self.project, act, "strava:moving-time"), ("9 min", ""))
        self.assertEqual(strava.ride_value(self.project, act, "strava:elevation"), ("33", "ft"))
        self.assertIn("performance", strava.ride_value(self.project, act, "strava:avg-power"))  # not imported -> error text
        self.assertEqual(strava.live_value(self.project, act, "strava:speed", 100), "22")
        self.assertEqual(strava.live_value(self.project, act, "strava:power", 103), "120")  # 3 s average: 110, 120, 130 W
        self.assertIsNone(strava.live_value(self.project, act, "strava:speed", 900))  # after the ride
        self.assertEqual(strava.moment_value(self.project, act, "strava:temp", 10, 20), ("68", "°F"))

    def test_stream_gap_has_no_live_value(self):
        s = ride()
        keep = [i for i in range(600) if not 200 < i < 260]  # auto-pause
        s = {k: [v[i] for i in keep] for k, v in s.items()}
        self.import_ride(s)
        act = strava.load_activity(self.project, "111")
        self.assertIsNone(strava.live_value(self.project, act, "strava:hr", 230))
        self.assertEqual(strava.live_value(self.project, act, "strava:hr", 199), "150")

    def test_route_trimmed_only_in_maine(self):
        act = {"streams": ride()}
        pts, trimmed = strava.route_points(act)
        self.assertFalse(trimmed)
        self.assertEqual(len(pts), 600)
        act = {"streams": ride(lat0=44.0, lon0=-69.5)}  # Maine
        pts, trimmed = strava.route_points(act)
        self.assertTrue(trimmed)
        self.assertAlmostEqual(pts[0][0], 100, delta=1)  # first 1 km = 100 s at 10 m/s
        self.assertAlmostEqual(pts[-1][0], 499, delta=1)

    def seg(self, asset_id, spec_extra=None, start=0, frames=90):
        spec = {"in": 0.0, "out": 3.0, "speed": 1.0, **(spec_extra or {})}
        return Seg(0, "video", load_asset(self.project, asset_id), spec, frames, start=start)

    def test_render_pills_and_route(self):
        self.asset("phone", "2026-11-04T07:02:00")
        self.asset("loose", "2026-11-05T07:02:00")
        self.import_ride()
        sg = self.seg("phone", {"stats": [{"value": "strava:speed"}, {"value": "strava:distance"}, {"value": "strava:mile"}],
                                "route": "strava"})
        stats = _stats(self.project, [sg], 90, 30, [])
        self.assertEqual([(s["label"], s["value"], s["unit"]) for s in stats],
                         [("speed", "22", "mph"), ("distance", "3.7", "mi"), ("mile", "0.7", "")])
        self.assertTrue(stats[0]["live"]["values"])
        routes = _routes(self.project, [sg], 90, 30, 90)
        self.assertEqual(len(routes), 1)
        self.assertEqual(len(routes[0]["dots"]), 90)
        with self.assertRaises(ReelsError) as e:
            _stats(self.project, [self.seg("loose", {"stats": [{"value": "strava:hr"}]})], 90, 30, [])
        self.assertIn("no Strava ride linked", str(e.exception))
        with self.assertRaises(ReelsError):
            _stats(self.project, [self.seg("phone", {"stats": [{"value": "strava:watts"}]})], 90, 30, [])


if __name__ == "__main__":
    unittest.main()
