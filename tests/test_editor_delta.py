"""Snapshot-delta payloads stay small when a single large-plan node changes."""

from copy import deepcopy
import json
import unittest

from reels.editor.delta import snapshot_delta, snapshot_token


class SnapshotDeltaPayloadTests(unittest.TestCase):
    def test_single_segment_edit_does_not_resend_other_segment_payloads(self):
        segments = [
            {"asset": f"asset-{index}", "volume": 1.0, "caption": "x" * 512}
            for index in range(42)
        ]
        before = {
            "documentId": "large-plan",
            "revisions": {"document": "before", "library": "library", "renderer": "renderer"},
            "plan": {"segments": segments},
            "nodes": [
                {"id": f"segment-{index}", "kind": "segment", "raw": deepcopy(segment)}
                for index, segment in enumerate(segments)
            ],
            "durationFrames": 2520,
        }
        after = deepcopy(before)
        after["revisions"]["document"] = "after"
        after["plan"]["segments"][17]["volume"] = 0.5
        after["nodes"][17]["raw"]["volume"] = 0.5
        before["snapshotToken"] = snapshot_token(before)
        after["snapshotToken"] = snapshot_token(after)

        delta = snapshot_delta(
            before, after, before["revisions"], before["snapshotToken"]
        )

        self.assertIsNotNone(delta)
        self.assertLess(len(json.dumps(delta, separators=(",", ":")).encode()), 5000)
        self.assertEqual(delta["changedNodes"], [after["nodes"][17]])
        self.assertEqual(delta["planPatch"], [
            {"op": "replace", "path": "/segments/17/volume", "value": 0.5},
        ])
