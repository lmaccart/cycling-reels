from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reels import telemetry, transcribe
from reels.config import Project
from reels.library import load_asset, save_asset
from reels.util import read_json


class SlowJobsKeepTagsTests(unittest.TestCase):
    """Tags saved while a slow job (Whisper, GPS extraction) runs must survive that job's save."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.project = Project(Path(self.temp.name))
        self.project.ensure_dirs()
        self.asset = {"id": "clip", "kind": "video", "media": "library/media/clip.mp4", "status": "needs_tags",
                      "probe": {"hasAudio": True}, "audio": {"speechSeconds": 5}}
        save_asset(self.project, self.asset)

    def tag_meanwhile(self):
        on_disk = load_asset(self.project, "clip")
        on_disk.update(status="tagged", tags={"type": "cafe", "summary": "tagged mid-job"})
        save_asset(self.project, on_disk)

    def test_transcribe_keeps_tags_saved_during_transcription(self):
        def fake_transcribe(audio, **kwargs):
            self.tag_meanwhile()
            words = [SimpleNamespace(word=" hi", start=0.0, end=0.4, probability=0.9)]
            return [SimpleNamespace(start=0.0, end=0.4, text=" hi", words=words)], SimpleNamespace(language="en")

        model = SimpleNamespace(transcribe=fake_transcribe)
        with patch.object(transcribe, "load_model", return_value=model), \
                patch.object(transcribe, "load_audio_16k", return_value=[0.0]):
            transcribe.transcribe(self.project, ["clip"])

        saved = load_asset(self.project, "clip")
        self.assertEqual(saved["status"], "tagged")
        self.assertEqual(saved["tags"]["summary"], "tagged mid-job")
        self.assertEqual(saved["transcript"], "library/transcripts/clip.json")
        self.assertEqual(read_json(self.project.root / saved["transcript"])["words"][0]["w"], "hi")

    def test_telemetry_keeps_tags_saved_during_extraction(self):
        def fake_extract(path, video_start=0.0):
            self.tag_meanwhile()
            return {"samples": [], "summary": {"goodSeconds": 0}}

        with patch.object(telemetry, "extract", side_effect=fake_extract):
            telemetry.extract_asset(self.project, dict(self.asset))

        saved = load_asset(self.project, "clip")
        self.assertEqual(saved["tags"]["summary"], "tagged mid-job")
        self.assertEqual(saved["gpsSummary"], {"goodSeconds": 0})
        self.assertIsNone(saved["telemetry"])


if __name__ == "__main__":
    unittest.main()
