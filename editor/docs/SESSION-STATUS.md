# Implementation checkpoint

## Integration into the main repository

The user subsequently authorized merging the implementation into
`/Users/leif/rice/cycling`. Native sources, documentation and entry points now
live in `editor/`; common service/renderer changes share the normal codebase.
All original work in `/Users/leif/rice/cycling-editor` is preserved.

Use `./editor/reels-editor setup`, then `./editor/reels-editor open`. Runtime
code/dependencies, native builds, private snapshots and exports are independent
directories under `~/Library/Application Support/CyclingReelsEditor/`.
No production data or dependency installation is used as an editor target.
See `editor/README.md` and `editor/docs/USAGE.md` for current commands; paths and
development isolation instructions below are historical.

Main's newer big-hook and decorative-exhibit changes were retained and their
new fields added to the typed Inspector. Native tests pass with 25 cases,
including the installed-runtime location/protection contract. An installer
initialization imported 136 registered assets with zero offline media.
The independent installed runtime passed the full 606-test Python suite with
one opt-in skip; the actual sandbox Exact/export fixture passed separately.
Integrated native playback advanced frames 15→30, and setup repeated without
reinstalling or replacing the private workspace. All 461 protected live
data/dependency records remained unchanged; newer main rendering changes were
preserved byte-for-byte. Main HEAD is unchanged. At the user's request, the
integration is being committed on `feature/native-editor-integration`; nothing
was pushed. The development handoffs below predate this integration commit.
The newer art plan opens with 64 nodes (57 decorative exhibits) and the big hook
intact. Authenticated installed-runtime search returned six regex matches and
two grounded enriched rows. No background commands/agents remain active.

---

## Final handoff (supersedes the historical checkpoint below)

- All child agents have handed off. No active agent ownership remains.
- Real laundry Interactive/Exact/private export now **pass**. Decoded video
  frames and decoded audio are identical between Exact and export; peak
  −1.76 dBFS, no clipped samples. No subjective audio claim.
  Report: `.cache/editor/acceptance/real-program-export.json`;
  inspected frame strip: `exact-export-frames.jpg` in that directory.
- Road-to-5 opens with 182 nodes after the progress-data fix.
- Native capture and actual WK frame advancement passed:
  `native-final.playback.json` (Interactive, frames 15→34), and
  `native-final.program.png` shows the real composed footage/captions.
  The whole-window NSBitmap capture omits the GPU video layer; the separate
  WK snapshot is authoritative for that viewport.
- Native refinements finished; parent subsequently fixed wrapped picker labels
  and caption pages being classified as B-roll, separated a Captions lane and
  hid duplicate word/settings strips. Final capture is complete. Latest xcresult confirms
  **24 native tests passed**, no failures or skips.
- **Final full Python suite: 590 tests run, one opt-in skip, no failures.**
  The opt-in sandboxed Exact/export test passed separately. Remotion typecheck
  and protected preview bundle passed. New cache fixtures now isolate both
  `sys.modules` and the package's renderer binding for full-suite test ordering.
- Fixed a second preview cancellation cause: `reload_external()` returns a
  snapshot even without changes; the watcher must compare announced document
  state before calling `_changed`. Added regression coverage.
- Renderer cache writes are now per-renderer namespaces. Workers cannot read
  live chat/MCP secrets, arbitrary checkout files or unregistered originals.
  Actual macOS denials and sandbox Exact/export fixture pass.
- Added explicit Browserslist targets/root scope to avoid home-directory config
  lookup. The document compiler explicitly grants only its necessary private
  inputs and passes the registered-original map to the shared sandbox helper.
- Per-clip staging reuse implemented and tested. Manifest has `mediaCache`
  hits/misses; unchanged clips survive another clip's staging changes.
- Source discovery and preparation share the unsupported-format proxy decision.
- README, `setup.sh --editor`, `USAGE.md` and integration docs are updated.
- New live test exposed an oversized `get_project` tool response: Claude Code
  saved it to a file the restricted model could not read. **Do not count that
  initial manual-wins observation as a successful live conflict test.**
  Added `ai_views.py`, paginated `get_project`/`get_node`, bounded asset/source
  reads and compact mutation/job responses. No unrestricted file tool added.
  The authenticated rerun passed: one typed volume edit on the first attempt,
  a genuinely attempted conflicting edit rejected in favor of the manual
  value, undo restoring the original, and Stop cancelling/draining owned
  launchers without changing the project. Main project is restored.
