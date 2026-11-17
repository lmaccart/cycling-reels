from __future__ import annotations

import json
import platform
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from helpers import TempProject
from reels.editor.document import ensure_node_ids
from reels.editor.protocol import ProtocolError
from reels.editor.service import EditorService
from reels.editor.timing import _slice_keys
from reels.library import save_asset


ANGLES = {"road": {"yaw": 90.0, "pitch": 25.0, "fov": 100.0, "roll": 0.0},
          "side": {"yaw": 160.0, "pitch": 25.0, "fov": 100.0, "roll": 90.0}}


class SphereCameraTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        asset = self.fixture.asset("ride360", duration=60.0, tags={
            "type": "group-ride", "shot": "pov", "quality": 4, "summary": "360", "angles": ANGLES,
            "moments": [{"id": "m-1", "in": 10.0, "out": 16.0, "label": "sprint", "view": "side"}],
        })
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 4096, "height": 2048}
        save_asset(self.fixture.project, asset)
        self.fixture.asset("flat")
        self.workspace = SimpleNamespace(root=self.fixture.root, data_root=self.fixture.root,
                                         code_root=Path(__file__).resolve().parents[1],
                                         project=self.fixture.project,
                                         media_status=lambda: [], library_revision=lambda: "library-fixture")
        self.service = EditorService(self.workspace, lambda *args, **kwargs: None)
        self.addCleanup(self.service.close)

    def open(self, segments: list[dict]) -> list[str]:
        plan = {"slug": "t", "segments": segments}
        nodes = ensure_node_ids(plan)
        self.service.document = SimpleNamespace(_current_raw_plan=lambda: plan)
        self.addCleanup(setattr, self.service, "document", None)
        by_locator = {entry["locator"]: nid for nid, entry in nodes.items()}
        return [by_locator[f"/segments/{i}"] for i in range(len(segments))]

    def test_named_views_and_moments_resolve_to_numbers(self):
        ids = self.open([
            {"asset": "ride360", "moment": "m-1", "stabilize": "strong"},
            {"asset": "ride360", "in": 20, "out": 26, "speed": 2,
             "path": [{"at": 0, "view": "road"}, {"at": 1.5, "view": "side", "cut": True}]},
        ])
        first = self.service.dispatch("sphere.camera", {"id": ids[0]})
        self.assertEqual(first["view"], ANGLES["side"])  # the moment's view
        self.assertEqual((first["in"], first["out"], first["seconds"]), (10.0, 16.0, 6.0))
        self.assertEqual(first["stabilize"], "strong")
        self.assertEqual(first["stabilizeSetting"], {"mode": "smooth", "seconds": 2.0})
        self.assertEqual(first["angles"], ANGLES)
        second = self.service.dispatch("sphere.camera", {"id": ids[1]})
        self.assertEqual(second["seconds"], 3.0)
        self.assertEqual([k["yaw"] for k in second["path"]], [90.0, 160.0])
        self.assertTrue(second["path"][1]["cut"])
        self.assertIsNone(second["stabilizeSetting"])

    def test_split_segment_camera_window_is_rebased(self):
        ids = self.open([{"asset": "ride360", "in": 21, "out": 23, "cameraWindow": {
            "durationSeconds": 4, "offsetSeconds": 1,
            "path": [{"at": 0, "view": "road"}, {"at": 4, "view": "side"}]}}])
        cam = self.service.dispatch("sphere.camera", {"id": ids[0]})
        self.assertEqual([k["at"] for k in cam["path"]], [-1.0, 3.0])

    def test_non_360_clips_are_refused(self):
        ids = self.open([{"asset": "flat", "in": 0, "out": 2}])
        with self.assertRaises(ProtocolError):
            self.service.dispatch("sphere.camera", {"id": ids[0]})
        with self.assertRaises(ProtocolError):
            self.service.dispatch("sphere.camera", {"id": "missing"})

    def test_prepare_validates_its_range_and_asset(self):
        with self.assertRaises(ProtocolError):
            self.service.dispatch("sphere.prepare", {"id": "../config", "start": 0, "end": 2})
        with self.assertRaises(ProtocolError):
            self.service.dispatch("sphere.prepare", {"id": "ride360", "start": 5, "end": 2})
        with self.assertRaises(ProtocolError):
            self.service.dispatch("sphere.prepare", {"id": "ride360", "start": "x", "end": 2})


