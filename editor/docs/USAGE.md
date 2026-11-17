# Native Reels Editor

A local macOS editor for the existing cycling-reels plan format. Manual actions
and Claude edits go through the same revision-checked, undoable transactions.
The reel JSON—not a second timeline format—remains authoritative.

## Open the editor

The app source and launcher live in `~/rice/cycling/editor/`. The editing service
is part of the normal `reels` CLI (`./reels editor ...`), runs on the repo's own
`.venv` and `remotion/`, and works directly on this folder.

```bash
cd ~/rice/cycling
./editor/reels-editor setup        # first run, or after source changes
./editor/reels-editor open         # or open the app from ~/Applications
./editor/reels-editor open plans/2026-11-11-easy-day-laundry.json
./editor/reels-editor info         # where everything lives
```

`setup` installs `requirements.txt` into `.venv`, runs `npm install` in `remotion/`
if needed, builds the editor preview bundle and the app (Release), replaces
`~/Applications/Reels Editor.app` and records this folder for the app
(`defaults write com.cycling.ReelsEditor reelsEditor.repositoryRoot <folder>`).
To keep it in the Dock, open it once, then right-click its icon, Options, Keep in Dock.

The app finds the folder from `--repo`, then that saved setting, then asks you to
choose your cycling folder (it must contain `reels` and `plans/`).

## Prerequisites

- macOS 14 or later and full Xcode with the macOS SDK and Swift 6 support.
- Python 3.10–3.13, Node 18+, ffmpeg/ffprobe, and `sandbox-exec`.
- Optional: locally installed, authenticated Claude Code for chat and semantic
  search. Sign in through Claude Code; the editor does not request API keys.
  Editor commands never source `.env`.

## What is live

- Projects are the real `plans/*.json`. The picker lists them with title, post
  date, format, length and status (from `out/<slug>/post.json` when rendered).
  New Reel creates `plans/<date>-<title>.json` with no segments (never overwriting
  a plan).
- `library/media/` is read-only to the editor. Nothing under `library/` or
  `plans/` is ever deleted by it. Transcript word corrections and export usage
  records are the only edits to library metadata.
- Import copies files into `inbox/` (hidden files and `.LRV`/`.THM` sidecars are
  skipped, folders are searched), then runs `./reels ingest` as a cancellable job
  with its output shown live. Your source files are never moved or removed.
- Terminal Claude Code or manual edits to `plans/` show up in the app within a
  couple of seconds. `.cache/editor/live/context.json` shows a terminal session what is
  open (plan, selection, playhead).
- Private state (undo history, jobs, previews, chat, `service.lock`) is in
  `.cache/editor/live/`. Only one editing service runs per folder.
- The renderer is the repo's current `remotion/` and `scripts/reels` source; when it
  changes, a plan starts a fresh undo history. Renderer code cannot be changed from the app.

## Launch and service

The app runs `<repo>/reels editor serve` itself (newline-delimited JSON on
stdin/stdout, working directory = the repo). For protocol development only:

```bash
./reels editor serve
```

Do not run it beside the app on the same folder. See [INTEGRATION.md](INTEGRATION.md).

## Editing

- Browse the Library or search. Library search uses local text matching only; it
  does not make a semantic-model request. Results do not move selection or playhead.
- Select a source, choose its source range and insert it into the primary reel,
  or add it as B-roll over the selected primary segment. B-roll does not replace
  the primary voice/caption spine.
- Trim, split, reorder, move overlays and change typed Inspector values. Times
  are resolved by the renderer, including speed, fades and beat timing. Generated
  caption timing is read-only; word corrections preserve original word times.
  In the timeline, **⌘K** splits selected clips, or every editable clip under the
  playhead when nothing is selected. **C** selects the razor tool (click a clip
  to split); **V** returns to selection. **B** is not a split shortcut.
