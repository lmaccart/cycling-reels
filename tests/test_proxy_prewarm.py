from __future__ import annotations

import io
import json
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from helpers import TempProject, live_workspace
from reels.editor.preview import PreviewManager
from reels.editor.worker import _stat_identity, prepare_source_proxy, prepare_sphere_proxy
from reels.library import save_asset
from reels.prewarm import MIN_FREE_BYTES, _queue_path, prewarm, queue_sources
from reels.proxy import PROXY_VERSION, encode_proxy, proxy_reservation, source_cache_key, sphere_cache_key
from reels.stabilize import Orientation, cached_correction_quats
from reels.util import ReelsError


class EncodingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.output = self.fixture.root / "proxy.mp4"

    def test_hardware_failure_retries_without_hardware_decode(self):
        commands = []

        def run(command, **kwargs):
            commands.append(command)
            if "videotoolbox" in command:
                self.output.write_bytes(b"partial hardware output")
                raise ReelsError("VideoToolbox unavailable")
            self.output.write_bytes(b"software output")

        with patch("reels.proxy.sys.platform", "darwin"), patch("reels.media.run", side_effect=run):
            codec = encode_proxy(Path("input.mp4"), self.output, ["-map", "0:v:0"], seconds=2)
        self.assertEqual(codec, "libx264")
        self.assertEqual(commands[0][commands[0].index("-c:v") + 1], "h264_videotoolbox")
        self.assertEqual(len(commands), 3)
        self.assertNotIn("-hwaccel", commands[-1])
        self.assertEqual(self.output.read_bytes(), b"software output")
        for command in commands:
            self.assertEqual(command[command.index("-g") + 1], "10")
            self.assertEqual(command[command.index("-bf") + 1], "0")

    def test_hardware_success_does_not_encode_again(self):
        with patch("reels.proxy.sys.platform", "darwin"), patch("reels.media.run") as run:
            run.side_effect = lambda *args, **kwargs: self.output.write_bytes(b"hardware output")
            codec = encode_proxy(Path("input.mp4"), self.output, [], seconds=1)
        self.assertEqual(codec, "h264_videotoolbox")
        self.assertEqual(run.call_count, 1)
        self.assertIn("yuv420p", run.call_args.args[0])

    def test_non_mac_uses_software_and_propagates_software_failure(self):
        with patch("reels.proxy.sys.platform", "linux"), patch("reels.media.run", side_effect=ReelsError("bad input")) as run:
            with self.assertRaisesRegex(ReelsError, "bad input"):
                encode_proxy(Path("input.mp4"), self.output, [])
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("-hwaccel", run.call_args.args[0])

    def test_file_limit_never_publishes_truncated_success(self):
        def run(command, **kwargs):
            with self.output.open("wb") as stream:
                stream.truncate(int(command[command.index("-fs") + 1]))
        with patch("reels.media.run", side_effect=run):
            with self.assertRaisesRegex(ReelsError, "truncated"):
                encode_proxy(Path("input.mp4"), self.output, [], seconds=1)

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_encoded_keyframes_bound_scrub_distance_to_ten_frames(self):
        source = self.fixture.root / "keyframe-input.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=512x256:rate=30:duration=2", "-c:v", "libx264", str(source)], check=True)
        for software in (False, True):
            with self.subTest(software=software):
                if software:
                    with patch("reels.proxy.sys.platform", "linux"):
                        encode_proxy(source, self.output, ["-map", "0:v:0"], seconds=2)
                else:
                    encode_proxy(source, self.output, ["-map", "0:v:0"], seconds=2)
                probe = json.loads(subprocess.run(
                    ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_streams",
                     "-show_entries", "frame=key_frame,pict_type:stream=pix_fmt,has_b_frames", "-of", "json", str(self.output)],
                    capture_output=True, text=True, check=True).stdout)
                frames = probe["frames"]
                keys = [i for i, frame in enumerate(frames) if frame["key_frame"]]
                self.assertEqual(len(frames), 60)
                self.assertEqual(keys[0], 0)
                self.assertTrue(all(b - a <= 10 for a, b in zip(keys, keys[1:] + [len(frames)])))
                self.assertFalse(any(frame["pict_type"] == "B" for frame in frames))
                self.assertEqual(probe["streams"][0]["pix_fmt"], "yuv420p")


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.asset = self.fixture.asset("sphere", duration=400)
        self.asset["probe"]["sphere"] = {"projection": "equirect", "width": 512, "height": 256}
        save_asset(self.fixture.project, self.asset)
        self.source = self.fixture.project.abs(self.asset["media"])
        self.workspace = self.fixture.root / "workspace"
        self.workspace.mkdir()
        self.job = {"sourcePath": str(self.source), "sourceIdentity": _stat_identity(self.source),
                    "sourceKind": "video", "assetId": "sphere", "probe": self.asset["probe"],
                    "mediaRoot": str(self.fixture.project.media_dir), "workspaceRoot": str(self.workspace),
                    "revisionDir": str(self.workspace / "spheres"), "start": 0, "end": 180,
                    "view": {"yaw": 0}, "stabilize": "medium"}

    @staticmethod
    def encode(source, output, *args, **kwargs):
        output.write_bytes(b"encoded proxy")

    def test_source_edits_do_not_invalidate_both_proxy_caches(self):
        # Runtime/renderer digests stand for unrelated worker/preview/render edits.
        # Exercise the worker markers as well as the broker's output directories.
        with patch("reels.editor.worker.encode_proxy", side_effect=self.encode), patch("reels.stabilize.read_orientation", return_value=None):
            for worker in (prepare_source_proxy, prepare_sphere_proxy):
                first = worker(self.job)
                with patch("reels.editor.worker.encode_proxy", side_effect=AssertionError("unexpected encode")):
                    again = worker({**self.job, "runtimeRevision": "edited-worker", "revisions": {"renderer": "edited-source"}})
                self.assertTrue(again["reused"])
                self.assertEqual(first["path"], again["path"])
        manager = PreviewManager(live_workspace(self.fixture))
        directories = []
        read_bytes = Path.read_bytes

        def worker(job, **kwargs):
            directories.append(job["revisionDir"])
            output = Path(job["revisionDir"]) / "fixture.mp4"
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"cache fixture")
            return {"path": str(output), "reused": True}

        with patch.object(manager, "_run_worker", side_effect=worker), patch("reels.editor.renderer.RendererManager.resolve_renderer", return_value=self.fixture.root):
            with patch("reels.editor.renderer.RendererManager._active_revision", return_value="before-edit"):
                manager.prepare_source("sphere")
                manager.prepare_sphere("sphere", 1, 3)
            with patch("reels.editor.renderer.RendererManager._active_revision", return_value="after-edit"), patch("pathlib.Path.read_bytes", lambda p: b"edited source" if p.name in ("preview.py", "worker.py") else read_bytes(p)):
                manager.prepare_source("sphere")
                manager.prepare_sphere("sphere", 1, 3)
        self.assertEqual(directories[:2], directories[2:])

    def test_settings_identity_and_version_change_cache_keys(self):
        identity = self.job["sourceIdentity"]
        source = source_cache_key(self.asset, identity, {})
        sphere = sphere_cache_key("sphere", identity, self.asset["probe"])
        self.assertNotEqual(source, source_cache_key(self.asset, identity, {"yaw": 90}))
        self.assertNotEqual(sphere, sphere_cache_key("sphere", {**identity, "size": 999}, self.asset["probe"]))
        with patch("reels.proxy.PROXY_VERSION", PROXY_VERSION + 1):
            self.assertNotEqual(source, source_cache_key(self.asset, identity, {}))
            self.assertNotEqual(sphere, sphere_cache_key("sphere", identity, self.asset["probe"]))

    def test_split_range_reuses_cover_and_preserves_proxy_clock(self):
        with patch("reels.editor.worker.encode_proxy", side_effect=self.encode), patch("reels.stabilize.read_orientation", return_value=None):
            full = prepare_sphere_proxy(self.job)
        with patch("reels.editor.worker.encode_proxy", side_effect=AssertionError("split re-encoded")), patch("reels.stabilize.read_orientation", side_effect=AssertionError("CORI re-read")):
            split = prepare_sphere_proxy({**self.job, "start": 15, "end": 40})
        self.assertTrue(split["reused"])
        self.assertEqual(split["path"], full["path"])
        self.assertEqual((split["start"], split["end"]), (0, 180))
        # A request across a chunk edge must not return incomplete media.
        with patch("reels.editor.worker.encode_proxy", side_effect=self.encode), patch("reels.stabilize.read_orientation", return_value=None):
            crossing = prepare_sphere_proxy({**self.job, "start": 170, "end": 190})
            changed = prepare_sphere_proxy({**self.job, "probe": {**self.job["probe"], "fps": 25}})
        self.assertFalse(crossing["reused"])
        self.assertFalse(changed["reused"])
        self.assertNotEqual(crossing["path"], full["path"])

    def test_cover_is_matched_before_optional_trim_padding(self):
        with patch("reels.editor.worker.encode_proxy", side_effect=self.encode), patch("reels.stabilize.read_orientation", return_value=None):
            full = prepare_sphere_proxy(self.job)
            split = prepare_sphere_proxy({**self.job, "start": 170, "end": 185,
                                          "requestedStart": 175, "requestedEnd": 180})
        self.assertTrue(split["reused"])
        self.assertEqual(split["path"], full["path"])

    def test_cori_cache_survives_calls_and_separates_strength_and_identity(self):
        ori = Orientation([0, 1, 2], [(1, 0, 0, 0), (0.98, 0, 0.2, 0), (1, 0, 0, 0)], (0, 1, 0))
        root = self.workspace / "corrections"
        setting = {"mode": "smooth", "seconds": 0.8}
        with patch("reels.stabilize.read_orientation", return_value=ori) as read:
            first = cached_correction_quats(self.source, root, self.job["sourceIdentity"], 0, 2, 30, setting)
            again = cached_correction_quats(self.source, root, self.job["sourceIdentity"], 0, 2, 30, setting)
            self.assertEqual(first, again)
            self.assertEqual(read.call_count, 1)
            locked = cached_correction_quats(self.source, root, self.job["sourceIdentity"], 0, 2, 30, {"mode": "lock"})
            cached_correction_quats(self.source, root, {"size": 999}, 0, 2, 30, setting)
            self.assertEqual(read.call_count, 3)
        self.assertNotEqual(first["stabilization"]["quats"], locked["stabilization"]["quats"])
        self.assertEqual(first["gravity"], [0, 1, 0])


class PrewarmTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.asset = self.fixture.asset("pano", duration=361)
        self.asset["probe"]["sphere"] = {"projection": "equirect", "width": 512, "height": 256}
        save_asset(self.fixture.project, self.asset)

    def test_disk_guard_reserves_next_output_and_leaves_queue_resumable(self):
        queue_sources(self.fixture.project, [self.asset])
        with patch("reels.editor.worker.prepare_source_proxy") as source, redirect_stdout(io.StringIO()):
            report = prewarm(self.fixture.project, free_disk=lambda _: MIN_FREE_BYTES + proxy_reservation(361) - 1)
        source.assert_not_called()
        self.assertTrue(report["stopped"])
        self.assertEqual(report["writtenBytes"], 0)
        self.assertTrue(_queue_path(self.fixture.project, "pano").exists())

    def test_guard_rechecks_between_jobs_and_chunks_cover_whole_clip(self):
        jobs = []

        def worker(job):
            jobs.append(job)
            return {"status": "ready", "reused": True}

        with patch("reels.editor.worker.prepare_source_proxy", side_effect=worker), patch("reels.editor.worker.prepare_sphere_proxy", side_effect=worker), redirect_stdout(io.StringIO()):
            report = prewarm(self.fixture.project, ["pano"], free_disk=lambda _: MIN_FREE_BYTES + 2_000_000_000)
        self.assertEqual([(j["start"], j["end"]) for j in jobs[1:]], [(0, 180), (180, 360), (360, 361)])
        self.assertEqual(report["reused"], 4)
        jobs.clear()
        frees = iter([MIN_FREE_BYTES + proxy_reservation(361), MIN_FREE_BYTES])
        with patch("reels.editor.worker.prepare_source_proxy", side_effect=worker), patch("reels.editor.worker.prepare_sphere_proxy", side_effect=worker), redirect_stdout(io.StringIO()):
            report = prewarm(self.fixture.project, ["pano"], free_disk=lambda _: next(frees))
        self.assertTrue(report["stopped"])
        self.assertEqual(len(jobs), 1)

    def test_ingest_queues_only_new_360_video_and_preserves_failed_jobs(self):
        from reels.ingest import ingest

        incoming = self.fixture.project.inbox / "new.mp4"
        incoming.write_bytes(b"new spherical clip")
        probe = {**self.asset["probe"], "hasAudio": False}
        with patch("reels.ingest.probe_video", return_value=probe), patch("reels.ingest.make_video_sheets", return_value=[]), patch("reels.telemetry.run_telemetry"), patch("reels.prewarm.shutil.disk_usage", return_value=type("Disk", (), {"free": MIN_FREE_BYTES})()), redirect_stdout(io.StringIO()):
            new = ingest(self.fixture.project, speech=False)
        self.assertEqual(len(new), 1)
        self.assertTrue(_queue_path(self.fixture.project, new[0]["id"]).is_file())
        flat = self.fixture.asset("flat")
        photo = self.fixture.asset("photo", kind="photo")
        self.assertEqual(queue_sources(self.fixture.project, [flat, photo]), [])

    @unittest.skipUnless(shutil.which("ffmpeg"), "needs ffmpeg")
    def test_tiny_clip_prewarms_real_media_reused_by_editor(self):
        media = self.fixture.project.abs(self.asset["media"])
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                        "testsrc2=size=256x128:rate=30:duration=0.6", "-c:v", "libx264", str(media)], check=True)
        self.asset["probe"].update(duration=0.6, width=256, height=128)
        self.asset["probe"]["sphere"].update(width=256, height=128)
        save_asset(self.fixture.project, self.asset)
        before = media.read_bytes()
        queue_sources(self.fixture.project, [self.asset])
        with redirect_stdout(io.StringIO()) as output:
            report = prewarm(self.fixture.project, free_disk=lambda _: MIN_FREE_BYTES + 1_000_000_000)
        self.assertEqual((report["prepared"], report["failed"]), (2, 0))
        self.assertGreater(report["writtenBytes"], 0)
        self.assertIn(f"{report['writtenBytes']:,} bytes written", output.getvalue())
        self.assertFalse(_queue_path(self.fixture.project, "pano").exists())
        manager = PreviewManager(live_workspace(self.fixture))
        # Use real worker logic without launching a sandbox in this portable test.
        def worker(job, **kwargs):
            return (prepare_source_proxy if job["action"] == "source_proxy" else prepare_sphere_proxy)(job)
        with patch.object(manager, "_run_worker", side_effect=worker), patch("reels.editor.worker.encode_proxy", side_effect=AssertionError("prewarm miss")), patch("reels.stabilize.read_orientation", return_value=None):
            self.assertTrue(manager.prepare_source("pano")["reused"])
            aim = manager.prepare_sphere("pano", 0.1, 0.4, stabilize="medium")
            self.assertTrue(aim["reused"])
        streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", aim["path"]],
                                            capture_output=True, text=True, check=True).stdout)["streams"]
        self.assertEqual((streams[0]["width"], streams[0]["height"]), (2048, 1024))
        self.assertEqual(media.read_bytes(), before)
        with redirect_stdout(io.StringIO()):
            repeated = prewarm(self.fixture.project, ["pano"], free_disk=lambda _: MIN_FREE_BYTES + 1_000_000_000)
        self.assertEqual((repeated["reused"], repeated["writtenBytes"]), (2, 0))


if __name__ == "__main__":
    unittest.main()
