# cycling-reels

Your ride footage in, natural-looking 9:16 Instagram Reels out — run by Claude.

```
inbox/  ──ingest──▶  library/  ──Claude tags moments──▶  plans/*.json  ──render──▶  out/<slug>/reel.mp4  ──queue──▶  Metricool ▶ Instagram
 GoPro, phone,       contact sheets,     (type, quality,        clips, text,        Remotion template:        daily slots,
 photos              speech detection    best 2–8 s moments)     music, caption      TikTok-style text, music   you approve first
```

- **Only your footage.** Every clip and photo in a reel comes from what you recorded and dropped in `inbox/`. Nothing is generated, and no stock or downloaded visuals are used: the renderer checks each file against the original it ingested and refuses anything else.
- **Classify:** Claude looks at a contact sheet of each clip and tags it (climb, descent, race, scenery…), scores it, and marks the best moments and where the rider is in frame.
- **360 video:** GoPro MAX / MAX 2 `.360` files go straight in. Claude picks the angles (road ahead, you, the group) and each reel shot is a reframed 9:16 view, a camera move or a tiny planet.
- **Subtitles:** clips where you talk get word-level transcripts (Whisper, on your Mac, free) shown as TikTok-style captions. Wordless ride footage gets a hook and short on-screen lines instead.
- **Text that looks and sounds like TikTok:** TikTok Sans (TikTok's own open-source font) in the classic white-box / black-box / plain styles, sentence case, written the way people actually post ("pov: the group ride was supposed to be chill"), not ad copy.
- **Music from YouTube:** `./reels music search` / `./reels yt2mp3 <url>` downloads tracks as MP3 with yt-dlp, records the source and credit line, and starts each reel past the song's intro.
- **Sound design:** your own ride sounds (freehub, clipping in, pass-bys) brought up for a moment, a music drop before the payoff, and a few sound effects from a small free (CC0) library — whooshes, pops, hits. Never AI-generated.
- **GoPro GPS:** real speed from the camera's GPS as a live pill, plus a small route map.
- **Weekly learning loop:** each batch starts from analytics, your feedback and production issues (`./reels review` → `learning/`). Lessons steer the next batch; registered experiments test improvements. Scheduling closes the production review, and the next week's analytics evaluates audience response.
- **Schedule:** approved reels get one slot per day and are scheduled through the Metricool connector (or exported for posting from your phone). Output is Instagram-spec (1080×1920, H.264, AAC 128k, -14 LUFS).

## Setup (once, ~10 minutes)

1. **Install Claude Code** (desktop app or terminal) and sign in.
2. **Run the setup script** in this folder (Terminal):
   ```bash
   cd ~/cycling-reels
   bash setup.sh
   ```
   It installs ffmpeg/Node/Python via Homebrew if they're missing, the Python environment (incl. yt-dlp), the Remotion template, Chrome Headless Shell for rendering, and the Whisper model, then runs `./reels doctor`.
3. **Edit `config.json`** (setup creates it from `config.example.json`): your handle (`brand.handle`), text style (`brand.textStyle`: `light`, `dark` or `plain`), posting time(s) and timezone, hashtags. `transcription.keyterms` holds words Whisper should get right (crit, KOM, your bike brand…).
4. **Optional check:** `./reels selftest` runs the whole pipeline on throwaway test clips in a temp folder (they never touch your library).

## Weekly use

0. On every ride, film the shots in [SHOTLIST.md](SHOTLIST.md). The ★ ones take about 5 minutes and unlock the formats that work best.
1. Copy the week's footage into `inbox/` (GoPro `.MP4` and 360 `.360`, iPhone `.MOV`/`.HEIC`/`.JPG`; export Insta360 clips to MP4 first).
2. Open this folder in Claude Code and say: **"Process my new footage and plan this week's reels."**
   Claude ingests, tags, transcribes, picks music, writes 7 plans, renders them and shows you previews.
3. Watch them (`out/<slug>/reel.mp4`), ask for changes ("make the hook funnier", "use this song instead: <link>", "swap the second clip"), then: **"Approve these and schedule them."**

### Learning every week

The skill runs `./reels review` before planning and closes a checkpoint after scheduling.
It separately tracks audience response, your preferences and production quality. It
records actual edits/rejections, render attempts/failures, scheduling issues, strengths,
weaknesses and next actions. Missing data stays unknown; small samples don't produce
automatic winners. Weekly analytics comparisons prefer snapshots collected at 7–14 days,
with actual ages retained rather than pretending these are exact day-seven results.

```bash
./reels review
./reels review --file notes.json
./reels review feedback --category preference --kind revision --slug <slug> --note "make the hook less polished"
./reels review experiment --file experiment.json
./reels review close --slugs <slug1> <slug2> --file notes.json
```

- `learning/LESSONS.md`: generated, concise guidance read before planning.
- `learning/reviews/YYYY-MM-DD.json`: all checkpoints and their evidence/decisions;
  the adjacent Markdown file summarizes the latest checkpoint. Dates are week-start Mondays.
- `learning/feedback.jsonl`: user feedback and production events, kept even after discards.
- `learning/experiments.json`: immutable hypotheses, primary metrics and test/control slugs,
  registered before test reels are rendered.

Aim for five familiar approaches and two tests in a seven-reel week when footage allows.
Experiments stay inconclusive until enough comparable results exist. Permanent skill,
recipe, shot-list or code changes are proposed for your approval, not silently applied.
See [.claude/skills/cycling-reels/reference/learning.md](.claude/skills/cycling-reels/reference/learning.md)
for input schemas, safeguards and missing-connector behavior. Test the learning commands
without media/network access with `PYTHONPATH=scripts .venv/bin/python -m unittest discover -s tests`.

### Music

Each batch, Claude checks this week's trending Reels audio lists (CreatorDB, Later, Scott Social…), picks songs that suit the footage, downloads them from YouTube and mixes them into the reels. Instagram recognizes songs in uploaded videos and credits the reel to the song (it shows up on the song's audio page), as if you'd picked it in the app. You don't need to do anything.

