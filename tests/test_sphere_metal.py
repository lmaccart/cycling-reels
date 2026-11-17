from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from reels.config import Project
from reels import render, sphere_metal
from reels.editor import preview
from reels.editor.bench_sphere_keys import _private_project, piece
from reels.editor.bench_sphere_metal import compare
from reels.media import run, ffprobe_json
from reels.util import ReelsError

ROOT = Path(__file__).resolve().parents[1]
ASSET = "261031-gs010110-08e8"
SOURCE = ROOT / f"library/media/{ASSET}.360"
BINARY = ROOT / ".cache/editor/bin/reels-sphere"
REAL = platform.system() == "Darwin" and SOURCE.is_file() and BINARY.is_file()


class MetalCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.probe = {"sphere": {"projection": "gopro-eac", "face": 736, "overlap": 64, "tracks": [0, 5]}}
        self.streams = [{"index": i, "id": hex(i+1), "codec_type": "video", "codec_name": "hevc", "pix_fmt": "yuvj420p",
                         "color_space": "bt709", "color_transfer": "bt709", "color_primaries": "bt709", "color_range": "pc",
                         "start_time": "0.0", "width": 2272, "height": 736} for i in (0, 5)]

    def test_rejects_hdr_depth_layout_and_clock_before_render(self):
        for probe_change, stream_change, reason in [
            ({"hdr": True}, {}, "HDR"), ({}, {"pix_fmt": "yuv420p10le"}, "8-bit"),
            ({}, {"width": 4096}, "layout"), ({}, {"start_time": "0.5"}, "origin"),
            ({}, {"color_primaries": "smpte432"}, "colour"), ({}, {"id": "missing"}, "track IDs"),
        ]:
            probe = {**self.probe, **probe_change}
            streams = [{**s, **stream_change} for s in self.streams]
            with self.subTest(reason=reason):
                self.assertIn(reason, sphere_metal.capability(probe, streams))

    def test_untagged_sd_preserves_staging_colour_interpretation(self):
        stream = {**self.streams[0], "width": 512, "height": 256}
        stream.pop("color_space")
        self.assertIn("BT.601", sphere_metal.capability({"sphere": {"projection": "equirect"}}, [stream]))
        stream["color_space"] = "bt709"
        self.assertIsNone(sphere_metal.capability({"sphere": {"projection": "equirect"}}, [stream]))

    def test_requires_matching_track_ranges_and_chroma_aligned_seams(self):
        streams = deepcopy(self.streams)
        streams[1]["color_range"] = "tv"
        self.assertIn("different", sphere_metal.capability(self.probe, streams))
        probe = deepcopy(self.probe)
        probe["sphere"]["overlap"] = 60
        self.assertIn("chroma", sphere_metal.capability(probe, self.streams))

    def test_required_metal_never_silently_uses_v360(self):
        sg = types.SimpleNamespace(asset={"media": str(SOURCE)}, spec={})
        with patch.dict(os.environ, REELS_SPHERE_BACKEND="metal"), patch.object(sphere_metal, "executable", return_value=None):
            with self.assertRaisesRegex(ReelsError, "Metal required"):
                sphere_metal.render(Project(ROOT), sg, Path("unused"), 30, 320, 568, Path("unused"))

    def test_fixed_stabilized_camera_matches_an_identical_held_path(self):
        from reels.sphere import camera_filter
        from reels.stabilize import v360_matrix

        view = {"yaw": 33, "pitch": -17, "roll": 81, "fov": 75}
        corrections = [v360_matrix(4, 2, -1), v360_matrix(7, -3, 2), v360_matrix(9, 1, -2)]
        for projection in ("flat", "tiny-planet"):
            with self.subTest(projection=projection), tempfile.TemporaryDirectory() as td:
                path = Path(td) / "camera.cmd"
                fixed = {"projection": projection, "view": view}
                a = camera_filter(fixed, "e", 320, 568, 30, 3, path, stab=corrections)
                commands = path.read_text()
                b = camera_filter({**fixed, "path": [{"at": 0, **view}]}, "e", 320, 568, 30, 3, path, stab=corrections)
                self.assertEqual(a, b)
                self.assertEqual(commands, path.read_text())


