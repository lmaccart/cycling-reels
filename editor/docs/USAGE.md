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
- **Colour** (Inspector, at reel, clip and project level): the colour grade's
  look (house / moody / none), strength, grey-day rescue toggle, and per-reel or
  per-clip warmth and exposure sliders. A slider commits when you let go, as one
  undoable edit; the arrow button resets it. Clip settings override the reel's,
  which override the project default (`config.json` `render.grade`). The preview
  re-stages the clips you changed.
- Drag the timeline playhead or ruler to scrub: the Program preview updates
  while the mouse is held down. The final playhead position is saved on release.
- Each validated gesture or operation batch autosaves as one transaction.
  Undo/redo covers both manual and Claude changes. Failed edits leave the last
  committed revision intact.
- Source video uses AVFoundation; still photos are images. Unsupported formats
  and 360 media use private proxies. Preparing a full 360 source can be expensive.

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

The optional sandbox/render proofs need macOS with ffmpeg, node and a Chrome
Headless Shell:

```bash
REELS_RUN_SANDBOX_RENDER_PROOF=1 PYTHONPATH=scripts:tests .venv/bin/python -m unittest \
  test_editor_preview.EditorPreviewTests.test_sandboxed_exact_preview_and_private_export_fixture \
  test_editor_live.LiveExportProof
```
