# Native Reels Editor

A local macOS editor for the existing cycling-reels plan format. Manual actions
and Claude edits go through the same revision-checked, undoable transactions.
The reel JSON—not a second timeline format—remains authoritative.

## Open the integrated editor

The app source and launcher now live in `~/rice/cycling/editor/`. The service and
renderer are shared with this codebase; a versioned copy runs independently.

```bash
cd ~/rice/cycling
./editor/reels-editor setup        # first run, or after source changes
./editor/reels-editor open
./editor/reels-editor open plans/2026-11-11-easy-day-laundry.json
```

After setup, double-clicking `editor/Open Editor.command` is equivalent to
`open` above. The native app reopens its last plan, or shows Projects so you can
choose one.

`setup` installs only into
`~/Library/Application Support/CyclingReelsEditor/`. It builds the app, installs
independent Python/Remotion/browser dependencies and initializes a private
snapshot of the current main project. It never uses the live `.venv` or
`remotion/node_modules`. Run `./editor/reels-editor info` for exact paths.
Previous runtime versions, snapshots and their history are retained.

## Prerequisites and isolation

- macOS 14 or later and full Xcode with the macOS SDK and Swift 6 support.
- Python 3.10–3.13, Node 18+, ffmpeg/ffprobe, and `sandbox-exec`.
- The installer's independent `.venv`, `remotion/node_modules`, Chrome Headless
  Shell, caches and build products. Do not link production dependencies to them.
- Optional: locally installed, authenticated Claude Code for chat and semantic
  search. Sign in through Claude Code; the editor does not request API keys.

The supported integrated setup command is:

```bash
cd ~/rice/cycling
./editor/reels-editor setup
```

Editor setup installs local dependencies and the rendering browser, checks the
Remotion template, and builds the preview bundle. It does not ingest footage,
download Whisper or create production config. The integrated launcher explicitly
snapshots the current checkout after building. Missing system tools are reported
rather than installed globally by this mode.

## Additional private projects (advanced)

Setup already initializes the default workspace. Existing low-level `reels editor`
commands are routed to the installed runtime before the main `.env` or `.venv`
is loaded. To create another workspace, use a new path **outside** the live
checkout:

```bash
./reels editor init \
  --source /path/to/cycling \
  --workspace "$HOME/Library/Application Support/CyclingReelsEditor/my-workspace"

./reels --editor-workspace "$HOME/Library/Application Support/CyclingReelsEditor/my-workspace" editor info
```

The destination must be new. Plans, transcripts, asset records, relevant metadata,
music, SFX and exhibits are independent copies. Original footage is registered by
path and file identity and shared read-only. Credentials, `.env`, source-code
settings, existing renders and production dependencies are not imported.

There is no background synchronization or automatic write-back to production.
Changed or missing originals are reported, not silently replaced. Keep the
production originals in their registered location.

Open it with `./editor/reels-editor open --workspace "/absolute/workspace/path"`.

## Launch and service

Pass an optional `plans/<existing-plan>.json` to `editor/reels-editor open`;
without it the app can reopen its previous plan. Only one editing
service may mutate a workspace at a time; use another private workspace for a
second independent editor.

The app launches the local service itself. For protocol development only:

```bash
./reels --editor-workspace "/absolute/private/workspace" editor serve
```

This uses versioned newline-delimited JSON on stdin/stdout. Do not run it beside
the app against the same workspace. See [INTEGRATION.md](INTEGRATION.md).

## Editing

- Browse the Library or search. Regex results appear locally; after 600 ms idle,
  a separate read-only Haiku request adds grounded related evidence. Semantic
  errors leave regex results available. Selection and playhead do not jump when
  later results arrive.
- Select a source, choose its source range and insert it into the primary reel,
  or add it as B-roll over the selected primary segment. B-roll does not replace
  the primary voice/caption spine.
- Trim, split, reorder, move overlays and change typed Inspector values. Times
  are resolved by the renderer, including speed, fades and beat timing. Generated
  caption timing is read-only; word corrections preserve original word times.
- Each validated gesture or operation batch autosaves as one transaction.
  Undo/redo covers both manual and Claude changes. Failed edits leave the last
  committed revision intact.
- Source video uses AVFoundation; still photos are images. Unsupported formats
  and 360 media use private proxies. Preparing a full 360 source can be expensive.

### Claude

Chat uses the existing Claude Code account authentication and a restricted local
MCP tool set. It cannot run arbitrary shell, publish, upload, prune originals or
self-approve permissions. Project `.env` files and API-key billing variables are
not used for this integration.

Semantic search requests exactly `claude-haiku-5-5`; there is no silent model
substitution. Chat uses the authenticated CLI's selected model unless explicitly
configured otherwise. Login, model-access, network and rate-limit failures are
shown separately.

Manual gestures take priority over stale/conflicting AI work. Stop cancels
pending work; already committed edits remain visible and undoable. New generated
visuals/audio still require explicit scoped consent; authority to edit effect
code is not blanket permission to generate content.

Project/node/asset reads are paginated to stay within Claude Code's tool-output
limit. Large summaries are explicitly labelled, and raw fields remain accessible
through bounded read tools; the integration does not grant general filesystem
access to recover oversized tool results.

## Preview modes

| Mode | What it means |
| --- | --- |
| **Source** | Registered original or private decoding/reframing proxy; no reel overlays or final mix. |
| **Interactive Program** | Protected Remotion Player bridge with the real composition and staged media. Suitable for seeking/editing, not final audiovisual sign-off. |
| **Exact Program** | Rendered movie using the finishing/audio path. Use this to compare with export. |

