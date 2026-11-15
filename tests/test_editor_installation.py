from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from reels.editor import installation as install


class EditorInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="editor-install-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "cycling"
        self.support = self.root / "Application Support/CyclingReelsEditor"
        for relative in ("scripts/reels", "tests", "remotion/src", "editor/ReelsEditor",
                         "editor/ReelsEditorTests", "editor/ReelsEditor.xcodeproj"):
            (self.source / relative).mkdir(parents=True)
        for relative in (*install._TOP_FILES, "scripts/reels/config.py", "tests/test_fixture.py",
                         "remotion/package.json", "remotion/src/index.ts", "editor/ReelsEditor/App.swift",
                         "editor/ReelsEditor.xcodeproj/project.pbxproj"):
            (self.source / relative).write_text("# fixture\n")

    def record(self):
        digest = install.source_digest(self.source)
        runtime = self.support / "runtime" / digest
        install.copy_runtime(self.source, runtime, digest)
        marker_path = runtime / install.RUNTIME_MARKER
        marker = json.loads(marker_path.read_text())
        marker["status"] = "ready"
        install._write_json(marker_path, marker)
        for relative in (".venv/bin/python", "remotion/node_modules/.bin/remotion",
                         "remotion/dist/editor-preview/index.html"):
            file = runtime / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("# installed fixture\n")
            file.chmod(0o700)
        workspace = self.support / "workspace"
        (workspace / "data/plans").mkdir(parents=True)
        (workspace / "state.json").write_text('{"version":1,"status":"active"}')
        (workspace / "data/plans/ride.json").write_text('{"segments":[]}')
        return {
            "version": 1, "repository": str(self.source), "runtime": str(runtime),
            "codeDigest": digest, "nativeDigest": install.source_digest(self.source, native=True),
            "app": str(self.support / "ReelsEditor.app"), "workspace": str(workspace),
        }

    def test_runtime_copies_only_source_not_live_data_credentials_or_dependencies(self):
        for relative in (".env", "plans/private.json", "library/media/original.mov",
                         "remotion/node_modules/private/package.json", "remotion/dist/bundle.js",
                         "scripts/reels/__pycache__/config.py", ".venv/private.py"):
            file = self.source / relative
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("DO NOT COPY")
        record = self.record()
        runtime = Path(record["runtime"])
        for relative in (".env", "plans", "library", ".venv/private.py",
                         "remotion/node_modules/private", "remotion/dist/bundle.js",
                         "scripts/reels/__pycache__"):
            self.assertFalse((runtime / relative).exists(), relative)
        self.assertEqual(install.source_digest(runtime), record["codeDigest"])
        self.assertTrue(os.access(runtime / "reels", os.X_OK))

    def test_newer_main_renderer_changes_produce_new_runtime_without_altering_old(self):
        first = self.record()
        (self.source / "remotion/src/index.ts").write_text("// newer main hook implementation\n")
        updated = install.source_digest(self.source)
        self.assertNotEqual(updated, first["codeDigest"])
        self.assertEqual(install.source_digest(Path(first["runtime"])), first["codeDigest"])

    def test_source_symlinks_and_private_install_locations_are_rejected(self):
        (self.source / "scripts/reels/link.py").symlink_to(self.source / "scripts/reels/config.py")
        with self.assertRaises(install.InstallationError):
            install.source_digest(self.source)
        with patch.dict(os.environ, {"CYCLING_EDITOR_HOME": str(self.source / "editor/data")}):
            with self.assertRaises(install.InstallationError):
                install.support_root(self.source)

    def test_runtime_marker_and_content_changes_fail_closed(self):
        record = self.record()
        runtime = Path(record["runtime"])
        install._assert_runtime(runtime, self.source, record["codeDigest"])
        (runtime / "scripts/reels/config.py").write_text("# changed\n")
        with self.assertRaises(install.InstallationError):
            install._assert_runtime(runtime, self.source, record["codeDigest"])

    def test_missing_dependencies_explain_how_to_repair_without_using_production(self):
        record = self.record()
        runtime = Path(record["runtime"])
        (runtime / ".venv/bin/python").unlink()
        with self.assertRaisesRegex(install.InstallationError, "reels-editor setup"):
            install._assert_runtime(runtime, self.source, record["codeDigest"])

    def test_launcher_passes_installed_runtime_and_private_plan_paths_with_spaces(self):
        record = self.record()
        args = install.launch_arguments(record, str(self.source / "plans/ride.json"))
        self.assertEqual(args[1:5], ["--repo", record["runtime"], "--workspace", record["workspace"]])
        self.assertEqual(args[-2:], ["--plan", "plans/ride.json"])
        for plan in ("../plans/ride.json", "library/ride.json", "/somewhere/ride.json", "plans/missing.json"):
            with self.subTest(plan=plan), self.assertRaises(install.InstallationError):
                install.launch_arguments(record, plan)
        with self.assertRaises(install.InstallationError):
            install.launch_arguments(record, workspace=self.source / "editor/data")

    def test_install_record_requires_setup_after_source_update_but_info_can_show_old_install(self):
        record = self.record()
        project = install._project_root(self.source, self.support)
        project.mkdir(parents=True)
        install._write_json(project / "install.json", record)
        self.assertEqual(install.installation(self.source, self.support), record)
        (self.source / "scripts/reels/config.py").write_text("# newer application\n")
        with self.assertRaises(install.InstallationError):
            install.installation(self.source, self.support)
        self.assertEqual(install.installation(self.source, self.support, current=False), record)

    def test_environment_does_not_use_production_python_overrides(self):
        with patch.dict(os.environ, {"PYTHONPATH": "production", "VIRTUAL_ENV": "production",
                                    "PIP_TARGET": "production", "NODE_PATH": "production"}):
            env = install._environment()
        for name in ("PYTHONPATH", "VIRTUAL_ENV", "PIP_TARGET", "NODE_PATH"):
            self.assertNotIn(name, env)
        self.assertEqual(env["REELS_EDITOR_RUNTIME"], "1")
        self.assertEqual(env["PIP_REQUIRE_VIRTUALENV"], "1")

    def test_shell_launcher_routes_only_real_editor_commands_before_loading_env(self):
        launcher = self.source / "reels"
        shutil.copyfile(Path(install.__file__).resolve().parents[3] / "reels", launcher)
        launcher.chmod(0o700)
        (self.source / ".venv/bin").mkdir(parents=True)
        python = self.source / ".venv/bin/python"
        python.write_text('#!/bin/sh\nprintf "production:%s\\n" "${ENV_LOADED:-no}"\n')
        python.chmod(0o700)
        editor = self.source / "editor/reels-editor"
        editor.write_text('#!/bin/sh\nprintf "runtime:%s\\n" "${ENV_LOADED:-no}"\n')
        editor.chmod(0o700)
        (self.source / ".env").write_text("ENV_LOADED=yes\nexport ENV_LOADED\n")
        env = dict(os.environ)
        env.pop("REELS_EDITOR_RUNTIME", None)
        env.pop("ENV_LOADED", None)
        for args in (["editor", "serve"], ["--editor-workspace", "/private/workspace", "editor", "info"]):
            result = subprocess.check_output([str(launcher), *args], env=env, text=True)
            self.assertEqual(result.strip(), "runtime:no")
        for args in (["search", "editor"], ["tag", "editor"], ["search", "--editor-workspace"]):
            result = subprocess.check_output([str(launcher), *args], env=env, text=True)
            self.assertEqual(result.strip(), "production:yes")

    def test_refresh_failure_does_not_change_selected_workspace(self):
        record = self.record()
        project = install._project_root(self.source, self.support)
        project.mkdir(parents=True)
        install._write_json(project / "install.json", record)
        with patch.object(install, "_run", side_effect=install.InstallationError("conflict")):
            with self.assertRaises(install.InstallationError):
                install.refresh(self.source, self.support)
        self.assertEqual(json.loads((project / "install.json").read_text()), record)

    def test_refresh_keeps_old_workspace_and_switches_only_after_success(self):
        record = self.record()
        project = install._project_root(self.source, self.support)
        project.mkdir(parents=True)
        install._write_json(project / "install.json", record)
        with patch.object(install, "_run") as run:
            updated = install.refresh(self.source, self.support, keep_private=True)
        args = run.call_args.args[0]
        self.assertIn("--keep-private", args)
        self.assertNotEqual(updated["workspace"], record["workspace"])
        self.assertTrue(Path(record["workspace"]).is_dir())
        self.assertEqual(json.loads((project / "install.json").read_text())["workspace"], updated["workspace"])


if __name__ == "__main__":
    unittest.main()
