# Performance analytics

Instagram's numbers for each posted reel, pulled from Metricool and turned into a report the planner reads before choosing the next batch.

## When

At the start of every weekly batch, before planning. Metrics keep growing for days, so a reel is only judged once it is 48 h old (the report marks younger ones with `*`).

## Pull

1. Call the Metricool connector `getAnalyticsDataByMetrics` with `brandId` 7655493, `from`/`to` covering the last ~3 weeks (so older reels get refreshed numbers) and
   `metrics` = `["IGRE02","IGRE03","IGRE04","IGRE06","IGRE07","IGRE10","IGRE11","IGRE12","IGRE21","IGRE23","IGRE24","IGRE25","IGRE27","IGRE28","IGRE29"]`.
   Rows come back in that order: timestamp (UTC), caption, reelId, url, comments, likes, reach, saved, shares, views, averageWatchTime (s), videoViewTotalTime (s), retention, reelsViewRate, reposts. Values are strings or null.
2. Save it in the scratchpad as `{"pulledAt": "<ISO time now>", "fields": [<the metric list above>], "rows": [<rows as returned>]}`. (A bare `{"rows": …}` also works; the default field order above is assumed.)
3. `./reels stats import <file>` validates it, stores a snapshot in `analytics/` (snapshots are kept; the latest per reel wins), matches rows to `out/*/post.json` by caption and posting time (within 3 h) and records `instagramUrl` and `reelId` there. Unmatched rows are listed, not dropped.
4. `./reels stats [--since YYYY-MM-DD]` prints the table and writes `out/PERFORMANCE.md`. Read that file.

5. Run `./reels review` and follow [learning.md](learning.md). This adds persistent weekly
   strengths/weaknesses, preferences, production issues and experiment results to the
   performance report. It works without analytics and explicitly flags missing/stale data.
   Under-48-hour samples are unjudged, 2–7-day samples preliminary; prefer 7–14-day
   collection windows for weekly comparisons, retaining actual ages and never treating
   lifetime metrics as exact day-seven results. This stricter learning policy does not
   change the existing stats report's descriptive 48-hour tables.

## Account numbers

The per-reel pull says whether a reel was watched. Whether the account is growing comes from Metricool's Instagram "evolution" metrics, pulled in the same sitting:

1. Call `getAnalyticsDataByMetrics` with the same `brandId` and dates and `metrics` = `["IGEV01","IGEV43","IGEV44","IGEV06","IGEV42"]`: followers, followers gained, followers lost, account reach, accounts engaged. Each row is those values in order and then the day as `YYYYMMDD`; days come back unordered.
2. Save it as `{"pulledAt": "<ISO time now>", "fields": [<that list>], "rows": [<rows as returned>]}` and run `./reels stats import <file>`. A pull whose fields are all `IGEV…` is stored in `analytics/account/`, apart from the reel snapshots. A later pull replaces a day's values, but an unknown never erases a known one.
3. `./reels stats` adds an **Account** section to `out/PERFORMANCE.md`: the latest follower count, and each day's account reach, accounts engaged and follower change beside the reel posted that day. `./reels review` records the count and its change over the last two weeks.

Reading it:

- **Follows per reel don't exist here.** Metricool has no profile-visits or follows-from-this-reel number (its profile-views metric is deprecated). A day's follower change beside a reel is a description; several reels are live at once, so don't credit one reel with it. Look for the same kind of reel sitting beside gains more than once.
- **The follower series is patchy, and only refreshes when the user opens Metricool.** Ask them to open it before the pull. As of the 2026-11-10 pulls Metricool returned a count on 10 of 22 days (0 through 2026-10-31, 1 on 11-06, 14 on 11-08, 19 on 11-09) and nothing for gained/lost. Unknown is shown as —, never as zero, and the count is labelled as Metricool's. If it disagrees with the number on the profile, say so and trust the profile.
- **Accounts engaged** per day is the closest thing to "people who did something"; set it against account reach rather than reading it alone.

## Plans carry a `format`

Set `"format"` (the recipe name from [recipes.md](recipes.md), e.g. `"pov-joke"`) in each plan so the report can group by format. Plans without it show "—".

## Reading it

Instagram's head, Adam Mosseri, names watch time, likes per reach and sends (shares) per reach as the top ranking signals.

