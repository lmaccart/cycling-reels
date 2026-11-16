"""Authoritative raw-plan document helpers and renderer snapshot projection."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from copy import deepcopy
from typing import Any

from .timing import seconds_to_frames

_ID_NAMESPACE = uuid.UUID("b438b4b1-d887-4910-99f5-7106c4fd6e4a")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def normalize_compiled(compiled: dict[str, Any]) -> dict[str, Any]:
    """Project real renderer Seg objects to wire values without mutating build."""
    def segment(value):
        if isinstance(value, dict):
            return deepcopy(value)
        spec = {key: deepcopy(item) for key, item in value.spec.items() if key != "_cutaways"}
        cuts = []
        for cut in value.spec.get("_cutaways") or []:
            child = segment(cut["seg"])
            match = re.search(r"cutaway (\d+)$", cut.get("where", ""))
            cuts.append({"start": cut["start"], "frames": child["frames"],
                         "rawIndex": int(match[1]) - 1 if match else None,
                         "assetId": child["asset"]["id"], "kind": child["kind"],
                         "sourceDuration": child["asset"].get("probe", {}).get("duration"),
                         "spec": child["spec"]})
        return {"index": value.index, "kind": value.kind, "asset": deepcopy(value.asset),
                "spec": spec, "frames": value.frames, "trans": value.trans,
                "start": value.start, "cutaways": cuts}
    return {**compiled, "segs": [segment(value) for value in compiled.get("segs", [])]}


def pointer(parts: list[str | int]) -> str:
    def escape(part: str | int) -> str:
        return str(part).replace("~", "~0").replace("/", "~1")
    return "/" + "/".join(escape(part) for part in parts)


def pointer_get(root: Any, path: str) -> Any:
    if not path:
        return root
    current = root
    for component in path.lstrip("/").split("/"):
        component = component.replace("~1", "/").replace("~0", "~")
        current = current[int(component)] if isinstance(current, list) else current[component]
    return current


def pointer_parent(root: Any, path: str) -> tuple[Any, str]:
    parent_path, _, key = path.rpartition("/")
    parent = pointer_get(root, parent_path)
    key = key.replace("~1", "/").replace("~0", "~")
    return parent, key


def _descriptors(plan: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    kinds = {"cutaways": "cutaway", "path": "camera_keyframe", "musicDrops": "music_drop",
             "captions": "caption_settings", "exhibits": "exhibit"}

    def add(value, parts, kind, parent):
        path = pointer(parts)
        out.append({"kind": kind, "path": path, "parentPath": parent, "raw": value})
        if not isinstance(value, dict):
            return
        for key in ("windows", "words", "keys", "path"):
            children = value.get(key)
            if not isinstance(children, list):
                continue
            child_kind = {"windows": f"{kind}_window", "words": f"{kind}_word",
                          "keys": f"{kind}_keyframe", "path": "camera_keyframe"}[key]
            for i, child in enumerate(children):
                add(child, parts + [key, i], child_kind, path)
        if kind == "cutaway":
            fields(value, parts, path, ("sphere", "path"))

    def fields(owner, parts, parent, names):
        for key in names:
            if key not in owner or owner[key] is None:
                continue
            value = owner[key]
            kind = kinds.get(key, key)
            if isinstance(value, list):
                for j, item in enumerate(value):
                    add(item, parts + [key, j], kind, parent)
            else:
                add(value, parts + [key], kind, parent)

    for i, segment in enumerate(plan.get("segments") or []):
        if not isinstance(segment, dict):
            continue
        segment_path = pointer(["segments", i])
        out.append({"kind": "segment", "path": segment_path, "parentPath": None, "raw": segment})
        fields(segment, ["segments", i], segment_path,
               ("cutaways", "text", "stats", "nat", "sfx", "bleep", "punch", "blur",
                "path", "sphere", "route", "progress", "exhibits", "captions"))
    fields(plan, [], None, ("text", "musicDrops", "sfx", "exhibits", "hook", "endCard",
                            "titleCard", "music", "punchOnBeats"))
    return out


def ensure_node_ids(plan: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Refresh stable IDs and the private ID-to-location index in ``_editor``.

    Exact fingerprints preserve identity across array reorder. The prior path is
    a fallback for edits to an existing node; callers never address a node by
    that path, only by its opaque UUID.
    """
    editor = plan.setdefault("_editor", {})
    editor.setdefault("version", 1)
    editor.setdefault("documentId", str(uuid.uuid4()))
    previous = editor.get("nodes")
    previous = previous if isinstance(previous, dict) else {}
    descs = _descriptors(plan)
    available = set(previous)
    matched: dict[int, str] = {}

    # Finish a parent level before matching children. Otherwise editing a
    # segment and reordering its overlays loses their exact-content matches.
    indexes = {d["path"]: i for i, d in enumerate(descs)}
    depths = {}
    for index, desc in enumerate(descs):
        parent = indexes.get(desc["parentPath"])
        depths[index] = depths.get(parent, -1) + 1
    for depth in sorted(set(depths.values())):
        group = [i for i in range(len(descs)) if depths[i] == depth]
        for mode in ("fingerprint", "locator"):
            for index in group:
                if index in matched:
                    continue
                desc = descs[index]
                parent_id = matched.get(indexes.get(desc["parentPath"]))
                value = fingerprint(desc["raw"]) if mode == "fingerprint" else desc["path"]
                candidates = [node_id for node_id in available
                              if previous[node_id].get("kind") == desc["kind"]
                              and previous[node_id].get(mode) == value
                              and previous[node_id].get("parentId") == parent_id]
                if len(candidates) == 1:
                    matched[index] = candidates[0]
                    available.remove(candidates[0])
        for index in group:
            matched.setdefault(index, str(uuid.uuid4()))

    refreshed: dict[str, dict[str, Any]] = {}
    path_to_id = {desc["path"]: matched[i] for i, desc in enumerate(descs)}
    for i, desc in enumerate(descs):
        node_id = matched[i]
        parent_id = path_to_id.get(desc["parentPath"]) if desc["parentPath"] else None
        refreshed[node_id] = {
            **previous.get(node_id, {}),
            "kind": desc["kind"],
            "parentId": parent_id,
            "locator": desc["path"],
            "fingerprint": fingerprint(desc["raw"]),
        }
    editor["nodes"] = refreshed
    return refreshed


