from __future__ import annotations

import json
import math
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
from reels.editor.timing import _slice_keys, window_piece_keys, window_with_piece_keys
from reels.sphere import path_value, plan_camera, view_quat
from reels.stabilize import qmat, v360_matrix
from reels.library import save_asset


ANGLES = {"road": {"yaw": 90.0, "pitch": 25.0, "fov": 100.0, "roll": 0.0},
          "side": {"yaw": 160.0, "pitch": 25.0, "fov": 100.0, "roll": 90.0}}
GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "sphere_path_golden.json").read_text())
SPRINT = next(case["keys"] for case in GOLDEN["cases"] if case["name"].startswith("sprint zone: roll"))


def _turn(m1, m2) -> float:
    """Degrees of rotation between two rotation matrices."""
    trace = sum(m1[k][i] * m2[k][i] for i in range(3) for k in range(3))  # trace(m1ᵀ m2)
    return math.degrees(math.acos(max(-1.0, min(1.0, (trace - 1) / 2))))


def _wrapped(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


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

    def test_split_segment_camera_window_is_clamped_to_the_piece(self):
        window = {"durationSeconds": 4, "offsetSeconds": 1,
                  "path": [{"at": 0, "view": "road"}, {"at": 4, "view": "side"}]}
        ids = self.open([{"asset": "ride360", "in": 21, "out": 23, "cameraWindow": window}])
        cam = self.service.dispatch("sphere.camera", {"id": ids[0]})
        self.assertEqual([k["at"] for k in cam["path"]], [0.0, 2.0])
        full = [{"at": 0.0, **ANGLES["road"]}, {"at": 4.0, **ANGLES["side"]}]
        for key, t in zip(cam["path"], (1.0, 3.0)):  # boundary poses on the renderer's own path
            expected = path_value(full, t)
            for name in ("yaw", "pitch", "fov", "roll"):
                self.assertAlmostEqual(key[name], expected[name], places=9)
        errors: list[str] = []
        plan_camera({"path": cam["path"]}, {"angles": ANGLES}, None, cam["seconds"], errors, [], "clip")
        self.assertEqual(errors, [])  # written back by an aim edit, the compiler accepts them

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

    def test_split_boundary_pose_is_on_the_rendered_path(self):
        right = _slice_keys(SPRINT, 10.0, 15.167, rebase=True)
        expected = path_value(SPRINT, 10.0)
        self.assertEqual(right[0]["at"], 0.0)
        for name in ("yaw", "pitch", "fov", "roll"):
            self.assertAlmostEqual(right[0][name], expected[name], places=9)
        self.assertEqual([k["at"] for k in right[1:]], [0.5, 2.867, 5.167])

    def test_blur_keys_still_slice_linearly(self):
        keys = [{"at": 0, "x": 0.0, "w": 0.2}, {"at": 2, "x": 1.0, "w": 0.4}]
        self.assertEqual(_slice_keys(keys, 0.5, 2, rebase=True)[0], {"at": 0.0, "x": 0.25, "w": 0.25})


class GoldenPathTests(unittest.TestCase):
    """tests/fixtures/sphere_path_golden.json: SphereMathTests asserts the same vectors in Swift."""

    def test_path_value_matches_the_golden_vectors(self):
        tol = GOLDEN["tolerance"]
        for case in GOLDEN["cases"]:
            for t, yaw, pitch, fov, roll in case["samples"]:
                with self.subTest(case=case["name"], t=t):
                    v = path_value(case["keys"], t)
                    self.assertLess(_wrapped(v["yaw"], yaw), tol)
                    self.assertLess(abs(v["pitch"] - pitch), tol)
                    self.assertLess(abs(v["fov"] - fov), tol)
                    self.assertLess(_wrapped(v["roll"], roll), tol)

    def test_every_move_takes_the_short_way(self):
        for case in GOLDEN["cases"]:
            keys = case["keys"]
            for arc in case["arcs"]:
                a, b = keys[arc["from"]], keys[arc["to"]]
                shortest = _turn(qmat(view_quat(a)), qmat(view_quat(b)))
                self.assertAlmostEqual(arc["degrees"], shortest, places=6)
                self.assertLessEqual(shortest, 180.0)
                frames = [path_value(keys, a["at"] + (b["at"] - a["at"]) * i / 400) for i in range(401)]
                mats = [v360_matrix(f["yaw"], f["pitch"], f["roll"]) for f in frames]
                travelled = sum(_turn(m1, m2) for m1, m2 in zip(mats, mats[1:]))
                with self.subTest(case=case["name"], arc=arc["from"]):
                    self.assertAlmostEqual(travelled, shortest, delta=1e-4)

    def test_sprint_zone_no_longer_rolls_most_of_a_turn(self):
        a, b = SPRINT[1], SPRINT[2]  # roll -95.47 -> 167.45 with pitch 35.63 -> 84.01
        def travelled(pose):
            mats = [v360_matrix(*pose(i / 400)) for i in range(401)]
            return sum(_turn(m1, m2) for m1, m2 in zip(mats, mats[1:]))
        def linear(u):  # the old per-component blend
            return tuple(a[k] + (b[k] - a[k]) * u for k in ("yaw", "pitch", "roll"))
        def slerped(u):
            v = path_value(SPRINT, a["at"] + (b["at"] - a["at"]) * u)
            return v["yaw"], v["pitch"], v["roll"]
        self.assertGreater(travelled(linear), 200.0)
        self.assertLess(travelled(slerped), 60.0)


class CameraWindowKeyTests(unittest.TestCase):
    def window(self):
        return {"offsetSeconds": 0.0, "durationSeconds": 8.0, "path": [dict(k, at=round(k["at"] - 7.833, 3)) for k in SPRINT]}

    def test_piece_keys_stay_inside_and_apart(self):
        path = self.window()["path"]  # keys at 0, 1.4, 2.667, 5.034, 7.334
        for offset, seconds in ((1.0, 3.0), (2.62, 2.0), (0.0, 7.334), (5.0, 0.05)):
            keys = window_piece_keys(path, offset, seconds)
            with self.subTest(offset=offset, seconds=seconds):
                if seconds < 0.1:
                    self.assertEqual(keys, [])
                    continue
                ats = [k["at"] for k in keys]
                self.assertEqual((ats[0], ats[-1]), (0.0, round(seconds, 6)))
                self.assertTrue(all(b - a >= 0.1 for a, b in zip(ats, ats[1:])), ats)
                errors: list[str] = []
                plan_camera({"path": keys}, {}, None, seconds, errors, [], "clip")
                self.assertEqual(errors, [])
        # 2.62 is 0.047 s before the 2.667 key: the boundary pose takes its place
        self.assertEqual([k["at"] for k in window_piece_keys(path, 2.62, 2.0)], [0.0, 2.0])

    def test_aim_edit_keys_merge_into_the_window(self):
        window = self.window()
        piece = {"asset": "a", "in": 11.0, "out": 14.0, "cameraWindow": window}
        edited = [{"at": 0, "yaw": 0, "pitch": 0, "fov": 70, "roll": 0},
                  {"at": 1.5, "yaw": 30, "pitch": 0, "fov": 70, "roll": 0},
                  {"at": 3, "yaw": 0, "pitch": 10, "fov": 70, "roll": 0}]
        window["offsetSeconds"] = 1.0
        merged = window_with_piece_keys(window, piece, edited)
        self.assertEqual([k["at"] for k in merged["path"]], [0.0, 1.0, 2.5, 4.0, 5.034, 7.334])
        self.assertEqual(merged["path"][2]["yaw"], 30)
        self.assertEqual(window["path"][1]["at"], 1.4)  # the input window is untouched
        errors: list[str] = []
        plan_camera({"path": merged["path"]}, {}, None, merged["durationSeconds"], errors, [], "clip")
        self.assertEqual(errors, [])
        self.assertIsNone(window_with_piece_keys({"offsetSeconds": 1.0, "durationSeconds": 8.0}, piece, edited))


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


@unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
class BenchSphereKeysTests(unittest.TestCase):
    def test_staged_frames_match_single_frame_views_and_not_the_old_blend(self):
        from reels.editor.bench_sphere_keys import run

        fixture = TempProject(self)
        media = fixture.project.media_dir / "pano.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=512x256:rate=30:duration=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)  # fmt: skip
        asset = fixture.asset("pano", duration=3.0, width=512, height=256, audio=False, media=media)
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 512, "height": 256}
        save_asset(fixture.project, asset)
        fixture.project.config["render"].update(width=144, height=256)
        plan = {"slug": "t", "segments": [{"asset": "pano", "in": 0, "out": 3, "path": [
            {"at": 0, "yaw": -12.89, "pitch": 35.63, "fov": 80, "roll": -95.47},
            {"at": 1.5, "yaw": -107.05, "pitch": 84.01, "fov": 80, "roll": 167.45},
            {"at": 2.9, "yaw": -24.65, "pitch": 47.35, "fov": 90, "roll": -105.51}]}]}
        report = run(fixture.project, plan, 0, fixture.root / "bench", start=0.5, end=2.95)  # a split piece
        self.assertTrue(report["pass"], report)
        self.assertEqual([r["label"].split()[0] for r in report["rows"]], ["mid", "key", "mid", "key"])
        for row in report["rows"]:
            if "linearPsnr" in row:  # the old per-component blend aims somewhere else entirely
                self.assertLess(row["linearPsnr"], row["psnr"] - 10, row)


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

    def test_aim_edit_on_a_split_piece_keeps_its_camera_window(self):
        from helpers import live_workspace
        from reels.editor.transactions import DocumentStore

        fixture = TempProject(self)
        asset = fixture.asset("ride360", duration=30.0, tags={
            "type": "group-ride", "shot": "pov", "quality": 4, "summary": "360", "angles": ANGLES, "moments": []})
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 4096, "height": 2048}
        save_asset(fixture.project, asset)
        window = {"offsetSeconds": 2.0, "durationSeconds": 6.0, "path": [
            {"at": 0, "yaw": 0, "pitch": 0, "fov": 90, "roll": 0}, {"at": 3, "yaw": 90, "pitch": 0, "fov": 90, "roll": 0},
            {"at": 6, "yaw": 0, "pitch": 0, "fov": 90, "roll": 0}]}
        fixture.plan("aim", [{"asset": "ride360", "in": 4, "out": 8, "cameraWindow": window,
                              "path": _slice_keys(window["path"], 2, 6, rebase=True)}])
        store = DocumentStore(live_workspace(fixture), "plans/aim.json")
        self.addCleanup(store.close)
        before = store.snapshot()
        seg = next(n for n in before["nodes"] if n["kind"] == "segment")
        edited = [{"at": 0, "yaw": 60, "pitch": 0, "fov": 90, "roll": 0},
                  {"at": 1, "yaw": -45, "pitch": 5, "fov": 80, "roll": 0},
                  {"at": 4, "yaw": 0, "pitch": 0, "fov": 90, "roll": 0}]
        result = store.apply({"expected": before["revisions"], "origin": "manual", "reason": "aim", "operations": [
            {"op": "set_field", "id": seg["id"], "path": "/path", "value": edited}]})
        self.assertFalse([d for d in result.get("diagnostics") or [] if d.get("severity") == "error"])
        raw = result["plan"]["segments"][0]
        self.assertEqual(raw["path"], edited)
        self.assertEqual([k["at"] for k in raw["cameraWindow"]["path"]], [0, 2.0, 3.0, 6.0])
        rendered = store._current_compiled["segs"][0]["spec"]["sphere"]["path"]
        for t in (0.0, 0.5, 1.0, 2.5, 4.0):  # the render follows the edit, not the old window keys
            self.assertAlmostEqual(path_value(rendered, t)["yaw"], path_value(edited, t)["yaw"], places=6)


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