- **3 s hold rate** (share of views past 3 s): low means the hook and first second fail. Change the opening shot or hook text.
- **Watch-through** (avg watch time / length): low means the reel is too long or the pacing drags. Shorten or cut faster.
- **Shares/reach and saves/reach**: high means relatable (shares) or useful (saves). Make more of what earns them.
- **Likes/reach**: the general "liked it" signal.
- **Views/reach** (replays): views per account reached; above 1 means people watched it again — what loops are for.
- **Comments/reach and engagement/reach** (likes + comments + shares + saves, per reach — Metricool's "interactions"): whether people responded, not just watched. Through the Nov 3 pull every reel had 0 comments and 0–2% engagement; the caption prompts in [captions.md](captions.md#give-people-a-reason-to-answer) target them. Engagement is unknown (—), not 0, when any part is missing.
- IGRE28 ("reel view rate") is Instagram's share of views that lasted past 3 s — the `3s hold` column.
- Compare formats, songs and 360 vs flat footage only once each group has a few reels. The report's group tables are only shown with 2+ groups of reels older than 48 h.
- Small-account numbers are noisy: act on big gaps, not 5% differences. The report gives numbers and rankings only; interpreting them is the planner's job.

## Footage lineage and the Footage section

`./reels render` saves what went into each reel to `out/<slug>/lineage.json`: one entry per segment and per cutaway (`"cutaway": true`), with the full asset id, the matched moment id, a snapshot of its tag values (`score`, `role`, `speaker`, `face`, `mood`, `opener`, `line`, `thread`, `beat`), `position` and `share` of runtime. The snapshot is the point: tags change later, and the file keeps what the reel was built from. Reels rendered earlier get one from `./reels stats backfill-lineage` (idempotent, `"backfilled": true`), which uses today's tags, so a backfilled snapshot isn't a render-time record.

- **Moment match:** the moment sharing the most seconds with the clip (ties: closest fit, then earliest). `momentCoverage` is the share of the clip it covers. No overlap means `moment: null`, `match: "none"`. Photos have no moments, so `moment` is null, `match` is `"photo"` and `score` is the photo's own.
- **Share and position** follow the picture. A segment ends where the next one starts (a fade counts toward the outgoing shot), and a cutaway takes its seconds out of the segment under it. `first` is the shot on screen at 0 s, including a cutaway placed at 0 s; `last` is the shot on screen at the end.
- **`voiceShare` (lineage v2):** unmuted seconds of the rider's voice, over runtime. The matched moment must have `speaker: me` and either `role: voice` or the segment must have `captions: "transcript"`. Teammates and unknown speakers don't count. The renderer's effective volume curve (including nat ramps), audio-track availability, speed limits and Instagram ride-volume setting determine unmuted time, saved as `audioSeconds` on each main segment. Time under cutaways still counts; cutaways themselves are silent. This is a segment-level estimate that includes pauses, not a word-by-word speech or intelligibility measurement.
- **`faceShare`:** seconds on screen of entries whose snapshot has `face: true`, over runtime. A cutaway covering a face segment is not face time. A missing `face` (photos have none) counts as no face.
- **`avgShotSeconds`:** runtime divided by picture shots. A cutaway in the middle of a segment makes three shots.
- **`thread`:** the most common `thread` among segments, not cutaways (ties: more seconds, then name), or null.
- **Mean moment score:** the score of each entry weighted by its share of runtime; entries with no score don't count.

The Footage section of `./reels stats` and `out/PERFORMANCE.md` is descriptive. No code changes scores or tags from it; a change to a tag is a judgment made and saved with `./reels tag`. It leaves out reels under 48 h old and reels without lineage, and says how many.

`./reels stats backfill-lineage` also upgrades v1 voice metrics to v2, preserving the original tag snapshots and their timestamps, and recording `voiceUpdatedAt`. Audio is reconstructed from the saved plan/post and current audio defaults, so historical audio estimates may differ if defaults changed. If the saved plan no longer matches the recorded timeline, the upgrade refuses that reel rather than rewriting its history. Existing v2 files are left alone. Stats excludes legacy metrics until upgraded, so old and corrected definitions aren't mixed.

- **3 s hold** is credited to the opening shot only: its score, role, opener, face and speaker.
- **Watch-through** is credited to the reel's mix: voice share (0%, under 50%, 50% or more), face share (same bands), average shot (under 3 s, 3–5 s, over 5 s) and thread.
- **Shares and comments** per reach are shown only by format and thread.
- **Score calibration** shows opening-shot score against 3 s hold and mean moment score (under 3.5, 3.5 to under 4, 4 or more) against watch-through, with each group's range.
- Every table is split by length band (under 11 s, 11–15 s, over 15 s), so compare rows within one band. A group counts only reels that have the metric; any group under 5 reels says "too small to act on" in its row.
