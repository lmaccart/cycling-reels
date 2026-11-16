from __future__ import annotations

import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import TempProject
from reels.editor import music_service
from reels.editor.protocol import ProtocolError
from reels.editor.service import EditorService
from reels.util import ReelsError, write_json


class MusicServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.project = self.fixture.project
        self.project.music_dir.mkdir(parents=True, exist_ok=True)
        (self.project.music_dir / "song.mp3").write_bytes(b"x")
        write_json(self.project.music_dir / "song.json", {
            "slug": "song", "file": self.project.rel(self.project.music_dir / "song.mp3"), "title": "Song",
            "artist": "Band", "duration": 120.0, "suggestedStart": 30.0, "source": "youtube",
            "url": "https://youtu.be/x", "credit": "music: Band - Song", "instagram": {"audio": "Song Band"}})
        (self.project.music_dir / "loose.mp3").write_bytes(b"y")

    def test_list_includes_trending_credit_and_untracked(self):
        tracks = {t["slug"]: t for t in music_service.list_tracks(self.project)["tracks"]}
        self.assertEqual(tracks["song"]["defaultStart"], 30.0)
        self.assertTrue(tracks["song"]["trending"])
        self.assertEqual(tracks["song"]["credit"], "music: Band - Song")
        self.assertTrue(tracks["song"]["path"].endswith("music/song.mp3"))
        self.assertFalse(tracks["loose"]["trending"])
        self.assertEqual(tracks["loose"]["source"], "file")

    def test_search_uses_youtube_search_and_adds_ids(self):
        calls = []
        def fake(project, query, *, limit, free_only):
            calls.append((query, limit, free_only))
            return [{"title": "A", "url": "https://www.youtube.com/watch?v=a"}, {"title": "no url", "url": None}]
        result = music_service.search(self.project, {"query": " pump it ", "limit": 3}, searcher=fake)
        self.assertEqual(calls, [("pump it", 3, False)])
        self.assertEqual(result["results"], [{"title": "A", "url": "https://www.youtube.com/watch?v=a",
                                              "id": "https://www.youtube.com/watch?v=a"}])
        with self.assertRaises(ProtocolError):
            music_service.search(self.project, {"query": "  "}, searcher=fake)

    def test_search_failure_is_protocol_error(self):
        def boom(*a, **k):
            raise ReelsError("yt-dlp missing")
        with self.assertRaises(ProtocolError) as error:
            music_service.search(self.project, {"query": "x"}, searcher=boom)
        self.assertEqual(error.exception.code, "music_search_failed")

    def test_add_only_takes_urls(self):
        for bad in ({}, {"url": "/Users/me/song.mp3"}, {"id": "ytsearch1:song"}):
            with self.assertRaises(ProtocolError):
                music_service.validate_add(bad)
        args = music_service.validate_add({"id": "https://youtu.be/z", "instagram": "Song Band"})
        self.assertEqual(args, {"source": "https://youtu.be/z", "instagram": "Song Band", "name": None})
        self.assertIsNone(music_service.validate_add({"url": "https://youtu.be/z"})["instagram"])

    def test_add_work_uses_music_add_and_logs_progress(self):
        logs = []
        context = SimpleNamespace(log=logs.append, check_cancelled=lambda: None)
        seen = {}
        def fake_add(project, source, **kwargs):
            seen.update(source=source, **kwargs)
            return {"slug": "new", "file": "music/new.mp3", "title": "New", "duration": 10}
        work = music_service.add_work(self.project, {"source": "https://youtu.be/z", "name": None, "instagram": None}, adder=fake_add)
        result = work(context)
        self.assertEqual(seen, {"source": "https://youtu.be/z", "name": None, "instagram": None})
        self.assertEqual(result["track"]["slug"], "new")
        self.assertTrue(any("%" in line for line in logs))

    def test_dispatch_methods(self):
        workspace = SimpleNamespace(root=self.fixture.root, data_root=self.fixture.root,
                                    code_root=Path(__file__).resolve().parents[1], project=self.project,
                                    media_status=lambda: [], library_revision=lambda: "r")
        events = []
        service = EditorService(workspace, lambda *a, **k: events.append((a, k)))
        self.addCleanup(service.close)
        self.assertEqual({t["slug"] for t in service.dispatch("music.list", {})["tracks"]}, {"song", "loose"})
        with patch("reels.music.search", return_value=[{"title": "A", "url": "https://youtu.be/a"}]):
            self.assertEqual(service.dispatch("music.search", {"query": "a"})["results"][0]["id"], "https://youtu.be/a")
        with patch("reels.music.add", return_value={"slug": "n", "file": "music/n.mp3"}) as add:
            job = service.dispatch("music.add", {"url": "https://youtu.be/a"})
            for _ in range(100):
                if service.jobs.status(job["jobId"])["state"] in ("completed", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(service.jobs.status(job["jobId"])["state"], "completed")
        add.assert_called_once()
        self.assertTrue(any(a and a[0] == "music.changed" for a, _ in events))


if __name__ == "__main__":
    unittest.main()
