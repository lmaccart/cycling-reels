# Audit — 2026-11-05

A full read of the pipeline (CLI, renderer, Remotion template, skill docs, learning records, plans and
analytics), aimed at three questions: what's broken or fragile, what would keep viewers watching longer,
and what would get them to respond. Plus: what Kino is and where it fits.

Everything marked **built** is in this branch with tests; **proposed** items need your call.

## The evidence so far

Latest Metricool pull (Nov 3) for the six reels posted Oct 26–31. The Nov 2–8 batch isn't measured yet.

| reel | views | reach | views/reach | avg watch | 3 s hold | likes | comments | saves | shares | engagement |
|---|---|---|---|---|---|---|---|---|---|---|
| golden-bridge | 216 | 182 | 1.19× | 4.0 s | 46% | 1 | 0 | 0 | 0 | 0.5% |
| group-ride-chill | 181 | 163 | 1.11× | 5.1 s | 53% | 2 | 0 | 0 | 0 | 1.2% |
| bike-trip | 178 | 157 | 1.13× | 3.8 s | 43% | 2 | 0 | 0 | 1 | 1.9% |
| first-ride-back | 659 | 511 | 1.29× | 7.6 s | 63% | 3 | 0 | 0 | 0 | 0.6% |
| chasing-the-group | 169 | 157 | 1.08× | 4.7 s | 56% | 0 | 0 | 0 | 0 | 0.0% |
| bridges | 154 | 127 | 1.21× | 4.7 s | 50% | 0 | 0 | 0 | 0 | 0.0% |

