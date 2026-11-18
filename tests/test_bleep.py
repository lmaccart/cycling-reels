from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from reels.bleep import SWEARS, check_bleep, mask, masked_words, mute_filter, resolve_bleeps, tone_graph, unbleeped_swears
from reels.config import Project
from reels.render import Seg, caption_pages_for, stage_video

VIDEO = {"id": "clip", "kind": "video", "transcript": "library/transcripts/clip.json", "probe": {"hasAudio": True}}
WORDS = [
    {"w": "My", "s": 13.1, "e": 13.3, "p": 0.9},
    {"w": "god", "s": 13.3, "e": 13.5, "p": 0.9},
    {"w": "it's", "s": 13.5, "e": 13.8, "p": 0.9},
    {"w": "fucking", "s": 13.8, "e": 14.3, "p": 0.9},
    {"w": "cold.", "s": 14.4, "e": 14.9, "p": 0.9},
]


def seg_spec(bleep, **extra):
    spec = {"in": 13.0, "out": 16.0, "speed": 1.0, "bleep": bleep, "captions": "transcript", **extra}
    errors: list[str] = []
    check_bleep(spec, VIDEO, errors, "segment 1")
    return spec, errors


class CheckBleepTests(unittest.TestCase):
    def test_forms(self):
        self.assertIsNone(seg_spec(None)[0]["bleep"])
        self.assertIsNone(seg_spec(False)[0]["bleep"])
        self.assertEqual(seg_spec(True)[0]["bleep"]["words"], SWEARS)
        self.assertEqual(seg_spec("auto")[0]["bleep"]["words"], SWEARS)
        spec, errors = seg_spec({"words": ["Fucking,"], "sound": "mute"})
        self.assertEqual(errors, [])
        self.assertEqual(spec["bleep"]["words"], {"fucking"})
        self.assertEqual(spec["bleep"]["sound"], "mute")
        spec, errors = seg_spec([{"at": 0.8, "for": 0.5}])
        self.assertEqual(errors, [])
        self.assertEqual(spec["bleep"]["windows"], [{"at": 0.8, "for": 0.5}])

    def test_errors(self):
        self.assertTrue(seg_spec({"words": []})[1])
        self.assertTrue(seg_spec([{"at": 1}])[1])
        self.assertTrue(seg_spec({"words": ["x"], "sound": "quack"})[1])
        self.assertTrue(seg_spec("loud")[1])
        errors: list[str] = []
        check_bleep({"bleep": True}, {**VIDEO, "transcript": None}, errors, "s")
        self.assertIn("transcript", errors[0])
        errors = []
        check_bleep({"bleep": True}, {"id": "p", "kind": "photo", "probe": {}}, errors, "s")
        self.assertIn("video", errors[0])


class ResolveBleepTests(unittest.TestCase):
    def test_words_become_padded_source_windows_and_captions_are_masked(self):
        spec, _ = seg_spec(True)
        wins = resolve_bleeps(spec, WORDS)
        self.assertEqual(wins, [(13.76, 14.34)])
        self.assertEqual(unbleeped_swears(spec, WORDS), [])
        shown = [w["w"] for w in masked_words(spec, WORDS)]
        self.assertEqual(shown, ["My", "god", "it's", "f******", "cold."])

    def test_time_windows_are_segment_relative_and_respect_speed(self):
        spec, _ = seg_spec([{"at": 0.4, "for": 0.25}], speed=2.0)
        spec["bleep"]["pad"] = 0
        wins = resolve_bleeps(spec, WORDS)
        self.assertEqual(wins, [(13.8, 14.3)])  # 13 + 0.4*2 .. + 0.25*2
        self.assertEqual(spec["_bleepWords"], [13.8])  # "fucking" is covered, so it's masked too

    def test_unbleeped_swearing_is_reported(self):
        spec = {"in": 13.0, "out": 16.0, "speed": 1.0, "bleep": None}
        resolve_bleeps(spec, WORDS)
        self.assertEqual(unbleeped_swears(spec, WORDS), ["fucking"])
        self.assertIsNone(mute_filter(spec))
        self.assertIsNone(tone_graph(spec, 3.0))

    def test_overlapping_windows_merge_and_clip_to_segment(self):
        spec, _ = seg_spec([{"at": 2.8, "for": 1.0}, {"at": 2.5, "for": 0.4}])
        self.assertEqual(resolve_bleeps(spec, WORDS), [(15.46, 16.0)])

    def test_mask(self):
        self.assertEqual(mask("fucking,"), "f******,")
        self.assertEqual(mask("Shit!"), "S***!")
        self.assertEqual(mask("a"), "a")

    def test_filters(self):
        spec, _ = seg_spec(True)
        resolve_bleeps(spec, WORDS)
        self.assertEqual(mute_filter(spec), "volume=0:enable='between(t,0.760,1.340)'")
        self.assertIn("not(between(t,0.760,1.340))", tone_graph(spec, 3.0))
        spec["bleep"]["sound"] = "mute"
        self.assertIsNone(tone_graph(spec, 3.0))

    def test_captions_show_masked_word(self):
        spec, _ = seg_spec(True)
        resolve_bleeps(spec, WORDS)
        sg = Seg(index=0, kind="video", asset=VIDEO, spec=spec, frames=90)
        pages = caption_pages_for(sg, masked_words(spec, WORDS), 30, {"maxWordsPerPage": 4, "maxCharsPerPage": 30})
        text = json.dumps(pages)
        self.assertIn("f******", text)
        self.assertNotIn("fucking", text)


    def test_caption_words_point_back_at_transcript_words(self):
        # the editor's word corrections address the transcript, not the word's place on its page
        spec = {"in": 13.4, "out": 16.0, "speed": 1.0, "captions": "transcript"}
        sg = Seg(index=0, kind="video", asset=VIDEO, spec=spec, frames=78)
        pages = caption_pages_for(sg, WORDS, 30, {"maxWordsPerPage": 4, "maxCharsPerPage": 30})
        got = [(w["index"], w["w"], w["s"]) for pg in pages for w in pg["words"]]
        self.assertEqual(got, [(i, WORDS[i]["w"], WORDS[i]["s"]) for i in range(1, 5)])

