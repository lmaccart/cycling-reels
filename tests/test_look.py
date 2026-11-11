from __future__ import annotations

import shutil
import subprocess
import unittest
from types import SimpleNamespace

from helpers import TempProject, words
from reels import look
from reels.render import build, load_plan
from reels.util import ReelsError, write_json

TALK = words(("so", 0.2, 0.4), ("this", 0.5, 0.7), ("is", 0.7, 0.8), ("my", 0.8, 1.0), ("week", 1.0, 1.4))


class SampleTests(unittest.TestCase):
    def test_a_frame_every_half_second_plus_the_last_and_the_events(self):
        got = look.sample(100, 30, 0.5, [(47, "cut 2"), (62, "text"), (20, "exhibit")])
        frames = [f for f, _ in got]
        self.assertEqual(frames[:4], [0, 15, 20, 30])
        self.assertEqual(frames[-1], 99)
        tags = dict(got)
        self.assertEqual((tags[20], tags[62]), ("exhibit", "text"))  # their own frames
        self.assertEqual(tags[45], "")
        self.assertIn(47, frames)  # a sample just before a cut would show the old shot: the cut gets its own frame
        self.assertEqual(dict(look.sample(100, 30, 0.5, [(58, "punch"), (59, "text")]))[60], "punch · text")  # shown on the next sample

    def test_events_follow_cuts_text_and_exhibits(self):
        segs = [SimpleNamespace(index=0, start=0, spec={"_cutaways": [{"start": 30, "seg": SimpleNamespace(frames=40)}], "_punches": []}),
                SimpleNamespace(index=1, start=120, spec={"_punches": [{"start": 150}]})]  # fmt: skip
        props = {"hook": {"endFrame": 70}, "beats": [{"startFrame": 90}], "exhibits": [{"startFrame": 130}], "stats": [],
                 "endCard": {"startFrame": 200}}  # fmt: skip
        got = look.events({"segs": segs, "props": props})
        self.assertEqual(got, [(32, "cutaway"), (64, "hook"), (72, "back"), (96, "text"), (122, "cut 2"), (140, "exhibit"),
                               (155, "punch"), (206, "end card")])  # fmt: skip

    def test_judge(self):
        self.assertEqual(look.judge({"kind": "speech", "over": 2.0}), "MUSIC CROWDS THE VOICE")
        self.assertEqual(look.judge({"kind": "speech", "over": 8.0}), "tight")
        self.assertEqual(look.judge({"kind": "speech", "over": 14.0}), "")
        self.assertEqual(look.judge({"kind": "nat", "over": 1.0}), "RIDE SOUND UNDER THE MUSIC")
        self.assertEqual(look.judge({"kind": "open", "over": -20.0}), "")  # music is meant to lead between lines
        self.assertEqual(look.judge({"kind": "speech", "over": None}), "")


class PiecesTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.t.asset("talk", duration=12, words=TALK)
        self.t.asset("ride", duration=20)

    def test_speech_nat_and_open_stretches_split_at_cuts(self):
        plan = self.t.plan("mix", [{"asset": "talk", "in": 0.0, "out": 3.0, "captions": "transcript"},
                                   {"asset": "ride", "in": 2.0, "out": 8.0, "nat": [{"at": 1.0, "for": 2.0, "volume": 2.0}]}])  # fmt: skip
        b = build(self.t.project, load_plan(self.t.project, plan))
        got = [(p["kind"], p["startFrame"], p["endFrame"], p["seg"], p["gain"]) for p in look.pieces(b)]
        speech = next(p for p in got if p[0] == "speech")
        self.assertEqual((speech[3], speech[4]), (0, 1.0))
        self.assertLess(speech[2], 90)
        self.assertIn(("nat", 120, 180, 1, 2.0), got)
        opens = [p for p in got if p[0] == "open"]
        self.assertEqual([(p[1], p[2], p[3]) for p in opens][-1], (180, 270, 1))
        self.assertTrue(all(p[0] != "open" or not (p[1] < 180 and 120 < p[2]) for p in got))  # nothing double-counted under the nat window

    def test_locate_wants_a_rendered_reel(self):
        with self.assertRaises(ReelsError) as cm:
            look.locate(self.t.project, "never-rendered")
        self.assertIn("render it first", str(cm.exception))
        out = self.t.project.out_dir / "done"
        out.mkdir(parents=True)
        (out / "reel.mp4").write_bytes(b"x")
        write_json(out / "plan.json", {"slug": "done"})
        self.assertEqual(look.locate(self.t.project, "done")[0::2], ("done", out / "plan.json"))
        self.assertEqual(look.locate(self.t.project, "out/done/reel.mp4")[0], "done")
        self.assertEqual(look.locate(self.t.project, str(self.t.plan("done", [])))[1], out / "reel.mp4")


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class RealFileTests(unittest.TestCase):
    def test_strips_and_levels_from_a_real_file(self):
        t = TempProject(self)
        out = t.project.out_dir / "tone"
        out.mkdir(parents=True)
        reel = out / "reel.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=270x480:rate=30:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-af", "volume='if(lt(t,1.5),1,0.1)':eval=frame",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(reel)], check=True)  # fmt: skip
        got = look.look(t.project, "tone")
        self.assertEqual([p.name for p in got["strips"]], ["strip-01.jpg"])
        self.assertTrue((out / "look" / "report.txt").read_text().startswith("tone  ·  3.0s"))
        self.assertIn("frames: 7 ", got["report"][1])
        loud, quiet = look.mean_db(reel, 0.0, 1.4), look.mean_db(reel, 1.6, 1.4)
        self.assertAlmostEqual(loud - quiet, 20, delta=1.5)
        mix = look.mix_stats(reel)
        self.assertGreater(mix["seconds"][0] - mix["seconds"][2], 15)


if __name__ == "__main__":
    unittest.main()