def node_location(plan: dict[str, Any], node_id: str) -> tuple[dict[str, Any], Any]:
    nodes = ensure_node_ids(plan)
    entry = nodes.get(node_id)
    if not entry:
        raise KeyError(f"unknown node id: {node_id}")
    return entry, pointer_get(plan, entry["locator"])


def _make_node(node_id: str, kind: str, lane: str, parent_id: str | None, start: int, end: int,
               raw: Any, **extra: Any) -> dict[str, Any]:
    node = {
        "id": node_id, "kind": kind, "lane": lane, "parentId": parent_id,
        "startFrame": int(start), "endFrame": int(end), "frames": max(0, int(end) - int(start)),
        "raw": deepcopy(raw),
    }
    node.update(extra)
    return node


def compiled_nodes(plan: dict[str, Any], compiled: dict[str, Any]) -> list[dict[str, Any]]:
    """Project final renderer frames; raw intent is never passed off as timing.

    Provenance from the worker survives renderer collision handling. Older
    renderers use unique content/range matches; ambiguous output remains a
    separate generated node rather than attaching it to the wrong raw item.
    """
    meta = ensure_node_ids(plan)
    id_by_path = {entry["locator"]: node_id for node_id, entry in meta.items()}
    fps = int(compiled["fps"])
    segs = compiled.get("segs") or []
    total = int(compiled["total"])
    props = compiled.get("props") or {}
    sound = compiled.get("sound") or {}
    descriptors = _descriptors(plan)
    raw_nodes: dict[str, dict] = {}
    nodes: list[dict] = []
    lanes = {"segment": "primary", "cutaway": "cutaway", "text": "text", "hook": "text",
             "endCard": "text", "titleCard": "text", "sfx": "audio", "nat": "audio",
             "music": "audio", "music_drop": "audio", "caption_settings": "captions",
             "bleep": "audio", "bleep_window": "audio", "stats": "data",
             "route": "data", "progress": "data", "exhibit": "overlay"}
    for desc in descriptors:
        node_id = id_by_path[desc["path"]]
        node = _make_node(node_id, desc["kind"], lanes.get(desc["kind"], "effects"),
                          meta[node_id].get("parentId"), 0, 0, desc["raw"],
                          generated=False, editable=True, readOnly=False, timingState="unresolved")
        raw = desc["raw"]
        if isinstance(raw, dict) and raw.get("at") is not None:
            node["requestedAt"] = raw["at"]
            node["requestedFor"] = raw.get("for")
        raw_nodes[desc["path"]] = node
        nodes.append(node)

    def resolve(path, value, start=None, end=None, **extra):
        node = raw_nodes.get(path)
        if node is None:
            return False
        start = int(value.get("startFrame", 0) if start is None else start)
        end = int(value.get("endFrame", start + value.get("durationInFrames", 0)) if end is None else end)
        node.update(startFrame=start, endFrame=end, frames=max(0, end - start),
                    resolvedStartFrame=start, resolvedEndFrame=end,
                    resolved=deepcopy(value), timingState="resolved", **extra)
        return True

    generated_counts: dict[str, int] = {}

    def generated(kind, lane, value, parent=None, start=None, end=None, **extra):
        start = int(value.get("startFrame", 0) if start is None else start)
        end = int(value.get("endFrame", start + value.get("durationInFrames", 1)) if end is None else end)
        identity = canonical_json([kind, parent, value])
        occurrence = generated_counts.get(identity, 0)
        generated_counts[identity] = occurrence + 1
        node_id = str(uuid.uuid5(_ID_NAMESPACE, f"{plan['_editor']['documentId']}:{identity}:{occurrence}"))
        node = _make_node(node_id, kind, lane, parent, start, end, value, generated=True,
                          editable=kind == "generated_sfx", resolved=deepcopy(value),
                          readOnly=kind != "generated_sfx",
                          timingState="resolved", **extra)
        nodes.append(node)
        return node

    def match(kind, value, *, prefix=None, text_field=None):
        provenance = value.get("_editorPath")
        if provenance in raw_nodes and raw_nodes[provenance]["kind"] == kind:
            return provenance
        candidates = [d for d in descriptors if d["kind"] == kind
                      and (prefix is None or d["path"].startswith(prefix))
                      and raw_nodes[d["path"]]["timingState"] != "resolved"]
        if kind in ("progress", "route", "stats", "exhibit") and "startFrame" in value:
            scoped = []
            for desc in candidates:
                parts = desc["path"].split("/")
                if len(parts) > 2 and parts[1] == "segments":
                    index = int(parts[2])
                    if index >= len(segs):
                        continue
                    seg = segs[index]
                    if not seg["start"] <= value["startFrame"] < seg["start"] + seg["frames"]:
                        continue
                scoped.append(desc)
            candidates = scoped
        if text_field:
            text = str(value.get(text_field, "")).strip()
            candidates = [d for d in candidates if
                          str(d["raw"].get(text_field, "") if isinstance(d["raw"], dict)
                              else d["raw"]).strip() == text]
        if len(candidates) == 1:
            return candidates[0]["path"]
        # Exact requested start disambiguates repeated content. Do not choose
        # "nearest": collision handling can move or drop any of those entries.
        exact = []
        for desc in candidates:
            raw = desc["raw"]
            if not isinstance(raw, dict):
                continue
            base = 0
            parts = desc["path"].split("/")
            if len(parts) > 2 and parts[1] == "segments" and int(parts[2]) < len(segs):
                base = int(segs[int(parts[2])]["start"])
            default = 0.15 if kind == "text" and base else 0
            if base + seconds_to_frames(float(raw.get("at", default) or 0), fps) == value.get("startFrame"):
                exact.append(desc["path"])
        return exact[0] if len(exact) == 1 else None

    for i, seg in enumerate(segs):
        if i >= len(plan.get("segments") or []):
            continue
        raw = plan["segments"][i]
        path = pointer(["segments", i])
        node_id = id_by_path[path]
        start = int(seg["start"])
        end = start + int(seg["frames"])
        resolve(path, seg["spec"], start, end, assetId=seg["asset"]["id"], sourceIn=seg["spec"].get("in"),
            sourceOut=seg["spec"].get("out"), speed=seg["spec"].get("speed", 1),
            transitionInFrames=int(seg.get("trans", 0)), mediaKind=seg.get("kind"),
            assetKind=seg.get("kind"))
        for cut in seg.get("cutaways") or []:
            cut_raws = raw.get("cutaways") or []
            j = cut.get("rawIndex")
            if j is None:
                candidates = [k for k, item in enumerate(cut_raws)
                              if seconds_to_frames(item.get("at", 0), fps) == cut["start"]
                              and str(cut.get("assetId", "")).startswith(str(item.get("asset", "")))]
                j = candidates[0] if len(candidates) == 1 else None
            if j is None or j >= len(cut_raws):
                generated("generated_cutaway", "cutaway", cut, node_id, start + cut["start"],
                          start + cut["start"] + cut["frames"],
                          assetId=cut.get("assetId"), assetKind=cut.get("kind"))
                continue
            cut_path = pointer(["segments", i, "cutaways", j])
            cut_start = start + int(cut["start"])
            spec = cut.get("spec") or {}
            resolve(cut_path, spec, cut_start, cut_start + int(cut["frames"]), assetId=cut.get("assetId"),
                    sourceIn=spec.get("in"), sourceOut=spec.get("out"), speed=spec.get("speed", 1),
                    sourceDuration=cut.get("sourceDuration"), mediaKind=cut.get("kind", "video"),
                    assetKind=cut.get("kind", "video"))
            for key in (spec.get("sphere") or {}).get("path") or []:
                for desc in descriptors:
                    if (desc["kind"] == "camera_keyframe" and desc["path"].startswith(cut_path + "/")
                            and isinstance(desc["raw"], dict) and desc["raw"].get("at") == key.get("at")):
                        frame = cut_start + seconds_to_frames(key["at"], fps)
                        resolve(desc["path"], key, frame, frame)
        if path + "/captions" in raw_nodes:
            resolve(path + "/captions", {"mode": seg["spec"].get("captions")}, start, end)
        speed = float(seg["spec"].get("speed", 1))
        for a, b in seg["spec"].get("_bleeps") or []:
            left = start + seconds_to_frames((a - seg["spec"]["in"]) / speed, fps)
            right = start + seconds_to_frames((b - seg["spec"]["in"]) / speed, fps)
            generated("generated_bleep", "audio", {"sourceIn": a, "sourceOut": b},
                      id_by_path.get(path + "/bleep", node_id), left, right, assetId=seg["asset"]["id"])
            for desc in descriptors:
                if desc["path"].startswith(path + "/bleep") and isinstance(desc["raw"], dict):
                    win = desc["raw"]
                    if "at" in win:
                        at = start + seconds_to_frames(win["at"], fps)
                        if left <= at < right:
                            resolve(desc["path"], {"sourceIn": a, "sourceOut": b}, left, right)
        for punch in seg["spec"].get("_punches") or []:
            value = {**punch, "startFrame": punch["start"], "endFrame": punch.get("end") or end}
            target = match("punch", value, prefix=path + "/punch")
            # Word/beat selectors can produce multiple windows; keep them
            # generated rather than collapsing the selector to its first hit.
            if target and isinstance(pointer_get(plan, target), dict) and "at" in pointer_get(plan, target):
                resolve(target, value)
            else:
                generated("generated_punch", "effects", value, id_by_path.get(path + "/punch", node_id),
                          assetId=seg["asset"]["id"])
        segment_props = (props.get("segments") or [])
        visual = segment_props[i] if i < len(segment_props) else {}
        for j, patch in enumerate(visual.get("blur") or []):
            keys = patch.get("keys") or []
            if not keys:
                continue
            blur_path = path + f"/blur/{j}"
            resolve(blur_path, patch, start + int(keys[0][0]), start + int(keys[-1][0]))
            for desc in descriptors:
                if desc["path"].startswith(blur_path + "/keys/") and isinstance(desc["raw"], dict):
                    frame = min(seg["frames"], seconds_to_frames(desc["raw"]["at"], fps))
                    key = next((k for k in keys if k[0] == frame), None)
                    if key:
                        resolve(desc["path"], {"key": key}, start + frame, start + frame)
        for key in (seg["spec"].get("sphere") or {}).get("path") or []:
            if not isinstance(key, dict) or "at" not in key:
                continue
            frame = start + seconds_to_frames(key["at"], fps)
            for desc in descriptors:
                if (desc["kind"] == "camera_keyframe" and desc["path"].startswith(path + "/")
                        and isinstance(desc["raw"], dict) and desc["raw"].get("at") == key["at"]):
                    resolve(desc["path"], key, frame, frame)

    for kind, field in (("text", "beats"), ("stats", "stats"), ("exhibit", "exhibits"),
                        ("progress", "progress"), ("route", "routes")):
        for value in props.get(field) or []:
            if kind == "route":
                mapped = False
                for i, seg in enumerate(segs):
                    left, right = max(seg["start"], value["startFrame"]), min(
                        seg["start"] + seg["frames"], value["endFrame"])
                    if right > left and resolve(f"/segments/{i}/route", value, left, right):
                        mapped = True
                if mapped:
                    continue
            target = match(kind, value, text_field="text" if kind == "text" else None)
            if target and resolve(target, value):
                continue
            generated("generated_" + kind, lanes.get(kind, "overlay"), value)
    for field in ("hook", "endCard", "titleCard", "music"):
        value = props.get(field)
        if value:
            value = {**value}
            if field == "music":
                value.update(startFrame=0, endFrame=total)
            if not resolve("/" + field, value):
                generated("generated_" + field, lanes.get(field, "overlay"), value)
    for page in props.get("captions") or []:
        owner = next((i for i, sg in enumerate(segs)
                      if sg["start"] <= page["startFrame"] < sg["start"] + sg["frames"]), None)
        parent = id_by_path.get(f"/segments/{owner}/captions", id_by_path.get(f"/segments/{owner}"))
        asset_id = segs[owner]["asset"]["id"] if owner is not None else None
        page_node = generated("caption_page", "captions", page, parent, assetId=asset_id)
        for word in page.get("words") or []:
            generated("caption_word", "captions", word, page_node["id"], assetId=asset_id)
    for value in props.get("foregrounds") or []:
        generated("generated_foreground", "overlay", value)
    for kind, field in (("nat", "nat"), ("music_drop", "drops")):
        for value in sound.get(field) or []:
            prefix = f"/segments/{value['seg']}/nat" if kind == "nat" and "seg" in value else None
            target = match(kind, value, prefix=prefix)
            if target:
                resolve(target, value)
            else:
                generated("generated_" + kind, "audio", value)
    for cue in sound.get("cues") or []:
        source = str(cue.get("source") or "")
        if source.startswith("auto:"):
            generated("generated_sfx", "audio", cue, event=source.removeprefix("auto:").strip())
        else:
            parts = re.fullmatch(r"segment (\d+)", source)
            prefix = f"/segments/{int(parts[1]) - 1}/sfx" if parts else "/sfx"
            target = match("sfx", cue, prefix=prefix, text_field="sound")
            if target:
                resolve(target, cue)
            else:
                generated("resolved_sfx", "audio", cue)
    # Explicit dropped states only when the compiler actually returned the
    # relevant output collection. Raw settings/keyframes remain addressable.
    authoritative = {"text": "beats", "stats": "stats", "exhibit": "exhibits",
                     "hook": "hook", "endCard": "endCard", "titleCard": "titleCard", "music": "music"}
    for node in nodes:
        if node["timingState"] == "unresolved" and authoritative.get(node["kind"]) in props:
            node["timingState"] = "dropped"
    return nodes


def editing_snapshot(plan: dict[str, Any], compiled: dict[str, Any], revisions: dict[str, str]) -> dict[str, Any]:
    """Wire-compatible renderer snapshot; raw plan is a separate deep copy."""
    from .tracks import annotate_nodes, track_list
    nodes = annotate_nodes(plan, compiled_nodes(plan, compiled))
    preview_ref = plan.get("_editor", {}).get("rendererPreview")
    if isinstance(preview_ref, dict):
        preview = deepcopy(preview_ref)
        preview["state"] = (
            "ready" if preview.get("rendererRevision") == revisions["renderer"]
            and preview.get("documentRevision") == revisions["document"]
            else "stale"
        )
        preview["revision"] = revisions["document"]
    else:
        preview = {"revision": revisions["document"], "state": "not_prepared"}
    return {
        "documentId": plan.get("_editor", {}).get("documentId"),
        "revisions": deepcopy(revisions),
        "plan": deepcopy(plan),
        "nodes": nodes,
        "tracks": track_list(plan.get("_editor")),
        "fps": int(compiled["fps"]),
        "durationFrames": int(compiled["total"]),
        "warnings": list(compiled.get("warnings") or []),
        "preview": preview,
    }
