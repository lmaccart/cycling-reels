"""Reels Editor: the colour grade controls (schema sliders/toggles, nested edits, preview cache keys)."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from helpers import TempProject, words
from reels.editor.schema import DescriptorError, inspector_schema, validate_descriptors
from reels.editor.transactions import DocumentError, DocumentStore
from reels.editor.worker import _cache_identity, _product_kind_and_options, _stage_spec
from test_editor_transactions import Workspace, fake_compile

PLAN_TARGETS = ("document", "segment.video", "segment.photo", "cutaway.video", "cutaway.photo")


def colour_fields(target: dict) -> dict[str, dict]:
    group = next(g for g in target["groups"] if g["id"] == "colour")
    (grade,) = group["fields"]
    return {f["id"]: f for f in grade["fields"]}


class GradeSchemaTests(unittest.TestCase):
    def test_every_footage_target_and_the_project_have_colour_controls(self):
        targets = inspector_schema()["targets"]
        for name in PLAN_TARGETS:
            fields = colour_fields(targets[name])
            self.assertEqual(set(fields), {"look", "strength", "rescue", "warmth", "exposure"}, name)
            self.assertEqual(fields["look"]["choices"], ["house", "moody", "none"])
            self.assertEqual(fields["rescue"]["type"], "boolean")
            self.assertEqual((fields["look"]["default"], fields["rescue"]["default"]), ("none", False), name)
            for slider in ("strength", "warmth", "exposure"):
                self.assertEqual(fields[slider]["display"], "slider")
                self.assertIn("min", fields[slider])
                self.assertIn("max", fields[slider])
        project = colour_fields(targets["config.render"])
        self.assertEqual(set(project), {"look", "strength", "rescue"})
        self.assertTrue(project["look"]["path"].startswith("/render/grade/"))

    def test_slider_ranges_match_the_renderer(self):
        fields = colour_fields(inspector_schema()["targets"]["segment.video"])
        self.assertEqual((fields["strength"]["min"], fields["strength"]["max"]), (0, 1.5))
        self.assertEqual((fields["warmth"]["min"], fields["warmth"]["max"]), (-1, 1))
        self.assertEqual((fields["exposure"]["min"], fields["exposure"]["max"]), (-1, 1))

    def test_display_hint_is_validated(self):
        def extra(**field):
            return [{"target": "segment.video", "group": "Effect",
                     "field": {"id": "x", "label": "X", "path": "/segments/{index}/x", **field}}]

        self.assertEqual(len(validate_descriptors(extra(type="number", min=0, max=1, display="slider"))), 1)
        for bad in (dict(type="number", min=0, max=1, display="knob"), dict(type="number", min=0, display="slider"),
                    dict(type="text", display="slider")):
            with self.subTest(bad=bad), self.assertRaises(DescriptorError):
                validate_descriptors(extra(**bad))


class GradeEditTests(unittest.TestCase):
    def setUp(self):
        self.temp = TempProject(self)
        self.workspace = Workspace(self.temp.project)
        self.temp.asset("asset-a", words=words(("hello", 1.0, 1.3)))
        self.temp.asset("asset-b")

    def store(self, **extra):
        path = self.temp.plan("2026-11-15-grade", [
            {"asset": "asset-a", "in": 0, "out": 5},
            {"asset": "asset-b", "in": 1, "out": 6},
        ], **extra)
        return DocumentStore(self.workspace, path, compiler=fake_compile), path

    def apply(self, store, *ops):
        before = store.snapshot()
        return store.apply({"expected": before["revisions"], "origin": "manual", "reason": "colour",
                            "operations": list(ops)})

    def test_first_slider_move_creates_the_reel_grade(self):
        store, path = self.store()
        self.apply(store, {"op": "set_field", "id": "document", "path": "/grade/strength", "value": 0.8})
        self.apply(store, {"op": "set_field", "id": "document", "path": "/grade/rescue", "value": False})
        self.assertEqual(json.loads(path.read_text())["grade"], {"strength": 0.8, "rescue": False})

    def test_clip_grade_is_local_to_that_clip(self):
        store, path = self.store()
        node = next(n for n in store.snapshot()["nodes"] if n["kind"] == "segment")
        self.apply(store, {"op": "set_field", "id": node["id"], "path": "/grade/warmth", "value": 0.3})
        segments = json.loads(path.read_text())["segments"]
        self.assertEqual(segments[0]["grade"], {"warmth": 0.3})
        self.assertNotIn("grade", segments[1])

    def test_a_string_grade_is_not_overwritten_by_a_nested_edit(self):
        store, path = self.store(grade="none")
        with self.assertRaises((DocumentError, ValueError)):
            self.apply(store, {"op": "set_field", "id": "document", "path": "/grade/strength", "value": 0.8})
        self.assertEqual(json.loads(path.read_text())["grade"], "none")

    def test_project_default_lives_in_config(self):
        store, _ = self.store()
        self.apply(store, {"op": "set_field", "id": "project", "path": "/render/grade/look", "value": "moody"})
        config = json.loads((self.temp.root / "config.json").read_text())
        self.assertEqual(config["render"]["grade"]["look"], "moody")


class GradePreviewCacheTests(unittest.TestCase):
    def test_grade_settings_are_part_of_the_staged_product(self):
        args = (None, object(), Path("seg0.mp4"), 30)
        plain = _product_kind_and_options("stage_video", args, {})[2]
        graded = _product_kind_and_options("stage_video", args, {"grade": {"look": "house", "strength": 0.7}})[2]
        self.assertNotIn("grade", plain)
        self.assertEqual(graded["grade"]["strength"], 0.7)
        photo = _product_kind_and_options("stage_photo", (None, object(), Path("seg1.jpg")), {"grade": {"look": "moody"}})[2]
        self.assertEqual(photo["grade"]["look"], "moody")

    def test_segment_grade_is_part_of_the_stage_spec(self):
        self.assertEqual(_stage_spec({"in": 0, "grade": {"warmth": 0.2}, "text": []}), {"grade": {"warmth": 0.2}, "in": 0})

    def test_reel_grade_changes_the_staging_cache_key(self):
        temp = TempProject(self)
        code_root = Path(__file__).resolve().parents[1]
        snapshot = {"revisions": {"renderer": "r1"}}
        built = {"segs": [], "fps": 30, "sound": {}, "props": {}, "exhibits": []}
        keys = {_cache_identity(temp.project, {**built, "grade": g}, snapshot, code_root)[0]
                for g in (None, {"strength": 0.5}, {"strength": 0.6}, "none")}
        self.assertEqual(len(keys), 4)


if __name__ == "__main__":
    unittest.main()
