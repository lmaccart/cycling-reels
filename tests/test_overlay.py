"""Fast render path: which reels qualify, which frames get a text still, and that ffmpeg puts every clip,
cutaway and text still on exactly the right frames. Also: render_plan falls back to the full render."""

from __future__ import annotations

import json
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

    def test_editor_tracks_and_safe_area_need_the_full_render(self):
        for word, p in {"tracks": props(timeline=True), "safe-area": props(safeArea=True)}.items():
            with self.subTest(word):
                self.assertIn(word, overlay.why_not(p))

    def test_chrome_only_effects_take_the_fast_path_when_they_are_a_small_part(self):
        seg = props()["segments"][0]
        long = {**seg, "durationInFrames": 300}
        base = props(totalFrames=300, segments=[long])
        cases = {
            "punch-ins": {"segments": [{**long, "punch": [[0, 1.0, 0.5, 0.5], [20, 1.2, 0.5, 0.5], [40, 1.2, 0.5, 0.5], [44, 1.0, 0.5, 0.5]]}]},
            "blur": {"segments": [{**long, "blur": [{"strength": 20, "keys": [[10, 0.5, 0.5, 0.2, 0.2], [30, 0.5, 0.5, 0.2, 0.2]]}]}]},
            "photo cutaway": {"segments": [{**long, "cutaways": [{"kind": "photo", "src": "c.jpg", "startFrame": 0, "durationInFrames": 9}]}]},
            "route map": {"routes": [{"startFrame": 10, "endFrame": 40, "paths": [], "dots": []}]},
            "progress bar": {"progress": [{"startFrame": 10, "endFrame": 40}]},
            "exhibits": {"exhibits": [{"startFrame": 10, "endFrame": 40}]},
            "title board": {"titleCard": {"title": "x", "startFrame": 0, "endFrame": 30}},
            "highlight": {"captions": [{"startFrame": 0, "endFrame": 9, "words": []}], "brand": {**props()["brand"], "captionStyle": "highlight"}},
        }  # fmt: skip
        for word, over in cases.items():
            with self.subTest(word):
                self.assertIsNone(overlay.why_not({**base, **over}))

    def test_a_reel_that_is_mostly_chrome_frames_keeps_the_full_render(self):
        why = overlay.why_not(props(exhibits=[{"startFrame": 0, "endFrame": 88, "depth": "behind"}]))
        self.assertIn("need Chrome", why)
        self.assertIn("exhibits", why)
        seg = props()["segments"][0]
        why = overlay.why_not(props(segments=[{**seg, "kind": "photo"}]))
        self.assertIn("photos", why)

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


