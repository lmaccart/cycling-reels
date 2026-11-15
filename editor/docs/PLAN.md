# Native Reels Editor

> Historical plan from the first build. Superseded: the editor is now fully
> integrated — it edits the real `plans/`, runs on the repo's `.venv` and
> `remotion/`, and there are no private snapshots or separate runtime. Current
> behaviour is in [USAGE.md](USAGE.md) and [INTEGRATION.md](INTEGRATION.md).

## Context

Build a personal macOS video editor over this repository: Premiere-style native panels and direct timeline manipulation, with Claude Code making visible, undoable edits to the same project.

Confirmed choices:
- **SwiftUI + AppKit**, macOS only. UIKit is not the desktop foundation.
- Complete editing of the repository’s existing reel format: trim, split, reorder, layered B-roll, text/captions, audio, existing effects, preview and export. Not an arbitrary-track replacement for every Premiere feature.
- Valid AI edits apply live and highlight their affected clips/properties. Manual edits win conflicts.
- Claude may also change renderer/effect code. Changes activate automatically after validation; checkpoints, undo and the last working preview remain available.
- Use the installed, authenticated Claude Code locally; no separate API-key billing integration.
- Immediate regex footage search, followed after **600 ms idle** by additional results from **`claude-haiku-5-5`**, even when regex already matched. Search uses existing metadata/transcripts only.
- Separate implementation branch/worktree and independent writable project data; original footage is shared read-only.

This is a plan, not an implementation. No production files, dependencies, branches or worktrees are changed during planning. The approved implementation begins with isolation.

## Approach

### 1. Establish the isolated worktree and runtime boundary

Create branch `feature/native-reel-editor` in sibling folder `/Users/leif/rice/cycling-editor`, based on the production checkout’s committed HEAD at execution time:

```sh
BASE=$(git -C /Users/leif/rice/cycling rev-parse HEAD)
git -C /Users/leif/rice/cycling worktree add \
  -b feature/native-reel-editor /Users/leif/rice/cycling-editor "$BASE"
```

Check for an existing branch/path first; do not overwrite, reset or repurpose someone else’s worktree. Do not switch branches, stash, install dependencies, run renders or change Git configuration in production. Registering the worktree necessarily updates shared Git administrative metadata, not production working files.

All subsequent implementation commands use the new worktree as their explicit working directory. Give it its own `.venv`, Remotion dependencies, build outputs and caches. Reuse the dependency definitions in `setup.sh`, not production’s writable dependency directories. Copy the approved plan into this worktree.

Add `./reels editor init --source /Users/leif/rice/cycling --workspace .cache/editor/workspace`. It creates:
- `data/`: a private project root containing plans, config, library metadata, transcripts, telemetry/Strava caches, music metadata, relevant learning records and its own `out/`.
- Independent copies or filesystem clones of ancillary media needed for rendering: music, SFX, exhibits, sheets and thumbnails. Never hard-link writable data back to production.
- A manifest of original footage paths and recorded identities, pointing to production’s `library/media/` without copying the footage.
- Private `history/`, `code/`, `jobs/`, `preview/` and chat/search state.

Snapshot relevant currently uncommitted/untracked **data**, not just Git-tracked files. Do not copy `.env`, Claude credentials, production outputs wholesale or uncommitted implementation code. Validate JSON, references and source-file stability while copying; if production changes during the snapshot, leave the incomplete snapshot inactive and report that initialization needs another attempt. Do not lock or pause production.

Extend `Project` with explicit optional code/media roots while preserving ordinary CLI defaults:
- `root` remains the writable data root.
- `remotion_dir` resolves against the selected code root.
- `media_dir`, `abs()` and `rel()` understand the explicit, read-only originals root and preserve the existing media-relative identifiers.
- `ensure_dirs()` never creates or modifies an externally supplied media root.
- Media-mutating commands reject read-only-media projects before doing work.