@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class StageBleepTests(unittest.TestCase):
    """Stages a real clip and measures the audio inside and outside the bleep."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Project(Path(self.temp.name))
        self.project.ensure_dirs()
        self.src = Path(self.temp.name) / "src.mp4"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x568:rate=30:duration=3",
             "-f", "lavfi", "-i", "anoisesrc=color=pink:amplitude=0.3:sample_rate=48000:duration=3",
             "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(self.src)],
            check=True,
        )

    def stage(self, sound: str) -> Path:
        asset = {"id": "t", "kind": "video", "media": str(self.src), "transcript": None,
                 "probe": {"hasAudio": True, "width": 320, "height": 568, "duration": 3.0}}
        spec = {"in": 0.0, "out": 3.0, "speed": 1.0, "frame": "crop", "zoom": 1.0, "focusX": 0.5, "focusY": 0.5,
                "captions": "none", "bleep": {"sound": sound, "windows": [{"at": 1.0, "for": 1.0}], "pad": 0}}
        errors: list[str] = []
        check_bleep(spec, asset, errors, "s")
        self.assertEqual(errors, [])
        resolve_bleeps(spec, [])
        out = Path(self.temp.name) / f"{sound}.mp4"
        stage_video(self.project, Seg(index=0, kind="video", asset=asset, spec=spec, frames=90), out, 30)
        return out

    def level(self, path: Path, start: float, dur: float, extra: str = "") -> float:
        af = (extra + "," if extra else "") + "volumedetect"
        r = subprocess.run(["ffmpeg", "-v", "info", "-ss", str(start), "-t", str(dur), "-i", str(path), "-af", af, "-f", "null", "-"],
                           capture_output=True, text=True)
        line = next(x for x in r.stderr.splitlines() if "mean_volume" in x)
        return float(line.split("mean_volume:")[1].split()[0])

    def test_mute_silences_the_window(self):
        out = self.stage("mute")
        self.assertLess(self.level(out, 1.2, 0.6), -70)
        self.assertGreater(self.level(out, 0.3, 0.5), -40)

    def test_tone_replaces_the_window_with_a_1khz_beep(self):
        out = self.stage("tone")
        narrow = "bandpass=f=1000:width_type=q:w=8"
        inside, inside_1k = self.level(out, 1.2, 0.6), self.level(out, 1.2, 0.6, narrow)
        outside, outside_1k = self.level(out, 0.3, 0.5), self.level(out, 0.3, 0.5, narrow)
        self.assertLess(abs(inside - inside_1k), 1.5)  # all of it is the 1 kHz tone: the noise is gone
        self.assertGreater(outside - outside_1k, 10)  # the clip's own audio is broadband
        self.assertGreater(inside, -30)  # loud enough to hear over the mix


if __name__ == "__main__":
    unittest.main()
