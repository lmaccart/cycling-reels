"""Atomic non-ripple footage edits; raw source files are never modified."""
from __future__ import annotations

from copy import deepcopy
import math

from ..timeline import deterministic_id, edit_id, integer, positioned, video_track
from .document import _descriptors, ensure_node_ids, fingerprint, pointer_get
from .timing import slice_local_events
from .tracks import effective_track, track_list


def _bounds(clip):
    value = clip["timeline"]
    return value["startFrame"], value["startFrame"] + value["durationFrames"]


_IDENTITY = "__timelineEditorId"


def video_source_bounds(asset, source_in, source_out, speed=1):
    """Validate editor source intent before renderer normalization can clamp it."""
    source_in, source_out, speed = float(source_in), float(source_out), float(speed)
    duration = float(asset["probe"]["duration"])
    if (not all(math.isfinite(value) for value in (source_in, source_out, speed, duration))
            or not 0 <= source_in < source_out <= duration or not .25 <= speed <= 4):
        raise ValueError("video source bounds must be finite and within the original source duration")
    return source_in, source_out, speed


def _slice(clip, left, right, fps, old_objects=None, project=None, old_nodes=None, compiled=None, *,
           piece_bounds=None):
    start, end = _bounds(clip)
    a, b = (left - start) / fps, (right - start) / fps
    memo = {}
    marked = deepcopy(clip, memo)
    for key, original in (old_objects or {}).items():
        cloned = memo.get(id(original))
        if isinstance(cloned, dict):
            cloned[_IDENTITY] = key
    root_id = next((key for key, original in (old_objects or {}).items() if original is clip), None)
    if isinstance(marked.get("text"), list):
        root_path = old_nodes[root_id]["locator"] if root_id and old_nodes else None
        for index, text in enumerate(marked["text"]):
            if isinstance(text, str):
                child_id = next((key for key, entry in (old_nodes or {}).items()
                                 if root_path and entry["locator"] == root_path + f"/text/{index}"), None)
                marked["text"][index] = {"text": text, "at": .15, "for": 2.2,
                                          **({_IDENTITY: child_id} if child_id else {})}
            elif isinstance(text, dict):
                # Renderer defaults describe an original local interval, not a
                # new interval to restart independently on each retained piece.
                text.setdefault("at", .15)
                text.setdefault("for", 2.2)
    resolved = None
    if compiled is not None and root_id:
        index = int(old_nodes[root_id]["locator"].split("/")[2])
        resolved = compiled["segs"][index]
    if piece_bounds is None:
        if "in" in clip:
            speed = float(clip.get("speed", 1))
            source_in, source_out = float(clip["in"]), float(clip["out"])
            piece_bounds = {
                "in": source_in if left == start else min(source_out, max(source_in, source_in + a * speed)),
                "out": source_out if right == end else min(source_out, max(source_in, source_in + b * speed)),
            }
        else:
            piece_bounds = {"duration": (right - left) / fps}
    value = slice_local_events(marked, a, b, piece_bounds=piece_bounds, rebase=True,
                               minimum_cutaway_frames=1, fps=fps, resolved=resolved)
    value.pop(_IDENTITY, None)
    if "in" not in clip:
        window = clip.get("motionWindow") or {"offsetSeconds": 0, "durationSeconds": clip["timeline"]["durationFrames"] / fps}
        value["motionWindow"] = {**window, "offsetSeconds": window["offsetSeconds"] + a}
    if project is not None and isinstance(marked.get("sfx"), list):
        from ..sfx import resolve_sfx
        cues = []
        for cue in marked["sfx"]:
            if not isinstance(cue, dict) or not cue.get("sound"):
                continue
            _, record = resolve_sfx(project, cue["sound"])
            duration = float(cue.get("_timelineDuration", record.get("duration") or .5))
            at = float(cue["at"]) if "at" in cue else float(cue["endAt"]) - duration
            lo, hi = max(at, a), min(at + duration, b)
            if hi > lo:
                result = {**cue, "at": lo-a,
                          "_timelineOffset": float(cue.get("_timelineOffset", 0)) + lo-at,
                          "_timelineDuration": hi-lo}
                result.pop("endAt", None)
                cues.append(result)
        value["sfx"] = cues
    value.pop("moment", None)
    # A cut inside an incoming fade can leave a head entirely covered by that
    # fade; its overlap is then exactly the surviving head's full duration.
    transition = min(int(clip["timeline"].get("transitionInFrames", 0)), right-left) if left == start else 0
    value["timeline"] = {**clip["timeline"], "startFrame": left, "durationFrames": right-left,
                         "transitionInFrames": max(0, transition)}
    value["transition"] = clip.get("transition", "cut") if transition > 0 else "cut"
    return value


