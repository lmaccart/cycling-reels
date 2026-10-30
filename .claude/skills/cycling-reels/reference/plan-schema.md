# Reel plan files

One JSON file per reel in `plans/`, named `<YYYY-MM-DD>-<slug>.json`. Validate with `./reels check <file>`, render with `./reels render <file>`. Output goes to `out/<slug>/` (reel.mp4, cover.jpg, preview.jpg, caption.txt, post.json — plus preview.mp4 when the song is attached by Instagram). `reel.mp4` is always exactly what gets uploaded.

Every segment must be a clip or photo from `library/` — footage the user recorded and ingested. The renderer checks each file's fingerprint against the original it ingested and refuses anything else; there is no way to add generated, stock or downloaded visuals.

## Top level

| Field | Required | Default | Notes |
| --- | --- | --- | --- |
| `slug` | yes | file name | lowercase letters, digits, dashes; becomes the `out/` folder name |
| `postDate` | no | – | `YYYY-MM-DD` the reel is meant for; `./reels queue` honours it when that slot is free |
| `format` | no | – | Free-text recipe name from [recipes.md](recipes.md), e.g. `"pov-joke"`, `"beat-edit"`; `./reels stats` groups performance by it (shown as "—" when missing) |
| `hook` | no | – | `"text"` or `{"text", "style", "position", "seconds"}`. Upper third by default (`position: "center"` also works). Stays up ~0.28 s per word (2.6–4.5 s) unless `seconds` is set |
| `segments` | yes | – | Played in order (see below) |
| `text` | no | `[]` | Text beats with absolute timing: `{"text","at","for","position","style"}` |
| `music` | no | none | Track slug from `./reels music list`: `"night-drive"`, or an object — see **Music** below. Leave it out (or `null`) for a ride-audio-only reel |
| `musicDrops` | no | `[]` | `[{"at": 6.2, "for": 0.5}]` (reel seconds): the music cuts to silence for that window and comes back hard — see **Sound design** |
| `sfx` | no | `[]` | Sound-effect cues in reel seconds: `{"sound": "whoosh-medium", "at": 3.1, "gain": 0.7}` or `"endAt"` instead of `at` — see **Sound design** |
| `autoSfx` | no | config `sfx.auto` (`true`) | `false` = no automatic sound effects on this reel (only its own `sfx` cues) |
| `endCard` | no | `false` | `true` or `{"cta": "follow for more rides", "handle": "@x", "style": "light", "seconds": 1.6}`. Off by default — reels loop, and natural TikToks rarely have outros |
| `loop` | no | `false` | `true` = a seamless loop: no music fades, no end card, the length snapped to whole bars of the song, and `check` / `render` judge the seam — see **Loops** |
| `punchOnBeats` | no | – | `{"every": 4, "scale": 1.1}`: a punch-in every `every` beats of the music (in on one, back out on the next), on clips without their own `punch` — see **Punch-ins** |
| `cover` | no | end of hook | Seconds into the reel for cover.jpg |
| `post` | for queueing | – | `{"caption": "...", "hashtags": ["#cycling", ...], "altText": "...", "firstComment": "..."}`; a music credit is appended automatically when the track has one |

