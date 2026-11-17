from __future__ import annotations

import json
import math
import platform
import random
import shutil
import subprocess
import threading
import traceback
import unittest
from pathlib import Path

from helpers import TempProject, live_workspace
from reels.editor.transactions import DocumentStore
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
    m = [[sum(m1[k][i] * m2[k][j] for k in range(3)) for j in range(3)] for i in range(3)]
    sine = math.sqrt(sum((m[i][j] - m[j][i]) ** 2 for i, j in ((0, 1), (1, 2), (2, 0)))) / 2
    cosine = (sum(m[i][i] for i in range(3)) - 1) / 2
    return math.degrees(math.atan2(sine, cosine))


def _wrapped(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


class SphereCameraTests(unittest.TestCase):
    def setUp(self):
        self.background_errors = []
        previous_hook = threading.excepthook
        threading.excepthook = lambda args: self.background_errors.append(
            "".join(traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)))
        def restore_hook():
            threading.excepthook = previous_hook
            self.assertEqual(self.background_errors, [], "\n".join(self.background_errors))
        self.addCleanup(restore_hook)
        self.fixture = TempProject(self)
        asset = self.fixture.asset("ride360", duration=60.0, tags={
            "type": "group-ride", "shot": "pov", "quality": 4, "summary": "360", "angles": ANGLES,
            "moments": [{"id": "m-1", "in": 10.0, "out": 16.0, "label": "sprint", "view": "side"}],
        })
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 4096, "height": 2048}
        save_asset(self.fixture.project, asset)
        self.fixture.asset("flat")
        self.workspace = live_workspace(self.fixture)
        self.stores = []
        self.service = EditorService(self.workspace, lambda *args, **kwargs: None)
        def close_documents():
            self.service.close()  # join the real watcher before closing retired documents
            for store in self.stores:
                store.close()
        self.addCleanup(close_documents)

    def open(self, segments: list[dict], **extra) -> list[str]:
        path = self.fixture.plan(f"camera-{len(self.stores)}", segments, **extra)
        store = DocumentStore(self.workspace, path)
        self.stores.append(store)
        self.service.document = store
        nodes = ensure_node_ids(store._current_raw_plan())
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

    def test_split_playback_matches_render_at_interior_frames_and_boundary_cuts(self):
        from reels import render

        for cut in (False, True):
            keys = [{"at": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 90},
                    {"at": 4, "yaw": 90, "pitch": 0, "roll": 0, "fov": 100, "cut": cut}]
            segment = {"asset": "ride360", "in": 21, "out": 24, "cameraWindow": {
                "durationSeconds": 4, "offsetSeconds": 1, "path": keys}}
            node_id = self.open([segment])[0]
            cam = self.service.dispatch("sphere.camera", {"id": node_id})
            rendered = render.build(self.fixture.project, {"slug": "split", "segments": [segment]})["segs"][0].spec["sphere"]
            for n in range(91):
                t = n / 30
                viewer = path_value(cam["playbackPath"], t + cam["playbackOffsetSeconds"])
                expected = path_value(rendered["path"], t)
                for name in ("yaw", "pitch", "roll", "fov"):
                    self.assertAlmostEqual(viewer[name], expected[name], places=9)
            if cut:
                self.assertTrue(cam["path"][-1]["cut"])
                self.assertEqual(path_value(cam["path"], 2.99)["yaw"], 0)
            else:
                self.assertGreater(abs(path_value(cam["path"], 0.5)["yaw"]
                                       - path_value(cam["playbackPath"], 1.5)["yaw"]), 1)

    def edit_piece(self, segment, *, store=None):
        """Real service camera → full aim batch → sandboxed transaction compiler."""
        if store is None:
            self.open([segment])
            store = self.service.document
        before = store.snapshot()
        node = next(n for n in before["nodes"] if n["kind"] == "segment")
        camera = self.service.dispatch("sphere.camera", {"id": node["id"]})
        self.assertEqual(camera["errors"], [])
        edited = [dict(k) for k in camera["path"]]
        self.assertGreaterEqual(len(edited), 2)
        edited[0]["yaw"] = (edited[0]["yaw"] + 25 + 180) % 360 - 180
        result = store.apply({"expected": before["revisions"], "origin": "manual", "reason": "aim", "operations": [
            *[{"op": "set_field", "id": node["id"], "path": "/" + name, "value": None}
              for name in ("yaw", "pitch", "fov", "roll")],
            {"op": "set_field", "id": node["id"], "path": "/path", "value": edited}]})
        raw = result["plan"]["segments"][0]
        self.assertEqual(raw["path"], edited)
        self.assertIn("cameraWindow", raw)
        merged = raw["cameraWindow"]
        shift = merged["offsetSeconds"] - segment["cameraWindow"]["offsetSeconds"]
        self.assertAlmostEqual(merged["durationSeconds"] - shift,
                               max(segment["cameraWindow"]["durationSeconds"],
                                   segment["cameraWindow"]["offsetSeconds"] + camera["seconds"]))
        self.assertFalse([d for d in result.get("diagnostics", []) if d.get("severity") == "error"])
        # Exterior keys retain both their physical source times and complete poses.
        lo = segment["cameraWindow"]["offsetSeconds"]
        hi = lo + camera["seconds"]
        for key in camera["playbackPath"]:
            if key["at"] < lo - 0.101 or key["at"] > hi + 0.101:
                matches = [k for k in merged["path"] if abs(k["at"] - key["at"] - shift) < 1e-9]
                self.assertEqual(len(matches), 1)
                self.assertEqual({k: v for k, v in key.items() if k != "at"},
                                 {k: v for k, v in matches[0].items() if k != "at"})
        return camera, raw

    def test_fractional_window_aim_transaction_keeps_100ms_intervals(self):
        window = {"durationSeconds": 1, "offsetSeconds": 0.0625,
                  "path": [{"at": t, "yaw": 10 * i} for i, t in enumerate((0, 0.2, 0.4, 0.5, 1))]}
        camera, raw = self.edit_piece({"asset": "ride360", "in": 2, "out": 3.2, "speed": 2,
                                      "cameraWindow": window})
        self.assertEqual([k["at"] for k in camera["path"]], [0, 0.138, 0.338, 0.438, 0.6])
        times = [round(k["at"] * 1000) for k in raw["cameraWindow"]["path"]]
        self.assertTrue(all(b - a >= 100 for a, b in zip(times, times[1:])), times)

    def test_shorthand_and_sparse_windows_preserve_exterior_camera_keys(self):
        for path in (["front", "back"], ["road", "side"],
                     [{"pitch": 15}, {"roll": 80, "view": "front"}]):
            with self.subTest(path=path):
                self.edit_piece({"asset": "ride360", "moment": "m-1", "in": 11, "out": 14,
                                 "roll": 25, "fov": 85, "cameraWindow": {
                                     "durationSeconds": 6, "offsetSeconds": 1.0625, "path": path}})

    def apply_operations(self, operations):
        store = self.service.document
        return store.apply({"expected": store.snapshot()["revisions"], "origin": "manual",
                            "reason": "camera regression", "operations": operations})

    def write_camera(self, node_id, keys):
        return self.apply_operations([
            *[{"op": "set_field", "id": node_id, "path": "/" + name, "value": None}
              for name in ("yaw", "pitch", "fov", "roll")],
            {"op": "set_field", "id": node_id, "path": "/path", "value": keys}])

    def assert_pose(self, actual, expected):
        self.assertLess(_turn(qmat(view_quat(actual)), qmat(view_quat(expected))), 1e-7)
        self.assertAlmostEqual(actual["fov"], expected["fov"], places=9)

    def assert_split_playback(self, result, original, *, window_path, window_duration, offset=0):
        """Check persisted edits, the renderer's camera and the live viewer independently."""
        from reels import render

        self.assertFalse([d for d in result.get("diagnostics", []) if d.get("severity") == "error"])
        store = self.service.document
        snapshot = store.snapshot()
        rendered = render.build(self.fixture.project, result["plan"])
        nodes = {n["startFrame"]: n for n in snapshot["nodes"] if n["kind"] == "segment"}
        for raw, piece in zip(result["plan"]["segments"], rendered["segs"]):
            start = piece.start / snapshot["fps"]
            window = raw["cameraWindow"]
            self.assertEqual(window["path"], window_path)  # no moved/dropped playback keys
            self.assertEqual(window["durationSeconds"], window_duration)
            self.assertAlmostEqual(window["offsetSeconds"], offset + start, places=12)
            camera = self.service.dispatch("sphere.camera", {"id": nodes[piece.start]["id"]})
            self.assertEqual(camera["errors"], [])
            seconds = camera["seconds"]
            for keys in (raw["path"], camera["path"]):
                # Decimal source bounds can differ from the same ms tick by a few ulps.
                self.assertTrue(all(0 <= k["at"] <= seconds + 1e-12 for k in keys), (seconds, keys))
                ticks = [round(k["at"] * 1000) for k in keys]
                self.assertTrue(all(b-a >= 100 for a, b in zip(ticks, ticks[1:])), ticks)
                errors = []
                plan_camera({"path": keys or None}, {}, None, seconds, errors, [], "editable piece")
                self.assertEqual(errors, [])
            # Every frame, including each boundary and its neighbors. The endpoint
            # also checks hard cuts landing exactly at the end of the left piece.
            for frame in range(piece.frames + 1):
                t = frame / snapshot["fps"]
                expected = path_value(original, start + t)
                self.assert_pose(path_value(piece.spec["sphere"]["path"], t), expected)
                self.assert_pose(path_value(camera["playbackPath"], camera["playbackOffsetSeconds"] + t),
                                 expected)

    def test_transaction_cuts_just_before_and_after_sprint_keys_preserve_playback(self):
        # Shared Python/Swift golden fixture: real sprint key at 10.5 s (frame 315).
        # First reproduce the successful 18.933 s cut, then cut one frame either
        # side of 10.5 s and again next to the following key (12.867 s).
        for placed in (False, True):
            for hard_cut in (False, True):
                for frames in ((568, 314, 385), (568, 316, 387)):
                    with self.subTest(placed=placed, hard_cut=hard_cut, frames=frames):
                        keys = [dict(k, **({"cut": True} if hard_cut and k["at"] == 10.5 else {}))
                                for k in SPRINT]
                        segment = {"asset": "ride360", "in": 9.133333333333333, "out": 50.733333333333334,
                                   "path": keys}
                        extra = {}
                        if placed:
                            segment["timeline"] = {"track": "V1", "startFrame": 0, "durationFrames": 1248}
                            extra["timeline"] = {"version": 1, "fps": 30, "durationFrames": 1248}
                        self.open([segment], **extra)
                        store = self.service.document
                        store.snapshot()
                        original = store._current_compiled["segs"][0]["spec"]["sphere"]["path"]
                        for at in frames:
                            node = next(n for n in store.snapshot()["nodes"] if n["kind"] == "segment"
                                        and n["startFrame"] < at < n["endFrame"])
                            result = self.apply_operations([{"op": "split_segment", "id": node["id"], "atFrame": at}])
                            self.assert_split_playback(result, original, window_path=original, window_duration=41.6)
                        self.assertEqual(len(result["plan"]["segments"]), 4)

    def test_transaction_recuts_keep_fractional_and_negative_window_clocks_and_hardcuts(self):
        for offset in (-1, 32 / 30):
            with self.subTest(offset=offset):
                window = {"offsetSeconds": offset, "durationSeconds": 8, "path": [
                    {"at": 0, "yaw": -20, "pitch": 10},
                    {"at": offset + 2, "yaw": 80, "cut": True},
                    {"at": offset + 4, "yaw": -90, "cut": True},
                    {"at": 8, "yaw": 30, "roll": 45}]}
                # Window-only input must not lose playback when /path is absent.
                self.open([{"asset": "ride360", "in": 10, "out": 22, "speed": 2, "cameraWindow": window}])
                store = self.service.document
                store.snapshot()
                original = store._current_compiled["segs"][0]["spec"]["sphere"]["path"]
                for at in (90, 59, 121):
                    node = next(n for n in store.snapshot()["nodes"] if n["kind"] == "segment"
                                and n["startFrame"] < at < n["endFrame"])
                    result = self.apply_operations([{"op": "split_segment", "id": node["id"], "atFrame": at}])
                    self.assert_split_playback(result, original, window_path=window["path"],
                                               window_duration=8, offset=offset)

    def test_fractional_window_cut_is_frame_exact_and_trim_keeps_exterior(self):
        from reels.sphere import camera_filter

        window = {"offsetSeconds": 32 / 30, "durationSeconds": 10,
                  "path": [{"at": 0, "yaw": -20}, {"at": 4, "yaw": 40}, {"at": 10, "yaw": -60}]}
        node = self.open([{"asset": "ride360", "in": 2, "out": 7, "pitch": 20, "fov": 95,
                           "roll": 10, "cameraWindow": window}])[0]
        camera = self.service.dispatch("sphere.camera", {"id": node})
        self.assertEqual(camera["errors"], [])
        keys = [{"at": t, "yaw": yaw, "pitch": 0, "roll": 0, "fov": 90, "cut": t == 1}
                for t, yaw in ((0, 0), (1, 90), (5, 90))]
        result = self.write_camera(node, keys)
        merged = result["plan"]["segments"][0]["cameraWindow"]
        rendered = self.service.document._current_compiled["segs"][0]["spec"]["sphere"]
        self.assertEqual(next(k["at"] for k in rendered["path"] if k.get("cut")), 1.0)
        for frame, yaw in ((29, 0), (30, 90), (31, 90)):
            self.assertEqual(path_value(rendered["path"], frame / 30)["yaw"], yaw)
        commands = self.fixture.root / "cut.cmd"
        camera_filter(rendered, "e", 90, 160, 30, 150, commands)
        yaw_commands = [line for line in commands.read_text().splitlines() if " yaw " in line]
        self.assertEqual(yaw_commands, ["0.9833 v360@cam yaw 90.000;"])
        # All clocks move by the same amount, including exterior fractional keys.
        shift = merged["offsetSeconds"] - window["offsetSeconds"]
        self.assertAlmostEqual(merged["durationSeconds"], window["durationSeconds"] + shift)
        for old in (camera["playbackPath"][0], camera["playbackPath"][-1]):
            retained = min(merged["path"], key=lambda k: abs(k["at"] - old["at"] - shift))
            self.assertAlmostEqual(retained["at"] - merged["offsetSeconds"],
                                   old["at"] - window["offsetSeconds"])
            self.assert_pose(retained, old)
        self.apply_operations([{"op": "trim_segment", "id": node, "sourceIn": 0, "sourceOut": 12}])
        trimmed = self.service.document._current_compiled["segs"][0]["spec"]["sphere"]["path"]
        for old in (camera["playbackPath"][0], camera["playbackPath"][-1]):
            self.assert_pose(path_value(trimmed, old["at"] - window["offsetSeconds"] + 2), old)
        for frame, yaw in ((89, 0), (90, 90), (91, 90)):
            self.assertEqual(path_value(trimmed, frame / 30)["yaw"], yaw)

    def test_negative_window_full_aim_transaction_and_later_trim(self):
        window = {"offsetSeconds": -1, "durationSeconds": 6,
                  "path": [{"at": 0, "yaw": 0}, {"at": 4, "yaw": 90}, {"at": 6, "yaw": -45}]}
        node = self.open([{"asset": "ride360", "in": 2, "out": 7, "pitch": 20, "fov": 95,
                           "roll": 10, "cameraWindow": window}])[0]
        camera = self.service.dispatch("sphere.camera", {"id": node})
        self.assertEqual([k["at"] for k in camera["path"]], [0, 1, 5])
        edited = [dict(k) for k in camera["path"]]
        edited[0]["yaw"] = 20
        edited[1].update(yaw=90, cut=True)
        result = self.write_camera(node, edited)
        merged = result["plan"]["segments"][0]["cameraWindow"]
        self.assertEqual(merged["offsetSeconds"], 0)
        self.assertEqual(merged["durationSeconds"], 7)
        self.assertTrue(all(k["at"] >= 0 for k in merged["path"]))
        rendered = self.service.document._current_compiled["segs"][0]["spec"]["sphere"]["path"]
        for frame in range(151):
            self.assert_pose(path_value(rendered, frame / 30), path_value(edited, frame / 30))
        self.apply_operations([{"op": "trim_segment", "id": node, "sourceIn": 1, "sourceOut": 10}])
        trimmed = self.service.document._current_compiled["segs"][0]["spec"]["sphere"]["path"]
        for frame in range(151):
            self.assert_pose(path_value(trimmed, frame / 30 + 1), path_value(edited, frame / 30))
        self.assert_pose(path_value(trimmed, 8), camera["playbackPath"][-1])
        self.assertEqual(self.service.dispatch("sphere.camera", {"id": node})["errors"], [])

    def test_sparse_path_trim_and_split_inherit_compiler_camera_context(self):
        for placed in (False, True):
            for settings in ({}, {"moment": "m-1"},
                             {"moment": "m-1", "view": "road", "pitch": 35, "fov": 110, "roll": 20}):
                for operation in ("trim_segment", "split_segment"):
                    with self.subTest(placed=placed, settings=settings, operation=operation):
                        segment = {"asset": "ride360", "in": 10, "out": 16, **settings,
                                   "path": [{"at": 0, "yaw": 0}, {"at": 6, "yaw": 80}]}
                        extra = {}
                        if placed:
                            segment["timeline"] = {"track": "V1", "startFrame": 0, "durationFrames": 180}
                            extra["timeline"] = {"version": 1, "fps": 30, "durationFrames": 180}
                        node = self.open([segment], **extra)[0]
                        store = self.service.document
                        store.snapshot()
                        original = store._current_compiled["segs"][0]["spec"]["sphere"]["path"]
                        self.assertEqual(original[0]["pitch"], settings.get("pitch", 25))
                        self.assertEqual(original[0]["fov"], settings.get("fov", 100))
                        args = ({"sourceIn": 11, "sourceOut": 15} if operation == "trim_segment"
                                else {"atFrame": 60})
                        self.apply_operations([{"op": operation, "id": node, **args}])
                        pieces = store._current_compiled["segs"]
                        intervals = [(1, 5)] if operation == "trim_segment" else [(0, 2), (2, 6)]
                        for piece, (start, end) in zip(pieces, intervals):
                            path = piece["spec"]["sphere"]["path"]
                            self.assert_pose(path_value(path, 0), path_value(original, start))
                            self.assert_pose(path_value(path, end-start), path_value(original, end))

    def test_sparse_cutaway_trim_and_parent_slices_inherit_compiler_context(self):
        for operation in ("trim_cutaway", "trim_segment", "split_segment"):
            with self.subTest(operation=operation):
                node = self.open([{"asset": "flat", "in": 0, "out": 10, "cutaways": [{
                    "asset": "ride360", "moment": "m-1", "at": 1, "fov": 110,
                    "path": [{"at": 0, "yaw": 0}, {"at": 6, "yaw": 80}]}]}])[0]
                store = self.service.document
                snapshot = store.snapshot()
                original = store._current_compiled["segs"][0]["cutaways"][0]["spec"]["sphere"]["path"]
                self.assertEqual((original[0]["pitch"], original[0]["fov"], original[0]["roll"]), (25, 110, 90))
                target = next(n["id"] for n in snapshot["nodes"] if n["kind"] == "cutaway")
                args = ({"id": target, "sourceIn": 11, "sourceOut": 15} if operation == "trim_cutaway"
                        else {"id": node, "sourceIn": 2, "sourceOut": 7} if operation == "trim_segment"
                        else {"id": node, "atFrame": 90})
                self.apply_operations([{"op": operation, **args}])
                intervals = ([(1, 5)] if operation == "trim_cutaway" else [(1, 6)] if operation == "trim_segment"
                             else [(0, 2), (2, 6)])
                cuts = [c for piece in store._current_compiled["segs"] for c in piece["cutaways"]]
                self.assertEqual(len(cuts), len(intervals))
                for cut, (start, end) in zip(cuts, intervals):
                    path = cut["spec"]["sphere"]["path"]
                    self.assert_pose(path_value(path, 0), path_value(original, start))
                    self.assert_pose(path_value(path, end-start), path_value(original, end))

    def test_random_split_keys_compile_and_playback_matches_render(self):
        from reels import render

        rng = random.Random(36033)
        for case in range(128):
            time_ms = 0
            keys = []
            for index in range(rng.randint(2, 9)):
                if index:
                    time_ms += rng.choice((100, 101, 150, 333, 900))
                keys.append({"at": time_ms / 1000, "yaw": rng.uniform(-180, 180),
                             "pitch": rng.uniform(-85, 85), "roll": rng.uniform(-180, 180),
                             "fov": rng.uniform(30, 120), "cut": index > 0 and rng.random() < 0.25})
            speed = rng.choice((0.25, 0.5, 0.75, 1, 1.5, 2, 3.25, 4, rng.uniform(0.25, 4)))
            minimum = max(0.404 / speed, 0.101)
            offset = rng.choice((0.0625, 0.0005, rng.uniform(0.001, 0.25)))
            duration = max((time_ms + rng.randint(100, 500)) / 1000, offset + 2 * minimum + 0.1)
            split = rng.uniform(offset + minimum, duration - minimum)
            if case % 4 == 0:
                keys = rng.choice((["front", "back"], ["road", "side"]))
            elif case % 4 == 1:
                for key in keys:
                    for component in rng.sample(["yaw", "pitch", "roll", "fov"], rng.randint(1, 3)):
                        key.pop(component)
            projection = rng.choice(("flat", "tiny-planet"))
            for start, end in ((offset, split), (split, duration)):
                with self.subTest(case=case, start=start, end=end, speed=speed, projection=projection):
                    segment = {"asset": "ride360", "moment": "m-1", "in": start * speed, "out": end * speed,
                               "speed": speed, "projection": projection, "cameraWindow": {
                                   "durationSeconds": duration, "offsetSeconds": start, "path": keys}}
                    node_id = self.open([segment])[0]
                    cam = self.service.dispatch("sphere.camera", {"id": node_id})
                    self.assertEqual(cam["errors"], [])
                    seconds = cam["seconds"]
                    bounded = cam["path"]
                    self.assertTrue(all(0 <= k["at"] <= seconds for k in bounded))
                    errors = []
                    plan_camera({"path": bounded or None, "projection": projection},
                                {}, None, seconds, errors, [], "piece")
                    self.assertEqual(errors, [])
                    rendered = render.build(self.fixture.project, {
                        "slug": "random-split", "segments": [segment]})["segs"][0].spec["sphere"]
                    for fraction in (0.07, 0.23, 0.5, 0.79, 0.97):
                        t = seconds * fraction
                        viewer = path_value(cam["playbackPath"], t + cam["playbackOffsetSeconds"], projection)
                        expected = path_value(rendered["path"], t, projection)
                        self.assertLess(_turn(qmat(view_quat(viewer)), qmat(view_quat(expected))), 1e-7)
                        self.assertAlmostEqual(viewer["fov"], expected["fov"], places=9)
                    self.edit_piece(segment, store=self.service.document)

    def test_tiny_planet_swap_halves_keeps_seam_protection(self):
        from reels import render
        from reels.loop import swap_halves

        from itertools import product

        for roll, first_path, last_path in product((0, 90, -120), (True, False), (True, False)):
            view = {"yaw": 0, "pitch": 0, "roll": roll, "fov": 90}
            segments = []
            for start, end, use_path in ((5, 8, first_path), (2, 5, last_path)):
                segment = {"asset": "ride360", "in": start, "out": end,
                           "projection": "tiny-planet", "view": dict(view)}
                if use_path:
                    segment["path"] = [{"at": 0, **view}, {"at": 3, **view}]
                segments.append(segment)
            built = render.build(self.fixture.project, {"slug": "planet-loop", "loop": True, "segments": segments})
            self.assertTrue(swap_halves(built["segs"], built["fps"])[0])
            self.assertTrue(built["segs"][-1].spec["_keepOut"])
            self.assertTrue(built["loop"]["swap"][0])  # staging suppresses boundary audio fades
        segments[0]["view"]["yaw"] = 70
        changed = render.build(self.fixture.project, {"slug": "changed-loop", "loop": True, "segments": segments})
        self.assertFalse(changed["loop"]["swap"][0])
        # Flat roll-only changes used to be missed entirely by the Euler seam check.
        segments[0]["view"] = {**view, "roll": view["roll"] + 10}
        for segment in segments:
            segment["projection"] = "flat"
        changed = render.build(self.fixture.project, {"slug": "roll-loop", "loop": True, "segments": segments})
        self.assertFalse(changed["loop"]["swap"][0])
        segments[0]["view"] = {**view, "fov": view["fov"] + 10}
        changed = render.build(self.fixture.project, {"slug": "zoom-loop", "loop": True, "segments": segments})
        self.assertFalse(changed["loop"]["swap"][0])

    def test_camera_move_cues_measure_rotation_not_euler_components(self):
        from reels.mix import _move_mids

        for projection, pitch in (("flat", -90), ("tiny-planet", 0)):
            equivalent = [{"at": 0, "yaw": 0, "pitch": pitch, "roll": 90, "fov": 90},
                          {"at": 2, "yaw": 90, "pitch": pitch, "roll": 0, "fov": 90}]
            self.assertEqual(_move_mids(equivalent, projection), [])
        start = {"at": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 90}
        for component, delta in (("yaw", 30), ("pitch", 25), ("roll", 30), ("fov", 30)):
            self.assertEqual(_move_mids([start, {**start, "at": 2, component: start[component] + delta}]), [1])

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

    def test_sparse_boundary_keeps_slerps_roll_and_base_components(self):
        raw = [{"at": 0, "yaw": 0, "pitch": 0}, {"at": 2, "yaw": 90, "pitch": 60}]
        full = plan_camera({"path": raw}, {}, None, 2, [], [], "clip")["path"]
        boundary = _slice_keys(raw, 1, 2, rebase=True)[0]
        expected = path_value(full, 1)
        self.assertGreater(abs(expected["roll"]), 10)
        for name in ("yaw", "pitch", "roll", "fov"):
            self.assertAlmostEqual(boundary[name], expected[name], places=9)
        # An omitted component inherits the clip view, never the other key.
        base = {"yaw": 0, "pitch": 10, "roll": 20, "fov": 100}
        raw[1]["roll"] = 80
        full = plan_camera({"view": base, "path": raw}, {}, None, 2, [], [], "clip")["path"]
        boundary = _slice_keys(raw, 1, 2, rebase=True, view=base)[0]
        expected = path_value(full, 1)
        for name in ("yaw", "pitch", "roll", "fov"):
            self.assertAlmostEqual(boundary[name], expected[name], places=9)