Revision labels distinguish ready, preparing, stale and failed states. A failed
preparation keeps the last good preview rather than presenting it as current.
Transparent foreground overlays automatically require Exact rendering because
WebKit alpha playback cannot be assumed correct. A visible reason accompanies
that fallback.

An attached Instagram song differs from a baked-in song: the upload artifact may
omit music that Instagram will attach, while an audible review includes it. Check
the selected export artifact and its post metadata rather than treating every
MP4 as interchangeable. Still-frame and loudness measurements do not establish
that someone has watched motion or listened to the mix.

## Renderer/effect changes

Claude may submit changes only to allowed effect/composition source areas.
Launcher, broker, provenance/privacy policy, authentication, dependencies,
preview bridge and trusted tests are protected.

Each candidate receives immutable source and private input snapshots, fixed
contract/type/bundle checks, and a real draft render inside the macOS sandbox.
Original/production writes and external network are denied; only necessary local
rendering access is allowed. The boundary is tested on an isolated original-write
fixture. Failure to enforce it blocks activation—there is no unsandboxed retry.

Source, accompanying plan operations and the matching preview activate together
only if their input revisions are still current. Invalid/stale candidates keep
the previous renderer and preview. Extra Inspector controls are declarative,
not executable native UI. Undo restores the previous recorded source revision.

## Export and save

Export freezes document, library and renderer revisions and runs the existing
renderer against private data. Finished artifacts, post metadata, captions and
lineage stay under:

```text
<workspace>/data/out/exports/<export-id>/
```

Usage bookkeeping is applied only to private metadata. Opening/exporting a reel
does not approve, schedule or publish it.

Use the native Save action to copy a completed reel or audible review elsewhere.
The save operation refuses production paths and symlink destinations; replacement
requires confirmation. An incomplete/interrupted job is not a completed export.

## Refresh without losing private edits

Close the app first. The launcher imports main-project updates into a new
snapshot and switches to it only after successful validation:

```bash
./editor/reels-editor refresh --keep-private
```

This keeps conflicting private files while importing new files. It does **not**
overwrite a changed private plan merely because its production counterpart was
updated. Running `refresh` without `--keep-private` stops on conflicts and prints
a report. Pass `--resolutions /absolute/choices.json` to choose individual files.

For advanced explicit destinations, use the low-level command with external
paths obtained from `editor/reels-editor info`:

Production changes are imported explicitly into a **new** workspace:

```bash
./reels editor refresh \
  --source /path/to/cycling \
  --workspace "/absolute/current/private/workspace" \
  --into "/absolute/new/private/workspace"
```

Differing files require choices; an unresolved candidate stays inactive and the
command prints a conflict-report path. Supply a JSON map of relative data paths:

```json
{
  "plans/example.json": "private",
  "library/progress.json": "source"
}
```

Pass it with `--resolutions /path/to/choices.json`, or explicitly choose
`--keep-private` to retain conflicting private files while adding new source
files. This is conservative file-level reconciliation, not a three-way merge.
After a low-level refresh, open with `editor/reels-editor open --workspace` and
the new path. The previous workspace
remains the full history archive; the new one starts fresh history while retaining
current plan/node identities and referenced renderer sources.

## Recovery

- **Service interrupted:** reopen the same workspace. Journal recovery restores
  the committed state; interrupted jobs cannot masquerade as completed exports.
  Do not delete `service.lock` to bypass an active editor—its process lock releases
  when the owning service exits.
- **Offline media:** a committed cached view/last-good preview can remain
  available read-only. Reconnect the recorded original before editing/exporting;
  never substitute a similarly named file.
- **Missing renderer revision:** restore that workspace's recorded
  `code/renderers/<revision>` from backup. Do not point it at current checkout
  code; reopening/export fails closed rather than changing the result.
- **Validation or preview error:** inspect the visible error and retry after
  correcting its cause. Manual editing and previous committed revisions remain
  separate from failed candidate work.
- **Disk full:** free space outside original footage/active history, then reopen
  and retry. Do not manually remove transaction journals or recorded sources.
- **Snapshot conflict/race:** review its report and retry into another new
  destination after stopping edits. Neither source nor existing workspace is
  overwritten.

Back up the entire private workspace, including its `data`, `history` and
`code/renderers`, alongside the separately retained originals.

## Verification

Use the runtime path shown by `./editor/reels-editor info` for Python/Remotion
checks, not the production dependencies:

```bash
cd "/installed/runtime/path"
PYTHONPATH=scripts:tests .venv/bin/python -m unittest discover -s tests
npm --prefix remotion run typecheck
npm --prefix remotion run build:editor-preview
cd ~/rice/cycling
xcodebuild -project editor/ReelsEditor.xcodeproj \
  -scheme ReelsEditor -configuration Debug -destination 'platform=macOS' \
  -derivedDataPath .cache/editor/DerivedData test
```

The optional actual sandbox/render fixture requires macOS and installed local
rendering dependencies:

```bash
REELS_RUN_SANDBOX_RENDER_PROOF=1 PYTHONPATH=scripts:tests .venv/bin/python -m unittest \
  test_editor_preview.EditorPreviewTests.test_sandboxed_exact_preview_and_private_export_fixture
```

Network/model tests and real-footage playback are separate acceptance checks;
the deterministic unit suite needs neither credentials nor private footage.

For app-owned development captures, invoke the installed app binary with its
installed runtime `--repo` and private `--workspace`, then append
`--capture-window /absolute/path.png
--capture-playback --capture-and-quit` to the app command. It waits for real
Program frame advancement, records `.playback.json`, and saves the actual WebKit
viewport separately as `.program.png`. The whole-window bitmap may omit
GPU-backed video layers; a file's existence alone is not playback proof. The
diagnostic does not claim that anyone listened to the audio.