class SliceCutTests(unittest.TestCase):
    def test_split_before_a_hard_cut_holds_the_previous_pose(self):
        keys = [{"at": 0, "yaw": 0.0}, {"at": 2, "yaw": 90.0, "cut": True}, {"at": 4, "yaw": 0.0}]
        left = _slice_keys(keys, 0, 1, rebase=False)
        self.assertEqual([k["yaw"] for k in left], [0.0, 0.0])
        right = _slice_keys(keys, 1, 4, rebase=True)
        self.assertEqual([(k["at"], k["yaw"], k.get("cut", False)) for k in right],
                         [(0.0, 0.0, False), (1.0, 90.0, True), (3.0, 0.0, False)])
        after = _slice_keys(keys, 3, 4, rebase=True)
        self.assertEqual([(k["yaw"], k.get("cut", False)) for k in after], [(45.0, False), (0.0, False)])


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class SphereProxyWorkerTests(unittest.TestCase):
    def test_equirect_proxy_is_made_once_and_reused(self):
        from reels.editor.worker import _stat_identity, prepare_sphere_proxy

        fixture = TempProject(self)
        media = fixture.project.media_dir / "pano.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=512x256:rate=30:duration=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)  # fmt: skip
        workspace = fixture.root / "ws"
        workspace.mkdir()
        job = {
            "sourcePath": str(media), "mediaRoot": str(fixture.project.media_dir), "workspaceRoot": str(workspace),
            "revisionDir": str(workspace / "spheres" / "k"), "sourceIdentity": _stat_identity(media),
            "assetId": "pano", "probe": {"duration": 3.0, "sphere": {"projection": "equirect", "width": 512, "height": 256}},
            "start": 0.0, "end": 2.0, "stabilize": "medium", "revisions": {"renderer": "r"},
        }
        first = prepare_sphere_proxy(job)
        self.assertEqual(first["status"], "ready", first)
        self.assertFalse(first["reused"])
        self.assertFalse(first["hasOrientation"])  # no GoPro telemetry: nothing to stabilize with
        self.assertIsNone(first["stabilization"])
        probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", first["path"]],
                                          capture_output=True, text=True, check=True).stdout)
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (2048, 1024))
        again = prepare_sphere_proxy({**job, "stabilize": "lock"})
        self.assertTrue(again["reused"])
        bad = prepare_sphere_proxy({**job, "stabilize": "wobbly"})
        self.assertEqual(bad["status"], "unsupported")
        outside = {**job, "revisionDir": str(fixture.root / "elsewhere")}
        with self.assertRaises(Exception):
            prepare_sphere_proxy(outside)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("sandbox-exec") and shutil.which("ffmpeg"),
                     "needs the macOS sandbox and ffmpeg")
