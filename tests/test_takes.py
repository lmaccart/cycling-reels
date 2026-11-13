from __future__ import annotations

import contextlib
import io
import json
import unittest

import numpy as np
from helpers import TempProject, words
from reels import cli
from reels.takes import build, parse_pick, silences, split, tighten
from reels.util import ReelsError


def say(text: str, start: float, each: float = 0.3) -> list[tuple[str, float, float]]:
    """Words spoken back to back from `start`, `each` seconds apiece."""
    return [(w, round(start + i * each, 2), round(start + (i + 1) * each, 2)) for i, w in enumerate(text.split())]


def tone(spans: list[tuple[float, float]], seconds: float, rate: int = 16000, floor: float = 0.001) -> np.ndarray:
    """Quiet noise with a loud tone during each span, like speech over a quiet room."""
    rng = np.random.default_rng(1)
    audio = rng.normal(0, floor, int(seconds * rate))
    t = np.arange(len(audio)) / rate
    for a, b in spans:
        on = (t >= a) & (t < b)
        audio[on] += 0.3 * np.sin(2 * np.pi * 220 * t[on])
    return audio.astype(np.float32)


class Phrases(unittest.TestCase):
    def test_splits_at_pauses_and_keeps_edges_clear_of_neighbours(self):
        ws = words(*say("one more time", 1.0), *say("here we go again", 3.0), ("now", 4.25, 4.5))
        a, b = build(ws, duration=10.0)[:2]
        self.assertEqual([a["text"], b["text"]], ["one more time", "here we go again now"])
        self.assertEqual((a["in"], a["out"]), (0.94, 2.05))  # 60 ms lead, 150 ms tail
        self.assertEqual(a["pauseAfter"], 1.1)
        self.assertEqual(b["out"], 4.65)

    def test_tail_never_runs_into_the_next_phrase_or_past_the_clip(self):
        ws = words(*say("first bit", 0.0), *say("second bit", 1.2))
        a, b = build(ws, pause=0.5, duration=1.85)
        self.assertEqual(a["out"], 0.75)
        self.assertEqual(b["out"], 1.85)
        tight = build(words(("a", 0.0, 0.5), ("b", 1.05, 1.5)), pause=0.5)
        self.assertEqual(tight[0]["out"], 0.65)
        self.assertEqual(tight[1]["in"], 0.99)

    def test_lower_pause_splits_finer(self):
        ws = words(("a", 0.0, 0.3), ("b", 0.6, 0.9), ("c", 2.0, 2.3))
        self.assertEqual(len(split(ws, 0.5)), 2)
        self.assertEqual(len(split(ws, 0.25)), 3)


class Takes(unittest.TestCase):
    def test_repeated_line_is_grouped_and_the_last_take_marked(self):
        line = "you will die stupid and slow"
        ws = words(*say(line, 0.0), *say(line, 5.0), *say("something else entirely here", 10.0), *say(line + " today", 15.0))
        ph = build(ws)
        self.assertEqual([(p["line"], p["take"], p["last"]) for p in ph], [("A", 1, False), ("A", 2, False), (None, None, None), ("A", 3, True)])

    def test_numbers_and_punctuation_dont_hide_a_retake(self):
        ws = words(*say("Did this to my 5 second power,", 0.0), *say("did this to my five second power", 6.0))
        self.assertEqual([p["line"] for p in build(ws)], ["A", "A"])

    def test_partial_repeat_is_an_overlap_with_a_cut_before_it(self):
        ws = words(*say("says you should take it too and double the dose most", 0.0),
                   *say("double the dose most recommend according to this study and that one", 6.0))  # fmt: skip
        a, b = build(ws)
        self.assertIsNone(a["line"])
        note = next(n for n in a["notes"] if "again in #2" in n)
        self.assertIn("'double the dose most'", note)
        self.assertIn("out 2.08", note)  # 'double' starts at 2.1 right after 'and': the cut stays 20 ms clear of it
        self.assertEqual(b["notes"], [])

    def test_far_apart_repeats_are_not_retakes(self):
        line = "see you next week"
        filler = [w for i in range(9) for w in say(f"topic number {i} is here", 5.0 + i * 4)]
        ph = build(words(*say(line, 0.0), *filler, *say(line, 60.0)))
        self.assertIsNone(ph[0]["line"])
        self.assertIsNone(ph[-1]["line"])


class Notes(unittest.TestCase):
    def test_fragment_trailing_word_filler_and_low_confidence(self):
        ws = words(("the", 0.0, 0.2), *say("this is um the whole point and", 2.0), *say("you know it works", 8.0))
        ws += words(*say("wind noise maybe words", 12.0), p=0.2)
        frag, trail, filler, shaky = build(ws)
        self.assertIn("fragment: likely a false start", frag["notes"])
        self.assertTrue(any(n.startswith("trails off on 'and': to end before it, out 3.78") for n in trail["notes"]), trail["notes"])
        self.assertTrue(any("'um' 2.60–2.90" in n for n in trail["notes"]))
        self.assertTrue(any("'you know' 8.00–8.60" in n for n in filler["notes"]))
        self.assertIn("low-confidence transcript: may be wind, not words", shaky["notes"])

    def test_last_phrase_ending_on_a_small_word_is_left_alone(self):
        ph = build(words(*say("that is what it comes down to", 0.0)))
        self.assertEqual(ph[0]["notes"], [])


