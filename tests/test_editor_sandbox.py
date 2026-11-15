from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from reels.config import Project
from reels.editor import preview
from reels.util import ReelsError


class EditorSandboxTests(unittest.TestCase):
    def setUp(self):
        checkout = Path(__file__).resolve().parents[1]
        (checkout / ".cache").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="editor-sandbox-", dir=checkout / ".cache")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.checkout = self.root / "checkout"
        self.workspace_root = self.checkout / "workspaces/editor"
        self.data_root = self.workspace_root / "data"
        self.media_root = self.root / "originals"
        self.source = self.workspace_root / "code/renderers/candidate/source"
        for base in (self.checkout, self.source):
            for relative in ("scripts", "tests", "remotion"):
                (base / relative).mkdir(parents=True)
        self.data_root.mkdir(parents=True)
        self.media_root.mkdir()
        self.original = self.media_root / "registered.bin"
        self.original.write_bytes(b"registered fixture original")
        self.workspace = types.SimpleNamespace(
            root=self.workspace_root,
            data_root=self.data_root,
            code_root=self.checkout,
            _originals={"library/media/registered.bin": {"path": str(self.original)}},
            project=Project(self.data_root, code_root=self.checkout, media_root=self.media_root),
        )
        self.manager = preview.PreviewManager(self.workspace)

    def test_installed_runtime_uses_its_own_browser_before_global_caches(self):
        local = self.checkout / "remotion/node_modules/.remotion/chrome-headless-shell/mac-arm64/chrome-headless-shell"
        local.parent.mkdir(parents=True)
        local.write_text("#!/bin/sh\nexit 0\n")
        local.chmod(0o700)
        with patch.object(preview.Path, "home", return_value=self.root / "unused-home"):
            self.assertEqual(preview._browser_executable(self.workspace), local.resolve())

    def _record_worker(self, revision="renderer-A", *, action="prepare", mode="interactive"):
        job_root = self.manager.worker_root / f"job-{len(list(self.manager.worker_root.iterdir()))}"
        job_root.mkdir()
        job = {
            "action": action,
            "mode": mode,
            "revisions": {"renderer": revision},
            "jobRoot": str(job_root),
            "jobPath": str(job_root / "job.json"),
            "resultPath": str(job_root / "result.json"),
            "revisionDir": str(self.manager.preview_root / "revisions" / job_root.name),
            "dataRoot": str(job_root / "data"),
        }
        Path(job["dataRoot"]).mkdir()
        real_which = shutil.which

        def run(*args, **kwargs):
            Path(job["resultPath"]).write_text("{}")

        with (
            patch.object(preview.platform, "system", return_value="Darwin"),
            patch.object(preview.shutil, "which", side_effect=lambda name: (
                "/usr/bin/sandbox-exec" if name == "sandbox-exec" else real_which(name)
            )),
            patch.object(preview, "_browser_executable", return_value=None),
            patch.object(preview, "_browser_preference_files", return_value=[]),
            patch.object(preview, "_executables", wraps=preview._executables) as executables,
            patch.object(preview, "_path_roots", wraps=preview._path_roots) as path_roots,
            patch.object(preview, "_probe_boundary"),
            patch.object(preview, "_run", side_effect=run),
        ):
            self.assertEqual(self.manager._run_worker(job, code_root=self.source), {})
        recorded = json.loads(Path(job["jobPath"]).read_text())
        return recorded, (job_root / "worker.sb").read_text(), executables, path_roots

    def test_prepare_interactive_has_browser_capability_for_worker_exact_fallback(self):
        job, profile, executables, _ = self._record_worker()
        self.assertEqual(job["mode"], "interactive")
        self.assertTrue(executables.call_args.kwargs["browser"])
        self.assertIn('(global-name "com.apple.windowserver.active")', profile)
        self.assertIn('(global-name-prefix "org.chromium.Chromium.MachPortRendezvousServer.")', profile)
        self.assertNotIn("(allow mach-lookup)", profile)
        self.assertNotIn("(allow process-exec)", profile)
        self.assertNotIn("(allow network*)", profile)

    def test_source_proxy_does_not_request_browser_capability(self):
        _, profile, executables, _ = self._record_worker(action="source_proxy", mode=None)
        self.assertFalse(executables.call_args.kwargs["browser"])
        self.assertNotIn("MachPortRendezvousServer", profile)

    @unittest.skipUnless(
        platform.system() == "Darwin" and shutil.which("sandbox-exec"),
        "macOS sandbox-exec is unavailable",
    )
    def test_document_compiler_uses_registered_reads_and_its_own_input_clone(self):
        from helpers import TempProject
        from reels.editor.transactions import DocumentStore
        from reels.editor.workspace import init_workspace

        fixture = TempProject(self)
        fixture.asset("registered")
        fixture.plan("registered", [{"asset": "registered", "in": 0, "out": 2}])
        workspace = init_workspace(
            fixture.root, self.root / "compiler-workspace", code_root=Path(__file__).resolve().parents[1])
        store = DocumentStore(workspace, "plans/registered.json")
        self.addCleanup(store.close)
        snapshot = store.snapshot()
        self.assertEqual(snapshot["durationFrames"], 60)
        self.assertEqual(store.reload_external()["revisions"], snapshot["revisions"])

    @unittest.skipUnless(
        platform.system() == "Darwin" and shutil.which("sandbox-exec") and shutil.which("node"),
        "macOS sandbox-exec and Node are required",
    )
    def test_node_browser_targets_do_not_search_unreadable_ancestor_config(self):
        job, profile, _, _ = self._record_worker()
        ancestor_config = self.source / "package.json"
        ancestor_config.write_text(json.dumps({"browserslist": ["not-a-valid-browser-target"]}))
        lookup_root = self.source / "remotion"
        (lookup_root / "package.json").write_text(json.dumps({
            "name": "isolated-node-fixture", "private": True, "type": "commonjs",
        }))
        env = preview._safe_env(self.workspace, Path(job["jobRoot"]), self.source, shutil.which("node"))
        self.assertEqual(env["BROWSERSLIST"], "defaults")
        self.assertEqual(Path(env["BROWSERSLIST_ROOT_PATH"]), lookup_root)
        module = Path(preview.__file__).resolve().parents[3] / "remotion/node_modules/browserslist"
        if not module.is_dir():
            self.skipTest("local rendering dependencies are not installed")
        script = "const b=require(process.argv[1]);if(!b(undefined,{path:process.argv[2]}).length)process.exit(12)"
        result = subprocess.run(
            ["sandbox-exec", "-p", profile, shutil.which("node"), "-e", script, str(module), str(lookup_root)],
            cwd=lookup_root, env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_cache_namespaces_are_stable_separate_and_only_grants_given_to_workers(self):
        active, active_profile, _, _ = self._record_worker("active")
        old_bundle = Path(active["bundleCache"]) / "old-bundle/index.html"
        old_bundle.parent.mkdir()
        old_bundle.write_text("active Program URL target")
        legacy = self.manager.bundle_cache / "legacy-bundle/index.html"
        legacy.parent.mkdir()
        legacy.write_text("legacy Program URL target")
        candidate, profile, _, roots = self._record_worker("candidate")
        repeat, _, _, _ = self._record_worker("active")
        for field, base, index in (
            ("cacheRoot", self.manager.cache_root, 4),
            ("bundleCache", self.manager.bundle_cache, 5),
        ):
            self.assertEqual(active[field], repeat[field])
            self.assertNotEqual(active[field], candidate[field])
            self.assertEqual(Path(candidate[field]).parent, base)
            self.assertEqual(roots.call_args.args[index], Path(candidate[field]))
            self.assertIn(f'(allow file-write* (subpath "{candidate[field]}"))', profile)
            self.assertNotIn(f'(subpath "{active[field]}")', profile)
            self.assertNotIn(f'(subpath "{base}")', profile)
            self.assertNotIn(f'(subpath "{base}")', active_profile)
        self.assertEqual(old_bundle.read_text(), "active Program URL target")
        self.assertEqual(legacy.read_text(), "legacy Program URL target")

    def test_cache_namespace_is_hash_derived_and_rejects_symlink_aliases(self):
        revision = "../../active/../renderer"
        roots = self.manager._renderer_cache_roots(revision)
        expected = "renderer-" + hashlib.sha256(revision.encode()).hexdigest()
        self.assertEqual([root.name for root in roots], [expected, expected])
        for value in (None, "", 7):
            with self.assertRaisesRegex(ReelsError, "exact source revision"):
                self.manager._renderer_cache_roots(value)
        alias = self.manager.cache_root / ("renderer-" + hashlib.sha256(b"alias").hexdigest())
        alias.symlink_to(roots[0], target_is_directory=True)
        with self.assertRaisesRegex(ReelsError, "symlink"):
            self.manager._renderer_cache_roots("alias")

    def test_trusted_reads_exclude_checkout_live_data_and_unregistered_media(self):
        dependencies = self.checkout / "dependencies/node_modules"
        dependencies.mkdir(parents=True)
        (self.source / "remotion/node_modules").symlink_to(dependencies, target_is_directory=True)
        job, _, _, roots = self._record_worker()
        reads, writes = preview._path_roots(*roots.call_args.args)
        for base in (self.checkout, self.source, Path(preview.__file__).resolve().parents[3]):
            for relative in ("scripts", "tests", "remotion"):
                self.assertIn(base / relative, reads)
        self.assertIn(dependencies, reads)
        self.assertIn(self.original, reads)
        self.assertIn(Path(job["jobRoot"]), reads)
        for blocked in (
            self.checkout / ".env",
            self.checkout / ".git/config",
            self.workspace_root / "chat/mcp-token",
            self.checkout / "other-workspace/chat/token",
            self.data_root / "library/assets/live.json",
            self.media_root / "unregistered.bin",
        ):
            self.assertFalse(any(blocked.is_relative_to(root) for root in reads), blocked)
            self.assertFalse(any(blocked.is_relative_to(root) for root in writes), blocked)
        # Even a boundary probe against the working checkout must not read it all.
        reads, _ = preview._path_roots(
            self.workspace, self.checkout, Path(job["jobRoot"]), Path(job["revisionDir"]),
            Path(job["cacheRoot"]), Path(job["bundleCache"]),
        )
        self.assertNotIn(self.checkout, reads)
        self.assertNotIn(self.data_root, reads)
        self.assertNotIn(self.media_root, reads)

    @unittest.skipUnless(
        platform.system() == "Darwin" and shutil.which("sandbox-exec"),
        "macOS sandbox-exec is unavailable",
    )
    def test_macos_denies_private_reads_and_other_renderer_cache_writes(self):
        active, _, _, _ = self._record_worker("active")
        candidate, profile, _, _ = self._record_worker("candidate")
        denied_reads = [
            self.checkout / ".env",
            self.checkout / ".git/config",
            self.workspace_root / "chat/mcp-token",
            self.checkout / "other-workspace/chat/token",
            self.data_root / "library/assets/live.json",
            self.media_root / "unregistered.bin",
            Path(active["bundleCache"]) / "active/index.html",
            self.manager.bundle_cache / "legacy/index.html",
        ]
        for path in denied_reads:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture secret")
        allowed_reads = [
            self.original,
            self.checkout / "scripts/trusted.py",
            self.checkout / "tests/contract.py",
            self.source / "scripts/candidate.py",
            Path(candidate["dataRoot"]) / "frozen.json",
            Path(preview.__file__).with_name("worker.py"),
        ]
        for path in allowed_reads[1:-1]:
            path.write_text("fixture input")
        own_writes = [
            Path(candidate["cacheRoot"]) / "staged.bin",
            Path(candidate["bundleCache"]) / "new-bundle.html",
            Path(candidate["jobRoot"]) / "scratch",
        ]
        denied_writes = [
            self.original,
            *denied_reads,
            Path(active["cacheRoot"]) / "staged.bin",
            Path(active["bundleCache"]) / "replace.html",
        ]
        payload = {
            "allowed_reads": list(map(str, allowed_reads)),
            "denied_reads": list(map(str, denied_reads)),
            "own_writes": list(map(str, own_writes)),
            "denied_writes": list(map(str, denied_writes)),
        }
        script = """
import json, pathlib, sys
cases = json.loads(sys.argv[1])
for name in cases['allowed_reads']:
    pathlib.Path(name).read_bytes()
for name in cases['denied_reads']:
    try:
        pathlib.Path(name).read_bytes()
    except PermissionError:
        pass
    else:
        raise AssertionError('read allowed: ' + name)
for name in cases['own_writes']:
    pathlib.Path(name).write_text('own namespace')
for name in cases['denied_writes']:
    try:
        pathlib.Path(name).write_text('must be denied')
    except PermissionError:
        pass
    else:
        raise AssertionError('write allowed: ' + name)
print('fixture denials verified')
"""
        profile_path = Path(candidate["jobRoot"]) / "worker.sb"
        self.assertEqual(profile_path.read_text(), profile)
        result = subprocess.run(
            [shutil.which("sandbox-exec"), "-f", str(profile_path), sys.executable,
             "-c", script, json.dumps(payload)],
            cwd=self.source,
            env=candidate["env"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixture denials verified", result.stdout)
        self.assertEqual(self.original.read_bytes(), b"registered fixture original")
        for path in denied_reads:
            self.assertEqual(path.read_text(), "fixture secret")


if __name__ == "__main__":
    unittest.main()
