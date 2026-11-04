---
name: cycling-reels
description: Turns ride videos and photos in this project's inbox/ into captioned 9:16 Instagram Reels with natural TikTok-style text, this week's trending Reels songs, ride sounds and sound effects, then queues them for daily posting. Use when asked to process new footage, tag or classify clips, plan or render reels, find, download or pick (trending) music, add sound effects, write captions, check how reels performed, learn from feedback or production problems, review weekly strengths and weaknesses, run the weekly batch, or schedule/queue posts from this folder.
---

# Cycling Reels

This folder is a pipeline: raw footage → library of tagged moments → reel plans (JSON) → rendered Reels (Remotion) → a posting queue. You do the judgment (classifying, choosing moments, picking music, writing text); the `./reels` CLI does the media work. Run every command from the project root.

## Rules that always apply

- **Every visual is the user's own footage.** Reels are built only from clips and photos in `library/` (what they recorded and dropped in `inbox/`). Never generate, download, screen-grab or synthesize video, images, B-roll, backgrounds or title cards, and never ask for "stock" shots. The renderer verifies each file against the original it ingested and refuses anything else. Short on material? Make fewer reels and say so.
- **Music: this week's trending Reels songs.** Each batch, check the current trending-audio lists and pick songs that fit the reels ([reference/music.md](reference/music.md)). Download the official audio from YouTube with `./reels music add <url> --instagram "<title> <artist>"`; it's mixed into the reel, and Instagram usually recognizes it and shows the song. If the user gives a link, use that song. No-copyright tracks (`./reels music search … --free`) are for talking reels or when the user wants no mute risk. Music is never generated; never pick a track flagged `⚠` without the user's say-so. Attaching Instagram's licensed copy at scheduling instead is opt-in (config `audio.attachSongs`).
- **Sound effects: the free library or the user's own ride audio.** Only sounds in `sfx/` (human-made, CC0 or Pixabay licence, source and author recorded per file) or cut from the user's footage with `./reels sfx add --from`. Never AI-generated sounds unless the user explicitly asks for them.
- **Write like a person, not a brand.** Hooks, on-screen text and captions follow [reference/captions.md](reference/captions.md): lowercase, casual, specific, a little funny; no hype words, no ALL CAPS, at most one emoji. Text renders exactly as you write it.
- **Never publish or schedule a reel the user hasn't approved.** Approval means they saw it (or its preview) and said yes, in this conversation. "Approve this batch" counts for that batch only.
- **Don't invent facts.** Speeds, grades, distances, watts, race results and place names must come from the user, the footage itself (a bike computer in frame, a road sign) or a transcript. Otherwise write text without numbers. Camera GPS from GoPro telemetry counts as the footage (the user confirmed): show it only through `"value": "gps…"` stats so the number comes from the data — never type a speed in by hand. GoPro altitude drifts badly, so no elevation numbers.
- **Be careful with other people.** Avoid close-ups of identifiable strangers and don't feature anyone's crash unless the user says it's fine. Tag `people` honestly so this is visible at planning time.
- Keep the user's originals: never delete or edit anything in `library/media/`.

## Weekly batch (the normal flow)