class SphereProxySandboxTests(unittest.TestCase):
    def test_proxy_is_prepared_by_the_sandboxed_worker(self):
        from helpers import live_workspace
        from reels.editor.preview import PreviewManager

        fixture = TempProject(self)
        media = fixture.project.media_dir / "pano.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=512x256:rate=30:duration=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)  # fmt: skip
        asset = fixture.asset("pano", duration=3.0, media=media)
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 512, "height": 256}
        save_asset(fixture.project, asset)
        manager = PreviewManager(live_workspace(fixture))
        result = manager.prepare_sphere("pano", 0.5, 2.0, stabilize="medium")
        self.assertEqual(result["status"], "ready", result)
        self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual((result["start"], result["end"]), (0.0, 3.0))  # whole 5 s blocks, clamped to the clip
        self.assertTrue(manager.prepare_sphere("pano", 0.2, 2.5, stabilize="lock")["reused"])


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("sandbox-exec"), "needs the macOS sandbox")
class SphereEditTransactionTests(unittest.TestCase):
    """The edits the 360 aim mode sends are valid plan changes the real compiler accepts."""

    def test_aim_edits_compile(self):
        from helpers import live_workspace
        from reels.editor.transactions import DocumentStore

        fixture = TempProject(self)
        asset = fixture.asset("ride360", duration=30.0, tags={
            "type": "group-ride", "shot": "pov", "quality": 4, "summary": "360", "angles": ANGLES, "moments": []})
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 4096, "height": 2048}
        save_asset(fixture.project, asset)
        fixture.plan("aim", [{"asset": "ride360", "in": 2, "out": 8, "view": "road", "yaw": 10}])
        store = DocumentStore(live_workspace(fixture), "plans/aim.json")
        self.addCleanup(store.close)
        before = store.snapshot()
        seg = next(n for n in before["nodes"] if n["kind"] == "segment")
        path = [{"at": 0, "yaw": 90, "pitch": 25, "fov": 100, "roll": 0},
                {"at": 2.5, "yaw": 160, "pitch": 25, "fov": 100, "roll": 90, "cut": True},
                {"at": 5, "yaw": -150, "pitch": 10, "fov": 80, "roll": 90}]
        result = store.apply({"expected": before["revisions"], "origin": "manual", "reason": "aim", "operations": [
            {"op": "set_field", "id": seg["id"], "path": "/yaw", "value": None},
            {"op": "set_field", "id": seg["id"], "path": "/path", "value": path},
            {"op": "set_field", "id": seg["id"], "path": "/stabilize", "value": "strong"},
        ]})
        raw = result["plan"]["segments"][0]
        self.assertEqual(raw["path"], path)
        self.assertEqual(raw["stabilize"], "strong")
        self.assertFalse([d for d in result.get("diagnostics") or [] if d.get("severity") == "error"])
        seg = next(n for n in result["nodes"] if n["kind"] == "segment")
        again = store.apply({"expected": result["revisions"], "origin": "manual", "reason": "single view", "operations": [
            {"op": "set_field", "id": seg["id"], "path": "/path", "value": None},
            {"op": "set_field", "id": seg["id"], "path": "/view", "value": {"yaw": 20, "pitch": 0, "fov": 90, "roll": 0}},
            {"op": "set_field", "id": seg["id"], "path": "/stabilize", "value": None},
        ]})
        self.assertEqual(again["plan"]["segments"][0]["view"]["yaw"], 20)
        with self.assertRaises(Exception):  # the compiler still rejects keyframes closer than 0.1 s
            seg = next(n for n in again["nodes"] if n["kind"] == "segment")
            store.apply({"expected": again["revisions"], "origin": "manual", "reason": "bad", "operations": [
                {"op": "set_field", "id": seg["id"], "path": "/path",
                 "value": [{"at": 0, "yaw": 0}, {"at": 0.05, "yaw": 10, "cut": True}]}]})


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class SourceFrontBackTests(unittest.TestCase):
    def test_360_source_proxy_shows_front_and_back(self):
        from reels.editor.worker import _stat_identity, prepare_source_proxy

        fixture = TempProject(self)
        media = fixture.project.media_dir / "pano.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=512x256:rate=30:duration=2",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)  # fmt: skip
        workspace = fixture.root / "ws"
        workspace.mkdir()
        job = {
            "sourcePath": str(media), "sourceIdentity": _stat_identity(media), "sourceKind": "video",
            "probe": {"duration": 2.0, "sphere": {"projection": "equirect", "width": 512, "height": 256}},
            "revisionDir": str(workspace / "sources" / "fb"), "mediaRoot": str(fixture.project.media_dir),
            "workspaceRoot": str(workspace), "assetId": "pano", "revisions": {"renderer": "r"}, "view": {},
        }
        result = prepare_source_proxy(job)
        self.assertEqual((result["status"], result["layout"]), ("ready", "front-back"), result)
        probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", result["path"]],
                                          capture_output=True, text=True, check=True).stdout)
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["width"], video["height"]), (1440, 1280))
        marker = json.loads((workspace / "sources" / "fb" / "source.json").read_text())
        self.assertEqual([v["yaw"] for v in marker["views"]], [0.0, 180.0])  # no gravity data: plain front/back
        single = prepare_source_proxy({**job, "view": {"yaw": 90}, "revisionDir": str(workspace / "sources" / "one")})
        self.assertEqual(single["layout"], "single")
