# Reels Editor

A native macOS editor that is part of this codebase. It edits the **real**
`plans/*.json`, so Claude Code in the terminal, `./reels render` and the app all
see the same files. Your original footage in `library/media/` is read-only to it.

## Install (once, and after source changes)

```bash
cd ~/rice/cycling
./editor/reels-editor setup      # or: bash setup.sh --editor
```

This installs the repo's Python and Remotion dependencies, builds the app and
copies it to `~/Applications/Reels Editor.app`. Open it from Finder or Spotlight
and keep it in the Dock (right-click its icon, Options, Keep in Dock). It remembers
this folder, so it launches with no terminal and no arguments.

Requires macOS 14+, full Xcode, Python 3.10–3.13, Node 18+, ffmpeg and
`sandbox-exec`. Claude Code is optional for manual editing; sign in to it locally
for chat and semantic search. No API key is needed.

From the terminal you can also use `./editor/reels-editor open [plans/x.json]`
(or double-click **Open Editor.command**) and `./editor/reels-editor info`.

## Using it

1. The welcome window shows your reels (newest first), **New Reel** and
   **Import Footage**.
2. Import copies files into `inbox/` (your originals stay where they are) and
   runs `./reels ingest`; the library refreshes when it finishes.
3. Edit with the library, timeline, inspector and chat. Every change is an undoable
   edit written straight to the plan file; edits made in the terminal appear in the app.
4. **Export** renders the plan to `out/<slug>/` exactly like `./reels render plans/<file>`.
   Nothing is ever approved, scheduled or published by the editor.

## Safety

- The editor never deletes or modifies anything in `library/media/`, and never
  deletes from `library/` or `plans/`. Footage import copies only.
- Editor state (undo history, previews, chat, jobs, lock) lives in
  `.cache/editor/live/`, which git ignores.
- Renderer and preview workers run in a macOS sandbox: no network, no `.env` or
  credential files, no writes anywhere in `library/`.
- Renderer code changes are not available in the app. Ask Claude Code in the
  terminal for those.

## Claude Code in the terminal

While the app is open it writes `.cache/editor/live/context.json` with the open
plan, the selected nodes and the playhead, so a terminal Claude Code session can
read it to see what you are looking at.

[Full usage and recovery](docs/USAGE.md)
