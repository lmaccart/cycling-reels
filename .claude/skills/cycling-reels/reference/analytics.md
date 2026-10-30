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

## Plans carry a `format`

Set `"format"` (the recipe name from [recipes.md](recipes.md), e.g. `"pov-joke"`) in each plan so the report can group by format. Plans without it show "—".

## Reading it

Instagram's head, Adam Mosseri, names watch time, likes per reach and sends (shares) per reach as the top ranking signals.

- **3 s hold rate** (share of views past 3 s): low means the hook and first second fail. Change the opening shot or hook text.
- **Watch-through** (avg watch time / length): low means the reel is too long or the pacing drags. Shorten or cut faster.
- **Shares/reach and saves/reach**: high means relatable (shares) or useful (saves). Make more of what earns them.
- **Likes/reach**: the general "liked it" signal.
- Compare formats, songs and 360 vs flat footage only once each group has a few reels. The report's group tables are only shown with 2+ groups of reels older than 48 h.
- Small-account numbers are noisy: act on big gaps, not 5% differences. The report gives numbers and rankings only; interpreting them is the planner's job.