- A real bounded punch-strength candidate activated with a matching Exact
  preview and visible descriptor; adjusting the new parameter and undo worked.
  A syntax-error candidate preserved source/HEAD, but an unnecessary automatic
  Interactive preparation replaced the Exact preview. Parent fixed renderer
  activation to call `_changed(..., prepare_preview=False)`; the real rerun
  passes all checks, including retaining the Exact preview after failure.
- Short real 360 and photo drafts now pass Exact rendering. Their source plans
  were not committed or changed. Frames were inspected.
- Final authenticated semantic search: six regex hits and two grounded
  enriched rows; no errors after transport pagination changes.
- Native capture has real Source frames, corrected B-roll/Captions lanes,
  a compact toolbar and restored chat history. Program's GPU-backed layer is
  captured separately; playback proof currently records frames 15→34.

No background commands or child agents remain active from this work.

Delivery/reference:
- `docs/native-editor/USAGE.md`: setup/build/run/refresh/recovery.
- `docs/native-editor/ACCEPTANCE.md`: evidence, automated checks and honest
  remaining human-review limits (subjective audio, sustained GUI interaction,
  full foreground/attached-song/loop review).
- `.cache/editor/acceptance/`: reports, app-owned captures and proof scripts.
- Main usable workspace remains `.cache/editor/workspace-current`.
- Bounded renderer test changes are confined to the separate
  `.cache/editor/acceptance/renderer-workspace`; its source/plan were undone.
- Production/original boundaries and no-commit/no-push state below still apply.

---

## Historical checkpoint

Updated during the user's network-drop/compaction request. This is an internal
handoff, **not a claim that all acceptance checks are complete**.

## Non-negotiable boundaries

- Primary working directory: `/Users/leif/rice/cycling-editor`.
- Branch: `feature/native-reel-editor`, isolated worktree from production HEAD
  `22b15ac7842d4eb209670c65e9dc443e9c865e7a`.
- `/Users/leif/rice/cycling` is actively used by the user. Never modify its
  working files, dependencies, settings, outputs, or original footage.
- Original footage is explicitly registered read-only, not symlinked writable.
- Approved plan: `docs/native-editor/PLAN.md` (copied from the user's external
  plan). Common protocol: `docs/native-editor/INTEGRATION.md`.
- User permits at most two Luna and one Astra concurrent agents; Luna has 2M
  TPM and Astra 1M TPM. Large old sessions were interrupted after a 2M TPM
  failure. Use fresh narrow contexts, not the old accumulated sessions.
- No commits or pushes made. Do not discard any work.

## Data and app

- **Use `.cache/editor/workspace-current` for further real acceptance.**
  It has 136 registered assets, zero offline, independent metadata/media copies.
- `.cache/editor/workspace` is the earlier private snapshot/history archive.
  It omitted `library/progress.json` due to an initialization bug; the allowlist
  and compiler input cloning are fixed. An explicit conservative refresh
  (`editor refresh ... --keep-private`) created `workspace-current` with the
  missing progress record. No FTP/weight values were invented.
- Baseline renderer source `ba5e29c6c8b21683c0b10e17` persists privately under
  `code/renderers/<revision>/source`. Missing source revisions fail closed.
- App project:
  `apps/ReelsEditor/ReelsEditor.xcodeproj`, shared scheme `ReelsEditor`.
- Swift sources are under **`apps/ReelsEditor/ReelsEditor/`**, including
  `MediaMonitors.swift` (not directly under `apps/ReelsEditor/`).
- Binary:
  `.cache/editor/DerivedData/Build/Products/Debug/ReelsEditor.app/Contents/MacOS/ReelsEditor`.
- Launch:
  `--repo /Users/leif/rice/cycling-editor --workspace /Users/leif/rice/cycling-editor/.cache/editor/workspace-current --plan plans/2026-11-11-easy-day-laundry.json`.
- Own-window capture flags: `--capture-window <absolute PNG>` and
  `--capture-and-quit`. No desktop-control tools are available; do not pretend
  audio or hands-on GUI interactions have been verified.

## Implemented

- Workspace snapshot/refresh isolation, source identities, safe Project roots,
  no `.env` in editor launcher, direct ingest/prune read-only guards.
