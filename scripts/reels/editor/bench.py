"""Reproducible latency benchmark for the editor's DocumentStore hot paths.

The CLI benchmarks a disposable copy of render inputs and plans. Original media
is linked read-only; editor state, temporary plans, and history stay in the
temporary workspace and are removed on every exit path.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shlex
import shutil
import statistics
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reels.config import Project  # noqa: E402
from reels.editor.document import _descriptors  # noqa: E402
from reels.editor.transactions import DocumentStore  # noqa: E402
from reels.editor.workspace import INPUT_DIRS, INPUT_FILES, Workspace  # noqa: E402

_DEFAULT_PLAN = "plans/2026-11-15-little-gray.json"
_METRICS = (
    "open (new document)",
    "snapshot compile-cold (cache miss)",
    "snapshot cold-memory (disk cache hit)",
    "snapshot warm",
    "open + first snapshot",
    "reopen unchanged + first snapshot",
    "apply set_field",
    "apply reorder_segments",
    "apply trim_segment",
    "apply move_overlay",
    "undo",
)


def _copy_regular_tree(source: Path, destination: Path) -> None:
    """Copy a data directory without following or reproducing symlinks."""
    if source.is_symlink() or not source.is_dir():
        return
    destination.mkdir(parents=True, exist_ok=True)
    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        base = Path(current)
        dirs[:] = [name for name in dirs if not (base / name).is_symlink()]
        for name in dirs:
            (destination / (base / name).relative_to(source)).mkdir(exist_ok=True)
        for name in files:
            path = base / name
            if path.is_symlink() or not path.is_file():
                continue
            target = destination / path.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)


def _clone_workspace(repo_root: Path, clone_root: Path) -> Workspace:
    """Make an isolated data workspace while using production renderer code."""
    clone_root.mkdir(parents=True)
    for relative in INPUT_DIRS:
        if relative == "library/media":
            continue
        _copy_regular_tree(repo_root / relative, clone_root / relative)
    for relative in INPUT_FILES:
        source = repo_root / relative
        if source.is_file() and not source.is_symlink():
            target = clone_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
    for relative in ("learning", "models"):
        _copy_regular_tree(repo_root / relative, clone_root / relative)

    # Workspace.live checks for this directory, while code_root below points at
    # the real renderer checkout. Keeping it empty avoids exposing a checkout
    # symlink from the cloned data tree.
    (clone_root / "remotion").mkdir(exist_ok=True)

    # Workspace.live enforces live-workspace invariants, but all writes resolve
    # under clone_root and the production code/media sources remain read-only.
    workspace = Workspace.live(clone_root, code_root=repo_root)
    # Register originals directly from their read-only source, rather than
    # linking library/media into the sandbox's data source tree.
    workspace.project = Project(
        clone_root, code_root=repo_root, media_root=repo_root / "library/media", media_read_only=True
    )
    workspace.sync_originals(force=True)
    return workspace


def _document_history_path(workspace: Any, plan_rel: str) -> Path:
    """Mirror DocumentStore's deterministic per-plan history directory."""
    live_revision = getattr(workspace, "renderer_revision", None)
    key_source = plan_rel
    if callable(live_revision):
        key_source = f"{plan_rel}\0{live_revision()}"
    key = hashlib.sha256(key_source.encode()).hexdigest()[:24]
    root = Path(getattr(workspace, "root", workspace.project.root)).resolve()
    return root / "history" / "documents" / key


def _remove_document_artifacts(workspace: Any, plan_path: Path, history: Path) -> None:
    root = Path(getattr(workspace, "root", workspace.project.root)).resolve()
    documents = (root / "history" / "documents").resolve()
    history = history.resolve()
    if history != documents and documents not in history.parents:
        raise RuntimeError(f"refusing to remove history outside benchmark workspace: {history}")
    first_error = None
    for cleanup in (
        lambda: shutil.rmtree(history) if history.exists() else None,
        lambda: (root / "documents" / f"{plan_path.stem}.json").unlink(missing_ok=True),
        lambda: plan_path.unlink(missing_ok=True),
    ):
        try:
            cleanup()
        except OSError as exc:
            first_error = first_error or exc
    if first_error:
        raise first_error


def _close_and_clean(workspace: Any, store: DocumentStore | None, plan_path: Path, history: Path) -> None:
    try:
        if store is not None:
            store.close()
    finally:
        _remove_document_artifacts(workspace, plan_path, history)


