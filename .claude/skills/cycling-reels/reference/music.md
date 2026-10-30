# Music: this week's trending Reels songs

Trending audio is how small accounts get found: a reel using a song that's trending sits on that song's audio page, and the song itself makes people stop. Trends move fast (an audio trend usually peaks within 7–10 days), so pick from the lists for the week you're planning, never from memory or an old batch.

**How the song gets on the reel:** download it from YouTube and mix it into the video like any track. Instagram runs uploaded audio through its matcher: when it recognizes licensed music it credits the reel to the artist and song and adds it to the song's audio page, as if the song had been picked in the app. Matching works best on a clean copy of the original recording that sits on top of the mix, so use the official audio, keep the song loud (ride audio stays low by default), and don't speed it up or talk over most of it. The trade-off: a reel with a major-label song can occasionally be muted or limited (more likely on a Business account), and a mix Instagram doesn't recognize shows as "Original audio".

**Optional: attaching Instagram's copy instead** (config `"audio": {"attachSongs": true}`). Metricool attaches the song from Instagram's licensed library at scheduling, the upload carries only ride audio, and the reel can't be muted. It only works on an Instagram **Business** account connected to Metricool through Facebook with a Page, only with songs cleared for business accounts, and the song always plays from 0:00. Turn it on only when the user asks for it and has confirmed that setup — see [Attaching](#attaching-instagrams-copy-audioattachsongs) below.

## Each batch

1. **Get the current lists** (WebFetch; check the date on each page):
   - CreatorDB weekly chart: <https://creatordb.app/free-tools/trending-instagram-audio/> — aggregates several weekly trend reports plus Spotify/Apple/YouTube movement; heat score and number of reels per song.
   - Later's weekly Reels trends: <https://later.com/blog/instagram-reels-trends/> — the trend *formats* that go with each sound, with how-tos.
   - Scott Social Marketing: <https://www.scottsocialmarketing.com/blog/trending-reels-audio-this-week-on-instagram> — updated ~3×/week; marks whether each sound is available to business accounts.
   - Cross-checks: SocialBee (`socialbee.com/blog/trending-instagram-songs-this-week/`), NewEngen (`newengen.com/insights/instagram-trends/`), Metricool's weekly top 10 (`metricool.com/trending-instagram-songs/`).
2. **Shortlist 3–6 songs** that suit cycling footage:
   - Energy matches the reel: driving/upbeat for speed, group rides and beat edits; warm or cinematic for scenery, golden hour, fall rides; the trend's own format when you're doing that trend.
   - Skip lyrics that clash with the reel. On a Business account, prefer sounds marked available to business accounts (Scott Social's ✅) — those are least likely to be muted.
   - Not the same song two days running; vary energy across the week.
3. **Download it** from the song's official upload — ideally the `<Artist> - Topic` channel or "Official Audio". That's the recording Instagram's matcher knows, so the reel gets credited to the song (and beats line up). Avoid sped-up, nightcore, lyric-video or "no copyright" re-uploads unless that version is the trend.
   ```
   ./reels music search "<title> <artist> official audio"
   ./reels music add <url> --instagram "<Title> <Artist>"
   ```
   `--instagram` marks it as a trending song: no credit line in the caption (Instagram shows the song) and no free-to-use warnings. Beats are detected on download (`./reels music beats <slug>` lists them).
4. **Plan with it:** `"music": {"track": "<slug>", "cutOnBeat": true}`. It starts at the song's loudest stretch, usually the chorus or drop (`./reels music list` shows where); set `start` for the exact moment a trend uses. Cuts and the ending snap onto the beat. Leave `volume` at the default (config `audio.musicVolume`, 0.5 — the user found 0.8 too loud): the song still sits well above the ride audio, so Instagram should still recognize it, and the song ducks under speech. If a posted reel with a trending song shows "Original audio" instead of the song, tell the user and raise `volume` for that song's next reel. Ride-sound moments (`nat`) and `musicDrops` also take the song down, so keep them under a quarter of the reel together (`./reels check` warns) and away from the song's hook.
5. **Review** `out/<slug>/reel.mp4`: it's exactly what's uploaded, song included.
6. **Schedule** as usual — [publishing.md](publishing.md). Nothing extra for the song.

The first time in a session that you use a copyrighted trending song, tell the user once: it's mixed into the video from YouTube, Instagram usually recognizes it and shows the song, and now and then a reel with a major-label song gets muted.

## If Instagram mutes a reel

The user sees a muted / "audio unavailable" notice on it in the app. Ask before changing anything: re-render it with another trending song (business-available ones are safest) or a no-copyright track and repost, and if it keeps happening, the account-level fixes are switching the Instagram account from Business to Creator, or turning on attaching (Business account connected to Metricool through Facebook).

## Attaching Instagram's copy (`audio.attachSongs`)

With it on, `--instagram` tracks are never put in the upload:
- Shortlist only business-available songs that work **from 0:00** (Metricool can't trim — drop long intros). A trend built on a mid-song moment has to be posted by hand.
- Plan without `start`. Volumes default to song 100 / ride audio 20, or song 25 / voice 100 when the reel has speech captions — override with `audioVolume` / `videoVolume` (0–100). Instagram mixes at constant levels (no ducking).
- Review `preview.mp4` (the song mixed in at those levels); `reel.mp4` is the upload without the song.
- Setting `start` marks the reel `manual`: it's scheduled with auto-publish off and the song and start point go in `out/QUEUE.md`.
- If Metricool rejects `audioConfiguration` (not a Business account connected through Facebook, or the song isn't licensed for third-party publishing), don't post the reel without its song. Ask the user: turn attaching off and re-render (song mixed in), post by hand, or pick another song.
- A plan can override the config either way with `"instagram": true` / `false` in its music block.

## Unattended runs

May check the lists, add trending songs and render with them (nothing is published), but never schedule.

## No-copyright tracks

For talking reels, ride-audio-first reels, or when the user wants no mute risk at all: `./reels music search "<mood> no copyright" --free`, then `./reels music add <url>` without `--instagram`. Their credit line goes in the caption automatically. Never pick a `⚠` track without the user's say-so.

## Beat timing

- `cutOnBeat` moves each cut (a fade's midpoint) and the ending onto the nearest beat, changing a segment by less than a beat. `./reels check` prints the cut times.
- Plan rough lengths in beats: `./reels music beats <slug>` gives the beat length (e.g. 0.5 s at 120 bpm). Montage shots of 2 beats feel quick, 4 beats steady, 8 beats slow.
- Time text beats to land on a beat too (`at` = a beat time minus the segment's start).
- At very fast tempos the detector can return every other beat (e.g. 77 for a 154 bpm song) — cuts still land on real beats, just fewer of them.