def _subtract(left, right, intervals):
    spans = [(left, right)]
    for a, b in intervals:
        spans = [(x, min(y, a)) for x, y in spans if x < min(y, a)] + [
            (max(x, b), y) for x, y in spans if max(x, b) < y]
    return sorted(spans)


def materialize(plan, compiled):
    explicit = positioned(plan)
    fps = int(compiled["fps"])
    for raw, segment in zip(plan.get("segments") or [], compiled["segs"]):
        spec = segment["spec"]
        if segment["kind"] == "video":
            for field in ("in", "out", "speed"):
                raw[field] = deepcopy(spec[field])
            raw.pop("moment", None)
        else:
            raw["duration"] = segment["frames"] / fps
        if not explicit:
            raw["timeline"] = {
                "track": "V1", "startFrame": segment["start"], "durationFrames": segment["frames"],
                "transitionInFrames": segment.get("trans", 0),
            }
    if not explicit:
        plan["timeline"] = {"version": 1, "fps": fps, "durationFrames": max(1, compiled["total"])}


def apply_overwrite(ctx, operation, project):
    plan, compiled = ctx["plan"], ctx["compiled"]
    edit = edit_id(operation.get("editId"))
    if operation.get("mode") != "overwrite":
        raise ValueError("only non-ripple overwrite is supported")
    placements = operation.get("placements")
    if not isinstance(placements, list) or not 1 <= len(placements) <= 500:
        raise ValueError("placements must contain 1–500 items")
    for placement in placements:
        if not isinstance(placement, dict) or set(placement) - {"id", "insert", "track", "atFrame"}:
            raise ValueError("invalid placement fields")
        if ("id" in placement) == ("insert" in placement):
            raise ValueError("a placement needs exactly one of id or insert")
        if "id" in placement and (
                not isinstance(placement["id"], str) or not placement["id"]
                or "\0" in placement["id"]):
            raise ValueError("footage IDs must be non-empty strings without NUL")
    fps = int(compiled["fps"])
    old_nodes = deepcopy(ensure_node_ids(plan))
    old_objects = {key: pointer_get(plan, item["locator"]) for key, item in old_nodes.items()}
    rows = {item["id"]: item for item in track_list(plan.get("_editor"), plan=plan)}
    locked = {key for key, item in rows.items() if item["locked"]}
    old_tracks = {key: effective_track(plan, key) for key in old_nodes}

    def assert_unlocked(node_id):
        own_path = old_nodes[node_id]["locator"]
        for child, entry in old_nodes.items():
            if child == node_id or entry["locator"].startswith(own_path + "/"):
                # Old cosmetic locks remain protective during migration.
                if old_tracks[child] in locked:
                    raise ValueError(f"Track {old_tracks[child]} is locked")

    def target_track(value):
        video_track(value)
        if value not in rows or rows[value]["kind"] != "video":
            raise ValueError(f"unknown destination track: {value}")
        if value in locked:
            raise ValueError(f"Track {value} is locked")
        return value

    seen = set()
    prepared = []
    # Validate source IDs and locks before materializing anything.
    for index, placement in enumerate(placements):
        track = target_track(placement.get("track"))
        at = integer(placement.get("atFrame"), "atFrame")
        node_id = placement.get("id")
        if node_id is not None:
            if not isinstance(node_id, str) or node_id not in old_nodes or node_id in seen:
                raise ValueError("unknown or repeated footage ID")
            if old_nodes[node_id]["kind"] not in ("segment", "cutaway"):
                raise ValueError("only footage can be placed")
            assert_unlocked(node_id)
            seen.add(node_id)
        prepared.append((index, placement, node_id, track, at))
    materialize(plan, compiled)
    # Bind all existing roots/children to object identity; cloned pieces receive
    # exact deterministic IDs below instead of ensure_node_ids' UUID fallback.
    bindings = {id(value): key for key, value in old_objects.items() if isinstance(value, (dict, list))}
    incoming, sources = [], set()
    for index, placement, node_id, track, at in prepared:
        if node_id is not None:
            original = old_objects[node_id]
            if old_nodes[node_id]["kind"] == "cutaway":
                parent_id = old_nodes[node_id]["parentId"]
                assert_unlocked(parent_id)
                parent = old_objects[parent_id]
                if parent["timeline"]["track"] in locked:
                    raise ValueError("source track is locked")
                pindex = int(old_nodes[parent_id]["locator"].split("/")[2])
                cindex = int(old_nodes[node_id]["locator"].split("/")[-1])
                cut = next((value for value in compiled["segs"][pindex].get("cutaways") or []
                            if value.get("rawIndex") == cindex), None)
                if cut is None:
                    raise ValueError("cutaway has no resolved source")
                memo = {}
                clip = deepcopy(original, memo)
                for child, value in old_objects.items():
                    copied = memo.get(id(value))
                    if isinstance(copied, (dict, list)):
                        bindings[id(copied)] = child
                clip.update({key: deepcopy(value) for key, value in cut["spec"].items()
                             if not key.startswith("_")})
                clip.pop("at", None)
                clip.update(volume=0, captions="none", transition="cut")
                frames = int(cut["frames"])
                parent["cutaways"] = [value for value in parent["cutaways"] if value is not original]
                bindings[id(clip)] = node_id
            else:
                clip = original
                if clip["timeline"]["track"] in locked:
                    raise ValueError("source track is locked")
                frames = clip["timeline"]["durationFrames"]
                sources.add(id(clip))
            ctx["affected"].add(node_id)
        else:
            clip = deepcopy(placement["insert"])
            if (not isinstance(clip, dict) or not isinstance(clip.get("asset"), str)
                    or not clip["asset"] or "\0" in clip["asset"] or any(
                    key in clip for key in ("timeline", "_editor"))):
                raise ValueError("insert must be a renderer segment without timeline/_editor metadata")
            from ..library import load_asset, resolve_id, resolve_moment
            asset = load_asset(project, resolve_id(project, str(clip["asset"])))
            if "moment" in clip:
                moment = resolve_moment(asset, clip["moment"])
                clip.setdefault("in", moment["in"])
                clip.setdefault("out", moment["out"])
            if asset["kind"] == "video":
                if "in" not in clip or "out" not in clip:
                    raise ValueError("inserted video needs valid in/out and speed")
                source_in, source_out, speed = video_source_bounds(
                    asset, clip["in"], clip["out"], clip.get("speed", 1))
                frames = round((source_out - source_in) * fps / speed)
            else:
                frames = round(float(clip.get("duration", 2.5)) * fps)
            if frames < 1:
                raise ValueError("inserted footage must occupy at least one frame")
            node_id = deterministic_id("clip-", edit, f"insert:{index}", at, at + frames)
            if node_id in old_nodes:
                raise ValueError("editId would reuse an existing inserted clip ID")
            bindings[id(clip)] = node_id
            ctx["affected"].add(node_id)
        clip["timeline"] = {"track": track, "startFrame": at, "durationFrames": frames,
                            "transitionInFrames": 0}
        clip["transition"] = "cut"
        incoming.append(clip)
    # Incoming intervals must not overwrite one another. Victims are calculated
    # against the original, not against partially applied earlier placements.
    intervals = {}
    for clip in incoming:
        a, b = _bounds(clip)
        spans = intervals.setdefault(clip["timeline"]["track"], [])
        if any(a < y and x < b for x, y in spans):
            raise ValueError("incoming footage overlaps another selected clip on the destination track")
        spans.append((a, b))
    results = []
    for clip in plan.get("segments") or []:
        if id(clip) in sources:
            continue
        a, b = _bounds(clip)
        spans = _subtract(a, b, intervals.get(clip["timeline"]["track"], []))
        if spans == [(a, b)]:
            results.append(clip)
            continue
        node_id = bindings[id(clip)]
        assert_unlocked(node_id)
        if clip["timeline"]["track"] in locked:
            raise ValueError("affected destination track is locked")
        ctx["affected"].add(node_id)
        used_children = set()
        for number, (left, right) in enumerate(spans):
            piece = _slice(clip, left, right, fps, old_objects, project, old_nodes, compiled)
            piece_id = node_id if number == 0 else deterministic_id("frag-", edit, node_id, left, right)
            bindings[id(piece)] = (piece_id, node_id)
            # Match child content after slicing by collection/order, retaining
            # a child's ID on its first surviving occurrence.
            local_plan = {"segments": [piece]}
            descriptors = _descriptors(local_plan)
            assigned_here = set()
            for descriptor in descriptors[1:]:
                value = descriptor["raw"]
                child = value.pop(_IDENTITY, None) if isinstance(value, dict) else None
                if child is None:
                    continue
                child_id = child if child not in used_children else deterministic_id(
                    "node-", edit, child, left, right)
                if child_id in assigned_here:
                    child_id = deterministic_id("node-", edit, child + ":" + descriptor["path"], left, right)
                assigned_here.add(child_id)
                used_children.add(child)
                bindings[id(value)] = (child_id, child)
            results.append(piece)
            ctx["affected"].add(piece_id)
    results.extend(incoming)
    plan["segments"] = results
    plan["timeline"]["durationFrames"] = max(plan["timeline"]["durationFrames"],
        max((_bounds(clip)[1] for clip in results if clip.get("enabled") is not False), default=0))
    finish_index(plan, old_nodes, old_objects, bindings, edit)
    ctx["overwriteMetadata"] = True


