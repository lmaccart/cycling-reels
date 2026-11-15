from __future__ import annotations

import json
import unittest

from reels.editor.ai_views import MAX_RESULT_CHARS, data_page, job_summary, project_summary, read_node, text_page
from reels.editor.protocol import ProtocolError


class AIViewTests(unittest.TestCase):
    def snapshot(self):
        return {
            "documentId": "doc", "revisions": {"document": "d", "library": "l", "renderer": "r"},
            "fps": 30, "durationFrames": 300,
            "plan": {"slug": "ride", "hook": {"text": "existing text"}, "_editor": {"internal": "not a plan tool"},
                     "segments": [{"asset": "a", "volume": .85}]},
            "projectConfig": {"render": {"fps": 30}}, "warnings": [],
            "nodes": [{"id": f"node-{i}", "kind": "segment", "lane": "primary", "startFrame": i * 30,
                       "endFrame": i * 30 + 30, "raw": {"asset": "a", "volume": .85, "unknown": ["x"] * 10_000}}
                      for i in range(300)],
            "preview": {"state": "ready", "mode": "interactive", "props": {"huge": "x" * 1_000_000}},
        }

    def test_project_pages_fit_cli_budget_and_preserve_current_tokens_ids(self):
        snapshot = self.snapshot()
        seen = []
        offset = 0
        while offset is not None:
            page = project_summary(snapshot, offset, 100)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=True, indent=2)), MAX_RESULT_CHARS)
            self.assertEqual(page["revisions"], snapshot["revisions"])
            self.assertNotIn("props", page["preview"])
            seen.extend(node["id"] for node in page["nodes"])
            offset = page["next"]
        self.assertEqual(seen, [node["id"] for node in snapshot["nodes"]])
        self.assertEqual(snapshot["nodes"][0]["raw"]["unknown"], ["x"] * 10_000)

    def test_raw_node_fields_and_large_collections_are_readable_without_files(self):
        snapshot = self.snapshot()
        result = read_node(snapshot, "node-0", "/volume")
        self.assertEqual(result["value"], .85)
        page = read_node(snapshot, "node-0", "/unknown", 20, 3)
        self.assertEqual([row["index"] for row in page["items"]], [20, 21, 22])
        self.assertEqual(page["next"], 23)
        self.assertEqual(read_node(snapshot, "project", "/render")["fields"][0]["value"], 30)
        self.assertNotIn("_editor", [row["key"] for row in read_node(snapshot, "document")["fields"]])
        with self.assertRaises(ProtocolError):
            read_node(snapshot, "missing")
        with self.assertRaises(ProtocolError):
            read_node(snapshot, "node-0", "/unknown/-1")

    def test_unicode_source_character_pages_reassemble_exactly(self):
        source = ("# 🚲 你好\n" * 5_000) + "pass\n"
        parts, offset = [], 0
        while offset is not None:
            page = text_page(source, offset, 6_000)
            self.assertLessEqual(len(json.dumps(page, ensure_ascii=True, indent=2)), MAX_RESULT_CHARS)
            parts.append(page["content"])
            offset = page["next"]
        self.assertEqual("".join(parts), source)

    def test_completed_job_does_not_duplicate_project_and_preview_props(self):
        summary = job_summary({
            "id": "job", "state": "completed", "kind": "renderer",
            "result": {"rendererRevision": "new", "snapshot": self.snapshot(),
                       "validation": {"checks": [{"name": "typecheck", "returncode": 0}],
                                      "preview": self.snapshot()["preview"]}},
        })
        self.assertEqual(summary["result"]["rendererRevision"], "new")
        self.assertEqual(summary["result"]["revisions"]["document"], "d")
        self.assertNotIn("props", summary["result"]["preview"])
        self.assertLess(len(json.dumps(summary)), 1_000)

    def test_grounded_metadata_pages_keep_exact_evidence_and_continuation(self):
        rows = [{"passageId": f"p{i}", "text": "exact evidence " * 60} for i in range(50)]
        page = data_page({"items": rows, "offset": 50, "total": 100})
        self.assertGreater(len(page["items"]), 0)
        self.assertLess(len(page["items"]), 50)
        self.assertEqual(page["items"], rows[:len(page["items"])])
        self.assertEqual(page["next"], 50 + len(page["items"]))
        self.assertEqual(page["limit"], len(page["items"]))
        regex = data_page({"matches": rows}, "matches", offset=20, limit=2)
        self.assertEqual(regex["matches"], rows[20:22])
        self.assertEqual(regex["next"], 22)

    def test_long_validation_diagnostic_is_readable_in_character_pages(self):
        log = "traceback\n" * 1_000 + "concrete failure"
        job = {"id": "j", "state": "failed", "error": {
            "code": "worker_failed", "message": "Worker exited", "details": {"stderr": log}}}
        chunks, offset = [], 0
        while offset is not None:
            result = job_summary(job, offset, 2_000)
            page = result["error"]["details"]["stderr"]
            chunks.append(page["content"])
            offset = page["next"]
            self.assertLess(len(json.dumps(result)), MAX_RESULT_CHARS)
        self.assertEqual("".join(chunks), log)


if __name__ == "__main__":
    unittest.main()
