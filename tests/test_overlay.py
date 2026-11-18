"""Fast render path: which reels qualify, which frames get a text still, and that ffmpeg puts every clip,
cutaway and text still on exactly the right frames. Also: render_plan falls back to the full render."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
from PIL import Image

from helpers import TempProject
from reels import overlay, render
from reels.util import ReelsError

FPS = 30


def props(**over) -> dict:
    p = {
        "width": 1080, "height": 1920, "fps": FPS, "totalFrames": 90,
        "brand": {"handle": "@x", "accent": "#FFE14D", "captionStyle": "native", "textStyle": "light"},
        "segments": [{"kind": "video", "src": "seg0.mp4", "durationInFrames": 90, "transitionInFrames": 0, "volume": 1.0}],
        "hook": None, "captions": [], "beats": [], "stats": [], "music": None, "endCard": None, "safeArea": False,
    }  # fmt: skip
    p.update(over)
    return p


class EligibilityTests(unittest.TestCase):
    def test_plain_footage_with_text_qualifies(self):
        p = props(hook={"text": "hi", "startFrame": 0, "endFrame": 30, "position": "top", "style": "light"},
                  beats=[{"text": "b", "startFrame": 40, "endFrame": 60, "position": "lower", "style": "light"}],
                  music={"src": "music.mp3"})  # fmt: skip
        p["segments"][0]["cutaways"] = [{"kind": "video", "src": "cut0-0.mp4", "startFrame": 10, "durationInFrames": 20}]
        self.assertIsNone(overlay.why_not(p))

    def test_animated_features_need_the_full_render(self):
        seg = props()["segments"][0]
        cases = {
            "photo": props(segments=[{**seg, "kind": "photo"}]),
            "crossfades": props(segments=[seg, {**seg, "transitionInFrames": 8}]),
            "punch-ins": props(segments=[{**seg, "punch": [[0, 1.2, 0.5, 0.5]]}]),
            "blur": props(segments=[{**seg, "blur": [{"strength": 20, "keys": [[0, 0.5, 0.5, 0.2, 0.2]]}]}]),
            "photo cutaway": props(segments=[{**seg, "cutaways": [{"kind": "photo", "src": "c.jpg", "startFrame": 0, "durationInFrames": 9}]}]),
            "route map": props(routes=[{}]),
            "progress bar": props(progress=[{}]),
            "exhibits": props(exhibits=[{}]),
            "title board": props(titleCard={"title": "x"}),
            "safe-area": props(safeArea=True),
            "highlight": props(captions=[{"startFrame": 0, "endFrame": 9, "words": []}],
                               brand={**props()["brand"], "captionStyle": "highlight"}),
        }  # fmt: skip
        for word, p in cases.items():
            with self.subTest(word):
                why = overlay.why_not(p)
                self.assertIsNotNone(why)
                self.assertIn(word.split()[0], why)

    def test_crossfade_on_the_first_segment_is_ignored(self):
        self.assertIsNone(overlay.why_not(props(segments=[{**props()["segments"][0], "transitionInFrames": 8}])))


class StatPillTests(unittest.TestCase):
    def chip(self, **over):
        return {"label": "speed", "startFrame": 10, "endFrame": 50, **over}

    def test_stats_no_longer_need_the_full_render(self):
        self.assertIsNone(overlay.why_not(props(stats=[self.chip()])))

    def test_fade_in_hold_fade_out_frames(self):
        c = self.chip()
        self.assertEqual([overlay._chip_state(c, f)[1] for f in range(10, 14)], [0, 1, 2, 3])  # fade in
        self.assertEqual(overlay._chip_state(c, 14)[1], "static")
        self.assertEqual(overlay._chip_state(c, 46)[1], "static")
        self.assertEqual([overlay._chip_state(c, f)[1] for f in range(47, 50)], [37, 38, 39])  # fade out

    def test_short_owned_pill_does_not_fade(self):
        c = self.chip(endFrame=16, ownerSegmentIndex=0)
        self.assertTrue(all(overlay._chip_state(c, f)[1] == "static" for f in range(10, 16)))
        c2 = self.chip(endFrame=16)  # not owned: still fades
        self.assertEqual(overlay._chip_state(c2, 10)[1], 0)

    def test_live_value_steps(self):
        c = self.chip(live={"values": ["1", "2", "3"], "everyFrames": 5})
        self.assertEqual([overlay._chip_state(c, f)[2] for f in (10, 14, 15, 19, 20, 30, 49)], [0, 0, 1, 1, 2, 2, 2])

    def test_spans_split_on_chip_changes_and_cover_every_frame(self):
        p = props(stats=[self.chip(live={"values": ["a", "b"], "everyFrames": 20})])
        sp = overlay.spans(p)
        self.assertEqual(sum(n for _, n in sp), 90)
        starts = [s for s, _ in sp]
        self.assertIn(30, starts)  # live value steps
        self.assertIn(10, starts)
        self.assertIn(50, starts)  # pill gone
        self.assertEqual(overlay.frame_state(p, 60), ())


class SpanTests(unittest.TestCase):
    def test_nothing_on_screen_is_one_still(self):
        self.assertEqual(overlay.spans(props()), [(0, 90)])

    def test_caption_fade_in_then_holds(self):
        p = props(captions=[{"startFrame": 10, "endFrame": 40, "words": []}, {"startFrame": 40, "endFrame": 50, "words": []}])
        self.assertEqual(overlay.spans(p), [(0, 10), (10, 1), (11, 1), (12, 28), (40, 1), (41, 1), (42, 8), (50, 40)])

    def test_text_beat_pops_in_and_fades_out(self):
        p = props(beats=[{"text": "b", "startFrame": 20, "endFrame": 40, "position": "lower", "style": "light"}])
        sp = overlay.spans(p)
        self.assertEqual(sp[0], (0, 20))
        self.assertEqual(sp[1:5], [(20, 1), (21, 1), (22, 1), (23, 1)])  # 4 entrance frames
        self.assertEqual(sp[5], (24, 13))  # holds 24..36
        self.assertEqual(sp[6:9], [(37, 1), (38, 1), (39, 1)])  # exit: interpolate(local, [16, 20]) moves after local 16
        self.assertEqual(sp[9], (40, 50))

    def test_opening_hook_is_already_in_on_frame_0(self):
        p = props(hook={"text": "hi", "startFrame": 0, "endFrame": 30, "position": "top", "style": "light"})
        sp = overlay.spans(p)
        self.assertEqual(sp[0], (0, 27))
        self.assertEqual(sp[1:4], [(27, 1), (28, 1), (29, 1)])

    def test_spans_cover_every_frame_once(self):
        p = props(captions=[{"startFrame": 5, "endFrame": 70, "words": []}],
                  beats=[{"text": "b", "startFrame": 30, "endFrame": 60, "position": "top", "style": "dark"}],
                  endCard={"cta": "x", "handle": "", "startFrame": 80, "endFrame": 90, "style": "light"})  # fmt: skip
        sp = overlay.spans(p)
        self.assertEqual(sum(n for _, n in sp), 90)
        self.assertEqual([s for s, _ in sp], sorted({s for s, _ in sp}))
        for (a, n), (b, _) in zip(sp, sp[1:]):
            self.assertEqual(a + n, b)

    def test_concat_list_repeats_the_last_still(self):
        text = overlay.concat_list([(0, 30), (30, 60)], FPS)
        self.assertEqual(text.splitlines(), [
            "ffconcat version 1.0",
            "file 'stills/0.png'", "option framerate 30", "duration 1.000000",
            "file 'stills/30.png'", "option framerate 30", "duration 2.000000",
            "file 'stills/30.png'", "option framerate 30",
        ])  # fmt: skip

    def test_timeline_places_cutaways_over_their_segment(self):
        seg = props()["segments"][0]
        p = props(segments=[{**seg, "durationInFrames": 30},
                            {**seg, "src": "seg1.mp4", "durationInFrames": 60,
                             "cutaways": [{"kind": "video", "src": "cut1-0.mp4", "startFrame": 12, "durationInFrames": 20}]}])  # fmt: skip
        self.assertEqual(overlay.timeline(p), [
            {"src": "seg0.mp4", "start": 0, "frames": 30, "cutaway": False},
            {"src": "seg1.mp4", "start": 30, "frames": 60, "cutaway": False},
            {"src": "cut1-0.mp4", "start": 42, "frames": 20, "cutaway": True},
        ])  # fmt: skip


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class ComposeTests(unittest.TestCase):
    """Synthetic clips in flat colours and one text still with a white box: every frame is checked."""

    W, H = 108, 192

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.d = Path(self.tmp.name)

    def clip(self, name: str, color: str, frames: int, audio: bool = True) -> None:
        cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c={color}:s={self.W}x{self.H}:r={FPS}"]
        cmd += ["-frames:v", str(frames), "-pix_fmt", "yuv420p", str(self.d / name)]
        subprocess.run(cmd, check=True)

    def test_every_frame_lands_where_it_should(self):
        self.clip("seg0.mp4", "red", 30)
        self.clip("seg1.mp4", "blue", 30)
        self.clip("cut1-0.mp4", "lime", 10)
        (self.d / "stills").mkdir()
        Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0)).save(self.d / "stills" / "0.png")
        box = Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0))
        box.paste((255, 255, 255, 255), (0, 0, self.W, 40))
        box.save(self.d / "stills" / "20.png")
        Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0)).save(self.d / "stills" / "45.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1.9", str(self.d / "audio.wav")], check=True)
        seg = {"kind": "video", "transitionInFrames": 0, "volume": 1.0}
        p = props(width=self.W, height=self.H, totalFrames=60, segments=[
            {**seg, "src": "seg0.mp4", "durationInFrames": 30},
            {**seg, "src": "seg1.mp4", "durationInFrames": 30,
             "cutaways": [{"kind": "video", "src": "cut1-0.mp4", "startFrame": 5, "durationInFrames": 10}]},
        ])  # fmt: skip
        (self.d / "overlay.ffconcat").write_text(overlay.concat_list([(0, 20), (20, 25), (45, 15)], FPS))
        out = self.d / "render.mp4"
        subprocess.run([str(c) for c in overlay.compose_cmd(p, self.d, self.d / "audio.wav", out)], cwd=self.d, check=True,
                       capture_output=True)  # fmt: skip

        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout  # fmt: skip
        frames = np.frombuffer(raw, np.uint8).reshape(-1, self.H, self.W, 3).astype(int)
        self.assertEqual(len(frames), 60)

        def colour(px) -> str:
            r, g, b = px
            return "white" if min(r, g, b) > 200 else "red" if r > 150 else "lime" if g > 150 else "blue" if b > 150 else "?"

        for n, f in enumerate(frames):
            bottom, top = colour(f[150, 54]), colour(f[10, 54])
            want = "red" if n < 30 else "lime" if 35 <= n < 45 else "blue"
            self.assertEqual(bottom, want, f"frame {n} footage")
            self.assertEqual(top, "white" if 20 <= n < 45 else want, f"frame {n} text")

        dur = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=duration",
                              "-of", "csv=p=0", str(out)], capture_output=True, text=True, check=True).stdout  # fmt: skip
        self.assertAlmostEqual(float(dur), 2.0, delta=0.03)  # audio padded to the picture's length


class FallbackTests(unittest.TestCase):
    def test_fast_path_failure_falls_back_to_full_render(self):
        t = TempProject(self)
        calls = []
        built = {"props": props(), "warnings": [], "segs": [], "fps": FPS, "total": 90, "loop": None, "music": None}

        def boom(*a, **k):
            raise ReelsError("Remotion didn't write the text stills")

        def full(project, props_path, public_dir, out_tmp, log_path):
            calls.append("full")
            raise RuntimeError("reached full render")

        with mock.patch.object(render, "load_plan", return_value={"slug": "x"}), \
             mock.patch.object(render, "build", return_value=built), \
             mock.patch.object(render, "stage"), \
             mock.patch.object(render.overlay, "render", boom), \
             mock.patch.object(render, "run_remotion", full), \
             mock.patch.object(render, "require"):  # fmt: skip
            with self.assertRaisesRegex(RuntimeError, "reached full render"):
                render.render_plan(t.project, "plans/x.json")
        self.assertEqual(calls, ["full"])

    def test_config_switch_turns_it_off(self):
        t = TempProject(self)
        t.project.config["render"]["fastPath"] = False
        built = {"props": props(), "warnings": [], "segs": [], "fps": FPS, "total": 90, "loop": None, "music": None}
        fast = mock.Mock()
        with mock.patch.object(render, "load_plan", return_value={"slug": "x"}), \
             mock.patch.object(render, "build", return_value=built), \
             mock.patch.object(render, "stage"), \
             mock.patch.object(render.overlay, "render", fast), \
             mock.patch.object(render, "run_remotion", side_effect=RuntimeError("full")), \
             mock.patch.object(render, "require"):  # fmt: skip
            with self.assertRaisesRegex(RuntimeError, "full"):
                render.render_plan(t.project, "plans/x.json")
        fast.assert_not_called()


if __name__ == "__main__":
    unittest.main()


class TemplateDriftTests(unittest.TestCase):
    """overlay.py mirrors the template's animation lengths; if the template changes, this fails first."""

    def test_animation_frames_match_the_template(self):
        src = Path(__file__).resolve().parent.parent / "remotion" / "src" / "components"
        tiktok = (src / "TikTokText.tsx").read_text()
        captions = (src / "Captions.tsx").read_text()
        self.assertIn(f"interpolate(frame, [0, {overlay.TEXT_IN}], [0, 1], clamp)", tiktok)
        self.assertIn(f"interpolate(frame, [durationInFrames - {overlay.TEXT_OUT}, durationInFrames], [1, 0], clamp)", tiktok)
        self.assertIn(f"interpolate(frame - page.startFrame, [0, {overlay.CAPTION_IN}], [0.4, 1], clamp)", captions)