class Audio(unittest.TestCase):
    def test_finds_the_pauses_between_loud_stretches(self):
        audio = tone([(1.0, 2.0), (3.0, 4.0)], 5.0)
        sil = silences(audio, words(("a", 1.0, 2.0), ("b", 3.0, 4.0)))
        self.assertEqual(len(sil), 3)
        self.assertAlmostEqual(sil[1][0], 2.0, delta=0.03)
        self.assertAlmostEqual(sil[1][1], 3.0, delta=0.03)

    def test_noisy_audio_gives_no_pauses(self):
        audio = tone([(1.0, 2.0)], 4.0, floor=0.2)
        self.assertIsNone(silences(audio, words(("a", 1.0, 2.0))))

    def test_stretched_word_is_pulled_back_and_reveals_the_pause(self):
        # Whisper ran 'day' on to 3.0 over a pause that starts at 1.6; 'next' follows at 3.0.
        ws = words(("every", 1.0, 1.3), ("day", 1.3, 3.0), ("next", 3.0, 3.4), ("line", 3.4, 3.8))
        sil = [(0.0, 1.0), (1.6, 3.0), (3.8, 5.0)]
        self.assertEqual(tighten(ws, sil)[1]["e"], 1.6)
        self.assertEqual(len(build(ws)), 1)
        a, b = build(ws, sil=sil, duration=5.0)
        self.assertEqual((a["text"], a["out"]), ("every day", 1.75))
        self.assertTrue(any("transcript ends 'day' at 3.00, the sound stops at 1.60" in n for n in a["notes"]))
        self.assertEqual(b["in"], 2.94)

    def test_edges_move_onto_the_sound(self):
        # The sound starts at 1.1 (Whisper said 1.0) and stops at 1.9 (Whisper said 2.0).
        ws = words(("hello", 1.0, 1.5), ("over", 1.5, 1.7), ("there", 1.7, 2.0))
        p = build(ws, sil=[(0.0, 1.1), (1.9, 4.0)], duration=4.0)[0]
        self.assertEqual((p["in"], p["out"]), (1.04, 2.05))
        self.assertEqual(p["notes"], [])  # out is past the transcript's word end: nothing clipped


class Command(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(self)
        self.project = self.tp.project
        line = "this is the line"
        self.tp.asset("talk", duration=20.0, words=words(*say(line, 1.0), *say(line, 5.0), *say("and then the ending comes", 9.0)))

    def run_cli(self, *argv: str) -> str:
        args = cli.build_parser().parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(args.fn(self.project, args), 0)
        return out.getvalue()

    def test_lists_phrases_with_takes(self):
        out = self.run_cli("takes", "talk", "--no-audio")
        self.assertIn("3 phrases", out)
        self.assertIn("word times only (--no-audio)", out)
        self.assertIn("[line A: take 1 of 2]", out)
        self.assertIn("[line A: ★ last take]", out)

    def test_json_and_range(self):
        data = json.loads(self.run_cli("takes", "talk", "--no-audio", "--json", "--start", "4"))
        self.assertEqual([p["n"] for p in data["phrases"]], [2, 3])
        self.assertEqual(data["phrases"][0]["last"], True)
        self.assertEqual(data["phrases"][0]["seconds"], 1.41)

    def test_pick_prints_plan_segments_in_the_order_given(self):
        segs = json.loads(self.run_cli("takes", "talk", "--no-audio", "--json", "--pick", "3,2"))
        self.assertEqual(segs, [{"asset": "talk", "in": 8.94, "out": 10.65, "captions": "transcript"},
                                {"asset": "talk", "in": 4.94, "out": 6.35, "captions": "transcript"}])  # fmt: skip

    def test_missing_media_falls_back_to_word_times(self):
        (self.project.media_dir / "talk.mp4").unlink()
        self.assertIn("word times only (the media file is gone)", self.run_cli("takes", "talk"))

    def test_errors(self):
        self.tp.asset("silent", duration=5.0)
        with self.assertRaisesRegex(ReelsError, "no transcript yet"):
            self.run_cli("takes", "silent")
        self.assertEqual(parse_pick("2,5-7,3", 8), [2, 5, 6, 7, 3])
        for bad in ("0", "9", "x", "5-2"):
            with self.assertRaises(ReelsError):
                parse_pick(bad, 8)


if __name__ == "__main__":
    unittest.main()