- Immutable multi-file transaction history, atomic HEAD/materialization recovery,
  stable IDs, native/MCP operations, actual renderer timing, trim/split/ripple,
  cutaway editing, generated-cue overrides, word correction, undo/redo,
  gesture leases, AI cancellation/conflict checks, offline view cache.
- Core overlays/captions/music/source types now have compiled nodes; caption
  timing is read-only; `assetKind` distinguishes video/photos.
- Service NDJSON, single-broker private flock, short authenticated Unix MCP
  relay (official SDK), read-only capability/range HTTP, jobs/process groups,
  private exports and native save path restrictions.
- Real Pydantic MCP operation schema in `operations.py` (previous opaque dict
  made the first live model guess six invalid operation names).
- Claude CLI uses restricted tools, exact MCP allowlists, denied unexpected
  permissions, normal account authentication, no API-key billing integration,
  no `--bare`, exact chat resume, current fixed system policy.
- Chat prose deltas normalized as `chat.delta`; completed/tool activity as
  `chat.record`; compact UI history as `{records:[{role,text,state}]}`.
- Immutable metadata index; Unicode regex deadlines; semantic Haiku starts
  600ms after query arrival, even after regex matches; grounded passage IDs,
  quotes, filters and late-result checks; same-asset enrichment.
- 31-target typed inspector inventory in `schema.py`, validated extra fields.
- Remotion Player/bridge outside AI-editable `src/`, direct pinned player,
  typecheck/build coverage, static media mapping.
- Sandboxed preview/export/code candidates with immutable source inheritance,
  protected policy/runner/dependency files, descriptors, frozen candidate plan
  and atomic source+plan+preview activation.
- Source proxies now handle 360 video **with optional audio**, 360 photos as PNG,
  and unsupported video containers/codecs as H.264/AAC preserving aspect/time.
- VP9-alpha foregrounds force real Exact preview in the same job, visibly
  returning `mode`, `requestedMode`, `fallbackReason`, and warning.

## Important recent service fixes (rechecked after network drop)

`scripts/reels/editor/service.py` is intact and `git diff --check` passes.

- Native and automatic preview preparation used to cancel each other. It now
  coalesces identical in-flight revision/mode requests and tracks active job ID
  plus preparation generation; stale cancellation cannot mark the new job failed.
- Actual job failures update preview state and emit `preview.error`, keeping the
  last good preview. Prepared URLs are unique immutable capability mappings.
- Preview publication uses `DocumentStore.publish_if_current`.
- Per-document last-ready previews are restored/re-registered on reopen.
- Frozen `candidate_snapshot` is wired before renderer validation; export usage
  is journaled through `commit_export_usage`.
- `export.start` rejects `viewOnly` snapshots; `export.save` only uses completed
  private receipts, refuses production/symlink destinations.
- Project config and permission state are exposed; grants require native action.
- SIGTERM/Close cancels owned processes and uncommitted operations.
- Parent's latest targeted run: **38 service/protocol/operation tests passed**.

`MediaMonitors.swift` was also re-read after the network drop:
it contains Source paused-frame caching/seeking, photos, AVPlayer Exact preview,
the `reelsPreview` handler, revision-tagged postMessage commands with props/static
mapping, and exact origin/capability navigation/message checks.

## Verified so far

- Earlier affected existing contracts: 121 tests passed.
- Core agent's final focused core/overlay run: 83 tests passed.
- Refresh/workspace/transactions combined run: 66 tests passed.
- Parent intermediate combined editor run: 106 tests passed.
- Most recent preview/renderer agent: 18 tests passed (one opt-in skip), plus
  opt-in sandboxed real Exact/export fixture passed independently.
- Remotion typecheck and preview bundle passed multiple times.
- Native app previously built with 17 tests passing; latest UI refinement is
  still an active child task (see below), so rerun the final build.
- Sandbox is **working**, not unsupported: earlier `-6` was dyld ancestor path
  access. Exact ancestor directory grants plus named Chrome Mach/IOKit,
  rendezvous/signaling allowances fixed it without opening original writes,
  arbitrary executables or external network. Actual fixture Exact/export
  produced 850,341-byte movies; live fixture usage stayed untouched.
- Real 136-asset index: ~122ms initial build, warm regex queries ~3–4ms.
- Real authenticated Haiku `laundry` query: six regex hits and two grounded
  enriched rows. Reports under `.cache/editor/acceptance/`.
