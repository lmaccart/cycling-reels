from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import TempProject
from reels.cli import build_parser, cmd_check
from reels.kino import MARKER, export_project, props_ts
from reels.util import ReelsError

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "needs ffmpeg")
class ProjectExportTests(unittest.TestCase):
    """./reels project: one reel as a standalone Remotion project (Kino / Remotion Studio)."""

    def setUp(self):
        self.t = TempProject(self)
        (self.t.root / "remotion").symlink_to(REPO / "remotion", target_is_directory=True)  # the template, read-only
        clip = self.t.project.media_dir / "clip.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=360x640:rate=30:duration=3",
                        "-f", "lavfi", "-i", "sine=frequency=330:duration=3", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", str(clip)], check=True)  # fmt: skip
        self.t.asset("clip", duration=3.0, width=360, height=640, media=clip)
        self.plan = self.t.plan("export-me", [{"asset": "clip", "in": 0.0, "out": 1.5},
                                              {"asset": "clip", "in": 1.5, "out": 3.0, "cutaways": [{"at": 0.2, "asset": "clip", "in": 0.0, "out": 0.8}]}],
                                hook={"text": "pov: editable"}, post={"caption": "x"})  # fmt: skip

    def test_folder_is_a_remotion_project_of_exactly_this_reel(self):
        dest = export_project(self.t.project, self.plan)
        self.assertEqual(dest, (self.t.project.out_dir / "export-me" / "project").resolve())
        for f in ("src/index.ts", "src/Root.tsx", "src/components/SegmentView.tsx", "package.json", "tsconfig.json", "README.md",
                  "props.json", MARKER, "public/seg0.mp4", "public/seg1.mp4", "public/cut1-0.mp4"):  # fmt: skip
            self.assertTrue((dest / f).exists(), f)
        props = json.loads((dest / "props.json").read_text())
        self.assertEqual(props["totalFrames"], 90)
        self.assertEqual(props["segments"][1]["cutaways"][0]["src"], "cut1-0.mp4")
        ts = (dest / "src/defaultProps.ts").read_text()
        self.assertIn('"text": "pov: editable"', ts)
        self.assertIn("export const defaultProps: ReelProps = {", ts)
        self.assertEqual(json.loads((dest / MARKER).read_text())["slug"], "export-me")
        # the template in the repo is untouched
        self.assertNotIn("export-me", (REPO / "remotion/src/defaultProps.ts").read_text())

    def test_reexport_replaces_its_own_files_and_keeps_node_modules(self):
        dest = export_project(self.t.project, self.plan)
        (dest / "node_modules").mkdir()
        (dest / "public" / "stale.mp4").write_bytes(b"old")
        export_project(self.t.project, self.plan)
        self.assertTrue((dest / "node_modules").is_dir())
        self.assertFalse((dest / "public" / "stale.mp4").exists())

    def test_refuses_a_folder_it_did_not_write(self):
        mine = self.t.root / "Desktop"
        mine.mkdir()
        (mine / "thesis.docx").write_bytes(b"precious")
        with self.assertRaises(ReelsError):
            export_project(self.t.project, self.plan, out=mine)
        self.assertTrue((mine / "thesis.docx").exists())


class PropsTsTests(unittest.TestCase):
    def test_props_literal_is_valid_typescript_shape(self):
        ts = props_ts("x", {"a": True, "b": None, "c": [1, 2], "d": "it's \"quoted\""})
        self.assertIn('"a": true', ts)
        self.assertIn('"b": null', ts)
        self.assertIn('"d": "it\'s \\"quoted\\""', ts)
        self.assertTrue(ts.rstrip().endswith("};"))


class CheckEveryPlanTests(unittest.TestCase):
    def setUp(self):
        self.t = TempProject(self)

    def test_check_keeps_going_after_a_broken_plan_and_fails_at_the_end(self):
        seen = []

        def fake(project, path):
            seen.append(path)
            if path.endswith("bad.json"):
                raise ReelsError("plan problems:\n  - segment 1: no asset matches 'x'")

        args = build_parser().parse_args(["check", "plans/bad.json", "plans/good.json"])
        with patch("reels.render.check_plan", side_effect=fake):
            self.assertEqual(cmd_check(self.t.project, args), 1)
        self.assertEqual(seen, ["plans/bad.json", "plans/good.json"])
        log = (self.t.root / "learning" / "feedback.jsonl").read_text()
        self.assertIn("check failed for 1 of 2 plan(s)", log)
        self.assertNotIn("no asset matches", log)  # raw errors stay out of the learning log

    def test_all_good_returns_zero(self):
        args = build_parser().parse_args(["check", "plans/a.json", "plans/b.json"])
        with patch("reels.render.check_plan"):
            self.assertEqual(cmd_check(self.t.project, args), 0)


if __name__ == "__main__":
    unittest.main()