```bash
./reels music add https://www.youtube.com/watch?v=… --instagram "Title Artist"   # trending song
./reels music beats <track>                                                      # tempo and beat times
./reels music search "upbeat electronic no copyright" --free                     # free tracks (credit added to captions)
./reels music list                                                               # what you have, bpm, start point, rights
```

- Reels start at the best part of the song (the chorus or drop), and plans with `"cutOnBeat": true` put every cut on the beat.
- Paste a link to Claude to use a specific song.
- Now and then Instagram mutes a reel with a major-label song. If you see that on one, tell Claude and it'll swap the song.
- Optional: with an Instagram **Business** account connected to Metricool **through Facebook**, set `"audio": {"attachSongs": true}` in config.json and Metricool attaches Instagram's licensed copy instead (never muted, but songs play from 0:00 and only business-cleared songs work).

### Sound

- **Ride sounds:** at ingest, a small on-device model (YAMNet) lists candidate sounds in each clip — freehub buzz, shifts, pass-bys, brakes — with a spectrogram image Claude reads before tagging the clean ones. A plan can bring a clip's own sound up for a moment (`"nat"`) while the song dips, or cut it into a reusable effect (`./reels sfx add --from <clip> --at 9.1 --for 1.2`).
- **Sound effects:** `sfx/` holds a small library of free, human-made CC0 sounds (source, author and licence in each `.json`). The renderer adds a few automatically (a whoosh on fades and 360 swings, a soft pop on text, a hit on the payoff) and plans can place more; `./reels check` prints the cue sheet.
- **Music drops, punch-ins, loops:** the song can cut out for a beat before the payoff, the footage can punch in on a beat or a spoken word, and short reels can loop seamlessly so replays add watch time.
- Every cut gets a 30 ms audio fade so hard cuts don't click.

