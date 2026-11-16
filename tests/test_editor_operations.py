from __future__ import annotations

import unittest
from pydantic import ValidationError
from reels.editor.operations import OperationBatch, operation_schema


class OperationSchemaTests(unittest.TestCase):
    def batch(self, operation):
        return {"expected": {"document": "d", "library": "l", "renderer": "r"},
                "reason": "a bounded edit", "operations": [operation]}

    def test_mcp_exposes_real_discriminated_operation_schema(self):
        schema = operation_schema()
        names = schema["properties"]["operations"]["items"]["discriminator"]["mapping"]
        self.assertIn("set_field", names)
        self.assertIn("trim_segment", names)
        self.assertIn("correct_words", names)
        self.assertNotIn("update_node", names)

    def test_node_relative_field_edit(self):
        value = self.batch({"op": "set_field", "id": "stable", "path": "/volume", "value": 0.85})
        self.assertEqual(OperationBatch.model_validate(value).wire(), value)

    def test_unknown_operations_and_extra_authority_are_rejected(self):
        for operation in ({"op": "set", "id": "stable", "path": "/volume", "value": 1},
                          {"op": "set_field", "id": "stable", "path": "/volume", "value": 1, "shell": "no"}):
            with self.assertRaises(ValidationError):
                OperationBatch.model_validate(self.batch(operation))
        with self.assertRaises(ValidationError):
            OperationBatch.model_validate({**self.batch({"op": "remove_overlay", "id": "x"}), "lease": "manual-token"})

    def test_time_units_and_resize_wire_alias(self):
        batch = OperationBatch.model_validate(self.batch({"op": "resize_overlay", "id": "x", "for": 1.5}))
        self.assertEqual(batch.wire()["operations"][0]["for"], 1.5)
        with self.assertRaises(ValidationError):
            OperationBatch.model_validate(self.batch({"op": "trim_segment", "id": "x", "sourceIn": 5, "sourceOut": 2}))

    def test_overwrite_insert_schema_and_wire_preserve_renderer_settings(self):
        schema = operation_schema()
        insertion = schema["$defs"]["FootageInsert"]
        self.assertEqual(insertion["required"], ["asset"])
        self.assertEqual(insertion["properties"]["asset"]["type"], "string")
        self.assertTrue(insertion["additionalProperties"])
        operation = {
            "op": "move_segments", "editId": "a0000000-0000-4000-8000-000000000001",
            "mode": "overwrite", "placements": [{
                "track": "V2", "atFrame": 90,
                "insert": {"asset": "source", "in": 1.25, "out": 2.5,
                           "speed": 0.75, "framing": {"x": 0.2}},
            }],
        }
        value = self.batch(operation)
        self.assertEqual(OperationBatch.model_validate(value).wire(), value)

    def test_overwrite_sources_reject_missing_asset_null_ambiguity_and_reserved_metadata(self):
        for source in (
            {}, {"id": None}, {"insert": None}, {"id": "clip", "insert": None},
            {"id": None, "insert": {"asset": "source"}}, {"id": "clip\0tail"},
            {"insert": {}}, {"insert": {"asset": ""}}, {"insert": {"asset": 123}},
            {"insert": {"asset": "source\0tail"}},
            {"insert": {"asset": "source", "timeline": {}}},
            {"insert": {"asset": "source", "_editor": {}}},
        ):
            with self.subTest(source=source), self.assertRaises(ValidationError):
                OperationBatch.model_validate(self.batch({
                    "op": "move_segments", "editId": "a0000000-0000-4000-8000-000000000001",
                    "mode": "overwrite", "placements": [{"track": "V1", "atFrame": 0, **source}],
                }))

    def test_raw_overwrite_rejects_invalid_ids_before_materializing_plan(self):
        from reels.editor.overwrite import apply_overwrite

        for node_id in (None, "", 123, "clip\0tail"):
            context = {"plan": {"segments": []}, "compiled": {"fps": 30}}
            with self.subTest(node_id=node_id), self.assertRaisesRegex(ValueError, "without NUL"):
                apply_overwrite(context, {
                    "op": "move_segments", "editId": "a0000000-0000-4000-8000-000000000001",
                    "mode": "overwrite", "placements": [{"id": node_id, "track": "V1", "atFrame": 0}],
                }, None)
            self.assertEqual(context["plan"], {"segments": []})


if __name__ == "__main__":
    unittest.main()