Add `--editor-workspace` to editor-aware CLI dispatch; construct the correct `Project` **before** command handling or error recording. `reels` currently overwrites `REELS_HOME` with its own location, and `cli.main()` records some failures through `review.auto_event()`: merely setting an environment variable is insufficient isolation.

No writable symlink to production’s `library/media/`. Resolve registered originals explicitly; protect them at the process boundary as well. If an original disappears or its identity changes, mark it offline, retain the last good preview and block affected exports rather than substituting media.

### 2. Add one trusted local editing service

Implement `scripts/reels/editor/` with separate modules for workspace loading, protocol, transactions/timing, search, preview/jobs and Claude/MCP integration. Register it through the existing CLI rather than making another unrelated launcher.

The Swift app launches `./reels --editor-workspace … editor serve` using `Process` and argument arrays, never shell interpolation. Use versioned newline-delimited JSON on stdin/stdout:
- Requests: request ID, method and parameters.
- Responses: the same request ID and either a result or structured error.
- Events: sequence number, document/query/job identity, revision and payload.

Reserve stdout for this protocol; send logs to stderr. Expected validation failures return structured results, not process crashes or production learning entries.

Use a private, permission-restricted Unix socket for MCP subprocesses to reach the same dispatcher. Keep its pathname short enough for macOS Unix sockets. The official Python `mcp` SDK handles stdio MCP framing; do not implement MCP itself or introduce the Claude Agent SDK unnecessarily.

The service owns mutations and durable revisions. Expensive renderer code runs in separate workers, never in the trusted broker’s interpreter. Jobs are cancellable, identify their input revision, and cannot publish results over a newer revision.

There is no remotely exposed editing API. A separate read-only loopback HTTP server serves preview bundles and registered media. It supports byte ranges, uses a per-launch capability URL, rejects traversal/unregistered paths, and exposes no mutation endpoints.

### 3. Make plan JSON the authoritative document

Keep the existing plan structure and existing renderer semantics. Do not build a second AVFoundation timeline format and translate between competing project models.

Preserve unknown fields on round-trip. Add a versioned `_editor` metadata block for document identity, stable node IDs and renderer revision. Maintain the ID-to-plan-location mapping when arrays change; neither the UI nor Claude addresses clips by a transient array index.

The service returns a compiled editing snapshot containing:
- Document, library and renderer revisions.
- Raw editable values plus resolved asset/moment references.
- Stable node IDs, parent relationships and lane assignments.
- Frame positions, durations, fade overlaps, source bounds and generated events.
- Warnings, provenance and the preview state for that exact revision.

Use `render.build()` and its `Seg.start`, `Seg.frames`, `Seg.trans`, `Seg.end`, `props`, `sound` and `loop` outputs as the timing authority. Beat snapping can retime clips; cutaways are fitted afterward. Do not sum raw source durations in Swift or mistake `loop.swap` diagnostics for a second playback timeline.

Define typed operations shared by native controls and MCP: insert/remove/reorder segments; trim/split; insert/move/resize overlays; set fields/keyframes; correct transcript words; undo/redo. Each atomic operation batch includes the expected document/library/renderer revisions and a human-readable reason.

Apply a batch to a private candidate, validate it, then commit all of it or none of it. Return field/node-specific errors. Never serialize `build()`’s mutated normalized dictionaries back over the user’s raw plan.

Persistence:
- Store immutable JSON revision objects and commit manifests under `history/`; atomically replace a HEAD pointer as the commit point.
- Journal all mutable JSON involved in a transaction, including transcript corrections, so a crash cannot commit half a change.
- Materialize current private plan/metadata files from the committed manifest. Recover unfinished materialization on startup; uncommitted candidates never become current.
- Keep origins/reasons and affected IDs for history and highlighting. Restoring a checkpoint creates a new revision rather than rewriting history.

