"""Editor preview cache: bounded eviction of regenerable revisions and staged media, never of proxies."""
from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from reels.editor.preview_cache import (
    DEFAULT_BUDGET_GB, DOCUMENT_MARKER, EVICT_PREFIX, PreviewCache, budget_bytes, renderer_namespace, tree_size,
)

NOW = 1_800_000_000.0
CURRENT = "renderer-current"
OLD = "renderer-old"
KB = 1000


def revision_name(seed: int) -> str:
    return f"{seed:020x}-{seed:08x}"


class CacheLayout(unittest.TestCase):
    """A temp workspace with <root>/preview laid out like the editor's, tiny fixture files, fake clock."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.root = Path(self.tmp.name) / "workspace"
        self.preview = self.root / "preview"
        for folder in ("revisions", "cache/staged-media", "cache/bundles", "documents", "sources", "spheres", "proxy-reservations"):
            (self.preview / folder).mkdir(parents=True)
        self.protected: list = []
        self.in_flight: list[Path] = []
        self.renderers = [CURRENT]

    def _cleanup(self):
        for current, dirs, _ in os.walk(self.tmp.name):
            for name in dirs:
                os.chmod(os.path.join(current, name), 0o700)
        self.tmp.cleanup()

    def cache(self, budget: int, **kwargs) -> PreviewCache:
        return PreviewCache(
            self.root, budget=budget, clock=lambda: NOW,
            protected=lambda: list(self.protected), in_flight=lambda: list(self.in_flight),
            current_renderers=lambda: list(self.renderers), **kwargs,
        )

    def revision(self, seed: int, *, age: float = 3600, size: int = KB, document: str | None = None,
                 exact: bool = False, intermediate: bool = False) -> Path:
        path = self.preview / "revisions" / revision_name(seed)
        (path / "public").mkdir(parents=True)
        (path / "public/seg0.mp4").write_bytes(b"v" * size)
        (path / "public/props.json").write_text("{}")
        if exact:
            (path / "exact.mp4").write_bytes(b"e" * 10)
        if intermediate:
            (path / "render.mp4").write_bytes(b"r" * 10)
        if document:
            (path / DOCUMENT_MARKER).write_text(json.dumps({"documentId": document}))
        self.age(path, age)
        return path

    def age(self, path: Path, age: float) -> None:
        for current, dirs, files in os.walk(path, topdown=False):
            for name in (*files, *dirs):
                os.utime(os.path.join(current, name), (NOW - age, NOW - age))
        os.utime(path, (NOW - age, NOW - age))

    def staged(self, namespace: str, key: str, *, age: float = 3600, size: int = KB, bundle: bool = False) -> Path:
        kind = "bundles" if bundle else "staged-media"
        path = self.preview / "cache" / kind / namespace / key
        path.mkdir(parents=True)
        (path / "seg0.mp4").write_bytes(b"s" * size)
        (path / "cache.json").write_text(json.dumps({"key": key}))
        self.readonly(path)
        self.age(path, age)
        return path

    def product(self, namespace: str, key: str, *, age: float = 3600, size: int = KB) -> Path:
        path = self.preview / "cache/staged-media" / namespace / "products" / key
        path.mkdir(parents=True)
        (path / "main").write_bytes(b"p" * size)
        self.readonly(path)
        self.age(path, age)
        return path

    @staticmethod
    def readonly(path: Path) -> None:
        for child in path.iterdir():
            child.chmod(0o444)
        path.chmod(0o555)

    def manifest(self, **fields) -> dict:
        return fields

    def write_manifest(self, name: str, manifest: dict) -> None:
        folder = self.preview / "documents"
        (folder / name if name != "last-ready.json" else self.preview / name).write_text(json.dumps(manifest))


class BudgetTests(unittest.TestCase):
    def test_default_and_configured_budget(self):
        self.assertEqual(budget_bytes({}), int(DEFAULT_BUDGET_GB * 1e9))
        self.assertEqual(budget_bytes({"editor": {"previewCacheGB": 2.5}}), 2_500_000_000)
        for unusable in (None, "8", -1, True, [8]):
            self.assertEqual(budget_bytes({"editor": {"previewCacheGB": unusable}}), 8_000_000_000, unusable)


class KeepSetTests(CacheLayout):
    def test_under_budget_touches_nothing(self):
        stale = self.revision(1, age=86400)
        report = self.cache(10 * KB * KB).run()
        self.assertEqual((report.evicted, report.skipped), ([], None))
        self.assertTrue(stale.is_dir())

    def test_manifest_referenced_directories_survive_any_budget(self):
        last = self.revision(1, age=9000)
        document = self.revision(2, age=9000)
        live = self.revision(3, age=9000)
        registered = self.revision(4, age=9000)
        unreferenced = self.revision(5, age=9000)
        bundle = self.staged(renderer_namespace(OLD), "bundlehash", bundle=True, age=9000)
        self.write_manifest("last-ready.json", {"staticDir": str(last / "public"), "bundleDir": str(bundle)})
        self.write_manifest("doc.json", {"exactPath": str(document / "exact.mp4"), "propsPath": str(document / "public/props.json")})
        self.protected = [
            {"state": "ready", "movie": str(live / "exact.mp4"), "url": "http://127.0.0.1:1/x"},
            str(registered / "public/seg0.mp4"),
        ]
        # The one live-referenced old-renderer bundle also survives; other revisions are free to go.
        report = self.cache(0, keep_per_document=0).run()
        for kept in (last, document, live, registered, bundle):
            self.assertTrue(kept.is_dir(), kept)
        self.assertFalse(unreferenced.exists())
        self.assertEqual(sorted(p.name for p in report.evicted), [unreferenced.name])

    def test_manifest_paths_match_through_a_resolved_workspace_root(self):
        kept = self.revision(1, age=9000)
        self.revision(2, age=9000)
        resolved = Path(os.path.realpath(self.preview)) / "revisions" / kept.name / "public"
        self.write_manifest("last-ready.json", {"staticDir": str(resolved)})
        self.cache(0, keep_per_document=0).run()
        self.assertEqual([p.name for p in (self.preview / "revisions").iterdir()], [kept.name])

    def test_newest_three_revisions_of_each_document_are_kept(self):
        docs = {"a": [10, 11, 12, 13, 14], "b": [20, 21, 22, 23]}
        for document, seeds in docs.items():
            for index, seed in enumerate(seeds):
                self.revision(seed, age=9000 - 100 * index, document=document)
        report = self.cache(0).run()
        remaining = sorted(p.name for p in (self.preview / "revisions").iterdir())
        self.assertEqual(remaining, sorted(revision_name(seed) for seed in (12, 13, 14, 21, 22, 23)))
        # Oldest go first, across documents.
        self.assertEqual([p.name for p in report.evicted], [revision_name(seed) for seed in (10, 20, 11)])

    def test_unmarked_revisions_take_their_document_from_the_manifest_that_names_them(self):
        legacy = [self.revision(seed, age=9000 - seed) for seed in (30, 31, 32, 33, 34)]
        self.write_manifest("doc.json", {"documentId": "doc-1", "staticDir": str(legacy[0] / "public")})
        self.protected = [{"documentId": "doc-1", "staticDir": str(legacy[1] / "public")}]
        self.cache(0).run()
        remaining = {p.name for p in (self.preview / "revisions").iterdir()}
        # doc-1 owns the two referenced folders; the three newest unmarked ones are kept too.
        self.assertEqual(remaining, {legacy[0].name, legacy[1].name, *(p.name for p in legacy[2:])})

    def test_grace_period_and_in_flight_jobs_are_kept(self):
        young = self.revision(1, age=599)
        old_enough = self.revision(2, age=601)
        running = self.revision(3, age=9000)
        entry = self.staged(renderer_namespace(CURRENT), "fresh", age=599)
        self.in_flight = [running]
        self.cache(0, keep_per_document=0).run()
        self.assertTrue(young.is_dir() and running.is_dir() and entry.is_dir())
        self.assertFalse(old_enough.exists())

    def test_in_flight_namespace_keeps_all_its_staged_media(self):
        busy = self.staged(renderer_namespace(OLD), "busy-key", age=9000)
        idle = self.staged("renderer-idle", "idle-key", age=9000)
        self.in_flight = [self.preview / "cache/staged-media" / renderer_namespace(OLD),
                          self.preview / "cache/bundles" / renderer_namespace(OLD)]
        self.cache(0).run()
        self.assertTrue(busy.is_dir())
        self.assertFalse(idle.exists())

    def test_staged_media_built_for_a_kept_manifest_stays_in_the_current_namespace_only(self):
        wanted = self.staged(renderer_namespace(CURRENT), "key-current", age=9000)
        unwanted = self.staged(renderer_namespace(CURRENT), "key-other", age=9000)
        old = self.staged(renderer_namespace(OLD), "key-old", age=9000)
        manifest = {"cacheKey": "key-current", "revisions": {"renderer": CURRENT}}
        old_manifest = {"cacheKey": "key-old", "revisions": {"renderer": OLD}}
        self.write_manifest("a.json", manifest)
        self.protected = [old_manifest]
        self.cache(0).run()
        self.assertTrue(wanted.is_dir())
        self.assertFalse(unwanted.exists())
        self.assertFalse(old.exists(), "a manifest does not keep an old renderer's staged media, only its folders")

    def test_current_renderer_bundles_are_never_evicted(self):
        current = self.staged(renderer_namespace(CURRENT), "bundle-a", bundle=True, age=9000)
        old = self.staged(renderer_namespace(OLD), "bundle-b", bundle=True, age=9000)
        self.cache(0).run()
        self.assertTrue(current.is_dir())
        self.assertFalse(old.exists())


class EvictionOrderTests(CacheLayout):
    def test_old_renderer_then_unreferenced_revisions_oldest_first_then_lru_staged_and_products(self):
        current, old = renderer_namespace(CURRENT), renderer_namespace(OLD)
        entries = {
            "old-staged": self.staged(old, "k1", age=2000),
            "old-product": self.product(old, "p1", age=1000),
            "rev-oldest": self.revision(1, age=9000),
            "rev-newer": self.revision(2, age=5000),
            "staged-lru-old": self.staged(current, "k2", age=7000),
            "product-lru-mid": self.product(current, "p2", age=4000),
            "staged-lru-new": self.staged(current, "k3", age=3000),
        }
        full = self.cache(10 ** 9).run()
        self.assertEqual(full.evicted, [])
        report = self.cache(0, keep_per_document=0).run()
        # Tier 1 old renderer (oldest use first), tier 2 revisions oldest first, tier 3 current staged/products LRU.
        order = ("old-staged", "old-product", "rev-oldest", "rev-newer", "staged-lru-old", "product-lru-mid", "staged-lru-new")
        self.assertEqual(report.evicted, [entries[name] for name in order])

    def test_stops_as_soon_as_the_budget_is_met(self):
        old = renderer_namespace(OLD)
        first = self.staged(old, "k1", age=5000, size=KB)
        second = self.staged(old, "k2", age=4000, size=KB)
        revision = self.revision(1, age=9000, size=KB)
        total = tree_size(self.preview / "revisions") + tree_size(self.preview / "cache")
        first_size = tree_size(first)
        report = self.cache(total - first_size, keep_per_document=0).run()
        self.assertEqual(report.evicted, [first])
        self.assertTrue(second.is_dir() and revision.is_dir())
        self.assertEqual((report.before, report.after), (total, total - first_size))

    def test_keep_set_wins_over_the_budget(self):
        live = self.revision(1, age=9000, size=10 * KB)
        self.write_manifest("last-ready.json", {"staticDir": str(live / "public")})
        report = self.cache(1).run()
        self.assertEqual(report.evicted, [])
        self.assertTrue(live.is_dir())
        self.assertGreater(report.after, report.budget)

    def test_read_only_cache_trees_are_deleted_and_nothing_is_left_parked(self):
        entry = self.staged(renderer_namespace(OLD), "locked", age=9000)
        self.assertEqual(stat.S_IMODE(entry.stat().st_mode), 0o555)
        self.cache(0).run()
        self.assertFalse(entry.exists())
        parked = [p for parent in (entry.parent,) for p in parent.iterdir()]
        self.assertEqual(parked, [])

    def test_interrupted_eviction_is_finished_by_the_next_pass(self):
        parked = self.preview / "revisions" / f"{EVICT_PREFIX}deadbeef"
        (parked / "public").mkdir(parents=True)
        (parked / "public/seg0.mp4").write_bytes(b"x" * 10)
        product_parked = self.preview / "cache/staged-media" / renderer_namespace(OLD) / "products" / f"{EVICT_PREFIX}cafe"
        product_parked.mkdir(parents=True)
        product_parked.chmod(0o555)
        self.cache(10 ** 9).run()
        self.assertFalse(parked.exists())
        self.assertFalse(product_parked.exists())


class SafetyTests(CacheLayout):
    def test_proxies_reservations_and_locks_are_never_touched(self):
        source = self.preview / "sources/abc"
        sphere = self.preview / "spheres/def"
        for folder in (source, sphere):
            folder.mkdir(parents=True)
            (folder / "proxy.mp4").write_bytes(b"p" * 5 * KB)
            (folder / ".proxy.lock").write_text("")
            self.age(folder, 99999)
        lease = self.preview / "proxy-reservations/x.reserve"
        lease.write_text("12345")
        self.age(self.preview / "proxy-reservations", 99999)
        self.revision(1, age=9000)
        before = sorted(str(p.relative_to(self.preview)) for folder in ("sources", "spheres", "proxy-reservations")
                        for p in (self.preview / folder).rglob("*"))
        report = self.cache(0, keep_per_document=0).run()
        after = sorted(str(p.relative_to(self.preview)) for folder in ("sources", "spheres", "proxy-reservations")
                       for p in (self.preview / folder).rglob("*"))
        self.assertEqual(before, after)
        self.assertEqual((source / "proxy.mp4").read_bytes(), b"p" * 5 * KB)
        self.assertEqual([p.parent.name for p in report.evicted], ["revisions"])
        self.assertTrue((self.preview / ".evict.lock").is_file(), "the pass lock file is never unlinked")

    def test_other_workspaces_and_symlinked_entries_are_never_followed(self):
        outside = Path(self.tmp.name) / "library/media"
        outside.mkdir(parents=True)
        original = outside / "ride.mp4"
        original.write_bytes(b"original footage")
        original.chmod(0o444)
        link = self.preview / "revisions" / revision_name(7)
        link.symlink_to(outside, target_is_directory=True)
        cache_link = self.preview / "cache/staged-media" / renderer_namespace(OLD) / "linked"
        cache_link.parent.mkdir(parents=True)
        cache_link.symlink_to(outside, target_is_directory=True)
        inner = self.revision(8, age=9000)
        (inner / "public/escape").symlink_to(outside, target_is_directory=True)
        other = Path(self.tmp.name) / "other-workspace/preview/revisions"
        other.mkdir(parents=True)
        foreign = other / revision_name(9)
        foreign.mkdir()
        (foreign / "seg.mp4").write_bytes(b"x")
        self.age(foreign, 9000)
        self.protected = [str(foreign / "seg.mp4")]
        self.cache(0, keep_per_document=0).run()
        self.assertEqual(original.read_bytes(), b"original footage")
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o444, "chmod never follows a link into the originals")
        self.assertTrue(link.is_symlink() and cache_link.is_symlink())
        self.assertFalse(inner.exists(), "a symlink inside an evicted folder is removed, not followed")
        self.assertTrue(foreign.is_dir(), "another workspace root is never touched")

    def test_symlinked_preview_folders_stop_the_pass(self):
        real = Path(self.tmp.name) / "elsewhere"
        (real / "revisions").mkdir(parents=True)
        victim = real / "revisions" / revision_name(1)
        victim.mkdir()
        (victim / "f").write_bytes(b"x" * 100)
        self.age(victim, 9000)
        (self.preview / "revisions").rmdir()
        (self.preview / "revisions").symlink_to(real / "revisions", target_is_directory=True)
        report = self.cache(0, keep_per_document=0).run()
        self.assertIsNotNone(report.skipped)
        self.assertTrue(victim.is_dir())

    def test_a_second_pass_waits_out_the_first(self):
        stale = self.revision(1, age=9000)
        fd = os.open(self.preview / ".evict.lock", os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        report = self.cache(0, keep_per_document=0).run()
        self.assertEqual(report.skipped, "another pass is running")
        self.assertTrue(stale.is_dir())

    def test_renderer_candidate_snapshots_are_not_preview_revisions(self):
        candidate = self.preview / "revisions" / "renderer-0123456789abcdef0123-deadbeef"
        (candidate / "public").mkdir(parents=True)
        (candidate / "public/x").write_bytes(b"c" * 10)
        self.age(candidate, 99999)
        report = self.cache(0, keep_per_document=0).run()
        self.assertEqual(report.evicted, [])
        self.assertTrue(candidate.is_dir())

    def test_scratch_of_killed_workers_goes_past_the_grace_period_even_under_budget(self):
        namespace = self.preview / "cache/staged-media" / renderer_namespace(CURRENT)
        abandoned = namespace / ".stage-abc"
        young = namespace / ".stage-def"
        half_product = namespace / "products" / ".keyhash.0123.tmp"
        for folder in (abandoned, young, half_product):
            folder.mkdir(parents=True)
            (folder / "seg.mp4").write_bytes(b"s" * 100)
        self.age(abandoned, 99999)
        self.age(half_product, 99999)
        self.age(young, 30)
        report = self.cache(10 ** 9).run()
        self.assertEqual(sorted(p.name for p in report.evicted), sorted([abandoned.name, half_product.name]))
        self.assertTrue(young.is_dir(), "a stage younger than the grace period may still be written")

    def test_scratch_in_a_running_jobs_namespace_is_kept(self):
        namespace = self.preview / "cache/staged-media" / renderer_namespace(CURRENT)
        running = namespace / ".stage-abc"
        running.mkdir(parents=True)
        (running / "seg.mp4").write_bytes(b"s" * 100)
        self.age(running, 99999)
        self.in_flight = [namespace]
        self.cache(0).run()
        self.assertTrue(running.is_dir(), "a long encode inside a busy namespace outlives the grace period")


class IntermediateTests(CacheLayout):
    def test_render_mp4_is_dropped_only_once_exact_exists_and_nothing_references_it(self):
        superseded = self.revision(1, age=9000, exact=True, intermediate=True)
        referenced = self.revision(2, age=9000, exact=True, intermediate=True)
        no_final = self.revision(3, age=9000, intermediate=True)
        young = self.revision(4, age=60, exact=True, intermediate=True)
        running = self.revision(5, age=9000, exact=True, intermediate=True)
        manifested = self.revision(6, age=9000, exact=True, intermediate=True)
        self.protected = [str(referenced / "render.mp4")]
        self.write_manifest("doc.json", {"exactPath": str(manifested / "exact.mp4")})
        self.in_flight = [running]
        report = self.cache(10 ** 9).run()
        self.assertFalse((superseded / "render.mp4").exists())
        self.assertFalse((manifested / "render.mp4").exists(), "a manifest that names only exact.mp4 does not pin render.mp4")
        self.assertTrue((superseded / "exact.mp4").is_file() and (manifested / "exact.mp4").is_file())
        for kept in (referenced, no_final, young, running):
            self.assertTrue((kept / "render.mp4").is_file(), kept)
        self.assertEqual(sorted(p.parent.name for p in report.dropped_intermediates),
                         sorted([superseded.name, manifested.name]))


if __name__ == "__main__":
    unittest.main()