class ChromeFrameTests(unittest.TestCase):
    """Which frames Chrome must draw: whole (the footage changes) or just the overlay layer."""

    def seg(self, n=100, **over):
        return {"kind": "video", "src": "s.mp4", "durationInFrames": n, "transitionInFrames": 0, "volume": 1.0, **over}

    def chrome(self, **over):
        full, over_, why = overlay.chrome_frames(props(**over))
        return sorted(full), sorted(over_), why

    def test_plain_footage_needs_no_chrome_frames(self):
        self.assertEqual(self.chrome(), ([], [], {}))

    def test_exhibit_like_layers_are_overlay_frames(self):
        full, over, why = self.chrome(
            totalFrames=100, segments=[self.seg()], exhibits=[{"startFrame": 10, "endFrame": 14}],
            routes=[{"startFrame": 20, "endFrame": 22}], progress=[{"startFrame": 30, "endFrame": 31}],
            titleCard={"startFrame": 40, "endFrame": 42}, foregrounds=[{"startFrame": 50, "durationInFrames": 2}])  # fmt: skip
        self.assertEqual(full, [])
        self.assertEqual(over, [10, 11, 12, 13, 20, 21, 30, 40, 41, 50, 51])
        self.assertEqual(set(why), {"exhibits", "route maps", "progress bars", "title board"})

    def test_punch_in_frames_are_the_non_identity_ones(self):
        keys = [[0, 1.0, 0.5, 0.5], [10, 1.0, 0.5, 0.5], [14, 1.15, 0.5, 0.5], [30, 1.15, 0.5, 0.5], [34, 1.0, 0.5, 0.5]]
        full, over, why = self.chrome(totalFrames=100, segments=[self.seg(punch=keys)])
        self.assertEqual(full, list(range(10, 34)))  # from the key where the zoom starts to the one where it is back at 1x
        self.assertEqual(over, [])
        self.assertIn("punch-ins", why)

    def test_punch_that_stays_zoomed_covers_the_rest_of_the_segment(self):
        full, _, _ = self.chrome(totalFrames=100, segments=[self.seg(punch=[[0, 1.0, 0.5, 0.5], [10, 1.2, 0.5, 0.5]])])
        self.assertEqual(full, list(range(0, 100)))

    def test_blur_patches_are_drawn_by_ffmpeg_so_they_cost_no_chrome_frames(self):
        blur = [{"strength": 20, "keys": [[5, 0.5, 0.5, 0.2, 0.2], [9, 0.5, 0.5, 0.2, 0.2]]}]
        full, over, why = self.chrome(totalFrames=100, segments=[self.seg(50), self.seg(50, blur=blur)])
        self.assertEqual((full, over, why), ([], [], {}))

    def test_blur_under_a_punch_in_is_drawn_by_chrome(self):
        punch = [[0, 1.0, 0.5, 0.5], [6, 1.2, 0.5, 0.5], [8, 1.0, 0.5, 0.5]]
        blur = [{"strength": 20, "keys": [[5, 0.5, 0.5, 0.2, 0.2], [30, 0.5, 0.5, 0.2, 0.2]]}]
        full, _, _ = self.chrome(totalFrames=100, segments=[self.seg(100, punch=punch, blur=blur)])
        self.assertEqual(full, list(range(0, 8)))  # the zoom frames; the rest of the patch is ffmpeg's

    def test_piecewise_expression_is_linear_between_keys_and_held_after(self):
        keys = [[0, 0.0, 1.0], [10, 1.0, 1.0], [20, 3.0, 1.0]]
        self.assertEqual(overlay._piecewise(keys, 2), "1.0")  # constant column
        expr = overlay._piecewise(keys, 1)
        # test-only: evaluates the expression string overlay.py itself just generated (numbers and if/lt only)
        n = lambda v: eval(expr.replace("if(", "_if(").replace("lt(N,", "_lt(N,"), {"_if": lambda c, a, b: a if c else b, "_lt": lambda x, y: x < y, "N": v})  # noqa: E731
        self.assertEqual([n(0), n(5), n(10), n(15), n(20), n(40)], [0.0, 0.5, 1.0, 2.0, 3.0, 3.0])

    def test_crossfade_photo_and_photo_cutaway(self):
        a = self.seg(40)
        b = self.seg(40, transitionInFrames=6)
        c = {**self.seg(20), "kind": "photo"}
        d = self.seg(40, cutaways=[{"kind": "photo", "src": "p.jpg", "startFrame": 5, "durationInFrames": 3},
                                   {"kind": "video", "src": "v.mp4", "startFrame": 20, "durationInFrames": 5}])  # fmt: skip
        full, over, why = self.chrome(totalFrames=40 + 34 + 20 + 40, segments=[a, b, c, d])
        # starts 0, 34 (crossfade 34..39), 74 (photo 74..93), 94 (photo cutaway 99..101)
        self.assertEqual(full, list(range(34, 40)) + list(range(74, 94)) + [99, 100, 101])
        self.assertEqual(set(why), {"crossfades", "photos", "photo cutaways"})

    def test_chrome_frames_win_over_overlay_frames(self):
        full, over, _ = self.chrome(totalFrames=100, segments=[self.seg(punch=[[0, 1.0, 0.5, 0.5], [10, 1.0, 0.5, 0.5], [20, 1.0, 0.5, 0.5], [21, 1.2, 0.5, 0.5], [22, 1.0, 0.5, 0.5]])],
                                    exhibits=[{"startFrame": 15, "endFrame": 30}])  # fmt: skip
        self.assertEqual(full, list(range(20, 22)))
        self.assertEqual(over, list(range(15, 20)) + [26, 27, 28, 29])  # 20, 21 are Chrome's whole frames; holds 22..25; leaves after 25

    def test_short_gaps_between_runs_are_bridged(self):
        _, over, _ = self.chrome(totalFrames=100, segments=[self.seg()], exhibits=[
            {"startFrame": 10, "endFrame": 20}, {"startFrame": 22, "endFrame": 30}, {"startFrame": 34, "endFrame": 40}])  # fmt: skip
        self.assertEqual(overlay.runs(set(over)), [(10, 30), (34, 40)])  # gap of 2 joined, gap of 4 kept

    def test_runs(self):
        self.assertEqual(overlay.runs({1, 2, 3, 7, 9, 10}), [(1, 4), (7, 8), (9, 11)])
        self.assertEqual(overlay.runs(set()), [])

    def test_seg_starts_follow_crossfades(self):
        p = props(segments=[self.seg(30), self.seg(30, transitionInFrames=5), self.seg(30)])
        self.assertEqual(overlay.seg_starts(p), [0, 25, 55])

    def test_each_chrome_frame_is_its_own_still_and_text_states_still_merge(self):
        p = props(totalFrames=100, segments=[self.seg()], exhibits=[{"startFrame": 10, "endFrame": 13}],
                  beats=[{"text": "b", "startFrame": 40, "endFrame": 70, "position": "lower", "style": "light"}])  # fmt: skip
        sp = overlay.spans(p)
        self.assertEqual(sp[:3], [(0, 10), (10, 3), (13, 27)])  # Chrome's frames are one blank span in the still stream
        self.assertEqual(sum(n for _, n in sp), 100)

    def test_exhibit_only_needs_chrome_while_it_moves(self):
        e = {"startFrame": 100, "endFrame": 160, "enter": "pop", "exit": "fade", "depth": "front"}
        _, over, _ = self.chrome(totalFrames=200, segments=[self.seg(200)], exhibits=[e])
        self.assertEqual(over, list(range(100, 107)) + list(range(156, 160)))  # in: 7 frames, out: frames after dur - 5
        sp = overlay.spans(props(totalFrames=200, segments=[self.seg(200)], exhibits=[e]))
        self.assertIn((107, 49), sp)  # holding still: one still for the whole stretch

    def test_exhibit_motion_rules(self):
        base = {"startFrame": 0, "endFrame": 60, "enter": "none", "exit": "none", "depth": "front"}
        moves = lambda e, local: overlay._exhibit_moves({**base, **e}, local)  # noqa: E731
        self.assertFalse(moves({}, 30))
        self.assertTrue(moves({"enter": "pop"}, 6))
        self.assertFalse(moves({"enter": "pop"}, 7))
        self.assertTrue(moves({"exit": "fade"}, 56))
        self.assertFalse(moves({"exit": "fade"}, 55))
        self.assertTrue(moves({"path": [[0, 1, 1], [20, 5, 5]]}, 19))
        self.assertFalse(moves({"path": [[0, 1, 1], [20, 5, 5]]}, 20))  # held after its last key
        self.assertTrue(moves({"kind": "chart", "chart": {"draw": True}}, 33))
        self.assertFalse(moves({"kind": "chart", "chart": {"draw": True}}, 34))
        self.assertTrue(moves({"depth": "behind"}, 30))  # the rider's cut-out video plays in front of it
        short = {"startFrame": 0, "endFrame": 6, "enter": "pop", "exit": "fade", "ownerSegmentIndex": 0}
        self.assertFalse(overlay._exhibit_moves(short, 0))  # short exhibit owned by a clip doesn't animate

    def test_progress_bar_motion(self):
        b = {"startFrame": 0, "endFrame": 60, "fillFrames": 20}
        self.assertEqual([overlay._bar_moves(b, f) for f in (0, 5, 6, 25, 26, 54, 55, 56)],
                         [True, True, True, True, False, False, False, True])  # fade in, fill, hold, fade out

    def test_chunks_cut_runs_into_pieces(self):
        p = props(totalFrames=200, segments=[self.seg(200)], routes=[{"startFrame": 10, "endFrame": 140, "paths": [], "dots": []}])
        got = [(c["kind"], c["start"], c["end"], c["dir"]) for c in overlay.chunks(p)]
        self.assertEqual(got, [("over", 10, 70, "over-10"), ("over", 70, 130, "over-70"), ("over", 130, 140, "over-130")])

    def test_highlight_caption_state_follows_the_spoken_word(self):
        words = [{"text": "a", "startFrame": 10, "endFrame": 15}, {"text": "b", "startFrame": 15, "endFrame": 20}]
        p = props(captions=[{"startFrame": 10, "endFrame": 20, "words": words}], brand={**props()["brand"], "captionStyle": "highlight"})
        sp = overlay.spans(p)
        starts = [s for s, _ in sp]
        self.assertIn(15, starts)  # the highlight moves
        self.assertEqual(overlay.frame_state(p, 16)[0][3], (1,))
        native = props(captions=p["captions"])
        self.assertEqual(overlay.frame_state(native, 16), overlay.frame_state(native, 18))  # native: one still


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


    def test_timeline_stops_a_segment_where_the_next_crossfade_starts_and_skips_photos(self):
        seg = props()["segments"][0]
        p = props(segments=[
            {**seg, "durationInFrames": 30},
            {**seg, "kind": "photo", "src": "p.jpg", "durationInFrames": 20},
            {**seg, "src": "seg2.mp4", "durationInFrames": 30, "transitionInFrames": 5,
             "cutaways": [{"kind": "photo", "src": "x.jpg", "startFrame": 1, "durationInFrames": 3},
                          {"kind": "video", "src": "c.mp4", "startFrame": 12, "durationInFrames": 5}]},
        ])  # fmt: skip
        self.assertEqual(overlay.timeline(p), [
            {"src": "seg0.mp4", "start": 0, "frames": 30, "cutaway": False},
            {"src": None, "start": 30, "frames": 15, "cutaway": False},
            {"src": "seg2.mp4", "start": 45, "frames": 30, "cutaway": False},
            {"src": "c.mp4", "start": 57, "frames": 5, "cutaway": True},
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


    def test_chrome_frames_splice_in_at_exact_frames(self):
        """red | photo (black under Chrome's opaque frames) | crossfade into blue: starts 0, 30, 45; 75 frames."""
        self.clip("seg0.mp4", "red", 30)
        self.clip("seg2.mp4", "blue", 40)
        self.clip("cut2.mp4", "lime", 5)
        (self.d / "stills").mkdir()
        Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0)).save(self.d / "stills" / "0.png")
        Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0)).save(self.d / "stills" / "50.png")
        overlay._blank_png(self.d / "stills" / "blank.png", self.W, self.H)
        (self.d / "stills" / "full-30").mkdir()
        for f in range(30, 50):  # Chrome's opaque frames for the photo, then the crossfade (a colour per frame)
            Image.new("RGBA", (self.W, self.H), (255, 255, 0, 255)).save(self.d / "stills" / "full-30" / f"{f}.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=2.5", str(self.d / "audio.wav")], check=True)
        seg = {"kind": "video", "transitionInFrames": 0, "volume": 1.0}
        p = props(width=self.W, height=self.H, totalFrames=75, segments=[
            {**seg, "src": "seg0.mp4", "durationInFrames": 30},
            {**seg, "kind": "photo", "src": "p.jpg", "durationInFrames": 20},
            {**seg, "src": "seg2.mp4", "durationInFrames": 30, "transitionInFrames": 5,
             "cutaways": [{"kind": "video", "src": "cut2.mp4", "startFrame": 12, "durationInFrames": 5}]},
        ])  # fmt: skip
        self.assertEqual([(c["start"], c["end"], c["dir"]) for c in overlay.chunks(p)], [(30, 50, "full-30")])
        sp = overlay.spans(p)
        self.assertEqual(sp, [(0, 30), (30, 20), (50, 25)])
        (self.d / "overlay.ffconcat").write_text(overlay.concat_list(sp, FPS, {30}))
        out = self.d / "render.mp4"
        subprocess.run([str(c) for c in overlay.compose_cmd(p, self.d, self.d / "audio.wav", out)], cwd=self.d, check=True,
                       capture_output=True)  # fmt: skip
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                             capture_output=True, check=True).stdout  # fmt: skip
        frames = np.frombuffer(raw, np.uint8).reshape(-1, self.H, self.W, 3).astype(int)
        self.assertEqual(len(frames), 75)

        def colour(px) -> str:
            r, g, b = px
            return "yellow" if r > 200 and g > 200 and b < 80 else "red" if r > 150 and g < 90 else "lime" if g > 150 and r < 120 else "blue" if b > 150 else "?"

        for n, f in enumerate(frames):
            want = "red" if n < 30 else "yellow" if n < 50 else "lime" if 57 <= n < 62 else "blue"  # cutaway: 45 + 12
            self.assertEqual(colour(f[96, 54]), want, f"frame {n}")

    def test_blur_patch_softens_only_its_ellipse_and_its_frames(self):
        d = self.d
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        f"color=c=black:s={self.W}x{self.H}:r={FPS},drawbox=x=40:y=70:w=28:h=52:color=white:t=fill",
                        "-frames:v", "20", "-pix_fmt", "yuv420p", str(d / "seg0.mp4")], check=True)  # fmt: skip
        (d / "stills").mkdir()
        overlay._blank_png(d / "stills" / "blank.png", self.W, self.H)
        (d / "overlay.ffconcat").write_text(overlay.concat_list([(0, 20)], FPS, {0}))
        Image.new("RGBA", (self.W, self.H), (0, 0, 0, 0)).save(d / "stills" / "0.png")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=1", str(d / "audio.wav")], check=True)
        keys = [[5, 0.5, 0.5, 0.8, 0.5], [14, 0.5, 0.5, 0.8, 0.5]]  # a wide ellipse around the white box's left edge
        seg = {"kind": "video", "src": "seg0.mp4", "durationInFrames": 20, "transitionInFrames": 0, "volume": 1.0,
               "blur": [{"strength": 10, "keys": keys}]}  # fmt: skip
        p = props(width=self.W, height=self.H, totalFrames=20, segments=[seg])
        out = d / "render.mp4"
        subprocess.run([str(c) for c in overlay.compose_cmd(p, d, d / "audio.wav", out)], cwd=d, check=True, capture_output=True)
        raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(out), "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                             capture_output=True, check=True).stdout  # fmt: skip
        frames = np.frombuffer(raw, np.uint8).reshape(-1, self.H, self.W).astype(int)
        self.assertEqual(len(frames), 20)
        edge = lambda n: frames[n][96, 40]  # noqa: E731  (the white box's left edge, inside the ellipse)
        self.assertGreater(edge(0), 200)  # not yet blurred: sharp
        self.assertGreater(edge(4), 200)
        for n in (5, 9, 14):
            self.assertLess(edge(n), 200, f"frame {n}")  # blurred while the patch is on
            self.assertGreater(frames[n][96, 54], 150)  # the middle of the box stays bright
        self.assertGreater(edge(15), 200)  # gone after its last key
        self.assertLess(frames[9][96, 3], 10)  # far from the ellipse: untouched black

    def test_photo_segment_is_black_underneath(self):
        p = props(width=self.W, height=self.H, totalFrames=20,
                  segments=[{"kind": "photo", "src": "p.jpg", "durationInFrames": 20, "transitionInFrames": 0, "volume": 1.0}])
        cmd = " ".join(str(c) for c in overlay.compose_cmd(p, self.d, self.d / "a.wav", self.d / "o.mp4"))
        self.assertIn("color=c=black:s=108x192", cmd)
        self.assertNotIn("p.jpg", cmd)


