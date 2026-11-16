"""Wire-format helpers for reconstructable editor snapshot deltas."""

from __future__ import annotations

import hashlib
import json
from typing import Any

_MISSING = object()
_REVISION_KEYS = ("document", "library", "renderer")


def snapshot_token(snapshot: dict[str, Any]) -> str:
    """Identify the exact wire snapshot, including non-revisioned UI metadata."""
    content = {key: value for key, value in snapshot.items() if key != "snapshotToken"}
    wire = json.dumps(content, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


def _json_pointer(path: str, key: str) -> str:
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _plan_patch(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    """Create a compact RFC-6902-style patch for the plan portion of a delta."""
    if isinstance(before, dict) and isinstance(after, dict):
        patch = []
        for key in sorted(before.keys() - after.keys()):
            patch.append({"op": "remove", "path": _json_pointer(path, key)})
        for key in sorted(after.keys() - before.keys()):
            patch.append({"op": "add", "path": _json_pointer(path, key), "value": after[key]})
        for key in sorted(before.keys() & after.keys()):
            patch.extend(_plan_patch(before[key], after[key], _json_pointer(path, key)))
        return patch
    if isinstance(before, list) and isinstance(after, list) and len(before) == len(after):
        return [
            operation
            for index, (old, new) in enumerate(zip(before, after))
            for operation in _plan_patch(old, new, _json_pointer(path, str(index)))
        ]
    if before == after:
        return []
    return [{"op": "replace", "path": path, "value": after}]


def snapshot_delta(before: dict[str, Any] | None, after: dict[str, Any],
                   base_revisions: Any, base_snapshot_token: str | None = None) -> dict[str, Any] | None:
    """Return a reconstructable delta only for an exact client/server base."""
    if not isinstance(before, dict) or not isinstance(base_revisions, dict):
        return None
    old_revisions = before.get("revisions")
    if not isinstance(old_revisions, dict) or before.get("documentId") != after.get("documentId"):
        return None
    if any(str(base_revisions.get(key, "")) != str(old_revisions.get(key, ""))
           for key in _REVISION_KEYS):
        return None
    if base_snapshot_token is not None and (
        base_snapshot_token != before.get("snapshotToken")
        or base_snapshot_token != snapshot_token(before)
        or after.get("snapshotToken") != snapshot_token(after)
    ):
        return None
    old_nodes = {str(node.get("id")): node for node in before.get("nodes", []) if isinstance(node, dict)}
    new_nodes = {str(node.get("id")): node for node in after.get("nodes", []) if isinstance(node, dict)}
    changed_nodes = [
        node for node in after.get("nodes", [])
        if isinstance(node, dict) and old_nodes.get(str(node.get("id"))) != node
    ]
    removed_ids = sorted(old_nodes.keys() - new_nodes.keys())
    fields = {
        key: value for key, value in after.items()
        if key not in ("plan", "nodes", "revisions", "snapshotToken")
        and before.get(key, _MISSING) != value
    }
    removed_fields = sorted(
        key for key in before.keys() - after.keys()
        if key not in ("plan", "nodes", "revisions", "snapshotToken")
    )
    return {
        "type": "document.delta",
        "documentId": after.get("documentId"),
        "baseRevisions": {key: old_revisions.get(key) for key in _REVISION_KEYS},
        "baseSnapshotToken": before.get("snapshotToken"),
        "revisions": after.get("revisions"),
        "snapshotToken": after.get("snapshotToken") or snapshot_token(after),
        "planPatch": _plan_patch(before.get("plan"), after.get("plan")),
        "changedNodes": changed_nodes,
        "removedIds": removed_ids,
        "nodeOrder": [str(node.get("id")) for node in after.get("nodes", []) if isinstance(node, dict)],
        "fields": fields,
        "removedFields": removed_fields,
    }
