"""Real low-resolution render proof: layers, gaps and linked audio, not UI metadata."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import TempProject
from reels.config import Project
from reels.editor.transactions import DocumentStore
from reels.overlay import why_not


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ffmpeg") and (ROOT / "remotion/node_modules/.bin/remotion").exists(),
                     "installed ffmpeg and Remotion required")
class PositionedRenderTests(unittest.TestCase):
    def _color_fixture(self):
        from reels.editor.preview import _browser_executable
        from reels.render import build, stage
        fixture = TempProject(self)
        (fixture.root / "config.json").write_text(json.dumps({
            "render": {"width": 96, "height": 96, "fps": 10, "transitionFrames": 4,
                       "grade": {"look": "none", "rescue": "off"}, "concurrency": 1},
            "brand": {"endCard": False},
        }))
        project = Project(fixture.root, code_root=ROOT)
        browser = _browser_executable(SimpleNamespace(project=project, code_root=ROOT))
        if browser is None:
            self.skipTest("no installed browser; the test never downloads one")
        project.config["render"]["browserExecutable"] = str(browser)
        for name, color in (("red", "red"), ("blue", "blue")):
            path = project.media_dir / f"{name}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                f"color=c={color}:s=96x96:r=10:d=0.8", "-f", "lavfi", "-i",
                "anullsrc=r=48000:cl=mono", "-shortest", "-c:v", "libx264",
                "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True, capture_output=True, timeout=30)
            fixture.asset(name, duration=.8, width=96, height=96, media=path)
        plan_path = fixture.plan("render-fades", [
            {"asset": "red", "in": 0, "out": .8},
            {"asset": "blue", "in": 0, "out": .8, "transition": "fade"},
        ])
        built = build(project, json.loads(plan_path.read_text()))
        public = fixture.root / "render-fades/public"
        public.mkdir(parents=True)
        stage(project, built, public)
        return fixture, project, built, public, plan_path

    def _frames(self, fixture, project, public, props, name):
        import numpy as np
        from reels.render import run_remotion
        props_path = public / f"{name}.json"
        props_path.write_text(json.dumps(props))
        output = fixture.root / f"render-fades/{name}.mp4"
        run_remotion(project, props_path, public, output, fixture.root / f"render-fades/{name}.log")
        pixels = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-an",
            "-pix_fmt", "rgb24", "-f", "rawvideo", "-"],
            check=True, capture_output=True, timeout=30).stdout
        return np.frombuffer(pixels, dtype=np.uint8).reshape(
            -1, props["height"], props["width"], 3).astype(float)

    def test_positioned_fade_matches_legacy_picture_at_each_frame(self):
        import numpy as np
        fixture, project, built, public, _ = self._color_fixture()
        legacy = built["props"]
        positioned = copy.deepcopy(legacy)
        positioned["timeline"] = {"version": 1, "fps": 10, "durationFrames": legacy["totalFrames"]}
        for seg, source in zip(positioned["segments"], built["segs"]):
            seg.update(track="V1", startFrame=source.start)
        self.assertEqual(positioned["segments"][1]["transitionInFrames"], 4)
        reference = self._frames(fixture, project, public, legacy, "legacy")
        actual = self._frames(fixture, project, public, positioned, "positioned")
        self.assertEqual(len(actual), len(reference))
        np.testing.assert_allclose(actual[:, 48, 48, :], reference[:, 48, 48, :], atol=8)

    def test_simultaneous_fading_layers_composite_owned_exhibits_with_picture(self):
        from reels.render import build, stage
        fixture, project, _, public, plan_path = self._color_fixture()
        plan = json.loads(plan_path.read_text())
        red, blue = plan["segments"]
        red.update(timeline={"track": "V1", "startFrame": 0, "durationFrames": 8, "transitionInFrames": 0},
                   text=[{"text": "lower", "at": 0, "for": .8}])
        blue.update(timeline={"track": "V2", "startFrame": 0, "durationFrames": 8, "transitionInFrames": 4},
                    text=[{"text": "upper", "at": 0, "for": .8}])
        plan.update(timeline={"version": 1, "fps": 10, "durationFrames": 9}, segments=[blue, red])
        built = build(project, plan)
        self.assertEqual({beat["text"]: beat["ownerSegmentIndex"] for beat in built["props"]["beats"]},
                         {"upper": 0, "lower": 1})
        stage(project, built, public)
        for name, color in (("green", "#00ff00"), ("yellow", "#ffff00")):
            (public / f"{name}.svg").write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
                f'<rect width="20" height="20" fill="{color}"/></svg>')
        props = copy.deepcopy(built["props"])
        props["beats"] = []  # The coloured exhibits provide unambiguous per-pixel ownership checks.
        common = {"startFrame": 0, "endFrame": 8, "kind": "image", "y": 48, "width": 20,
                  "height": 20, "tilt": 0, "style": "cutout", "padding": 0, "shadow": False,
                  "depth": "front", "enter": "none", "exit": "none"}
        props["exhibits"] = [
            {**common, "x": 48, "src": "green.svg", "ownerSegmentIndex": 1},
            {**common, "x": 20, "src": "yellow.svg", "ownerSegmentIndex": 0},
        ]
        frames = self._frames(fixture, project, public, props, "owned-overlays")
        lower = frames[:, 48, 48, :]
        upper = frames[:, 48, 20, :]
        self.assertGreater(lower[0, 1], 200, "transparent upper clip must reveal lower owner's exhibit")
        self.assertGreater(lower[2, 2], 80, "upper picture must cover lower exhibit during its fade")
        self.assertGreater(lower[2, 1], 80, "lower exhibit remains partly visible through the fade")
        self.assertGreater(lower[4, 2], 200, "opaque upper clip must cover lower exhibit")
        self.assertLess(upper[0, 1], 30, "upper owner's exhibit must share its initially transparent fade")
        self.assertGreater(upper[2, 1], 80)
        self.assertLess(upper[2, 1], 200)
        self.assertGreater(upper[4, 1], 200)

    def test_global_behind_uses_visible_subjects_and_keeps_foreground_provenance(self):
        from PIL import Image, ImageDraw
        from reels.exhibits import foregrounds
        from reels.render import build, stage
        fixture, project, _, public, plan_path = self._color_fixture()
        plan = json.loads(plan_path.read_text())
        red, blue = plan["segments"]
        red["timeline"] = {"track": "V1", "startFrame": 0, "durationFrames": 8, "transitionInFrames": 0}
        blue.update(out=.6, timeline={"track": "V2", "startFrame": 2, "durationFrames": 6, "transitionInFrames": 4})
        plan["timeline"] = {"version": 1, "fps": 10, "durationFrames": 9}
        built = build(project, plan)
        stage(project, built, public)
        for name, color in (("white", "#ffffff"), ("magenta", "#ff00ff"), ("cyan", "#00ffff")):
            (public / f"{name}.svg").write_text(
                f'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
                f'<rect width="20" height="20" fill="{color}"/></svg>')
        for index, box, color in ((0, (42, 42, 54, 54), (255, 255, 0, 255)),
                                  (1, (48, 42, 72, 54), (0, 255, 0, 255))):
            image = Image.new("RGBA", (96, 96))
            ImageDraw.Draw(image).rectangle(box, fill=color)
            image.save(public / f"alpha{index}.png")
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "10",
                "-i", str(public / f"alpha{index}.png"), "-frames:v", "8", "-c:v", "libvpx-vp9",
                "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", str(public / f"fg{index}.webm")],
                check=True, capture_output=True, timeout=30)
        common = {"kind": "image", "y": 48, "width": 60, "height": 60, "tilt": 0,
                  "style": "cutout", "padding": 0, "shadow": False, "enter": "none", "exit": "none"}
        owned_behind = {**common, "src": "white.svg", "x": 48, "startFrame": 0, "endFrame": 2,
                        "depth": "behind", "ownerSegmentIndex": 0}
        global_behind = {**common, "src": "white.svg", "x": 48, "startFrame": 2, "endFrame": 9, "depth": "behind"}
        global_front = {**common, "src": "magenta.svg", "x": 34, "width": 10, "height": 10,
                        "startFrame": 0, "endFrame": 9, "depth": "front"}
        owned_front = {**common, "src": "cyan.svg", "x": 12, "y": 12, "width": 12, "height": 12,
                       "startFrame": 0, "endFrame": 8, "depth": "front", "ownerSegmentIndex": 0}
        fg = foregrounds([{"props": owned_behind}, {"props": global_behind}], built["segs"], 10)
        self.assertEqual([(f["seg"], f["ownedBehindRanges"], f["globalBehindRanges"]) for f in fg],
                         [(0, [(0, 2)], [(2, 8)]), (1, [], [(2, 8)])])
        props = copy.deepcopy(built["props"])
        props.update(exhibits=[owned_behind, global_behind, global_front, owned_front], foregrounds=fg)
        frames = self._frames(fixture, project, public, props, "global-behind")
        self.assertLess(frames[0, 48, 44, 2], 70, "owned behind exhibit must stay behind its subject")
        self.assertLess(frames[2, 48, 44, 2], 70, "global exhibit must stay behind the lower subject")
        self.assertGreater(frames[3, 48, 44, 2], 35, "fading upper footage must partially cover the lower subject")
        self.assertLess(frames[3, 48, 51, 2], 35, "overlapping subjects must not double-dim during the fade")
        self.assertGreater(frames[6, 48, 44, 2], 200, "opaque upper footage must fully cover the lower subject")
        self.assertGreater(frames[6, 48, 66, 1], frames[6, 48, 66, 0] + 100, "upper subject stays in front")
        self.assertGreater(frames[6, 48, 34, 0], frames[6, 48, 34, 1] + 100, "global front stays on top")
        self.assertGreater(frames[0, 12, 12, 1], frames[0, 12, 12, 0] + 100, "owned front stays with lower clip")
        self.assertGreater(frames[6, 12, 12, 2], frames[6, 12, 12, 1] + 100, "upper footage covers lower front")
        self.assertGreater(frames[8, 48, 48, 2], 200, "global exhibit remains in a gap without a subject")
        self.assertLess(max(frames[8, 12, 12]), 25)

    def test_global_behind_can_start_in_a_gap_before_footage(self):
        from unittest.mock import patch
        from reels.render import build
        fixture, project, _, _, plan_path = self._color_fixture()
        plan = json.loads(plan_path.read_text())
        red = plan["segments"][0]
        red.update(out=.6, timeline={"track": "V1", "startFrame": 2, "durationFrames": 6, "transitionInFrames": 0})
        plan.update(segments=[red], timeline={"version": 1, "fps": 10, "durationFrames": 9},
                    exhibits=[{"chart": {"points": [[0, 1], [1, 2]]}, "at": 0, "for": .9, "depth": "behind"}])
        with patch("reels.exhibits.vision.available", return_value=None):
            built = build(project, plan)
        self.assertEqual(built["exhibits"][0]["host"], -1)
        self.assertEqual(built["props"]["foregrounds"][0]["globalBehindRanges"], [(2, 8)])
        self.assertEqual(built["props"]["foregrounds"][0]["ownedBehindRanges"], [])

    def test_positioned_short_owned_overlays_keep_owner_and_last_frame(self):
        from reels.render import _trim, build
        fixture, project, _, _, plan_path = self._color_fixture()
        plan = json.loads(plan_path.read_text())
        for index, segment in enumerate(plan["segments"]):
            fixture.asset(segment["asset"], duration=.8, width=96, height=96,
                          media=project.media_dir / f'{segment["asset"]}.mp4',
                          words=[{"w": f"last{index}", "s": .7, "e": .8, "p": .99}])
            segment.update(
                timeline={"track": f"V{index + 1}", "startFrame": 0, "durationFrames": 8,
                          "transitionInFrames": 0},
                captions="transcript",
                text=[{"text": f"full {index}", "at": 0, "for": .8},
                      {"text": f"last {index}", "at": .7, "for": .1}],
                stats=[{"label": f"stat {index}", "value": "1", "at": .7, "for": .1}],
                exhibits=[{"chart": {"points": [[0, 1], [1, 2]]}, "at": .7, "for": .1,
                           "enter": "none", "exit": "none", "safeArea": False}],
            )
        plan["timeline"] = {"version": 1, "fps": 10, "durationFrames": 8}
        props = build(project, plan)["props"]
        self.assertEqual([(b["text"], b["ownerSegmentIndex"], b["startFrame"], b["endFrame"])
                          for b in props["beats"]],
                         [("full 0", 0, 0, 8), ("last 0", 0, 7, 8),
                          ("full 1", 1, 0, 8), ("last 1", 1, 7, 8)])
        self.assertEqual([(s["ownerSegmentIndex"], s["startFrame"], s["endFrame"])
                          for s in props["stats"]], [(0, 7, 8), (1, 7, 8)])
        self.assertEqual([(e["ownerSegmentIndex"], e["startFrame"], e["endFrame"])
                          for e in props["exhibits"]], [(0, 7, 8), (1, 7, 8)])
        self.assertEqual([(p["ownerSegmentIndex"], p["startFrame"], p["endFrame"])
                          for p in props["captions"]], [(0, 7, 8), (1, 7, 8)])
        item = {"startFrame": 0, "endFrame": 8, "ownerSegmentIndex": 0}
        self.assertEqual(_trim([item], 8), [], "legacy trim policy must not change")
        self.assertEqual(_trim([item], 8, keep_owned=True), [item])

    def test_short_owned_animation_frames_visible_and_legacy_unchanged(self):
        import numpy as np
        fixture, project, built, public, _ = self._color_fixture()
        (public / "green.svg").write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
            '<rect width="20" height="20" fill="#00ff00"/></svg>')
        base = copy.deepcopy(built["props"])
        red = base["segments"][0]
        red.update(startFrame=0, track="V1", transitionInFrames=0)
        base.update(width=360, height=720, segments=[red], totalFrames=8,
                    timeline={"version": 1, "fps": 10, "durationFrames": 8},
                    beats=[], stats=[], captions=[])

        def overlays(start, end, enter):
            common = {"startFrame": start, "endFrame": end, "ownerSegmentIndex": 0}
            return (
                {**common, "text": "hello", "position": "center", "style": "light"},
                {**common, "label": "power", "value": "123"},
                {**common, "kind": "image", "src": "green.svg", "x": 180, "y": 550,
                 "width": 80, "height": 80, "tilt": 0, "style": "cutout", "padding": 0,
                 "shadow": False, "depth": "front", "enter": enter, "exit": "fade"},
            )

        owned = copy.deepcopy(base)
        for start, end, enter in ((2, 3, "pop"), (3, 4, "fade"), (5, 8, "slide")):
            beat, stat, exhibit = overlays(start, end, enter)
            owned["beats"].append(beat)
            owned["stats"].append(stat)
            owned.setdefault("exhibits", []).append(exhibit)
        legacy = copy.deepcopy(owned)
        legacy.pop("timeline")
        for key in ("beats", "stats", "exhibits"):
            for item in legacy[key]:
                item.pop("ownerSegmentIndex")
        reference = self._frames(fixture, project, public, base, "short-base")
        actual = self._frames(fixture, project, public, owned, "short-owned")
        old = self._frames(fixture, project, public, legacy, "short-legacy")
        regions = ((slice(320, 420), slice(0, 360)),
                   (slice(230, 320), slice(55, 350)),
                   (slice(510, 590), slice(140, 220)))
        for frame in (2, 3, 5, 6, 7):
            for y, x in regions:
                self.assertGreater(np.abs(actual[frame, y, x] - reference[frame, y, x]).sum(), 5000,
                                   f"owned overlay invisible at frame {frame}, region {y}")
        for frame in (2, 3, 5):
            for y, x in regions:
                self.assertLess(np.abs(old[frame, y, x] - reference[frame, y, x]).mean(), 2,
                                "legacy entrance must remain unchanged")

        full = copy.deepcopy(base)
        full["beats"], full["stats"], full["exhibits"] = map(list, zip(overlays(0, 8, "pop")))
        full_legacy = copy.deepcopy(full)
        full_legacy.pop("timeline")
        for key in ("beats", "stats", "exhibits"):
            full_legacy[key][0].pop("ownerSegmentIndex")
        full_frames = self._frames(fixture, project, public, full, "full-owned")
        old_full_frames = self._frames(fixture, project, public, full_legacy, "full-legacy")
        for y, x in regions:
            self.assertLess(np.abs(full_frames[:, y, x] - old_full_frames[:, y, x]).mean(), 2,
                            "full-length owned animation must match legacy")

    def test_owned_exhibit_does_not_reposition_another_clip_text(self):
        from reels.exhibits import make_room
        obstacle = {"props": {"startFrame": 0, "endFrame": 30, "x": 500, "y": 1280,
                              "width": 200, "height": 200, "ownerSegmentIndex": 1}}
        lower = {"text": "lower", "position": "lower", "startFrame": 0, "endFrame": 30,
                 "ownerSegmentIndex": 0}
        upper = {**lower, "text": "upper", "ownerSegmentIndex": 1}
        make_room([obstacle], None, [lower, upper], [], [], 30, positioned=True)
        self.assertEqual(lower["position"], "lower")
        self.assertNotEqual(upper["position"], "lower")

    def test_global_exhibit_renders_in_gap_and_chart_uses_top_track(self):
        from PIL import Image
        from reels.render import build, stage
        fixture, project, _, public, plan_path = self._color_fixture()
        project.exhibits_dir.mkdir(exist_ok=True)
        Image.new("RGB", (20, 20), (0, 255, 0)).save(project.exhibits_dir / "green.png")
        plan = json.loads(plan_path.read_text())
        red, blue = plan["segments"]
        red["timeline"] = {"track": "V1", "startFrame": 0, "durationFrames": 8, "transitionInFrames": 0}
        blue["timeline"] = {"track": "V10", "startFrame": 0, "durationFrames": 8, "transitionInFrames": 0}
        plan.update(timeline={"version": 1, "fps": 10, "durationFrames": 10},
                    segments=[red, blue], exhibits=[
                        {"chart": {"points": [[0, 1], [1, 2]]}, "at": 0, "for": .1},
                        {"image": "green.png", "at": .9, "for": .1, "x": .5, "y": .5, "width": .4,
                         "enter": "none", "exit": "none", "safeArea": False, "style": "cutout"},
                    ])
        built = build(project, plan)
        self.assertEqual(built["exhibits"][0]["host"], 1, "global chart uses topmost footage, not array order")
        blue["timeline"]["transitionInFrames"] = 4
        self.assertEqual(build(project, plan)["exhibits"][0]["host"], 0,
                         "a fully transparent fade-in must not select the upper source")
        blue["timeline"]["transitionInFrames"] = 0
        self.assertEqual(built["exhibits"][1]["host"], -1)
        self.assertNotIn("ownerSegmentIndex", built["props"]["exhibits"][1])
        stage(project, built, public)
        frames = self._frames(fixture, project, public, built["props"], "global-gap-exhibit")
        self.assertLess(max(frames[8, 48, 48]), 25)
        self.assertGreater(frames[9, 48, 48, 1], 200)
        from reels.util import ReelsError
        plan["exhibits"] = [{"chart": {"data": "strava:power"}, "at": .9, "for": .1}]
        with self.assertRaisesRegex(ReelsError, "no Strava ride linked"):
            build(project, plan)

    def test_rendered_layers_source_gap_and_independent_linked_audio(self):
        import numpy as np
        from reels.editor.preview import _browser_executable
        from reels.render import build, stage, run_remotion
        fixture = TempProject(self)
        config = {"render": {"width": 96, "height": 96, "fps": 10, "grade": {"look": "none", "rescue": "off"}, "concurrency": 1},
                  "brand": {"endCard": False}}
        (fixture.root / "config.json").write_text(json.dumps(config))
        project = Project(fixture.root, code_root=ROOT)
        browser = _browser_executable(SimpleNamespace(project=project, code_root=ROOT))
        if browser is None:
            self.skipTest("no installed browser; the test never downloads one")
        project.config["render"]["browserExecutable"] = str(browser)
        originals = {}
        for name, color, frequency in (("red", "red", 440), ("blue", "blue", 880)):
            path = project.media_dir / f"{name}.mp4"
            subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                f"color=c={color}:s=96x96:r=10:d=0.8", "-f", "lavfi", "-i",
                f"sine=frequency={frequency}:sample_rate=48000:duration=0.8",
                "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)],
                check=True, capture_output=True, timeout=30)
            fixture.asset(name, duration=.8, width=96, height=96, media=path)
            originals[name] = path.read_bytes()
        plan_path = fixture.plan("render-placement", [
            {"asset": "red", "in": 0, "out": .4, "volume": 1},
            {"asset": "blue", "in": 0, "out": .4, "volume": 1},
        ])
        store = DocumentStore(SimpleNamespace(project=fixture.project, root=fixture.root), plan_path)
        self.addCleanup(store.close)
        before = store.snapshot()
        moving = [node for node in before["nodes"] if node["kind"] == "segment"][1]
        result = store.apply({
            "origin": "manual", "expected": before["revisions"],
            "operations": [{"op": "move_segments", "editId": "a0000000-0000-4000-8000-000000000001",
                "mode": "overwrite", "placements": [{"id": moving["id"], "track": "V2", "atFrame": 2}]}],
        })
        built = build(project, result["plan"])
        self.assertIn("explicit", why_not(built["props"]))
        public = fixture.root / "render-test/public"
        public.mkdir(parents=True)
        stage(project, built, public)
        props_path = public / "props.json"
        props_path.write_text(json.dumps(built["props"]))
        output = fixture.root / "render-test/layered.mp4"
        run_remotion(project, props_path, public, output, fixture.root / "render-test/render.log")
        pixels = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-an", "-vf",
            "format=rgb24,crop=1:1:48:48", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            check=True, capture_output=True, timeout=30).stdout
        frames = np.frombuffer(pixels, dtype=np.uint8).reshape(-1, 3).astype(float)
        self.assertEqual(len(frames), 8)
        self.assertGreater(frames[1, 0], frames[1, 2] + 100, "red V1 before the upper clip")
        self.assertGreater(frames[3, 2], frames[3, 0] + 100, "V2 must cover V1, not concatenate after it")
        self.assertGreater(frames[5, 2], frames[5, 0] + 100, "the moved blue source continues on its own clock")
        self.assertLess(max(frames[7]), 25, "the vacated trailing source interval must render black")
        pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-vn", "-ac", "1", "-ar",
            "48000", "-f", "f32le", "-"], check=True, capture_output=True, timeout=30).stdout
        audio = np.frombuffer(pcm, dtype="<f4")
        def energy(at, frequency):
            values = audio[round((at-.04)*48000):round((at+.04)*48000)]
            spectrum = abs(np.fft.rfft(values * np.hanning(len(values))))
            frequencies = np.fft.rfftfreq(len(values), 1/48000)
            return max(spectrum[abs(frequencies-frequency) < 20])
        self.assertGreater(energy(.1, 440), 5 * energy(.1, 880))
        self.assertGreater(energy(.3, 440), 1, "covered V1 audio must remain in the mix")
        self.assertGreater(energy(.3, 880), 1, "V2 source audio must follow its new placement")
        self.assertGreater(energy(.5, 880), 5 * energy(.5, 440))
        gap = audio[round(.66*48000):round(.74*48000)]
        self.assertLess(float(np.sqrt(np.mean(gap**2))), .005)
        for name, content in originals.items():
            self.assertEqual((project.media_dir / f"{name}.mp4").read_bytes(), content)