- Right-click a clip for Split here / Split at playhead, Delete / Ripple delete,
  Duplicate, Copy / Paste, speed presets or custom speed, Enable / Disable, Flag
  source range, Aim 360° when the source probe identifies a sphere, and Reveal
  in Source. **⌘C / ⌘V** copy and paste primary clips at the playhead. Flagged
  Library rows can be dragged to the timeline; the flagged source In/Out range
  is preserved.
- Disabled clips remain visible and editable at their full timeline positions.
  Their picture and sound are omitted from preview and render; linked nodes stay
  on the timeline. Editing navigation reaches the complete editing extent,
  including disabled tails, while playback/export ends with rendered media.
  Paused seeks keep that editing position even when preview must show the next
  enabled boundary (or the rendered endpoint for a disabled tail). Playing maps
  the compact preview clock back to the matching editing clip.
  With every clip disabled, Program shows “Nothing to preview” and releases
  picture and sound; transport cannot resume the previous movie.
- The timeline shows waveforms only (no spectrogram mode). Use the logarithmic
  zoom slider, plus/minus controls, Fit, or the bottom zoom scroller.
- **Colour** (Inspector, at reel, clip and project level): the look defaults to
  **none**; house or moody can be selected explicitly. Strength, grey-day rescue,
  warmth and exposure remain available. A slider commits when you let go, as one
  undoable edit; the arrow button resets it. Clip settings override the reel's,
  which override the project default (`config.json` `render.grade`). The
  colour-space correction remains enabled; the preview re-stages changed clips.
- Drag the timeline playhead or ruler to scrub: the Program preview updates
  while the mouse is held down. The final playhead position is saved on release.
  Clicking or dragging anywhere else in the tracks never moves the playhead.
  Timeline drawing pauses while the window is occluded and refreshes when it
  becomes visible again; playback continues.
- Drag across empty track space to draw a selection box, like Premiere: every
  clip it touches is selected (locked tracks are skipped). Hold ⌘ or ⇧ to add
  to the current selection. A plain click on empty space deselects.
- Each validated gesture or operation batch autosaves as one transaction.
  Undo/redo covers both manual and Claude changes. Failed edits leave the last
  committed revision intact.
  Optimistic trim/delete timing can shrink both extents. Sequential deletion
  preserves fade-adjusted timing; positioned ripple preserves only real enabled
  transition overlaps and does not predict edits blocked by crossing clips.
- Source video uses AVFoundation; still photos are images. Unsupported formats
  and 360 media use private proxies.
- A 360 clip in Source shows **Front** and **Back** side by side, both level with
  the horizon. Front is the direction of travel, measured from the camera's
  accelerometer against GPS speed changes, so it works whichever way the camera
  was mounted; clips without GPS use their tagged angles, then the lens. The
  first open prepares a private proxy; subsequent opens reuse it. Supported
  spherical video uses native Metal reprojection and hardware encoding. Mark
  In/Out as usual, then **Aim 360°** on the timeline to pick the exact angle.

### Flagging clips

- In the Source monitor, **Flag** marks the selected clip to come back to: the
  In–Out range (In + 5 s when only In is set, the whole clip when neither is),
  with an optional note.
- Flags appear under **Flagged** at the top of the Footage list (and a small
  flag on the clip's row). Click one to open it in Source with its range marked
  In/Out; right-click to edit the note or remove it.
- Drag a Flagged row directly onto an unlocked video track to use its saved
  In/Out range at the drop time. No prior click or Source selection is needed;
  clicking the row still opens the range without inserting it.
- Flags live in `flags.json`, shared with `./reels flag` / `./reels flags` and
  Claude Code. They never change footage, tags or plans.

### Aiming 360 clips

Select a 360 clip on the timeline and click **Aim 360°** in the Program header.
The Program monitor becomes a live view into the sphere (a private equirectangular
proxy of the clip plus a little either side; the first open takes a few seconds,
then it is reused). It uses the renderer's exact v360 math, so what you see is
what renders.

- **Drag** the picture to aim; **scroll or pinch** to zoom (field of view).
- With no keyframes, aiming sets the clip's one angle (`view`).
- **Add keyframe** (or double-click the picture) stores the angle at the
  playhead in the clip's `path`. Once there are keyframes, aiming at a keyframe
  updates it and aiming between keyframes adds one. Keyframes must be 0.1 s apart.
  Between keyframes the camera turns the shortest way (no axis spins the long
  way round), exactly as the render does.
- **Hard cut** makes the camera jump to that keyframe's angle instead of panning
  there. The strip under the picture shows keyframes (filled = hard cut); click
  one to jump to it.
- **Angles** lists the clip's tagged angles, **Level horizon** (needs camera
  gravity data) and **Remove all keyframes**.