def finish_index(plan, old_nodes, old_objects, bindings, edit):
    # Rebuild the complete index by object identity, preserving track lock rows.
    def clean(value):
        if isinstance(value, dict):
            value.pop(_IDENTITY, None)
            for child in value.values():
                clean(child)
        elif isinstance(value, list):
            for child in value:
                clean(child)
    clean(plan)
    metadata = {}
    paths = {}
    origins = {}
    for descriptor in _descriptors(plan):
        value = descriptor["raw"]
        binding = bindings.get(id(value))
        key, origin = binding if isinstance(binding, tuple) else (binding, binding)
        if key is None and not isinstance(value, (dict, list)):
            parent_key = paths.get(descriptor["parentPath"])
            original_parent_key = origins.get(parent_key, parent_key)
            old_parent = old_nodes.get(original_parent_key)
            if old_parent is not None:
                suffix = descriptor["path"][len(descriptor["parentPath"]):]
                candidates = [node_id for node_id, entry in old_nodes.items()
                              if entry.get("parentId") == original_parent_key
                              and entry["locator"] == old_parent["locator"] + suffix]
                if len(candidates) == 1:
                    origin = candidates[0]
                    key = origin
                    if key in metadata:
                        root = plan["segments"][int(descriptor["path"].split("/")[2])]
                        key = deterministic_id("node-", edit, origin, *_bounds(root))
            elif descriptor["parentPath"] is None:
                candidates = [node_id for node_id, entry in old_nodes.items()
                              if entry["locator"] == descriptor["path"] and entry["kind"] == descriptor["kind"]
                              and fingerprint(value) == entry["fingerprint"] and node_id not in metadata]
                key = candidates[0] if len(candidates) == 1 else None
        if key is None:
            key = deterministic_id("node-", edit, descriptor["path"], 0, 0)
        if key in metadata:
            raise ValueError("deterministic node identity collision")
        paths[descriptor["path"]] = key
        origins[key] = origin or key
        metadata[key] = {**old_nodes.get(origin or key, {}), "kind": descriptor["kind"],
                         "parentId": paths.get(descriptor["parentPath"]),
                         "locator": descriptor["path"], "fingerprint": fingerprint(value)}
        if descriptor["kind"] == "segment":
            metadata[key]["track"] = value["timeline"]["track"]
    plan["_editor"]["nodes"] = metadata


