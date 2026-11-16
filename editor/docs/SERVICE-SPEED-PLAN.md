# Editor service speed plan

Status: phases 0–3 and 5–6 implemented and verified (2026-11-16), on `perf/editor-service-speed`. Cold broker startup remains above the 400 ms opening goal; measured results are below.
Implementation: one Sol and one Luna subagent, with integration and acceptance checks in the parent session.

## Decisions

- Use **A + B1**: one broker remains the writer; a persistent sandboxed worker accelerates compilation.
- **Validate before saving.** The user selected this option explicitly. Optional phase 4's asynchronous validation and automatic undo are excluded.
- Keep AI/candidate validation and renderer changes on the isolated validation path.
- Capture a reproducible baseline before integrating performance changes; record measured results and any unmet targets below.

## Goal

Make every editing round trip in Reels Editor feel instant:

| Path | Today | Target |
|---|---|---|
| `document.apply` (timeline drag, trim, inspector field) | ~1.15 s (was 1.5–2 s before PR #21) | **< 100 ms p50, < 250 ms p95** |
| `document.snapshot` (warm) | ~100 ms (first call ~750 ms) | **< 20 ms** |
| Open a reel (`document.open` + first snapshot) | ~1.7 s | < 400 ms |
| Library revision check | ~65–100 ms per call, 4–8 calls per request | ~0 ms (event-driven) |

The app already shows timeline edits immediately (`editor/ReelsEditor/OptimisticTimeline.swift`, PR #21). This plan is about the service catching up, so that preview refresh, inspector values, undo and quick follow-up edits stop waiting.

## Where the time goes (measured 2026-11-16, 42-node plan, M-series Mac)

Measured by calling `DocumentStore` directly (the app's service holds the workspace lock, so it can't be benchmarked live).

1. **Worker compile: ~550–630 ms each, two per timing edit.** `transactions._worker_compile` builds a sandbox profile, then copies `library/assets`, `library/transcripts`, `music/` and config into a scratch folder. Next it starts `sandbox-exec python`, imports `reels.render` and runs `build()`. **The same `build()` run in-process takes 21 ms.** About 96 % of the compile time is process start-up, imports and copying files, not the work itself.
   - Call 1: candidate validation after the ops (`apply`, transactions.py ~line 1277).
   - Call 2: timing remap for reorder, trim, split and speed edits (`_apply_edit_operation` → `self._compile(plan, aux)` ~line 1360). It recompiles only to learn the new segment start frames.
   - Call 0 (open / cache miss): `compiled_before`, when `_current_compiled` misses.
2. **Library revision: ~65–100 ms per call.** `Workspace.library_revision()` walks every input folder (~8k `stat`s, `pathlib` overhead) on every call. One `snapshot()` calls it 4 times, and one `apply()` about 8 times. PR #21 reuses it inside a single `apply()` only.
3. **Snapshot: ~100 ms warm, ~750 ms cold.** Almost all of that is (2) plus `_current_compiled` revision checks. The payload is about 35 KB of JSON, so serialisation isn't the problem.
4. **Protocol:** NDJSON over stdio with a 6-thread pool. Cheap (< 5 ms). The single `DocumentStore` RLock serialises applies, which is right, but long compiles hold the queue.

## Options considered

### A. Make the one service fast (no second process)
Remove the waste in place: warm compiles, an event-driven library revision, and one compile per edit. This gets apply down to roughly 80–150 ms. The architecture stays the same, so the risk is low.

### B. Add a "fast lane" next to the current service
A slim, always-warm process that answers the hot path: snapshot, plus applying and validating timeline/inspector ops against the current renderer. The existing service keeps the slow or rare work: preview renders, export, ingest, music, search, AI/MCP edits, scheduling and renderer revisions. This could be either:
- **B1. A persistent sandboxed compile worker** (a "compiler daemon") that the broker talks to over a pipe, instead of spawning one per compile. Same sandbox profile and same trust boundary. It's started once per open document and restarted when renderer code or the library changes. This is the "secondary service that runs even faster".
- **B2. A separate fast broker process** that the app talks to directly for edits. **Not recommended.** Two writers to `HEAD`, the journal and leases would need cross-process locking, and the existing invariants (expected revisions, undo journal, lease rules) live in one `DocumentStore`. That's a lot of risk for little gain over B1.

### Recommendation
**A + B1.** Keep one broker as the only writer, and make its expensive dependency (the compile) a warm, persistent, sandboxed worker. Run the slim path (warm worker, cached revisions, one compile) for every manual edit. Fall back to today's cold spawn only when the warm worker is unavailable, crashed, or running a different renderer revision.

## Plan

Each phase ships on its own and has its own acceptance check. Phases 1–3 do most of the work.

### Phase 0: Benchmark harness (do first, ~½ day)
- Add `scripts/reels/editor/bench.py` (or `./reels editor bench`). It copies a given plan to a temporary `plans/zz-bench-*.json`, opens a `DocumentStore`, and times: open, cold and warm snapshot, apply for `set_field`, `reorder_segments`, `trim_segment` and `move_overlay`, plus undo. It prints p50 and p95 over N runs. Afterwards it deletes the temp plan **and its `.cache/editor/live/history/documents/<id>` folder**.
- It must refuse to run while the app's service holds the workspace lock, or use a cloned workspace.
- Record baseline numbers in this file.
- Acceptance: one command reproduces the table above.

#### Recorded baseline

Captured before integrating performance changes, using Python 3.12.16 on macOS 27 arm64 (12 CPUs), with 10 runs. The disposable benchmark fixture has **3 segments and 16 snapshot nodes**, including a timed text overlay added only to its copies. This is smaller than the original 42-node investigation above. The p95 uses nearest rank (the maximum with 10 samples).

```sh
PYTHONPATH=scripts .venv/bin/python scripts/reels/editor/bench.py \
  --plan plans/2026-11-15-little-gray.json --runs 10
```

| Path | Baseline p50 (ms) | Baseline p95 (ms) |
|---|---:|---:|
| Open (without first snapshot) | 542.92 | 719.01 |
| Cold snapshot | 622.53 | 653.09 |
| Warm snapshot | 107.61 | 109.05 |
| Apply `set_field` | 616.34 | 660.72 |
| Apply `reorder_segments` | 1132.36 | 2876.22 |
| Apply `trim_segment` | 1143.98 | 1230.44 |
| Apply `move_overlay` | 619.84 | 635.20 |
| Undo | 587.69 | 616.37 |

Open and cold snapshot were measured separately; neither baseline row is an end-to-end open measurement.

### Phase 1: Event-driven library revision (~1 day)
- Replace per-call folder walks with a cached revision. A file watcher marks it dirty: FSEvents via `watchdog`, or extend the existing `_watch` thread in `service.py`, which already polls plans. If neither is available, use one cheap stamp: the mtime of each input directory plus the count of JSON files. Recompute only when dirty.
- Keep explicit invalidation where the service itself writes library inputs: `_commit_files` (transcript/config edits), ingest publish, music add, and the preview `_copy_inputs` freeze check, which must still compare against a fresh value.
- The existing tests in `tests/test_editor_workspace.py` expect a change to show up straight after writing a file. Keep that by invalidating synchronously when the watcher is unavailable, or by having the tests call a `refresh()` hook. **Don't loosen those tests.**
- Acceptance: warm snapshot < 20 ms, and the revision is still correct after external edits (`./reels ingest` running in a terminal).

### Phase 2: Warm sandboxed compile worker (B1) (~2–3 days)
- New `scripts/reels/editor/compile_worker.py`: a long-lived loop under the same `sandbox-exec` profile. It imports `reels.render` once, then reads compile requests (`{plan, overrides, requestId}`) on stdin and writes results on stdout.
- Keep a persistent scratch data folder per worker instead of copying for every compile. Sync only the JSON files whose `(mtime, size)` changed since the last request (assets, transcripts, music, config). The worker's write access stays limited to its own folder.
- The broker keeps a small pool, one worker per renderer revision (usually just one). Each worker is keyed by renderer revision and the set of library roots it may read.
- Restart the worker when: the renderer revision changes, the library roots or sandbox profile change, it crashes or times out, or it reaches a memory or request-count cap (for example 500 compiles).
- Cancellation: today `cancel_pending` kills the spawned process. With a warm worker, cancel by request ID, and kill and restart the worker only if a request overruns its deadline.
- Fallback: if the warm worker is unavailable, use today's `_worker_compile` unchanged.
- Acceptance: compile < 60 ms p50 when warm. Every `test_editor_*` test passes, including the sandbox and offline-media tests. A fault-injection test kills the worker mid-request and the next edit still succeeds through the fallback.

### Phase 3: One compile per edit (~1 day)
- The timing remap (`_apply_edit_operation`, ~line 1351) compiles only to get new segment start frames. Move the remap after the candidate compile and reuse that result, or compute segment starts from the compiled output of the edited plan once. Either way, one compile per apply instead of two.
- Check that this is valid: today the remap runs *before* the candidate compile because it changes the plan (it moves reel-level text, music drops, sfx and exhibits). Options are (a) a cheap in-process timing pass (`render.build` minus media probing) used only for the remap, or (b) remap, then compile once, accepting that the remap uses the pre-remap timing of overlays. The reviewer should check which one keeps `test_editor_transactions` timing tests green.
- Acceptance: a reorder or trim apply makes exactly one worker call, and the timing tests pass.

### Phase 4: Cheaper validation for simple manual ops (optional, needs a decision)
- Many manual edits (`set_field` on non-timing fields, `move_overlay`, `resize_overlay`, `set_node_track`, rename or lock track) can't change renderer validity beyond the schema checks already in `operations.py` (pydantic). For an allow-listed set:
  - commit right after the structural/schema checks (≈ 5–10 ms), then
  - run the full renderer validation asynchronously on the warm worker. If it fails, auto-undo that commit and tell the app (`document.changed` with a warning).
- This breaks today's rule that "HEAD is never written before compile" (the comments in `candidate_snapshot` and `apply`). **The user decides whether that's acceptable.** If not, phases 1–3 alone should already reach ~80–150 ms.

### Phase 5: Payload and app-side polish (~1 day)
- After an apply, send a **delta** (`changedNodes`, `removedIds`, `revisions`, `durationFrames`) instead of the full snapshot when the app's base revision matches. Send the full snapshot only on mismatch. This matters more as plans grow; at 35 KB it's minor today.
- The app drops the optimistic prediction for that edit when the delta lands (already structured that way in `EditorStore.send`).
- Debounce preview preparation (`_changed` already uses a 0.15 s timer). Make sure a burst of queued drags triggers one preview, not one per edit.
- Acceptance: payload per apply < 5 KB for single-node edits.

### Phase 6: Open/startup (~½ day)
- Start the warm worker as soon as a document opens, in parallel with the first snapshot, and cache the last compiled result on disk, keyed by (document revision, library revision, renderer revision). Reopening an unchanged reel then needs no compile at all.

## What stays on the existing (slow) path
Preview render (Remotion), export, ingest/import, music download, search indexing, scheduling, AI/MCP edits (still fully validated before commit, still blocked by manual leases), renderer revision activation, undo/redo across renderer revisions, and offline-media documents.

## Risks
- **Sandbox boundary:** a long-lived worker gets more chances to be confused by earlier requests. Mitigate with no state between requests besides the imported modules and the synced read-only scratch, a request-count cap, and a restart on any error.
- **Stale library data in the warm scratch:** sync is by `(mtime, size)`, and the worker gets the expected library revision with each request and refuses if it differs.
- **Memory:** one Python process holding `reels.render` is about 60–100 MB. That's acceptable.
- **Phase 4 auto-undo** could surprise the user if validation fails after the clip already moved. Keep the allow-list tight and log every revert.

## Test plan
- All existing `tests/test_editor_*.py` (236 today) and Swift `ReelsEditorTests` (52 today) stay green. Run them with `PYTHONPATH=scripts:tests .venv/bin/python -m unittest discover -s tests -p "test_editor*.py"` and `xcodebuild ... test`.
- New tests: warm worker request/response, fallback after a worker crash, renderer revision switch restarts the worker, library change invalidates the cached revision, one compile per timing apply, and a benchmark smoke test with a generous threshold so CI isn't flaky.
- Live check: in the app, drag 5 clips quickly in a row. No error dialog, the final order matches, and the preview refreshes once.

## Constraints to respect (from project rules)
- Never delete or rename anything under `library/` (`tests/test_editor_live.py` audits this). Benchmarks work on copies in `plans/` and clean up after themselves.
- After Swift changes, run `./editor/reels-editor setup`. Python changes need only an app relaunch.
- Finished changes go on a new branch with a PR containing only that change's files.

## Open questions for the reviewer
1. Is B2 (a separate fast broker) really not worth it, given the single-writer invariants? Or is there a clean way to make the fast lane read-only (snapshot/preview data) and still win?
2. Phase 3: which remap ordering keeps timing correct with only one compile?
3. Phase 4: is "commit first, validate async, auto-undo on failure" acceptable for an allow-listed set of manual ops?
4. FSEvents (`watchdog` dependency) or a stdlib polling stamp for the library revision?
5. Should the warm worker also serve `candidate_snapshot` for AI edits, or keep AI on cold spawns for stronger isolation?

## Implementation notes

- **Phase 0:** `scripts/reels/editor/bench.py` runs against an isolated cloned workspace and disposable plans. It reports p50/p95, node counts, delta bytes, uncached snapshots, disk-backed reopen, end-to-end open plus snapshot, and warm worker RPCs. Cold broker startup is reported separately from warm-broker percentiles. Cleanup includes each temporary document's history.
- **Phase 1:** `Workspace` supplies cached revisions and input signatures while its stdlib polling watcher is active. Explicit invalidation covers service writes and transaction materialization; `refresh_library_revision()` performs a fresh scan, including both sides of preview input freezing. Without an active watcher, revision reads remain synchronous.
- **Phase 2:** `compile_worker.py` manages a bounded workspace-wide pool of sandboxed workers; `compile_runtime.py` runs the request loop inside the sandbox. Document cancellation is request-local, and an explicit broker session owns shutdown. Workers synchronize changed inputs, restore per-request overrides, reject changed inputs, and discard cancelled results. Infrastructure failures fall back to the cold sandbox; validation failures do not.
- **Phase 3:** Timing-only preparation and the final full renderer validation share one worker request. The broker replays the operations and verifies the resulting plan before committing. A batch that edits an unmaterialized generated cue after a timing change conservatively uses cold intermediate-layout compilation and final validation.
- **Phase 4:** Not selected: validation still completes before HEAD is written.
- **Phase 5:** Native manual applies opt into deltas matched by both revisions and an exact snapshot token, including selection, preview, and permissions. `delta.py` and the Swift snapshot merger cover changed/removed nodes, plan changes, revisions, and other snapshot fields. Swift merges into a separate authoritative snapshot before applying queued predictions once. Unknown or stale bases trigger full-snapshot recovery. Preview preparation waits for the current gesture lease to finish so a drag burst coalesces.
- **Phase 6:** Opening a document prewarms its compiler. `COMPILED.json` is a disposable, checksummed cache keyed by plan, revisions, sandbox capabilities, original-media registrations, and runner code. It is distinct from the offline `VIEW.json`.

The reviewer questions above resolve to A+B1, sandboxed timing preparation, validate-before-save, stdlib polling with explicit refresh, and cold isolation for AI/candidate validation.

## Measured result

Final controlled run: **20 samples per repeated path**, on the same Python/macOS environment and 3-segment, 16-node fixture as the baseline. The shared broker session stays alive across document opens. No builds or test suites ran alongside the benchmark.

```sh
PYTHONPATH=scripts .venv/bin/python scripts/reels/editor/bench.py \
  --plan plans/2026-11-15-little-gray.json --runs 20
```

| Path | Final p50 (ms) | Final p95 (ms) |
|---|---:|---:|
| Open constructor, warm broker | 57.08 | 59.98 |
| Uncached snapshot, warm worker | 83.59 | 88.07 |
| Cold-memory snapshot, disk cache hit | 29.72 | 34.01 |
| Warm snapshot | **5.82** | **6.23** |
| Open + first snapshot, warm broker | **66.30** | **78.62** |
| Unchanged reopen + first snapshot | **32.37** | **42.28** |
| Apply `set_field` | **64.41** | **69.77** |
| Apply `reorder_segments` | **69.37** | **79.29** |
| Apply `trim_segment` | **66.22** | **78.57** |
| Apply `move_overlay` | **64.44** | **68.52** |
| Undo | 60.10 | 63.65 |
| Warm compile worker RPC | **42.00** | **43.82** |

The measured warm paths meet the apply, snapshot, compile, and opening targets. The `set_field` delta is **1,552 bytes** at both p50 and p95, including exact-base snapshot tokens.

**Remaining startup gap:** the cold broker's first open plus snapshot took **760.43 ms** (one startup sample, excluded from the warm-broker percentiles). This remains above the 400 ms opening goal. Opening subsequent documents shares the warm worker, and unchanged documents can reuse their disk compile cache.

## Verification

- **Python:** 295 editor test cases; 293 passed and 2 skipped.
  ```sh
  PYTHONPATH=scripts:tests .venv/bin/python -m unittest discover -s tests -p 'test_editor*.py'
  ```
- **Swift:** all 54 native tests passed.
  ```sh
  xcodebuild -quiet -project editor/ReelsEditor.xcodeproj -scheme ReelsEditor \
    -destination 'platform=macOS,arch=arm64' -derivedDataPath .cache/editor/DerivedData test
  ```
- **Installation:** `./editor/reels-editor setup` succeeded, rebuilding the preview bundle and installing the Release app.
- **Interaction coverage:** automated tests cover gesture bursts, concurrent/replaced leases, queued optimistic edits, exact delta bases, watcher retirement, and service-startup failures. A 42-segment payload regression verifies that changing one node does not resend the other segments.
- **Manual check:** the five-drag app smoke check was not performed in this session.