- **Stabilize** (Off / Light / Medium / Strong / Lock) cancels shake using the
  GoPro MAX's own orientation track. Light follows turns closely, Strong
  smooths more, Lock holds the direction the camera faced at the clip's start.
  It costs nothing in picture (the sphere is rotated, not cropped), but
  stabilized clips take a few seconds per second of footage to stage.
- While the panel is open it follows the timeline: selecting another 360 clip
  aims that one, and with nothing selected it aims the 360 clip under the
  playhead. Splitting a selected clip moves selection and aim to the new right
  piece when the playhead is there. Undo/redo restores the corresponding selection
  and aim target through the normal selection/Inspector pathway, including the
  service and terminal context. Clearing the selection keeps playhead-following active across
  aim edits and refreshes. Split playback retains the original path's easing
  intervals and hard cuts; bounded start/end keys are editing handles, not a
  replacement playback path.
  Cutting within 100 ms of an original key is allowed: the nearby key may be
  omitted from the editable handles, but stays unchanged in the playback window.
  Repeated cuts retain that same window and advance only the piece's offset.
  Editable key times use the compiler's millisecond grid and 100 ms minimum gap.
  Aim edits rebase the whole camera window (keys, offset and duration) to a
  nonnegative integer-second editing origin, so fractional and negative offsets
  stay editable and local hard cuts retain their exact frame. Exterior window
  keys retain fractional coordinates: `key.at - offsetSeconds` is invariant,
  including when a later trim reveals them; those keys are not rounded again.
  Hard-cut comparisons tolerate 1 ps of floating-point translation error, not a
  frame or an editable-key gap; stored playback times are unchanged.
  Named/string and sparse keys retain compiler-resolved tagged/moment defaults
  and exterior poses when clearing old angle overrides. Ordinary trims, splits
  and cutaway slices sample that same resolved camera, not empty-tag defaults.
  Deleting an endpoint key holds the nearest remaining pose.
  Tiny-planet paths interpolate after applying their fixed downward pitch.
  Loop seams and automatic camera-move cues compare quaternion rotation, not Euler
  readouts at the poles; FOV is checked separately. Camera-move cues use a 20° rotation
  threshold on any axis (including roll), or a 25° FOV change.
- A **Custom** stabilize value (a number of seconds set in the plan) is shown
  with that same smoothing, not as Medium.
- `scripts/reels/editor/bench_sphere_keys.py` stages an isolated temporary plan and
  compares keyframe/midpoint frames with single-frame v360 references. It resolves
  abbreviated asset IDs against the original library before copying metadata;
  originals and the input plan remain read-only.
- Space plays the clip in the 360 view; **Done** returns to the Program preview.

### Preparing 360 proxies

`./reels prewarm-360 <asset-id>` prepares the same front/back browsing and
whole-clip aim caches as the installed service, with aim ranges up to 180 s.
No arguments resumes the pending ingest queue; `--all` is an explicit whole-library
backfill, not a routine verification command. Originals remain read-only.

Supported 8-bit SDR BT.709 footage uses Metal plus hardware H.264 (HEVC fallback).
Other formats or missing helpers use ffmpeg/v360 with an explicit reason.
Prewarm logs and service results expose the actual backend/encoder. Front/back
is 1440×1280; aim is an unstabilized 2048×1024 sphere; both are 30 fps, 8 Mb/s
target/12 Mb/s ceiling, GOP 10, no B-frames. Ordinary photos/compatibility proxies
are unchanged.

