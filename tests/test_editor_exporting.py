from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from reels.editor.exporting import save_artifact
from reels.editor.http import identity
from reels.editor.protocol import ProtocolError


class ExportSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.production = self.base / "production"
        (self.production / "library/media").mkdir(parents=True)
        self.root = self.base / "editor"
        self.root.mkdir()
        self.source = self.root / "reel.mp4"
        self.source.write_bytes(b"finished-render")
        self.destination = self.base / "saved.mp4"
        self.workspace = SimpleNamespace(root=self.root, project=SimpleNamespace(media_dir=self.production / "library/media"))
        self.receipt = {"reelPath": str(self.source), "revisions": {"document": "frozen"},
                        "artifactIdentities": {"reelPath": list(identity(self.source))}}

    def test_copy_uses_frozen_completed_artifact_and_is_independent(self):
        result = save_artifact(self.workspace, self.receipt, str(self.destination))
        self.assertEqual(result["revisions"]["document"], "frozen")
        self.assertEqual(self.destination.read_bytes(), self.source.read_bytes())
        self.assertNotEqual(self.destination.stat().st_ino, self.source.stat().st_ino)
        self.assertEqual(self.destination.stat().st_nlink, 1)

    def test_production_and_symlinked_production_destination_are_denied(self):
        alias = self.base / "alias"
        alias.symlink_to(self.production, target_is_directory=True)
        for destination in (self.production / "reel.mp4", alias / "reel.mp4"):
            with self.assertRaises(ProtocolError) as error:
                save_artifact(self.workspace, self.receipt, str(destination))
            self.assertEqual(error.exception.code, "production_write_denied")
        self.assertFalse((self.production / "reel.mp4").exists())

    def test_replacement_requires_explicit_confirmation(self):
        self.destination.write_bytes(b"keep")
        with self.assertRaises(ProtocolError):
            save_artifact(self.workspace, self.receipt, str(self.destination))
        self.assertEqual(self.destination.read_bytes(), b"keep")
        save_artifact(self.workspace, self.receipt, str(self.destination), overwrite=True)
        self.assertEqual(self.destination.read_bytes(), b"finished-render")

    def test_changed_export_not_silently_copied(self):
        self.source.write_bytes(b"changed")
        with self.assertRaises(ProtocolError) as error:
            save_artifact(self.workspace, self.receipt, str(self.destination))
        self.assertEqual(error.exception.code, "artifact_changed")
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
