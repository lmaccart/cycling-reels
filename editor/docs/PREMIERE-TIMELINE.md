# Premiere-style timeline dragging

Status: implemented and verified, 2026-11-16. Included in PR #22.

## Requested behavior

Footage can move between V1, V2, and other video tracks. Dropping footage onto occupied time on its destination track performs an undoable overwrite edit: the covered portions are cut away, with surviving heads and tails retained.

## Research

Adobe's documentation establishes these rules:

1. **Overwrite is the default drag edit.** The incoming clip replaces the destination track's covered frames. Material outside that interval keeps its timeline position. An insert edit is a separate operation, selected with Command-drag on macOS.
2. **Video tracks are layers.** A clip on a higher video track appears above lower-track footage. Placing a clip on V2 does not overwrite the footage on V1.
3. **Moving is non-ripple by default.** The clip's old position can become a gap. The surrounding sequence does not automatically close that gap.
4. **Vertical moves preserve timing through snapping.** With snapping enabled, a clip moving to another track snaps to its original time position. Clip boundaries and the playhead are useful alignment targets.
5. **The drag preview communicates the edit.** Premiere uses a translucent duration rectangle, an overwrite/insert indication, and a time-offset readout.
6. **Locked tracks are protected.** Clips cannot be modified on a locked track, and new clips cannot be dropped onto it.
7. **Cuts are non-destructive.** Timeline fragments reference different source In/Out ranges; the original media file is untouched.

### Sources

- Adobe, [Rearrange and move clips](https://helpx.adobe.com/nz/premiere-pro/using/rearranging-clips-sequence.html): overwrite default, cross-track movement, snapping, gaps, and drag modifiers.
- Adobe, [Add clips to sequences](https://helpx.adobe.com/au/premiere-pro/using/adding-clips-sequences.html): covered-frame overwrite semantics and destination-track targeting.
- Adobe, [Edit video in Premiere](https://helpx.adobe.com/premiere/desktop/edit-projects/intro-to-editing/edit-video-in-premiere.html): stacked video tracks and non-destructive editing.
- Adobe, [Track Lock to prevent changes](https://helpx.adobe.com/premiere/desktop/edit-projects/change-clip-sequence/track-lock-to-prevent-changes.html): protected tracks, including preview/export behavior.

Research checked 2026-11-16. Adobe's indexed documentation supplied the relevant text; direct page fetches returned HTTP 403.

## Interaction and editing contract

- A drag specifies a destination video track and an exact frame position.
- The source clip retains its source range, speed, and effects when moved.
- Overwrite affects the destination track's occupied intersections, preserving unrelated tracks and unrelated absolute timing.
- A partially covered destination clip keeps its surviving portion. A covered middle produces two independently editable fragments with source-correct In/Out points.
- Moving away leaves the source interval empty; extending past the current endpoint extends the sequence.
- An invalid or locked destination is visibly blocked and produces no partial edit.
- One drop is one validated, undoable transaction. The renderer, preview, exported video, snapshot, and optimistic timeline must agree.
- Existing reel plans retain their current rendered arrangement until an editing operation changes them.

## Service contract

One drop submits a `move_segments` operation inside the existing revision-checked batch:

```json
{
  "op": "move_segments",
  "editId": "a0000000-0000-4000-8000-000000000001",
  "mode": "overwrite",
  "placements": [
    {"id": "existing-clip-id", "track": "V2", "atFrame": 90}
  ]
}
```

For a library drop, a placement supplies `insert` instead of `id`, using the renderer's source-range/effects object, for example:

```json
{"insert": {"asset": "library-asset-id", "in": 2, "out": 5}, "track": "V1", "atFrame": 90}
```

Placements are resolved together. Moved clips are excluded from overwrite victims, and incoming clips cannot overlap one another on the same destination track. The snapshot advertises the supported operation through `timelineCapabilities`, so the app can match the selected renderer's capabilities.

Positioned plans opt into a versioned root `timeline` with FPS and sequence duration; footage segments carry their own track, start frame, duration, and transition-in frames. Legacy plans continue through the existing render path until the first placement edit freezes their compiled arrangement.

### Stable fragment identity

Python and Swift use the same UTF-8 SHA-256 input:

```text
reels-overwrite-v1\0lowercase(editId)\0originId\0startFrame\0endFrame
```

There is no trailing separator. Frame ranges are half-open and refer to the victim's pre-edit absolute placement. The earliest surviving fragment retains its original ID; later fragments use `frag-` plus the lowercase hexadecimal digest. Inserted footage uses `clip-` and origin `insert:<placement index>`; duplicated owned nodes use `node-` and their original child ID. This lets a queued drag address a newly predicted fragment before the first edit response arrives.

## Acceptance cases

| Case | Expected result |
|---|---|
| V1 → empty V2 at the same time | Clip moves vertically; its timing stays fixed |
| V2 above occupied V1 | V1 remains intact underneath |
| Drop over a destination clip's head | Covered head is removed; surviving tail has a later source In |
| Drop over a destination clip's tail | Covered tail is removed; surviving head has an earlier source Out |
| Drop inside a longer destination clip | Destination becomes a head and tail around the moved clip |
| Cover several destination clips | Fully covered clips disappear; partial boundary clips are trimmed |
| Move a clip along its own track | Old interval becomes a gap; the moved clip does not overwrite itself |
| Move beyond the endpoint | Gap and extended sequence duration are represented in the actual render |
| Overwrite speed-adjusted footage | Source-range arithmetic respects speed and frame boundaries |
| Locked source or affected destination | Entire edit is rejected |
| Undo / redo | Original placement and all survivor fragments restore consistently |
| Rapid queued edits | Predictions reconcile without double movement or unstable fragment identity |

## Verification

- Full Python suite: **797 cases — 795 passed, 2 skipped**.
- Native macOS suite: **72 passed**, with no failures or skips.
- Remotion TypeScript typecheck passed.
- Real rendered-frame/audio regressions cover layer order, black gaps, linked audio, owner-specific overlays, fades, short fragments, and depth exhibits.
- Production AppKit canvas captures were inspected at normal and narrow widths, including cross-track ghosts, middle-overwrite previews, surviving fragments/source gaps, and blocked drops.
- `./editor/reels-editor setup` rebuilt the preview bundle and installed the Release app.

```sh
PYTHONPATH=scripts:tests .venv/bin/python -m unittest discover -s tests
xcodebuild -quiet -project editor/ReelsEditor.xcodeproj -scheme ReelsEditor \
  -destination 'platform=macOS,arch=arm64' -derivedDataPath .cache/editor/DerivedData test
npm --prefix remotion run typecheck
./editor/reels-editor setup
```

Local visual captures are in `.cache/editor/timeline-captures/final/`; the pre-change captures are preserved in `incumbent/`. Verification uses isolated fixtures and offscreen native views.

The supported drag mode is overwrite. Command-drag insert remains unavailable and is explicitly rejected rather than changing edit modes silently.