class GoldenPathTests(unittest.TestCase):
    """tests/fixtures/sphere_path_golden.json: SphereMathTests asserts the same vectors in Swift."""

    def test_path_value_matches_the_golden_vectors(self):
        tol = GOLDEN["tolerance"]
        for case in GOLDEN["cases"]:
            for t, yaw, pitch, fov, roll in case["samples"]:
                with self.subTest(case=case["name"], t=t):
                    v = path_value(case["keys"], t, case.get("projection", "flat"))
                    self.assertLess(_wrapped(v["yaw"], yaw), tol)
                    self.assertLess(abs(v["pitch"] - pitch), tol)
                    self.assertLess(abs(v["fov"] - fov), tol)
                    self.assertLess(_wrapped(v["roll"], roll), tol)

    def test_every_move_takes_the_short_way(self):
        for case in GOLDEN["cases"]:
            keys = case["keys"]
            projection = case.get("projection", "flat")
            for arc in case["arcs"]:
                a, b = keys[arc["from"]], keys[arc["to"]]
                shortest = _turn(qmat(view_quat(path_value(keys, a["at"], projection))),
                                 qmat(view_quat(path_value(keys, b["at"], projection))))
                self.assertAlmostEqual(arc["degrees"], shortest, places=6)
                self.assertLessEqual(shortest, 180.0)
                frames = [path_value(keys, a["at"] + (b["at"] - a["at"]) * i / 400, projection) for i in range(401)]
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

    def test_shifted_key_spacing_is_checked_after_millisecond_rounding(self):
        path = [{"at": 0, "yaw": 0}, {"at": 0.2, "yaw": 20}, {"at": 0.3, "yaw": 30}, {"at": 1, "yaw": 90}]
        resolved = plan_camera({"path": path}, {}, None, 1, [], [], "source")["path"]
        for offset in (0, 0.0005, 0.0005001, 0.1):
            seconds = 1 - offset
            bounded = window_piece_keys(resolved, offset, seconds)
            errors = []
            plan_camera({"path": bounded}, {}, None, seconds, errors, [], "piece")
            self.assertEqual(errors, [])
            self.assertTrue(all(k["at"] == round(k["at"], 3) and 0 <= k["at"] <= seconds for k in bounded))

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

    def test_deleting_first_boundary_holds_the_first_remaining_pose(self):
        self.check_deleted_boundary(first=True)

    def test_deleting_last_boundary_holds_the_last_remaining_pose(self):
        self.check_deleted_boundary(first=False)

    def check_deleted_boundary(self, *, first):
        window = {"offsetSeconds": 2.0, "durationSeconds": 10.0, "path": [
            {"at": 0, "yaw": -45}, {"at": 10, "yaw": -90}]}
        segment = {"in": 2, "out": 8}
        for gap in (1.0, 0.05):
            keys = [{"at": gap if first else 0, "yaw": 90, "pitch": 0, "fov": 90, "roll": 0},
                    {"at": 3, "yaw": 45, "pitch": 0, "fov": 90, "roll": 0},
                    {"at": 6 if first else 6 - gap, "yaw": 0, "pitch": 0, "fov": 90, "roll": 0}]
            merged = window_with_piece_keys(window, segment, keys)
            errors = []
            resolved = plan_camera({"path": merged["path"]}, {}, None, merged["durationSeconds"], errors, [], "clip")
            self.assertEqual(errors, [])
            for n in range(181):
                t = n / 30
                self.assertAlmostEqual(path_value(resolved["path"], t + 2)["yaw"],
                                       path_value(keys, t)["yaw"], places=7)


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
        from reels.editor.bench_sphere_keys import main

        fixture = TempProject(self)
        media = fixture.project.media_dir / "pano.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=512x256:rate=30:duration=3",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media)], check=True)  # fmt: skip
        asset = fixture.asset("pano-1234", duration=3.0, width=512, height=256, audio=False, media=media)
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 512, "height": 256}
        save_asset(fixture.project, asset)
        fixture.project.config["render"].update(width=144, height=256)
        config_path = fixture.root / "config.json"
        config_path.write_text(json.dumps(fixture.project.config))
        plan = {"slug": "t", "segments": [{"asset": "pano", "in": 0, "out": 3, "path": [
            {"at": 0, "yaw": -12.89, "pitch": 35.63, "fov": 80, "roll": -95.47},
            {"at": 1.5, "yaw": -107.05, "pitch": 84.01, "fov": 80, "roll": 167.45},
            {"at": 2.9, "yaw": -24.65, "pitch": 47.35, "fov": 90, "roll": -105.51}]}]}
        from contextlib import chdir, redirect_stdout
        import io

        plan_path = fixture.plan("t", plan["segments"])
        originals = {p: p.read_bytes() for p in (plan_path, config_path, media,
                     fixture.project.assets_dir / "pano-1234.json")}
        output = io.StringIO()
        with chdir(fixture.root), redirect_stdout(output):
            status = main(["--root", str(fixture.root), "--plan", "plans/t.json",
                           "--out", "bench", "--from", "0.5", "--to", "2.95", "--json"])
        report = json.loads(output.getvalue())
        self.assertEqual(status, 0)
        self.assertEqual(report["asset"], "pano-1234")
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content, path)
        self.assertEqual(Path(report["dir"]), (fixture.root / "bench").resolve())
        self.assertTrue((fixture.root / "bench" / "staged.mp4").is_file())
        self.assertTrue(report["pass"], report)
        self.assertEqual([r["label"].split()[0] for r in report["rows"]], ["mid", "key", "mid", "key"])
        for row in report["rows"]:
            if "linearPsnr" in row:  # the old per-component blend aims somewhere else entirely
                self.assertLess(row["linearPsnr"], row["psnr"] - 10, row)

    def test_moment_only_segment_slices_resolved_bounds(self):
        from reels.editor.bench_sphere_keys import piece

        asset = {"tags": {"moments": [{"id": "m-1", "in": 10, "out": 18, "view": "road"}]}}
        segment = {"asset": "pano", "moment": "m-1", "speed": 2,
                   "path": [{"at": 0, "yaw": 0}, {"at": 4, "yaw": 90}]}
        sliced = piece(segment, 1, 3, asset=asset)
        self.assertEqual((sliced["in"], sliced["out"]), (12, 16))
        self.assertEqual(sliced["moment"], "m-1")
        self.assertEqual(sliced["cameraWindow"]["offsetSeconds"], 1)
        self.assertEqual(sliced["cameraWindow"]["durationSeconds"], 4)
        self.assertNotIn("in", segment)


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
                              "yaw": 10, "pitch": 20, "roll": 30, "fov": 100,
                              "path": _slice_keys(window["path"], 2, 6, rebase=True)}])
        store = DocumentStore(live_workspace(fixture), "plans/aim.json")
        self.addCleanup(store.close)
        before = store.snapshot()
        seg = next(n for n in before["nodes"] if n["kind"] == "segment")
        edited = [{"at": 0, "yaw": 60, "pitch": 0, "fov": 90, "roll": 0},
                  {"at": 1, "yaw": -45, "pitch": 5, "fov": 80, "roll": 0},
                  {"at": 4, "yaw": 0, "pitch": 0, "fov": 90, "roll": 0}]
        result = store.apply({"expected": before["revisions"], "origin": "manual", "reason": "aim", "operations": [
            *[{"op": "set_field", "id": seg["id"], "path": "/" + name, "value": None}
              for name in ("yaw", "pitch", "fov", "roll")],
            {"op": "set_field", "id": seg["id"], "path": "/path", "value": edited}]})
        self.assertFalse([d for d in result.get("diagnostics") or [] if d.get("severity") == "error"])
        raw = result["plan"]["segments"][0]
        self.assertEqual(raw["path"], edited)
        self.assertEqual([k["at"] for k in raw["cameraWindow"]["path"]], [0, 2.0, 3.0, 6.0])
        rendered = store._current_compiled["segs"][0]["spec"]["sphere"]["path"]
        for t in (0.0, 0.5, 1.0, 2.5, 4.0):  # the render follows the edit, not the old window keys
            self.assertAlmostEqual(path_value(rendered, t)["yaw"], path_value(edited, t)["yaw"], places=6)

    def test_aim_edit_accepts_bounded_keys_at_zero_point_nine_and_one(self):
        from helpers import live_workspace
        from reels.editor.transactions import DocumentStore

        fixture = TempProject(self)
        asset = fixture.asset("ride360", duration=10)
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 4096, "height": 2048}
        save_asset(fixture.project, asset)
        path = [{"at": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 90},
                {"at": 0.9, "yaw": 90, "pitch": 0, "roll": 0, "fov": 90}]
        window = {"durationSeconds": 2, "offsetSeconds": 0, "path": path}
        fixture.plan("spacing", [{"asset": "ride360", "in": 2, "out": 3, "cameraWindow": window}])
        store = DocumentStore(live_workspace(fixture), "plans/spacing.json")
        self.addCleanup(store.close)
        before = store.snapshot()
        node = next(n for n in before["nodes"] if n["kind"] == "segment")
        edited = window_piece_keys(path, 0, 1)
        self.assertEqual([k["at"] for k in edited], [0, 0.9, 1])
        edited[0]["yaw"] = 20
        result = store.apply({"expected": before["revisions"], "origin": "manual", "reason": "aim", "operations": [
            {"op": "set_field", "id": node["id"], "path": "/path", "value": edited}]})
        self.assertEqual(result["plan"]["segments"][0]["path"], edited)
        rendered = store._current_compiled["segs"][0]["spec"]["sphere"]["path"]
        self.assertEqual(path_value(rendered, 0)["yaw"], 20)
        self.assertEqual(path_value(rendered, 0.95)["yaw"], 90)


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
