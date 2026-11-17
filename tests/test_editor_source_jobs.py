"""Source (front/back) and 360 aim proxy jobs: one per asset, never superseded, always answered."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from helpers import TempProject
from reels.editor.service import EditorService
from reels.util import ReelsError


class FakeProxyManager:
    """prepare_source/prepare_sphere stand-ins: each call blocks until its asset is released."""

    def __init__(self, root: Path):
        self.root = root
        self.calls: list[str] = []
        self.entered: dict[str, threading.Event] = {}
        self.gates: dict[str, threading.Event] = {}
        self.outcomes: dict = {}

    def gate(self, aid: str) -> threading.Event:
        return self.gates.setdefault(aid, threading.Event())

    def _wait(self, aid: str, context) -> None:
        self.calls.append(aid)
        self.entered.setdefault(aid, threading.Event()).set()
        while not self.gate(aid).wait(0.02):
            context.check_cancelled()
        context.check_cancelled()

    def prepare_source(self, asset_id, view=None, context=None):
        self._wait(asset_id, context)
        outcome = self.outcomes.get(asset_id)
        if outcome is not None:
            return outcome()
        path = self.root / f"{asset_id}-proxy.mp4"
        path.write_bytes(b"proxy")
        return {"status": "ready", "assetId": asset_id, "path": str(path), "layout": "front-back"}

    def prepare_sphere(self, asset_id, start, end, stabilize=None, context=None):
        self._wait(asset_id, context)
        outcome = self.outcomes.get(asset_id)
        if outcome is not None:
            return outcome()
        path = self.root / f"{asset_id}-sphere.mp4"
        path.write_bytes(b"sphere")
        return {"status": "ready", "assetId": asset_id, "path": str(path), "start": start, "end": end}


def failing(message: str):
    def fail():
        raise ReelsError(message)
    return fail


class SourceJobTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        for aid in ("a", "b"):
            self.fixture.asset(aid)
        self.workspace = SimpleNamespace(root=self.fixture.root, data_root=self.fixture.root,
                                         code_root=Path(__file__).resolve().parents[1],
                                         project=self.fixture.project,
                                         media_status=lambda: [], library_revision=lambda: "library-fixture")
        self.events = []
        self.service = EditorService(self.workspace, lambda *args, **kwargs: self.events.append((args, kwargs)))
        self.addCleanup(self.service.close)
        self.manager = FakeProxyManager(self.fixture.root)
        self.service._preview_manager = self.manager
        self.addCleanup(lambda: [gate.set() for gate in self.manager.gates.values()])

    def source_events(self, aid):
        return [(args[0], args[1]) for args, _ in list(self.events)
                if args[0].startswith("source.") and args[1].get("assetId") == aid]

    def wait_for(self, name, aid, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for args, _ in list(self.events):
                if args[0] == name and args[1].get("assetId") == aid:
                    return args[1]
            time.sleep(0.01)
        self.fail(f"no {name} for {aid}; saw {[args[0] for args, _ in self.events]}")

    def wait_for_end(self, jid, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.service.jobs.status(jid)["state"] not in ("queued", "running"):
                return
            time.sleep(0.01)
        self.fail(f"job {jid} never ended")

    def entered(self, aid):
        self.assertTrue(self.manager.entered.setdefault(aid, threading.Event()).wait(5), f"{aid} never started")

    def test_two_assets_requested_back_to_back_both_complete(self):
        first = self.service.dispatch("source.prepare", {"id": "a"})
        second = self.service.dispatch("source.prepare", {"id": "b"})
        self.entered("a")
        self.entered("b")
        self.manager.gate("a").set()
        self.manager.gate("b").set()
        for aid, job in (("a", first), ("b", second)):
            ready = self.wait_for("source.ready", aid)
            self.assertTrue(ready["sourceURL"].startswith("http://127.0.0.1:"))
            self.assertEqual(ready["jobId"], job["id"])
            self.assertEqual([name for name, _ in self.source_events(aid)], ["source.ready"])

    def test_repeat_request_for_one_asset_joins_its_running_job(self):
        first = self.service.dispatch("source.prepare", {"id": "a"})
        self.entered("a")
        second = self.service.dispatch("source.prepare", {"id": "a"})
        self.assertEqual(second["id"], first["id"])
        self.assertTrue(second["joined"])
        self.manager.gate("a").set()
        self.wait_for("source.ready", "a")
        self.assertEqual(self.manager.calls, ["a"])
        self.assertEqual([name for name, _ in self.source_events("a")], ["source.ready"])
        self.wait_for_end(first["id"])
        # A finished job is not joined: the next request is new work.
        third = self.service.dispatch("source.prepare", {"id": "a"})
        self.assertNotEqual(third["id"], first["id"])
        self.assertNotIn("joined", third)

    def test_every_terminal_state_emits_one_source_event(self):
        def offline():
            return {"status": "offline", "assetId": "a", "reason": "The registered original is offline."}

        cases = (
            ("ready", None, "source.ready", None),
            ("unavailable", offline, "source.unavailable", None),
            ("failed", failing("ffmpeg fell over"), "source.error", "failed"),
            ("cancelled", None, "source.error", "cancelled"),
            ("timed out", None, "source.error", "timed_out"),
        )
        for label, outcome, event, state in cases:
            with self.subTest(label):
                self.events.clear()
                self.manager.gates.pop("a", None)
                self.manager.entered.pop("a", None)
                self.manager.outcomes["a"] = outcome
                with patch("reels.editor.service.SOURCE_JOB_TIMEOUT", 0.2 if label == "timed out" else 60.0):
                    job = self.service.dispatch("source.prepare", {"id": "a"})
                self.entered("a")
                if label == "cancelled":
                    self.service.dispatch("job.cancel", {"id": job["id"]})
                elif label != "timed out":
                    self.manager.gate("a").set()
                payload = self.wait_for(event, "a")
                self.assertEqual(payload["jobId"], job["id"])
                if state is not None:
                    self.assertEqual(payload["state"], state)
                    self.assertTrue(payload["message"])
                if label == "failed":
                    self.assertEqual(payload["message"], "ffmpeg fell over")
                self.wait_for_end(job["id"])
                self.assertEqual(len(self.source_events("a")), 1, self.source_events("a"))

    def test_a_retry_from_the_error_event_starts_fresh_work(self):
        retried = []

        def emit(*args, **kwargs):
            self.events.append((args, kwargs))
            if args[0] == "source.error" and not retried:
                self.manager.outcomes["a"] = None
                retried.append(self.service.dispatch("source.prepare", {"id": "a"}))

        self.service.emit = emit
        self.manager.outcomes["a"] = failing("sandbox hiccup")
        first = self.service.dispatch("source.prepare", {"id": "a"})
        self.entered("a")
        self.manager.gate("a").set()
        self.assertEqual(self.wait_for("source.error", "a")["message"], "sandbox hiccup")
        self.assertEqual(len(retried), 1)
        self.assertNotEqual(retried[0]["id"], first["id"])
        self.assertNotIn("joined", retried[0])
        self.wait_for("source.ready", "a")

    def test_document_edit_mid_job_keeps_source_and_sphere_results(self):
        revisions = {"document": "one", "library": "library-fixture"}
        self.service.document = SimpleNamespace(snapshot=lambda: {"revisions": dict(revisions)})
        try:
            source = self.service.dispatch("source.prepare", {"id": "a"})
            sphere = self.service.dispatch("sphere.prepare", {"id": "b", "start": 1, "end": 4, "requestId": "aim-1"})
            self.entered("a")
            self.entered("b")
            revisions["document"] = "two"
            self.service._changed({"documentId": "doc", "revisions": dict(revisions)}, prepare_preview=False)
            self.manager.gate("a").set()
            self.manager.gate("b").set()
            self.wait_for("source.ready", "a")
            self.assertEqual(self.wait_for("sphere.ready", "b")["requestId"], "aim-1")
            for job in (source, sphere):
                self.wait_for_end(job["id"])
                self.assertEqual(self.service.jobs.status(job["id"])["state"], "completed")
        finally:
            self.service.document = None

    def test_failed_sphere_job_answers_its_request(self):
        self.manager.outcomes["b"] = failing("decoder crashed")
        self.service.dispatch("sphere.prepare", {"id": "b", "start": 0, "end": 3, "requestId": "aim-2"})
        self.entered("b")
        self.manager.gate("b").set()
        payload = self.wait_for("sphere.unavailable", "b")
        self.assertEqual(payload["requestId"], "aim-2")
        self.assertEqual(payload["reason"], "decoder crashed")

    def test_service_start_removes_only_stale_partial_proxies(self):
        self.service.close()
        root = self.fixture.root
        hour_ago = time.time() - 3600

        def make(relative, old=True):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"partial")
            if old:
                os.utime(path, (hour_ago, hour_ago))
            return path

        stale = [make("preview/sources/k1/.source-dead.mp4"), make("preview/sources/k1/.source-dead.png"),
                 make("preview/spheres/k2/.sphere-dead.mp4")]
        kept = [make("preview/sources/k1/.source-encoding.mp4", old=False), make("preview/sources/k1/source.mp4"),
                make("preview/sources/k1/source.json"), make("preview/spheres/k2/sphere.mp4"),
                make("library/media/.source-original.mp4"), make("elsewhere/.source-x.mp4")]
        (root / "preview/sources/linked").symlink_to(root / "elsewhere", target_is_directory=True)

        reopened = EditorService(self.workspace, lambda *args, **kwargs: None)
        reopened.close()

        self.assertEqual([path for path in stale if path.exists()], [])
        self.assertEqual([path for path in kept if not path.exists()], [])


class RendererBaselineRaceTests(unittest.TestCase):
    def test_concurrent_jobs_share_one_new_baseline(self):
        from helpers import live_workspace
        from reels.editor.renderer import RendererManager

        workspace = live_workspace(TempProject(self))
        revision = RendererManager(workspace)._code_revision()
        copy = RendererManager._copy_baseline

        def slow_copy(manager, destination, **kwargs):
            time.sleep(0.3)  # widen the window two jobs used to collide in
            return copy(manager, destination, **kwargs)

        results, errors = [], []
        start = threading.Barrier(3)

        def resolve():
            start.wait()
            try:
                root = RendererManager(workspace).resolve_renderer(revision)
                # What a worker would run from at this moment: the finished copy, never a half-made one.
                results.append((root, (root.parent / "manifest.json").is_file()))
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        with patch.object(RendererManager, "_copy_baseline", slow_copy):
            threads = [threading.Thread(target=resolve) for _ in range(3)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(len({root for root, _ in results}), 1)
        self.assertEqual([complete for _, complete in results], [True, True, True])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class UnreadableTelemetryTests(unittest.TestCase):
    def test_proxies_survive_an_orientation_read_error(self):
        from reels.editor.worker import _stat_identity, prepare_source_proxy, prepare_sphere_proxy

        fixture = TempProject(self)
        media = fixture.project.media_dir / "pano.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=512x256:rate=30:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)  # fmt: skip
        workspace = fixture.root / "ws"
        workspace.mkdir()
        job = {
            "sourcePath": str(media), "sourceIdentity": _stat_identity(media), "sourceKind": "video",
            "probe": {"duration": 2.0, "sphere": {"projection": "equirect", "width": 512, "height": 256}},
            "mediaRoot": str(fixture.project.media_dir), "workspaceRoot": str(workspace), "assetId": "pano",
            "revisions": {"renderer": "r"},
        }
        with patch("reels.stabilize.read_orientation", side_effect=ReelsError("ffprobe failed")):
            source = prepare_source_proxy({**job, "view": {}, "revisionDir": str(workspace / "sources" / "fb")})
            sphere = prepare_sphere_proxy({**job, "start": 0.0, "end": 2.0, "stabilize": "medium",
                                           "revisionDir": str(workspace / "spheres" / "k")})
        self.assertEqual((source["status"], source["layout"]), ("ready", "front-back"), source)
        self.assertEqual(sphere["status"], "ready", sphere)
        self.assertFalse(sphere["hasOrientation"])
        self.assertIsNone(sphere["stabilization"])


if __name__ == "__main__":
    unittest.main()
