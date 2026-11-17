"""Colour management and the colour grade (scripts/reels/grade.py)."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PIL import Image

from reels import grade
from reels.config import Project
from reels.render import Seg, stage_photo, stage_video


def frames(top: tuple[float, float, float], bottom: tuple[float, float, float], n: int = 2, h: int = 96, w: int = 54) -> np.ndarray:
    """Synthetic frames: `top` fills the top 40% (sky), `bottom` the rest, with a little texture."""
    x = np.empty((n, h, w, 3), np.float32)
    x[:, : int(h * 0.4)] = top
    x[:, int(h * 0.4):] = bottom
    rng = np.random.default_rng(1)
    x += rng.normal(0, 0.01, x.shape).astype(np.float32)
    return np.clip(x, 0, 1)


def chroma(rgb) -> float:
    rgb = np.asarray(rgb, np.float64)
    return float(rgb.max(-1).mean() - rgb.min(-1).mean()) if rgb.ndim > 1 else float(rgb.max() - rgb.min())


NEUTRAL = {"overcast": 0.0, "blueSky": 0.0, "wb": [1.0, 1.0, 1.0], "black": 0.0, "scale": 1.0, "gamma": 1.0}


class ColourManagementTests(unittest.TestCase):
    def colour(self, tags: dict, **probe) -> dict:
        with patch.object(grade, "_tags", return_value=tags):
            return grade.source_colour({"width": 1080, "height": 1920, **probe}, "clip.mov")

    def test_iphone_copies_keep_their_real_tags(self):
        c = self.colour({"color_space": "smpte170m", "color_primaries": "smpte432", "color_range": "pc"})
        self.assertEqual(c, {"matrix": "smpte170m", "primaries": "smpte432", "range": "pc"})

    def test_hdr_clips_are_already_bt709_after_tone_mapping(self):
        c = self.colour({"color_space": "bt2020nc", "color_primaries": "bt2020", "color_range": "tv"}, hdr=True)
        self.assertEqual(c, grade.BT709)

    def test_missing_tags_get_the_usual_guesses(self):
        self.assertEqual(self.colour({"pix_fmt": "yuvj420p"})["range"], "pc")
        self.assertEqual(self.colour({"pix_fmt": "yuv420p"}), grade.BT709)
        sd = self.colour({"width": 640, "height": 480})
        self.assertEqual(sd["matrix"], "smpte170m")

    def test_conversion_filters(self):
        p3 = {"matrix": "smpte170m", "primaries": "smpte432", "range": "pc"}
        self.assertEqual(grade.to_bt709(grade.BT709), "format=yuv420p")
        with patch.object(grade, "use_zscale", return_value=True):
            z = grade.to_bt709(p3)
        self.assertIn("pin=smpte432", z)
        self.assertIn("min=170m", z)
        self.assertIn("rin=full", z)
        self.assertIn("r=limited", z)
        with patch.object(grade, "use_zscale", return_value=False), patch.object(grade, "has_filter", return_value=True):
            cs = grade.to_bt709(p3)
        self.assertTrue(cs.startswith("colorspace="))
        self.assertIn("iprimaries=smpte432", cs)
        self.assertIn("irange=pc", cs)


class SettingsTests(unittest.TestCase):
    def test_clip_overrides_plan_overrides_project(self):
        s = grade.settings({"look": "house", "strength": 0.9}, {"strength": 0.7, "warmth": 0.2}, {"warmth": -0.1})
        self.assertEqual((s["look"], s["strength"], s["warmth"], s["rescue"]), ("house", 0.7, -0.1, "auto"))

    def test_default_is_no_look_but_an_explicit_grade_block_means_house(self):
        self.assertEqual(grade.settings(None)["look"], "none")
        self.assertFalse(grade.active(grade.settings(None)))
        self.assertEqual(grade.settings(None, {"strength": 0.8})["look"], "house")
        self.assertEqual(grade.settings(None, "none", {"warmth": 0.1})["look"], "house")
        self.assertEqual(grade.settings(None, {"look": "moody"}, {"warmth": 0.1})["look"], "moody")
        self.assertEqual(grade.settings({"look": "house"}, None)["look"], "house")

    def test_none_turns_the_grade_off(self):
        self.assertFalse(grade.active(grade.settings(None, "none")))
        self.assertFalse(grade.active(grade.settings(None, None, False)))
        self.assertTrue(grade.active(grade.settings(None, {"look": "moody"})))
        self.assertEqual(grade.rescue_amount({"overcast": 1.0}, grade.settings(None, "none")), 0.0)

    def test_the_editor_toggle_maps_to_auto_and_off(self):
        self.assertEqual(grade.settings(None, {"rescue": True})["rescue"], "auto")
        self.assertEqual(grade.settings(None, {"rescue": False})["rescue"], "off")
        self.assertEqual(grade.rescue_amount({"overcast": 0.8}, grade.settings(None, {"rescue": False})), 0.0)
        self.assertEqual(grade.rescue_amount({"overcast": 0.8}, grade.settings(None, {"rescue": True})), 0.8)
        self.assertEqual(grade.rescue_amount({"overcast": 0.8}, grade.settings(None, {"rescue": 0.3})), 0.3)

    def test_check(self):
        def errors(g):
            out: list[str] = []
            grade.check("plan", g, out)
            return out

        self.assertEqual(errors({"look": "house", "strength": 1.2, "rescue": True, "warmth": -0.5, "exposure": 0.3}), [])
        self.assertEqual(errors("none"), [])
        self.assertEqual(errors(None), [])
        self.assertTrue(errors({"look": "teal-orange"}))
        self.assertTrue(errors({"strength": 2}))
        self.assertTrue(errors({"strength": True}))
        self.assertTrue(errors({"rescue": 3}))
        self.assertTrue(errors({"saturation": 1.2}))
        self.assertTrue(errors("vivid"))


class MeasureTests(unittest.TestCase):
    def test_overcast_with_a_blue_cast_is_warmed_within_bounds(self):
        p = grade.measure(frames(top=(0.86, 0.87, 0.90), bottom=(0.42, 0.45, 0.52)))
        self.assertGreater(p["overcast"], 0.9)
        r, g, b = p["wb"]
        self.assertGreater(r, 1.0)
        self.assertLess(b, 1.0)
        for v in p["wb"]:
            self.assertTrue(0.9 <= v <= 1.1)

    def test_blue_sky_keeps_blue_shadows(self):
        p = grade.measure(frames(top=(0.25, 0.45, 0.85), bottom=(0.42, 0.45, 0.52)))
        self.assertLess(p["overcast"], 0.1)
        self.assertGreater(p["blueSky"], 0.5)
        for v in p["wb"]:
            self.assertAlmostEqual(v, 1.0, delta=0.03)

    def test_golden_hour_keeps_most_of_its_glow(self):
        p = grade.measure(frames(top=(0.95, 0.80, 0.55), bottom=(0.60, 0.48, 0.32)))
        self.assertGreater(p["wb"][0], 0.95)  # red is pulled down only a little

    def test_levels_and_midtones_are_bounded(self):
        for top, bottom in [((0.1, 0.1, 0.1), (0.05, 0.05, 0.05)), ((0.98, 0.98, 0.98), (0.9, 0.9, 0.9)), ((0.5, 0.3, 0.2), (0.2, 0.4, 0.1))]:
            p = grade.measure(frames(top, bottom))
            self.assertTrue(1.0 <= p["scale"] <= 1.15)
            self.assertTrue(0.95 <= p["gamma"] <= 1.0625)
            self.assertTrue(0 <= p["black"] <= 0.05)

    def test_a_bright_foggy_shot_is_not_crushed(self):
        x = frames(top=(0.95, 0.95, 0.96), bottom=(0.88, 0.88, 0.9))
        p = grade.measure(x)
        out = grade.grade_rgb(x, p, grade.settings(None))
        self.assertGreater(float(grade._luma(out).mean()), 0.75)


class LookTests(unittest.TestCase):
    house = grade.settings(None, {"look": "house"})

    def test_strength_zero_is_identity(self):
        x = np.random.default_rng(2).random((50, 1, 3)).astype(np.float32)
        out = grade.grade_rgb(x, NEUTRAL, {**self.house, "strength": 0.0})
        np.testing.assert_allclose(out, x, atol=1e-6)

    def test_clouds_stay_white(self):
        for look in ("house", "moody"):
            out = grade.grade_rgb(np.array([[[0.93, 0.93, 0.93]]], np.float32), NEUTRAL, {**self.house, "look": look})
            self.assertLess(chroma(out[0, 0]), 0.012, look)

    def test_saturation_stays_mild(self):
        # the user found the first mock-up "way too saturated": the house look may add only a little colour
        for rgb in [(0.2, 0.35, 0.8), (0.75, 0.2, 0.15), (0.35, 0.5, 0.25), (0.55, 0.5, 0.45)]:
            x = np.array([[rgb]], np.float32)
            out = grade.grade_rgb(x, NEUTRAL, self.house)
            self.assertLess(chroma(out[0, 0]), chroma(x[0, 0]) * 1.25 + 0.01, rgb)

    def test_grey_day_extra_colour_is_small(self):
        x = np.array([[(0.45, 0.42, 0.40)]], np.float32)
        calm = grade.grade_rgb(x, {**NEUTRAL, "overcast": 0.0}, self.house)
        grey = grade.grade_rgb(x, {**NEUTRAL, "overcast": 1.0}, self.house)
        self.assertLess(chroma(grey[0, 0]), chroma(calm[0, 0]) * 1.1 + 0.005)

    def test_skin_keeps_its_hue(self):
        x = np.array([[(0.80, 0.60, 0.50)]], np.float32)
        out = grade.grade_rgb(x, NEUTRAL, self.house)
        self.assertAlmostEqual(float(grade._hue(out)[0, 0]), float(grade._hue(x)[0, 0]), delta=4)

    def test_tones_stay_in_order(self):
        ramp = np.repeat(np.linspace(0, 1, 64, dtype=np.float32)[:, None, None], 3, axis=2)
        for look in ("house", "moody"):
            y = grade._luma(grade.grade_rgb(ramp, NEUTRAL, {**self.house, "look": look}))[:, 0]
            self.assertTrue(np.all(np.diff(y) >= -1e-4), look)

    def test_warmth_and_exposure_go_the_right_way(self):
        x = np.array([[(0.5, 0.5, 0.5)]], np.float32)
        warm = grade.grade_rgb(x, NEUTRAL, {**self.house, "warmth": 0.8})[0, 0]
        cool = grade.grade_rgb(x, NEUTRAL, {**self.house, "warmth": -0.8})[0, 0]
        self.assertGreater(warm[0] - warm[2], cool[0] - cool[2])
        bright = grade.grade_rgb(x, NEUTRAL, {**self.house, "exposure": 0.5})
        dark = grade.grade_rgb(x, NEUTRAL, {**self.house, "exposure": -0.5})
        self.assertGreater(grade._luma(bright)[0, 0], grade._luma(dark)[0, 0])

    def test_lut_file_is_a_cube_with_red_changing_fastest(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.cube"
            grade.write_lut(path, NEUTRAL, self.house, n=5)
            lines = path.read_text().splitlines()
        self.assertEqual(lines[1], "LUT_3D_SIZE 5")
        rows = [list(map(float, ln.split())) for ln in lines[2:]]
        self.assertEqual(len(rows), 125)
        expect = grade.grade_rgb(np.array([[[0.0, 0, 0]], [[0.25, 0, 0]], [[0, 0.25, 0]]], np.float32), NEUTRAL, self.house)
        np.testing.assert_allclose(rows[0], expect[0, 0], atol=1e-5)
        np.testing.assert_allclose(rows[1], expect[1, 0], atol=1e-5)  # second entry: red stepped
        np.testing.assert_allclose(rows[5], expect[2, 0], atol=1e-5)  # sixth: green stepped


class RescueTests(unittest.TestCase):
    def test_rescue_only_touches_luma(self):
        # blend's chroma planes must stay "normal" at full opacity; opacity 0 smeared blurred colour round riders
        g = grade.rescue_graph("in", "out", 1.0, 1.0, 1080, 1920, 30)
        self.assertNotIn("c1_opacity", g)
        self.assertNotIn("c2_opacity", g)
        self.assertNotIn("all_mode", g)
        self.assertTrue(g.endswith("[out]"))

    def test_blur_size_stays_even(self):
        for w, h in [(1080, 1920), (1512, 2688), (1366, 2430)]:
            g = grade.rescue_graph("in", "out", 0.5, 1.0, w, h, 30)
            dims = g.split("[gr_b]scale=")[1].split(":flags")[0]
            bw, bh = map(int, dims.split(":"))
            self.assertEqual((bw % 2, bh % 2), (0, 0), (w, h))

    def test_low_resolution_sources_get_less(self):
        self.assertAlmostEqual(grade.source_quality({"width": 720, "height": 1280}), 0.667, places=2)
        self.assertEqual(grade.source_quality({"width": 2160, "height": 3840}), 1.0)
        self.assertEqual(grade.source_quality({"width": 176, "height": 144}), 0.5)


class PlanTests(unittest.TestCase):
    def setUp(self):
        from helpers import TempProject

        self.t = TempProject(self)
        self.t.asset("clip", duration=12)
        self.t.asset("broll", duration=12)

    def build(self, **extra):
        from reels.render import build, load_plan

        seg = {"asset": "clip", "in": 0, "out": 4, **extra.pop("seg", {})}
        path = self.t.plan("2026-11-15-g", [seg], **extra)
        return build(self.t.project, load_plan(self.t.project, path))

    def test_bad_grades_are_plan_errors(self):
        from reels.util import ReelsError

        for kwargs, needle in [({"grade": {"look": "teal"}}, "plan: grade.look"),
                               ({"seg": {"grade": {"warmth": 3}}}, "segment 1: grade.warmth"),
                               ({"seg": {"cutaways": [{"asset": "broll", "in": 0, "out": 1, "grade": "vivid"}]}},
                                "segment 1 cutaway 1: grade must be")]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ReelsError) as caught:
                self.build(**kwargs)
            self.assertIn(needle, str(caught.exception))

    def test_plan_grade_reaches_staging_and_lineage(self):
        from reels.lineage import from_built

        built = self.build(grade={"look": "moody", "strength": 0.8})
        self.assertEqual(built["grade"], {"look": "moody", "strength": 0.8})
        self.assertNotIn("grade", from_built("2026-11-15-g", built))
        built["graded"] = [{"clip": "seg0", "asset": "clip", "look": "moody", "overcast": 0.4}]
        self.assertEqual(from_built("2026-11-15-g", built)["grade"], {"version": grade.VERSION, "clips": built["graded"]})


def ffprobe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_frames", "-show_entries",
                          "stream=color_space,color_primaries,color_range,nb_read_frames", "-of", "json", str(path)],
                         capture_output=True, text=True, check=True).stdout  # fmt: skip
    return json.loads(out)["streams"][0]


def decode_rgb(path: Path, matrix: str, rng: str, size: tuple[int, int] = (64, 112)) -> np.ndarray:
    w, h = size
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-frames:v", "1", "-vf",
                          f"scale={w}:{h}:in_color_matrix={matrix}:in_range={rng}:out_range=pc,format=rgb24",
                          "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout  # fmt: skip
    return np.frombuffer(raw, np.uint8).reshape(h, w, 3).astype(np.float32) / 255


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class StagingTests(unittest.TestCase):
    """Stages real (tiny) clips: an iPhone-style Display P3 / BT.601 / full-range copy."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name)
        self.project = Project(self.dir)
        self.project.ensure_dirs()
        self.project.config["render"].update(width=216, height=384)
        self.src = self.dir / "p3.mov"
        # a skin-like colour, encoded full range with a BT.601 matrix and tagged Display P3 (setparams: the
        # muxer otherwise drops primaries the frames don't carry)
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=0x99735A:size=216x384:rate=30:duration=1",
             "-vf", "scale=out_color_matrix=bt601:out_range=pc,format=yuv420p,"
                    "setparams=colorspace=smpte170m:color_primaries=smpte432:color_trc=bt709:range=pc",
             "-c:v", "libx264", "-qp", "4", str(self.src)],
            check=True,
        )  # fmt: skip
        tags = ffprobe(self.src)
        if tags.get("color_primaries") != "smpte432":
            self.skipTest("this ffmpeg can't write Display P3 tags")

    def seg(self) -> Seg:
        asset = {"id": "p3", "kind": "video", "media": str(self.src), "transcript": None,
                 "probe": {"hasAudio": False, "width": 216, "height": 384, "duration": 1.0, "pixFmt": "yuvj420p"}}
        spec = {"in": 0.0, "out": 1.0, "speed": 1.0, "frame": "crop", "zoom": 1.0, "focusX": 0.5, "focusY": 0.5}
        return Seg(index=0, kind="video", asset=asset, spec=spec, frames=30)

    def converters(self):
        out = []
        if grade.has_filter("colorspace"):
            out.append(("colorspace", False))
        if grade.use_zscale():
            out.append(("zscale", True))
        return out

    def test_p3_copies_keep_their_colour_and_come_out_tagged_bt709(self):
        naive = decode_rgb(self.src, "bt601", "pc")  # what the old staging showed: P3 values read as BT.709
        converters = self.converters()
        if not converters:
            self.skipTest("this ffmpeg has neither zscale nor colorspace")
        for name, z in converters:
            with self.subTest(converter=name), patch.object(grade, "use_zscale", return_value=z):
                out = self.dir / f"fix-{name}.mp4"
                measured = stage_video(self.project, self.seg(), out, 30, audio=False, grade=grade.settings(None, "none"))
                self.assertIsNone(measured)
                tags = ffprobe(out)
                self.assertEqual((tags["color_space"], tags["color_primaries"], tags["color_range"]), ("bt709", "bt709", "tv"))
                fixed = decode_rgb(out, "bt709", "tv")
                # P3 (0.60, 0.45, 0.35) is about (0.62, 0.44, 0.33) in BT.709: more colour, not less
                self.assertGreater(chroma(fixed.reshape(-1, 3)), chroma(naive.reshape(-1, 3)) * 1.1)
                self.assertGreater(float(fixed[..., 0].mean()), float(naive[..., 0].mean()) + 0.01)

    def test_graded_staging_measures_cleans_up_and_keeps_the_length(self):
        out = self.dir / "seg0.mp4"
        measured = stage_video(self.project, self.seg(), out, 30, audio=False, grade=grade.settings(None, {"look": "house"}))
        self.assertIsNotNone(measured)
        for key in ("overcast", "wb", "black", "scale", "gamma", "version", "look", "rescue"):
            self.assertIn(key, measured)
        self.assertEqual(measured["look"], "house")
        tags = ffprobe(out)
        self.assertEqual((tags["color_space"], tags["color_range"]), ("bt709", "tv"))
        self.assertGreaterEqual(int(tags["nb_read_frames"]), 30)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir() if p.name.startswith("seg0")), ["seg0.mp4"])

    def test_rescue_path_runs(self):
        out = self.dir / "seg0.mp4"
        measured = stage_video(self.project, self.seg(), out, 30, audio=False, grade=grade.settings(None, {"look": "house", "rescue": 1.0}))
        self.assertEqual(measured["rescue"], 1.0)
        self.assertTrue(out.is_file())

    def test_photos_are_graded_in_place(self):
        photo = self.dir / "photo.jpg"
        Image.new("RGB", (300, 400), (112, 120, 134)).save(photo)  # grey with a blue cast
        asset = {"id": "ph", "kind": "photo", "media": str(photo), "transcript": None, "probe": {"width": 300, "height": 400}}
        spec = {"fit": "blurfill", "focusX": 0.5, "focusY": 0.5, "duration": 2.0}
        out = self.dir / "seg1.jpg"
        measured = stage_photo(self.project, Seg(index=1, kind="photo", asset=asset, spec=spec, frames=60), out, grade=grade.settings(None, {"look": "house"}))
        self.assertEqual(measured["look"], "house")
        self.assertGreater(measured["wb"][0], measured["wb"][2])  # the blue cast is measured and warmed
        self.assertTrue(out.with_name("seg1.bg.jpg").is_file())
        with Image.open(out) as im:
            graded = np.asarray(im.convert("RGB"), np.float32)
        before = np.asarray(Image.open(photo).convert("RGB").resize(graded.shape[1::-1]), np.float32)
        self.assertFalse(np.allclose(graded, before, atol=2))
        self.assertAlmostEqual(float(graded.mean()), float(before.mean()), delta=before.mean() * 0.25)
        plain = self.dir / "seg2.jpg"
        self.assertIsNone(stage_photo(self.project, Seg(index=2, kind="photo", asset=asset, spec={**spec, "fit": "cover"}, frames=60), plain))


if __name__ == "__main__":
    unittest.main()
