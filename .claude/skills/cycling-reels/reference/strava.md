# Strava rides

Ride numbers (speed, power, HR, distance, temperature, route) come from the user's Strava through the `claude.ai Strava` connector. The CLI never talks to Strava: Claude calls the connector, saves each response as a file, and runs `./reels strava import`. Rides live in `strava/activities/<id>.json` (gitignored: it holds home GPS).

Scope: rides from 2026-10-27 on (the user's call: "from now on, and last week included"). Don't back-fill older rides unless asked.

## Each batch / after ingest

1. `./reels ingest` links new clips to rides already imported and prints days with no ride yet.
2. For those days: `list_activities` with `range_start`/`range_end` (local time). Only rides (`sport_type` Ride/GravelRide/…) that overlap footage need the next steps.
3. Save the list response to a file in the scratchpad and `./reels strava import <file>`. Small responses come back inline, so write them out exactly as returned (Write tool), don't retype values.
4. Per ride with clips:
   - `get_activity_streams` with `streams: ["time","location","distance","velocity_smooth","watts","heart_rate","cadence","temp"]` and **no** `resolution` (full detail; ~200–550k chars, saved to a tool-results file automatically). Then `./reels strava import <that file> --activity <id>`.
   - `get_activity_performance` (summary, HR/power flags, segment efforts). Import it the same way with `--activity <id>`.
5. Check the link lines it prints: every clip gets `N:NN into the ride` and a clock note.

### Connector gotchas

- **Saved-file names can collide** when several large results finish in the same millisecond: one overwrites the other. Before importing, check each file belongs to the ride. For streams, the last `time` = the ride's `elapsed_time`; for performance, the summed lap `distance` ≈ ride distance. `--activity` is trusted as given. Re-fetch anything missing one call at a time.
- A performance result small enough to come back inline (short rides, few segments) can be saved with only its summary fields (`has_heartrate`, `has_device_watts`, `average_heartrate`, `max_heartrate`, `average_watts`, `average_cadence`, `calories`), copied exactly. That's all the pills use.
- Segment efforts have no PR/rank data, only the ride's `pr_count`. Don't claim a segment PR.

## How clips are placed on the ride

- Capture time = the asset's `created` (iPhone: `com.apple.quicktime.creationdate`, local; not ffprobe `creation_time`, which can be the export time). Compared in local time against Strava `start_local`.
- GoPro chapters (GS02…, GS03…) carry chapter 1's time; the earlier chapters' lengths are added.
- **GoPro clock check:** the clip's own GPS is slid along the ride's GPS (±15 min) to find the clock error (`clock: gps`; the 10-31 MAX was 192 s fast). Chapters without a fix borrow it from the same camera (`gps-same-camera`). Phone clocks are trusted (`phone`); a non-phone camera without GPS is `none`. Treat its live numbers as approximate and prefer ride totals.
- Re-link anytime with `./reels strava link [ids]` (e.g. after fixing a `created` time).

## Privacy

- Full route maps are fine. Rides that start or end in Maine (home) are trimmed 1 km at each end automatically.
- Never show other athletes' names or data from Strava (segment names can include people's names; keep them off screen too). Teammate names still follow the transcript rule.