class FallbackTests(unittest.TestCase):
    def test_missing_chrome_frames_raise_so_the_full_render_takes_over(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "remotion" / "scripts").mkdir(parents=True)
        (root / "remotion" / "node_modules" / "@remotion" / "renderer").mkdir(parents=True)
        staging = root / "staging"
        (staging).mkdir()
        project = mock.Mock(remotion_dir=root / "remotion")
        project.cfg.return_value = None
        project.rel = lambda p: str(p)

        def node(cmd, **kw):  # Remotion "succeeds": sound is there but no still for the exhibit frames
            (staging / "audio.wav").write_bytes(b"x")
            (staging / "stills").mkdir(exist_ok=True)
            for f in json.loads((staging / "overlay-job.json").read_text())["frames"]:
                (staging / "stills" / f"{f}.png").write_bytes(b"x")
            return mock.Mock(returncode=0)

        p = props(exhibits=[{"startFrame": 10, "endFrame": 20}])
        (staging / "props.json").write_text("{}")
        with mock.patch.object(overlay.subprocess, "run", node), mock.patch.object(overlay, "require"):
            with self.assertRaisesRegex(ReelsError, "didn't write Chrome frames"):
                overlay.render(project, p, staging / "props.json", staging, staging / "o.mp4", staging / "log")
        job = json.loads((staging / "overlay-job.json").read_text())
        self.assertEqual(job["chunks"], [{"kind": "over", "start": 10, "end": 20, "dir": "over-10"}])
        self.assertNotIn(12, job["frames"])  # Chrome-frame stills come from the run, not a text still

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