Concurrency:
- A drag/trim gesture obtains a short document edit lease. Claude can continue thinking, but cannot commit into the gesture.
- Validation may run asynchronously; recheck the expected revisions immediately before committing.
- A stale AI batch is rejected with current revision/conflict information. No silent whole-plan overwrite or last-writer-wins merge.
- One gesture or one successful AI operation batch is one undo step. Undo/redo also changes revision IDs, invalidating stale jobs.
- Detect external edits to private materialized plans; validate/reload them as revisions rather than silently overwriting them.

### 4. Implement the complete reel-format editing surface

Create a macOS app and shared Xcode scheme under `apps/ReelsEditor/`, targeting macOS 14+ with Swift 6. Use SwiftUI for panels/forms and AppKit for split views, keyboard handling and the high-interaction timeline. Personal local deployment; no App Store sandbox/distribution work.

**Workspace:** dense, dark Premiere-style chrome; resizable panel dividers, compact controls, monospaced timecode and restrained lane colors. Left: project/library browser. Center: Source and Program monitors, tabbed at smaller widths and optionally side-by-side when space permits. Right: inspector above persistent chat/change history. Bottom: timeline with useful editing height, not a chat-sized strip. Persist panel proportions and collapsed state. Support a 1280×800 window without hiding transport or undo.

Use native system typography and focus behavior. No decorative dashboard cards, oversized padding or a web-based editor shell. Provide keyboard access, visible focus, accessible names for clips/handles, non-color-only AI-change indicators and reduced-motion behavior.

| Surface/lane | Required editing behavior |
| --- | --- |
| Primary video/photos | Source In/Out, insert, ripple trim/delete, split at playhead, reorder, speed, photo duration/motion, crop/focus, frame mode and transitions. Primary segments remain the schema’s contiguous spine; no invented blank tracks. |
| B-roll/cutaways | Separate visual lane linked to the owning segment; drag, trim and replace source ranges while preserving the underlying voice/captions. |
| Text/captions | Hook, text beats, end card, styling/position/timing, transcript captions and word corrections. Correct `words[].w` without changing recorded word timings. |
| Existing effects | Punch-ins, beat/word-driven punches, blur, 360 view/path/projection, route/stat overlays, progress, exhibits/charts/hand anchoring/foreground depth, and existing permission-gated title cards. |
| Audio | Clip volume, natural-sound windows, bleeps, music selection/start/mix/beat snapping, music drops and SFX. Show generated cues distinctly from explicit cues. |
| Project inspector | Format/spine, loop settings, render dimensions/fps, warnings and caption/post metadata. No publishing, scheduling or prune controls. |

Use an `NSView`-backed virtualized timeline, with cached thumbnails/waveform tiles and a separately updated playhead layer. Do not instantiate a SwiftUI view for every frame or republish the whole document at playback frequency.

Support pointer selection/multiselection, snapping, edge autoscroll, horizontal zoom/scroll, drag handles, Space playback, frame stepping, source I/O, split, delete, undo/redo and focus-aware shortcuts. Dragging a timestamped search result inserts that source range, not an unrelated section of the file.

Implement timing changes centrally in Python:
- Half-open frame intervals and the renderer’s actual fps/rounding rules.
- Distinguish source seconds, segment-local reel seconds and global reel time.
- Trims/splits preserve source-linked material, rebase/clip segment-local events and ripple subsequent global events consistently.
- Splits preserve external transitions and insert an internal cut; regenerate derived word/beat events rather than copying stale derived props.
- Retiming must account for speed and crossfade overlaps. Explicit bleep `at`/`for`, for example, are **segment-local reel seconds**, not source seconds.
- Moving a generated event first materializes an editable override and disables/suppresses its corresponding automatic event, so it does not immediately regenerate as a duplicate.

Use the existing schema/validators as the field inventory, including nested and less-common features. An advanced JSON inspector preserves access to extension fields; it is not a substitute for native controls for existing features.

### 5. Reuse the renderer for preview, without rendering on every keystroke

