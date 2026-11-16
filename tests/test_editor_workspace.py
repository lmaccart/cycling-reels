from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import TempProject, live_workspace
from reels import cli
from reels.config import Project
from reels.editor.workspace import Workspace
from reels.library import save_asset
from reels.util import ReelsError, write_json


class LiveWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.asset = self.fixture.asset("clip")
        self.workspace = live_workspace(self.fixture)
        self.repo = self.fixture.root.resolve()

    def test_live_roots_are_the_repo_and_state_is_private_under_cache(self):
        ws = self.workspace
        self.assertEqual(ws.data_root, self.repo)
        self.assertEqual(ws.root, self.repo / ".cache/editor/live")
        self.assertEqual(oct(ws.root.stat().st_mode & 0o777), "0o700")
        for name in ("history", "jobs", "preview", "chat", "code"):
            self.assertTrue((ws.root / name).is_dir())
        self.assertTrue((ws.root / "state.json").is_file())
        self.assertEqual(ws.project.root, self.repo)
        self.assertEqual(ws.project.media_dir, self.repo / "library/media")

    def test_originals_are_read_only_but_paths_behave_normally(self):
        project = self.workspace.project
        with self.assertRaises(ReelsError):
            project.assert_media_writable()
        self.assertEqual(project.abs("library/media/clip.mp4"), self.repo / "library/media/clip.mp4")
        self.assertEqual(project.rel(self.repo / "library/media/clip.mp4"), "library/media/clip.mp4")
        shutil.rmtree(project.media_dir)
        project.ensure_dirs()
        self.assertFalse(project.media_dir.exists(), "ensure_dirs must not recreate read-only media")

    def test_plain_projects_stay_writable(self):
        project = Project(self.repo)
        project.assert_media_writable()
        self.assertFalse(project.read_only_media)

    def test_registry_is_built_from_asset_records_and_follows_the_library(self):
        ws = self.workspace
        self.assertEqual(set(ws._originals), {self.asset["media"]})
        self.assertEqual(ws.media_status()[self.asset["media"]]["status"], "online")
        before = ws.library_revision()
        self.fixture.asset("second")
        self.assertIn("library/media/second.mp4", ws.media_status())
        self.assertNotEqual(ws.library_revision(), before)

    def test_offline_and_changed_originals(self):
        ws = self.workspace
        media = self.repo / self.asset["media"]
        media.write_bytes(b"different bytes now")
        self.assertEqual(ws.media_status()[self.asset["media"]]["status"], "changed")
        media.unlink()
        self.assertEqual(ws.media_status()[self.asset["media"]]["status"], "offline")

    def test_library_revision_ignores_thumbnails_and_plans_but_tracks_inputs(self):
        ws = self.workspace
        before = ws.library_revision()
        (self.repo / "library/thumbs").mkdir(exist_ok=True)
        (self.repo / "library/thumbs/x.jpg").write_bytes(b"jpg")
        self.fixture.plan("p", [])
        self.assertEqual(ws.library_revision(), before)
        (self.repo / "music").mkdir(exist_ok=True)
        write_json(self.repo / "music/song.json", {"slug": "song"})
        self.assertNotEqual(ws.library_revision(), before)

    def _wait_for_library_change(self, before, timeout=3):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.workspace.library_revision()
            if current != before:
                return current
            time.sleep(0.01)
        self.fail("library watcher did not publish the input change")

    def test_library_watcher_tracks_overwrite_add_delete_and_directory_move(self):
        ws = self.workspace
        self.assertTrue(ws.start_library_watcher(interval=0.02))
        self.addCleanup(ws.stop_library_watcher)
        initial = ws.library_revision()

        nested = self.repo / "library/transcripts/nested"
        nested.mkdir(parents=True)
        entry = nested / "words.json"
        entry.write_text('{"words":["before"]}\n', encoding="utf-8")
        added = self._wait_for_library_change(initial)

        entry.write_text('{"words":["after!"]}\n', encoding="utf-8")
        overwritten = self._wait_for_library_change(added)

        moved_dir = self.repo / "library/transcripts/renamed"
        nested.rename(moved_dir)
        moved = moved_dir / "words.json"
        relocated = self._wait_for_library_change(overwritten)

        moved.unlink()
        deleted = self._wait_for_library_change(relocated)
        self.assertNotEqual(len({initial, added, overwritten, relocated, deleted}), 1)

    def test_refresh_is_fresh_and_stopped_or_unavailable_watcher_falls_back(self):
        ws = self.workspace
        path = self.repo / "library/transcripts/fresh.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"value":"before"}\n', encoding="utf-8")
        before = ws.refresh_library_revision()
        before_generation = ws.library_generation()
        original_mtime = path.stat().st_mtime_ns
        path.write_text('{"value":"after!"}\n', encoding="utf-8")
        os.utime(path, ns=(original_mtime, original_mtime))
        after = ws.refresh_library_revision()
        self.assertNotEqual(after, before)
        self.assertGreater(ws.library_generation(), before_generation)
        self.assertIn("library/transcripts/fresh.json", ws.library_signatures())

        self.assertTrue(ws.start_library_watcher(interval=0.02))
        self.assertEqual(ws.library_revision(), after)
        ws.stop_library_watcher()
        path.write_text('{"value":"stopped"}\n', encoding="utf-8")
        stopped = ws.library_revision()
        self.assertNotEqual(stopped, after)

        with patch.object(threading.Thread, "start", side_effect=RuntimeError("watcher unavailable")):
            self.assertFalse(ws.start_library_watcher(interval=0.02))
        path.write_text('{"value":"fallback"}\n', encoding="utf-8")
        self.assertNotEqual(ws.library_revision(), stopped)

    def test_retired_stalled_watcher_cannot_publish_into_restarted_generation(self):
        ws = self.workspace
        self.assertTrue(ws.start_library_watcher(interval=60))
        old_signatures = ws.library_signatures()
        ws.stop_library_watcher()

        entered, release, finished = threading.Event(), threading.Event(), threading.Event()

        def stalled_signature():
            entered.set()
            release.wait(3)
            finished.set()
            return old_signatures

        try:
            with patch.object(ws, "_input_watch_signature", side_effect=stalled_signature):
                self.assertTrue(ws.start_library_watcher(interval=0.01))
                self.assertTrue(entered.wait(2))
                retired = ws._library_watch_thread
                added = self.repo / "library/transcripts/new-generation.json"
                added.write_text('{"words":["new"]}\n', encoding="utf-8")
                ws.stop_library_watcher(join_timeout=0.01)
                self.assertTrue(ws.start_library_watcher(interval=60))
                current_revision = ws.library_revision()
                current_signatures = ws.library_signatures()
                release.set()
                if retired is not None:
                    retired.join(2)
                self.assertTrue(finished.wait(2))
                self.assertEqual(ws.library_revision(), current_revision)
                self.assertEqual(ws._library_signatures, current_signatures)
                self.assertFalse(ws._library_revision_dirty)
        finally:
            release.set()
            ws.stop_library_watcher()

    def test_renderer_revision_follows_the_repo_source(self):
        ws = self.workspace
        first = ws.renderer_revision()
        self.assertRegex(first, r"^[0-9a-f]{24}$")
        self.assertEqual(ws.renderer_revision(), first)

    def test_requires_a_cycling_folder(self):
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaises(ReelsError):
                Workspace.live(empty)

    def test_missing_originals_are_not_created_by_syncing(self):
        write_json(self.repo / "library/assets/ghost.json", {
            "id": "ghost", "kind": "video", "media": "library/media/ghost.mp4", "probe": {}})
        self.assertEqual(self.workspace.media_status()["library/media/ghost.mp4"]["status"], "offline")
        self.assertFalse((self.repo / "library/media/ghost.mp4").exists())


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fixture = Path(self.temp.name) / "launcher fixture"
        (self.fixture / ".venv/bin").mkdir(parents=True)
        (self.fixture / "scripts").mkdir()
        self.launcher = self.fixture / "reels"
        shutil.copy2(Path(__file__).resolve().parents[1] / "reels", self.launcher)
        (self.fixture / ".env").write_text("EDITOR_LAUNCHER_SENTINEL=loaded\n", encoding="utf-8")
        python_stub = self.fixture / ".venv/bin/python"
        python_stub.write_text(
            "#!/bin/sh\n"
            "printf 'sentinel=%s\\n' \"${EDITOR_LAUNCHER_SENTINEL-unset}\"\n"
            "for arg in \"$@\"; do printf '<%s>\\n' \"$arg\"; done\n",
            encoding="utf-8",
        )
        python_stub.chmod(0o755)
        self.env = os.environ.copy()
        self.env.pop("EDITOR_LAUNCHER_SENTINEL", None)

    def run_launcher(self, *args):
        return subprocess.run(["bash", str(self.launcher), *args], check=True, capture_output=True,
                              text=True, env=self.env).stdout

    def test_editor_commands_use_the_repo_venv_and_never_source_env(self):
        output = self.run_launcher("editor", "serve")
        self.assertIn("sentinel=unset", output)
        self.assertIn("<editor>", output)
        self.assertIn("<serve>", output)
        self.assertNotIn("editor-workspace", output)

    def test_ordinary_commands_still_load_env(self):
        self.assertIn("sentinel=loaded", self.run_launcher("doctor"))
        # A search term named "editor" is not the subcommand.
        self.assertIn("sentinel=loaded", self.run_launcher("search", "editor"))

    def test_editor_workspace_option_is_gone(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--editor-workspace", "/x", "editor", "serve"])


if __name__ == "__main__":
    unittest.main()