- Real authenticated main chat changed first clip volume to 0.85 in a validated
  transaction; six earlier guessed shapes were rejected with no commits.
  **Undo restored the original omitted volume.** Typed schemas now fix discovery.
- Real feature plans opened: creatine (275 nodes incl captions/exhibits/blur),
  bike-trip, 360 group-ride, elevator-loop. Road-to-5 exposed the progress-copy
  gap; repeat it using `workspace-current` after the fix.

## Active agent

Only the fresh native refinement agent may still be running:

- `ses_e2363725effem1IuZz6x9pMsHV`, Luna, owns `apps/ReelsEditor/` only.
- Task: paginate all 136 assets, real paused Source poster/seeking, compact
  toolbar, parse chat history/deltas, merge BOTH related/enriched semantic rows
  with Unicode-safe evidence, separate Audio lane from B-roll.
- Told **not to start final capture until parent has made Program preview ready**.
- Do not poll it; await the automatic completion message. Other previous agents
  are completed or explicitly interrupted; do not resume their large contexts.

Latest completed source-parity agent: `ses_e236498e0ffeJFoUrVo05UCt2E`.
It owns no active work now; its changes/tests are saved.

## Next work — prioritize finishing, not expanding scope

1. Resume in this isolated worktree after compaction.
2. Run editor tests after latest parent service changes and source-parity handoff.
   One previous test failure was the old export root; `merge_usage_fields` now
   permits both old private `workspace/exports` receipts and new private
   `workspace.project.out_dir/exports` receipts. Verify it is now green.
3. **Real Program proof remains outstanding.** Use one EditorService against
   `workspace-current`, open the laundry plan, let the automatic Interactive
   preparation finish (Event/Queue wait, not tool polling). Avoid starting
   duplicate preparation; coalescing fix is now in place.
4. Then prepare real Exact preview and export the frozen revision, compare
   representative decoded frames/audio measurements; keep receipts/reports in
   `.cache/editor/acceptance/`. Do not claim hearing the audio.
5. Repeat road-to-5 open to prove progress/Strava metadata cloning.
6. Live conflict/cancellation and bounded renderer-change/failed-candidate proof
   still need finishing. No new generated visuals/audio without scoped consent;
   a harmless existing-effect constant/style change or comment is suitable.
7. Tell native agent Program is ready, or perform app-owned final captures after
   its handoff. Old `native.png`/`native.default.png` show a loaded timeline but
   black Source and cancelled Program; they are **not** playback acceptance.
8. `_asset()` in service currently requests proxies only for 360; align it with
   new source proxy support for unsupported codecs/containers if needed.
9. Inspect per-asset media reuse: current staging cache definitely reuses text-
   only changes; confirm unchanged clips are reused after a different clip trim.
   If not finished, report this limitation rather than claiming full acceptance.
10. Update README/setup/usage/help with build/run, refresh, recovery, mode
    differences, sandbox/code policy, verified/unverified acceptance. Root docs
    currently only PLAN/INTEGRATION/checkpoint; full usage docs are not done.
11. Final tests, Remotion checks, native tests, diff check, concise honest result.

## Useful verification commands

```sh
PYTHONPATH=scripts .venv/bin/python -m unittest discover -s tests -p 'test_editor_*.py' -v
PYTHONPATH=scripts:tests .venv/bin/python -m unittest test_asset_updates test_search test_cutaways test_bleep test_exhibits test_blur test_titlecard test_moment_references test_kino test_mix_streams
REELS_RUN_SANDBOX_RENDER_PROOF=1 PYTHONPATH=scripts:tests .venv/bin/python -m unittest test_editor_preview.EditorPreviewTests.test_sandboxed_exact_preview_and_private_export_fixture -v
npm --prefix remotion run typecheck
npm --prefix remotion run build:editor-preview
xcodebuild -project apps/ReelsEditor/ReelsEditor.xcodeproj -scheme ReelsEditor -configuration Debug -destination 'platform=macOS' -derivedDataPath .cache/editor/DerivedData test
```

Use `.venv/bin/python`, not system `python3` (the latter has incomplete Pillow).
Do not poll background commands or agents; their completion notifications arrive
automatically. Keep test/media creation in owned fixtures/private output only.
