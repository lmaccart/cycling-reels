"""Parallel clip staging (order, errors) and the staged-clip cache (keys, hit/miss/bypass, LRU trim)."""

from __future__ import annotations

import os
import threading
import time
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from helpers import TempProject
from reels import render, stagecache
from reels.render import Seg
from reels.util import ReelsError


def make_seg(index: int, aid: str = "a1", *, cut: bool = False, **spec) -> Seg:
    base = {"in": 1.0, "out": 3.0, "speed": 1.0, "frame": "blurfill", "zoom": 1.0, "focusX": 0.5, "focusY": 0.5}
    base.update(spec)
    return Seg(index, "video", {"id": aid, "media": f"library/media/{aid}.mp4", "probe": {"width": 1080, "height": 1920}}, base, 60)


class Base(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(self)
        self.project = self.tp.project
        self.tp.asset("a1")
        self.tp.asset("a2")
        self.calls: list[str] = []
        self.lock = threading.Lock()

        def fake_video(project, sg, out, fps, *, grade=None, **video):
            with self.lock:
                self.calls.append(out.stem)
            time.sleep(0.05 * (3 - sg.index % 3))  # finish out of order
            if sg.spec.get("boom"):
                raise ReelsError("ffmpeg exploded")
            out.write_bytes(f"{sg.asset['id']}|{sg.spec['in']}|{video}|{grade}".encode())
            out.with_name(f"{out.stem}.cam.cmd").write_text("cam")
            return {"look": "house", "overcast": 0.1, "rescue": 0.2, "wb": [1, 1, 1], "gamma": 1.0, "n": sg.index} if grade and grade.get("look") != "none" else None

        patches = [patch.object(render, "stage_video", fake_video), patch.object(render, "stage_exhibits", lambda *a, **k: None),
                   patch.object(render, "stage_scale", lambda *a, **k: 1.0)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def built(self, segs: list[Seg], grade=None) -> dict:
        return {"segs": segs, "fps": 30, "sound": {"cleanNat": False, "cues": []}, "props": {"music": False}, "loop": None, "grade": grade}

    def stage(self, built: dict, *, cache: bool = False, workers=None, grade="house") -> Path:
        self.project.config["render"]["stageWorkers"] = workers
        self.project.config["render"]["stageCacheGB"] = 1
        self.project.config["render"]["grade"] = {"look": grade, "strength": 1.0, "rescue": "auto"}
        staging = self.project.root / "staging"
        render.stage(self.project, built, staging, use_cache=cache)
        return staging


class ParallelStaging(Base):
    def test_graded_order_is_plan_order(self):
        built = self.built([make_seg(i) for i in range(6)])
        self.stage(built, workers=4)
        self.assertEqual([g["clip"] for g in built["graded"]], [f"seg{i}" for i in range(6)])
        self.assertEqual([g["n"] for g in built["graded"]], list(range(6)))

    def test_cutaways_keep_their_place(self):
        a, b = make_seg(0), make_seg(1)
        a.spec["_cutaways"] = [{"seg": make_seg(0, "a2", **{"in": 5.0, "out": 6.0})}, {"seg": make_seg(0, "a2", **{"in": 7.0, "out": 8.0})}]
        built = self.built([a, b])
        self.stage(built, workers=4)
        self.assertEqual([g["clip"] for g in built["graded"]], ["seg0", "cut0-0", "cut0-1", "seg1"])

    def test_actually_concurrent(self):
        seen = {"now": 0, "max": 0}

        def slow(project, sg, out, fps, *, grade=None, **video):
            with self.lock:
                seen["now"] += 1
                seen["max"] = max(seen["max"], seen["now"])
            time.sleep(0.1)
            with self.lock:
                seen["now"] -= 1
            out.write_bytes(b"x")

        with patch.object(render, "stage_video", slow):
            self.stage(self.built([make_seg(i) for i in range(6)]), workers=3, grade="none")
        self.assertEqual(seen["max"], 3)

    def test_colour_tooling_is_settled_before_clips_run(self):
        """can_tonemap() may swap the ffmpeg binary; clips racing it would pick different colour filters."""
        order: list[str] = []
        real = render.grading.use_zscale
        with patch.object(render.grading, "use_zscale", lambda: order.append("settled") or real()):
            self.calls = order
            self.stage(self.built([make_seg(0), make_seg(1)]), workers=2, grade="none")
        self.assertEqual(order[0], "settled")

    def test_one_worker_is_sequential(self):
        self.stage(self.built([make_seg(i) for i in range(4)]), workers=1)
        self.assertEqual(self.calls, [f"seg{i}" for i in range(4)])

    def test_first_error_propagates_and_pending_is_cancelled(self):
        segs = [make_seg(i) for i in range(8)]
        segs[1].spec["boom"] = True
        with self.assertRaisesRegex(ReelsError, "exploded"):
            self.stage(self.built(segs), workers=2)
        self.assertLess(len(self.calls), 8)  # not-yet-started clips never ran
        self.assertFalse([t for t in threading.enumerate() if t.name.startswith("stage")])  # the pool was awaited

    def test_sphere_clips_are_limited(self):
        seen = {"now": 0, "max": 0}

        def slow(project, sg, out, fps, *, grade=None, **video):
            with self.lock:
                seen["now"] += 1
                seen["max"] = max(seen["max"], seen["now"])
            time.sleep(0.1)
            with self.lock:
                seen["now"] -= 1
            out.write_bytes(b"x")

        segs = [make_seg(i, sphere={"view": "road"}) for i in range(4)]
        with patch.object(render, "stage_video", slow):
            self.stage(self.built(segs), workers=4, grade="none")
        self.assertEqual(seen["max"], 1)

    def test_worker_count(self):
        self.assertEqual(stagecache.worker_count(3), 3)
        for bad in (None, 0, -1, True, "x"):
            self.assertEqual(stagecache.worker_count(bad), stagecache.default_workers())
        self.assertTrue(1 <= stagecache.default_workers() <= 4)

    def test_run_jobs_order(self):
        jobs = [lambda i=i: (time.sleep(0.03 * (5 - i)), i)[1] for i in range(5)]
        self.assertEqual(stagecache.run_jobs(jobs, 5), list(range(5)))


class CacheKeys(Base):
    def key(self, seg: Seg, grade=None, **opts) -> str:
        return stagecache.clip_key(self.project, seg, "video", 30, opts, grade)

    def test_same_inputs_same_key(self):
        self.assertEqual(self.key(make_seg(0)), self.key(make_seg(0)))

    def test_each_relevant_field_changes_the_key(self):
        base = self.key(make_seg(0), {"look": "house"}, scale=1.0, clean=False, edges=(True, True), audio=True)
        changes = {
            "in": make_seg(0, **{"in": 1.1}), "out": make_seg(0, out=3.5), "speed": make_seg(0, speed=0.5),
            "frame": make_seg(0, frame="crop"), "zoom": make_seg(0, zoom=1.2), "focusX": make_seg(0, focusX=0.4),
            "sphere": make_seg(0, sphere={"view": "road"}), "punch": make_seg(0, punch={"scale": 1.2}),
            "bleep": make_seg(0, bleep={"sound": "tone"}), "_bleeps": make_seg(0, _bleeps=[(1.0, 1.2)]),
            "grade": make_seg(0, grade={"look": "moody"}), "nat": make_seg(0, nat=True), "clean": make_seg(0, clean=True),
        }
        for what, seg in changes.items():
            self.assertNotEqual(self.key(seg, {"look": "house"}, scale=1.0, clean=False, edges=(True, True), audio=True), base, what)
        other = [
            self.key(make_seg(0), {"look": "moody"}, scale=1.0, clean=False, edges=(True, True), audio=True),
            self.key(make_seg(0), {"look": "house"}, scale=1.4, clean=False, edges=(True, True), audio=True),
            self.key(make_seg(0), {"look": "house"}, scale=1.0, clean=True, edges=(True, True), audio=True),
            self.key(make_seg(0), {"look": "house"}, scale=1.0, clean=False, edges=(False, True), audio=True),
            self.key(make_seg(0), {"look": "house"}, scale=1.0, clean=False, edges=(True, True), audio=False),
            stagecache.clip_key(self.project, make_seg(0), "video", 24, dict(scale=1.0, clean=False, edges=(True, True), audio=True), {"look": "house"}),
            stagecache.clip_key(self.project, make_seg(0), "photo", 30, dict(scale=1.0, clean=False, edges=(True, True), audio=True), {"look": "house"}),
        ]
        for k in other:
            self.assertNotEqual(k, base)
        self.assertEqual(len({base, *other}), len(other) + 1)

    def test_frames_and_asset_and_probe_change_the_key(self):
        base = self.key(make_seg(0))
        seg = make_seg(0)
        seg.frames = 61
        self.assertNotEqual(self.key(seg), base)
        self.assertNotEqual(self.key(make_seg(0, "a2")), base)
        seg = make_seg(0)
        seg.asset["probe"] = {"width": 1080, "height": 1080}
        self.assertNotEqual(self.key(seg), base)

    def test_size_config_changes_the_key(self):
        base = self.key(make_seg(0))
        self.project.config["render"]["width"] = 720
        self.assertNotEqual(self.key(make_seg(0)), base)

    def test_source_file_identity_changes_the_key(self):
        base = self.key(make_seg(0))
        media = self.project.abs("library/media/a1.mp4")
        media.write_bytes(b"different bytes entirely")
        self.assertNotEqual(self.key(make_seg(0)), base)
        os.utime(media, (1, 1))
        self.assertNotEqual(self.key(make_seg(0)), base)

    def test_text_only_fields_do_not_change_the_key(self):
        self.assertEqual(self.key(make_seg(0)), self.key(make_seg(0, captions="transcript", text="hello", _cutaways=[1])))

    def test_code_change_changes_the_key(self):
        base = self.key(make_seg(0))
        stagecache._code_revision.cache_clear()
        with patch.object(stagecache, "CODE_MODULES", stagecache.CODE_MODULES + ("__nope__",)), \
                patch.object(stagecache, "KEY_VERSION", stagecache.KEY_VERSION + 1):
            self.assertNotEqual(self.key(make_seg(0)), base)
        stagecache._code_revision.cache_clear()


class CacheBehaviour(Base):
    def test_hit_skips_staging_and_reproduces_files_and_grade(self):
        built = self.built([make_seg(0), make_seg(1, "a2")])
        staging = self.stage(built, cache=True, workers=2)
        first = {p.name: p.read_bytes() for p in staging.iterdir()}
        graded = deepcopy(built["graded"])
        self.assertEqual(len(self.calls), 2)

        again = self.built([make_seg(0), make_seg(1, "a2")])
        staging = self.stage(again, cache=True, workers=2)
        self.assertEqual(len(self.calls), 2)  # nothing re-staged
        self.assertEqual({p.name: p.read_bytes() for p in staging.iterdir()}, first)
        self.assertEqual(again["graded"], graded)

    def test_only_the_changed_clip_restages(self):
        self.stage(self.built([make_seg(i) for i in range(3)]), cache=True)
        self.calls.clear()
        self.stage(self.built([make_seg(0), make_seg(1, **{"in": 2.0}), make_seg(2)]), cache=True)
        self.assertEqual(self.calls, ["seg1"])

    def test_bypass(self):
        self.stage(self.built([make_seg(0)]), cache=True)
        self.calls.clear()
        self.stage(self.built([make_seg(0)]), cache=False)
        self.assertEqual(self.calls, ["seg0"])
        self.project.config["render"]["stageCache"] = False
        self.assertIsNone(stagecache.StageCache.for_project(self.project))
        self.project.config["render"]["stageCache"] = True
        self.project.config["render"]["stageCacheGB"] = 0
        self.assertIsNone(stagecache.StageCache.for_project(self.project))

    def test_cache_lives_outside_library(self):
        self.stage(self.built([make_seg(0)]), cache=True)
        root = self.project.root / ".cache" / "render" / "staged"
        self.assertTrue(any(root.iterdir()))

    def test_corrupt_entry_is_a_miss(self):
        self.stage(self.built([make_seg(0)]), cache=True)
        for meta in (self.project.root / ".cache" / "render" / "staged").glob("*/meta.json"):
            meta.write_text("{not json")
        self.calls.clear()
        self.stage(self.built([make_seg(0)]), cache=True)
        self.assertEqual(self.calls, ["seg0"])

    def test_missing_file_in_entry_is_a_miss(self):
        self.stage(self.built([make_seg(0)]), cache=True)
        for f in (self.project.root / ".cache" / "render" / "staged").glob("*/files/.mp4"):
            f.unlink()
        self.calls.clear()
        self.stage(self.built([make_seg(0)]), cache=True)
        self.assertEqual(self.calls, ["seg0"])

    def test_failed_clip_is_not_cached(self):
        seg = make_seg(0, boom=True)
        with self.assertRaises(ReelsError):
            self.stage(self.built([seg]), cache=True, workers=1)
        root = self.project.root / ".cache" / "render" / "staged"
        self.assertFalse(root.exists() and any(root.iterdir()))


class LruTrim(unittest.TestCase):
    def setUp(self):
        self.tp = TempProject(self)
        self.cache = stagecache.StageCache(self.tp.root / "c", 2500)
        self.src = self.tp.root / "src"
        self.src.mkdir()

    def put(self, key: str, size: int, age: float) -> Path:
        (self.src / "clip.mp4").write_bytes(b"x" * size)
        self.cache.store(key, "clip", self.src, None)
        meta = self.cache.root / key / "meta.json"
        t = time.time() - age
        os.utime(meta, (t, t))
        return meta

    def test_oldest_entries_go_first(self):
        # budget 2500: three entries of ~1000 bytes (plus meta) don't fit
        self.put("a", 1000, 300)
        self.put("b", 1000, 200)
        self.put("c", 1000, 100)
        self.assertEqual(sorted(p.name for p in self.cache.root.iterdir()), ["b", "c"])

    def test_fetch_refreshes_recency(self):
        self.put("a", 1000, 300)
        self.put("b", 1000, 200)
        out = self.tp.root / "out"
        out.mkdir(exist_ok=True)
        self.assertEqual(self.cache.fetch("a", "clip", out), {"grade": None})  # a is now the newest
        self.put("c", 1000, 0)
        self.assertEqual(sorted(p.name for p in self.cache.root.iterdir()), ["a", "c"])

    def test_new_entry_survives_even_when_too_big(self):
        self.put("big", 5000, 0)
        self.assertEqual([p.name for p in self.cache.root.iterdir()], ["big"])

    def test_stale_tmp_dirs_removed(self):
        self.put("a", 100, 0)
        tmp = self.cache.root / ".tmp-old"
        tmp.mkdir()
        os.utime(tmp, (1, 1))
        self.cache.trim()
        self.assertFalse(tmp.exists())

    def test_config_budget(self):
        self.tp.project.config["render"]["stageCacheGB"] = 0.5
        c = stagecache.StageCache.for_project(self.tp.project)
        self.assertEqual(c.max_bytes, 500_000_000)
        self.assertEqual(c.root.resolve(), (self.tp.root / ".cache" / "render" / "staged").resolve())


if __name__ == "__main__":
    unittest.main()
