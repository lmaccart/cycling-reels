# Reels Editor

The native macOS editor is now part of this codebase. Its Swift app and launchers
live here; the editing service is `scripts/reels/editor/`, and the renderer is
shared with the existing `reels` pipeline.

## Open it

```bash
cd ~/rice/cycling
./editor/reels-editor open
```

Or double-click **Open Editor.command** in this folder. You can choose a specific
plan:

```bash
./editor/reels-editor open plans/2026-11-11-easy-day-laundry.json
```

On a new machine, or after changing source code, first run:

```bash
./editor/reels-editor setup
```

Requires macOS 14+, full Xcode, Python 3.10–3.13, Node 18+, ffmpeg/ffprobe and
`sandbox-exec`. Claude Code is optional for manual editing; sign in to it locally
for chat and semantic search. No API key is needed by the editor.

## Basic workflow

1. Choose a reel in **Projects**.
2. Use **Library** and the Source monitor to find footage. Select a timeline
   clip to change its typed Inspector values, trim it, or add B-roll.
3. Ask **Chat** for changes such as “lower the music under my voice.” Claude
   edits the same timeline; undo/redo covers both manual and Claude changes.
4. Use **Interactive** for editing, then **Exact** for final picture/audio review.
5. **Export**, then **Save Reel** or **Save Review** to a folder outside the live
   checkout. Nothing is approved, uploaded or published automatically.

## Where edits go

Use `./editor/reels-editor info` to see exact locations. Independent dependencies,
builds, editable snapshots and exports are under:

```text
~/Library/Application Support/CyclingReelsEditor/
```

**Changes autosave to a private snapshot, not the live `cycling/plans/`.**
Your originals and existing production dependencies are not modified.
Previous snapshots and runtime versions are retained; setup never replaces them.

After ingesting footage or creating plans with the usual pipeline, close the app
and import them:

```bash
./editor/reels-editor refresh --keep-private
```

This creates a new snapshot, imports new files and keeps conflicting private
files. For a changed production file that should replace its private counterpart,
use the explicit resolutions workflow in [the usage guide](docs/USAGE.md).
There is no automatic write-back to production; ask explicitly when an edited
plan should be promoted.

[Full usage and recovery](docs/USAGE.md) · [Acceptance evidence and limits](docs/ACCEPTANCE.md)