@unittest.skipUnless(REAL, "built macOS Metal renderer and user's own GoPro source required")
class MetalRealFootageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="sphere-parity-", dir=ROOT / ".cache")
        self.addCleanup(self.tmp.cleanup)
        self.work = Path(self.tmp.name).resolve()
        self.project = _private_project(ROOT, self.work / "project", ASSET, 1)
        self.project.config["render"].update(width=320, height=568)

    def segment(self, raw):
        built = render.build(self.project, {"slug": "sphere-parity", "segments": [raw], "autoSfx": False})
        return built["segs"][0], built["fps"]

    def assertParity(self, raw, samples, name):
        sg, fps = self.segment(raw)
        work = self.work / name
        work.mkdir()
        report = compare(self.project, sg, fps, work, samples)
        print("METAL_PARITY", name, json.dumps(report))
        self.assertTrue(report["pass"], json.dumps(report, indent=2))
        self.assertTrue(report["matchedTimestamps"])

    def test_dynamic_poles_seams_holds_cuts_numeric_stabilization_and_speed(self):
        keys = [
            {"at": 0, "yaw": 179, "pitch": 84, "roll": 167, "fov": 59},
            {"at": .3, "yaw": -179, "pitch": -84, "roll": -105, "fov": 110},
            {"at": .6, "yaw": 90, "pitch": 0, "roll": 72, "fov": 75, "cut": True},
            {"at": .9, "yaw": -90, "pitch": 20, "roll": -30, "fov": 95},
        ]
        raw = {"asset": ASSET, "in": 19.6375, "out": 20.7625, "speed": 1.25, "path": keys, "stabilize": .73}
        self.assertParity(raw, [0, 4, 9, 13, 17, 18, 22, 26], "dynamic")

    def test_sprint_105_key_and_fractional_split_window(self):
        fixture = json.loads((ROOT / "tests/fixtures/sphere_path_golden.json").read_text())
        keys = next(c["keys"] for c in fixture["cases"] if c["name"].startswith("sprint zone: roll"))
        asset = json.loads((ROOT / f"library/assets/{ASSET}.json").read_text())
        raw = {"asset": ASSET, "in": 9.133333333333, "out": 40, "path": keys, "stabilize": .73}
        raw = piece(raw, 10.125, 10.925, asset=asset)
        self.assertParity(raw, [0, 10, 11, 12, 23], "split-105")

    def test_tiny_planet(self):
        self.assertParity({"asset": ASSET, "in": 25.1375, "out": 25.5375, "projection": "tiny-planet",
                           "view": {"yaw": -179, "roll": 123}, "stabilize": .73}, [0, 5, 11], "planet")

    def test_slow_motion_duplicates_with_output_fps_conversion(self):
        self.project.config["render"]["fps"] = 24
        self.assertParity({"asset": ASSET, "in": 25.1375, "out": 25.55, "speed": .5,
                           "view": {"yaw": 135, "pitch": -35, "roll": 43}, "stabilize": .73},
                          [0, 1, 2, 9, 18, 19], "slow-24fps")

    def test_direct_hevc_hardware_encoding(self):
        from reels.editor.bench_sphere_metal import backend

        sg, fps = self.segment({"asset": ASSET, "in": 19.6375, "out": 20.0375})
        with backend("metal"):
            sphere_metal.render(self.project, sg, self.work / "h264.mp4", fps, 320, 568, self.work)
        path = self.work / "request.json"
        request = json.loads(path.read_text())
        request.update(codec="hevc", destination=str(self.work / "hevc.mp4"))
        path.write_text(json.dumps(request))
        report = json.loads(run([BINARY, "--render", path]).stdout)
        self.assertEqual(report["encoder"], "hevc_videotoolbox")
        stream = ffprobe_json(self.work / "hevc.mp4")["streams"][0]
        self.assertEqual(stream["codec_name"], "hevc")
        self.assertEqual(int(stream["nb_frames"]), sg.frames)
        self.assertEqual([stream["width"], stream["height"]], [320, 568])

    def test_equirect_derived_only_from_own_real_frames(self):
        from reels.sphere import source_graph
        from reels.library import load_asset, save_asset
        asset = load_asset(self.project, ASSET)
        graph, pad, _ = source_graph(asset["probe"])
        self.project = Project(self.work / "derived", code_root=ROOT)
        self.project.config["render"].update(width=320, height=568)
        path = self.project.media_dir / "own-equirect.mp4"
        path.parent.mkdir(parents=True, exist_ok=True)
        run(["ffmpeg", "-v", "error", "-y", "-ss", "19.6375", "-t", "0.5", "-i", SOURCE,
             "-filter_complex", graph+f";[{pad}]setpts=PTS-STARTPTS,v360=eac:e:w=2944:h=1472:interp=cubic[v]", "-map", "[v]", "-an",
             "-c:v", "libx264", "-qp", "0", "-pix_fmt", "yuv420p", "-color_range", "pc", "-colorspace", "bt709",
             "-color_primaries", "bt709", "-color_trc", "bt709", path])
        asset["id"] = "own-equirect"
        from reels.util import quick_hash
        asset["media"] = "library/media/own-equirect.mp4"
        asset["hash"] = quick_hash(path)
        asset["probe"].update(duration=.5, sphere={"projection": "equirect", "width": 2944, "height": 1472})
        save_asset(self.project, asset)
        self.assertParity({"asset": "own-equirect", "in": .0125, "out": .4125,
                           "path": [{"at": 0, "yaw": 179, "pitch": 86, "roll": 33},
                                    {"at": .4, "yaw": -179, "pitch": -86, "roll": -127}]}, [0, 4, 8, 11], "equirect")

    def test_actual_frozen_stage_executes_metal_under_production_sandbox(self):
        if not shutil.which("sandbox-exec"):
            self.skipTest("macOS sandbox required")
        sg, fps = self.segment({"asset": ASSET, "in": 19.6375, "out": 20.0375, "stabilize": .73})
        # Freeze the same Python baseline as the editor, with no helper copied into
        # it: discovery MUST cross the trusted runtime boundary, not repo-relative luck.
        from reels.editor.renderer import RendererManager
        frozen = self.work / "frozen"
        frozen.mkdir()
        workspace = types.SimpleNamespace(code_root=ROOT, root=self.work / "workspace", _originals={
            "source": {"path": str(SOURCE.resolve())}}, project=self.project)
        RendererManager(workspace)._copy_baseline(frozen)
        job = self.work / "job"
        job.mkdir()
        reads, writes = preview._path_roots(workspace, frozen, job, job, job, job)
        reads.extend([self.work / "project"])
        env = preview._safe_env(workspace, job, frozen)
        env["REELS_SPHERE_BACKEND"] = "metal"
        executables = preview._executables(workspace, frozen)
        profile = job / "worker.sb"
        profile.write_text(preview._profile_text(reads, writes, executables, metal=True))
        serialized = {"asset": sg.asset, "spec": sg.spec, "frames": sg.frames}
        (job / "segment.json").write_text(json.dumps(serialized))
        script = (
            "import json,sys;from pathlib import Path;from reels.config import Project;from reels.render import Seg,stage_video;"
            "d=json.loads(Path(sys.argv[1]).read_text());"
            "p=Project(Path(sys.argv[2]),code_root=Path(sys.argv[3]),media_root=Path(sys.argv[4]),media_read_only=True);"
            "stage_video(p,Seg(0,'video',d['asset'],d['spec'],d['frames']),Path(sys.argv[5]),30,audio=True,grade=None)"
        )
        out = job / "staged.mp4"
        result = subprocess.run(["sandbox-exec", "-f", str(profile), sys.executable, "-c", script,
                                 str(job / "segment.json"), str(self.work / "project"), str(frozen), str(ROOT / "library/media"), str(out)],
                                env=env, cwd=frozen, capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertIn("sphere renderer: metal (h264_videotoolbox", result.stdout+result.stderr)
        info = ffprobe_json(out)
        self.assertEqual([s["codec_type"] for s in info["streams"]], ["video", "audio"])
        self.assertEqual(int(info["streams"][0]["nb_frames"]), sg.frames)

    def test_benchmark_stdout_remains_machine_readable_json(self):
        from contextlib import redirect_stdout
        import io
        from reels.editor.bench_sphere_metal import command

        plan = self.work / "benchmark-plan.json"
        plan.write_text(json.dumps({"segments": [{"asset": ASSET, "in": 19.6375, "out": 20.0375}]}))
        args = types.SimpleNamespace(root=str(ROOT), plan=str(plan), segment=1, seconds=.4, scale=.1, report=None)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            status = command(Project(ROOT), args)
        report = json.loads(stdout.getvalue())
        self.assertEqual(status, 0)
        self.assertTrue(report["pass"])
        self.assertTrue(report["temporaryMediaRemoved"])

    def test_cli_errors_leave_no_destination(self):
        destination = self.work / "never.mp4"
        request = self.work / "invalid.json"
        request.write_text(json.dumps({"version": 99, "destination": str(destination)}))
        p = subprocess.run([str(BINARY), "--render", str(request)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertFalse(destination.exists())
        self.assertIn("reels-sphere:", p.stderr)


if __name__ == "__main__":
    unittest.main()
