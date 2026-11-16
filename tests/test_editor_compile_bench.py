from __future__ import annotations

import platform
import shutil
import unittest
from unittest.mock import patch

from helpers import TempProject, live_workspace
from test_editor_bench import fake_compile, Workspace
from reels.editor.bench import run_document_bench


class WorkerRPCBenchTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.fixture.asset("a")
        self.fixture.asset("b")
        self.plan = self.fixture.plan("source", [
            {"asset": "a", "in": 0, "out": 5}, {"asset": "b", "in": 1, "out": 6},
        ], text=[{"text": "test", "at": 1, "for": 1}])

    def test_injected_compiler_is_explicitly_not_reported_as_worker_rpc(self):
        with patch("reels.editor.compile_worker.WarmCompilePool",
                   side_effect=AssertionError("injected smoke test started a real pool")):
            result = run_document_bench(Workspace(self.fixture.project), self.plan, 1, compiler=fake_compile)
        self.assertFalse(result["workerRPCEnabled"])
        self.assertIn("injected", result["workerRPCSkippedReason"])
        self.assertNotIn("compile warm (worker RPC)", result["summaryMs"])
        self.assertNotIn("coldBrokerOpenMs", result)

    @unittest.skipUnless(platform.system() == "Darwin" and shutil.which("sandbox-exec"), "macOS sandbox required")
    def test_real_worker_metric_is_prewarmed_and_broker_lifetime_is_cleaned_up(self):
        workspace = live_workspace(self.fixture)
        workspace.start_library_watcher()
        self.addCleanup(workspace.stop_library_watcher)
        result = run_document_bench(workspace, self.plan, 1)
        self.assertTrue(result["workerRPCEnabled"])
        self.assertGreater(result["coldBrokerOpenMs"], 0)
        samples = result["samplesMs"]["compile warm (worker RPC)"]
        self.assertEqual(len(samples), 1)
        self.assertGreater(samples[0], 0)
        self.assertLess(samples[0], 1000)
        self.assertEqual(result["summaryMs"]["compile warm (worker RPC)"]["p50"], samples[0])
        self.assertTrue(workspace._editor_compile_owner.closed)
        self.assertFalse(list(self.fixture.project.plans_dir.glob("zz-bench-*.json")))
