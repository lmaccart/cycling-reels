"""Focused warm-worker contracts. No production integration is required."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from reels.editor.compile_runtime import InputMirror, private_path
from reels.editor.compile_worker import boundary_key
from reels.editor.compiled_cache import cache_record, read_cache


class InputMirrorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="editor-mirror-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        self.private = self.root / "worker/private"
        self.source.mkdir()
        self.asset = self.source / "library/assets/a.json"
        self.asset.parent.mkdir(parents=True)
        self.asset.write_text('{"id":"a"}')
        self.mirror = InputMirror(self.source, self.private)

    def test_unchanged_inputs_are_not_copied_again(self):
        self.mirror.sync()
        target = self.private / "library/assets/a.json"
        before = target.stat().st_mtime_ns
        self.mirror.sync()
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_renderer_mutations_do_not_survive_the_next_request(self):
        self.mirror.sync()
        target = self.private / "library/assets/a.json"
        target.write_text('{"id":"corrupted"}')
        self.mirror.sync()
        self.assertEqual(target.read_bytes(), self.asset.read_bytes())

    def test_overrides_and_deletions_are_restored(self):
        config = self.source / "config.json"
        config.write_text('{"brand":{"handle":"original"}}')
        self.mirror.sync()
        self.mirror.apply({"config.json": {"brand": {"handle": "candidate"}}})
        self.assertEqual(json.loads((self.private / "config.json").read_text())["brand"]["handle"], "candidate")
        self.mirror.sync()
        self.assertEqual((self.private / "config.json").read_bytes(), config.read_bytes())
        self.mirror.apply({"__editor_deleted_files__": ["config.json"]})
        self.assertFalse((self.private / "config.json").exists())
        self.mirror.sync()
        self.assertEqual((self.private / "config.json").read_bytes(), config.read_bytes())

    def test_new_and_deleted_inputs_sync_without_changing_source(self):
        self.mirror.sync()
        added = self.source / "library/assets/b.json"
        added.write_text('{"id":"b"}')
        self.asset.unlink()
        self.mirror.sync()
        self.assertFalse((self.private / "library/assets/a.json").exists())
        self.assertEqual((self.private / "library/assets/b.json").read_bytes(), added.read_bytes())
        self.assertEqual(added.read_text(), '{"id":"b"}')

    def test_request_only_transcript_is_removed_on_next_sync(self):
        self.mirror.sync()
        relative = "library/transcripts/candidate.json"
        self.mirror.apply({relative: {"words": []}})
        self.assertTrue((self.private / relative).exists())
        self.mirror.sync()
        self.assertFalse((self.private / relative).exists())

    def test_source_symlinks_are_rejected(self):
        (self.asset.parent / "escape.json").symlink_to(self.asset)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.mirror.sync()

    def test_override_cannot_escape_or_write_media(self):
        self.mirror.sync()
        for relative in ("../outside.json", "/absolute.json", "library/media/original.mp4",
                         "library/transcripts/../../escape.json"):
            with self.subTest(relative=relative), self.assertRaises(ValueError):
                self.mirror.apply({relative: {}})
        self.assertFalse((self.root / "outside.json").exists())

    def test_private_paths_reject_symlink_ancestors(self):
        self.private.mkdir(parents=True, exist_ok=True)
        (self.private / "escape").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            private_path(self.private, "escape/changed.json")


class BoundaryKeyTests(unittest.TestCase):
    def test_scratch_is_normalized_but_roots_profiles_and_revisions_are_not(self):
        source, first, second = Path("/source"), Path("/scratch/one"), Path("/scratch/two")

        def key(scratch=first, revision="renderer-A", root=source, suffix=""):
            return boundary_key(revision, [root, scratch], f"write {scratch}{suffix}", scratch,
                                data_root=source, media_root=Path("/media"))

        self.assertEqual(key(), key(scratch=second))
        self.assertNotEqual(key(), key(revision="renderer-B"))
        self.assertNotEqual(key(), key(root=Path("/other")))
        self.assertNotEqual(key(), key(suffix=" broader grant"))


class CompiledCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="editor-compiled-cache-")
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "COMPILED.json"
        self.key = {"revisions": {"document": "doc", "library": "lib", "renderer": "renderer"},
                    "boundary": "sandbox", "planHash": "raw-content", "runner": "trusted"}
        self.compiled = {"fps": 30, "total": 0, "segs": [], "props": {}, "sound": {}, "warnings": []}
        self.record = cache_record(self.key, self.compiled)
        self.path.write_text(json.dumps(self.record))

    def test_exact_match_only(self):
        self.assertEqual(read_cache(self.path, self.key), self.compiled)
        for field in ("boundary", "planHash", "runner"):
            with self.subTest(field=field):
                self.assertIsNone(read_cache(self.path, {**self.key, field: "changed"}))
        for field in ("document", "library", "renderer"):
            key = {**self.key, "revisions": {**self.key["revisions"], field: "changed"}}
            with self.subTest(revision=field):
                self.assertIsNone(read_cache(self.path, key))

    def test_malformed_or_tampered_result_is_ignored(self):
        self.record["compiled"]["total"] = 30
        self.path.write_text(json.dumps(self.record))
        self.assertIsNone(read_cache(self.path, self.key))
        self.path.write_text('{"version":')
        self.assertIsNone(read_cache(self.path, self.key))
        invalid = {**self.compiled, "fps": -30}
        self.path.write_text(json.dumps(cache_record(self.key, invalid)))
        self.assertIsNone(read_cache(self.path, self.key))

    def test_symlink_and_hardlink_caches_are_ignored(self):
        alias = self.path.with_name("alias.json")
        alias.symlink_to(self.path)
        self.assertIsNone(read_cache(alias, self.key))
        alias.unlink()
        alias.hardlink_to(self.path)
        self.assertIsNone(read_cache(self.path, self.key))
