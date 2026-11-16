from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from helpers import TempProject
from reels.editor.bench import run_document_bench
from reels.editor.transactions import DocumentError


class Workspace:
    def __init__(self, project):
        self.project = project
        self.root = project.root
        self.data_root = project.root


def fake_compile(project, plan, overrides=None):
    fps = int(project.cfg("render", "fps"))
    segments = []
    for index, raw in enumerate(plan.get("segments") or []):
        source_in = float(raw.get("in", 0))
        source_out = float(raw.get("out", source_in + 5))
        frames = max(1, round((source_out - source_in) * fps))
        start = segments[-1]["start"] + segments[-1]["frames"] if segments else 0
        segments.append({
            "index": index, "kind": "video", "asset": {"id": raw.get("asset")},
            "spec": {**raw, "in": source_in, "out": source_out, "speed": raw.get("speed", 1)},
            "frames": frames, "trans": 0, "start": start, "cutaways": [],
        })
    return {
        "fps": fps, "total": segments[-1]["start"] + segments[-1]["frames"] if segments else 0,
        "segs": segments, "warnings": [], "props": {}, "sound": {"cues": []}, "loop": None,
    }


class EditorBenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.workspace = Workspace(self.temp.project)
        self.temp.asset("asset-a")
        self.temp.asset("asset-b")
        self.plan = self.temp.plan(
            "source-plan",
            [{"asset": "asset-a", "in": 0, "out": 5},
             {"asset": "asset-b", "in": 1, "out": 6}],
            text=[{"text": "bench overlay", "at": 1, "for": 1}],
        )

    def test_smoke_measures_all_paths_and_cleans_each_temp_plan_and_history(self):
        result = run_document_bench(self.workspace, self.plan, 1, compiler=fake_compile)

        self.assertEqual(result["runs"], 1)
        self.assertEqual(result["segments"], 2)
        self.assertEqual(result["snapshotNodes"], 3)
        self.assertEqual(
            set(result["summaryMs"]),
            {
                "open (new document)", "snapshot compile-cold (cache miss)",
                "snapshot cold-memory (disk cache hit)", "snapshot warm",
                "open + first snapshot", "reopen unchanged + first snapshot", "apply set_field",
                "apply reorder_segments", "apply trim_segment", "apply move_overlay", "undo",
            },
        )
        self.assertLess(result["deltaBytes"]["p95"], 5_000)
        self.assertFalse(result["diskCompileCacheEnabled"], "the injected compiler intentionally has no disk cache")
        self.assertFalse(list(self.temp.project.plans_dir.glob("zz-bench-*.json")))
        self.assertFalse(list((self.temp.project.root / ".cache/editor/live/history/documents").glob("*")))
        self.assertTrue(self.plan.is_file())

    def test_failure_during_open_still_removes_temp_plan_and_history(self):
        def fail_compile(*_args):
            raise DocumentError("validation_failed", "expected smoke-test failure")

        with self.assertRaises(DocumentError):
            run_document_bench(self.workspace, self.plan, 1, compiler=fail_compile)

        self.assertFalse(list(self.temp.project.plans_dir.glob("zz-bench-*.json")))
        self.assertFalse(list((self.temp.project.root / ".cache/editor/live/history/documents").glob("*")))
        self.assertTrue(self.plan.is_file())


if __name__ == "__main__":
    unittest.main()
