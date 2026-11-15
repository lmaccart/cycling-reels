from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from reels.config import Project
from reels.editor.renderer import RendererManager, _source_path


class _Workspace:
    def __init__(self, root: Path, code_root: Path):
        self.root = root / "workspace"
        self.root.mkdir()
        self.data_root = self.root / "data"
        self.data_root.mkdir()
        self.code_root = code_root
        self.project = Project(self.data_root, code_root=code_root)
        self._originals = {}

    def library_revision(self):
        return "fixture-library-revision"


class EditorRendererTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.workspace = _Workspace(Path(self.tmp.name), Path(__file__).resolve().parents[1])
        self.manager = RendererManager(self.workspace)

    def test_allowlist_accepts_renderer_and_effect_sources_only(self):
        self.assertEqual(_source_path("remotion/src/components/SegmentView.tsx").as_posix(), "remotion/src/components/SegmentView.tsx")
        self.assertEqual(_source_path("scripts/reels/blur.py").as_posix(), "scripts/reels/blur.py")
        self.assertEqual(_source_path("scripts/reels/effects/new_effect.py").as_posix(), "scripts/reels/effects/new_effect.py")
        self.assertEqual(_source_path("tests/renderer_effects/test_new_effect.py").as_posix(), "tests/renderer_effects/test_new_effect.py")
        for path in (
            "../render.py",
            "/tmp/renderer.py",
            "scripts/reels/config.py",
            "scripts/reels/editor/worker.py",
            "scripts/reels/render.py",
            "scripts/reels/titlecard.py",
            "scripts/reels/vision.py",
            "remotion/editor-preview.tsx",
            "remotion/editor-preview.css",
            "remotion/editorStaticFile.ts",
            "remotion/src/editor-preview.tsx",
            "remotion/src/editor-preview.css",
            "remotion/package.json",
            "tests/test_editor_renderer.py",
            "scripts/reels/effects/../../config.py",
        ):
            with self.subTest(path=path), self.assertRaises(Exception):
                _source_path(path)

    def test_read_returns_only_allowlisted_utf8_source(self):
        result = self.manager.read("scripts/reels/blur.py")
        self.assertEqual(result["path"], "scripts/reels/blur.py")
        self.assertIn("def ", result["content"])
        with self.assertRaises(Exception):
            self.manager.read("scripts/reels/config.py")

    def test_submission_requires_frozen_snapshot_and_expected_renderer_revision(self):
        with self.assertRaisesRegex(Exception, "frozen"):
            self.manager.submit({"scripts/reels/blur.py": "pass"}, expected="r1")
        snapshot = {
            "plan": {"slug": "draft", "segments": []},
            "revisions": {"document": "d1", "library": "l1", "renderer": "r1"},
        }
        with self.assertRaisesRegex(Exception, "conflict"):
            self.manager.submit(
                {"scripts/reels/blur.py": "pass"},
                expected="r0",
                snapshot=snapshot,
            )

    def test_source_revision_paths_cannot_fall_back_when_missing(self):
        with self.assertRaisesRegex(Exception, "unavailable"):
            self.manager.read("scripts/reels/blur.py", revision="missing-renderer")

    def test_renderer_operation_batch_requires_applied_candidate_snapshot(self):
        snapshot = {
            "plan": {"slug": "draft", "segments": []},
            "revisions": {"document": "d1", "library": "l1", "renderer": "r1"},
        }
        with self.assertRaisesRegex(Exception, "candidateSnapshot"):
            self.manager.validate_candidate(
                {"scripts/reels/blur.py": "pass"},
                snapshot,
                operations=[{"op": "set_field", "path": "/caption", "value": "new"}],
            )


if __name__ == "__main__":
    unittest.main()
