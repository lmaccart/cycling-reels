from __future__ import annotations

from copy import deepcopy
import json
from fractions import Fraction
import math
import os
from pathlib import Path
import platform
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from helpers import TempProject
from reels.config import Project
from reels.editor import proxy, proxy_metal
from reels.editor.preview import PreviewManager
from reels.editor.proxy_cache import MIN_FREE_BYTES, ProxyDiskFull, budget_root, cache_lock, reserve_disk
from reels.editor.service import PARTIAL_PROXY_GRACE, remove_partial_proxies
from reels.editor.worker import _stat_identity, prepare_source_proxy, prepare_sphere_proxy
from reels.media import ffprobe_json
from reels.stabilize import Orientation
from reels.util import ReelsError

ROOT = Path(__file__).resolve().parents[1]
ASSET = "261031-gs010110-08e8"
SOURCE = (ROOT / f"library/media/{ASSET}.360").resolve()
BINARY = ROOT / ".cache/editor/bin/reels-sphere"
REAL = (platform.system() == "Darwin" and SOURCE.is_file() and BINARY.is_file()
        and shutil.which("sandbox-exec") and shutil.which("ffmpeg"))


class ProxyMetalTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        asset = self.fixture.asset("pano", duration=2)
        self.source = self.fixture.project.abs(asset["media"])
        self.root = self.fixture.root / "workspace"
        self.root.mkdir()
        self.probe = {"duration": 2, "fps": 30, "hasAudio": True,
                      "sphere": {"projection": "equirect", "width": 1920, "height": 960}}
        self.streams = [{"index": 0, "id": "0x1", "codec_type": "video", "codec_name": "h264",
                         "pix_fmt": "yuv420p", "color_space": "bt709", "color_primaries": "bt709",
                         "color_transfer": "bt709", "color_range": "tv", "width": 1920, "height": 960,
                         "duration_ts": 60, "time_base": "1/30"},
                        {"index": 1, "codec_type": "audio", "codec_name": "aac"}]
        self.job = {"sourcePath": str(self.source), "sourceIdentity": _stat_identity(self.source),
                    "mediaRoot": str(self.source.parent), "workspaceRoot": str(self.root), "assetId": "pano",
                    "sourceKind": "video", "probe": self.probe, "codeRoot": str(self.fixture.root / "frozen"),
                    "start": 0, "end": 2, "view": {}}
        self.requests, self.commands = [], []
        self.enterContext(patch.dict(os.environ, REELS_SPHERE_BACKEND="auto"))
        self.enterContext(patch.object(proxy_metal, "executable", return_value=Path("/trusted/reels-sphere")))
        self.container = {"streams": self.streams, "format": {"duration": "2.000000"}}
        self.enterContext(patch.object(proxy_metal, "ffprobe_json", return_value=self.container))
        self.enterContext(patch("reels.stabilize.read_orientation", return_value=None))

    def native(self, command, **kwargs):
        command = list(map(str, command))
        self.commands.append((command, kwargs))
        if "--check" in command:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "--render" in command:
            request = json.loads(Path(command[-1]).read_text())
            self.requests.append(request)
            Path(request["destination"]).write_bytes(b"native video")
            report = {"renderer": "metal", "encoder": request["codec"] + "_videotoolbox",
                      "frames": request["frames"], "wallSeconds": .1, "outputBytes": len(b"native video")}
            return subprocess.CompletedProcess(command, 0, json.dumps(report), "")
        Path(command[-1]).write_bytes(b"native video and audio")
        return subprocess.CompletedProcess(command, 0, "", "")

    @staticmethod
    def fallback(source, output, *args, **kwargs):
        output.write_bytes(b"accurate ffmpeg proxy")
        return "libx264"

    def test_native_requests_use_resolved_views_and_never_bake_live_aim(self):
        views = [{"yaw": 42, "pitch": -12, "roll": 7, "fov": 103},
                 {"yaw": -138, "pitch": 12, "roll": -7, "fov": 103}]
        with patch("reels.stabilize.front_back_views", return_value=views), patch.object(
                proxy, "_run_encoder", side_effect=self.native), patch(
                "reels.sphere_metal.capability", side_effect=AssertionError("untrusted renderer capability")):
            result = prepare_source_proxy({**self.job, "revisionDir": str(self.root / "source"),
                                           "keys": [{"yaw": 99}], "corrections": {"unsafe": True}, "speed": 9})
        self.assertEqual(result["status"], "ready", result)
        self.assertEqual(result["backend"], "metal")
        request = self.requests[0]
        self.assertEqual(request["views"], [{"at": 0, **v} for v in views])
        self.assertEqual((request["preset"], request["projection"], request["width"], request["height"]),
                         ("proxy", "front-back", 1440, 1280))
        self.assertEqual((request["speed"], request["fps"], request["keys"], request["corrections"]), (1, 30, [], None))
        self.assertGreater(request["maxOutputBytes"], 0)
        self.assertEqual(request["trackIDs"], [1])
        self.assertFalse(request["fullRange"])
        self.assertLessEqual(2 * request["maxOutputBytes"], proxy.proxy_reservation(2) - 16_000_000)
        self.assertTrue(all(k["max_output_bytes"] == request["maxOutputBytes"] for _, k in self.commands))
        marker = json.loads(Path(result["path"]).with_suffix(".json").read_text())
        self.assertEqual(marker["views"], views)
        self.assertEqual(marker["execution"]["native"], result["native"])
        self.assertEqual(marker["version"], 5)

    def test_equirect_trim_audio_copy_and_full_range_input(self):
        self.streams[0].update(pix_fmt="yuvj420p", color_range="pc")
        with patch.object(proxy, "_run_encoder", side_effect=self.native):
            result = prepare_sphere_proxy({**self.job, "revisionDir": str(self.root / "sphere"),
                                           "start": .125499, "end": 1.375499})
        self.assertEqual(result["status"], "ready", result)
        request = self.requests[0]
        self.assertEqual((request["projection"], request["width"], request["height"]), ("equirect", 2048, 1024))
        self.assertEqual((request["sourceIn"], request["sourceOut"], request["frames"]), (.125, 1.375, 38))
        self.assertEqual((result["start"], result["end"]), (.125499, 1.375499))
        self.assertNotIn("views", request)
        self.assertTrue(request["fullRange"])
        mux = self.commands[-1][0]
        self.assertEqual(mux[mux.index("-ss"):mux.index("-ss")+4], ["-ss", "0.125", "-t", "1.250"])
        self.assertIn(["-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy"],
                      [mux[i:i+6] for i in range(len(mux)-5)])
        self.assertEqual(result["outputBytes"], Path(result["path"]).stat().st_size)
        self.assertGreaterEqual(result["encodeSeconds"], result["muxSeconds"])
        self.assertEqual(list(Path(result["path"]).parent.glob(".sphere-*")), [])

    def test_container_tail_uses_video_eof_without_extending_video_or_trimming_audio(self):
        # 59.94 fps video and AAC have different physical EOFs. Container-based
        # rounding would request 301 frames; vf_fps emits only 300.
        self.streams[0].update(duration_ts=600600, time_base="1/60000")
        self.container["format"]["duration"] = "10.024000"
        for kind, start, expected_frames in (("source", 0, 300), ("sphere", 9, 30)):
            with self.subTest(kind=kind), patch.object(proxy, "_run_encoder", side_effect=self.native):
                job = {**self.job, "probe": {**self.probe, "duration": 10.024},
                       "revisionDir": str(self.root / ("eof-" + kind)), "start": start, "end": 10.024}
                result = (prepare_source_proxy if kind == "source" else prepare_sphere_proxy)(job)
                self.assertEqual((result["status"], result["backend"]), ("ready", "metal"), result)
                request = self.requests[-1]
                self.assertEqual((request["sourceIn"], request["sourceOut"], request["frames"]),
                                 (start, 10.01, expected_frames))
                mux = self.commands[-1][0]
                self.assertEqual(mux[mux.index("-ss"):mux.index("-ss") + 4],
                                 ["-ss", f"{start:.3f}", "-t", f"{10.024 - start:.3f}"])

    def test_eof_contract_rejects_out_of_container_range_and_large_track_mismatch(self):
        self.streams[0].update(duration_ts=600600, time_base="1/60000")
        self.container["format"]["duration"] = "10.024000"
        cases = [
            (10.025, "container EOF"),
            (12, "container EOF"),
        ]
        for end, reason in cases:
            with self.subTest(end=end), patch.object(proxy, "_run_encoder", side_effect=self.native):
                result = proxy_metal.render_proxy(self.job, self.source, self.root / "invalid.mp4",
                                                  projection="equirect", start=9, end=end)
                self.assertEqual(result["backend"], "ffmpeg")
                self.assertIn(reason, result["fallbackReason"])
        # Two source frames plus one time-base tick must still fall back.
        other = {**self.streams[0], "index": 5, "id": "0x6", "duration_ts": 598597}
        self.streams.append(other)
        self.job["probe"] = {"sphere": {"projection": "gopro-eac", "face": 256,
                                      "overlap": 64, "tracks": [0, 5]}}
        for stream in (self.streams[0], other):
            stream.update(width=832, height=256)
            stream.update(r_frame_rate="60000/1001", avg_frame_rate="60000/1001")
        with patch.object(proxy, "_run_encoder", side_effect=self.native):
            result = proxy_metal.render_proxy(self.job, self.source, self.root / "mismatch.mp4",
                                              projection="equirect", start=9, end=10.024)
        self.assertEqual(result["backend"], "ffmpeg")
        self.assertIn("different EOF", result["fallbackReason"])
        self.assertEqual(self.commands, [])

    def test_small_dual_track_eof_mismatch_uses_shorter_track_for_whole_and_all_aim_chunks(self):
        # Real one/two-frame GoPro tails. The longer EOF would add output frames.
        for short_ticks, extra_ticks, end in ((23698675, 1001, 395.115), (40665625, 2002, 677.931)):
            for short_first in (False, True):
                with self.subTest(short_ticks=short_ticks, short_first=short_first):
                    first = {**self.streams[0], "duration_ts": short_ticks, "time_base": "1/60000",
                             "width": 832, "height": 256,
                             "r_frame_rate": "60000/1001", "avg_frame_rate": "60000/1001"}
                    second = {**first, "index": 5, "id": "0x6", "duration_ts": short_ticks + extra_ticks}
                    if not short_first:
                        first["duration_ts"], second["duration_ts"] = second["duration_ts"], first["duration_ts"]
                    self.container["streams"] = [first, second]
                    self.container["format"]["duration"] = str(end)
                    job = {**self.job, "probe": {"sphere": {"projection": "gopro-eac", "face": 256,
                                                          "overlap": 64, "tracks": [0, 5]}}}
                    ranges = [("front-back", 0, end)] + [
                        ("equirect", start, min(start + 180, end)) for start in range(0, math.ceil(end), 180)]
                    for projection, start, stop in ranges:
                        with self.subTest(projection=projection, start=start), patch.object(
                                proxy, "_run_encoder", side_effect=self.native):
                            result = proxy_metal.render_proxy(
                                job, self.source, self.root / "shorter.mp4", projection=projection,
                                start=start, end=stop, views=[{}, {}] if projection == "front-back" else None)
                            self.assertEqual(result["backend"], "metal", result)
                            request = self.requests[-1]
                            video_out = min(Fraction(str(stop)), Fraction(short_ticks, 60000))
                            expected = math.floor((video_out - start) * 30 + Fraction(1, 2))
                            self.assertEqual(request["sourceOut"], float(video_out))
                            self.assertEqual(request["frames"], expected)
                            mux = self.commands[-1][0]
                            self.assertEqual(mux[mux.index("-ss"):mux.index("-ss") + 4],
                                             ["-ss", f"{start:.3f}", "-t", f"{stop - start:.3f}"])

    def test_dual_track_frame_rates_must_agree_even_when_eofs_match(self):
        first = {**self.streams[0], "width": 832, "height": 256,
                 "r_frame_rate": "60000/1001", "avg_frame_rate": "60000/1001"}
        second = {**first, "index": 5, "id": "0x6"}
        self.container["streams"] = [first, second]
        job = {**self.job, "probe": {"sphere": {"projection": "gopro-eac", "face": 256,
                                              "overlap": 64, "tracks": [0, 5]}}}
        for key, rate in (("r_frame_rate", "60"), ("avg_frame_rate", "30"),
                          ("r_frame_rate", "0/0"), ("avg_frame_rate", "0")):
            with self.subTest(key=key, rate=rate), patch.dict(second, {key: rate}), patch.object(
                    proxy, "_run_encoder", side_effect=self.native):
                result = proxy_metal.render_proxy(job, self.source, self.root / "rate.mp4",
                                                  projection="equirect", start=0, end=2)
                self.assertEqual(result["backend"], "ffmpeg")
                self.assertIn("frame rates" if rate != "0/0" else "EOF is unavailable", result["fallbackReason"])
                self.assertEqual(self.commands, [])

    def test_exact_video_eof_half_tick_and_rounded_container_representation(self):
        self.streams[0].update(duration_ts=601, time_base="1/60")
        self.container["format"]["duration"] = "10.016667"
        with patch.object(proxy, "_run_encoder", side_effect=self.native):
            result = proxy_metal.render_proxy(self.job, self.source, self.root / "tie.mp4",
                                              projection="equirect", start=9, end=10.017,
                                              inputs=["-ss", "9.000", "-t", "1.017"])
        self.assertEqual(result["backend"], "metal", result)
        self.assertEqual(self.requests[-1]["frames"], 31)
        self.assertEqual(self.requests[-1]["sourceOut"], 601 / 60)

    def test_h264_failure_tries_hevc_then_reports_actual_encoder(self):
        def run(command, **kwargs):
            if "--render" in command and json.loads(Path(command[-1]).read_text())["codec"] == "h264":
                raise ReelsError("hardware H.264 unavailable")
            return self.native(command, **kwargs)
        with patch.object(proxy, "_run_encoder", side_effect=run):
            result = prepare_sphere_proxy({**self.job, "revisionDir": str(self.root / "hevc")})
        self.assertEqual(result["backend"], "metal")
        self.assertEqual(result["encoder"], "hevc_videotoolbox")
        self.assertIn("hardware H.264 unavailable", result["fallbackReason"])

    def test_native_and_audio_failures_are_visible_fallback_not_silent_audio_loss(self):
        for stage in ("native", "audio"):
            with self.subTest(stage=stage):
                def run(command, **kwargs):
                    if stage == "native" or str(command[0]) == "ffmpeg":
                        raise ReelsError(stage + " failed")
                    return self.native(command, **kwargs)
                directory = self.root / stage
                with patch.object(proxy, "_run_encoder", side_effect=run), patch(
                        "reels.editor.worker.encode_proxy", side_effect=self.fallback) as fallback:
                    result = prepare_sphere_proxy({**self.job, "revisionDir": str(directory)})
                self.assertEqual(result["status"], "ready", result)
                self.assertEqual((result["backend"], result["encoder"]), ("ffmpeg", "libx264"))
                self.assertIn(stage + " failed", result["fallbackReason"])
                self.assertEqual(fallback.call_count, 1)
                self.assertEqual(list(directory.rglob("*.native.mp4")), [])
                with patch.object(proxy_metal, "render_proxy", side_effect=AssertionError("warm re-encoded")), patch(
                        "reels.editor.worker.encode_proxy", side_effect=AssertionError("warm re-encoded")):
                    reused = prepare_sphere_proxy({**self.job, "revisionDir": str(directory)})
                self.assertTrue(reused["reused"])
                self.assertEqual(reused["fallbackReason"], result["fallbackReason"])

    def test_required_metal_never_falls_back_or_reuses_cached_ffmpeg(self):
        directory = self.root / "required"
        with patch.object(proxy_metal, "executable", return_value=None), patch(
                "reels.editor.worker.encode_proxy", side_effect=self.fallback):
            first = prepare_sphere_proxy({**self.job, "revisionDir": str(directory)})
        self.assertEqual(first["backend"], "ffmpeg")
        with patch.dict(os.environ, REELS_SPHERE_BACKEND="metal"), patch.object(
                proxy, "_run_encoder", side_effect=ReelsError("native unavailable")), patch(
                "reels.editor.worker.encode_proxy", side_effect=AssertionError("forbidden fallback")):
            result = prepare_sphere_proxy({**self.job, "revisionDir": str(directory)})
        self.assertEqual(result["status"], "unsupported", result)
        self.assertIn("Metal required", result["reason"])
        self.assertEqual(Path(first["path"]).read_bytes(), b"accurate ffmpeg proxy")

    def test_execution_evidence_does_not_break_reuse_and_v4_is_not_reused(self):
        for worker, kind in ((prepare_source_proxy, "source"), (prepare_sphere_proxy, "sphere")):
            with self.subTest(kind=kind):
                job = {**self.job, "revisionDir": str(self.root / (kind + "-version"))}
                with patch.object(proxy, "_run_encoder", side_effect=self.native):
                    first = worker(job)
                marker = Path(first["path"]).with_suffix(".json")
                with patch("reels.editor.worker.render_proxy", side_effect=AssertionError("warm re-encoded")):
                    warm = worker({**job, "runtimeRevision": "unrelated-source-edit"})
                self.assertTrue(warm["reused"])
                self.assertEqual(warm["native"], first["native"])
                saved = json.loads(marker.read_text())
                marker.chmod(0o600)
                marker.write_text(json.dumps({**saved, "version": 4}))
                with patch.object(proxy, "_run_encoder", side_effect=self.native):
                    replaced = worker(job)
                self.assertFalse(replaced["reused"])
                self.assertEqual(json.loads(marker.read_text())["version"], 5)

    def test_native_cover_keeps_request_clock_and_missing_sidecar_below_floor(self):
        job = {**self.job, "revisionDir": str(self.root / "cover"), "stabilize": False}
        with patch.object(proxy, "_run_encoder", side_effect=self.native):
            full = prepare_sphere_proxy(job)
        saved = {p: p.read_bytes() for p in (self.root / "cover").rglob("*") if p.is_file()}
        ori = Orientation([0, 1, 2], [(1, 0, 0, 0), (.98, 0, .2, 0), (1, 0, 0, 0)], (0, 1, 0))
        request = {**job, "start": .25, "end": 1.75, "requestedStart": .5, "requestedEnd": 1.5,
                   "stabilize": "lock", "freeDisk": lambda _: MIN_FREE_BYTES - 1}
        with patch("reels.editor.worker.render_proxy", side_effect=AssertionError("cover re-encoded")), patch(
                "reels.stabilize.read_orientation", return_value=ori):
            warm = prepare_sphere_proxy(request)
        self.assertEqual((warm["start"], warm["end"], warm["backend"]), (0, 2, "metal"))
        self.assertEqual(warm["path"], full["path"])
        self.assertEqual(warm["stabilization"]["start"], .5)
        self.assertEqual(len(warm["stabilization"]["quats"]), 30)
        self.assertEqual(warm["stabilization"]["quats"][0], [1, 0, 0, 0])
        self.assertEqual({p: p.read_bytes() for p in (self.root / "cover").rglob("*") if p.is_file()}, saved)

    def test_capability_matches_proven_gate_for_unsupported_inputs(self):
        for change, reason in (({"pix_fmt": "yuv420p10le"}, "8-bit"), ({"start_time": ".1"}, "origin"),
                               ({"id": "missing"}, "track IDs"), ({"color_space": "bt2020nc"}, "colour")):
            with self.subTest(reason=reason):
                streams = [{**self.streams[0], **change}]
                self.assertIn(reason, proxy_metal.capability(self.probe, streams))
        sd = {**self.streams[0], "width": 512, "height": 256, "color_space": "unknown"}
        self.assertIn("BT.601", proxy_metal.capability(self.probe, [sd]))
        self.assertIn("HDR", proxy_metal.capability({**self.probe, "hdr": True}, self.streams))

    def test_truncation_or_false_native_evidence_never_publishes(self):
        for invalid in ("bytes", "frames"):
            with self.subTest(invalid=invalid):
                directory = self.root / ("bad-" + invalid)
                def run(command, **kwargs):
                    result = self.native(command, **kwargs)
                    if "--render" in command:
                        report = json.loads(result.stdout)
                        report["outputBytes" if invalid == "bytes" else "frames"] += 1
                        result.stdout = json.dumps(report)
                    return result
                with patch.dict(os.environ, REELS_SPHERE_BACKEND="metal"), patch.object(
                        proxy, "_run_encoder", side_effect=run):
                    result = prepare_sphere_proxy({**self.job, "revisionDir": str(directory)})
                self.assertEqual(result["status"], "unsupported")
                self.assertFalse(list(directory.rglob("sphere.json")))
                self.assertFalse(list(directory.rglob("sphere.mp4")))

    def test_native_and_audio_soft_file_limits_cannot_publish_truncation(self):
        for stage in ("native", "audio"):
            with self.subTest(stage=stage):
                directory = self.root / ("truncated-" + stage)

                def run(command, **kwargs):
                    result = self.native(command, **kwargs)
                    if stage == "native" and "--render" in command:
                        request = json.loads(Path(command[-1]).read_text())
                        with Path(request["destination"]).open("wb") as stream:
                            stream.truncate(request["maxOutputBytes"])
                        report = json.loads(result.stdout)
                        report["outputBytes"] = request["maxOutputBytes"]
                        result.stdout = json.dumps(report)
                    if stage == "audio" and str(command[0]) == "ffmpeg":
                        with Path(command[-1]).open("wb") as stream:
                            stream.truncate(kwargs["max_output_bytes"])
                    return result

                with patch.dict(os.environ, REELS_SPHERE_BACKEND="metal"), patch.object(
                        proxy, "_run_encoder", side_effect=run):
                    result = prepare_sphere_proxy({**self.job, "revisionDir": str(directory)})
                self.assertEqual(result["status"], "unsupported", result)
                self.assertFalse(list(directory.rglob("sphere.json")))
                self.assertFalse(list(directory.rglob("*.mp4")))

    def test_killed_native_attempt_partials_are_removed_before_fallback(self):
        directory = self.root / "killed-native"

        def run(command, **kwargs):
            if "--render" in command:
                request = json.loads(Path(command[-1]).read_text())
                (Path(request["destination"]).parent / ".sphere-native-killed.mp4").write_bytes(b"uncommitted video")
                raise ReelsError("killed by hard file limit")
            return self.native(command, **kwargs)

        with patch.object(proxy, "_run_encoder", side_effect=run), patch(
                "reels.editor.worker.encode_proxy", side_effect=self.fallback):
            result = prepare_sphere_proxy({**self.job, "revisionDir": str(directory)})
        self.assertEqual(result["backend"], "ffmpeg", result)
        self.assertIn("hard file limit", result["fallbackReason"])
        self.assertEqual(list(directory.rglob(".*.mp4")), [])

    def test_interrupted_worker_leaves_child_inputs_and_partials_for_locked_cleanup(self):
        for stage in ("native", "audio"):
            with self.subTest(stage=stage):
                directory = self.root / ("interrupted-" + stage)
                partials = []

                def run(command, **kwargs):
                    if "--render" in command and stage == "native":
                        request = json.loads(Path(command[-1]).read_text())
                        path = Path(request["destination"]).parent / ".sphere-active.mp4"
                        path.write_bytes(b"active native writer")
                        partials.extend((path, Path(command[-1])))
                        raise KeyboardInterrupt
                    if str(command[0]) == "ffmpeg" and stage == "audio":
                        path = Path(command[-1])
                        path.write_bytes(b"active mux writer")
                        partials.extend((path, Path(command[command.index("-i") + 1])))
                        raise KeyboardInterrupt
                    return self.native(command, **kwargs)

                with patch.object(proxy, "_run_encoder", side_effect=run):
                    with self.assertRaises(KeyboardInterrupt):
                        prepare_sphere_proxy({**self.job, "revisionDir": str(directory)})
                self.assertTrue(all(path.is_file() for path in partials))
                self.assertFalse(list(directory.rglob("sphere.json")))

    def test_encoder_has_hard_finite_file_limit_even_if_child_ignores_soft_limit(self):
        output = self.root / "oversized.bin"
        with self.assertRaises(ReelsError):
            proxy._run_encoder([sys.executable, "-c",
                                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes(b'x'*100000)", output],
                               timeout=10, max_output_bytes=4096)
        self.assertLessEqual(output.stat().st_size, 4096)


@unittest.skipUnless(REAL, "built macOS Metal helper, sandbox and user's own registered source required")
class ProxyMetalSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="proxy-metal-regression-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.project = Project(ROOT)
        self.asset = json.loads((ROOT / f"library/assets/{ASSET}.json").read_text())
        self.identity = _stat_identity(SOURCE)
        self.workspace = SimpleNamespace(root=self.root / "workspace", code_root=ROOT,
                                         project=self.project, _originals={ASSET: {"path": str(SOURCE)}})
        self.workspace.root.mkdir()
        self.manager = PreviewManager(self.workspace)
        self.enterContext(patch.dict(os.environ, REELS_SPHERE_BACKEND="metal", REELS_SPHERE=str(BINARY)))

    def tearDown(self):
        self.assertEqual(_stat_identity(SOURCE), self.identity)

    def job(self, kind, name):
        job_root = self.manager.worker_root / name
        job_root.mkdir()
        return {"action": kind + "_proxy", "jobRoot": str(job_root), "jobPath": str(job_root / "job.json"),
                "resultPath": str(job_root / "result.json"), "revisionDir": str(self.manager.preview_root / name),
                "sourcePath": str(SOURCE), "sourceIdentity": self.identity, "assetId": ASSET, "sourceKind": "video",
                "mediaRoot": str(SOURCE.parent), "workspaceRoot": str(self.workspace.root),
                "probe": {**deepcopy(self.asset["probe"]), "duration": .4}, "view": {},
                "angles": (self.asset.get("tags") or {}).get("angles") or {}, "start": 0, "end": .4,
                "stabilize": "lock", "revisions": {"renderer": "proxy-metal-regression"}}

    def assert_frame_clock(self, path, frames, start=0):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames",
             "-show_entries", "frame=best_effort_timestamp_time", "-of", "json", str(path)],
            check=True, capture_output=True, text=True, timeout=60)
        timestamps = [float(frame["best_effort_timestamp_time"])
                      for frame in json.loads(result.stdout)["frames"]]
        self.assertEqual(len(timestamps), frames)
        for index, timestamp in enumerate(timestamps):
            self.assertAlmostEqual(timestamp, start + index / 30, places=5)

    def test_cli_prewarm_runtime_selects_native_for_both_private_range_jobs(self):
        from contextlib import redirect_stdout
        import io
        from reels.library import save_asset
        from reels.prewarm import prewarm

        project = Project(self.root / "prewarm-project", code_root=ROOT,
                          media_root=SOURCE.parent, media_read_only=True)
        asset = deepcopy(self.asset)
        asset["probe"]["duration"] = .4
        save_asset(project, asset)
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            result = prewarm(project, [ASSET])
        self.assertEqual((result["prepared"], result["failed"], result["stopped"]), (2, 0, False))
        markers = list((project.root / ".cache/editor/live/preview").rglob("source.json"))
        markers += list((project.root / ".cache/editor/live/preview").rglob("sphere.json"))
        self.assertEqual(len(markers), 2)
        for marker in markers:
            evidence = json.loads(marker.read_text())["execution"]
            self.assertEqual(evidence["backend"], "metal")
            self.assertEqual(evidence["native"]["frames"], 12)

    def test_real_cover_preserves_requested_corrections_and_below_floor_warm_media(self):
        cover_job = self.job("sphere", "real-cover")
        cover_job["stabilize"] = "off"
        full = self.manager._run_worker(cover_job, code_root=ROOT)
        self.assertEqual(full["status"], "ready", full)
        self.assertEqual(full["backend"], "metal", full)
        media = Path(full["path"])
        identity = _stat_identity(media)
        for setting in ("lock", "medium", "off"):
            with self.subTest(setting=setting):
                cold_job = self.job("sphere", "cold-" + setting)
                cold_job.update(start=.1, end=.3, requestedStart=.1, requestedEnd=.3,
                                stabilize=setting)
                cold = self.manager._run_worker(cold_job, code_root=ROOT)
                self.assertEqual(cold["status"], "ready", cold)
                self.assertEqual(cold["backend"], "metal", cold)
                self.assertFalse(cold["reused"])
                self.assertEqual(cold["native"]["frames"], 6)
                self.assert_frame_clock(cold["path"], 6)
                request = {**cover_job, "start": 0, "end": .4, "requestedStart": .1,
                           "requestedEnd": .3, "stabilize": setting}
                # Exercise a real cached native proxy without serializing the free-space
                # callback: this direct worker must compute missing CORI only in memory.
                cache = Path(cover_job["revisionDir"])
                saved = {p: (p.read_bytes(), _stat_identity(p))
                         for p in cache.rglob("*") if p.is_file()}
                below_floor = prepare_sphere_proxy(
                    {**request, "freeDisk": lambda _: MIN_FREE_BYTES - 1})
                self.assertTrue(below_floor["reused"], below_floor)
                self.assertEqual(below_floor["path"], full["path"])
                self.assertEqual({p: (p.read_bytes(), _stat_identity(p))
                                  for p in cache.rglob("*") if p.is_file()}, saved)
                covered = self.manager._run_worker(request, code_root=ROOT)
                self.assertTrue(covered["reused"], covered)
                self.assertEqual(covered["backend"], "metal", covered)
                self.assertEqual(covered["path"], full["path"])
                self.assertEqual((covered["start"], covered["end"]), (0, .4))
                self.assertEqual(_stat_identity(media), identity)
                self.assertTrue(cold["hasOrientation"], cold)
                self.assertIsNotNone(cold["gravity"])
                for key in ("hasOrientation", "gravity", "stabilization", "stabilize"):
                    self.assertEqual(covered[key], cold[key], key)
                    self.assertEqual(below_floor[key], cold[key], key)
                if setting == "off":
                    self.assertIsNone(covered["stabilization"])
                else:
                    correction = covered["stabilization"]
                    self.assertEqual((correction["start"], correction["fps"]), (.1, 30))
                    self.assertEqual(len(correction["quats"]), 6)
                    if setting == "lock":
                        self.assertEqual(correction["quats"][0], [1, 0, 0, 0])
        self.assert_frame_clock(media, 12)

    def test_limited_equirect_own_footage_keeps_range_and_every_pts_in_both_native_paths(self):
        base = self.manager._run_worker(self.job("sphere", "limited-base"), code_root=ROOT)
        self.assertEqual(base["status"], "ready", base)
        self.assertEqual(base["backend"], "metal", base)
        original_dir = self.root / "derived-originals"
        original_dir.mkdir()
        source = original_dir / "limited-own-ride.mp4"
        converted = subprocess.run(
            ["ffmpeg", "-v", "error", "-y", "-i", base["path"], "-t", "0.4",
             "-map", "0:v:0", "-map", "0:a:0?", "-vf",
             "scale=in_range=full:out_range=limited,format=yuv420p",
             "-c:v", "libx264", "-crf", "10", "-bf", "0", "-color_range", "tv",
             "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
             "-c:a", "copy", str(source)],
            capture_output=True, text=True, timeout=120)
        self.assertEqual(converted.returncode, 0, converted.stderr)
        source.chmod(0o400)
        identity = _stat_identity(source)
        video = next(s for s in ffprobe_json(source)["streams"] if s["codec_type"] == "video")
        self.assertEqual((video["pix_fmt"], video["color_range"]), ("yuv420p", "tv"))
        self.assert_frame_clock(source, 12)
        self.workspace._originals["limited-own-ride"] = {"path": str(source)}
        for kind, dimensions in (("source", [1440, 1280]), ("sphere", [2048, 1024])):
            with self.subTest(kind=kind):
                job = self.job(kind, "limited-" + kind)
                job.update(sourcePath=str(source), sourceIdentity=identity, assetId="limited-own-ride",
                           mediaRoot=str(original_dir), angles={}, stabilize="off",
                           probe={"duration": .4, "width": 2048, "height": 1024, "fps": 30,
                                  "sphere": {"projection": "equirect", "width": 2048, "height": 1024}})
                result = self.manager._run_worker(job, code_root=ROOT)
                self.assertEqual(result["status"], "ready", result)
                self.assertEqual(result["backend"], "metal", result)
                self.assertEqual(result["native"]["frames"], 12)
                output = next(s for s in ffprobe_json(Path(result["path"]))["streams"]
                              if s["codec_type"] == "video")
                self.assertEqual([output["width"], output["height"]], dimensions)
                self.assertEqual((output["pix_fmt"], output["color_range"]), ("yuv420p", "tv"))
                self.assertEqual(int(output["nb_frames"]), 12)
                self.assert_frame_clock(result["path"], 12)
                self.assertEqual(_stat_identity(source), identity)

    def test_both_actual_sandbox_workers_require_metal_and_preserve_audio_pts_and_format(self):
        for kind, dimensions in (("source", [1440, 1280]), ("sphere", [2048, 1024])):
            with self.subTest(kind=kind):
                job = self.job(kind, kind)
                result = self.manager._run_worker(job, code_root=ROOT)
                self.assertEqual(result["status"], "ready", result)
                self.assertEqual(result["backend"], "metal", result)
                self.assertIn(result["encoder"], ("h264_videotoolbox", "hevc_videotoolbox"))
                self.assertEqual(result["native"]["frames"], 12)
                self.assertGreater(result["native"]["outputBytes"], 0)
                streams = ffprobe_json(Path(result["path"]))["streams"]
                video = next(s for s in streams if s["codec_type"] == "video")
                self.assertEqual([video["width"], video["height"]], dimensions)
                self.assertEqual(video["pix_fmt"], "yuvj420p")
                self.assertEqual(video["color_range"], "pc")
                for key in ("color_space", "color_primaries", "color_transfer"):
                    self.assertEqual(video[key], "bt709")
                self.assertEqual(video["has_b_frames"], 0)
                self.assertEqual(int(video["nb_frames"]), 12)
                self.assertAlmostEqual(float(video["start_time"]), 0, places=6)
                self.assertAlmostEqual(float(video["duration"]), .4, places=5)
                self.assert_frame_clock(result["path"], 12)
                audio = next(s for s in streams if s["codec_type"] == "audio")
                self.assertEqual(audio["codec_name"], "aac")
                self.assertAlmostEqual(float(audio["duration"]), .4, delta=.04)
                warm = self.manager._run_worker(job, code_root=ROOT)
                self.assertTrue(warm["reused"])
                self.assertEqual(warm["native"], result["native"])

    def test_existing_frozen_renderer_cannot_supply_native_proxy_capability(self):
        frozen = self.root / "frozen"
        shutil.copytree(ROOT / "scripts", frozen / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("proxy.py", "proxy_cache.py", "proxy_stabilize.py", "proxy_metal.py"):
            (frozen / "scripts/reels/editor" / name).unlink()
        (frozen / "scripts/reels/sphere_metal.py").write_text("raise RuntimeError('untrusted native capability imported')\n")
        (frozen / "scripts/reels/editor/worker.py").write_text("raise RuntimeError('untrusted worker imported')\n")
        for kind in ("source", "sphere"):
            result = self.manager._run_worker(self.job(kind, "frozen-" + kind), code_root=frozen)
            self.assertEqual(result["status"], "ready", result)
            self.assertEqual(result["backend"], "metal", result)

    def test_native_and_audio_mux_children_keep_leases_after_python_sigint_and_sigkill(self):
        base = self.manager._run_worker(self.job("sphere", "interruption-base"), code_root=ROOT)
        self.assertEqual(base["backend"], "metal", base)
        streams = ffprobe_json(SOURCE)["streams"]
        sphere = self.asset["probe"]["sphere"]
        indices = sphere["tracks"] if sphere["projection"] == "gopro-eac" else [
            next(s["index"] for s in streams if s.get("codec_type") == "video")]
        videos = [next(s for s in streams if s["index"] == i) for i in indices]
        seconds = min(60, float(self.asset["probe"]["duration"]))
        amount = proxy.proxy_reservation(seconds)
        limit = (amount - 16_000_000) // 2
        code = '''
import json, subprocess, sys
from pathlib import Path
from unittest.mock import patch
from reels.editor.proxy import _run_encoder
from reels.editor.proxy_cache import MIN_FREE_BYTES, proxy_job
job, command = json.loads(sys.argv[1]), json.loads(sys.argv[2])
job['freeDisk'] = lambda _: MIN_FREE_BYTES + int(sys.argv[3])
popen = subprocess.Popen
def launch(*args, **kwargs):
    process = popen(*args, **kwargs)
    print(process.pid, flush=True)
    sys.stdin.readline()
    return process
@proxy_job
def work(job):
    job['reserveOutput']()
    _run_encoder(command, timeout=1800, max_output_bytes=int(sys.argv[4]))
with patch('subprocess.Popen', side_effect=launch):
    work(job)
'''
        for stage in ("native", "audio"):
            for sig in (signal.SIGINT, signal.SIGKILL):
                with self.subTest(stage=stage, signal=sig):
                    folder, prefix = ("spheres", ".sphere-") if stage == "native" else ("sources", ".source-")
                    directory = self.manager.preview_root / folder / f"{stage}-{sig}"
                    directory.mkdir(parents=True)
                    destination = directory / f"{prefix}interrupted.mp4"
                    if stage == "native":
                        request = {
                            "version": 1, "preset": "proxy", "source": str(SOURCE), "destination": str(destination),
                            "sourceProjection": sphere["projection"], "trackIDs": [int(str(s["id"]), 0) for s in videos],
                            "sourceIn": 0, "sourceOut": seconds, "speed": 1, "fps": 30,
                            "frames": math.floor(seconds * 30 + .5), "width": 2048, "height": 1024,
                            "projection": "equirect", "keys": [], "corrections": None, "codec": "h264",
                            "fullRange": videos[0].get("color_range") == "pc" or videos[0]["pix_fmt"].startswith("yuvj"),
                            "maxOutputBytes": limit,
                        }
                        path = directory / "request.json"
                        path.write_text(json.dumps(request))
                        command = [str(BINARY), "--render", str(path)]
                    else:
                        command = ["ffmpeg", "-v", "error", "-y", "-re", "-stream_loop", "-1", "-i", base["path"],
                                   "-ss", "0", "-t", str(seconds), "-i", str(SOURCE),
                                   "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy",
                                   "-c:a", "aac", "-b:a", "160k", "-t", str(seconds),
                                   "-movflags", "+faststart", "-fs", str(limit), str(destination)]
                    job = {"workspaceRoot": str(self.workspace.root), "revisionDir": str(directory),
                           "start": 0, "end": seconds}
                    parent = subprocess.Popen(
                        [sys.executable, "-u", "-c", code, json.dumps(job), json.dumps(command), str(amount), str(limit)],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                    encoder = None
                    try:
                        with selectors.DefaultSelector() as selector:
                            selector.register(parent.stdout, selectors.EVENT_READ)
                            self.assertTrue(selector.select(20), "child did not launch encoder")
                        encoder = int(parent.stdout.readline().strip())

                        def written():
                            return sum(p.stat().st_size for p in directory.glob(".*.mp4") if p.is_file())

                        deadline = time.monotonic() + 20
                        while written() == 0 and time.monotonic() < deadline:
                            time.sleep(.02)
                        self.assertGreater(written(), 0, "real encoder never opened a partial output")
                        os.kill(encoder, signal.SIGSTOP)
                        parent.send_signal(sig)
                        self.assertNotEqual(parent.wait(10), 0)
                        with cache_lock(directory, blocking=False) as acquired:
                            self.assertFalse(acquired, "Python released the active encoder key")
                        old = time.time() - PARTIAL_PROXY_GRACE - 60
                        partials = list(directory.glob(".*.mp4"))
                        self.assertTrue(partials)
                        for partial in partials:
                            os.utime(partial, (old, old))
                        self.assertEqual(remove_partial_proxies(self.workspace.root), [])
                        self.assertTrue(all(partial.is_file() for partial in partials))
                        with self.assertRaises(ProxyDiskFull):
                            with reserve_disk(budget_root(self.workspace.root), amount, lambda _: MIN_FREE_BYTES + amount):
                                self.fail("Python released the active encoder reservation")
                        before = written()
                        os.kill(encoder, signal.SIGCONT)
                        deadline = time.monotonic() + 20
                        while written() <= before and time.monotonic() < deadline:
                            time.sleep(.02)
                        self.assertGreater(written(), before, "orphan encoder stopped writing after Python exited")
                        self.assertFalse((directory / ("sphere.json" if stage == "native" else "source.json")).exists())
                    finally:
                        if parent.poll() is None:
                            parent.kill()
                        if encoder is not None:
                            try:
                                os.kill(encoder, signal.SIGCONT)
                                os.kill(encoder, signal.SIGTERM)
                            except ProcessLookupError:
                                pass
                        parent.communicate(timeout=20)
                        deadline = time.monotonic() + 20
                        while True:
                            with cache_lock(directory, blocking=False) as acquired:
                                if acquired:
                                    break
                            self.assertLess(time.monotonic(), deadline, "orphan encoder did not release its descriptors")
                            time.sleep(.02)
                    partials = list(directory.glob(".*.mp4"))
                    self.assertTrue(partials)
                    old = time.time() - PARTIAL_PROXY_GRACE - 60
                    for partial in partials:
                        os.utime(partial, (old, old))
                    self.assertCountEqual(remove_partial_proxies(self.workspace.root), partials)
                    self.assertTrue(all(not partial.exists() for partial in partials))
                    with reserve_disk(budget_root(self.workspace.root), amount, lambda _: MIN_FREE_BYTES + amount):
                        self.assertEqual(len(list(budget_root(self.workspace.root).glob("*.reserve"))), 1)


if __name__ == "__main__":
    unittest.main()
