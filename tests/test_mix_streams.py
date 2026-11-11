from __future__ import annotations

import shutil
import subprocess
import unittest

from helpers import TempProject
from reels.mix import quiet_nat
from reels.render import Seg


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class NatStreamTests(unittest.TestCase):
    def test_quiet_nat_measures_the_audio_stream_the_renderer_uses(self):
        """iPhone clips can carry a second audio track (spatial audio). The reel uses the first one,
        so the warning has to measure the first one too — not whichever ffmpeg would pick by default."""
        t = TempProject(self)
        clip = t.project.media_dir / "two-tracks.mov"
        # first track: a very quiet stereo tone (what the reel plays); second: a loud 5.1 one flagged as the default, as on
        # an iPhone .mov (ffmpeg's own pick)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=180x320:rate=30:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=3", "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                        "-filter_complex", "[1:a]volume=0.004,aformat=channel_layouts=stereo[q];[2:a]aformat=channel_layouts=5.1[l]",
                        "-map", "0:v", "-map", "[q]", "-map", "[l]", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                        "-disposition:a:0", "0", "-disposition:a:1", "default", str(clip)], check=True)  # fmt: skip
        asset = t.asset("two-tracks", duration=3.0, media=clip)
        sg = Seg(index=0, kind="video", asset=asset, spec={"in": 0.0, "out": 3.0, "speed": 1.0}, frames=90)
        warnings: list[str] = []
        quiet_nat(t.project, [sg], [{"seg": 0, "startFrame": 15, "endFrame": 75, "volume": 1.0, "music": 0.2}], 30, warnings)
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("the ride sound is quiet", warnings[0])


if __name__ == "__main__":
    unittest.main()