Add a small preview entry outside the AI-editable composition code, importing the existing `CyclingReel` component. Declare `@remotion/player` directly at the same pinned version as the existing Remotion packages (`4.0.538` at discovery), and add a reproducible preview bundle command/typecheck coverage.

Host only the Program monitor in a nonpersistent `WKWebView`; the editing UI remains native. Bridge typed commands for loading a revision, seek/play/pause and playback rate, plus revision-tagged readiness/frame/error events. Use `PlayerRef.seekTo()`, playback controls and `frameupdate`; never infer playback position from chat events.

Map staged relative media names to the loopback server. Existing components call `staticFile()`, which rejects absolute filesystem paths and remote URLs: provide a revision-specific static-file mapping, not absolute paths injected into `ReelProps`. Restrict navigation and script/message origins; the web preview bridge cannot invoke editing tools.

Preview preparation:
- Run `build()` on private data in a worker. It is **not pure**: beat grids and vision caches may be populated.
- Stage into a fresh revision-owned directory. Existing `stage()` deletes its entire target, so it must never receive a shared/live directory.
- Add a content-addressed staged-media cache, using immutable artifacts and keys covering source identity, range, speed, fps, framing, 360 path, audio processing, relevant metadata and renderer revision.
- Reuse unchanged staged assets. Text/style-only changes update props without recutting video.
- During a drag, update native geometry immediately; commit once at gesture completion. Coalesce/cancel obsolete preparation work, retaining the last good preview until the new one is ready.
- Keep document revision and displayed preview revision visibly distinct while preparation is pending. Export never silently uses an older preview.

Use AVFoundation for the Source monitor and ready rendered movies. Prepare view-specific proxies for unsupported media/360 sources through the existing pipeline; do not pretend AVPlayer can directly reframe `.360` footage.

Provide **Interactive** and **Exact** Program preview modes. Interactive uses the shared Remotion composition. Exact uses the same staged inputs, Remotion renderer and finishing/mix logic as export, without recording footage/music usage. This is needed for export-accurate loudness and any browser incompatibility, notably the existing VP9-alpha foreground layers. If WebKit cannot reproduce a feature correctly, show the reason and use a real rendered Exact preview—never omit the effect or silently substitute a different compositor.

Respect the existing distinction between a baked song and Instagram-attached audio: the audible review may be `preview.mp4`, while the upload artifact is `reel.mp4`. Keep safe-area guides and selection overlays out of exports.

### 6. Connect Claude Code to the same transaction system

Use the installed Claude CLI through `Process`; discovery found version `2.1.325`. Main editing chat uses the CLI’s default model unless the user selects another. Persist the session ID and chat/tool history privately, and resume that exact conversation.

Use the supported print/stream interface, including `--output-format stream-json`, `--verbose` and `--include-partial-messages`. Limit authority with:
- `--restricted`, `--tools ""`, `--disable-slash-commands`, `--no-chrome`.
- `--strict-mcp-config` and an explicit session-local MCP config.
- An exact MCP tool allowlist and noninteractive denial of unexpected permission requests.
- Explicit session settings/context rather than saving project permissions or running `claude mcp add`.

Do **not** use `--bare`: its documented authentication path excludes subscription OAuth/keychain credentials. Do not extract credentials, copy `.env`, enable permission bypass or silently switch to API billing. Claude’s normal account-owned authentication/cache remains its responsibility; no production project settings are written.

Expose narrowly scoped tools:
- Read current project/selection/playhead and asset metadata/transcripts.
- Query the library.
- Apply typed editing operations with expected revisions.
- Read allowed renderer files and submit renderer/effect changes through the validation path below.
- Read job/validation status and request a scoped user permission when needed.

No arbitrary shell, direct production file editing, downloads, publishing, scheduling or pruning tools.

Stream prose/tool activity into chat, but mutate the timeline only on complete, validated service transactions—not partial JSON or filesystem-watcher guesses. Highlight affected nodes/properties and show a concise change entry with the originating request. Stop cancels the CLI process group and uncommitted jobs; already committed edits remain undoable.

