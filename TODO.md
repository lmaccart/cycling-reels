# TODO

For the next ingest (added 2026-11-17, worked through 2026-11-18).

## Quick list

- [x] **⌘K split bug.** Cut every clip under the playhead instead of only the selected one. Fixed 11-18 (details below).
- [x] **Voice share shows "-9%".** `lineage.py` assumed one picture track, so on a two-track plan a V1 piece "ended" where the V2 clip started (0 s) and its length went negative. Positioned plans are now laid out per track (highest track on screen, each clip runs its own length), with two-track tests. `./reels check` on the sprint zone plan no longer warns.
- [ ] **Hitmarker download in `music/`.** `music/mlg-gaming-hitmarker-greenscreen-sound-e.mp3` (+ `.json`) came in through `yt2mp3` but is a sound effect. The trimmed hit is now `sfx/hitmarker.wav`. Remove the music entry so it's never picked as a song (ask first: it's not in git, so deleting is permanent).
- [x] **Commit this session's data.** Done in 43e86b5.
- [x] **Stacked PRs closed on merge.** Noted for next time: merge stacked PRs from the top down, or retarget each one to `main` (`gh pr edit N --base main`) before deleting its base.

## Editor: ⌘K cut every clip except the selected one (fixed 11-18)

**Reported 2026-11-17** on `plans/2026-11-16-sprint-zone-nov-26.json`: with a clip selected and the playhead at frame 477, ⌘K split both the V1 voiceover and the V2 sprint footage.

**Cause:** `TimelineEditingTargets.split` treated any selection that wasn't a `segment` (a caption, a sound, an id that no longer exists) as "nothing selected" and cut every clip under the playhead. Clicking a clip itself selects it correctly (a hosted test now clicks V1, V2 and the linked audio and presses ⌘K), so the selection at the time most likely wasn't the clip. The exact node can't be recovered from that session.

**Fix:**
- Only an empty selection cuts every clip under the playhead (Premiere's behaviour), and a notice says how many were cut.
- With anything selected, only selected clips are cut; a clip's linked audio counts as the clip. A selection with no clip in it cuts nothing and shows a notice.
- `service.log` now gets a `split (manual): [(id, frame)…] selection=… playhead=…` line for every cut, so a wrong cut can be traced next time.