def _pctl(samples: list[float], percentile: int) -> float:
    ordered = sorted(samples)
    # Nearest-rank percentile, including the max for p95 when N < 20.
    index = max(0, (percentile * len(ordered) + 99) // 100 - 1)
    return ordered[index]


def _timed(call: Callable[[], Any]) -> tuple[float, Any]:
    started = time.perf_counter()
    result = call()
    return (time.perf_counter() - started) * 1000, result


def _segment_operation(snapshot: dict[str, Any], store: DocumentStore) -> dict[str, Any]:
    nodes = [node for node in snapshot["nodes"] if node["kind"] == "segment"]
    if len(nodes) < 2:
        raise ValueError("benchmark fixture needs at least two primary segments")
    return {"op": "reorder_segments", "ids": [nodes[1]["id"]], "beforeId": nodes[0]["id"]}


def _trim_operation(snapshot: dict[str, Any], store: DocumentStore) -> dict[str, Any]:
    for index, node in enumerate(node for node in snapshot["nodes"] if node["kind"] == "segment"):
        if node.get("assetKind") == "photo":
            continue
        spec = store._current_compiled["segs"][index]["spec"]
        source_in = float(spec.get("in", 0))
        source_out = float(spec.get("out", source_in + 1))
        delta = min(0.05, (source_out - source_in) / 10)
        if delta <= 0 or source_out - source_in <= 3 * delta:
            continue
        return {
            "op": "trim_segment",
            "id": node["id"],
            "sourceIn": round(source_in + delta, 6),
            "sourceOut": round(source_out - delta, 6),
        }
    raise ValueError("benchmark fixture needs a trim-able video segment")


def _move_operation(snapshot: dict[str, Any]) -> dict[str, Any]:
    for node in snapshot["nodes"]:
        raw = node.get("raw")
        if (node.get("editable") and not node.get("generated")
                and node.get("kind") not in ("segment", "cutaway")
                and isinstance(raw, dict) and raw.get("at") is not None):
            return {"op": "move_overlay", "id": node["id"], "at": round(float(raw["at"]) + 0.01, 6)}
    raise ValueError("benchmark fixture needs an editable timed overlay")


def run_document_bench(
    workspace: Any,
    source_plan: Path | str,
    runs: int = 10,
    *,
    compiler: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Use one broker-lifetime compiler session, just like the app service."""
    from contextlib import nullcontext
    from reels.editor.compile_worker import begin_compile_session
    actual_worker = compiler is None and not callable(getattr(workspace, "compiler_for_renderer", None))
    with begin_compile_session(workspace) if actual_worker else nullcontext():
        return _run_document_bench(workspace, source_plan, runs, compiler=compiler)


def _run_document_bench(
    workspace: Any,
    source_plan: Path | str,
    runs: int = 10,
    *,
    compiler: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Measure one isolated plan across N fresh-document repetitions per path."""
    if runs < 1:
        raise ValueError("runs must be at least 1")
    source_plan = Path(source_plan).expanduser().resolve()
    if not source_plan.is_file():
        raise FileNotFoundError(source_plan)
    plan_value = json.loads(source_plan.read_text(encoding="utf-8"))
    if not isinstance(plan_value, dict) or len(plan_value.get("segments") or []) < 2:
        raise ValueError("benchmark plan must be an object with at least two segments")
    synthetic_overlay = not any(
        node["kind"] not in ("segment", "cutaway")
        and isinstance(node["raw"], dict)
        and node["raw"].get("at") is not None
        for node in _descriptors(plan_value)
    )
    if synthetic_overlay:
        # Some otherwise representative existing plans have no movable timed
        # overlay. Add one only to the disposable benchmark copies.
        plan_value = json.loads(json.dumps(plan_value))
        text_overlays = plan_value.get("text")
        if not isinstance(text_overlays, list):
            text_overlays = []
            plan_value["text"] = text_overlays
        text_overlays.append({
            "text": "Editor benchmark marker", "at": 0.2, "for": 0.3, "position": "top"
        })

    plan_dir = Path(workspace.project.plans_dir)
    plan_dir.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:10]
    samples: dict[str, list[float]] = {name: [] for name in _METRICS}
    result: dict[str, Any] = {
        "segments": len(plan_value.get("segments") or []),
        "rawNodes": len(_descriptors(plan_value)),
        "runs": runs,
        "syntheticOverlay": synthetic_overlay,
        "diskCompileCacheEnabled": (
            compiler is None
            and not callable(getattr(workspace, "compiler_for_renderer", None))
        ),
    }
    actual_worker = result["diskCompileCacheEnabled"]
    result["workerRPCEnabled"] = actual_worker
    if actual_worker:
        samples["compile warm (worker RPC)"] = []
    else:
        result["workerRPCSkippedReason"] = "injected compiler/provider; not a sandbox worker RPC"

    def fresh_plan(metric: str, iteration: int) -> tuple[Path, Path]:
        path = plan_dir / f"zz-bench-{run_id}-{metric.replace(' ', '-')}-{iteration}.json"
        path.write_text(json.dumps(plan_value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        rel = path.resolve().relative_to(Path(workspace.data_root).resolve()).as_posix()
        return path, _document_history_path(workspace, rel)

    def open_store(path: Path) -> DocumentStore:
        return DocumentStore(workspace, path, compiler=compiler)

    if actual_worker:
        path, history = fresh_plan("cold-broker-startup", 0)
        store = None
        try:
            def cold_open():
                nonlocal store
                store = open_store(path)
                return store.snapshot()
            elapsed, _ = _timed(cold_open)
            result["coldBrokerOpenMs"] = elapsed
        finally:
            _close_and_clean(workspace, store, path, history)

        for iteration in range(runs):
            path, history = fresh_plan("worker-rpc", iteration)
            store = None
            try:
                store = open_store(path)
                snapshot = store.snapshot()
                project = store._project_for_renderer(snapshot["revisions"]["renderer"])
                plan = store._current_raw_plan()
                overrides = {key: value for key, value in store._load_files().items()
                             if key != store.plan_rel}
                pool = store._warm_compiler()
                args = (project, plan, overrides, snapshot["revisions"]["renderer"],
                        snapshot["revisions"]["library"])
                pool.prewarm(project, snapshot["revisions"]["renderer"])
                pool.compile(*args)  # imports, input sync and any lazy initialization are untimed
                elapsed, _ = _timed(lambda: pool.compile(*args))
                samples["compile warm (worker RPC)"].append(elapsed)
            finally:
                _close_and_clean(workspace, store, path, history)

    for iteration in range(runs):
        path, history = fresh_plan("open", iteration)
        store = None
        try:
            elapsed, store = _timed(lambda: open_store(path))
            samples["open (new document)"].append(elapsed)
        finally:
            _close_and_clean(workspace, store, path, history)

    cold_node_count = 0
    for iteration in range(runs):
        path, history = fresh_plan("compile-cold", iteration)
        store = None
        try:
            store = open_store(path)
            store.snapshot()  # seed normal state, then remove only this temp document's cache
            cache_path = store.history / "COMPILED.json"
            store.close()
            store = None
            cache_path.unlink(missing_ok=True)
            store = open_store(path)
            elapsed, snapshot = _timed(store.snapshot)
            samples["snapshot compile-cold (cache miss)"].append(elapsed)
            cold_node_count = len(snapshot["nodes"])
        finally:
            _close_and_clean(workspace, store, path, history)

    for iteration in range(runs):
        path, history = fresh_plan("cold-memory", iteration)
        store = None
        try:
            store = open_store(path)
            store.snapshot()  # persist the compiled result, but not in-memory state
            store.close()
            store = open_store(path)
            elapsed, snapshot = _timed(store.snapshot)
            samples["snapshot cold-memory (disk cache hit)"].append(elapsed)
            cold_node_count = len(snapshot["nodes"])
        finally:
            _close_and_clean(workspace, store, path, history)

    for iteration in range(runs):
        path, history = fresh_plan("warm", iteration)
        store = None
        try:
            store = open_store(path)
            store.snapshot()
            elapsed, _ = _timed(store.snapshot)
            samples["snapshot warm"].append(elapsed)
        finally:
            _close_and_clean(workspace, store, path, history)

    for iteration in range(runs):
        path, history = fresh_plan("open-first-snapshot", iteration)
        store = None
        try:
            def open_and_snapshot():
                nonlocal store
                store = open_store(path)
                return store.snapshot()
            elapsed, snapshot = _timed(open_and_snapshot)
            samples["open + first snapshot"].append(elapsed)
            cold_node_count = len(snapshot["nodes"])
        finally:
            _close_and_clean(workspace, store, path, history)

    for iteration in range(runs):
        path, history = fresh_plan("reopen-unchanged", iteration)
        store = None
        try:
            store = open_store(path)
            store.snapshot()  # leave the unchanged document's view cache on disk
            store.close()
            store = None
            def reopen_and_snapshot():
                nonlocal store
                store = open_store(path)
                return store.snapshot()
            elapsed, snapshot = _timed(reopen_and_snapshot)
            samples["reopen unchanged + first snapshot"].append(elapsed)
            cold_node_count = len(snapshot["nodes"])
        finally:
            _close_and_clean(workspace, store, path, history)

    delta_sizes: list[int] = []
    operations = {
        "apply set_field": lambda snapshot, store: {
            "op": "set_field", "path": "/benchNote", "value": f"run-{uuid.uuid4().hex}"
        },
        "apply reorder_segments": _segment_operation,
        "apply trim_segment": _trim_operation,
        "apply move_overlay": lambda snapshot, _store: _move_operation(snapshot),
    }
    for metric, operation_factory in operations.items():
        for iteration in range(runs):
            path, history = fresh_plan(metric, iteration)
            store = None
            try:
                store = open_store(path)
                before = store.snapshot()
                operation = operation_factory(before, store)
                batch = {"expected": before["revisions"], "origin": "manual", "operations": [operation]}
                elapsed, after = _timed(lambda: store.apply(batch))
                samples[metric].append(elapsed)
                if metric == "apply set_field":
                    from reels.editor.delta import snapshot_delta, snapshot_token

                    before["snapshotToken"] = snapshot_token(before)
                    after["snapshotToken"] = snapshot_token(after)
                    delta = snapshot_delta(
                        before, after, before["revisions"], before["snapshotToken"]
                    )
                    if delta is None:
                        raise RuntimeError("benchmark could not construct a delta for its matching base")
                    delta_sizes.append(len(json.dumps(delta, ensure_ascii=False, separators=(",", ":")).encode()))
            finally:
                _close_and_clean(workspace, store, path, history)

    for iteration in range(runs):
        path, history = fresh_plan("undo", iteration)
        store = None
        try:
            store = open_store(path)
            before = store.snapshot()
            applied = store.apply({
                "expected": before["revisions"],
                "origin": "manual",
                "operations": [{"op": "set_field", "path": "/benchNote", "value": "undo-me"}],
            })
            elapsed, _ = _timed(lambda: store.undo(applied["revisions"]))
            samples["undo"].append(elapsed)
        finally:
            _close_and_clean(workspace, store, path, history)

    result["snapshotNodes"] = cold_node_count
    result["samplesMs"] = samples
    result["summaryMs"] = {
        name: {"p50": statistics.median(values), "p95": _pctl(values, 95)}
        for name, values in samples.items()
    }
    result["deltaBytes"] = {
        "p50": statistics.median(delta_sizes),
        "p95": _pctl(delta_sizes, 95),
    }
    return result


def _print_report(
    result: dict[str, Any],
    *,
    command: str,
    plan: Path,
    source_root: Path,
) -> None:
    print(f"command: {command}")
    synthetic = "; adds temporary timed text overlay" if result["syntheticOverlay"] else ""
    print(
        f"fixture: {plan} ({result['segments']} segments, {result['snapshotNodes']} snapshot nodes"
        f"{synthetic})"
    )
    print(f"workspace: isolated data clone; original media read-only; source={source_root}")
    print(
        "environment: "
        f"Python {platform.python_version()} ({sys.executable}), {platform.platform()}, "
        f"machine={platform.machine()}, CPUs={os.cpu_count()}"
    )
    print(f"runs: {result['runs']}")
    cache_label = "enabled" if result["diskCompileCacheEnabled"] else "disabled by injected compiler"
    print(
        "timing: constructor-only new-document open; compile-cold snapshot removes COMPILED.json; "
        f"cold-memory reopen keeps the shared worker; disk compile cache {cache_label}."
    )
    if result.get("workerRPCEnabled"):
        print(f"cold broker first open + snapshot: {result['coldBrokerOpenMs']:.2f} ms "
              "(one startup sample; excluded from warm-broker percentiles)")
        print("warm worker RPC includes sandbox input verification, build and transport; no journal/disk-result cache.")
    else:
        print(f"warm worker RPC: skipped ({result['workerRPCSkippedReason']})")
    path_width = max(24, max(map(len, result["summaryMs"])))
    print(f"{'path':<{path_width}} {'p50 ms':>10} {'p95 ms':>10}")
    for name, values in result["summaryMs"].items():
        print(f"{name:<{path_width}} {values['p50']:>10.2f} {values['p95']:>10.2f}")
    print(
        "apply set_field delta JSON bytes: "
        f"p50 {result['deltaBytes']['p50']:.0f}, p95 {result['deltaBytes']['p95']:.0f}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", default=_DEFAULT_PLAN, help="source plan, copied read-only into the bench workspace")
    parser.add_argument("--runs", type=int, default=10, help="fresh-document repetitions per path (default: 10)")
    args = parser.parse_args(argv)

    repo_root = Path(__file__).resolve().parents[3]
    source_plan = Path(args.plan)
    if not source_plan.is_absolute():
        source_plan = repo_root / source_plan
    source_plan = source_plan.resolve()
    try:
        source_plan.relative_to(repo_root)
    except ValueError:
        parser.error("--plan must be inside the project workspace")
    if args.runs < 1:
        parser.error("--runs must be at least 1")

    with tempfile.TemporaryDirectory(prefix="reels-editor-bench-") as temporary:
        clone_root = Path(temporary) / "workspace"
        workspace = _clone_workspace(repo_root, clone_root)
        workspace.start_library_watcher()
        try:
            result = run_document_bench(workspace, source_plan, args.runs)
        finally:
            workspace.stop_library_watcher()
    exact_command = f"PYTHONPATH=scripts {shlex.join([sys.executable, *sys.argv])}"
    _print_report(result, command=exact_command, plan=source_plan, source_root=repo_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