Treat transcripts, filenames and source text as data, not instructions. Include the repository’s footage, factual-claim, privacy and approval rules in the fixed editing policy. Code-edit authority does not grant permission for new generated visuals, new audio generation, uploads or publication.

### 7. Implement immediate regex plus debounced Haiku augmentation

Build an immutable in-memory passage index once per library revision, updated when private metadata/transcripts change. Reuse the existing search implementation’s filtering, source ranges, moment membership and low-confidence semantics; do not spawn `./reels search` on every keystroke.

The existing CLI search is token/prefix/phrase matching, **not regex**. Add regex as an editor search path without changing that CLI contract.

Index filenames, summaries, tags, moment labels/descriptions, notes/hooks/landmarks and transcript passages. Transcript `words[{w,s,e,p}]` are authoritative: corrected words may disagree with stale `segments[].text`. Each passage has a stable ID, asset/moment IDs, source bounds, field/evidence text and confidence metadata.

Immediate stage:
- Run case-insensitive Unicode regex off the UI thread on every query/filter change.
- Use the `regex` package with a whole-query deadline and per-match remaining-time bounds; pathological expressions cannot freeze the service.
- Return deterministic matches with highlighted evidence/source ranges. Invalid or over-budget patterns produce an inline explanation, not a crash.
- An empty query shows the filtered library and does not call a model.

Semantic stage:
- After exactly 600 ms without query/filter changes, start one separate, cancellable Claude Code search session using `--model claude-haiku-5-5` and `--no-session-persistence`.
- Run it **even when regex found matches**. Invalid natural-language-as-regex input may still receive semantic results.
- Expose only read-only, metadata-only index tools: paginated catalog access, passage search and passage retrieval. No original-media paths, frames, contact sheets or mutation tools.
- Have Haiku expand the meaning/query, inspect grounded passages and return schema-validated passage IDs, short reasons and quoted evidence. Paginated tools prevent silently restricting search to the first files in a large library.
- Resolve IDs/ranges locally and reject nonexistent IDs, invalid quotes, deleted assets, filter violations or out-of-range results. Do not trust model-supplied paths/timestamps.

Keep regex rows first and stable; append a separately labelled “Related · Haiku” section. Group/deduplicate by asset and source range; additional passages for an existing asset enrich its row rather than duplicating the same file. Do not steal selection or move the timeline playhead when results arrive.

Every request/result carries query generation, filter hash and library revision. Cancel the previous process group immediately on a new query; discard late responses even if cancellation loses a race. Cache semantic results by query, filters, library revision, model and prompt version.

Model/login/rate-limit/network failures leave regex usable and display a specific semantic-search error. The exact model ID is documented, but account access has not been exercised; never silently substitute another model.

### 8. Safely activate renderer/effect code revisions

Code changes are persistent, per-project immutable source revisions under the editor workspace. They do not modify production, the broker, the running Swift app or unrelated projects.

Allow the composition/effect source area (`remotion/src/`) and an explicit set of renderer/effect Python modules, plus a dedicated area for new effect modules/tests. Protect launcher, workspace/protocol, authentication, provenance policy, validation runner, dependency manifests and Git/settings files. Resolve every requested path against the allowlist; reject symlinks/traversal.

A source-edit tool may submit an accompanying plan-operation batch so a new effect and its first use activate together. New configurable parameters include a declarative inspector descriptor (number, boolean, text, enum, color, timing/keyframe controls); no arbitrary native UI execution.

For each candidate:
1. Create an isolated source/data snapshot; never import candidate Python into the broker.
2. Run syntax/type/bundle checks and the fixed renderer contract tests appropriate to the changed modules. Candidate tests supplement, never replace or edit, trusted validation.
3. Run the current candidate plan through preparation and a real draft render, including finishing/audio paths. Validate the preview bridge and descriptor schema.
4. Recheck document/library/renderer revisions. If still current, checkpoint and atomically activate the source revision, associated plan change and matching preview. Otherwise return a conflict without overwriting the user’s intervening edit.

