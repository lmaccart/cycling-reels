# Strava ride data — plan (2026-11-04)

Status: **built 2026-11-04** (steps 2–3, tests in `tests/test_strava.py`). Workflow: `.claude/skills/cycling-reels/reference/strava.md`; plan values: plan-schema.md "Strava stats and route map".

User answers (11-04): full route maps OK, trim the first/last 1 km only for rides in Maine · HR and power OK on screen · link from 2026-10-27 on, no older back-fill · miles/mph.
The Strava connector (`claude.ai Strava`, https://mcp.strava.com/mcp) was added on 11-04. Its tools load only in a session started after that.

## Why

- Most footage is phone video, which has no GPS. Strava gives every clip real numbers: distance, moving time, elevation, speed, power, heart rate, temperature, segment PRs.
- Shot-list story reels need facts we currently can't put on screen ("two hours zone two" → the actual time, the cold → the Garmin's temperature).
- Ride context helps planning: activity title/description, who rode, where the climb was.

## Step 1 — explore the connector (first thing next session, read-only)

1. List the Strava MCP tools and note their names and arguments: activity list, activity detail, streams (time/latlng/velocity/watts/heartrate/temp/altitude), segment efforts, laps.
2. Pull the 11-04 ride and the 10-31 group ride. Check:
   - timestamps: activity `start_date` (UTC) + stream `time` offsets vs each asset's capture time (`./reels show <id>`, ffprobe `creation_time`) — the phone clock should match; GoPro clocks can drift, so cross-check GoPro GPS against the Strava `latlng` stream and record any offset
   - which fields are present (power/HR/temp depend on the sensors)
   - rate limits / page sizes the connector reports
3. Report what's there before writing code.

### Step 1 results (2026-11-04)

- Eligible, full access. Profile: imperial units. Tools: `list_activities` / `search_activities` (summary + weather), `get_activity_performance` (HR/power flags, averages, segment efforts, best efforts, laps), `get_activity_streams` (time, location, heart_rate, watts, cadence, distance, altitude, velocity_smooth, grade_smooth, moving, temp). No rate limits reported.
- Rides found: 10-30 `20790800209`, 10-31 `20806944935` ("ruct halloween ride", start 07:42:51 local), 11-01 `20820570919`, 11-03 `20843729622`, 11-04 `20857636231` (start 06:54:34 local). All have HR, power, temp and weather.
- Sizes: performance for 11-04 is ~112k chars (369 segment efforts, mostly Picnic Loop laps), so it has to be saved to a file. Streams need `resolution` (4000 points ≈ 100k chars).
- Segment efforts have **no PR/rank field**. Only the activity's `pr_count`/`achievement_count`. Segment-PR hooks aren't possible from the data, so they're dropped from step 3.
- **GoPro clock is ~192 s (3:12) fast** vs Strava on 10-31. I matched GoPro GPS to the Strava route: chapters 0110/0111/0112 gave −190 to −195 s, a steady offset. GoPro chapter files (gs02…, gs03…) share chapter 1's `created`, so each one's start = created + earlier chapters' durations. The import must handle that.
- Phone clips: use `com.apple.quicktime.creationdate` (local with tz, matches library `created`), **not** ffprobe `creation_time`. On IMG_3579 that's 15:06Z, ~2 h after filming, probably when the file was exported. There's no GPS to cross-check, but phone clocks are network-synced.
- Linking by time: 11-04 IMG_3579 (07:16) is inside the ride; 3576/3577 are before the start and 3580 is ~2.5 min after the end. 10-31: GoPro 0109–0112 + IMG_3552–3554 are inside; 0108 + IMG_3555/3556 fall outside.
- Rides start/end near the same point (home/campus), so start/end trimming is needed for maps.
- Timezones: DST ended 11-01, so compare local times on both sides, or UTC on both sides. Never mix them.

## Step 2 — store and link (code + tests)

- `strava/activities/<activity-id>.json`: raw summary + streams as the connector returned them (MCP calls are made by Claude; the CLI never talks to Strava directly).
- `./reels strava import <file.json>`: normalizes it (like `./reels stats import`) and links every library asset whose capture time falls inside the activity → `asset.strava = {"activity": id, "offset": s, "clockCheck": "gps"|"phone"|"none"}`.
- `./reels strava list` / `./reels show <id>` print the linked ride: name, date, distance, moving time, elevation gain, avg/max speed, avg power/HR/temp when present, and the stream values at the clip's moments.
- Ingest prints "no Strava ride linked" for ride footage so the batch knows to fetch one.

## Step 3 — use it in reels (code + tests)

Same rule as GoPro GPS: **numbers only come from the data, never typed by hand.**

- Stats pills: `{"value": "strava:distance"}`, `strava:moving-time`, `strava:elevation-gain`, `strava:avg-speed`, `strava:temp`, plus live `strava:speed` / `strava:power` / `strava:hr` for the clip's exact seconds (from streams). The renderer reads the linked activity; `./reels check` prints the value it will show.
- Route map: `"route": "strava"` draws the activity's line for phone clips too, with a dot where the clip was filmed.
- ~~Segment PRs as story hooks~~: dropped, the connector has no per-segment PR data.
- Elevation from Strava is OK (Garmin barometric), unlike GoPro altitude.

## Rules to add (CLAUDE.md + skill, after approval)

- Strava data from the user's own account counts as the user giving the number. It goes on screen only through `strava:` stats, and the activity id is recorded in the plan.
- **Privacy:** route maps and GPS-based text never show where a ride starts or ends (home). Trim the first/last ~1 km of the line, or skip the map for rides from home. Never show other athletes' names/data from Strava; teammate names still follow the transcript rule.
- Don't put heart rate or power on screen unless the user says it's fine (personal data).

## Questions for the user

1. OK to show route maps with the start/end trimmed, or no maps from home rides at all?
2. OK to show heart rate and power numbers?
3. Should past rides (since the library started, 2026-06) be back-filled, or only from now on?