0. **Learn before planning** — follow [reference/learning.md](reference/learning.md): refresh/import analytics when available, run `./reels review`, and read `learning/LESSONS.md`, this/last week's review JSON and `learning/experiments.json`. Record evidence-backed strengths, weaknesses and next actions across audience response, user preferences and production quality. Missing/stale data is unknown, not failure. Aim for five familiar approaches and two tests in a seven-reel week if footage permits; register each experiment's hypothesis, one change, primary metric and test/control slugs before rendering. No automatic winners or permanent skill/code rewrites.
1. **Ingest** — `./reels ingest` moves new files from `inbox/` into the library, makes a contact sheet per asset, detects talking, finds candidate ride sounds and reads GoPro GPS (`library/telemetry/<id>.json`; `./reels show <id>` prints good-fix time, max/avg speed and distance).
2. **Classify** — `./reels pending` lists what needs tags. For each asset:
   - Read its contact sheet image(s) (`library/sheets/<id>.jpg`; long clips have `-2`, `-3` sheets). Tiles are labelled `#n  m:ss.s`; on landscape footage the yellow lines show the default 9:16 crop.
   - `./reels show <id>` gives audio/speech info. For a closer look: `./reels frames <id> --start 40 --end 70` (zoomed sheet) or `--at 52.5,55` (big frames for crop decisions).
   - **Sounds:** `./reels sounds <id>` lists candidate ride sounds (freehub, shift/clip-in, pass-by, brakes, car…) and writes `library/sounds/<id>.jpg`: loudness + spectrogram with click / wind / speech lanes and the candidates boxed (zoom long clips with `--start A --end B`). Read it, cross-check the frames, and tag only clean, audible ones in `sounds` per [reference/taxonomy.md](reference/taxonomy.md#sounds-video), never something buried in wind or talked over. Detection is a shortlist, not a verdict: freehub is fairly reliable, shifts and pass-bys much less so.
   - Save tags: `./reels tag <id> --json '{…}'` following [reference/taxonomy.md](reference/taxonomy.md). Moments are the 2–8 s stretches worth using, with `focusX` = where the subject sits horizontally (0 left … 1 right) so the 9:16 crop keeps it.
   - The user films from [SHOTLIST.md](../../../SHOTLIST.md) (pass-bys, face at the hard moment, one line to camera, the computer screen, kit-up, café…). Tag those honestly so plans can find them: `roadside` for pass-bys, `chase` for third-person views, `selfie` for face clips, `screen` for a bike computer, `talking` type for lines to camera.
   - Junk (lens cap, pocket footage, blurry): `./reels skip <id> --reason "…"`.
   - **360 footage** (GoPro MAX/MAX 2 `.360`, 360 MP4s and photos — the sheet says `360°` and shows the whole sphere unwrapped with yaw labels): find the road and the rider, check with `./reels frames <id> --views` / `--view yaw,pitch,fov`, and save them as named `angles` ([reference/taxonomy.md](reference/taxonomy.md#360-footage-gopro-max--max-2-360-360-mp4s-360-photos)). Plans then aim a virtual camera with `view`, camera moves with `path`, or `projection: "tiny-planet"` ([reference/plan-schema.md](reference/plan-schema.md#360-segments-gopro-360-360-mp4s-360-photos)).
3. **Transcribe talking clips** — `./reels transcribe` (only clips with detected speech). Check each with `./reels show <id>`. Fix misheard cycling words by editing the `w` values in `library/transcripts/<id>.json` (keep the timings). If it's wind noise turned into gibberish, don't use `captions: "transcript"` for that clip.
4. **Plan** — `./reels library --unused --min-score 3` lists available moments, best first. Pick the week's reels (default 7, one per day) using [reference/recipes.md](reference/recipes.md): a person on screen, a payoff the footage shows, a mix of formats (trend, story, tips, POV joke, beat edit…), and never the same clip in two reels. Before choosing, read how the last reels did: pull analytics per [reference/analytics.md](reference/analytics.md) (Metricool, brandId 7655493), `./reels stats import <file>`, then `./reels stats`, and read `out/PERFORMANCE.md` — hold rate, watch-through and shares/saves per reach steer formats, hooks, lengths and songs; act on big gaps only. For each reel:
   - **Format:** set `"format"` to the recipe name from recipes.md so performance can be compared by format.
   - **Music:** fetch this week's trending lists and shortlist songs per [reference/music.md](reference/music.md) (fits the footage, official audio), add each with `./reels music add <url> --instagram "<title> <artist>"`, and plan `"music": {"track": "<slug>", "cutOnBeat": true}` (it starts at the loudest part; set `start` for a trend's exact moment). Reuse a song only while it's still on the lists. No-copyright tracks (`--free`) carry their credit line automatically.
   - **GPS (GoPro clips):** `./reels telemetry <id> --fast 5` finds the fastest stretches; a live speed pill is `"stats": [{"value": "gps"}]` (also `gps:max`, `gps:avg`) and `"route": true` adds a small route map ([reference/plan-schema.md](reference/plan-schema.md#gps-stats-and-route-map-gopro-telemetry)).
   - **Swearing:** add `"bleep": true` on any segment where the clip's own audio swears; `./reels check` warns about unbleeped swear words ([reference/plan-schema.md](reference/plan-schema.md#bleeps)).
   - **Punch-ins / loops (optional):** `"punch": [{"at": 1.2, "scale": 1.15}]` or `{"words": ["no", "way"]}` on a captioned clip zooms the footage for emphasis; `"punchOnBeats": {"every": 4}` with a beat-synced song. `"loop": true` for 6–12 s reels that should replay seamlessly — end on the lead-in to the first shot (swap halves, see recipes.md Loops) and read the `seam:` line in `./reels check` ([reference/plan-schema.md](reference/plan-schema.md#punch-ins)).
   - **Text:** hook, 1–3 text beats, `post.caption`, 3–5 hashtags and alt text in the natural voice of [reference/captions.md](reference/captions.md).
   - **Sound:** 1–2 ride-sound moments (`"nat"`) on the best sounds in the shots you picked (`./reels library --sounds`, `./reels sounds <id>`), at most one `musicDrops` ending where the `"payoff": true` segment starts, and 2–5 sound effects in total — the automatic rules add whooshes on fades/360 swings, a pop on text and a hit on the payoff; add or move cues with `sfx`. Read the sound sheet `./reels check` prints and act on its quiet-nat and trending-song warnings (freehub moments usually need `volume` 2–4) — [reference/recipes.md](reference/recipes.md#sound-design), [reference/plan-schema.md](reference/plan-schema.md#sound-design). For a no-music "just the bike" reel, leave `music` out and set `"autoSfx": false`.
   - Write `plans/<YYYY-MM-DD>-<slug>.json` per [reference/plan-schema.md](reference/plan-schema.md) and validate with `./reels check plans/<file>.json`; fix every error and any warning that matters.
5. **Render** — `./reels render plans/<file>.json`, one plan per command. Then Read `out/<slug>/preview.jpg` and check: hook readable and not covering the subject, subject inside the crop, nothing colliding, text sounds like a person, the payoff visible. For loop reels, render prints `loop seam:`; fix a visible jump before review. Fix the plan and re-render when something's off.
6. **Review with the user** — show each reel's preview, hook, caption and music (song — artist, trending or free track), and point them to `out/<slug>/reel.mp4` to watch (with attaching on, `preview.mp4` — `reel.mp4` is then the upload without the song). Apply their edits. `./reels mark <slug> approved` for each yes (`./reels mark rendered approved` when they approve everything they just reviewed); `./reels discard <slug>` for each no (frees its clips).
7. **Queue and schedule** — `./reels queue` gives approved reels daily slots (unapproved ones are left alone) per config `schedule.times` / `schedule.timezone` and writes `out/QUEUE.md`. Then follow [reference/publishing.md](reference/publishing.md): schedule each queued reel through the Metricool connector if it's available, and record it with `./reels mark <slug> scheduled --metricool-id <id>`. Without a connector, hand the user `out/QUEUE.md` or `./reels export` for manual posting.
8. **Close the production review** — record actual user edits/rejection reasons and visual/audio/scheduling issues with `./reels review feedback` as they happen (before discarding rejected reels). After scheduling, run `./reels review close --slugs <all intended batch slugs> --file <notes.json>` per [reference/learning.md](reference/learning.md). Include unresolved/manual posts; don't mark scheduled unless confirmed. Record which lessons influenced each reel, experiments and proposed improvements. Scheduling isn't publication: evaluate audience outcomes next week. Permanent skill/recipe/shot-list/code changes need approval; code changes need tests.

## When running unattended (a scheduled task)

Nobody is there to approve, so do steps 0–5 only, then stop: don't approve, queue, upload or schedule anything, and don't close a scheduling checkpoint. Run the planning learning review even with no new footage; include its path, experiments and proposed permanent changes in the report. The analytics pull needs the Metricool connector; if it isn't available, plan from the last `out/PERFORMANCE.md` and saved learning records and say so. Music: check this week's trending lists and add songs with `--instagram` as usual (nothing is published), or use tracks already in `music/` / `--free` results without a `⚠` caution. If the inbox is empty and fewer than 3 good unused moments remain, render nothing and say the library is running low. Finish with a short report: what was ingested, which reels were rendered (slug, hook, length, music, preview path), anything skipped or uncertain, and how much unused material is left (`./reels library --unused`).

## Command reference

| Command | What it does |
| --- | --- |
| `./reels doctor [--fix]` | Checks ffmpeg, Node, Remotion, Whisper, yt-dlp; `--fix` installs/updates what's missing |
| `./reels ingest` | Inbox → library, contact sheets, speech detection |
| `./reels pending` | Assets waiting for tags |
| `./reels frames <id> --start A --end B` / `--at t1,t2` | Closer looks at a clip (also works on `out/<slug>/reel.mp4`) |
| `./reels frames <id> --views` / `--view rider` or `--view 180,-15,85` / `--planet` (360 only) | Four directions, or a preview of an exact 9:16 reframe / tiny planet |
| `./reels tag <id> --json '{…}'` / `./reels skip <id>` | Save classification |
| `./reels show <id>` | Everything about one asset, incl. moments and usage |
| `./reels transcribe [ids…]` | Word-level transcripts (default: clips with speech) |
| `./reels library [--unused] [--min-score N] [--type T]` | Tagged moments, best first |
| `./reels sounds <id> [--start A --end B] [--best] [--kind K]` / `--pending` | Candidate ride sounds + labelled spectrogram `library/sounds/<id>.jpg`; `--pending` analyses every video without a cache |
| `./reels library --sounds [--kind K] [--unused]` | Tagged ride sounds, best first |
| `./reels telemetry [ids…] [--force]` | Read GPS from GoPro telemetry (ingest does this automatically) |
| `./reels telemetry <id> --at 120.5` / `--start A --end B` / `--fast 5` | Speed at a moment, min/avg/max over a range, fastest gap-free stretches |
| `./reels sfx list [--category C]` | Sound effects in `sfx/` (whoosh, swish, riser, hit, pop, click, shutter, bell, scratch, stop, nat) with length, level, license, author |
| `./reels sfx add <file-or-url> --name <slug> --category C --license CC0-1.0 --source <page-url> --author "<name>" [--trim A,B]` | Add a CC0 / Pixabay-licence sound (trimmed, 48 kHz WAV, normalized); refuses without license + source |
| `./reels sfx add --from <asset-id> --at <s> --for <s> --name <slug> [--clean]` | Cut a sound out of the user's own footage (category `nat`); `--clean` = highpass + denoise for wind |
| `./reels stats import <file.json>` / `./reels stats [--since YYYY-MM-DD]` | Store a Metricool analytics pull and match it to reels / per-reel performance table, writes `out/PERFORMANCE.md` |
| `./reels review [--week YYYY-MM-DD] [--file notes.json]` | Weekly evidence/lessons before planning; append a checkpoint to learning/reviews/ |
| `./reels review feedback --category preference\|production --kind <kind> --note "…" [--slug <slug>]` | Record actual feedback or a sanitized production issue/fix |
| `./reels review experiment --file experiment.json` | Register hypothesis/metric/test-control design before rendering tests |
| `./reels review close --slugs <slugs…> [--week YYYY-MM-DD] [--file notes.json]` | Snapshot scheduling results and unresolved posts; never changes statuses |
| `./reels music search "<query>" [--free] [--limit N]` | Search YouTube; shows channel, length, views and free-to-use signals |
| `./reels music add <url> [--instagram "Title Artist"] [--name x] [--start m:ss] [--credit "…"]` (alias `./reels yt2mp3 <url>`) | Download a track to `music/` as MP3, detect its beats, record source, rights and credit; `--instagram` marks a trending song (mixed in; attached from Instagram's library only with config `audio.attachSongs`) |
| `./reels music list` | Tracks with bpm, start point, rights, credit and usage |
| `./reels music beats <track> [--from m:ss] [--for N]` | Tempo and beat times, for timing cuts and text |
| `./reels check <plan>` / `./reels render <plan>` | Validate (prints the timeline and the sound sheet: nat windows, drops, sound-effect cues with their source) / render a plan |
| `./reels discard <slug>` | Delete a render, free its clips and its song's usage |
| `./reels queue [--start YYYY-MM-DD] [--times 07:30,18:30]` / `--list` | Assign posting slots, write `out/QUEUE.md` |
| `./reels mark <slug> <approved\|scheduled\|posted\|rejected>` | Track status |
| `./reels export --to <folder> [slugs…]` | Copy approved/queued reels + caption `.txt` files (e.g. to iCloud Drive for posting from a phone) |
| `./reels upload <slug…>` / `--queued` | Optional: push to S3/R2 for a public media URL |
| `./reels selftest` | End-to-end test on throwaway clips in a temp folder |

Track records live in `music/<slug>.json`; you may fix a wrong `credit` there, or set `"rightsOk": true` when the user confirms they have the rights.

`ingest` (on long GoPro files — 360 `.360` files especially), `transcribe`, `music add` and `render` can each take several minutes: give those commands the maximum Bash timeout (10 minutes) or run them in the background, one plan per `render` call.

If a command fails with "run ./setup.sh first" or a missing dependency, run `./reels doctor` and tell the user exactly what to install rather than working around it. If YouTube downloads start failing, `./reels doctor --fix` updates yt-dlp.