Run workers under a tested macOS process sandbox with only necessary reads, revision-owned writes, allowlisted subprocesses and loopback access needed by Remotion; deny production/shared-original writes and external network access. The app itself may launch local tools, but renderer code does not inherit that unrestricted authority. Run an original-write-denial capability probe against an isolated fixture before permitting source activation.

Validation is a correctness check, not proof arbitrary code is benign; filesystem/process/network boundaries are separate requirements. If that boundary cannot be enforced on the installed macOS, source activation must fail closed with a diagnostic, not run unsandboxed.

Compilation, tests, staging, render or web-preview failures leave the previous renderer/preview active and return actionable errors to Claude. Renderer revision participates in every cache key and export receipt. Missing recorded source revisions block reopening/export rather than falling back to different code.

### 9. Complete save/export, recovery and delivery

Autosave each committed revision. Reopening restores plan, stable IDs, source revision, panel state and chat/session identity; unfinished jobs restart only when needed and cannot impersonate completed exports.

Export freezes a specific document/library/renderer revision and runs the existing `./reels render` path with that revision’s code and a private input snapshot. Keep `render_plan()`’s output, post metadata, lineage, captions and usage accounting for **export**, not scrubbing. Merge export bookkeeping only into private metadata, without overwriting newer edits.

Output remains under the editor’s private `out/`; copying a finished artifact elsewhere is an explicit native save action that cannot target production. Store revision/input identity with the export. Opening an exported review does not mark it approved, scheduled or posted.

Handle missing dependencies/auth, offline media, invalid plan/code, interrupted workers, disk-full writes and stale previews as visible states with an intact last committed project. Close/Stop terminates owned subprocess groups and removes only uncommitted, revision-owned scratch data.

After the implementation smoke proof, update setup/usage documentation and CLI help in the new worktree with build/run instructions, snapshot refresh, preview distinctions, code-change policy and recovery. Do not rewrite production skills or content rules.

## Verification

These are implementation acceptance checks, **not checks already run during planning**.

### Automated checks

Use the existing `unittest`/`tests.helpers.TempProject` conventions. Add focused `test_editor_workspace`, `test_editor_transactions`, `test_editor_search`, `test_editor_preview` and `test_editor_renderer` coverage for:
- Originals/production write denial, symlink escape rejection and private failure/usage recording.
- Plan round-trip without field loss; stable identities across reorder; atomic multi-file recovery.
- Trim/split/ripple at differing speeds and fade overlaps; segment-local bleeps, nested cutaways, explicit versus generated cues, and beat-retimed bounds.
- A manual gesture or edit invalidating an in-flight AI commit; undo invalidating stale preview/export work.
- Corrected transcript words, regex errors/timeouts, the 600 ms trigger, semantic augmentation after nonempty regex results, filters/deduplication and late-result rejection.
- Cache invalidation on source/range/effect changes, text-only media reuse, alpha/exact-preview handling and retaining the working code revision after candidate failure.

Keep automated model tests deterministic with captured protocol records and a controllable clock; do not require network, Claude credentials or private footage in the unit suite. Real authenticated search/editing and actual footage playback are separate acceptance scenarios.

Run the new Python tests plus the existing contract tests affected by shared changes, then the Remotion typecheck/preview bundle and native tests. For the proposed file names:

```sh
PYTHONPATH=scripts .venv/bin/python -m unittest discover -s tests -p 'test_editor_*.py' -v
PYTHONPATH=scripts:tests .venv/bin/python -m unittest \
  test_asset_updates test_search test_cutaways test_bleep test_exhibits \
  test_blur test_titlecard test_moment_references test_kino test_mix_streams
npm --prefix remotion run typecheck
npm --prefix remotion run build:editor-preview
xcodebuild -project apps/ReelsEditor/ReelsEditor.xcodeproj \
  -scheme ReelsEditor -configuration Debug -destination 'platform=macOS' \
  -derivedDataPath .cache/editor/DerivedData test
```

