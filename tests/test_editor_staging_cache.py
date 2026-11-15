from __future__ import annotations

import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import reels
from reels.editor import worker
from reels.util import ReelsError, quick_hash


class StagingCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.media_root = self.root / "originals"
        self.media_root.mkdir()
        self.cache_root = self.root / "cache"
        self.code_root = self.root / "code"
        (self.code_root / "scripts/reels").mkdir(parents=True)
        (self.code_root / "remotion/src").mkdir(parents=True)
        self.project = types.SimpleNamespace(
            config={"render": {"width": 720, "height": 1280, "punchStageScale": 1.2}},
            abs=lambda path: (self.media_root / path).resolve(strict=True),
        )
        self.snapshot = {"revisions": {"renderer": "renderer-1"}, "runtimeRevision": "runtime-1"}
        self.encodes: list[tuple[str, str, dict]] = []
        self.run_count = 0
        self._install_renderer_mocks()

    def _install_renderer_mocks(self):
        def write_product(seg, out, function, options):
            out.parent.mkdir(parents=True, exist_ok=True)
            payload = f"{seg.asset['id']}|{seg.asset['media']}|{seg.spec}|{options}".encode()
            out.write_bytes(payload)
            if function in ("stage_video", "stage_photo_pan") and seg.spec.get("sphere"):
                out.with_name(f"{out.stem}.cam.cmd").write_text(f"camera-{seg.asset['id']}")
            if function == "stage_photo" and seg.spec.get("fit") == "blurfill":
                out.with_name(f"{out.stem}.bg.jpg").write_text(f"background-{seg.asset['id']}")

        def stage_video(project, seg, out, fps, *, clean=False, scale=1.0, edges=(True, True), audio=True):
            options = {"fps": fps, "clean": clean, "scale": scale, "edges": edges, "audio": audio}
            self.encodes.append((seg.asset["id"], "stage_video", options))
            write_product(seg, Path(out), "stage_video", options)

        def stage_photo(project, seg, out):
            self.encodes.append((seg.asset["id"], "stage_photo", {}))
            write_product(seg, Path(out), "stage_photo", {})

        def stage_photo_pan(project, seg, out, fps):
            options = {"fps": fps}
            self.encodes.append((seg.asset["id"], "stage_photo_pan", options))
            write_product(seg, Path(out), "stage_photo_pan", options)

        def stage(project, built, staging):
            staging.mkdir(parents=True)
            options_by_id = built.get("options", {})
            for seg in built["segs"]:
                if seg.kind == "video":
                    options = options_by_id.get(seg.asset["id"], {})
                    self.render.stage_video(
                        project,
                        seg,
                        staging / f"seg{seg.index}.mp4",
                        built["fps"],
                        clean=options.get("clean", False),
                        scale=options.get("scale", 1.0),
                        edges=options.get("edges", (True, True)),
                        audio=options.get("audio", True),
                    )
                elif (seg.spec.get("sphere") or {}).get("path"):
                    self.render.stage_photo_pan(project, seg, staging / f"seg{seg.index}.mp4", built["fps"])
                else:
                    self.render.stage_photo(project, seg, staging / f"seg{seg.index}.jpg")
                for cut in seg.spec.get("_cutaways") or []:
                    cs = cut["seg"]
                    self.render.stage_video(
                        project, cs, staging / f"cut-{seg.index}.mp4", built["fps"], audio=False
                    )
            if built.get("exhibits"):
                (staging / "exhibit.jpg").write_bytes(b"fixture exhibit")

        self.render = types.ModuleType("reels.render")
        self.render.stage = stage
        self.render.stage_video = stage_video
        self.render.stage_photo = stage_photo
        self.render.stage_photo_pan = stage_photo_pan
        context = patch.dict("sys.modules", {"reels.render": self.render})
        context.start()
        self.addCleanup(context.stop)
        # A full discovery run may already have imported reels.render onto
        # the package. Relative "from reels import render" uses that binding,
        # not only sys.modules; restore both after this isolated fake renderer.
        package_context = patch.object(reels, "render", self.render, create=True)
        package_context.start()
        self.addCleanup(package_context.stop)

    def _seg(self, asset_id: str, *, kind="video", index=0, media=None, **spec_overrides):
        media = media or f"{asset_id}.mp4"
        path = self.media_root / media
        if not path.exists():
            path.write_bytes(f"original-{asset_id}".encode())
        spec = {
            "in": 0.0,
            "out": 3.0,
            "speed": 1.0,
            "frame": "crop",
            "zoom": 1.0,
            "focusX": 0.5,
            "focusY": 0.5,
            "fit": "cover",
            "motion": None,
            "sphere": None,
            "punch": False,
            "blur": None,
            "clean": False,
            "nat": True,
            "bleep": False,
            "tone": None,
            "volume": 1.0,
            "_bleeps": [],
        }
        spec.update(spec_overrides)
        return types.SimpleNamespace(
            asset={
                "id": asset_id,
                "media": media,
                "hash": quick_hash(path),
                "probe": {"width": 1920, "height": 1080, "hasAudio": True},
            },
            spec=spec,
            kind=kind,
            index=index,
            frames=90,
        )

    def _built(self, segs, **kwargs):
        return {
            "segs": segs,
            "fps": 30,
            "sound": {"cleanNat": False, "cues": []},
            "music": None,
            "props": {"music": None, "foregrounds": []},
            "exhibits": [],
            **kwargs,
        }

    def _run(self, built, cache_key):
        self.run_count += 1
        output = self.root / f"stage-{self.run_count}-{cache_key}"
        stats = {}
        reused = worker._stage_cached(
            self.project,
            built,
            output,
            self.cache_root,
            cache_key,
            snapshot=self.snapshot,
            code_root=self.code_root,
            media_stats=stats,
        )
        return output, reused, stats

    def test_trim_reencodes_only_changed_clip_and_text_only_keeps_whole_tree_hit(self):
        first = self._built([self._seg("one", index=0), self._seg("two", index=1)])
        _, reused, stats = self._run(first, "whole-a")
        self.assertFalse(reused)
        self.assertEqual(stats, {"hits": 0, "misses": 2})
        self.assertEqual(len(self.encodes), 2)

        text_edit = self._built([self._seg("one", index=0), self._seg("two", index=1)], caption="new text")
        _, reused, stats = self._run(text_edit, "whole-a")
        self.assertTrue(reused)
        self.assertEqual(stats, {"hits": 2, "misses": 0})
        self.assertEqual(len(self.encodes), 2)

        trimmed = self._built([
            self._seg("one", index=0, **{"out": 2.5}),
            self._seg("two", index=1),
        ])
        _, reused, stats = self._run(trimmed, "whole-b")
        self.assertFalse(reused)
        self.assertEqual(stats, {"hits": 1, "misses": 1})
        self.assertEqual([entry[0] for entry in self.encodes], ["one", "two", "one"])

    def test_reorder_reuses_products_and_remaps_main_and_blurfill_sidecars(self):
        first = self._built([
            self._seg("one", kind="photo", index=0, **{"fit": "blurfill"}),
            self._seg("two", kind="photo", index=1, **{"fit": "blurfill"}),
        ])
        self._run(first, "ordered")
        self.encodes.clear()

        reordered = self._built([
            self._seg("two", kind="photo", index=0, **{"fit": "blurfill"}),
            self._seg("one", kind="photo", index=1, **{"fit": "blurfill"}),
        ])
        output, reused, stats = self._run(reordered, "reordered")
        self.assertFalse(reused)
        self.assertEqual(stats, {"hits": 2, "misses": 0})
        self.assertEqual(self.encodes, [])
        self.assertIn(b"two|", (output / "seg0.jpg").read_bytes())
        self.assertEqual((output / "seg0.bg.jpg").read_text(), "background-two")
        self.assertIn(b"one|", (output / "seg1.jpg").read_bytes())
        self.assertEqual((output / "seg1.bg.jpg").read_text(), "background-one")

    def test_source_spec_flags_renderer_and_runtime_revisions_invalidate(self):
        base = self._built([self._seg("clip", **{"sphere": {"view": {"yaw": 0}}})])
        self._run(base, "warm")
        variants = [
            ("speed", self._built([self._seg("clip", **{"speed": 1.25, "sphere": {"view": {"yaw": 0}}})]), None),
            ("view", self._built([self._seg("clip", **{"sphere": {"view": {"yaw": 15}}})]), None),
            ("clean", self._built([self._seg("clip", **{"sphere": {"view": {"yaw": 0}}})], options={"clip": {"clean": True}}), None),
            ("audio", self._built([self._seg("clip", **{"sphere": {"view": {"yaw": 0}}})], options={"clip": {"audio": False}}), None),
            ("scale", self._built([self._seg("clip", **{"sphere": {"view": {"yaw": 0}}})], options={"clip": {"scale": 1.4}}), None),
            ("edges", self._built([self._seg("clip", **{"sphere": {"view": {"yaw": 0}}})], options={"clip": {"edges": (False, True)}}), None),
        ]
        for index, (label, built, revision) in enumerate(variants):
            with self.subTest(label=label):
                if revision:
                    self.snapshot = revision
                _, _, stats = self._run(built, f"variant-{index}")
                self.assertEqual(stats, {"hits": 0, "misses": 1})
        self.snapshot = {"revisions": {"renderer": "renderer-2"}, "runtimeRevision": "runtime-1"}
        _, _, renderer_stats = self._run(base, "renderer-changed")
        self.assertEqual(renderer_stats, {"hits": 0, "misses": 1})
        self.snapshot = {"revisions": {"renderer": "renderer-1"}, "runtimeRevision": "runtime-2"}
        _, _, runtime_stats = self._run(base, "runtime-changed")
        self.assertEqual(runtime_stats, {"hits": 0, "misses": 1})

        self.snapshot = {"revisions": {"renderer": "renderer-1"}, "runtimeRevision": "runtime-1"}
        source = self.media_root / "clip.mp4"
        source.write_bytes(b"replacement original")
        changed_source = self._seg("clip", **{"sphere": {"view": {"yaw": 0}}})
        _, _, source_stats = self._run(self._built([changed_source]), "source-changed")
        self.assertEqual(source_stats, {"hits": 0, "misses": 1})

    def test_cutaway_is_staged_and_reused_as_picture_only(self):
        cutaway = self._seg("broll")
        primary = self._seg("primary")
        primary.spec["_cutaways"] = [{"seg": cutaway}]
        built = self._built([primary])
        self._run(built, "cutaway-first")
        cutaway_encode = [entry for entry in self.encodes if entry[0] == "broll"]
        self.assertEqual(len(cutaway_encode), 1)
        self.assertFalse(cutaway_encode[0][2]["audio"])

        self.encodes.clear()
        _, reused, stats = self._run(built, "cutaway-second")
        self.assertFalse(reused)
        self.assertEqual(stats, {"hits": 2, "misses": 0})
        self.assertEqual(self.encodes, [])

    def test_photo_pan_restores_camera_sidecar_and_remains_silent(self):
        seg = self._seg(
            "pan",
            kind="photo",
            **{"sphere": {"path": [{"yaw": 0}, {"yaw": 10}], "projection": "equirectangular"}},
        )
        built = self._built([seg])
        output, _, _ = self._run(built, "pan-a")
        self.assertTrue((output / "seg0.cam.cmd").is_file())
        self.encodes.clear()
        output, reused, stats = self._run(built, "pan-b")
        self.assertFalse(reused)
        self.assertEqual(stats, {"hits": 1, "misses": 0})
        self.assertEqual(self.encodes, [])
        self.assertEqual((output / "seg0.cam.cmd").read_text(), "camera-pan")
        self.assertTrue((output / "seg0.mp4").is_file())

    def test_interrupted_staging_never_publishes_a_product_marker(self):
        originals = {
            name: getattr(self.render, name)
            for name in ("stage_video", "stage_photo", "stage_photo_pan")
        }

        def interrupted(project, seg, out, fps, **kwargs):
            Path(out).parent.mkdir(parents=True, exist_ok=True)
            Path(out).write_bytes(b"partial")
            raise RuntimeError("interrupted encoder")

        with patch.object(self.render, "stage_video", interrupted):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self._run(self._built([self._seg("clip")]), "interrupted")
        self.assertFalse(list(self.cache_root.glob("products/*/cache.json")))
        self.assertFalse((self.cache_root / "interrupted").exists())
        for name, original in originals.items():
            self.assertIs(getattr(self.render, name), original)

    def test_corrupt_product_is_a_miss_and_symlink_product_is_rejected(self):
        built = self._built([self._seg("clip")])
        self._run(built, "first")
        product_entries = [path for path in (self.cache_root / "products").iterdir() if path.is_dir()]
        self.assertEqual(len(product_entries), 1)
        entry = product_entries[0]
        (entry / "main").chmod(0o600)
        (entry / "main").write_bytes(b"corrupted")
        _, _, stats = self._run(built, "corrupt")
        self.assertEqual(stats, {"hits": 0, "misses": 1})
        self.assertEqual((entry / "main").read_bytes(), b"corrupted")

        target = self.root / "external-cache"
        target.mkdir()
        (target / "main").write_bytes(b"must not be reused")
        saved = entry.with_name(f"{entry.name}-saved")
        entry.rename(saved)
        entry.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ReelsError, "symlink"):
            self._run(built, "symlink")


if __name__ == "__main__":
    unittest.main()
