"""Bounded metadata views that never require Claude Code's unavailable spill-file reader."""
from __future__ import annotations

import copy
import json

from .protocol import ProtocolError

MAX_RESULT_CHARS = 12_000
MAX_VALUE_CHARS = 1_800


def _size(value):
    # Count escaped, pretty JSON too: Unicode and indentation must not turn a
    # nominally small tool response into an inaccessible CLI spill file.
    return len(json.dumps(value, ensure_ascii=True, indent=2, default=str))


def _preview(value):
    if _size(value) <= MAX_VALUE_CHARS:
        return copy.deepcopy(value)
    if isinstance(value, dict):
        return {"summaryOnly": True, "type": "object", "count": len(value),
                "keys": [str(key)[:80] for key in list(value)[:20]]}
    if isinstance(value, list):
        return {"summaryOnly": True, "type": "array", "count": len(value)}
    return {"summaryOnly": True, "type": "string", "characters": len(str(value)), "preview": str(value)[:160]}


def _bounds(offset, limit, maximum=50):
    return max(0, int(offset)), min(maximum, max(1, int(limit)))


def _node_row(node):
    fields = ("id", "kind", "lane", "parentId", "startFrame", "endFrame", "frames",
              "assetId", "sourceIn", "sourceOut", "assetKind", "generated", "readOnly", "timingState")
    row = {key: node[key] for key in fields if key in node}
    if "raw" in node:
        row["raw"] = _preview(node["raw"])
    return row


def project_summary(snapshot, offset=0, limit=20):
    offset, limit = _bounds(offset, limit)
    plan = snapshot.get("plan") or {}
    root_fields = {key: _preview(value) for key, value in plan.items() if key not in {"_editor", "segments"}}
    if _size(root_fields) > 3_000:
        root_fields = {"summaryOnly": True, "keys": [str(key)[:80] for key in list(root_fields)[:20]],
                       "readWith": "get_node(id='document', path='', offset=0)"}
    preview = snapshot.get("preview") or {}
    result = {
        key: copy.deepcopy(snapshot[key])
        for key in ("documentId", "revisions", "fps", "durationFrames", "viewOnly") if key in snapshot
    }
    result.update(
        documentFields=root_fields, segmentCount=len(plan.get("segments") or []),
        projectConfigKeys=list((snapshot.get("projectConfig") or snapshot.get("config") or {}).keys())[:30],
        preview={key: preview[key] for key in ("state", "mode", "requestedMode", "revisions", "fallbackReason")
                 if key in preview},
        warnings=[str(value)[:200] for value in (snapshot.get("warnings") or [])[:6]],
        nodes=[], offset=offset, total=len(snapshot.get("nodes") or []), next=None,
        readHelp="Paginate get_project with next. get_node reads raw fields by stable id and relative JSON pointer; "
                 "id=document reads reel fields, id=project reads config. summaryOnly values are NOT raw data.",
    )
    result["permissions"] = [
        {key: _preview(permission[key]) for key in ("id", "kind", "state", "documentId", "scope")
         if key in permission}
        for permission in (snapshot.get("permissions") or [])[:3]
    ]
    if snapshot.get("change"):
        change = snapshot["change"]
        result["change"] = {
            "origin": change.get("origin"), "reason": str(change.get("reason") or "")[:300],
            "affectedIds": (change.get("affectedIds") or [])[:20],
        }
    nodes = snapshot.get("nodes") or []
    for node in nodes[offset:offset + limit]:
        row = _node_row(node)
        candidate = {**result, "nodes": [*result["nodes"], row]}
        if _size(candidate) > MAX_RESULT_CHARS:
            break
        result["nodes"].append(row)
    end = offset + len(result["nodes"])
    result["next"] = end if end < len(nodes) else None
    if nodes[offset:] and not result["nodes"]:
        raise ProtocolError("metadata_too_large", "Use get_node with a stable node ID and a specific field.")
    return result


def value_page(value, offset=0, limit=20):
    offset, limit = _bounds(offset, limit)
    if isinstance(value, dict):
        entries = list(value.items())
        rows = [{"key": key, "value": _preview(child)} for key, child in entries[offset:offset + limit]]
        result = {"type": "object", "fields": [], "offset": offset, "total": len(entries), "next": None}
        field = "fields"
    elif isinstance(value, list):
        rows = [{"index": index, "value": _preview(child)}
                for index, child in enumerate(value[offset:offset + limit], offset)]
        result = {"type": "array", "items": [], "offset": offset, "total": len(value), "next": None}
        field = "items"
    elif isinstance(value, str):
        return text_page(value, offset, 4_000)
    else:
        return {"type": "scalar", "value": value, "offset": 0, "total": 1, "next": None}
    for row in rows:
        if _size({**result, field: [*result[field], row]}) > MAX_RESULT_CHARS - 1_000:
            break
        result[field].append(row)
    end = offset + len(result[field])
    result["next"] = end if end < result["total"] else None
    if rows and not result[field]:
        raise ProtocolError("metadata_too_large", "Select a smaller field with a JSON pointer.")
    return result