### Actual app acceptance

Launch the built `.app` against the private snapshot and inspect the real native surface at normal and minimum window sizes.

1. Open existing real plans covering voice/captions, photos, B-roll, music/SFX, transitions, 360 and exhibits/foregrounds. Verify actual frames and audio, not only successful process exit.
2. Find a clip by regex; stop typing and see additional grounded Haiku matches without disturbing selection. Change the query during an active request and confirm old matches never appear under the new query.
3. Insert the chosen source range; trim, split, reorder, move an overlay and adjust audio from the timeline/inspector. Undo/redo and reopen; confirm the same project and source ranges return.
4. Ask Claude to edit the reel. Observe incremental validated highlights and playable revisions. Make a conflicting manual edit during generation; confirm it survives.
5. Ask for a bounded renderer/effect change. Observe validation, automatic activation, native parameter adjustment and undo. Submit a failing candidate and confirm the working preview remains intact.
6. Export the selected revision. Compare representative cut/overlay/360/foreground frames and audio against Exact preview, including the attached-song distinction and loop seam behavior.
7. Interrupt the service during a pending edit/render and reopen. Verify the last committed document, no partial export presented as complete, and no orphan owned processes.
8. Verify the production-write boundary using an isolated fixture plus the actual processes’ configured roots/sandbox. Do not use a live production Git diff as proof while unrelated production work continues.

Interactive editing should remain responsive while validation/rendering/search runs. Measure regex response and seek/drag responsiveness on the real library; the target is immediate local results, not an unmeasured performance claim.

## Critical existing integration points

1. **`reels` / `scripts/reels/config.py`** — launcher root/`.env` behavior and `Project` path resolution define the isolation boundary; a worktree alone does not provide ignored runtime data or dependencies.
2. **`scripts/reels/cli.py`** — constructs `Project` before dispatch and can persist failure events. New service/workspace paths must not fall through to production defaults.
3. **`scripts/reels/render.py`** — `build()` mutates/caches, `stage()` clears its destination, and `render_plan()` writes usage/lineage/post state. Separate editing preparation from export side effects.
4. **`scripts/reels/search.py` / `tests/test_search.py`** — existing filter/range/confidence semantics and authoritative corrected words must survive the new regex index.
5. **`remotion/src/CyclingReel.tsx` / `components/Exhibit.tsx`** — reuse the real composition; transparent foreground video and final audio processing require explicit preview-parity handling.

## Assumptions and boundaries

- This is a local, single-user reel editor—not cloud collaboration, an iPad app, a general-purpose unlimited-track NLE, or a publishing client.
- The initial snapshot is independent of subsequent production edits. Refresh is explicit and must reconcile private edits; no background bidirectional synchronization.
- Existing assets/content permissions carry over. Do not recheck licences for user-supplied content, invent factual claims, expose other athletes’ data, or relax route-privacy rules.
- Permission to edit effect code is not blanket approval to generate visuals or sound. New generated material needs the existing scoped conversational consent.
- Full export remains the repository’s renderer. Kino is interaction/reference material, not an upload destination or export backend.
- Xcode 27.2 / Swift 6.4.2, ffmpeg/ffprobe, Node/npm, Claude Code and `sandbox-exec` were found during discovery. The app, sandbox profile, WKWebView parity and account access to the requested model still require the implementation checks above.

References consulted: [Kino](https://kino.ai/), the repository’s Kino/plan-schema references, [Remotion Player](https://www.remotion.dev/docs/player/player), [Claude model documentation](https://platform.claude.com/docs/en/models/overview), installed `claude --help`, and the [official Python MCP SDK](https://py.sdk.modelcontextprotocol.io/get-started/first-steps/).
