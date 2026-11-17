from __future__ import annotations

import json
import platform
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import TempProject
from reels import flags
from reels.editor.protocol import ProtocolError
from reels.editor.service import EditorService
from reels.util import ReelsError


class FlagTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.fixture.asset("clip", duration=60.0)
        self.fixture.asset("pic", kind="photo")
        self.project = self.fixture.project

    def test_add_list_update_remove(self):
        f = flags.add(self.project, "clip", 12, 40, "  sprinting   away ")
        self.assertEqual((f["asset"], f["in"], f["out"], f["note"]), ("clip", 12.0, 40.0, "sprinting away"))
        whole = flags.add(self.project, "clip")
        self.assertEqual((whole["in"], whole["out"]), (0.0, 60.0))
        photo = flags.add(self.project, "pic", note="cover?")
        self.assertNotIn("in", photo)
        self.assertEqual([x["id"] for x in flags.load(self.project)], [f["id"], whole["id"], photo["id"]])
        self.assertEqual(flags.update(self.project, f["id"], "")["id"], f["id"])
        self.assertNotIn("note", flags.load(self.project)[0])
        flags.unflag(self.project, whole["id"])
        self.assertEqual(len(flags.load(self.project)), 2)
        saved = json.loads((self.project.root / "flags.json").read_text())
        self.assertEqual(saved["version"], 1)

    def test_bad_flags_are_refused(self):
        with self.assertRaises(ReelsError):
            flags.add(self.project, "clip", 5, 5.05)
        with self.assertRaises(ReelsError):
            flags.add(self.project, "missing", 0, 2)
        with self.assertRaises(ReelsError):
            flags.add(self.project, "clip", 0, 2, "x" * 300)
        with self.assertRaises(ReelsError):
            flags.unflag(self.project, "f-nope")
        self.assertEqual(flags.add(self.project, "clip", 50, 99)["out"], 60.0)  # clamped to the clip

    @unittest.skipUnless(platform.system() == "Darwin", "the editor service's relay socket needs macOS")
    def test_service_round_trip_emits_changes(self):
        events = []
        workspace = SimpleNamespace(root=self.fixture.root, data_root=self.fixture.root,
                                    code_root=Path(__file__).resolve().parents[1], project=self.project,
                                    media_status=lambda: [], library_revision=lambda: "library-fixture")
        service = EditorService(workspace, lambda *a, **k: events.append(a))
        self.addCleanup(service.close)
        added = service.dispatch("flags.add", {"asset": "clip", "in": 1, "out": 3, "note": "look"})
        self.assertEqual(added["flags"][0]["note"], "look")
        self.assertEqual(events[-1][0], "flags.changed")
        self.assertEqual(len(service.dispatch("flags.list", {})["flags"]), 1)
        service.dispatch("flags.update", {"id": added["flag"]["id"], "note": "better"})
        self.assertEqual(service.dispatch("flags.list", {})["flags"][0]["note"], "better")
        service.dispatch("flags.remove", {"id": added["flag"]["id"]})
        self.assertEqual(service.dispatch("flags.list", {})["flags"], [])
        for bad in ({"asset": "../x"}, {"asset": "clip", "in": "nan", "out": 2}, {"asset": "clip", "in": 4, "out": 4}):
            with self.assertRaises(ProtocolError):
                service.dispatch("flags.add", bad)


if __name__ == "__main__":
    unittest.main()
