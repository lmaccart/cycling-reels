from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import TempProject
from reels.editor import Workspace, init_workspace
from reels.editor import refresh as refresh_module
from reels.editor.document import ensure_node_ids
from reels.editor.refresh import RefreshConflict, refresh_workspace
from reels.editor.transactions import DocumentStore
from reels.util import ReelsError, write_json


class EditorRefreshTests(unittest.TestCase):
    def setUp(self):
        self.fixture = TempProject(self)
        self.source = self.fixture.root.resolve()
        self.asset = self.fixture.asset("clip-a")
        self.fixture.plan("ride", [{"asset": "clip-a", "in": 0, "out": 2}])
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.code = self.base / "isolated-code"
        self.code.mkdir()
        self.old = init_workspace(self.source, self.base / "old", code_root=self.code)
        self.target = self.base / "refreshed"
        self.plan_name = "plans/ride.json"
        self.private_plan = self.old.data_root / self.plan_name

    def refresh(self, **kwargs):
        return refresh_workspace(self.source, self.old, self.target, **kwargs)

    def edit_private(self):
        plan = json.loads(self.private_plan.read_bytes())
        ensure_node_ids(plan)
        plan["segments"][0]["out"] = 1
        plan["uncommittedNote"] = "preserve this"
        write_json(self.private_plan, plan)
        return plan

    def assert_inactive(self):
        self.assertFalse(self.target.exists())
        candidates = list(self.base.glob(".refreshed.refresh-*"))
        self.assertEqual(len(candidates), 1)
        with self.assertRaisesRegex(ReelsError, "not active"):
            Workspace.load(candidates[0], code_root=self.code)
        return candidates[0]

    @staticmethod
    def fingerprint(root):
        return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob("*") if p.is_file() and not p.is_symlink()}

    def test_uncommitted_private_plan_and_new_source_assets_survive(self):
        edited = self.edit_private()
        self.fixture.asset("clip-b")
        self.fixture.plan("new-ride", [{"asset": "clip-b", "in": 0, "out": 1}])
        (self.source / ".env").write_text("TOKEN=do not copy")
        (self.source / "out/production-export.mp4").write_bytes(b"production output")
        (self.old.data_root / "out").mkdir(exist_ok=True)
        (self.old.data_root / "out/private-export.mp4").write_bytes(b"private output")
        old_before = self.fingerprint(self.old.root)
        source_before = self.fingerprint(self.source)
        fresh = self.refresh(keep_private=True)
        plan = json.loads((fresh.data_root / self.plan_name).read_bytes())
        self.assertEqual(plan["uncommittedNote"], edited["uncommittedNote"])
        self.assertEqual(plan["segments"][0]["out"], 1)
        self.assertEqual(plan["_editor"]["documentId"], edited["_editor"]["documentId"])
        self.assertEqual(set(plan["_editor"]["nodes"]), set(edited["_editor"]["nodes"]))
        self.assertTrue((fresh.data_root / "library/assets/clip-b.json").is_file())
        self.assertTrue((fresh.data_root / "plans/new-ride.json").is_file())
        self.assertEqual(len(fresh._originals), 2)
        self.assertFalse((fresh.data_root / ".env").exists())
        self.assertFalse((fresh.data_root / "out").exists())
        self.assertEqual(self.fingerprint(self.old.root), old_before)
        self.assertEqual(self.fingerprint(self.source), source_before)
        self.assertNotEqual(self.private_plan.stat().st_ino,
                            (fresh.data_root / self.plan_name).stat().st_ino)

    def test_default_conflict_reports_identities_and_leaves_candidate_inactive(self):
        self.edit_private()
        old_before = self.fingerprint(self.old.root)
        with self.assertRaises(RefreshConflict) as caught:
            self.refresh()
        candidate = self.assert_inactive()
        report = json.loads(caught.exception.report_path.read_bytes())
        self.assertEqual(caught.exception.report_path.parent, candidate)
        self.assertEqual(report["conflicts"][0]["path"], self.plan_name)
        self.assertNotEqual(report["conflicts"][0]["before"]["sha256"],
                            report["conflicts"][0]["incoming"]["sha256"])
        self.assertEqual(report["conflicts"][0]["resolutionChoices"], ["private", "source"])
        self.assertEqual(self.fingerprint(self.old.root), old_before)

    def test_explicit_source_choice_preserves_stable_ids_and_drops_private_content(self):
        edited = self.edit_private()
        fresh = self.refresh(resolutions={self.plan_name: "source"})
        plan = json.loads((fresh.data_root / self.plan_name).read_bytes())
        self.assertNotIn("uncommittedNote", plan)
        self.assertEqual(plan["segments"][0]["out"], 2)
        self.assertEqual(plan["_editor"]["documentId"], edited["_editor"]["documentId"])
        self.assertEqual(set(plan["_editor"]["nodes"]), set(edited["_editor"]["nodes"]))

    def test_explicit_private_choice_and_private_only_deletion(self):
        self.edit_private()
        private_only = self.old.data_root / "learning/private.json"
        write_json(private_only, {"note": "private-only"})
        fresh = self.refresh(resolutions={
            self.plan_name: "private", "learning/private.json": "source",
        })
        self.assertEqual(json.loads((fresh.data_root / self.plan_name).read_bytes())["uncommittedNote"],
                         "preserve this")
        self.assertFalse((fresh.data_root / "learning/private.json").exists())
        self.assertTrue(private_only.exists())

    def test_private_only_requires_choice_and_keep_private_retains_it(self):
        private_only = self.old.data_root / "learning/private.json"
        write_json(private_only, {"note": "private-only"})
        with self.assertRaises(RefreshConflict) as caught:
            self.refresh()
        conflict = json.loads(caught.exception.report_path.read_bytes())["conflicts"][0]
        self.assertIsNone(conflict["incoming"])
        self.assertTrue(conflict["sourceDeletes"])
        self.target = self.base / "second"
        fresh = self.refresh(keep_private=True)
        self.assertEqual((fresh.data_root / "learning/private.json").read_bytes(), private_only.read_bytes())

    def test_identical_files_do_not_conflict_and_source_additions_arrive(self):
        self.fixture.asset("clip-b")
        fresh = self.refresh()
        self.assertTrue((fresh.data_root / "library/assets/clip-b.json").exists())
        self.assertEqual(len(fresh._originals), 2)

    def test_media_file_differences_need_choice(self):
        (self.source / "music/song.mp3").write_bytes(b"incoming")
        (self.old.data_root / "music").mkdir(exist_ok=True)
        (self.old.data_root / "music/song.mp3").write_bytes(b"private")
        with self.assertRaises(RefreshConflict) as caught:
            self.refresh()
        self.assertEqual(json.loads(caught.exception.report_path.read_bytes())["conflicts"][0]["path"],
                         "music/song.mp3")

    def test_private_and_source_races_after_initialization_abort(self):
        original_init = refresh_module.init_workspace
        for side in ("private", "source"):
            with self.subTest(side=side):
                self.target = self.base / f"race-{side}"

                def initialize_then_edit(*args, **kwargs):
                    result = original_init(*args, **kwargs)
                    path = self.private_plan if side == "private" else self.source / self.plan_name
                    value = json.loads(path.read_bytes())
                    value["raced"] = side
                    write_json(path, value)
                    return result

                with mock.patch.object(refresh_module, "init_workspace", side_effect=initialize_then_edit):
                    with self.assertRaisesRegex(ReelsError, "changed"):
                        self.refresh(keep_private=True)
                self.assertFalse(self.target.exists())

    def test_original_race_aborts_and_missing_original_is_offline(self):
        original_init = refresh_module.init_workspace
        original = self.source / self.asset["media"]

        def initialize_then_edit(*args, **kwargs):
            result = original_init(*args, **kwargs)
            original.write_bytes(b"changed original")
            return result

        with mock.patch.object(refresh_module, "init_workspace", side_effect=initialize_then_edit):
            with self.assertRaisesRegex(ReelsError, "source original changed"):
                self.refresh(resolutions={"library/assets/clip-a.json": "source"})
        self.assert_inactive()
        original.unlink()
        self.target = self.base / "offline"
        fresh = self.refresh(keep_private=True)
        self.assertEqual(fresh.media_status()[self.asset["media"]]["status"], "offline")
        self.assertEqual(fresh._originals[self.asset["media"]]["identity"],
                         self.old._originals[self.asset["media"]]["identity"])

    def test_changed_original_is_a_conflict_and_private_never_substitutes(self):
        (self.source / self.asset["media"]).write_bytes(b"replacement")
        with self.assertRaises(RefreshConflict) as caught:
            self.refresh()
        conflict = json.loads(caught.exception.report_path.read_bytes())["conflicts"][0]
        self.assertEqual(conflict["path"], "library/assets/clip-a.json")
        self.assertNotEqual(conflict["beforeOriginal"], conflict["incomingOriginal"])
        self.target = self.base / "private-identity"
        with self.assertRaisesRegex(ReelsError, "refusing substitution"):
            self.refresh(keep_private=True)

    def test_symlink_and_hardlink_escape_rejected(self):
        for side in ("private", "source"):
            for kind in ("symlink", "hardlink"):
                with self.subTest(side=side, kind=kind):
                    root = self.old.data_root if side == "private" else self.source
                    escape = root / "music/escape.mp3"
                    escape.parent.mkdir(exist_ok=True)
                    outside = self.base / "outside.mp3"
                    outside.write_bytes(b"untouched")
                    if kind == "symlink":
                        escape.symlink_to(outside)
                    else:
                        os.link(outside, escape)
                    with self.assertRaisesRegex(ReelsError, "symlink|hard.?link"):
                        self.refresh(keep_private=True)
                    self.assertFalse(self.target.exists())
                    self.assertEqual(outside.read_bytes(), b"untouched")
                    escape.unlink()

    def test_existing_target_and_target_creation_race_never_overwrite(self):
        self.target.mkdir()
        marker = self.target / "owned"
        marker.write_text("keep")
        with self.assertRaisesRegex(ReelsError, "already exists"):
            self.refresh()
        self.assertEqual(marker.read_text(), "keep")
        self.target = self.base / "raced-target"
        publish = refresh_module.ws._publish_without_replace

        def create_target(staged, destination):
            if destination == self.target:
                destination.mkdir()
                (destination / "owned").write_text("keep raced")
            publish(staged, destination)

        with mock.patch.object(refresh_module.ws, "_publish_without_replace", side_effect=create_target):
            with self.assertRaisesRegex(ReelsError, "appeared during initialization"):
                self.refresh()
        self.assertEqual((self.target / "owned").read_text(), "keep raced")
        candidates = list(self.base.glob(".raced-target.refresh-*"))
        self.assertEqual(json.loads((candidates[0] / "state.json").read_bytes())["status"], "failed")

    def seed_renderer(self):
        revision = "fixture-renderer-1"
        root = self.old.root / "code/renderers" / revision
        source = root / "source/scripts/reels"
        source.mkdir(parents=True)
        (source / "render.py").write_text("# exact selected renderer\n")
        (source / "render.py").chmod(0o400)
        write_json(root / "manifest.json", {
            "rendererRevision": revision, "parentRendererRevision": None, "state": "baseline",
        })
        dependencies = self.code / "remotion/node_modules"
        dependencies.mkdir(parents=True)
        remotion = root / "source/remotion"
        remotion.mkdir()
        (remotion / "node_modules").symlink_to(dependencies, target_is_directory=True)
        plan = self.edit_private()
        plan["_editor"]["rendererRevision"] = revision
        plan["_editor"]["rendererPreview"] = {"url": "/old-preview", "rendererRevision": revision}
        write_json(self.private_plan, plan)
        # An old committed baseline deliberately differs from the live plan.
        committed = json.loads(self.private_plan.read_bytes())
        committed["segments"][0]["out"] = 2
        refresh_module._initialize_history(self.old.root, self.plan_name, committed, revision, {})
        (self.old.root / "code/renderers/unreferenced").mkdir()
        (self.old.root / "code/renderers/unreferenced/unsafe").symlink_to(self.source)
        return revision

    def test_selected_renderer_retained_new_history_uses_uncommitted_plan(self):
        revision = self.seed_renderer()
        before = self.fingerprint(self.old.root)
        fresh = self.refresh(keep_private=True)
        persisted = fresh.root / "code/renderers" / revision
        self.assertEqual((persisted / "source/scripts/reels/render.py").read_text(),
                         "# exact selected renderer\n")
        self.assertTrue((persisted / "source/remotion/node_modules").is_symlink())
        self.assertEqual((persisted / "source/remotion/node_modules").resolve(),
                         self.code / "remotion/node_modules")
        self.assertFalse((fresh.root / "code/renderers/unreferenced").exists())
        plan = json.loads((fresh.data_root / self.plan_name).read_bytes())
        self.assertEqual(plan["_editor"]["rendererRevision"], revision)
        self.assertNotIn("rendererPreview", plan["_editor"])
        self.assertEqual(plan["segments"][0]["out"], 1)
        old_head = next((self.old.root / "history").rglob("HEAD"))
        new_head = next((fresh.root / "history").rglob("HEAD"))
        self.assertNotEqual(json.loads(old_head.read_bytes())["revision"],
                            json.loads(new_head.read_bytes())["revision"])
        # Real recovery must not re-materialize the old plan or change renderer.
        store = DocumentStore(fresh, self.plan_name, compiler=lambda *a, **k: {})
        self.addCleanup(store.close)
        self.assertEqual(store._renderer_revision(), revision)
        self.assertEqual(json.loads((fresh.data_root / self.plan_name).read_bytes())["segments"][0]["out"], 1)
        self.assertEqual(self.fingerprint(self.old.root), before)

    def test_private_head_change_during_refresh_aborts(self):
        self.seed_renderer()
        original_copy = refresh_module._copy_renderer

        def copy_then_edit(*args, **kwargs):
            result = original_copy(*args, **kwargs)
            head = next((self.old.root / "history").rglob("HEAD"))
            value = json.loads(head.read_bytes())
            value["raced"] = True
            write_json(head, value)
            return result

        with mock.patch.object(refresh_module, "_copy_renderer", side_effect=copy_then_edit):
            with self.assertRaisesRegex(ReelsError, "private data or HEAD changed"):
                self.refresh(keep_private=True)
        self.assert_inactive()

    def test_selected_renderer_edit_during_snapshot_aborts(self):
        revision = self.seed_renderer()
        original_init = refresh_module.init_workspace
        path = self.old.root / "code/renderers" / revision / "source/scripts/reels/render.py"

        def initialize_then_edit(*args, **kwargs):
            result = original_init(*args, **kwargs)
            path.chmod(0o600)
            path.write_text("# edited during refresh\n")
            return result

        with mock.patch.object(refresh_module, "init_workspace", side_effect=initialize_then_edit):
            with self.assertRaisesRegex(ReelsError, "selected renderer changed"):
                self.refresh(keep_private=True)
        self.assert_inactive()

    def test_selected_renderer_head_fallback_survives_source_choice(self):
        revision = self.seed_renderer()
        plan = json.loads(self.private_plan.read_bytes())
        plan["_editor"].pop("rendererRevision")
        write_json(self.private_plan, plan)
        fresh = self.refresh(resolutions={self.plan_name: "source"})
        current = json.loads((fresh.data_root / self.plan_name).read_bytes())
        self.assertEqual(current["segments"][0]["out"], 2)
        self.assertEqual(current["_editor"]["rendererRevision"], revision)
        self.assertTrue((fresh.root / "code/renderers" / revision / "manifest.json").exists())

    def test_selected_renderer_escape_or_missing_snapshot_aborts(self):
        revision = self.seed_renderer()
        path = self.old.root / "code/renderers" / revision / "source/scripts/reels/escape.py"
        path.symlink_to(self.source / self.plan_name)
        with self.assertRaisesRegex(ReelsError, "symlink"):
            self.refresh(keep_private=True)
        self.assertFalse(self.target.exists())

    def test_bad_resolution_paths_and_merged_references_fail_closed(self):
        for resolutions in ({"../bad": "private"}, {self.plan_name: "merge"}, {"plans/nope.json": "source"}):
            with self.subTest(resolutions=resolutions):
                with self.assertRaises(ReelsError):
                    self.refresh(resolutions=resolutions)
                self.assertFalse(self.target.exists())
        self.target = self.base / "bad-reference"
        plan = json.loads(self.private_plan.read_bytes())
        plan["segments"][0]["asset"] = "not-present"
        write_json(self.private_plan, plan)
        with self.assertRaisesRegex(ReelsError, "unresolved asset"):
            self.refresh(keep_private=True)
        self.assertFalse(self.target.exists())

    def test_merge_cannot_delete_an_asset_still_referenced_by_a_private_plan(self):
        (self.source / "library/assets/clip-a.json").unlink()
        self.fixture.plan("ride", [])
        with self.assertRaisesRegex(ReelsError, "unresolved asset"):
            self.refresh(resolutions={
                self.plan_name: "private", "library/assets/clip-a.json": "source",
            })
        self.assert_inactive()

    def test_nested_or_symlinked_destination_never_writes_old_workspace(self):
        before = self.fingerprint(self.old.root)
        self.target = self.old.root / "nested"
        with self.assertRaisesRegex(ReelsError, "independent"):
            self.refresh()
        alias = self.base / "alias"
        alias.symlink_to(self.old.root, target_is_directory=True)
        self.target = alias / "nested"
        with self.assertRaisesRegex(ReelsError, "symlink"):
            self.refresh()
        self.assertEqual(self.fingerprint(self.old.root), before)


if __name__ == "__main__":
    unittest.main()
