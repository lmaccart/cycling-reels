from __future__ import annotations

import copy
import json
import unittest

from reels.editor.schema import CONTROL_TYPES, DescriptorError, inspector_schema, validate_descriptors


def fields(value):
    if isinstance(value, dict):
        if "path" in value and "type" in value and "label" in value:
            yield value
        for child in value.values():
            yield from fields(child)
    elif isinstance(value, list):
        for child in value:
            yield from fields(child)


class InspectorSchemaTests(unittest.TestCase):
    def test_complete_native_target_inventory_is_json_data(self):
        schema = inspector_schema()
        self.assertEqual(schema["version"], 1)
        required = {"document", "segment.video", "segment.photo", "cutaway.video", "cutaway.photo",
                    "hook", "endCard", "titleCard", "punch.manual", "punch.words", "punch.onBeats",
                    "blur.patch", "stat", "route", "progress", "bleep.window", "exhibit.chart",
                    "exhibit.reel", "exhibit.segment", "musicDrop", "sfxCue.reel", "sfxCue.segment",
                    "config.render", "config.audio", "config.brand", "transcript.word"}
        self.assertTrue(required <= set(schema["targets"]))
        self.assertGreater(len(list(fields(schema))), 200)
        for item in fields(schema):
            self.assertIn(item["type"], CONTROL_TYPES)
            self.assertNotEqual(item["type"], "raw_json")
        json.dumps(schema, allow_nan=False)

    def test_boundaries_and_local_timing_are_exposed(self):
        schema = inspector_schema()
        by_path = {field["path"]: field for field in fields(schema)}
        self.assertEqual(by_path["/segments/{index}/speed"]["min"], 0.25)
        self.assertEqual(by_path["/segments/{index}/speed"]["max"], 4)
        self.assertEqual(by_path["/segments/{index}/bleep/windows/{item}/at"]["timingDomain"], "segment_seconds")
        self.assertEqual(by_path["/segments/{index}/in"]["timingDomain"], "source_seconds")
        self.assertTrue(by_path["/words/{index}/s"]["readOnly"])
        self.assertTrue(by_path["/words/{index}/e"]["readOnly"])

    def extra(self, path="/segments/{index}/myEffect/amount", **values):
        return {"target": "segment.video", "group": "My effect", "field": {
            "id": "amount", "label": "Amount", "path": path, "type": "number", "min": 0, "max": 1, **values}}

    def test_latest_main_hook_and_decorative_exhibit_controls_are_typed(self):
        schema = inspector_schema()
        by_path = {field["path"]: field for field in fields(schema)}
        self.assertEqual(by_path["/hook/size"]["choices"], ["normal", "big"])
        for path in ("/exhibits/{index}/safeArea", "/segments/{index}/exhibits/{item}/safeArea"):
            self.assertEqual(by_path[path]["type"], "boolean")
            self.assertTrue(by_path[path]["default"])

    def test_declarative_effect_addition_and_independent_round_trip(self):
        extra = self.extra()
        schema = inspector_schema([extra])
        extra["field"]["label"] = "changed caller data"
        self.assertIn("Amount", [item["label"] for item in fields(schema)])
        self.assertNotIn("changed caller data", [item["label"] for item in fields(schema)])
        self.assertNotIn("/segments/{index}/myEffect/amount", {item["path"] for item in fields(inspector_schema())})

    def test_executable_and_reserved_descriptors_fail_closed(self):
        for path in ("/segments/{index}/../bad", "/segments/{index}/_editor/id",
                     "/segments/{index}/shell", "/segments/{index}/permissions",
                     "/segments/{index}/titleCards/allowed"):
            with self.assertRaises(DescriptorError):
                validate_descriptors([self.extra(path)])
        for extra in (self.extra(type="swift"), self.extra(onChange="run()"), self.extra(min=float("nan")),
                      self.extra("/segments/{index}/speed")):
            with self.assertRaises(DescriptorError):
                validate_descriptors([extra])

    def test_effects_cannot_add_project_runtime_or_transcript_controls(self):
        for target, path in (("config.render", "/render/browserExecutable"),
                             ("config.audio", "/audio/other"),
                             ("transcript.word", "/words/{index}/newTime")):
            extra = self.extra(path)
            extra["target"] = target
            with self.assertRaises(DescriptorError):
                validate_descriptors([extra])

    def test_duplicate_and_unknown_target_rejected(self):
        with self.assertRaises(DescriptorError):
            validate_descriptors([self.extra(), self.extra()])
        extra = self.extra()
        extra["target"] = "native.code"
        with self.assertRaises(DescriptorError):
            validate_descriptors([extra])


if __name__ == "__main__":
    unittest.main()