**Text styles** (`style` on hook, text beats and end card): `light` = black text on a white box (TikTok's classic), `dark` = white text on a black box, `plain` = white text with an outline. Default comes from config `brand.textStyle`. Text appears exactly as written — write it in lowercase if that's how it should look. `*asterisks*` are ignored. One emoji is fine.

## Music

| Field | Default | Notes |
| --- | --- | --- |
| `track` | – | Slug from `./reels music list` |
| `cutOnBeat` | `false` | Snap every cut (a fade's midpoint) and the ending onto the nearest beat; segments change by less than a beat. `./reels check` prints the cut times |
| `start` | the song's loudest stretch / `0` when attached | Seconds or `m:ss` into the song (`./reels music list` shows the default). Attached songs play from 0:00; setting `start` on one means the reel must be posted by hand |
| `volume` | 0.5 (config `audio.musicVolume`) | Mixed-in tracks (ducks under speech, fades out). The user found 0.8 too loud over the ride sounds and effects (2026-10-27). Don't go back above the default unless a trending song comes out as "Original audio" on Instagram |
| `audioVolume`, `videoVolume` | 100 / 20 (25 / 100 with speech captions) | Attached songs only: song and ride-audio levels, 0–100, used by Instagram's mixer (no ducking) and by preview.mp4 |
| `instagram` | config `audio.attachSongs` (`false`) | `true` leaves an `--instagram` track out of the upload and has Metricool attach Instagram's copy (Business account connected through Facebook only); `false` mixes it in |

A track added with `--instagram` (a trending song, see [music.md](music.md)) is mixed in like any other, and Instagram usually recognizes it and credits the song. When it's attached instead (`audio.attachSongs` or `"instagram": true`), `reel.mp4` has ride audio only, `preview.mp4` has the song mixed the way Instagram will, and post.json carries `instagramAudio` for scheduling.

```json
"music": {"track": "pump-it", "cutOnBeat": true}
"music": {"track": "roa-highway", "start": "0:42", "cutOnBeat": true}
```

## Video segments

| Field | Default | Notes |
| --- | --- | --- |
| `asset` | – | Asset id (a unique prefix works, e.g. `"gx010042"`) |
| `moment` | – | Index of a tagged moment — fills `in`, `out`, `focusX`, `focusY` |
| `in`, `out` | from moment | Seconds in the source |
| `speed` | 1 | 0.25–4. 0.5 = slow motion (best from 60 fps footage); 2 = sped up. Clip audio is dropped outside 0.5–2 |
| `frame` | `crop` | `crop` = fill 9:16 around `focusX`; `blurfill` = whole landscape frame over a blurred copy of itself (wide scenery, group shots); `fit` = black bars |
| `focusX`, `focusY` | 0.5 | Crop centre |
| `zoom` | 1 | 1–3, a static zoom on `focus` for the whole segment |
| `punch` | – | Quick zoom-ins for emphasis: `[{"at": 1.2, "scale": 1.15, "for": 0.8}]` or `{"words": ["no", "way"]}` — see **Punch-ins** |
| `captions` | `none` | `transcript` = speech captions from the clip's transcript (and clip audio at full volume) |
| `volume` | auto | 0–1.5 clip audio. Auto: 1.0 when captioned, 0.15 under music, 0.8 with no music |
| `nat` | – | Ride-sound moment: `true` (whole segment) or `{"at": 1.2, "for": 1.5, "volume": 2, "music": 0.15}` (or a list of them). See **Sound design** |
| `clean` | `false` | `true` = gentle highpass (~90 Hz) + light denoise on the clip's audio (wind, rumble). Segments with a `nat` window get it automatically (config `audio.cleanNat`) |
| `payoff` | `false` | `true` marks the payoff shot: the automatic sound effects put a hit at its start (and a riser into it) |
| `sfx` | `[]` | Sound-effect cues with `at` / `endAt` in seconds from the segment start: `[{"sound": "hit-soft", "at": 0.2}]` |
| `transition` | `cut` | `fade` = short crossfade from the previous segment (overlaps by ~0.27 s) |
| `text` | `[]` | Text beats: `{"text": "legs said no", "at": 0.3, "for": 2.2, "position": "lower", "style": "dark"}`; `at` is seconds from the segment start; position `top`, `center` or `lower` (default `lower`, or `top` when the segment has captions). Beats that would collide with the hook or captions are moved automatically (the CLI says so) |
| `stats` | `[]` | Small data pills top-left: `{"label": "grade", "value": "12%", "at": 0.3, "for": 3}`; `for` defaults to the rest of the segment. Real numbers only. GoPro clips can also show their own GPS — see **GPS stats and route map** |
| `route` | `false` | `true` = small map of the recording's GPS track, top-right, with a dot at the current position (GoPro clips with telemetry) |

## Sound design

The mix: the music (ducks under speech captions, fades out), each clip's ride audio at its segment `volume`, and sound effects from `sfx/` (CC0, human-made; `./reels sfx list`). Every clip's audio fades in/out over 30 ms at its edges, so hard cuts don't pop. On top of that:

**Ride-sound moments (`nat`, segment field).** In the window the clip's ride audio rises to `volume` (default config `audio.natVolume`, 1.0; up to 4) and the music ducks to `music` (× its volume; default `audio.natMusicVolume`, 0.2), with ~6-frame ramps. `at`/`for` are seconds from the segment start, like text beats; `for` defaults to the rest of the segment; `"nat": true` = the whole segment. Good for a car or rider passing, freehub buzz while coasting, clipping in at the start. Find candidates with `./reels library --sounds` / `./reels sounds <id>`. Needs a video clip with audio at speed 0.5–2. Freehub ticks are quiet: `./reels check` warns when a window will sit under the music and suggests a `volume`.

**Music drops (`musicDrops`, top level).** `[{"at": 5.8, "for": 0.5}]` in reel seconds (`for` 0.1–3): the music goes silent (2-frame cut) and comes back hard on the next frame after — the beat-before-the-payoff trick. Ride audio is unchanged unless a nat window overlaps. End the drop exactly where the payoff starts. Ignored when the reel has no music in its audio.

**Sound effects (`sfx`).** Top-level cues use reel seconds, segment cues seconds from the segment start. Each cue: `sound` (a slug from `./reels sfx list`; unknown names are refused with the close matches), `at` (when it starts) or `endAt` (when it ends — for risers that should land on a moment), `gain` 0–2 (default config `sfx.gain`, 0.7).

**Automatic cues** (config `sfx.auto`, default on; `"autoSfx": false` turns them off per reel). `sfx.autoRules` maps an event to a sound slug, `{"sound", "gain"}`, or `null` to switch it off:

| Event | When | Default |
| --- | --- | --- |
| `transition` | each `fade` transition, centred on the fade's midpoint | `whoosh-medium` |
| `cameraMove` | each real move (≥30° yaw, 20° pitch or 25° fov) in a 360 `path`, centred on it | `whoosh-long` |
| `textBeat` | each text beat appearing (not the hook, not speech captions) | `pop-soft` at gain 0.45 |
| `payoff` | start of the segment with `"payoff": true` | `hit-big` |
| `payoffRiser` | a riser ending at the payoff, when the previous segment is long enough | `riser-1-5s` at gain 0.5 |
| `punchIn` | each punch-in zoom (segment `punch`, `punchOnBeats`), starting with the zoom | `swish-whip` |

They stay sparse: at most one per `sfx.autoSpacing` seconds (1.5) and `sfx.autoMax` per reel (4; the riser doesn't count), the payoff first, then transitions, then camera moves and punch-ins, then text. None over speech captions or on fades into/out of talking clips. A manual cue within 0.3 s of an auto cue replaces it.

`./reels check` prints the sound sheet: nat windows, drops, cleaned segments, and every cue with its time, sound, gain and source (`auto: <event>`, `plan`, `segment N`).

**Trending songs:** Instagram has to recognize them, so `check` warns when nat windows and drops together take the song down for more than 25% of the reel.

**No music** (no `music` key or `"music": null`): ride audio plays at 0.8 (`nat` windows higher), and the final mix gets one linear gain toward -14 LUFS — at most config `audio.rideOnlyMaxBoost` (12 dB) and at most 6 dB of peak limiting — instead of dynamic loudness normalization, so the bike sounds keep their punch and wind noise isn't pumped up. Quiet ride-only reels land around -18 to -21 LUFS; that's fine.

Config (`config.json`): `audio.natVolume` 1.0, `audio.natMusicVolume` 0.2, `audio.cleanNat` true, `audio.rideOnlyMaxBoost` 12; `sfx.gain` 0.7, `sfx.auto` true, `sfx.autoSpacing` 1.5, `sfx.autoMax` 4, `sfx.autoRules` (table above).

```json
{"asset": "260701-img-2860-818a", "in": 6.4, "out": 10.4, "nat": {"at": 2.6, "for": 1.2, "volume": 3}},
{"asset": "261003-gs010097-d301", "in": 0, "out": 3, "payoff": true, "sfx": [{"sound": "bell-bike", "at": 0.8}]}
"musicDrops": [{"at": 5.83, "for": 0.5}],
"sfx": [{"sound": "swish-whip", "endAt": 4.0}]
```

## Punch-ins

A punch-in is a quick zoom on the footage (4 frames, snappy, no overshoot) for emphasis — what auto-editors do on a punchline or a beat. Only the footage zooms: hook, text, captions, stat pills and the route map stay put. Video segments only (photos have `motion`); works on 360 clips too.

| Form | Notes |
| --- | --- |
| `"punch": [{"at": 1.2, "scale": 1.15, "for": 0.8}]` | `at` = seconds from the segment start (like text beats). `scale` 1.05–1.4 (default 1.15). `for` = how long it holds before easing back out over 6 frames; leave it out to hold until the cut. `"x"`/`"y"` (0–1, default 0.5) aim the zoom at a point (e.g. the rider's face); `"release": "cut"` snaps back instead of easing |
| `"punch": {"words": ["no", "way"]}` | Segments with `captions: "transcript"`: punches on every listed word as it's spoken (case and punctuation ignored), holding for that caption phrase unless `for` is set. Same `scale`/`for`/`x`/`y` options |
| `"punchOnBeats": {"every": 4, "scale": 1.1}` (top level) | Needs the music's beat grid (`"cutOnBeat": true`, or a track whose beats are cached by `./reels music beats`). Every `every`-th beat from the music start, alternating in and out so it never keeps creeping in; skips talking (captioned) segments, 360 camera moves and segments with their own `punch` |

A punch landing on top of another one goes straight from one zoom to the next; the same zoom again just holds longer. Each punch fires the `punchIn` automatic sound effect (`swish-whip`), subject to the usual spacing/max — and like every auto cue, not over speech captions. `./reels check` lists each punch with its time, scale, how long it holds and why (`at 1.2s`, `word 'sweaty'`, `beat 24`).

Sharpness: a punch scales the clip up, so segments with punches are staged at their punch scale (up to config `render.punchStageScale`, 1.4; `1` turns it off) and scaled back down by the template — the punch is then drawn 1:1. That only happens when the source has the detail: 4K iPhone portrait clips do, landscape 4K up to ~1.12×, 1080p and 360 footage don't (they're already upscaled).

```json
{"asset": "260724-img-3021-a7d4", "in": 2.0, "out": 6.6, "punch": [{"at": 1.0, "scale": 1.2, "for": 1.2}, {"at": 3.0, "scale": 1.3, "y": 0.4}]},
{"asset": "260630-img-2846-cdc4", "in": 3.9, "out": 9.7, "captions": "transcript", "punch": {"words": ["sweaty", "pretty"]}}
"punchOnBeats": {"every": 4, "scale": 1.1}
```

## Loops

`"loop": true` makes the reel's end flow into its start, since Reels replay on their own and replays are watch time.

- **Music** neither fades in nor out. With a beat grid (`"cutOnBeat": true`, or beats cached by `./reels music beats`), the last segment is retimed so the reel is a whole number of bars (4 beats) of the song, counted on the grid from the music start — the song restarts on a bar. `check` prints the length it chose (`loop: 8.00s = 4 bars at 120 bpm`) and the cut list ends on it.
- **End card** is off (a warning if the plan asked for one). Nothing else fades at the end.
- **Seam.** The cleanest one is the *swap-halves* edit: start the reel mid-shot and end it with the lead-in to that same shot — the last segment is the same clip with `out` = the first segment's `in`, and the same framing (`frame`/`zoom`/`focus`, or the 360 view the first segment starts on). `check` says `seam: seamless (swap halves…)`; retiming then moves the last segment's `in`, never its `out`, and its ride audio runs straight across the seam (no 30 ms edge fades there). Otherwise `check` stages the last and first frames (footage only) and prints their SSIM next to the shot's own frame-to-frame SSIM, with advice when it jumps; other loops keep the edge fades (a hard audio cut at the seam clicks; the fade is a 30 ms dip).
- `./reels render` measures the seam again on the staged clips and prints it (`loop seam: …`); post.json gets `"loop": {"swapHalves", "seamSsim", "stepSsim"}`.
- A punch held to the end of the last segment would jump back to 1× on the replay — `check` warns; give it a `for`.

```json
{"slug": "2026-11-04-bike-lane-loop", "loop": true, "hook": {"text": "how long have you been watching this"},
 "segments": [
   {"asset": "261003-gs020099-11c4", "in": 209.5, "out": 212.5, "view": "road"},
   {"asset": "260724-img-3021-a7d4", "in": 2.0, "out": 4.0},
   {"asset": "261003-gs020099-11c4", "in": 207.0, "out": 209.5, "view": "road"}],
 "music": {"track": "roa-highway", "cutOnBeat": true}}
```

## GPS stats and route map (GoPro telemetry)

GoPro clips carry the camera's GPS. `./reels telemetry` (run automatically by `ingest`) keeps only good fixes (3D fix, DOP ≤ 5) in `library/telemetry/<id>.json`; `./reels show <id>` prints the summary. Camera GPS counts as something the footage shows, so these numbers may go on screen — the CLI computes them, never type them in yourself.

| Stat `value` | Shows | Default `label` |
| --- | --- | --- |
| `"gps"` | Live speed, updating ~5×/s in whole units, at the real-world speed of the frame on screen (a `speed: 0.5` segment shows the speed the rider was actually doing) | `speed` |
| `"gps:max"` | Top speed over the segment's `in`–`out` | `top speed` |
| `"gps:avg"` | Average speed over the segment's `in`–`out` | `avg` |

Units follow config `units` (`imperial` = mph, `metric` = km/h). `label` is optional; set your own (`{"label": "fast group", "value": "gps"}`). `at`/`for` work as usual.

`"route": true` on a segment draws the GPS track of the whole recording (all GoPro chapters of it, e.g. GS010099 + GS020099) as a thin line in the top-right corner, with a dot moving along it; consecutive segments from the same recording share one map. Lines break where there was no fix. Top text and the hook move below it automatically.

`./reels check` fails if a GPS stat or route covers a moment without a good fix (or a clip without telemetry) — move `in`/`out` or drop the stat. It prints what each GPS pill will show. No elevation pill: GoPro altitude drifts by tens of metres, so it's not shown.

```json
{"asset": "261003-gs020099-11c4", "in": 351, "out": 357, "view": "road", "route": true,
 "stats": [{"value": "gps"}, {"value": "gps:max"}]}
```

## Photo segments

| Field | Default | Notes |
| --- | --- | --- |
| `asset` | – | Photo asset id |
| `duration` | 2.5 | 0.8–12 seconds |
| `motion` | `zoom-in` | `zoom-in`, `zoom-out`, `pan-left`, `pan-right`, `pan-up`, `pan-down`, `none` |
| `fit` | `cover` | `cover` fills the frame around `focusX/focusY`; `blurfill` shows the whole photo over a blurred copy of itself (landscape photos you don't want cropped) |
| `focusX`, `focusY` | from tags | |
| `transition`, `text`, `stats`, `sfx`, `payoff` | | Same as video (photos have no audio, so no `nat`) |

## 360 segments (GoPro .360, 360 MP4s, 360 photos)

360 footage has no fixed frame — each segment points a virtual camera into the sphere instead of cropping. `frame`, `zoom`, `focusX`/`focusY` and `fit` don't apply; everything else (`in`/`out`, `moment`, `speed`, `captions`, `text`, `stats`, `transition`, `volume`) works as usual.

| Field | Default | Notes |
| --- | --- | --- |
| `view` | moment's view → first tagged angle → front | An angle name from the tags (`"rider"`, `"road"`…) or built in `front`/`right`/`back`/`left`, or `{"view": "rider", "yaw", "pitch", "fov", "roll"}` |
| `yaw`, `pitch`, `fov`, `roll` | from `view` | Quick overrides on top of the view |
| `path` | – | Camera move: `[{"at": 0.5, "view": "road"}, {"at": 2.5, "view": "rider", "fov": 95}]`. `at` = seconds from the segment start (after `speed`); holds the first view until the first keyframe; eased, and yaw turns the short way round |
| `projection` | `flat` | `tiny-planet` = the little-planet look. Its yaw decides which direction faces up; a path with yaw keyframes spins it |

On a 360 **photo**, `view`/`projection` reframe it and the usual `motion` (slow zoom/pan) applies; with a `path` the photo becomes a camera move instead.

```json
{"asset": "261026-gs010042-77aa", "moment": 1, "path": [{"at": 0.3, "view": "road"}, {"at": 2.6, "view": "rider"}]}
{"asset": "261026-gs010042-77aa", "in": 140.0, "out": 143.0, "view": "rider", "fov": 95, "speed": 0.5}
{"asset": "261026-gsaa0457-1b2c", "duration": 2.4, "projection": "tiny-planet", "path": [{"at": 0, "yaw": 0}, {"at": 2.4, "yaw": 120}]}
```

Preview any framing before rendering: `./reels frames <id> --view rider --at 140,141.5` or `--planet`.

## Timing tips

- Total length 7–30 s performs best; the CLI warns under 5 s and over 90 s.
- The first segment should already be moving while the hook is up.
- Text beats need ~0.3 s per word to read; 2–3 s each.
- With `cutOnBeat`, plan lengths in beats (`./reels music beats <track>`): 2–4 beats per montage shot, 4–8 for story shots.

## Example

```json
{
  "slug": "2026-10-27-hill-repeats",
  "postDate": "2026-10-27",
  "hook": {"text": "this hill had no business being this steep"},
  "segments": [
    {"asset": "261025-gx010057-1f2e", "moment": 0, "stats": [{"label": "grade", "value": "12%"}]},
    {"asset": "261025-gx010057-1f2e", "moment": 1, "transition": "fade",
     "text": [{"text": "rep 4 of 6 (lie)", "at": 0.3}]},
    {"asset": "261025-img-5521-0aa4", "in": 3.0, "out": 7.5, "captions": "transcript"},
    {"asset": "261025-img-5530-c9d1", "duration": 2.4, "motion": "zoom-out", "transition": "fade",
     "text": [{"text": "worth it tho", "at": 0.3, "style": "dark", "position": "center"}]}
  ],
  "music": {"track": "sky-high", "cutOnBeat": true},
  "post": {
    "caption": "saturday hill repeats. number four was the honest one. what's your local wall?",
    "hashtags": ["#cycling", "#roadcycling", "#hillrepeats"],
    "altText": "POV of a cyclist climbing a steep wooded road, then talking to camera at the top."
  }
}
```

The 12% is only allowed because the user said it or the bike computer in frame shows it.
