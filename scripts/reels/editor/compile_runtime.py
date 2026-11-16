"""Trusted warm compiler runner. Executed by path inside sandbox-exec.

This module deliberately has no package imports at module load time: the broker
selects a recorded renderer's package root before importing any renderer code.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import stat
import sys
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = 1
INPUT_DIRS = (
    "library/assets", "library/transcripts", "music", "exhibits", "sfx",
    "library/sounds", "library/telemetry", "strava", "learning", "models",
)
INPUT_FILES = ("config.json", "library/progress.json", "progress.json")


def signature(path: Path) -> tuple[int, ...]:
    return stat_signature(path.stat(follow_symlinks=False))


def stat_signature(info) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def private_path(root: Path, relative: str) -> Path:
    """No absolute paths, dot components, or symlink traversal in worker writes."""
    if root.is_symlink():
        raise ValueError("scratch root is a symlink")
    parts = relative.split("/")
    if not relative or any(part in ("", ".", "..") for part in parts):
        raise ValueError("invalid compiler override path")
    target = root.joinpath(*parts)
    if target.is_absolute() and not target.is_relative_to(root):
        raise ValueError("compiler override escapes scratch")
    for candidate in (target, *target.parents):
        if candidate == root:
            break
        if candidate.is_symlink():
            raise ValueError("compiler override traverses a symlink")
    return target


class InputMirror:
    """Incrementally copy inputs, restoring request-local mutations/overrides.

    Source and destination identities are checked. A renderer modifying a copied
    input cannot make its changed contents become the next request's baseline.
    Only paths below ``private`` are ever written or removed.
    """

    def __init__(self, source: Path, private: Path, *, media_root: Path | None = None):
        self.source = source.resolve()
        self.private = private.resolve()
        if self.private == self.source or self.source.is_relative_to(self.private):
            raise ValueError("scratch must not contain the source workspace")
        self.private.mkdir(parents=True, exist_ok=True)
        self.copied: dict[str, tuple[tuple[int, ...], tuple[int, ...]]] = {}
        self.overridden: set[str] = set()
        self.media_root = media_root
        self.source_signatures: dict[str, tuple[int, ...]] = {}

    def _inputs(self) -> tuple[dict[str, Path], dict[str, tuple[int, ...]]]:
        found: dict[str, Path] = {}
        observed = {}
        for relative in INPUT_DIRS:
            root = private_path(self.source, relative)
            if not root.is_dir():
                continue
            pending = [(root, relative)]
            while pending:
                directory, prefix = pending.pop()
                with os.scandir(directory) as entries:
                    for entry in entries:
                        key = prefix + "/" + entry.name
                        if entry.is_symlink():
                            raise ValueError(f"compiler input is a symlink: {key}")
                        if entry.is_dir(follow_symlinks=False):
                            pending.append((entry.path, key))
                            continue
                        info = entry.stat(follow_symlinks=False)
                        if stat.S_ISREG(info.st_mode):
                            found[key], observed[key] = Path(entry.path), stat_signature(info)
        for relative in INPUT_FILES:
            path = private_path(self.source, relative)
            if path.is_file():
                found[relative] = path
                observed[relative] = signature(path)
        return found, observed

    def sync(self, expected: dict | None = None) -> None:
        sources, observed = self._inputs()
        if expected is not None:
            # These signatures belong to the exact expected library revision,
            # not merely to a later watcher scan. A stale broker generation
            # cannot be silently paired with newer scratch inputs.
            from reels.editor.workspace import _safe_name
            watched = {
                relative: list(observed[relative]) for relative in sources
                if (relative in INPUT_FILES or any(relative.startswith(root + "/") for root in INPUT_DIRS
                    if root not in ("learning", "models"))) and _safe_name(Path(relative))
            }
            wanted = {relative: list(value) for relative, value in expected.items()}
            if watched != wanted:
                raise ValueError("library inputs do not match the expected revision")
        destinations = self._prune(sources)
        self.copied = {key: value for key, value in self.copied.items() if key in sources}
        for relative, source in sources.items():
            before = observed[relative]
            # _prune has already checked every existing destination ancestor
            # and rejected links. Source-relative names were enumerated, never
            # supplied by the request, so do not repeat per-ancestor lstat here.
            target = self.private / relative
            previous = self.copied.get(relative)
            if (relative not in self.overridden and previous is not None
                    and previous[0] == before and destinations.get(relative) == previous[1]):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            if signature(source) != before:
                raise ValueError(f"compiler input changed while copying: {relative}")
            self.copied[relative] = (before, signature(target))
        self.overridden.clear()
        self.source_signatures = observed
        # Ordinary fixture Projects resolve media under scratch. Live projects
        # instead pass the explicitly registered media root to Project.
        media = self.private / "library/media"
        source_media = self.source / "library/media"
        if self.media_root is None and source_media.is_dir() and not media.exists():
            media.parent.mkdir(parents=True, exist_ok=True)
            media.symlink_to(source_media.resolve(), target_is_directory=True)

    def _prune(self, sources: dict[str, Path]) -> dict[str, tuple[int, ...]]:
        """Discard renderer-created inputs/caches, never follow their links."""
        if self.private.is_symlink() or not self.private.is_dir():
            raise ValueError("renderer replaced the compiler scratch root")
        directories = {"library"} if self.media_root is None else set()
        for relative in sources:
            parent = relative.rpartition("/")[0]
            while parent and parent not in directories:
                directories.add(parent)
                parent = parent.rpartition("/")[0]
        destinations = {}
        pending = [(self.private, "")]
        while pending:
            directory, prefix = pending.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    relative = prefix + entry.name
                    if (relative == "library/media" and self.media_root is None
                            and entry.is_symlink()
                            and Path(entry.path).resolve() == (self.source / "library/media").resolve()):
                        continue
                    if entry.is_symlink():
                        raise ValueError("renderer introduced a symlink into compiler inputs")
                    if entry.is_dir(follow_symlinks=False):
                        if relative in directories:
                            pending.append((entry.path, relative + "/"))
                        else:
                            shutil.rmtree(entry.path)
                    elif relative not in sources:
                        Path(entry.path).unlink()
                    else:
                        info = entry.stat(follow_symlinks=False)
                        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                            raise ValueError("renderer introduced a hardlink or special file into compiler inputs")
                        destinations[relative] = stat_signature(info)
        return destinations

    def verify(self) -> None:
        """Do not publish a result if source inputs moved during validation."""
        if self.private.is_symlink() or not self.private.is_dir():
            raise ValueError("renderer replaced the compiler scratch root")
        _, observed = self._inputs()
        if observed != self.source_signatures:
            raise ValueError("library inputs changed during validation")

    def apply(self, overrides: dict[str, Any]) -> None:
        for relative, value in overrides.items():
            if relative.startswith("__editor_"):
                continue
            # These are the only shared files the transaction compiler accepts.
            if not allowed_override(relative):
                raise ValueError(f"unsupported compiler override: {relative}")
            target = private_path(self.private, relative)
            if target.exists() and (not target.is_file() or target.stat().st_nlink != 1):
                raise ValueError("unsafe compiler override target")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            self.overridden.add(relative)
        for relative in overrides.get("__editor_deleted_files__", []):
            if not allowed_override(relative):
                raise ValueError(f"unsupported compiler deletion: {relative}")
            private_path(self.private, relative).unlink(missing_ok=True)
            self.overridden.add(relative)


def allowed_override(relative: str) -> bool:
    return relative in INPUT_FILES or (
        relative.endswith(".json") and any(relative.startswith(root + "/") for root in INPUT_DIRS)
    )


def content_hash(plan: dict, overrides: dict) -> str:
    """Raw transaction content, excluding non-rendering randomly allocated IDs."""
    import hashlib
    clean = {key: value for key, value in plan.items() if key != "_editor"}
    return hashlib.sha256(json.dumps(
        {"plan": clean, "overrides": overrides}, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode()).hexdigest()


def reset_request_caches() -> None:
    """Imported modules stay warm, but their file-backed function caches do not."""
    seen = set()
    for name, module in tuple(sys.modules.items()):
        if not name.startswith("reels.") or module is None:
            continue
        for value in tuple(vars(module).values()):
            if not callable(value) or id(value) in seen:
                continue
            clear = getattr(value, "cache_clear", None)
            if callable(clear):
                seen.add(id(value))
                clear()


def timing_build(renderer, project, plan: dict) -> dict:
    """The renderer's segment timing prefix, never a replacement for validation.

    These are the same renderer functions, in the same order, as build() before
    it starts laying out reel-level overlays. Beat retiming and loop seam
    protection therefore remain authoritative. A full build always follows.
    """
    if not plan.get("segments"):
        return {"fps": int(project.cfg("render", "fps")), "total": 0, "segs": [],
                "props": {}, "sound": {}, "warnings": []}
    fps = int(project.cfg("render", "fps"))
    errors: list[str] = []
    loop = renderer.check_loop(plan, errors)
    on_beats = renderer.check_beats_punch(plan, errors)
    segments, warnings = renderer.normalize(project, plan)
    swap = renderer.swap_halves(segments, fps) if loop else (False, "")
    if swap[0]:
        segments[-1].spec["_keepOut"] = True
    renderer.resolve_music(project, plan, segments, fps, warnings, loop=loop,
                           grid_for="punchOnBeats" if on_beats else "")
    renderer.fit_cutaways(segments, fps, warnings)
    return serialize({"fps": fps, "total": segments[-1].end, "segs": segments,
                      "props": {}, "sound": {}, "warnings": warnings})


class UnsupportedBatch(Exception):
    pass


def prepare_batch(renderer, project, plan, overrides, batch):
    """Execute trusted operations privately, returning timing evidence for replay.

    The broker never accepts a worker-produced raw plan: it replays the typed
    operations itself and compares content hashes before considering a commit.
    """
    # Only editor helpers come from the trusted application; renderer imports
    # continue resolving against the selected immutable source.
    import reels.editor
    reels.editor.__path__ = [str(Path(__file__).parent)]
    from reels.editor.transactions import DocumentStore, DocumentError
    from reels.editor.document import ensure_node_ids
    from reels.config import DEFAULTS, deep_merge
    timing = []

    class BatchStore(DocumentStore):
        def __init__(self):
            self.project = project
            self.data_root = project.root
            self._compile_state = threading.local()

        def _revisions(self, document=None):
            return batch["revisions"]

        def _compile(self, value, aux, *args, **kwargs):
            project.config = deep_merge(deepcopy(DEFAULTS), deepcopy(aux.get("config.json", {})))
            result = timing_build(renderer, project, deepcopy(value))
            timing.append(deepcopy(result))
            return result

    store = BatchStore()
    context = {"plan": plan, "files": {}, "aux": overrides,
               "compiled": batch["compiledBefore"], "fps": batch["compiledBefore"]["fps"],
               "affected": set(), "origin": "manual"}
    for index, operation in enumerate(batch["operations"]):
        # A generated cue after a timing change needs a complete intermediate
        # layout. Keep that uncommon batch on the existing isolated slow path.
        target = operation.get("id")
        if (timing and operation.get("op") in ("move_overlay", "resize_overlay", "remove_overlay")
                and target not in ensure_node_ids(plan)
                and target not in context.get("generatedAliases", {})):
            raise UnsupportedBatch("generated cue edit after timing change requires isolated validation")
        try:
            store._apply_edit_operation(context, operation)
        except DocumentError as exc:
            exc.details.setdefault("operation", index)
            raise
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise DocumentError("invalid_operation", str(exc), {"operation": index}) from exc
    ensure_node_ids(plan)
    return timing, content_hash(plan, overrides)


def instrument(renderer: Any) -> None:
    """Install the same raw-locator tracing used by the cold compiler once."""
    class LocatedText(str):
        pass

    def traced(original, field):
        def wrapped(*args, **kwargs):
            screen = renderer.screen_text

            def located(value, warnings, where):
                text = LocatedText(screen(value, warnings, where))
                match = re.fullmatch(r"segment (\d+) (text|stats)\[(\d+)\]", where)
                root = re.fullmatch(r"text\[(\d+)\]", where)
                text.locator = (
                    f"/segments/{int(match[1])-1}/{match[2]}/{match[3]}" if match
                    else f"/text/{root[1]}" if root else None
                )
                return text

            renderer.screen_text = located
            try:
                values = original(*args, **kwargs)
                for value in values:
                    locator = getattr(value.get(field), "locator", None)
                    if locator:
                        value["_editorPath"] = locator
                return values
            finally:
                renderer.screen_text = screen
        return wrapped

    for helper, field in (("_beats", "text"), ("_stats", "label")):
        original = getattr(renderer, helper, None)
        if callable(original) and callable(getattr(renderer, "screen_text", None)):
            setattr(renderer, helper, traced(original, field))
    original = getattr(renderer, "build_exhibits", None)
    if callable(original):
        def located_exhibits(*args, **kwargs):
            values = original(*args, **kwargs)
            for value in values:
                match = re.fullmatch(r"(?:segment (\d+) )?exhibit (\d+)", value.get("where", ""))
                if match:
                    value["props"]["_editorPath"] = (
                        (f"/segments/{int(match[1])-1}" if match[1] else "")
                        + f"/exhibits/{int(match[2])-1}"
                    )
            return values
        renderer.build_exhibits = located_exhibits


def serialize(built: dict[str, Any]) -> dict[str, Any]:
    segments = []
    for segment in built["segs"]:
        cuts = []
        for cut in segment.spec.get("_cutaways") or []:
            child = cut["seg"]
            match = re.search(r"cutaway (\d+)$", cut.get("where", ""))
            cuts.append({
                "start": cut["start"], "frames": child.frames, "assetId": child.asset["id"],
                "kind": child.kind, "rawIndex": int(match[1]) - 1 if match else None,
                "sourceDuration": child.asset.get("probe", {}).get("duration"),
                "spec": {key: value for key, value in child.spec.items() if not key.startswith("_")},
            })
        segments.append({
            "index": segment.index, "kind": segment.kind, "asset": {"id": segment.asset["id"]},
            "spec": {key: value for key, value in segment.spec.items() if key != "_cutaways"},
            "frames": segment.frames, "trans": segment.trans, "start": segment.start, "cutaways": cuts,
        })
    sound = dict(built.get("sound") or {})
    sound["cues"] = [
        {key: value for key, value in cue.items() if key != "path"}
        for cue in sound.get("cues") or []
    ]
    return {
        "fps": built["fps"], "total": built["total"], "warnings": built["warnings"],
        "segs": segments, "props": built["props"], "sound": sound, "loop": built.get("loop"),
    }


def main() -> None:
    package, source, private, revision, media = sys.argv[1:]
    # Executing this file by path otherwise exposes editor/http.py as the
    # top-level "http" package, shadowing Python's own http.server.
    sys.path[:] = [entry for entry in sys.path
                   if Path(entry or ".").resolve() != Path(__file__).parent.resolve()]
    sys.path.insert(0, package)
    # Renderer chatter must never share the response protocol's stdout.
    output = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):
        from reels.config import Project
        import reels.render as renderer
        instrument(renderer)
    mirror = InputMirror(Path(source), Path(private), media_root=Path(media) if media else None)
    for line in sys.stdin:
        request: dict[str, Any] = {}
        stage = "inputs"
        try:
            request = json.loads(line)
            if (request.get("version") != PROTOCOL_VERSION
                    or request.get("rendererRevision") != revision
                    or not isinstance(request.get("libraryRevision"), str)):
                raise ValueError("compiler revision/protocol mismatch")
            with contextlib.redirect_stdout(sys.stderr):
                mirror.sync(request.get("inputSignatures"))
                reset_request_caches()
                overrides = request.setdefault("overrides", {})
                mirror.apply(overrides)
                project = Project(Path(private), code_root=Path(package).parent,
                                  **({"media_root": media} if media else {}))
                plan = request["plan"]
                timing, candidate_hash = None, None
                stage = "validation"
                if request.get("batch") is not None:
                    timing, candidate_hash = prepare_batch(
                        renderer, project, plan, overrides, request["batch"])
                    mirror.apply(overrides)
                    project = Project(Path(private), code_root=Path(package).parent,
                                      **({"media_root": media} if media else {}))
                if not plan.get("segments"):
                    result = {
                        "fps": int(project.cfg("render", "fps") or 30), "total": 0,
                        "warnings": ["No footage yet: add a clip from the library to start."],
                        "segs": [], "props": {}, "sound": {"cues": []}, "loop": None,
                    }
                else:
                    result = serialize(renderer.build(project, deepcopy(plan)))
                stage = "inputs"
                mirror.verify()
            response = {
                "requestId": request["requestId"], "version": PROTOCOL_VERSION,
                "rendererRevision": revision, "libraryRevision": request["libraryRevision"],
                "result": result,
            }
            if timing is not None:
                response.update(timingResults=timing, candidateHash=candidate_hash)
        except Exception as exc:  # error invalidates this worker; never reuse partially mutated state
            response = {
                "requestId": request.get("requestId"), "version": PROTOCOL_VERSION,
                "error": str(exc)[-4000:],
                "errorKind": "unavailable" if stage == "inputs" or isinstance(exc, UnsupportedBatch) else "validation",
                "errorCode": getattr(exc, "code", "validation_failed"),
                "errorDetails": getattr(exc, "details", {}),
            }
            print(json.dumps(response), file=output, flush=True)
            return
        print(json.dumps(response, ensure_ascii=False,
                         default=lambda value: sorted(value) if isinstance(value, set) else str(value)),
              file=output, flush=True)


if __name__ == "__main__":
    main()
