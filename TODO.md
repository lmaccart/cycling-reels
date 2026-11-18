# TODO

For the next ingest (added 2026-11-17).

## Quick list

- [ ] **⌘K split bug.** Cuts every clip except the selected one. Details below.
- [ ] **Voice share shows "-9%".** `./reels check` / `render` on `plans/2026-11-16-sprint-zone-nov-26.json` warns `spine: marked voice, but the rider's voice covers only -9% of the reel`. A share can't be negative. The reel is voice-led: the voice runs on V1 under the 360 sprint footage on V2, and the voice clip is tagged `role: voice`, `speaker: me`. Likely cause: `voiceShare` in `lineage.py` doesn't handle a positioned timeline where the voice clip sits under a higher track; it may subtract V2 coverage or count frames twice. Fix it and add a test with a two-track plan.
- [ ] **Hitmarker download in `music/`.** `music/mlg-gaming-hitmarker-greenscreen-sound-e.mp3` (+ `.json`) came in through `yt2mp3` but is a sound effect. The trimmed hit is now `sfx/hitmarker.wav`. Remove the music entry so it's never picked as a song (ask first).
- [ ] **"Cold = slower" talking reel needs a new day.** The sprint zone reel took its Thu 11-19 18:30 slot (Metricool id 432344797).
- [ ] **Commit this session's data.** Not committed yet:
  - the sprint zone plan
  - the voice clip's asset, sounds and transcript (`261117-img-3679-2e2f`)
  - `sfx/fortnite-downed.*`, `sfx/hitmarker.*`, `sfx/README.md`
  - `learning/feedback.jsonl`
  - this file
- [ ] **Stacked PRs closed on merge.** Merging #49 with `--delete-branch` closed #50 and #52, because their base branch was deleted, and merged #51 into #50's branch instead of main. They were re-landed as #53. Next time, merge stacked PRs from the top down, or retarget each one to `main` before deleting its base.

## Editor: ⌘K cuts every clip except the selected one

**Reported 2026-11-17**, on `plans/2026-11-16-sprint-zone-nov-26.json` with the editor built from `fix/editor-split-empty` (PR #52, now on main via #53).

**What happened:** with a clip selected and the playhead at 15.9 s (frame 477), ⌘K cut every clip under the playhead *except* the selected one. It split:
- V1: the voiceover clip `261117-img-3679-2e2f` 201.47–225.40 s, into 201.47–210.80 and 210.80–225.40
- V2: the sprint footage `261031-gs010110-08e8` 9.13–50.73 s, into 9.13–25.03 and 25.03–50.73 (with `cameraWindow` offset 15.9)

The cut was undone by hand (the pieces were merged back in the plan file). A copy of the plan right after the cut is in the session scratchpad (`sprint-zone-after-cut.json`), which may not survive.

**Expected:** with a clip selected, ⌘K cuts only that clip. Premiere's ⌘K with nothing selected cuts every track.

**Error seen before PR #52**, when ⌘K found no target: `operations must be a non-empty array`. This comes from `scripts/reels/editor/transactions.py` (`invalid_batch`): the Swift side sent an empty `document.apply` batch. PR #52 stops empty batches, and with nothing to split it now shows a notice instead.

**Likely cause (not confirmed):** `TimelineEditingTargets.split` (`editor/ReelsEditor/EditorStore+Editing.swift`) treats the selection as a clip selection only if a selected id is a `segment` node. Clicking a clip probably selects a different node: its linked `source_audio`, a timeline row id, or an id the optimistic timeline renamed. "No clip selected" then falls back to every clip under the playhead. Before PR #52, the same mismatch gave the empty batch above.

**To check first:**
- What `selectedIDs` holds after clicking a clip on V1 and on V2.
- Whether the selected id survives an optimistic prediction or refresh.

**Fix:** add a test that clicks a clip and then presses ⌘K.

**Logs:** `~/Library/Logs/Reels Editor/service.log` has nothing about the split: no request or error is logged, only unrelated `ConnectionResetError` tracebacks from the preview HTTP server. The editor history in `.cache/editor/live` doesn't keep the operation either. Adding logging of the split targets and the selection would make this one easier to pin down.
