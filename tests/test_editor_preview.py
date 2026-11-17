from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import types
import unittest
from unittest.mock import patch
from pathlib import Path

from reels.config import Project
from reels.editor.preview import PreviewManager
from reels.editor.worker import (
    _stat_identity,
    alpha_foreground_fallback_reason,
    prepare,
    prepare_source_proxy,
)
from reels.util import quick_hash, write_json


class _Workspace:
    def __init__(self, root: Path, code_root: Path):
        self.root = root / "workspace"
        self.data_root = self.root / "data"
        self.data_root.mkdir(parents=True)
        self.media_root = root / "originals"
        self.media_root.mkdir()
        self._originals: dict = {}
        self.fresh_revision_calls = 0
        self.code_root = code_root
        self.project = Project(self.data_root, code_root=code_root, media_root=self.media_root)

    def library_revision(self) -> str:
        digest = hashlib.sha256()
        for base in (self.data_root / "library/assets", self.data_root / "library/transcripts"):
            if base.exists():
                for path in sorted(base.rglob("*.json")):
                    digest.update(path.relative_to(self.data_root).as_posix().encode())
                    digest.update(path.read_bytes())
        digest.update(json.dumps(self._originals, sort_keys=True).encode())
        return digest.hexdigest()

    def refresh_library_revision(self) -> str:
        self.fresh_revision_calls += 1
        return self.library_revision()


class EditorPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.workspace = _Workspace(self.root, Path(__file__).resolve().parents[1])
        self.manager = PreviewManager(self.workspace)

    def test_copy_inputs_freezes_private_data_and_rejects_stale_library_revision(self):
        plan = {"slug": "draft", "segments": [{"text": "frozen"}]}
        write_json(self.workspace.data_root / "plans/draft.json", {"slug": "draft", "segments": []})
        snapshot = {
            "plan": plan,
            "revisions": {"document": "doc-1", "library": self.workspace.library_revision(), "renderer": "renderer-1"},
        }
        job_root = self.manager.worker_root / "snapshot-test"
        job_root.mkdir()
        self.addCleanup(lambda: __import__("shutil").rmtree(job_root, ignore_errors=True))
        self.workspace.fresh_revision_calls = 0
        copied = self.manager._copy_inputs(snapshot, job_root)
        self.assertEqual(self.workspace.fresh_revision_calls, 2, "freeze must check fresh inputs before and after copying")
        self.assertEqual(json.loads((copied / "plans/draft.json").read_text()), {"slug": "draft", "segments": []})
        (self.workspace.data_root / "library/transcripts/changed.json").parent.mkdir(parents=True, exist_ok=True)
        (self.workspace.data_root / "library/transcripts/changed.json").write_text("{}")
        with self.assertRaisesRegex(Exception, "stale"):
            self.manager._copy_inputs(snapshot, self.manager.worker_root / "stale-snapshot")

    def test_prepare_refuses_to_fall_back_from_an_unavailable_renderer_revision(self):
        snapshot = {
            "plan": {"slug": "draft", "segments": [{"asset": "fixture", "in": 0, "out": 1}]},
            "revisions": {"document": "doc-1", "library": self.workspace.library_revision(), "renderer": "missing-r"},
        }
        with self.assertRaisesRegex(Exception, "unavailable"):
            self.manager.prepare(snapshot)

    def test_text_only_fields_do_not_change_stage_spec_but_visual_effects_do(self):
        from reels.editor.worker import _stage_spec

        baseline = {"in": 1.0, "out": 3.0, "speed": 1.0, "captions": "transcript", "text": "old", "hook": "old"}
        text_edit = {**baseline, "captions": "off", "text": "new", "hook": "new"}
        self.assertEqual(_stage_spec(baseline), _stage_spec(text_edit))
        effect_edit = {**baseline, "blur": [{"strength": 5, "keys": [[0, 0.5, 0.5, 0.1, 0.1]]}]}
        self.assertNotEqual(_stage_spec(baseline), _stage_spec(effect_edit))
        trim_edit = {**baseline, "in": 1.5}
        self.assertNotEqual(_stage_spec(baseline), _stage_spec(trim_edit))

    def test_360_source_monitor_requests_a_private_reframed_proxy(self):
        media = self.workspace.media_root / "spherical.360"
        media.write_bytes(b"fixture360")
        identifier = "library/media/spherical.360"
        stat = media.stat()
        identity = {
            "dev": stat.st_dev,
            "ino": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "quick_hash": quick_hash(media),
        }
        self.workspace._originals[identifier] = {
            "path": str(media),
            "asset_id": "spherical",
            "identity": identity,
        }
        self.workspace.project = Project(
            self.workspace.data_root,
            code_root=self.workspace.code_root,
            media_root=self.workspace.media_root,
        )
        write_json(self.workspace.data_root / "library/assets/spherical.json", {
            "id": "spherical", "media": identifier, "kind": "video",
            "probe": {"sphere": {"projection": "gopro-eac", "face": 16, "overlap": 2, "tracks": [0, 1]}},
        })
        proxy = self.manager.preview_root / "sources/fixture/source.mp4"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"private-view-specific-proxy")
        evidence = {"backend": "metal", "encoder": "h264_videotoolbox", "fallbackReason": None,
                    "native": {"renderer": "metal", "frames": 30}}
        with patch.object(self.manager, "_run_worker", return_value={"path": str(proxy), **evidence}) as run_worker:
            result = self.manager.prepare_source("spherical", {"yaw": 90, "pitch": -5, "fov": 70})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(Path(result["path"]), proxy.resolve())
        self.assertTrue(result["proxy"])
        self.assertEqual({key: result[key] for key in evidence}, evidence)
        job = run_worker.call_args.args[0]
        self.assertEqual(job["action"], "source_proxy")
        self.assertEqual(job["view"]["yaw"], 90)
        self.assertEqual(job["sourceKind"], "video")

    def _register_source(self, asset_id: str, filename: str, kind: str, probe: dict) -> Path:
        media = self.workspace.media_root / filename
        media.write_bytes(b"isolated-source-fixture")
        identity = _stat_identity(media)
        identifier = f"library/media/{filename}"
        self.workspace._originals[identifier] = {
            "path": str(media),
            "asset_id": asset_id,
            "identity": identity,
        }
        self.workspace.project = Project(
            self.workspace.data_root,
            code_root=self.workspace.code_root,
            media_root=self.workspace.media_root,
        )
        write_json(self.workspace.data_root / f"library/assets/{asset_id}.json", {
            "id": asset_id,
            "kind": kind,
            "media": identifier,
            "hash": identity["quick_hash"],
            "probe": probe,
        })
        return media

    def test_360_photo_source_uses_png_proxy_and_native_image_kind(self):
        self._register_source(
            "sphere-photo",
            "sphere-photo.jpg",
            "photo",
            {"width": 4000, "height": 2000, "sphere": {"projection": "equirect"}},
        )
        proxy = self.manager.preview_root / "sources/fixture/source.png"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"private-view-specific-image-proxy")
        with patch.object(self.manager, "_run_worker", return_value={"path": str(proxy), "sourceKind": "image"}) as run_worker:
            result = self.manager.prepare_source("sphere-photo", {"yaw": 120, "pitch": -10, "fov": 80})
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["sourceKind"], "image")
        self.assertEqual(Path(result["path"]).suffix, ".png")
        self.assertTrue(result["proxy"])
        self.assertEqual(run_worker.call_args.args[0]["sourceKind"], "photo")

    def test_nonstandard_video_container_or_codec_requests_compatibility_proxy(self):
        self._register_source(
            "unsupported-video",
            "unsupported-video.webm",
            "video",
            {"width": 1280, "height": 720, "fps": 29.97, "vcodec": "vp9", "hasAudio": True},
        )
        proxy = self.manager.preview_root / "sources/fixture/source.mp4"
        proxy.parent.mkdir(parents=True)
        proxy.write_bytes(b"private-compatible-video-proxy")
        with patch.object(self.manager, "_run_worker", return_value={"path": str(proxy)}) as run_worker:
            result = self.manager.prepare_source("unsupported-video")
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["proxy"])
        self.assertEqual(run_worker.call_args.args[0]["sourceKind"], "video")
        self.assertEqual(run_worker.call_args.args[0]["probe"]["vcodec"], "vp9")

    def test_generic_video_proxy_preserves_audio_aspect_and_source_clock(self):
        source = self._register_source(
            "compat-video",
            "compat-video.webm",
            "video",
            {"width": 1920, "height": 1080, "fps": 29.97, "vcodec": "vp9"},
        )
        output_root = self.workspace.root / "preview/sources/compat"
        identity = _stat_identity(source)
        commands = []

        def fake_run(command, **_kwargs):
            commands.append([str(value) for value in command])
            Path(command[-1]).write_bytes(b"encoded-fixture")

        job = {
            "sourcePath": str(source),
            "sourceIdentity": identity,
            "sourceKind": "video",
            "probe": {"width": 1920, "height": 1080, "fps": 29.97, "vcodec": "vp9"},
            "revisionDir": str(output_root.resolve()),
            "mediaRoot": str(self.workspace.media_root),
            "workspaceRoot": str(self.workspace.root.resolve()),
            "assetId": "compat-video",
            "revisions": {"renderer": "fixture-renderer"},
            "runtimeRevision": "fixture-runtime",
        }
        with patch("reels.media.run", side_effect=fake_run):
            result = prepare_source_proxy(job)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["sourceKind"], "video")
        command = commands[0]
        self.assertIn("0:a:0?", command)
        self.assertIn("aac", command)
        self.assertIn("passthrough", command)
        self.assertIn("force_original_aspect_ratio=decrease", command[command.index("-vf") + 1])
        self.assertNotIn("crop=", command[command.index("-vf") + 1])
        marker = json.loads((output_root / "source.json").read_text())
        self.assertEqual(marker["kind"], "video")
        self.assertEqual(marker["sourceCodec"], "vp9")
        self.assertEqual(marker["fps"], 29.97)

    def test_360_video_proxy_reframes_the_requested_view_and_keeps_optional_audio(self):
        source = self._register_source(
            "sphere-video-worker",
            "sphere-video-worker.360",
            "video",
            {
                "width": 3840,
                "height": 1920,
                "fps": 29.97,
                "vcodec": "h264",
                "sphere": {"projection": "equirect", "width": 3840, "height": 1920},
            },
        )
        output_root = self.workspace.root / "preview/sources/sphere-video"
        identity = _stat_identity(source)
        commands = []

        def fake_run(command, **_kwargs):
            commands.append([str(value) for value in command])
            Path(command[-1]).write_bytes(b"360-video fixture")

        job = {
            "sourcePath": str(source),
            "sourceIdentity": identity,
            "sourceKind": "video",
            "probe": {
                "width": 3840,
                "height": 1920,
                "fps": 29.97,
                "vcodec": "h264",
                "sphere": {"projection": "equirect", "width": 3840, "height": 1920},
            },
            "view": {"yaw": 90, "pitch": -5, "fov": 70},
            "revisionDir": str(output_root),
            "mediaRoot": str(self.workspace.media_root),
            "workspaceRoot": str(self.workspace.root),
            "assetId": "sphere-video-worker",
            "revisions": {"renderer": "renderer-360"},
            "runtimeRevision": "runtime-360",
        }
        with patch("reels.editor.proxy._run_encoder", side_effect=fake_run):
            result = prepare_source_proxy(job)
        self.assertEqual(result["status"], "ready")
        command = commands[0]
        self.assertIn("0:a:0?", command)
        self.assertIn("aac", command)
        self.assertIn("v360=input=e:output=flat", command[command.index("-filter_complex") + 1])
        self.assertIn("yaw=90.000", command[command.index("-filter_complex") + 1])
        self.assertEqual(json.loads((output_root / "source.json").read_text())["kind"], "360-video")

    def test_360_photo_worker_uses_sphere_photo_view_and_rechecks_identity(self):
        source = self._register_source(
            "sphere-photo-worker",
            "sphere-photo-worker.jpg",
            "photo",
            {"width": 4000, "height": 2000, "sphere": {"projection": "equirect"}},
        )
        output_root = self.workspace.root / "preview/sources/sphere-photo"
        identity = _stat_identity(source)
        calls = []

        def fake_photo_view(src, out, view, width, height, projection):
            calls.append((src, view, width, height, projection))
            out.write_bytes(b"png fixture")

        job = {
            "sourcePath": str(source),
            "sourceIdentity": identity,
            "sourceKind": "photo",
            "probe": {"width": 4000, "height": 2000, "sphere": {"projection": "equirect"}},
            "view": {"yaw": 45, "pitch": -4, "fov": 72},
            "revisionDir": str(output_root),
            "mediaRoot": str(self.workspace.media_root),
            "workspaceRoot": str(self.workspace.root),
            "assetId": "sphere-photo-worker",
            "revisions": {"renderer": "renderer-r1"},
            "runtimeRevision": "runtime-r1",
        }
        with patch("reels.sphere.photo_view", side_effect=fake_photo_view):
            result = prepare_source_proxy(job)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(Path(result["path"]).suffix, ".png")
        self.assertEqual(result["sourceKind"], "image")
        self.assertEqual(calls[0][2:], (720, 1280, "flat"))
        marker = json.loads((output_root / "source.json").read_text())
        self.assertEqual(marker["codec"]["video"], "png")
        self.assertEqual(marker["view"]["yaw"], 45)

    def test_alpha_foregrounds_force_exact_mode_in_the_same_prepare_job(self):
        reason = alpha_foreground_fallback_reason({"foregrounds": [{"type": "fixture"}]})
        self.assertTrue(reason)
        self.assertIsNone(alpha_foreground_fallback_reason({"foregrounds": []}))

        output_root = self.workspace.root / "preview/alpha-exact"
        job = {
            "jobRoot": str(self.workspace.root),
            "dataRoot": str(self.workspace.data_root),
            "codeRoot": str(self.workspace.code_root),
            "revisionDir": str(output_root.resolve()),
            "cacheRoot": str(self.workspace.root / "cache"),
            "bundleCache": str(self.workspace.root / "bundle-cache"),
            "workspaceRoot": str(self.workspace.root.resolve()),
            "mediaRoot": str(self.workspace.media_root),
            "originals": {},
            "plan": {"slug": "alpha-fixture"},
            "revisions": {"document": "d1", "library": "l1", "renderer": "r1"},
            "mode": "interactive",
        }
        built = {
            "warnings": [],
            "props": {"width": 720, "height": 1280, "foregrounds": [{"type": "fixture-alpha"}]},
            "fps": 30,
            "total": 30,
            "segs": [],
            "exhibits": [],
            "sound": {},
        }
        fake_render = types.ModuleType("reels.render")
        fake_render.build = lambda _project, _plan: built
        with (
            patch.dict("sys.modules", {"reels.render": fake_render}),
            patch("reels.editor.worker._cache_identity", return_value=("cache-key", {"segments": []})),
            patch("reels.editor.worker._stage_cached", return_value=False),
            patch("reels.editor.worker._verify_sources"),
            patch("reels.editor.worker._build_bundle", return_value=self.workspace.root / "bundle"),
            patch("reels.editor.worker._run_exact", return_value={"exactPath": str(output_root / "exact.mp4")}) as run_exact,
        ):
            result = prepare(job)
        self.assertEqual(result["mode"], "exact")
        self.assertEqual(result["requestedMode"], "interactive")
        self.assertEqual(result["fallbackReason"], reason)
        self.assertEqual(len(result["warnings"]), 1)
        run_exact.assert_called_once()

    def test_usage_merge_is_freshness_checked_and_transaction_facade_scoped(self):
        revisions = {"document": "d1", "library": "l1", "renderer": "r1"}
        export_root = self.workspace.root / "exports/e1"
        export_root.mkdir(parents=True)
        receipt = {
            "exportRoot": str(export_root),
            "sideEffects": "private-snapshot-only",
            "revisions": revisions,
            "usageUpdates": {"assets": {"asset": [{"plan": "draft"}]}, "music": {"song": ["draft"]}},
        }
        called = []
        result = self.manager.merge_usage_fields(receipt, revisions, revisions, lambda updates, revs: called.append((updates, revs)))
        self.assertIsNone(result)
        self.assertEqual(called[0][0], receipt["usageUpdates"])
        with self.assertRaisesRegex(Exception, "stale"):
            self.manager.merge_usage_fields(receipt, revisions, {**revisions, "document": "d2"}, lambda *_: self.fail())

    @unittest.skipUnless(shutil.which("sandbox-exec"), "macOS sandbox-exec is unavailable")
    def test_real_worker_boundary_probe_allows_private_writes_and_denies_original_fixture(self):
        result = self.manager.verify_sandbox_boundary()
        self.assertEqual(result, {"status": "verified", "fixture": "isolated-original-write-denial"})

    @unittest.skipUnless(
        os.environ.get("REELS_RUN_SANDBOX_RENDER_PROOF") == "1",
        "set REELS_RUN_SANDBOX_RENDER_PROOF=1 for the opt-in real browser/render proof",
    )
    def test_sandboxed_exact_preview_and_private_export_fixture(self):
        from reels.editor.renderer import RendererManager

        media = (self.workspace.media_root / "sandbox-fixture.mp4").resolve()
        ffmpeg = shutil.which("ffmpeg")
        self.assertIsNotNone(ffmpeg)
        generated = subprocess.run(
            [
                str(ffmpeg), "-v", "error", "-y", "-f", "lavfi", "-i",
                "testsrc2=size=360x640:rate=24", "-t", "2", "-an",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(generated.returncode, 0, generated.stderr[-2000:])
        stat = media.stat()
        identity = {
            "dev": stat.st_dev,
            "ino": stat.st_ino,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns,
            "quick_hash": quick_hash(media),
        }
        identifier = "library/media/sandbox-fixture.mp4"
        self.workspace._originals[identifier] = {
            "path": str(media),
            "asset_id": "sandbox-fixture",
            "identity": identity,
        }
        self.workspace.project = Project(
            self.workspace.data_root,
            code_root=self.workspace.code_root,
            media_root=self.workspace.media_root.resolve(),
        )
        self.workspace.project.ensure_dirs()
        write_json(self.workspace.data_root / "library/assets/sandbox-fixture.json", {
            "id": "sandbox-fixture",
            "kind": "video",
            "media": identifier,
            "hash": identity["quick_hash"],
            "originalName": "sandbox-fixture.mp4",
            "created": "2026-11-14T00:00:00",
            "status": "tagged",
            "probe": {
                "duration": 2.0, "fps": 24.0, "width": 360, "height": 640,
                "hasAudio": False,
            },
            "tags": {
                "type": "solo-ride", "shot": "pov", "quality": 4,
                "summary": "isolated sandbox fixture", "moments": [],
            },
            "transcript": None,
            "used": [],
        })
        snapshot = {
            "plan": {
                "slug": "sandbox-proof",
                "autoSfx": False,
                "segments": [{"asset": "sandbox-fixture", "in": 0, "out": 2}],
            },
            "revisions": {
                "document": "fixture-document-r1",
                "library": self.workspace.library_revision(),
                "renderer": RendererManager(self.workspace)._working_revision(),
            },
        }

        exact = self.manager.prepare(snapshot, mode="exact")
        exact_path = Path(exact["exactPath"])
        self.assertGreater(exact_path.stat().st_size, 0)
        self.assertEqual(exact["revisions"], snapshot["revisions"])

        receipt = self.manager.export(snapshot)
        reel_path = Path(receipt["reelPath"])
        self.assertGreater(reel_path.stat().st_size, 0)
        self.assertTrue(reel_path.is_relative_to(Path(receipt["exportRoot"]).resolve()))
        self.assertEqual(receipt["sideEffects"], "private-snapshot-only")
        self.assertEqual(receipt["usageUpdates"]["assets"]["sandbox-fixture"][0]["plan"], "sandbox-proof")
        live_asset = json.loads((self.workspace.data_root / "library/assets/sandbox-fixture.json").read_text())
        self.assertEqual(live_asset["used"], [])
        self.assertEqual(
            self.manager.verify_sandbox_boundary(),
            {"status": "verified", "fixture": "isolated-original-write-denial"},
        )


if __name__ == "__main__":
    unittest.main()