- **Interaction is the biggest gap:** 0 comments and 0 saves on every reel, 0–3 likes, one share in total.
  People watch; almost nobody responds — and sends (shares) per reach are one of the three signals Instagram's
  head has named ([dataslayer](https://www.dataslayer.ai/blog/instagram-algorithm-2025-complete-guide-for-marketers),
  [socialync](https://www.socialync.io/blog/adam-mosseri-shares-instagram-algorithm-2026)).
- **Retention is middling and uneven:** 3 s hold 43–63%. The best reel (first-ride-back: a face, a contrast,
  a spoken payoff; ~3× the reach) also had the best watch-through (70.6%, per the Nov 1 review). The worst
  (bike-trip: 6 shots, 4 of 5 cuts crossfades) had the lowest hold (43%) and 23.9% watch-through.
- Six reels is a tiny, observational sample — these are directions to test, not proven causes.

## Retention

| # | Finding | Status |
|---|---|---|
| R1 | **The hook was invisible on frame 0.** Its entrance animation started at opacity 0, so the first frame — the feed's first impression, and where every loop replay restarts — had no text (verified on a render: frame 0 blank, frame 2 half-faded, frame 6 full). The recipe rule is "the hook text is up from frame 0." | **built** — the opening hook is fully there on frame 0; only its exit animates |
| R2 | **Talking reels couldn't cut away while the voice kept going** — each clip played only its own sound, so the voice stopped at every cut. TALKING-REELS.md listed `cutaways` as "needed"; both scripts picked on Nov 1 depend on it. | **built** — segment field `cutaways`: library B-roll over a clip (video, photo or 360) while its audio, captions, text and punch-ins carry on; footage guard, usage tracking, check output, selftest |
| R3 | **Stray half-word captions** were caught by hand twice in the Nov 2–8 batch ("on", "left"); the Nov 2 review proposed a check. | **built** — `check` warns when an in-point cuts into a word (with the time to move to), an out-point clips one, or transcript words are low-confidence (often wind) |
| R4 | **Slideshow edits and dead air aren't flagged.** bike-trip (worst hold) was 4/5 crossfades; bridges 2/3. | **built** — `retention:` warnings for a photo as the first shot, a crossfade in the first 2 s, mostly-fade edits, and 5 s+ where nothing changes on screen |
| R5 | **Nothing stops the same footage appearing in two reels** (the skill's rule) — usage was only shown in `library --unused`. | **built** — `check` warns when a segment or cutaway reuses footage another reel already shows |
| R6 | Caption pages hold up to 6 words / 32 chars. Shorter pages (2–4 words) read faster on a phone; the `highlight` caption style exists but is untested. | proposed — run as an experiment on two talking reels, not a default change |
| R7 | Non-loop reels fade the music out over 1.2 s — a cue that it's over, right before the replay. | proposed — try 0.4–0.6 s on non-loop reels |
| R8 | Reels > 12 s have no mid-reel "rehook" (a text beat at ~40–60% that renews the promise). | proposed — recipes guidance once the length lesson (7–12 s) is re-checked with 7-day data |

## Interaction

| # | Finding | Status |
|---|---|---|
| I1 | **Captions rarely give a reason to respond**, and comment prompts were "optional, not on every reel." | **built (docs)** — one reason to respond per reel (question, take, send prompt, pick, next episode), rotated; Meta-style engagement bait ("comment YES", "tag 3 friends") ruled out; `firstComment` used to start the thread |
| I2 | **No Collabs or people tags.** Group-ride reels with the club are the account's natural reach multiplier: a Collab shows the reel on both profiles and to both audiences. The Metricool connector supports `instagramData.collaborators` (≤ 5) and people tags (`carouselTags`). | **built** — `post.collaborators` / `post.peopleTags` (validated handles, never guessed) → post.json, QUEUE.md, phone export, publishing.md mapping |
| I3 | **The cover frame was never sent to Metricool**, so Instagram picked the grid thumbnail itself. | **built** — post.json `coverMs` → `videoCoverMilliseconds` (or `videoThumbnailUrl` from an R2 upload); `isAiGenerated: false` set explicitly |
| I4 | **Analytics never measured responses or replays** — comments were a raw count, reposts unused, views/reach (replays: what loops are for) missing; experiments couldn't target them. | **built** — `commentsReach`, `engagement` (likes+comments+shares+saves per reach; unknown when any part is missing) and `viewsReach` in `./reels stats`, PERFORMANCE.md, weekly review samples and experiment metrics |
| I5 | Replying to early comments is the user's highest-leverage habit, and nothing prompted it. | **built** — QUEUE.md reminder |
| I6 | Trial Reels (non-followers first) would be ideal for experiments, but Metricool needs 1,000+ followers and they can't have collaborators. | documented in publishing.md; not built |
| I7 | `hashtags.always` is two very broad tags (#cycling, #roadcycling). Captions with plain searchable words matter more on Instagram now; niche/local tags (#houstoncycling, #collegecycling) suit a small account better. | proposed — your call on config.json |
| I8 | Posting time is a fixed 18:30. Metricool has `getBestTimeToPostByNetwork`. | proposed — check it monthly before changing `schedule.times` |

## Kino

"Kino" is three different products: a discontinued Linux DV editor
([Wikipedia](https://en.wikipedia.org/wiki/Kino_(software))), Lux's iPhone camera app *Kino – Pro Video Camera*
([App Store](https://apps.apple.com/us/app/id6472380172)), and **Kino (kino.ai)** — the one that matters here.

**What kino.ai is:** a browser/desktop video editor and media manager from Kino AI (YC S23) that relaunched
around Oct 29, 2026 as "a video editor for teams and agents": footage is indexed on import (transcribed,
embedded, described, face-detected), an agent can search it in plain language and build rough cuts on the
timeline, people leave notes and share review links, and it exports to Premiere/Resolve. Agents connect over
MCP — `claude mcp add kino https://kino.ai/mcp`, or the desktop app's local connector via the
[`kino-ai/kino-plugin`](https://github.com/kino-ai/kino-plugin) Claude Code plugin. Its docs say it can **import a
Remotion project folder and keep editing it as a Kino composition**. The free tier includes the editor and MCP
access with a weekly call cap; Pro (reportedly ~$25/editor/month) adds share links and unlimited calls.
Sources: [kino.ai](https://kino.ai/), [docs](https://kino.ai/docs), [editing docs](https://kinoai.mintlify.app/features/editing),
[changelog](https://kino.ai/changelog), [launch post](https://x.com/lukeigel/status/2115876931749200320),
[review (zh)](https://www.aiposthub.com/kino-ai-video-editor-claude-code-codex-mcp-review/),
[Outlier review](https://outlierkit.com/resources/kino-ai-review/).

**Where it fits:**

| Option | Verdict |
|---|---|
| Replace the renderer with Kino | **No.** The local renderer is what guarantees every frame is your footage (fingerprint check), keeps facts tied to GPS/Strava data, and works offline. |
| Post Kino's exports | **No**, for the same reason: a file out of Kino can't be verified. |
| Review + fine-tuning on a real timeline | **Yes — built.** `./reels project <plan>` writes one reel as a standalone Remotion project (template, exact props, only that reel's staged clips). Import it into Kino for a review link and time-coded notes, or open it locally with `npm run studio`. Notes come back as plan edits; `./reels render` makes the reel that posts. Verified here: the exported folder typechecks and renders on its own to the same 6.5 s reel. |
| Plain-language footage search | **Optional, later.** It means uploading raw footage (with teammates' faces) to a third-party cloud; the contact-sheet tagging already covers planning. Only with your say-so. |

Rules for Kino are in `.claude/skills/cycling-reels/reference/kino.md` (opt-in, no generative features, no
raw-library uploads without asking, approval unchanged). Caveat: Kino relaunched a week ago and this session's
network couldn't reach kino.ai, so the details above come from its docs as indexed by search, not from using
it; tool names are read from the connector at run time. The Lux camera app is a filming choice, not an editing
one — its 180° "AutoMotion" shutter would add natural motion blur, but it doesn't change anything in this repo.

## Correctness and code health

| # | Finding | Status |
|---|---|---|
| C1 | **`queue` could book a slot that had already passed** (a `postDate` of today, or `--start` today): at noon it booked today's 06:00. Metricool refuses past dates. | **fixed** — slots must be ≥ 15 min ahead; test fails on the old code |
| C2 | **Re-rendering wiped `history`, `metricoolId`, `reelId` and `instagramUrl`.** After a post-scheduling edit the scheduled Metricool post became untraceable (risk: a second scheduled copy) and the analytics link was lost. | **fixed** — carried over; the reel still needs re-approval and re-upload |
| C3 | `stats import` silently saved a snapshot with a future `pulledAt` (local time with a Z), which the review then skipped (happened Nov 1). | **fixed** — refused at import with the fix |
| C4 | `./reels check a.json b.json` stopped at the first broken plan. | **fixed** — checks every plan, lists all failures, exits 1 |
| C5 | An unreadable asset record vanished from the library silently (and its usage with it). | **fixed** — warns |
| C6 | 360 camera command files were named `cam<index>.cmd`, so two staged clips with the same segment index shared one file — which cutaways (and a one-segment loop's seam check) do. | **fixed** — named after their output file |
| C7 | No CI; a compiled `.pyc` was committed. | **fixed** — GitHub Actions runs the unit tests (with ffmpeg) and a template typecheck on PRs; `.pyc` removed |
| C8 | Tests covered learning, bleeps and Strava, not the timeline builder. | **built** — 47 → 85 tests: cutaways, retention checks, caption cuts, reuse, handles, carry-over, queue slots, analytics ratios, project export, multi-plan check |
| C9 | `render.py` is ~1,700 lines (props, staging, encoding and check output together). | proposed — split timeline/props, staging/encoding and `check` printing into modules when next touched |
| C10 | Audio is encoded lossy three times (staged AAC 192k → Remotion AAC 192k → final AAC 128k). | proposed — stage PCM audio; small but free quality |
| C11 | `requirements.txt` has only lower bounds (`yt-dlp` must float; the rest needn't). | proposed — pin Pillow/faster-whisper/onnxruntime for reproducible setups |

## Privacy

- **GoPro GPS tracks** (`library/telemetry/`): **done 2026-11-07** (user's call). They're git-ignored and untracked, and local copies stay. Older commits still contain them (private repo). Strava is the main GPS source now; see plan-schema.md "GPS: Strava first".
- Transcripts (teammates' voices, names) are committed too — fine for a private repo; worth knowing if it's ever shared.
- Kino uploads are opt-in, one reel at a time (see above).

## What to do next

1. Review this PR; run `./reels selftest` on the Mac (it needs the Whisper/YAMNet downloads this session couldn't reach — here steps 1–7 were run directly and passed, including the new cutaway check).
2. Tell Claude which accounts are fine for Collabs/tags (e.g. the club), and whether to try Kino.
3. Film the two talking reels in TALKING-REELS.md — cutaways now make them work as written.
4. Record this audit in the learning history: on the Mac, after merging, run
   `./reels review --week 2026-11-02 --file learning/notes/2026-11-05-audit.json`. It adds a checkpoint
   (strengths, weaknesses, the Nov 2 proposal marked implemented, proposals) and regenerates `learning/LESSONS.md`
   with two new lessons: `aud-interaction-01` and `prod-captions-01`. It wasn't run here because this checkout has no
   `out/`, so the review would have matched none of the posted reels. Then consider registering a comments experiment
   (`primaryMetric: "commentsReach"`) for the caption prompts.
