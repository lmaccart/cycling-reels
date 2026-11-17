from __future__ import annotations

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import reels
from reels.editor import worker
from reels.util import ReelsError, quick_hash


def _evict(trim, entry: Path) -> bool:
    """Evict one cache folder the way the trim does (by its place under preview/), whatever spelling of the path a reader holds."""
    import os
    key = Path(os.path.realpath(entry)).relative_to(os.path.realpath(trim.preview_root)).parts
    return trim._evict(key)


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

    def test_helper_runtime_identity_invalidates_whole_tree_and_same_identity_hits(self):
        built = self._built([self._seg("clip", **{"sphere": {"view": {"yaw": 0}}})])
        # The broker folds the helper binary/metallib digest into runtimeRevision.
        # Compute the aggregate key exactly as prepare() does on every request.
        for runtime, expected_reuse, expected_encodes in [
            ("a" * 64, False, 1),
            ("a" * 64, True, 1),
            ("b" * 64, False, 2),
            ("b" * 64, True, 2),
        ]:
            with self.subTest(runtime=runtime, reused=expected_reuse):
                self.snapshot["runtimeRevision"] = runtime
                key, _ = worker._cache_identity(self.project, built, self.snapshot, self.code_root)
                _, reused, stats = self._run(built, key)
                self.assertEqual(reused, expected_reuse)
                self.assertEqual(stats, {"hits": 1, "misses": 0} if expected_reuse else {"hits": 0, "misses": 1})
                self.assertEqual(len(self.encodes), expected_encodes)

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

    def test_non_directory_product_is_rejected(self):
        built = self._built([self._seg("clip")])
        self._run(built, "first")
        (entry,) = (self.cache_root / "products").iterdir()
        worker._discard_tree(entry)
        entry.write_bytes(b"not a directory")
        with self.assertRaisesRegex(ReelsError, "not a directory"):
            self._run(built, "non-directory")

    def test_symlink_and_non_directory_aggregate_entries_are_rejected(self):
        built = self._built([self._seg("clip")])
        self.cache_root.mkdir()
        target = self.root / "external-cache"
        target.mkdir()
        (self.cache_root / "symlink").symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(ReelsError, "symlink"):
            self._run(built, "symlink")
        (self.cache_root / "non-directory").write_bytes(b"not a directory")
        with self.assertRaisesRegex(ReelsError, "not a regular directory"):
            self._run(built, "non-directory")

    # -- clones instead of copies, and a cache the editor may trim underneath a prepare ----------------

    def _use_preview_layout(self):
        """Put the cache where the editor does, so the real trim can evict from it."""
        self.cache_root = self.root / "preview/cache/staged-media/renderer-x"
        from reels.editor.preview_cache import PreviewCache
        self.addCleanup(self._unlock_tree)
        return PreviewCache(self.root, budget=0, grace=0)

    def _unlock_tree(self):
        import os
        for current, dirs, _ in os.walk(self.root):
            for name in dirs:
                os.chmod(os.path.join(current, name), 0o700)

    def _assert_independent_copy(self, entry: Path, stage: Path):
        names = sorted(path.name for path in entry.iterdir() if path.name != "cache.json")
        self.assertEqual(sorted(path.name for path in stage.iterdir()), names)
        for name in names:
            cached, staged = entry / name, stage / name
            self.assertEqual(cached.read_bytes(), staged.read_bytes())
            self.assertNotEqual(cached.stat().st_ino, staged.stat().st_ino)
            self.assertEqual((cached.stat().st_nlink, staged.stat().st_nlink), (1, 1))
        worker._cache_tree_safe(entry)  # clones are separate inodes: the one-link invariant holds on both sides
        worker._cache_tree_safe(stage)
        self.assertEqual(stage.stat().st_mode & 0o777, 0o700)
        return names

    def test_revision_staging_is_cloned_from_the_cache_entry_on_miss_and_hit(self):
        import os
        built = self._built([self._seg("one", index=0), self._seg("two", index=1)])
        probe = self.root / "probe"
        probe.write_bytes(b"x")
        supported = worker._clonefile(probe, self.root / "probe-clone")
        calls: list[tuple[str, bool]] = []
        real = worker._clonefile

        def spy(source, destination):
            result = real(source, destination)
            calls.append((source.name, result))
            return result

        with patch.object(worker, "_clonefile", spy):
            built_stage, reused, _ = self._run(built, "clone")
            hit_stage, hit_reused, _ = self._run(built, "clone")
        self.assertEqual((reused, hit_reused), (False, True))
        entry = self.cache_root / "clone"
        for stage in (built_stage, hit_stage):
            names = self._assert_independent_copy(entry, stage)
            self.assertFalse((stage / "cache.json").exists())
            self.assertEqual(sorted(name for name, _ in calls[:len(names)]), names)
        self.assertEqual([result for _, result in calls], [supported] * len(calls))
        # Rewriting a revision's copy (props, audio) never reaches the shared cache entry.
        target = hit_stage / names[0]
        target.chmod(0o600)
        target.write_bytes(b"rewritten for this revision")
        self.assertNotEqual((entry / names[0]).read_bytes(), b"rewritten for this revision")
        worker._cache_tree_safe(entry)
        self.assertTrue(all(os.stat(entry / name).st_nlink == 1 for name in names))

    def test_unsupported_cloning_falls_back_to_a_plain_copy(self):
        built = self._built([self._seg("one", index=0), self._seg("two", index=1)])
        with patch.object(worker, "_clonefile", return_value=False) as clone:
            first, reused, first_stats = self._run(built, "copy")
            second, hit_reused, hit_stats = self._run(built, "copy")
        self.assertGreaterEqual(clone.call_count, 4)
        self.assertEqual((reused, hit_reused), (False, True))
        self.assertEqual((first_stats, hit_stats), ({"hits": 0, "misses": 2}, {"hits": 2, "misses": 0}))
        for stage in (first, second):
            self._assert_independent_copy(self.cache_root / "copy", stage)

    @unittest.skipUnless(sys.platform == "darwin", "clonefile is macOS-only")
    def test_a_missing_clone_source_is_reported_as_missing_not_as_unsupported(self):
        with self.assertRaises(FileNotFoundError):
            worker._clonefile(self.root / "no-such-file", self.root / "no-such-clone")
        (self.root / "present").write_bytes(b"x")
        (self.root / "taken").write_bytes(b"y")
        with self.assertRaises(FileExistsError):
            worker._clonefile(self.root / "present", self.root / "taken")

    def test_entry_trimmed_while_a_prepare_clones_it_is_a_miss_and_restages(self):
        trim = self._use_preview_layout()
        built = self._built([self._seg("one", index=0), self._seg("two", index=1)])
        self._run(built, "key")
        entry = self.cache_root / "key"
        real = worker._clonefile
        trimmed = []

        def clone_after_trim(source, destination):
            if not trimmed:
                trimmed.append(_evict(trim, entry))  # rename aside, then delete: the reader loses the folder
            return real(source, destination)

        self.encodes.clear()
        with patch.object(worker, "_clonefile", clone_after_trim):
            stage, reused, stats = self._run(built, "key")
        self.assertEqual(trimmed, [True])
        self.assertFalse(reused, "a lost entry is staged again, not reported as a cache hit")
        self.assertEqual(stats, {"hits": 2, "misses": 0}, "the per-clip products survived, so no clip is re-encoded")
        self.assertEqual(self.encodes, [])
        self._assert_independent_copy(entry, stage)
        self.assertEqual(len(list(stage.parent.glob(f"{stage.name}*"))), 1)

    def test_entry_trimmed_before_its_marker_is_read_is_a_miss(self):
        trim = self._use_preview_layout()
        built = self._built([self._seg("one")])
        self._run(built, "key")
        entry = self.cache_root / "key"
        real = worker._cache_tree_safe
        calls = []

        def safe_then_gone(root):
            real(root)
            if not calls and Path(root).resolve() == entry.resolve():
                calls.append(_evict(trim, entry))

        with patch.object(worker, "_cache_tree_safe", safe_then_gone):
            stage, reused, stats = self._run(built, "key")
        self.assertEqual(calls, [True])
        self.assertFalse(reused)
        self.assertEqual(stats["misses"], 0)
        self.assertTrue((stage / "seg0.mp4").is_file())

    def test_product_disappearing_during_initial_lstat_is_a_miss(self):
        trim = self._use_preview_layout()
        built = self._built([self._seg("clip")])
        self._run(built, "first")
        (product,) = (self.cache_root / "products").iterdir()
        product = product.resolve()
        real = Path.lstat
        trimmed = []

        def lstat_after_trim(path, *args, **kwargs):
            if path == product and not trimmed:
                trimmed.append(False)
                trimmed[0] = _evict(trim, product)
            return real(path, *args, **kwargs)

        self.encodes.clear()
        with patch.object(Path, "lstat", lstat_after_trim):
            stage, reused, stats = self._run(built, "second")
        self.assertEqual(trimmed, [True])
        self.assertFalse(reused)
        self.assertEqual(stats, {"hits": 0, "misses": 1})
        self.assertEqual([entry[0] for entry in self.encodes], ["clip"])
        self.assertIn(b"clip|", (stage / "seg0.mp4").read_bytes())
        self.assertTrue((product / "cache.json").is_file())

    def test_product_trimmed_at_initial_metadata_check_is_a_miss_and_regenerates(self):
        trim = self._use_preview_layout()
        built = self._built([self._seg("clip")])
        self._run(built, "first")
        (product,) = (self.cache_root / "products").iterdir()
        product = product.resolve()
        real = Path.stat
        checks = []
        trimmed = []

        def stat_then_trim(path, *args, **kwargs):
            info = real(path, *args, **kwargs)
            if path == product and not trimmed:
                checks.append(kwargs.get("follow_symlinks", True))
                # The old validator's second lookup was exists(): it could return True just as the
                # entry vanished, making its later is_dir() incorrectly report an unsafe entry.
                # The single-lstat validator reaches its tree safety check instead; disappearing
                # there must also regenerate rather than treating the missing directory as unsafe.
                if kwargs.get("follow_symlinks", True) or len(checks) == 2:
                    trimmed.append(False)  # Do not recurse if eviction inspects the same path.
                    trimmed[0] = _evict(trim, product)
            return info

        self.encodes.clear()
        with patch.object(Path, "stat", stat_then_trim):
            stage, reused, stats = self._run(built, "second")
        self.assertEqual(trimmed, [True])
        self.assertFalse(reused)
        self.assertEqual(stats, {"hits": 0, "misses": 1})
        self.assertEqual([entry[0] for entry in self.encodes], ["clip"])
        self.assertIn(b"clip|", (stage / "seg0.mp4").read_bytes())
        self.assertTrue((product / "cache.json").is_file())

    def test_product_trimmed_while_it_is_restored_is_a_miss_and_encodes_again(self):
        trim = self._use_preview_layout()
        built = self._built([self._seg("clip")])
        self._run(built, "first")
        (product,) = [path for path in (self.cache_root / "products").iterdir() if path.is_dir()]
        real = worker._restore_product
        trimmed = []

        def restore_after_trim(entry, roles, marker):
            if not trimmed:
                trimmed.append(_evict(trim, entry))
            return real(entry, roles, marker)

        self.encodes.clear()
        with patch.object(worker, "_restore_product", restore_after_trim):
            stage, reused, stats = self._run(built, "second")
        self.assertEqual(trimmed, [True])
        self.assertEqual(stats, {"hits": 0, "misses": 1})
        self.assertEqual([entry[0] for entry in self.encodes], ["clip"])
        self.assertIn(b"clip|", (stage / "seg0.mp4").read_bytes())
        self.assertTrue((product / "cache.json").is_file(), "the re-encoded product is published again")

    def _exact_fixture(self, final_bytes: bytes):
        import os
        browser = self.root / "browser"
        browser.write_bytes(b"#!/bin/sh\n")
        project = types.SimpleNamespace(cfg=lambda *_args: None, root=self.root / "project")
        revision = self.root / "revision"
        stage = revision / "public"
        stage.mkdir(parents=True)
        built = {"props": {"width": 1080}, "music": None, "total": 60, "fps": 30}
        seen = {}

        def run_remotion(project, props, static, intermediate, log):
            seen["props"] = Path(props).is_file()
            Path(intermediate).write_bytes(b"remotion intermediate")

        def final_encode(project, intermediate, final, boost):
            seen["intermediate_present"] = Path(intermediate).is_file()
            Path(final).write_bytes(final_bytes)

        self.render.run_remotion = run_remotion
        self.render.final_encode = final_encode
        environment = patch.dict(os.environ, {"REELS_BROWSER": str(browser)})
        environment.start()
        self.addCleanup(environment.stop)
        return project, built, stage, revision, seen

    def test_exact_mode_drops_the_intermediate_movie_once_exact_mp4_exists(self):
        project, built, stage, revision, seen = self._exact_fixture(b"final movie")
        result = worker._run_exact(project, built, stage, revision)
        self.assertTrue(seen["intermediate_present"], "final_encode still reads render.mp4")
        self.assertFalse((revision / "render.mp4").exists())
        self.assertEqual((revision / "exact.mp4").read_bytes(), b"final movie")
        self.assertEqual(result, {"exactPath": str(revision / "exact.mp4"), "attachedAudioPreviewPath": None,
                                  "movie": str(revision / "exact.mp4")})

    def test_exact_mode_keeps_the_intermediate_when_the_final_encode_left_nothing_usable(self):
        project, built, stage, revision, _ = self._exact_fixture(b"")
        worker._run_exact(project, built, stage, revision)
        self.assertTrue((revision / "render.mp4").is_file())

    def _planted_links(self, folder: Path):
        """A stand-in original plus every kind of link a worker could leave in `folder`."""
        import os
        import stat
        originals = self.root / "stand-in-originals"
        originals.mkdir(exist_ok=True)
        original = originals / "clip.mp4"
        original.write_bytes(b"ORIGINAL-FOOTAGE")
        inner = originals / "inner"
        inner.mkdir()
        (inner / "more.mp4").write_bytes(b"ORIGINAL-INNER")
        inner.chmod(0o500)
        self.addCleanup(inner.chmod, 0o700)
        folder.mkdir(parents=True, exist_ok=True)
        os.symlink(original, folder / "file-link.mp4")
        os.symlink(originals, folder / "dir-link")
        os.symlink(inner, folder / "inner-link")
        os.link(original, folder / "hard.mp4")

        def unchanged():
            self.assertEqual(original.read_bytes(), b"ORIGINAL-FOOTAGE")
            self.assertEqual((inner / "more.mp4").read_bytes(), b"ORIGINAL-INNER")
            self.assertEqual(stat.S_IMODE(inner.stat().st_mode), 0o500)
            self.assertEqual(sorted(p.name for p in inner.iterdir()), ["more.mp4"])
            self.assertEqual(original.stat().st_nlink, 1)
        return original, unchanged

    def test_discarding_scratch_never_follows_or_writes_through_links(self):
        scratch = self.root / "scratch-tree"
        _original, unchanged = self._planted_links(scratch / "assets")
        import os
        os.symlink(_original, scratch / "top-link")
        os.link(_original, scratch / "top-hard")
        scratch.chmod(0o555)
        (scratch / "assets").chmod(0o555)
        worker._discard_tree(scratch)
        self.assertFalse(scratch.exists())
        unchanged()

    def test_discarding_a_symlink_unlinks_only_the_link(self):
        import os
        _original, unchanged = self._planted_links(self.root / "unused")
        link = self.root / "scratch-link"
        os.symlink(self.root / "stand-in-originals", link)
        worker._discard_tree(link)
        self.assertFalse(link.is_symlink())
        self.assertTrue(link.parent.is_dir())
        (self.root / "unused" / "hard.mp4").unlink()
        unchanged()

    def test_exact_mode_intermediate_that_is_a_link_is_never_removed_through(self):
        import os
        original, unchanged = self._planted_links(self.root / "links")
        revision = self.root / "revision"
        revision.mkdir()
        (self.root / "links" / "hard.mp4").unlink()  # leave the original with its one name
        final = revision / "exact.mp4"
        final.write_bytes(b"encoded")
        os.symlink(original, revision / "render.mp4")
        self.assertFalse(worker._drop_intermediate(revision / "render.mp4", final))
        self.assertTrue((revision / "render.mp4").is_symlink())
        (revision / "render.mp4").unlink()
        os.link(original, revision / "render.mp4")
        self.assertTrue(worker._drop_intermediate(revision / "render.mp4", final))
        self.assertFalse((revision / "render.mp4").exists())
        final.unlink()
        os.symlink(original, final)
        (revision / "render.mp4").write_bytes(b"intermediate")
        self.assertFalse(worker._drop_intermediate(revision / "render.mp4", final), "a linked exact.mp4 is no finished encode")
        self.assertTrue((revision / "render.mp4").is_file())
        unchanged()


if __name__ == "__main__":
    unittest.main()
