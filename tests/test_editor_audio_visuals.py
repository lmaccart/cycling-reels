from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest

from helpers import TempProject
from reels.editor import audio_visuals as av
from reels.editor.protocol import ProtocolError

HAS_FFMPEG = shutil.which("ffmpeg") is not None


class PeakTests(unittest.TestCase):
    def test_peaks_min_max_per_bucket(self):
        peaks = av.compute_peaks([0, 16384, -16384, 32767, 0, 0, 0, 0], 4)
        self.assertEqual(len(peaks), 4)
        self.assertAlmostEqual(peaks[0][1], 0.5, places=3)
        self.assertAlmostEqual(peaks[1][0], -0.5, places=3)
        self.assertAlmostEqual(peaks[1][1], 0.9999, places=3)

    def test_empty_and_short_input(self):
        self.assertEqual(av.compute_peaks([], 3), [[0.0, 0.0]] * 3)
        self.assertEqual(len(av.compute_peaks([100], 5)), 5)

    def test_cache_key_varies(self):
        t = TempProject(self)
        f = t.root / "a.bin"
        f.write_bytes(b"x")
        base = av.cache_key(f, 0, 1, 100, 40, "waveform")
        self.assertEqual(base, av.cache_key(f, 0, 1, 100, 40, "waveform"))
        for other in (av.cache_key(f, 0, 2, 100, 40, "waveform"), av.cache_key(f, 0, 1, 200, 40, "waveform"),
                      av.cache_key(f, 0, 1, 100, 40, "spectrogram"), av.cache_key(f, 0.5, 1, 100, 40, "waveform")):
            self.assertNotEqual(base, other)


@unittest.skipUnless(HAS_FFMPEG, "ffmpeg missing")
class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.tone = self.t.root / "tone.mp3"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
                        str(self.tone)], check=True)
        shutil.copy(self.tone, self.t.project.music_dir / "song.mp3")
        (self.t.root / "sfx").mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=880:duration=1",
                        str(self.t.root / "sfx/ding.wav")], check=True)
        self.snapshot = {"fps": 30, "plan": {"music": {"track": "song", "start": 1}}, "nodes": [
            {"id": "m", "kind": "music", "startFrame": 0, "endFrame": 60, "raw": {}},
            {"id": "s", "kind": "sfx", "startFrame": 0, "endFrame": 15, "raw": {"sound": "ding"}},
            {"id": "x", "kind": "text", "startFrame": 0, "endFrame": 15, "raw": {}}]}

    def test_music_resolution_uses_start_and_duration(self):
        src = av.resolve_source(self.t.project, self.snapshot, "m")
        self.assertTrue(src["path"].endswith("music/song.mp3"))
        self.assertEqual((src["start"], src["end"]), (1.0, 3.0))

    def test_sfx_and_missing(self):
        self.assertTrue(av.resolve_source(self.t.project, self.snapshot, "s")["path"].endswith("sfx/ding.wav"))
        with self.assertRaises(ProtocolError):
            av.resolve_source(self.t.project, self.snapshot, "x")
        with self.assertRaises(ProtocolError):
            av.resolve_source(self.t.project, self.snapshot, "nope")

    def test_waveform_peaks_and_cache(self):
        out = av.audio_visual(self.t.project, self.snapshot, {"nodeId": "m", "mode": "waveform", "width": 50})
        self.assertEqual(len(out["peaks"]), 50)
        self.assertGreater(max(p[1] for p in out["peaks"]), 0.05)
        cached = self.t.root / ".cache/editor/audio" / f"{out['key']}.json"
        self.assertTrue(cached.is_file())
        self.assertEqual(json.loads(cached.read_text())["peaks"], out["peaks"])
        self.assertEqual(av.audio_visual(self.t.project, self.snapshot,
                                         {"nodeId": "m", "mode": "waveform", "width": 50}), out)

    def test_spectrogram_png_in_cache(self):
        out = av.audio_visual(self.t.project, self.snapshot, {"nodeId": "m", "mode": "spectrogram", "width": 120, "height": 30})
        self.assertTrue(os.path.realpath(out["path"]).startswith(str((self.t.root / ".cache/editor/audio").resolve())))
        self.assertEqual(open(out["path"], "rb").read(4), b"\x89PNG")

    def test_bad_mode_and_outside_path(self):
        with self.assertRaises(ProtocolError):
            av.audio_visual(self.t.project, self.snapshot, {"nodeId": "m", "mode": "bogus"})
        with self.assertRaises(ProtocolError):
            av.audio_visual(self.t.project, None, {"source": {"path": "/etc/hosts"}, "mode": "waveform"})


if __name__ == "__main__":
    unittest.main()
