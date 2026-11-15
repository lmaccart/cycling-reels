from __future__ import annotations

import json
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from helpers import TempProject
from reels.editor import Workspace, init_workspace
from reels import cli
from reels.util import ReelsError, write_json
from reels.editor import workspace as workspace_module
from reels.editor.protocol import ProtocolError
from reels import review as review_module
from reels.ingest import ingest
from reels.prune import apply_round


class EditorWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source_fixture = TempProject(self)
        self.source = self.source_fixture.root
        self.original_asset = self.source_fixture.asset("source-clip")
        self.plan = self.source_fixture.plan(
            "fixture-plan",
            [{"asset": "source-clip", "in": 0, "out": 2}],
        )
        self.workspace_path = self.base / "private" / "editor-workspace"

    def tearDown(self):
        self.source_fixture.temp.cleanup()

    def _snap(self, *, code_root=None):
        return init_workspace(self.source, self.workspace_path, code_root=code_root)

    def test_snapshot_is_private_allowlisted_and_maps_originals_read_only(self):
        write_json(self.source / "config.json", {
            "brand": {"handle": "@fixture"},
            "upload": {"api_key": "must-not-copy"},
            "custom": {"nested": {"access_token": "must-not-copy"}},
        })
        (self.source / ".env").write_text("TOKEN=do-not-copy\n", encoding="utf-8")
        (self.source / "out").mkdir(exist_ok=True)
        (self.source / "out" / "production.mp4").write_bytes(b"not copied")
        (self.source / "remotion").mkdir()
        (self.source / "remotion" / "source.tsx").write_text("not copied", encoding="utf-8")
        (self.source / "music").mkdir(exist_ok=True)
        (self.source / "music" / "fixture.mp3").write_bytes(b"fixture music")

        workspace = self._snap(code_root=self.base)
        self.assertEqual(workspace.root, self.workspace_path.resolve())
        self.assertEqual(workspace.data_root, workspace.root / "data")
        self.assertEqual(workspace.code_root, self.base.resolve())
        self.assertFalse((workspace.data_root / ".env").exists())
        self.assertFalse((workspace.data_root / "out").exists())
        self.assertFalse((workspace.data_root / "remotion").exists())
        self.assertEqual((workspace.data_root / "music/fixture.mp3").read_bytes(), b"fixture music")
        copied_config = json.loads((workspace.data_root / "config.json").read_text())
        self.assertEqual(copied_config["brand"]["handle"], "@fixture")
        self.assertNotIn("api_key", copied_config.get("upload", {}))
        self.assertNotIn("access_token", copied_config["custom"]["nested"])

        identifier = self.original_asset["media"]
        original = self.source / identifier
        self.assertEqual(workspace.project.abs(identifier), original.resolve())
        self.assertEqual(workspace.project.rel(original), identifier)
        private_plan = workspace.data_root / "plans/fixture-plan.json"
        self.assertEqual(workspace.project.abs(private_plan), private_plan)
        self.assertFalse((workspace.data_root / "library/media").exists())
        self.assertEqual(workspace.media_status()[identifier]["status"], "online")
        self.assertEqual(workspace.project.remotion_dir, self.base.resolve() / "remotion")
        with self.assertRaises(ReelsError):
            workspace.project.abs("../outside")
        with self.assertRaises(ReelsError):
            workspace.project.abs("library/media/not-registered.mp4")
        with self.assertRaises(ReelsError):
            workspace.project.rel(self.base / "outside")
        with self.assertRaises(ReelsError):
            workspace.project.abs(self.base / "outside")
        with self.assertRaises(ReelsError):
            workspace.project.assert_media_writable()

        # Workspace file payloads are ordinary copies, not hardlinks or symlinks.
        _assert_workspace_has_no_links(self, workspace.root)

    def test_ensure_dirs_never_creates_an_external_media_root(self):
        workspace = self._snap()
        media_root = workspace.project.media_dir
        shutil.rmtree(media_root)
        self.assertFalse(media_root.exists())
        workspace.project.ensure_dirs()
        self.assertFalse(media_root.exists())
        self.assertEqual(next(iter(workspace.media_status().values()))["status"], "offline")

    def test_registered_original_disappearance_and_identity_change_are_offline_or_changed(self):
        workspace = self._snap()
        identifier = self.original_asset["media"]
        original = self.source / identifier
        original.unlink()
        self.assertEqual(workspace.media_status()[identifier]["status"], "offline")
        original.write_bytes(b"different content")
        self.assertEqual(workspace.media_status()[identifier]["status"], "changed")
        with self.assertRaisesRegex(ReelsError, "identity changed"):
            workspace.project.abs(identifier)

    def test_missing_original_at_snapshot_stays_unusable_if_path_later_appears(self):
        identifier = self.original_asset["media"]
        original = self.source / identifier
        original.unlink()
        workspace = self._snap()
        self.assertEqual(workspace.media_status()[identifier]["status"], "offline")
        original.write_bytes(b"a replacement is not the recorded original")
        self.assertEqual(workspace.media_status()[identifier]["status"], "changed")
        with self.assertRaisesRegex(ReelsError, "previously offline"):
            workspace.project.abs(identifier)

    def test_invalid_reference_leaves_only_an_inactive_failed_snapshot(self):
        self.source_fixture.plan("dangling", [{"asset": "not-in-library", "in": 0, "out": 1}])
        with self.assertRaisesRegex(ReelsError, "retry initialization"):
            self._snap()
        self.assertFalse(self.workspace_path.exists())
        incomplete = list(self.workspace_path.parent.glob(".editor-workspace.incomplete-*"))
        self.assertEqual(len(incomplete), 1)
        state = json.loads((incomplete[0] / "state.json").read_text())
        self.assertEqual(state["status"], "failed")
        with self.assertRaisesRegex(ReelsError, "not active"):
            Workspace.load(incomplete[0])

    def test_symlink_in_snapshot_tree_fails_closed(self):
        learning = self.source / "learning"
        learning.mkdir()
        (learning / "escape").symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ReelsError, "symlink"):
            self._snap()
        self.assertFalse(self.workspace_path.exists())

    def test_nested_original_media_path_and_symlink_components(self):
        nested = self.source / "library/media/nested"
        nested.mkdir()
        original = self.source / self.original_asset["media"]
        moved = nested / original.name
        shutil.move(original, moved)
        self.original_asset["media"] = self.source_fixture.project.rel(moved)
        from reels.library import save_asset
        save_asset(self.source_fixture.project, self.original_asset)

        workspace = self._snap()
        self.assertEqual(workspace.project.abs(self.original_asset["media"]), moved.resolve())

    def test_nested_symlinked_original_directory_is_rejected(self):
        media_root = self.source / "library/media"
        other = media_root / "other"
        other.mkdir()
        original = self.source / self.original_asset["media"]
        moved = other / original.name
        shutil.move(original, moved)
        (media_root / "nested").symlink_to(other, target_is_directory=True)
        self.original_asset["media"] = "library/media/nested/" + original.name
        from reels.library import save_asset
        save_asset(self.source_fixture.project, self.original_asset)
        with self.assertRaisesRegex(ReelsError, "symlink"):
            self._snap()

    def test_replaced_original_symlink_is_changed_after_reopen_not_substituted(self):
        workspace = self._snap()
        original = self.source / self.original_asset["media"]
        replacement = self.base / "replacement.mp4"
        replacement.write_bytes(b"different media")
        original.unlink()
        original.symlink_to(replacement)
        reopened = Workspace.load(workspace.root)
        self.assertEqual(reopened.media_status()[self.original_asset["media"]]["status"], "changed")
        with self.assertRaisesRegex(ReelsError, "symlink"):
            reopened.project.abs(self.original_asset["media"])

    def test_asset_prefix_reference_and_extension_asset_id_validate_like_renderer(self):
        self.plan.write_text(json.dumps({
            "slug": "fixture-plan",
            "segments": [{"asset": "source"}],
            "extensionData": {"assetId": "not-a-library-reference"},
        }), encoding="utf-8")
        workspace = self._snap()
        self.assertTrue((workspace.data_root / "plans/fixture-plan.json").is_file())

    def test_implementation_files_are_not_copied_from_allowed_data_directories(self):
        learning = self.source / "learning"
        learning.mkdir()
        (learning / "unexpected.py").write_text("print('not data')", encoding="utf-8")
        with self.assertRaisesRegex(ReelsError, "implementation file is not snapshot data"):
            self._snap()

    def test_source_change_after_copy_prevents_activation(self):
        original_read = workspace_module._read_stable
        changed = False

        def read_then_change(path, expected):
            nonlocal changed
            payload = original_read(path, expected)
            if path.parent.name == "plans" and not changed:
                changed = True
                path.write_text('{"slug":"changed","segments":[]}', encoding="utf-8")
            return payload

        with mock.patch.object(workspace_module, "_read_stable", side_effect=read_then_change):
            with self.assertRaisesRegex(ReelsError, "changed during initialization"):
                self._snap()
        self.assertFalse(self.workspace_path.exists())
        incomplete = list(self.workspace_path.parent.glob(".editor-workspace.incomplete-*"))
        self.assertEqual(json.loads((incomplete[0] / "state.json").read_text())["status"], "failed")

    def test_activation_never_replaces_a_workspace_created_during_snapshot(self):
        publish = workspace_module._publish_without_replace

        def race_create_target(staged, destination):
            destination.mkdir()
            (destination / "user-file").write_text("keep", encoding="utf-8")
            publish(staged, destination)

        with mock.patch.object(workspace_module, "_publish_without_replace", side_effect=race_create_target):
            with self.assertRaisesRegex(ReelsError, "appeared during initialization"):
                self._snap()
        self.assertEqual((self.workspace_path / "user-file").read_text(), "keep")

    def test_invalid_json_prevents_activation(self):
        (self.source / "learning").mkdir()
        (self.source / "learning" / "broken.json").write_text("{", encoding="utf-8")
        with self.assertRaisesRegex(ReelsError, "invalid JSON"):
            self._snap()
        self.assertFalse(self.workspace_path.exists())

    def test_ordinary_project_path_defaults_are_preserved(self):
        project = self.source_fixture.project
        self.assertEqual(project.remotion_dir, project.root / "remotion")
        self.assertEqual(project.abs("plans/example.json"), project.root / "plans/example.json")
        absolute = self.base / "external.mp4"
        self.assertEqual(project.abs(absolute), absolute)
        self.assertEqual(project.rel(project.root / "plans/example.json"), "plans/example.json")

    def test_snapshot_includes_the_actual_library_progress_record(self):
        value = {"wkg": {"goal": 5, "entries": [{"date": "2026-11-07", "ftp": 300, "weightKg": 75, "source": "fixture"}]}}
        write_json(self.source / "library/progress.json", value)
        workspace = self._snap()
        self.assertEqual(json.loads((workspace.data_root / "library/progress.json").read_text()), value)

    def test_direct_media_mutators_fail_before_work(self):
        workspace = self._snap()
        self.assertFalse((workspace.data_root / "inbox").exists())
        with self.assertRaisesRegex(ReelsError, "read-only"):
            ingest(workspace.project)
        self.assertFalse((workspace.data_root / "inbox").exists())
        with self.assertRaisesRegex(ReelsError, "read-only"):
            apply_round(workspace.project, None, yes=True)

    def test_private_cache_links_and_failed_job_symlinks_do_not_block_workspace_load(self):
        workspace = self._snap()
        cache = workspace.root / "preview/cache.bin"
        cache.write_bytes(b"immutable private cache")
        (workspace.root / "jobs/cache.bin").hardlink_to(cache)
        (workspace.root / "jobs/failed-candidate").symlink_to(self.base, target_is_directory=True)
        reloaded = Workspace.load(workspace.root)
        self.assertEqual(reloaded.project.root, workspace.project.root)

    def test_existing_workspace_inside_original_source_is_rejected(self):
        workspace = self._snap()
        nested = self.source / "nested-workspace"
        shutil.copytree(workspace.root, nested)
        with self.assertRaisesRegex(ReelsError, "outside the snapshot source"):
            Workspace.load(nested)

    def test_authoritative_external_hardlink_and_symlink_escape_rejected(self):
        workspace = self._snap()
        private = workspace.data_root / "plans/fixture-plan.json"
        external = self.base / "external.json"
        external.hardlink_to(private)
        with self.assertRaisesRegex(ReelsError, "hard link outside"):
            Workspace.load(workspace.root)
        external.unlink()
        private.unlink()
        private.symlink_to(self.plan)
        with self.assertRaisesRegex(ReelsError, "symlink"):
            Workspace.load(workspace.root)

    def test_committed_history_is_checked_but_uncommitted_scratch_links_are_ignored(self):
        from reels.editor.transactions import DocumentStore
        from test_editor_transactions import fake_compile
        workspace = self._snap()
        store = DocumentStore(workspace, "plans/fixture-plan.json", compiler=fake_compile)
        (store.history / "uncommitted-scratch").symlink_to(self.base, target_is_directory=True)
        Workspace.load(workspace.root)
        external = self.base / "head-link"
        external.hardlink_to(store.head_path)
        with self.assertRaisesRegex(ReelsError, "hard link outside"):
            Workspace.load(workspace.root)

    def test_library_revision_covers_ancillary_inputs_and_caches_unchanged_json(self):
        workspace = self._snap()
        telemetry = workspace.data_root / "library/telemetry/large.json"
        telemetry.parent.mkdir(parents=True, exist_ok=True)
        telemetry.write_text(json.dumps({"samples": list(range(10000))}))
        original_read = workspace_module._read_stable
        reads = []
        def counted_read(path, expected):
            reads.append(path)
            return original_read(path, expected)
        with mock.patch.object(workspace_module, "_read_stable", side_effect=counted_read):
            initial = workspace.library_revision()
            self.assertEqual(workspace.library_revision(), initial)
            self.assertEqual(reads.count(telemetry), 1)
            telemetry.write_text(json.dumps({"samples": [2, 3, 4]}))
            self.assertNotEqual(workspace.library_revision(), initial)
            self.assertEqual(reads.count(telemetry), 2)
        for relative in ("sfx/sound.json", "exhibits/chart.json", "strava/activity.json", "progress.json"):
            path = workspace.data_root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            before = workspace.library_revision()
            path.write_text('{"value":1}')
            self.assertNotEqual(workspace.library_revision(), before, relative)
        media = workspace.data_root / "music/song.mp3"
        media.parent.mkdir(exist_ok=True)
        media.write_bytes(b"one")
        before = workspace.library_revision()
        media.write_bytes(b"two")
        self.assertNotEqual(workspace.library_revision(), before)

    def test_launcher_skips_env_for_editor_flag_and_editor_subcommand(self):
        fixture = self.base / "launcher fixture"
        (fixture / ".venv/bin").mkdir(parents=True)
        (fixture / "scripts").mkdir()
        launcher = fixture / "reels"
        shutil.copy2(Path(__file__).resolve().parents[1] / "reels", launcher)
        (fixture / ".env").write_text("EDITOR_LAUNCHER_SENTINEL=loaded\n", encoding="utf-8")
        python_stub = fixture / ".venv/bin/python"
        python_stub.write_text(
            "#!/bin/sh\n"
            "printf 'sentinel=%s\\n' \"${EDITOR_LAUNCHER_SENTINEL-unset}\"\n"
            "for arg in \"$@\"; do printf '<%s>\\n' \"$arg\"; done\n",
            encoding="utf-8",
        )
        python_stub.chmod(0o755)
        env = os.environ.copy()
        env.pop("EDITOR_LAUNCHER_SENTINEL", None)

        flagged = subprocess.run(
            ["bash", str(launcher), "--editor-workspace", "/private/work space", "editor", "serve"],
            check=True, capture_output=True, text=True, env=env,
        ).stdout
        self.assertIn("sentinel=unset", flagged)
        self.assertIn("</private/work space>", flagged)
        self.assertIn("<editor>", flagged)

        subcommand = subprocess.run(
            ["bash", str(launcher), "editor", "init"],
            check=True, capture_output=True, text=True, env=env,
        ).stdout
        self.assertIn("sentinel=unset", subcommand)

        ordinary = subprocess.run(
            ["bash", str(launcher), "doctor"],
            check=True, capture_output=True, text=True, env=env,
        ).stdout
        self.assertIn("sentinel=loaded", ordinary)

    def test_editor_init_does_not_construct_a_default_project(self):
        calls = []
        commands = _FakeEditorCommands(run=lambda project, args: calls.append(project) or 0)
        with mock.patch.object(cli.importlib, "import_module", return_value=commands), \
                mock.patch.object(cli, "Project") as project_factory:
            self.assertEqual(cli.main(["editor", "init"]), 0)
        project_factory.assert_not_called()
        self.assertEqual(calls, [None])

    def test_editor_workspace_errors_never_record_production_auto_events(self):
        workspace = self._snap()
        commands = _FakeEditorCommands(run=lambda project, args: (_ for _ in ()).throw(ReelsError("fixture error")))
        with mock.patch.object(cli.importlib, "import_module", return_value=commands), \
                mock.patch.object(review_module, "auto_event") as auto_event, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["--editor-workspace", str(workspace.root), "editor", "serve"]), 1)
        auto_event.assert_not_called()

    def test_editor_workspace_rejects_non_editor_dispatch_before_project_creation(self):
        commands = _FakeEditorCommands()
        with mock.patch.object(cli.importlib, "import_module", return_value=commands), \
                mock.patch.object(cli, "Project") as project_factory, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(
                cli.main(["--editor-workspace", str(self.workspace_path), "doctor"]),
                1,
            )
        project_factory.assert_not_called()


def _assert_workspace_has_no_links(case: unittest.TestCase, root: Path) -> None:
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            case.assertFalse(path.is_symlink(), str(path))
            if path.is_file():
                case.assertEqual(path.stat().st_nlink, 1, str(path))


class _FakeEditorCommands:
    ProtocolError = ProtocolError

    def __init__(self, run=None):
        self._run = run or (lambda project, args: 0)

    @staticmethod
    def register_parser(subparsers):
        editor = subparsers.add_parser("editor")
        sub = editor.add_subparsers(dest="editor_action", required=True)
        sub.add_parser("init")
        sub.add_parser("serve")

    def run(self, project, args):
        return self._run(project, args)


if __name__ == "__main__":
    unittest.main()