def positioned_source_edit(ctx, operation, project, revisions):
    """Non-ripple trim/split with deterministic IDs, including worker replay."""
    from uuid import NAMESPACE_URL, uuid5
    from .document import canonical_json
    from .tracks import locked_target
    plan = ctx["plan"]
    op = operation["op"]
    if op not in ("trim_segment", "split_segment"):
        return False
    node_id = operation["id"]
    nodes = deepcopy(ensure_node_ids(plan))
    entry = nodes.get(node_id)
    if entry is None or entry["kind"] != "segment":
        return False
    objects = {key: pointer_get(plan, item["locator"]) for key, item in nodes.items()}
    for key, child in nodes.items():
        if key == node_id or child["locator"].startswith(entry["locator"] + "/"):
            message = locked_target(plan, {"id": key})
            if message:
                raise ValueError(message)
    bindings = {id(value): key for key, value in objects.items() if isinstance(value, (dict, list))}
    edit = operation.get("editId") or str(uuid5(NAMESPACE_URL, canonical_json({
        "document": revisions["document"], "operation": operation,
    })))
    clip = objects[node_id]
    a, b = _bounds(clip)
    fps = ctx["fps"]
    if op == "split_segment":
        at = integer(operation["atFrame"], "atFrame")
        if not a < at < b:
            raise ValueError("split must be inside the clip")
        spans = [(a, at), (at, b)]
        pieces = []
        used_children = set()
        for number, (left, right) in enumerate(spans):
            piece = _slice(clip, left, right, fps, objects, project, nodes, ctx["compiled"])
            key = node_id if number == 0 else deterministic_id("frag-", edit, node_id, left, right)
            bindings[id(piece)] = (key, node_id)
            assigned_here = set()
            for desc in _descriptors({"segments": [piece]})[1:]:
                value = desc["raw"]
                child = value.pop(_IDENTITY, None) if isinstance(value, dict) else None
                if child:
                    child_id = child if child not in used_children else deterministic_id("node-", edit, child, left, right)
                    if child_id in assigned_here:
                        child_id = deterministic_id("node-", edit, child + ":" + desc["path"], left, right)
                    assigned_here.add(child_id)
                    used_children.add(child)
                    bindings[id(value)] = (child_id, child)
            pieces.append(piece)
            ctx["affected"].add(key)
        index = next(index for index, value in enumerate(plan["segments"]) if value is clip)
        plan["segments"][index:index+1] = pieces
    else:
        if "in" not in clip:
            raise ValueError("photos use duration, not source In/Out")
        from ..library import load_asset, resolve_id
        asset = load_asset(project, resolve_id(project, clip["asset"]))
        source_in, source_out, speed = video_source_bounds(
            asset, operation["sourceIn"], operation["sourceOut"], clip.get("speed", 1))
        frames = max(1, round((source_out-source_in) * fps / speed))
        # Use the ID-preserving slicer with the exact final source bounds,
        # including extensions already checked against the original source.
        synthetic_left = a + (source_in - float(clip["in"])) / speed * fps
        piece = _slice(clip, synthetic_left, synthetic_left + frames, fps, objects, project, nodes, ctx["compiled"],
                       piece_bounds={"in": source_in, "out": source_out})
        piece["timeline"].update(startFrame=a, durationFrames=frames)
        bindings[id(piece)] = node_id
        for desc in _descriptors({"segments": [piece]})[1:]:
            value = desc["raw"]
            if isinstance(value, dict):
                child = value.pop(_IDENTITY, None)
                if child:
                    bindings[id(value)] = child
        plan["segments"][plan["segments"].index(clip)] = piece
        ctx["affected"].add(node_id)
    plan["timeline"]["durationFrames"] = max(plan["timeline"]["durationFrames"],
        max((_bounds(value)[1] for value in plan["segments"] if value.get("enabled") is not False), default=0))
    finish_index(plan, nodes, objects, bindings, edit)
    ctx["overwriteMetadata"] = True
    return True
