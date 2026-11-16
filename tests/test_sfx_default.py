from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import TempProject
from reels.config import DEFAULTS
from reels.editor.schema import _base_schema

ROOT = Path(__file__).resolve().parents[1]


class SfxDefaultTests(unittest.TestCase):
    """User 2026-11-16: no automatic sound effects unless they ask for them on a reel."""

    def test_automatic_sound_effects_are_off_by_default(self):
        self.assertIs(DEFAULTS["sfx"]["auto"], False)
        self.assertIs(json.loads((ROOT / "config.example.json").read_text())["sfx"]["auto"], False)
        self.assertIs(TempProject(self).project.config["sfx"]["auto"], False)

    def test_editor_shows_the_same_default(self):
        found = []

        def walk(node):
            if isinstance(node, dict):
                if node.get("key") == "autoSfx" or node.get("id") == "autoSfx":
                    found.append(node)
                for v in node.values():
                    walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(_base_schema())
        self.assertTrue(found)
        self.assertTrue(all(n.get("default") is False for n in found), found)


if __name__ == "__main__":
    unittest.main()
