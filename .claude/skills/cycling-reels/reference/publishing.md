# Publishing approved reels

Instagram's API can publish but can't schedule, so scheduling lives in a scheduler that posts for you at the chosen time. This project is set up for **Metricool** (it has an official Claude connector), with a manual fallback.

Only reels the user approved get scheduled. Start with `./reels queue`, which gives each one a slot and writes `out/QUEUE.md`.

The text to post is always `out/<slug>/caption.txt`: the caption, the music credit line when the track has one (NCS and Creative Commons tracks require it), and the hashtags. Trending songs (added with `--instagram`) have no credit line — Instagram shows the song on the reel.

## A. Metricool connector (automatic)

One-time setup by the user: a Metricool account with their Instagram **Business or Creator** account connected, and the Metricool connector enabled in Claude. Metricool's free plan caps scheduling at 20 posts, so daily posting needs a paid plan.

Metricool can't take a file from this computer directly — the video must be reachable by link: a public URL, or a file in the Google Drive / Dropbox account linked to Metricool.

1. Get a link for each queued reel:
   - If upload is configured (see C), run `./reels upload --queued`; each `out/<slug>/post.json` gets a `mediaUrl`.
   - Otherwise ask the user to put the reel files in their Metricool-linked Google Drive or Dropbox and share the links.
2. Look up the brand and timezone with the connector's brand/settings tool.
3. For each queued reel, call the connector's schedule-post tool with:
   - date/time = `scheduledFor` from post.json, timezone = `timezone` from post.json
   - provider/network `instagram`, Instagram type `REEL`, show on feed `true`
   - text = the contents of `out/<slug>/caption.txt`
   - media = the reel's link; first comment = `firstComment` if set
   - not a draft, auto-publish on
   - **the song**, when post.json has `instagramAudio` (see below)
   Tool and field names vary between connector versions, so read the tool's schema rather than assuming. If Metricool rejects a field, show the user the error instead of guessing.
4. Confirm with the connector's list-scheduled-posts tool, then record it: `./reels mark <slug> scheduled --metricool-id <id>`.
5. Close the weekly production review per [learning.md](learning.md):
   `./reels review close --slugs <all intended scheduling-batch slugs> --file <notes.json>`.
   Include posts still queued/approved after failures; record sanitized scheduling errors
   and fixes with `./reels review feedback`. The checkpoint preserves IDs, status, slots,
   lessons and experiments; it does not schedule anything or imply successful publication.

### Attaching the song (Instagram audio)

Only when config `audio.attachSongs` is on (it's off by default: trending songs are mixed into `reel.mp4`, which needs nothing extra here). Then reels whose track was added with `--instagram` upload without the song (`reel.mp4` has ride audio only), so the song must be attached or the reel goes out without music. post.json's `instagramAudio` holds what to send:

- `manual: false` → add to the Instagram data: `"audioConfiguration": {"audioId": "<instagramAudio.audio>", "audioVolume": <audioVolume>, "videoVolume": <videoVolume>}`. `audioId` takes a numeric Instagram audio id or a "title artist" search term. If the search matches several catalog entries, the request is rejected with the candidates: pick the original version by the right artist (not a sped-up/remix unless that's the trend) and retry with its numeric id; then save that id in `music/<track>.json` as `"instagram": {"audio": "…", "id": "<id>"}` so the next reel finds it directly. Metricool fills in title/artist/artwork itself — don't send them.
- `manual: true` (the reel needs the song from a mid-song point) → schedule with auto-publish **off**: Metricool sends a push at the time and the user posts it from their phone, adding the song in the Instagram app at the point shown in `out/QUEUE.md`.
- Rejected because the account can't attach audio (it needs an Instagram **Business** account connected to Metricool through Facebook with a linked Page; Instagram-Login connections and some Creator setups can't) or because the song isn't licensed for third-party publishing: don't schedule it without its song. Tell the user and ask — fix the connection, turn attaching off and re-render (song mixed in), post by hand (auto-publish off), or choose another song.

Notes:
- Mixed-in music goes out inside the video: trending songs are usually recognized and credited by Instagram, though a reel with a major-label song is occasionally muted; no-copyright tracks carry their credit line in the caption.
- Audio must be AAC ≤128 kbps and video ≤25 Mbps — the renderer already outputs that.
- Changing a reel after it's scheduled: re-render, re-upload, then update the scheduled post (or delete and recreate it) with the connector, and confirm with the user.

## B. Manual (no connector)

Send the user `out/QUEUE.md`. For each entry they upload `reel.mp4` in Instagram (or Meta Business Suite / Metricool's web planner) at the listed time and paste the caption block — and when the entry lists a song, add it in the app (the file has no music). Optional: `cover.jpg` as the cover. When done: `./reels mark <slug> scheduled` (or `posted`).

Run the same closing review for this batch. Unconfirmed manual posts stay queued and
the checkpoint stays incomplete; only mark scheduled/posted after the user confirms.

To post from a phone, export the queue to iCloud Drive (or any synced folder) — each reel lands next to a `.txt` with its time, caption, credit and hashtags:

```
./reels export --to "~/Library/Mobile Documents/com~apple~CloudDocs/Reels"
```

## C. Public links via Cloudflare R2 (optional, enables A without Drive/Dropbox)

`./reels upload` copies reels to any S3-compatible bucket and records public URLs. R2 has a free tier and no egress fees.

1. Cloudflare dashboard → R2 → create a bucket (e.g. `reels`).
2. Bucket → Settings → enable public access on the r2.dev subdomain; note the `https://pub-….r2.dev` URL.
3. R2 → Manage API tokens → create a token with Object Read & Write on that bucket; note the Access Key ID and Secret.
4. In `config.json` → `upload`: `"endpoint": "https://<ACCOUNT_ID>.r2.cloudflarestorage.com"`, `"bucket": "reels"`, `"publicBaseUrl": "https://pub-….r2.dev"`.
5. Create `.env` in the project root (it's git-ignored):
   ```
   REELS_S3_ACCESS_KEY_ID=…
   REELS_S3_SECRET_ACCESS_KEY=…
   ```
6. `.venv/bin/pip install boto3`, then `./reels doctor`.
7. Optional: an R2 lifecycle rule that deletes objects after 30 days, since Metricool only needs them until they post.

Never print or paste the secret key into chat or plan files.

## Other routes (not built in)

- **Postiz** — open-source scheduler you can self-host, with its own MCP server.
- **Instagram Graph API directly** — publish from a Meta app in development mode with the user's own account as an Instagram Tester (no App Review). Needs a scheduler of its own (a daily scheduled task) since the API only publishes immediately.
