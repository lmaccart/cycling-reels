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


if __name__ == "__main__":
    unittest.main()