Cache version 5 separates the new pixel/codec contract from older proxies,
without invalidating proxies for unrelated code edits. A covering v5 aim range
opens split pieces without re-encoding; stabilization uses each requested piece's
own clock. Workers share inherited process locks and finite disk reservations
with prewarm, preserving the 50 GB floor. Cached editor media remains playable
below that floor, with missing corrections computed without a disk write.
`./reels bench-proxy-metal --asset <id> --seconds 30 --report /tmp/proxies.json`
checks pre-encode parity ≥35 dB and native-vs-W5b quality against the same raw
v360 reference (Y and average, ≤0.10 dB deficit per sample), plus ≥2× original
full aim preparation. Encoded-to-encoded PSNR stays diagnostic: its old ≥35 dB
proxy gate failed and was explicitly replaced, not passed. W7 render's encoded
≥35 dB gate is unchanged. Proof readback is bounded, opt-in and measured
separately from normal cold preparation; temporary derived footage is deleted.
See [measured proxy performance and fallback details](../README.md#metal-proxy-preparation).


### Footage dragging

- Drag footage horizontally to a frame position or vertically between V1, V2,
  and other video tracks. Higher video tracks appear above lower footage.
- A normal drop is an **overwrite** on the destination track: covered footage is
  removed from that interval, with surviving heads and tails kept as editable clips.
  Footage on other video tracks keeps its placement.
- The moved clip leaves a gap at its old position. Moving past the endpoint
  extends the reel; surrounding clips keep their timing.
- With snapping enabled, a vertical move snaps back to the clip's original time.
  The drag ghost shows the destination and affected interval before release.
- Drop Library or Source footage directly onto the desired video row and time.
  Source-monitor drops carry the chosen source range.
- Locked tracks reject changes. Source audio stays linked to its footage; an A1
  lock also protects edits that would change that audio.
- One drop is one validated autosave and one undo step. Command-drag insert mode
  is currently unavailable; ordinary footage dragging uses overwrite mode.

See [Premiere-style timeline behavior and research](PREMIERE-TIMELINE.md) for the
editing rules and source references.

### Claude

Chat uses the existing Claude Code account authentication and a restricted local
MCP tool set. It cannot run arbitrary shell, publish, upload, prune originals or
self-approve permissions. Project `.env` files and API-key billing variables are
not used for this integration.


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

## Renderer changes

The app does not change renderer or effect code (`renderer.submit` returns an
error). Ask Claude Code in the terminal; code changes need your approval and tests.

## Export

Export freezes the document, library and renderer revisions and renders the plan
with the normal pipeline inside a macOS sandbox (no network, no `.env`, no writes
to `library/`). The result is published to `out/<slug>/` (`reel.mp4`, `cover.jpg`,
`post.json`, `caption.txt`, `plan.json`, ...) like `./reels render plans/<file>`;
an existing `post.json` history is carried over. Opening or exporting a reel
does not approve, schedule or publish it; use the normal pipeline for that.

**Save Reel** / **Save Review** copy a completed export elsewhere (not into this
folder); replacement requires confirmation.

## Schedule

The toolbar's **Schedule…** button (also File > Schedule…, Shift-Cmd-S) replaces the old
Export and Claude Code buttons. The sheet walks through: caption (editable, saved into
`post.caption`), the networks (Instagram Reel, TikTok and YouTube Short, all on), the
time (America/Chicago, starting at the next open daily slot from `config.json`, at least
15 minutes ahead), then a live checklist: Render (skipped when the reel hasn't changed
since `out/<slug>/reel.mp4`), Upload to R2 (this one reel, only when there is no fresh
link), Send to Metricool, Marked scheduled. Pressing **Schedule** is your approval for
this reel; the line above the button says exactly when and where it will post. If the
reel already has a Metricool post, the button reads **Update scheduled post**.

The Metricool step runs headless Claude Code in the background (`claude -p`, found at
`~/.local/bin/claude`, or set `REELS_CLAUDE_BIN`) using your claude.ai Metricool
connector, so Claude Code must be signed in with your account. The app is not sandboxed
and its service PATH includes `~/.local/bin`, so it can reach the network and login.
Errors from Claude or Metricool appear in the sheet word for word; "Try again" is safe
(a failed run leaves the reel approved, not scheduled). If a run times out, check
Metricool before retrying in case the post exists.

## Recovery

- **Service interrupted:** reopen the app. Journal recovery restores
  the committed state; interrupted jobs cannot masquerade as completed exports.
  Do not delete `service.lock` to bypass an active editor—its process lock releases
  when the owning service exits.
- **Offline media:** a committed cached view/last-good preview can remain
  available read-only. Reconnect the recorded original before editing/exporting;
  never substitute a similarly named file.
- **Missing renderer revision:** reopen the plan; it starts a fresh history on the current renderer source.
- **Validation or preview error:** inspect the visible error and retry after
  correcting its cause. Manual editing and previous committed revisions remain
  separate from failed candidate work.
- **Disk full:** free space outside original footage/active history, then reopen
  and retry. Do not manually remove transaction journals or recorded sources.

Back up `plans/` and `.cache/editor/live/history` if you want the undo history kept; original footage is backed up separately.

## Verification

```bash
cd ~/rice/cycling
PYTHONPATH=scripts:tests .venv/bin/python -m unittest discover -s tests
npm --prefix remotion run typecheck
xcodebuild -project editor/ReelsEditor.xcodeproj \
  -scheme ReelsEditor -configuration Debug -destination 'platform=macOS' \
  -derivedDataPath .cache/editor/DerivedData test
```

XCTest runs a prohibited-activation AppKit host: no Dock icon, editor scene,
window restoration or service auto-connect. Hosted views use unordered windows;
custom-speed input is supplied at the modal presentation boundary.

Optimistic-delete ordering and zoom-slider binding checks run offscreen in Debug
(the full Debug suite includes all of these focused tests):

```bash
TEST_RUNNER_REELS_TEST_PYTHON=/absolute/path/to/.venv/bin/python \
xcodebuild -project editor/ReelsEditor.xcodeproj -scheme ReelsEditor \
  -configuration Debug -destination 'platform=macOS,arch=arm64' \
  -derivedDataPath .cache/editor/delete-debug-tests \
  -only-testing:ReelsEditorTests/EditorOperationTests/testHostedDeleteBeforeRealServiceAcknowledgementAndRollback \
  -only-testing:ReelsEditorTests/EditorOperationTests/testHostedZoomSliderBindingUpdatesClipGeometry \
  -only-testing:ReelsEditorTests/EditorUXHelpersTests/testZoomSliderBindingRoundTripsLogScale \
  -only-testing:ReelsEditorTests/EditorUXHelpersTests/testZoomSliderBindingClampsAndPreservesNonfiniteFallback \
  -only-testing:ReelsEditorTests/EditorUXHelpersTests/testZoomSliderBindingAgreesWithButtonsAndFit test
```

The 50 ms latency gate is a separate, explicitly Release-configured test:

```bash
TEST_RUNNER_REELS_TEST_PYTHON=/absolute/path/to/.venv/bin/python \
xcodebuild -project editor/ReelsEditor.xcodeproj -scheme ReelsEditor \
  -configuration Release ENABLE_TESTABILITY=YES ONLY_ACTIVE_ARCH=YES \
  -destination 'platform=macOS,arch=arm64' -derivedDataPath .cache/editor/delete-tests \
  -only-testing:ReelsEditorTests/EditorOperationTests/testReleaseHostedDeleteInputToRedrawLatency test
```

The ordering test has no wall-clock budget and runs in every configuration. The
latency test explicitly skips in Debug with a reason identifying the Release
requirement; Debug timing is never compared to a Release budget.

Both delete tests host production `ContentView`, including nested hosting views
and timeline, against a real broker/compiler on a temporary project with 570
camera keyframes. The broker holds `document.apply` before validation/persistence.
Deletion must publish synchronously on the input handler's turn, and the actual
hosted canvas must remove the clip and request redraw while the saved plan still
has both clips. Sequential delete, positioned gap preservation, ripple, intervening
refresh and a real revision conflict/rollback are covered in every configuration.

`DELETE-LATENCY Release` reports input-to-redraw requests on a monotonic clock.
XCTest observes the hosted canvas's `needsDisplay` setter; production code has no
test-only timing callbacks. The Release gate requires each scenario below 50 ms.
This measures redraw requests, **not** screen presentation. Media decoding and
automatic preview rendering are disabled for this fixture. Offscreen view bitmaps
are in `.cache/editor/timeline-captures/delete-*.png`; no screen-capture or system
Accessibility service is used.

`TimelineZoomControl` owns the timeline's zoom and exposes the logarithmic
`sliderPosition` binding used directly by the production Slider. The canvas,
plus/minus controls, Fit, keyboard commands and bottom scroller share that state.
The extracted mapping preserves `log(zoom)` / `clamp(exp(position))`, including
the existing nonfinite fallback; zoom limits, step size and fit math are unchanged.

Three helper tests cover logarithmic round trips, clamping/nonfinite values, and
agreement with the actions used by the zoom/Fit buttons. The hosted binding test
sets that same production binding, checks the zoom state immediately, and waits
for exact clip widths in the production `TimelinePanel` canvas. It checks both
zoom directions, returning to the same position, button/command updates and both
clamp boundaries. No playhead-only geometry refresh is needed.

This replaces the unreliable offscreen Slider mouse-event test. An unordered
SwiftUI host exposes neither an `NSSlider` nor a virtual slider provider, and
direct mouse injection did not establish that input reached its binding.
The replacement verifies the binding-to-canvas path, not gesture delivery;
it uses no mouse events, private proxies, system input or external AX calls.

PR #40 binding continuation: one full Debug run executed 150 tests with zero
failures (149 passed, one explicit Release-only latency skip), including all four
zoom-binding regression tests. Evidence:
`.cache/agents/ui-delete-zoom-binding-debug.log` and
`.cache/agents/ui-delete-zoom-binding-debug.xcresult`. No failed-test rerun was
needed. Timing code was unchanged, so the Release latency test was not rerun.

Delete predictions do not wait for a Task hop or earlier backend edits. Persistence
still runs serially against the authoritative revision; pending predictions replay
over refreshes and rejected edits roll back. Inspector sections and collection rows
are lazy so offscreen camera controls do not block the timeline's update.

Recorded Release input-to-redraw results for that hosted fixture (574 nodes):

| Delete | Eager inspector baseline | Lazy inspector + synchronous enqueue |
| --- | ---: | ---: |
| Sequential | 3864.3 ms | 48.28 ms |
| Positioned | 3642.9 ms | 5.29 ms |
| Ripple | 3718.1 ms | 41.32 ms |
| Rejected edit (optimistic phase) | 3816.2 ms | 4.26 ms |

Baseline: inherited XCTest log `.cache/agents/ui-delete-interval-before.log`;
current separate Release gate: `.cache/agents/ui-delete-review-tests.log`.
The baseline ordered its hosted window; the current host is offscreen and
nonactivating. These are single-run diagnostics, not controlled screen-presentation
measurements or a guarantee under arbitrary load. The measured baseline stall was
inspector layout before canvas update, not a dependency on backend acknowledgement.

The optional sandbox/render proofs need macOS with ffmpeg, node and a Chrome
Headless Shell:

```bash
REELS_RUN_SANDBOX_RENDER_PROOF=1 PYTHONPATH=scripts:tests .venv/bin/python -m unittest \
  test_editor_preview.EditorPreviewTests.test_sandboxed_exact_preview_and_private_export_fixture \
  test_editor_live.LiveExportProof
```
