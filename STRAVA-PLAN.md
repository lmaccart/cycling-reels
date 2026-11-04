# Strava ride data — plan (2026-11-04)

Status: **proposal, waiting for the user's OK.** Code changes need approval and tests (CLAUDE.md).
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

## Step 2 — store and link (code + tests)

- `strava/activities/<activity-id>.json`: raw summary + streams as the connector returned them (MCP calls are made by Claude; the CLI never talks to Strava directly).
- `./reels strava import <file.json>`: normalizes it (like `./reels stats import`) and links every library asset whose capture time falls inside the activity → `asset.strava = {"activity": id, "offset": s, "clockCheck": "gps"|"phone"|"none"}`.
- `./reels strava list` / `./reels show <id>` print the linked ride: name, date, distance, moving time, elevation gain, avg/max speed, avg power/HR/temp when present, and the stream values at the clip's moments.
- Ingest prints "no Strava ride linked" for ride footage so the batch knows to fetch one.

## Step 3 — use it in reels (code + tests)

Same rule as GoPro GPS: **numbers only come from the data, never typed by hand.**

- Stats pills: `{"value": "strava:distance"}`, `strava:moving-time`, `strava:elevation-gain`, `strava:avg-speed`, `strava:temp`, plus live `strava:speed` / `strava:power` / `strava:hr` for the clip's exact seconds (from streams). The renderer reads the linked activity; `./reels check` prints the value it will show.
- Route map: `"route": "strava"` draws the activity's line for phone clips too, with a dot where the clip was filmed.
- Segment PRs / top-10s as story hooks ("third try on this climb and finally"): only when the effort data says so.
- Elevation from Strava is OK (Garmin barometric), unlike GoPro altitude.

## Rules to add (CLAUDE.md + skill, after approval)

- Strava data from the user's own account counts as the user giving the number. It goes on screen only through `strava:` stats, and the activity id is recorded in the plan.
- **Privacy:** route maps and GPS-based text never show where a ride starts or ends (home). Trim the first/last ~1 km of the line, or skip the map for rides from home. Never show other athletes' names/data from Strava; teammate names still follow the transcript rule.
- Don't put heart rate or power on screen unless the user says it's fine (personal data).

## Questions for the user

1. OK to show route maps with the start/end trimmed, or no maps from home rides at all?
2. OK to show heart rate and power numbers?
3. Should past rides (since the library started, 2026-06) be back-filled, or only from now on?
