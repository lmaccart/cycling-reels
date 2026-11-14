"""Encoder presets: Remotion's intermediate file is encoded fast, the final upload at the configured preset."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest import mock

from helpers import TempProject
from reels import render


class RenderPresetTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)
        self.p = self.t.project

    def remotion_cmd(self) -> list[str]:
        binary = self.p.remotion_dir / "node_modules" / ".bin" / "remotion"
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.touch()
        tmp = self.p.root / "out.mp4"
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            tmp.touch()
            return mock.Mock(returncode=0)

        with mock.patch.object(render.subprocess, "run", fake_run):
            render.run_remotion(self.p, Path("props.json"), self.p.root, tmp, self.p.root / "render.log")
        return seen["cmd"]

    def final_cmd(self) -> list[str]:
        seen = {}
        with mock.patch.object(render, "run", lambda cmd, **kw: seen.setdefault("cmd", [str(c) for c in cmd])), \
             mock.patch.object(render, "loudnorm_args", lambda p, s: []):  # fmt: skip
            render.final_encode(self.p, Path("in.mp4"), Path("reel.mp4"))
        return seen["cmd"]

    def test_remotion_intermediate_is_encoded_fast(self):
        self.assertIn("--x264-preset=veryfast", self.remotion_cmd())

    def test_remotion_preset_can_be_turned_off(self):
        self.p.config["render"]["remotionPreset"] = None
        self.assertFalse(any(c.startswith("--x264-preset") for c in self.remotion_cmd()))

    def test_final_encode_uses_configured_preset(self):
        cmd = self.final_cmd()
        self.assertEqual(cmd[cmd.index("-preset") + 1], "medium")
        self.p.config["render"]["preset"] = "slow"
        cmd = self.final_cmd()
        self.assertEqual(cmd[cmd.index("-preset") + 1], "slow")


if __name__ == "__main__":
    unittest.main()