```bash
./reels sounds <clip>        # candidate ride sounds + spectrogram
./reels library --sounds     # tagged sounds, best first
./reels sfx list             # the sound-effect library
```

### GoPro GPS

GoPro clips carry GPS, read at ingest into `library/telemetry/`. A plan can show the real speed as a live pill (`"stats": [{"value": "gps"}]`, or `gps:max` / `gps:avg`) and a small route map (`"route": true`). Only stretches with a good GPS fix can show numbers. `./reels telemetry <clip> --fast 5` finds the fastest stretches. GoPro altitude drifts, so there's no elevation pill.

### 360 video (GoPro MAX / MAX 2)

Drop `.360` files straight from the camera into `inbox/` — no GoPro Player export needed. 360 MP4s (exported from GoPro Player/Quik or Insta360 Studio) and 360 photos work too.

- Claude sees the whole sphere on the contact sheet, finds where the road and the rider are, and saves them as named angles.
- Each shot in a reel is a 9:16 window aimed into the sphere: a fixed view, a camera move (e.g. "the look back": road ahead → you, mid-climb), or a tiny planet.
- Under the hood the two cube-face tracks in the `.360` are stitched back into a sphere with ffmpeg (seams cross-faded the way GoPro's own decoder does), then a virtual camera renders the 9:16 view.

Good to know:
- Raw `.360` isn't horizon-levelled or stabilised the way GoPro Player's export is. A handlebar or seatpost mount keeps it steady; `roll` fixes a tilted horizon. If you want GoPro's levelling, export a 360 MP4 from GoPro Player and drop that in instead.
- 5.6K/8K 360 is heavy to decode, so ingesting and rendering 360 clips takes a few times longer than normal clips. Camera moves are the slowest shots to render, since the virtual camera is re-aimed every frame.
- The MAX's 5.6K layout is well documented; the MAX 2's (8K) is less so. The decoder handles both from the file's own dimensions, but if a MAX 2 contact sheet shows visible seams, export a 360 MP4 from GoPro Player for those clips.

### Automate the batch (weekly scheduled task)

In the Claude desktop app, create a **local** scheduled task (Code → Routines/Scheduled → New, choose *Local*) with this folder as the working folder, weekly (e.g. Sunday 6 PM), and this prompt:

> Run the weekly cycling-reels batch in unattended mode using the cycling-reels skill. First refresh analytics when available, run ./reels review, read the learning records, and record evidence-backed strengths, weaknesses and next actions. Register any experiment before rendering its test reels. Ingest anything new in inbox/, tag it, transcribe talking clips, plan and render up to 7 reels from the best unused moments with songs from this week's trending Reels lists (or free-to-use tracks), and check each preview. Record production issues and fixes. Do not approve, queue, upload, schedule or close a scheduling checkpoint. Finish with a short report: review path, stale/missing analytics, lessons and experiments, proposed permanent changes, what was ingested, each rendered reel (slug, hook, length, music, preview path), anything uncertain, and how much unused material is left.

Your Mac has to be awake at that time (it's skipped otherwise). When it's done, open a session and approve/schedule as above. Approval stays with you on purpose: nothing goes out under your name unseen.

## Posting

- **Metricool (automatic):** connect your Instagram Business/Creator account in Metricool and enable the Metricool connector in Claude. Metricool takes media by link, so either set up the optional R2 bucket (`./reels upload`, see `.claude/skills/cycling-reels/reference/publishing.md`) or put the files in your Metricool-linked Google Drive/Dropbox. The free plan caps scheduling at 20 posts — daily posting needs a paid plan.
- **From your phone:** `./reels export --to "~/Library/Mobile Documents/com~apple~CloudDocs/Reels"` drops each queued reel and a caption `.txt` into iCloud Drive; post from the Instagram app (the `.txt` names the song to add if a reel was set up to have Instagram attach it).
- `out/QUEUE.md` always lists what's queued, when, and the caption to paste.

## Commands

Run `./reels --help` or see `.claude/skills/cycling-reels/SKILL.md`. The main ones:

| | |
|---|---|
| `./reels ingest` | inbox → library, contact sheets, speech detection |
| `./reels pending` / `./reels tag <id> --json …` | tagging (Claude does this) |
| `./reels library --unused` | available moments, best first |
| `./reels music search "…" --free` / `./reels yt2mp3 <url>` / `./reels music list` | find, download and list tracks |
| `./reels sounds <id>` / `./reels library --sounds` / `./reels sfx list` | ride sounds in a clip / tagged sounds / the sound-effect library |
| `./reels telemetry <id> --fast 5` | GoPro GPS: fastest stretches (`--at`, `--start/--end` too) |
| `./reels stats import <file>` / `./reels stats` | import a Metricool analytics pull / per-reel performance → `out/PERFORMANCE.md` |
| `./reels review` / `review feedback` / `review experiment` / `review close` | weekly retrospective, lessons, user feedback, production issues and experiments |
| `./reels check plans/x.json` / `./reels render plans/x.json` | validate (incl. the sound cue sheet) / render a reel |
| `./reels queue` / `./reels mark <slug> approved\|scheduled\|posted` | posting slots and status |
| `./reels export --to <folder>` | copy queued reels + captions somewhere |
| `./reels doctor [--fix]` / `./reels selftest` | health checks (`--fix` also updates yt-dlp) |

## Folders

| | |
|---|---|
| `inbox/` | drop new footage here; ingested files move out |
| `library/media/` | your originals, renamed `<date>-<name>-<hash>` — the only source of visuals |
| `library/assets/` | one JSON per asset: probe info, fingerprint, tags, moments, usage |
| `library/sheets/` / `library/transcripts/` | contact sheets / word-level transcripts |
| `plans/` | one JSON per reel (Claude writes these) |
| `out/<slug>/` | reel.mp4, cover.jpg, preview.jpg, caption.txt, post.json |
| `music/` | downloaded tracks (`.mp3`) with their source/credit records (`.json`) |
| `sfx/` | free sound effects (`.wav`) with source/author/licence records (`.json`) |
| `library/sounds/` / `library/telemetry/` | detected ride sounds + spectrograms / GoPro GPS per clip |
| `analytics/` | Metricool analytics snapshots (`./reels stats import`) |
| `learning/` | weekly review histories, generated active lessons, feedback and experiment designs |
| `models/` | the on-device sound tagger (YAMNet, Apache-2.0) |
| `remotion/` | the video template (`cd remotion && npm run studio` to preview/tweak the text styles) |

## Notes

- **Updating:** unzip a new version over this folder and run `bash setup.sh` again. Preserve your `config.json`, `library/`, `plans/`, `out/`, `music/`, `sfx/`, `analytics/` and `learning/`; these contain your settings, media and learning history, not replaceable application files.
- YouTube changes often; if downloads start failing, `./reels doctor --fix` updates yt-dlp. (The old `yt2mp3` Python package is abandoned and no longer works, so `./reels yt2mp3` is built on yt-dlp, its maintained successor.)
- Emoji in on-screen text use your Mac's emoji font.
- HDR iPhone/GoPro footage needs converting to SDR. Homebrew's standard `ffmpeg` can't tone-map, so HLG clips get a close approximation; `brew install ffmpeg-full` gives accurate tone-mapping and is picked up automatically. Shooting SDR (iPhone: Settings → Camera → Record Video → HDR Video off) avoids the issue entirely.
- Long rides are fine: Claude skims the contact sheets and zooms into interesting stretches.
- Remotion is free for individuals and companies of up to 3 people. TikTok Sans is under the SIL Open Font License.
- Nothing here talks to Instagram directly; posting goes through Metricool or you.
