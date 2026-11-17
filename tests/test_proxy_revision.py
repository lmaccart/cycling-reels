from __future__ import annotations

import json
import math
import platform
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import TempProject, live_workspace
from reels.editor.preview import PreviewManager, _executables, _path_roots, _profile_text, _safe_env
from reels.editor.worker import _stat_identity, prepare_sphere_proxy
from reels.library import save_asset
from reels.stabilize import Orientation
from reels.util import ReelsError


class CoveringCorrectionTests(unittest.TestCase):
    def test_lock_reference_and_smooth_corrections_match_cold_request_with_separate_clocks(self):
        fixture = TempProject(self)
        asset = fixture.asset("pano", duration=180)
        source = fixture.project.abs(asset["media"])
        root = fixture.root / "workspace"
        root.mkdir()
        job = {"sourcePath": str(source), "sourceIdentity": _stat_identity(source), "assetId": "pano",
               "mediaRoot": str(source.parent), "workspaceRoot": str(root),
               "probe": {"duration": 180, "sphere": {"projection": "equirect", "width": 512, "height": 256}},
               "start": 0, "end": 180, "revisionDir": str(root / "warm")}
        times = [n / 10 for n in range(1801)]
        quats = [(math.cos(math.radians(t / 2) / 2), 0, math.sin(math.radians(t / 2) / 2), 0) for t in times]
        ori = Orientation(times, quats, (0, 1, 0))

        def encode(source, output, *args, **kwargs):
            output.write_bytes(b"proxy fixture")

        with patch("reels.stabilize.read_orientation", return_value=ori), patch("reels.editor.worker.encode_proxy", side_effect=encode):
            prepare_sphere_proxy(job)
            for setting in ("lock", "medium"):
                with self.subTest(setting=setting):
                    # Cold media contains trim padding; neither padding nor the
                    # warm media's much earlier start may become the lock anchor.
                    request = {**job, "start": 55, "end": 70, "requestedStart": 60, "requestedEnd": 65,
                               "stabilize": setting}
                    cold = prepare_sphere_proxy({**request, "revisionDir": str(root / f"cold-{setting}")})
                    with patch("reels.editor.worker.encode_proxy", side_effect=AssertionError("warm range re-encoded")):
                        warm = prepare_sphere_proxy(request)
                    self.assertEqual((warm["start"], warm["end"]), (0, 180))
                    self.assertEqual((cold["start"], cold["end"]), (55, 70))
                    self.assertEqual(warm["stabilization"], cold["stabilization"])
                    self.assertEqual(warm["stabilization"]["start"], 60)
                    self.assertEqual(warm["stabilization"]["fps"], 30)
                    self.assertEqual(len(warm["stabilization"]["quats"]), 150)
                    if setting == "lock":
                        self.assertEqual(warm["stabilization"]["quats"][0], [1, 0, 0, 0])
                        self.assertNotEqual(warm["stabilization"]["quats"][-1], [1, 0, 0, 0])


@unittest.skipUnless(platform.system() == "Darwin" and shutil.which("sandbox-exec") and shutil.which("ffmpeg"),
                     "needs macOS sandbox and ffmpeg")
class ProxyRuntimeSandboxTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.source = self.fixture.project.media_dir / "pano.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=512x256:rate=30:duration=1",
                        "-c:v", "libx264", str(self.source)], check=True)
        asset = self.fixture.asset("pano", duration=1, media=self.source)
        asset["probe"]["sphere"] = {"projection": "equirect", "width": 512, "height": 256}
        save_asset(self.fixture.project, asset)
        self.asset = asset
        self.workspace = live_workspace(self.fixture)
        self.manager = PreviewManager(self.workspace)
        self.code_root = Path(__file__).resolve().parents[1]

    def test_both_hardware_codecs_and_hardware_decode_work_in_proxy_profile(self):
        job = self.workspace.root / "vt-test"
        job.mkdir()
        env = _safe_env(self.workspace, job, self.code_root)
        reads, writes = _path_roots(self.workspace, self.code_root, job, job / "output", job / "cache", job / "bundles")
        sb = job / "proxy.sb"
        sb.write_text(_profile_text(reads, writes, _executables(self.workspace, self.code_root, None), videotoolbox=True))
        for encoder in ("h264_videotoolbox", "hevc_videotoolbox"):
            with self.subTest(encoder=encoder):
                # Forced hardware frames plus hwdownload cannot silently use a
                # software decoder; allow_sw=0 forbids a software VT encoder.
                args = ["ffmpeg", "-v", "error", "-y", "-hwaccel", "videotoolbox",
                        "-hwaccel_output_format", "videotoolbox_vld", "-i", str(self.source),
                        "-vf", "hwdownload,format=nv12", "-c:v", encoder, "-allow_sw", "0", "-g", "10", "-bf", "0",
                        "-pix_fmt", "yuv420p", str(job / f"{encoder}.mp4")]
                outside = subprocess.run(args, env=env, capture_output=True, text=True)
                if outside.returncode:
                    self.skipTest(f"{encoder} unavailable outside the sandbox: {outside.stderr}")
                inside = subprocess.run(["sandbox-exec", "-f", str(sb), *args], env=env, capture_output=True, text=True)
                self.assertEqual(inside.returncode, 0, inside.stderr)
                self.assertEqual(inside.stderr, "")

    def test_actual_editor_source_and_aim_workers_use_hardware_encoding(self):
        outside = self.fixture.root / "vt-capability.mp4"
        probe = subprocess.run(["ffmpeg", "-v", "error", "-i", str(self.source), "-c:v", "h264_videotoolbox",
                                "-allow_sw", "0", str(outside)], capture_output=True, text=True)
        if probe.returncode:
            self.skipTest(f"hardware H.264 unavailable: {probe.stderr}")
        for result in (self.manager.prepare_source("pano"), self.manager.prepare_sphere("pano", 0, 1, stabilize="lock")):
            self.assertEqual(result["status"], "ready", result)
            streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", result["path"]],
                                                capture_output=True, text=True, check=True).stdout)["streams"]
            video = next(s for s in streams if s["codec_type"] == "video")
            self.assertIn("h264_videotoolbox", video["tags"]["encoder"])

    def test_frozen_renderer_without_proxy_helpers_runs_proxy_and_nonproxy_jobs(self):
        frozen = self.fixture.root / "existing-renderer"
        shutil.copytree(self.code_root / "scripts", frozen / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        # Model an already-persisted renderer from before these runtime helpers
        # existed. The trusted installed application must supply them, not mutate
        # the frozen renderer or silently switch the whole renderer revision.
        for name in ("proxy.py", "proxy_cache.py", "proxy_stabilize.py"):
            (frozen / "scripts/reels/editor" / name).unlink()
        (frozen / "scripts/reels/editor/worker.py").write_text("raise RuntimeError('must not run frozen worker')\n")
        identity = _stat_identity(self.source)
        for action in ("source_proxy", "sphere_proxy", "prepare"):
            job_root = self.manager.worker_root / action
            job_root.mkdir()
            job = {"action": action, "jobRoot": str(job_root), "jobPath": str(job_root / "job.json"),
                   "resultPath": str(job_root / "result.json"), "revisionDir": str(self.manager.preview_root / action),
                   "sourcePath": str(self.source), "sourceIdentity": identity, "sourceKind": "video", "assetId": "pano",
                   "probe": self.asset["probe"], "view": {"yaw": 0}, "start": 0, "end": 1, "stabilize": "lock",
                   "mediaRoot": str(self.source.parent), "workspaceRoot": str(self.workspace.root),
                   "dataRoot": str(self.fixture.root), "plan": {}, "revisions": {"renderer": "existing-frozen-revision"}}
            if action == "prepare":
                # A non-proxy action reaches its own validation, not an import
                # error before dispatch (and not the frozen untrusted runner).
                with self.assertRaisesRegex(ReelsError, "preview plan has an invalid slug"):
                    self.manager._run_worker(job, code_root=frozen)
            else:
                result = self.manager._run_worker(job, code_root=frozen)
                self.assertEqual(result["status"], "ready", result)
                self.assertTrue(Path(result["path"]).is_file())
        self.assertEqual(_stat_identity(self.source), identity)


if __name__ == "__main__":
    unittest.main()