def read_node(snapshot, node_id, path="", offset=0, limit=20):
    if node_id == "document":
        value = {key: child for key, child in snapshot["plan"].items() if key != "_editor"}
    elif node_id == "project":
        value = snapshot.get("projectConfig") or snapshot.get("config") or {}
    else:
        node = next((node for node in snapshot.get("nodes") or [] if node["id"] == node_id), None)
        if node is None:
            raise ProtocolError("node_not_found", "Read get_project for current stable node IDs.")
        value = node.get("raw")
    if not isinstance(path, str) or (path and not path.startswith("/")):
        raise ProtocolError("invalid_pointer", "Use a relative JSON pointer, such as /volume.")
    try:
        for component in path.split("/")[1:] if path else []:
            component = component.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not component.isdigit():
                    raise KeyError(component)
                value = value[int(component)]
            else:
                value = value[component]
    except (KeyError, IndexError, TypeError) as exc:
        raise ProtocolError("field_not_found", "That field is not present in this node.") from exc
    return {"id": node_id, "path": path, "revisions": snapshot["revisions"],
            **value_page(value, offset, limit)}


def text_page(content, offset=0, limit=4_000):
    offset, limit = _bounds(offset, limit, maximum=6_000)
    text = content[offset:offset + limit]
    while _size({"content": text}) > MAX_RESULT_CHARS - 1_000 and len(text) > 1:
        text = text[:len(text) // 2]
    end = offset + len(text)
    return {"type": "string", "content": text, "offset": offset, "total": len(content),
            "next": end if end < len(content) else None}


def library_page(result, offset=0):
    rows = result.get("assets") or []
    page = {"assets": [], "offset": int(offset), "total": result.get("total", len(rows)), "next": None}
    for asset in rows:
        row = {key: asset[key] for key in ("id", "kind", "originalName", "created", "status") if key in asset}
        row["summary"] = str((asset.get("tags") or {}).get("summary") or "")[:300]
        row["duration"] = (asset.get("probe") or {}).get("duration")
        row["moments"] = len((asset.get("tags") or {}).get("moments") or [])
        if _size({**page, "assets": [*page["assets"], row]}) > MAX_RESULT_CHARS:
            break
        page["assets"].append(row)
    end = int(offset) + len(page["assets"])
    page["next"] = end if end < page["total"] else None
    return page


def data_page(result, field="items", offset=None, limit=None, *, max_chars=MAX_RESULT_CHARS):
    """Shrink metadata pages without truncating evidence or hiding continuation."""
    rows = result.get(field) or []
    offset = max(0, int(result.get("offset", 0) if offset is None else offset))
    count = len(rows) if limit is None else min(50, max(1, int(limit)))
    # Existing index pages are already sliced; regex matches are not.
    available = rows[offset:offset + count] if field == "matches" else rows[:count]
    total = result.get("total", len(rows))
    page = {key: value for key, value in result.items() if key != field}
    page.update({field: [], "offset": offset, "limit": 0, "total": total, "next": None})
    for row in available:
        if _size({**page, field: [*page[field], row]}) > min(MAX_RESULT_CHARS, max_chars):
            break
        page[field].append(row)
    page["limit"] = len(page[field])
    end = offset + page["limit"]
    page["next"] = end if end < total else None
    if available and not page[field]:
        raise ProtocolError("metadata_too_large", "One metadata item exceeds the tool budget; refine the query.")
    return page


def job_summary(job, offset=0, limit=2_000):
    result = {key: value for key, value in job.items() if key not in {"result", "error"}}
    if job.get("error"):
        error = job["error"]
        details = error.get("details")
        if isinstance(details, dict):
            details = {
                key: (text_page(value, offset, limit) if isinstance(value, str) and _size(value) > MAX_VALUE_CHARS
                      else _preview(value))
                for key, value in details.items()
            }
        result["error"] = {"code": error.get("code"), "message": str(error.get("message", ""))[:1_000],
                           "details": details}
    value = job.get("result")
    if isinstance(value, dict):
        validation = value.get("validation") or {}
        preview = value.get("preview") or validation.get("preview") or {}
        result["result"] = {
            key: value[key] for key in ("rendererRevision", "revisions", "status", "mode", "jobId") if key in value
        }
        if isinstance(value.get("snapshot"), dict):
            result["result"]["revisions"] = value["snapshot"].get("revisions")
        if validation.get("checks"):
            result["result"]["checks"] = validation["checks"]
        if preview:
            result["result"]["preview"] = {key: preview[key] for key in ("mode", "state", "revisions") if key in preview}
    return result
