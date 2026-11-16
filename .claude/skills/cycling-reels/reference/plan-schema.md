# Reel plan files

One JSON file per reel in `plans/`, named `<YYYY-MM-DD>-<slug>.json`. Validate with `./reels check <file>` (several at once: `./reels check plans/2026-11-*.json` checks them all and lists every problem), render with `./reels render <file>`. Output goes to `out/<slug>/` (reel.mp4, cover.jpg, preview.jpg, caption.txt, post.json — plus preview.mp4 when the song is attached by Instagram). `reel.mp4` is always exactly what gets uploaded. `./reels project <file>` writes the same reel as an editable Remotion project for Kino or Remotion Studio ([kino.md](kino.md)).

Every segment must be a clip or photo from `library/` — footage the user recorded and ingested. The renderer checks each file's fingerprint against the original it ingested and refuses anything else; there is no way to add generated, stock or downloaded visuals. The one thing that isn't footage is an **exhibit**: an image from the user's `exhibits/` folder or a chart of their numbers, laid over a segment ([Exhibits](#exhibits)).

## Top level

| Field | Required | Default | Notes |
| --- | --- | --- | --- |
| `slug` | yes | file name | lowercase letters, digits, dashes; becomes the `out/` folder name |
| `postDate` | no | – | `YYYY-MM-DD` the reel is meant for; `./reels queue` honours it when that slot is free |
| `spine` | no (`check` warns) | – | What carries the reel: `voice`, `story`, `sound` or `trend`. See **Spine** below |
| `format` | no | – | Free-text recipe name from [recipes.md](recipes.md), e.g. `"pov-joke"`, `"beat-edit"`; `./reels stats` groups performance by it (shown as "—" when missing) |
| `hook` | no | – | `"text"` or `{"text", "style", "position", "seconds", "size"}`. Upper third by default (`position: "center"` also works). Stays up ~0.28 s per word (2.6–4.5 s) unless `seconds` is set. `"size": "big"` = a statement hook for trend formats ("THIS\nIS\nMY\nART"): huge off-white text with a soft glow (no box or outline; `style` doesn't apply), one line per `\n`, and it stays on top of exhibits instead of moving out of their way |
| `segments` | yes | – | Played in order (see below) |
| `text` | no | `[]` | Text beats with absolute timing: `{"text","at","for","position","style"}` |
| `music` | no | none | Track slug from `./reels music list`: `"night-drive"`, or an object — see **Music** below. Leave it out (or `null`) for a ride-audio-only reel |
| `musicDrops` | no | `[]` | `[{"at": 6.2, "for": 0.5}]` (reel seconds): the music cuts to silence for that window and comes back hard — see **Sound design** |
| `sfx` | no | `[]` | Sound-effect cues in reel seconds: `{"sound": "whoosh-medium", "at": 3.1, "gain": 0.7}` or `"endAt"` instead of `at` — see **Sound design** |
| `autoSfx` | no | config `sfx.auto` (`false`) | `true` = automatic sound effects on this reel (only when the user asked for sound effects); `false` = none, only its own `sfx` cues |
| `endCard` | no | `false` | `true` or `{"cta": "follow for more rides", "handle": "@x", "style": "light", "seconds": 1.6}`. Off by default — reels loop, and natural TikToks rarely have outros |
| `loop` | no | `false` | `true` = a seamless loop: no music fades, no end card, the length snapped to whole bars of the song, and `check` / `render` judge the seam — see **Loops** |
| `punchOnBeats` | no | – | `{"every": 4, "scale": 1.1}`: a punch-in every `every` beats of the music (in on one, back out on the next), on clips without their own `punch` — see **Punch-ins** |
| `cover` | no | end of hook | Seconds into the reel for cover.jpg |
| `grade` | no | config `render.grade` | Colour grade for the whole reel: `{"look": "house", "strength": 1.0, "rescue": "auto", "warmth": 0, "exposure": 0}` or `"none"` (colour fix only). Segments and cutaways take the same `grade` to override it — see **Colour grade** |
| `post` | for queueing | – | `{"caption": "...", "hashtags": ["#cycling", ...], "altText": "...", "collaborators": ["@club"], "peopleTags": ["@friend"]}`; a music credit is appended automatically when the track has one. `collaborators` (≤ 5) = an Instagram Collab: the reel shows on their profile and to their followers too, once each accepts the invite in the app; `peopleTags` = people tagged on the reel (they're notified). Only handles the user gave you and only people who are in it or agreed — never guess a handle |

**Text styles** (`style` on hook, text beats and end card): `light` = black text on a white box (TikTok's classic), `dark` = white text on a black box, `plain` = white text with an outline. Default comes from config `brand.textStyle`. Text appears exactly as written — write it in lowercase if that's how it should look. `*asterisks*` are ignored. One emoji is fine.

## Spine

Say what the reel is carried by. Decide this first: voice and story reels start from the line or the story, and the footage is cut under it ([recipes.md](recipes.md#start-from-the-voice)).

| Value | Means |
| --- | --- |
| `voice` | Carried by a spoken line from the rider |
| `story` | A setup, a turn and a payoff a stranger could retell in a sentence |
| `sound` | A real sound event, like the train horn |
| `trend` | A trend format |

Any other value is an error. `./reels check` warns (old plans still render) when:

- `spine` is missing;
- `spine` is `voice` but the rider's voice share is under 30%. It's the same `voiceShare` as `lineage.json` ([analytics.md](analytics.md)): unmuted time on segments tagged `speaker: me` and either `role: voice` or `captions: "transcript"`, counted through their cutaways, over the reel's length. Teammates, unknown speakers, missing audio and audio disabled by speed or volume don't count;
- a batch of exactly 7 plans passed to one `./reels check` has fewer than 4 `spine: voice` reels, or more than 1 reel that has no spine or is a pure montage (`format` `ride-montage` or `beat-edit`). The check only runs on 7, so pass the week's seven without a bonus reel.

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
| `moment` | – | Stable moment id from `./reels show`, e.g. `"m-1a2b3c"` — fills `in`, `out`, `focusX`, `focusY`. Legacy numeric indexes still work, but use ids for new plans so reordering tags cannot change the shot |
| `in`, `out` | from moment | Seconds in the source |
| `speed` | 1 | 0.25–4. 0.5 = slow motion (best from 60 fps footage); 2 = sped up. Clip audio is dropped outside 0.5–2 |
| `frame` | `crop` | `crop` = fill 9:16 around `focusX`; `blurfill` = whole landscape frame over a blurred copy of itself (wide scenery, group shots); `fit` = black bars |
| `focusX`, `focusY` | 0.5 | Crop centre |
| `zoom` | 1 | 1–3, a static zoom on `focus` for the whole segment |
| `punch` | – | Quick zoom-ins for emphasis: `[{"at": 1.2, "scale": 1.15, "for": 0.8}]` or `{"words": ["no", "way"]}` — see **Punch-ins** |
| `captions` | `none` | `transcript` = speech captions from the clip's transcript (and clip audio at full volume) |
| `volume` | auto | 0–1.5 clip audio. Auto: 1.0 when captioned, 0.15 under music, 0.8 with no music |
| `nat` | – | Ride-sound moment: `true` (whole segment) or `{"at": 1.2, "for": 1.5, "volume": 2, "music": 0.15}` (or a list of them). See **Sound design** |
| `blur` | – | Blur patches over the footage: hide an object until its reveal — see **Blur patches** |
| `bleep` | – | Censor swearing in the clip's own audio: `true` (every swear word in the transcript), `{"words": ["fucking"]}`, or `[{"at": 1.4, "for": 0.5}]` (seconds from the segment start). Optional `"sound": "mute"` instead of the default 1 kHz tone, `"pad"` seconds. Bleeped words show masked in captions (`f******`) — see **Bleeps** |
| `clean` | `false` | `true` = gentle highpass (~90 Hz) + light denoise on the clip's audio (wind, rumble). Segments with a `nat` window get it automatically (config `audio.cleanNat`) |
| `payoff` | `false` | `true` marks the payoff shot: the automatic sound effects put a hit at its start (and a riser into it) |
| `sfx` | `[]` | Sound-effect cues with `at` / `endAt` in seconds from the segment start: `[{"sound": "hit-soft", "at": 0.2}]` |
| `transition` | `cut` | `fade` = short crossfade from the previous segment (overlaps by ~0.27 s) |
| `text` | `[]` | Text beats: `{"text": "legs said no", "at": 0.3, "for": 2.2, "position": "lower", "style": "dark"}`; `at` is seconds from the segment start; position `top`, `center` or `lower` (default `lower`, or `top` when the segment has captions). Beats that would collide with the hook or captions are moved automatically (the CLI says so) |
| `stats` | `[]` | Small data pills top-left: `{"label": "grade", "value": "12%", "at": 0.3, "for": 3}`; `for` defaults to the rest of the segment. Real numbers only. GoPro clips can also show their own GPS — see **GPS stats and route map**; clips linked to a Strava ride can show its numbers — see **Strava stats and route map** |
| `progress` | `false` | `true` or `{"asOf": "2026-11-07"}` = the road to 5 W/kg bar across the top — see **Progress bar** |
| `route` | `false` | `true` = small map of the recording's GPS track, top-right, with a dot at the current position (GoPro clips with telemetry); `"strava"` = the whole linked Strava ride (any clip, phone too) — see **Strava stats and route map** |
| `cutaways` | `[]` | B-roll over this clip while its own audio (the voice), captions and text keep going: `[{"at": 1.5, "asset": "<id>", "in": 3.0, "out": 5.0}]` — see **Cutaways** |

## Cutaways

A talking clip that cuts to the ride while the voice carries on — the standard way to keep a talking reel moving. A cutaway lays library footage over a video segment for a stretch: the picture changes, but the segment's own audio (speech, ride sounds, nat windows, bleeps), its captions, text beats and punch-ins carry on underneath. The cutaway is silent, staged and framed like any segment, and checked against its original like any segment.

| Field | Default | Notes |
| --- | --- | --- |
| `at` | – | Seconds from the segment start (reel time, like text beats). The cutaway has to end inside the segment |
| `asset` | – | Clip or photo id from the library (a prefix works) |
| `moment` / `in`, `out` | – | Video: as on a segment. Length on screen = (`out` − `in`) / `speed`; at least 0.4 s |
| `speed`, `frame`, `focusX`, `focusY`, `zoom` | as on segments | e.g. `"speed": 0.5` slow-mo B-roll under the voice |
| `view`, `yaw`, `pitch`, `fov`, `roll`, `path`, `projection` | as on segments | 360 clips |
| `duration`, `motion`, `fit` | 2.5 s, `zoom-in`, `cover` | Photos |

Not on a cutaway (they belong to the segment): `captions`, `text`, `stats`, `route`, `nat`, `bleep`, `punch`, `sfx`, `transition`, `payoff`, `volume`, `clean`. One cutaway at a time per segment. Live numbers (`gps…`, live/clip `strava:` values) and route maps can't share a segment with cutaways — they'd describe the clip underneath, not the shot on screen; whole-ride Strava totals are fine.

- `cutOnBeat` moves segment cuts, not cutaways; a cutaway that no longer fits a segment the beat made shorter is trimmed to its end (or dropped, with a warning).
- **Hiding a jump cut:** end a cutaway on the last frames of one talking segment and start the next segment with a cutaway that continues the same B-roll (`in` = where the first one's `out` left off) — the voice jumps, the picture doesn't.
- `./reels check` lists each cutaway under its segment; usage tracking covers cutaways too (`./reels show <id>`, `library --unused`).

```json
{"asset": "261029-img-3536", "in": 0.4, "out": 9.0, "captions": "transcript",
 "cutaways": [
   {"at": 1.8, "asset": "261028-img-3528", "in": 1.0, "out": 3.2},
   {"at": 5.0, "asset": "261029-img-3533", "moment": 0, "speed": 0.5},
   {"at": 7.4, "asset": "261031-img-3418", "duration": 1.2, "motion": "zoom-in"}]}
```

## Cutting a long take

A talking reel is cut from one long recording where each line is said several times. `./reels takes <id>` proposes the cuts:

```
#5     46.48 → 50.21     3.7s  [line A: take 5 of 6]
      you will die stupid and slow if you don't take this every day
#6     53.36 → 57.35     4.0s  [line A: ★ last take]
      you will die stupid and slow if you don't take this every day.
#13    96.70 → 101.83    5.1s  [line C: take 2 of 4]
      most famous neuroscientist says you should take it too and double the dose most recommend and
      · says 'double the dose most recommend' again in #14: to drop it here, out 99.68
      · trails off on 'and': to end before it, out 101.50
```

- **Phrases** end at a pause of 0.5 s or more (`--pause 0.3` splits finer). `in` is 60 ms before the first word and `out` 150 ms after the last, never into the neighbouring phrase.
- **Edges on the audio level:** when the talking is clearly louder than the room, in/out move onto where the sound starts and stops, and a word Whisper stretched over a pause is pulled back (which can split a phrase the transcript ran together). In wind the header says `word times only`. `--no-audio` forces that.
- **`transcript ends '<word>' at …, the sound stops at …`:** the proposed `out` is before the transcript's end of that word, so `./reels check` would call it clipped. Set that word's `e` in `library/transcripts/<id>.json` to the time given (the rule for fixing timings, as with bleeps), or keep the transcript's end as `out`.
- **Takes:** phrases that repeat most of each other within eight phrases are one line; the last is marked, since later takes are usually the keeper. A line the transcript split differently between takes shows up as separate lines, so read the text.
- **Notes** offer an alternative `out`: before words that are said again in a later phrase, or before a trailing "the / and / so / because". `fragment` is a false start of one or two words; `filler` lists um / uh / "you know" with their times.
- `./reels takes <id> --pick 6,13,17` prints those phrases as segments (`captions: "transcript"`) in the order given, for pasting into the plan. Then add cutaways, exhibits and bleeps as usual.

It only reads the transcript and the audio level: it can't tell which take was delivered best. Fix misheard words first, pick by reading, and let the user choose between takes when it matters.

## Exhibits

A study screenshot, diagram or chart over the footage, for the rider to point at: `"exhibits": [{"image": "<file in exhibits/>", "at": 1.2, "for": 4.5, "x": 0.66, "y": 0.34, "width": 0.55}]` on a segment (or at the top level, in reel seconds). `"anchor": "hand"` places it from the pointing fingertip, `"depth": "behind"` puts the rider in front of it, `"chart": {…}` draws one from the user's numbers or Strava ride. Captions, hook and text beats move clear of it. Every field, the three placement modes and the rules for what may be shown: [exhibits.md](exhibits.md).

## Sound design

The mix: the music (ducks under speech captions, fades out), each clip's ride audio at its segment `volume`, and sound effects from `sfx/` (CC0, human-made; `./reels sfx list`). Every clip's audio fades in/out over 30 ms at its edges, so hard cuts don't pop. On top of that:

**Ride-sound moments (`nat`, segment field).** In the window the clip's ride audio rises to `volume` (default config `audio.natVolume`, 1.0; up to 4) and the music ducks to `music` (× its volume; default `audio.natMusicVolume`, 0.2), with ~6-frame ramps. `at`/`for` are seconds from the segment start, like text beats; `for` defaults to the rest of the segment; `"nat": true` = the whole segment. Good for a car or rider passing, freehub buzz while coasting, clipping in at the start. Find candidates with `./reels library --sounds` / `./reels sounds <id>`. Needs a video clip with audio at speed 0.5–2. Freehub ticks are quiet: `./reels check` warns when a window will sit under the music and suggests a `volume`.

**Music drops (`musicDrops`, top level).** `[{"at": 5.8, "for": 0.5}]` in reel seconds (`for` 0.1–3): the music goes silent (2-frame cut) and comes back hard on the next frame after — the beat-before-the-payoff trick. Ride audio is unchanged unless a nat window overlaps. End the drop exactly where the payoff starts. Ignored when the reel has no music in its audio.

**Sound effects (`sfx`).** Top-level cues use reel seconds, segment cues seconds from the segment start. Each cue: `sound` (a slug from `./reels sfx list`; unknown names are refused with the close matches), `at` (when it starts) or `endAt` (when it ends — for risers that should land on a moment), `gain` 0–2 (default config `sfx.gain`, 0.7).

**Automatic cues** (config `sfx.auto`, default off since 2026-11-16; `"autoSfx": true` turns them on for one reel when the user asks for sound effects). `sfx.autoRules` maps an event to a sound slug, `{"sound", "gain"}`, or `null` to switch it off:

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

## Bleeps

The user is fine with swearing being **bleeped** (2026-11-04) rather than cut. On a talking/selfie segment with `captions: "transcript"`, add `"bleep": true`: every swear word in the transcript inside the segment's in/out is silenced (40 ms padding each side) and covered by a 1 kHz censor tone at about speech level, and the caption shows it masked (`f******`). Use `{"words": [...]}` for a specific word, or `[{"at": s, "for": s}]` when the transcript missed it (check the word timings first). `"sound": "mute"` drops the audio without a tone.

`./reels check` lists each segment's bleep windows in the timeline (`bleep(tone): 13.78-14.44s`, source seconds) and warns `swearing not bleeped` for any swear word left audible. Fix the transcript word timings first if a bleep lands late or early.

## Blur patches

Hide one thing in a talking clip until its reveal (the tub before "it's called creatine"). On a video segment:

```json
"blur": [{"keys": [{"at": 0.0, "x": 0.26, "y": 0.52, "w": 0.25, "h": 0.17}, {"at": 0.4, "x": 0.3, "y": 0.6, "w": 0.34, "h": 0.2}], "strength": 28}]
```

| Field | Default | Notes |
| --- | --- | --- |
| `keys` | – | `at` = seconds from the segment start; `x`, `y` = centre and `w`, `h` = size of a soft-edged ellipse, as fractions of the segment's framed 9:16 picture (after `zoom`/`focusX`/`focusY`, before punch-ins). Straight-line moves between keys; shown from the first key to the last. A single key holds to the end of the segment |
| `strength` | 28 | Blur radius in px (4–80) |

- It blurs only the segment's own footage: cutaways, exhibits, captions and text sit on top, and it follows punch-ins. Not allowed on a cutaway.
- End it by ending the keys: for a reveal, put the last key just before the word (`./reels show <id>` for word times).
- A moving object needs a key about every 0.1 s while it moves, and a patch about 1.5× the object, since the soft edge eats the outer fifth. Keep it off the rider's face when the object is held beside it.
- Find the positions from frames (`./reels frames <id> --at …`), then check `./reels look` strips and a few exact frames of the render: the strips can sit a frame or two off during fast moves.

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

## GPS: Strava first, GoPro only when needed

The user's rule (2026-11-07): GPS numbers and route maps come from the linked Strava ride (`strava:speed`, `"route": "strava"`). GoPro telemetry is the fallback for clips with no linked ride, or moments where the Strava streams have a gap. `./reels check` refuses `"gps"` and `"route": true` when the linked ride covers the moment and names the Strava value to use. `gps:max` / `gps:avg` (top/avg speed over just the clip) have no Strava counterpart, so they stay allowed. GoPro tracks are still read at ingest: they correct the GoPro clock against Strava. `library/telemetry/` isn't tracked in git; `./reels telemetry --force` rebuilds it.

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

## Strava stats and route map (the linked ride)

Clips filmed during a ride the user recorded on Strava are linked to it (`./reels strava list`, `./reels show <id>` prints the link and the speed/power/HR at the clip). Strava data from the user's own account counts as the user giving the number (they confirmed, incl. heart rate and power), so it may go on screen — but only through these values, never typed in. Works on phone clips and photos as well as GoPro.

| Stat `value` | Shows | Default `label` |
| --- | --- | --- |
| `"strava:speed"` / `"strava:power"` / `"strava:hr"` / `"strava:cadence"` | Live, ~5×/s, at the real-world moment on screen (power = 3 s average). Video only | `speed` / `power` / `HR` / `cadence` |
| `"strava:mile"` | Distance into the ride where the segment starts (mi; km when metric) | `mile` |
| `"strava:temp"` | Bike computer temperature over the segment (°F) | `temp` |
| `"strava:distance"` / `"strava:moving-time"` / `"strava:elapsed-time"` / `"strava:elevation"` | Whole ride: mi, h:mm, h:mm, ft (Garmin barometric — fine to show) | `distance` / `moving time` / `ride time` / `elevation` |
| `"strava:avg-speed"` / `"strava:max-speed"` / `"strava:avg-power"` / `"strava:avg-hr"` / `"strava:max-hr"` | Whole ride averages / maxima | `avg` / `top speed` / `avg power` / `avg HR` / `max HR` |

`"route": "strava"` draws the whole ride's line top-right with a dot where the clip was filmed; consecutive segments from the same ride share one map. Rides that start or end in Maine (home) lose their first and last 1 km; elsewhere the full route is fine (the user's call). No segment-PR text: Strava only says how many PRs a ride had, not which.

`./reels check` fails when a clip has no linked ride, the ride lacks the streams/performance data for a value, or the moment falls in a gap (auto-pause, before/after the ride). It prints what each `strava:` pill will show.

```json
{"asset": "261104-img-3579", "in": 0, "out": 3, "route": "strava",
 "stats": [{"value": "strava:speed"}, {"value": "strava:hr"}, {"value": "strava:distance"}]}
```

## Progress bar (road to 5 W/kg)

A series bar for the Road to 5 W/kg episodes. The numbers are the FTP and weight the user gives, logged by date in `library/progress.json`:

```
./reels progress set --ftp 335 --weight 78 --date 2026-11-07   # only numbers the user said
./reels progress                                               # list entries
```

`"progress": {"asOf": "2026-11-07"}` on a segment draws a full-width card at the top: the label (`road to 5 w/kg`), the W/kg (FTP ÷ weight, 2 decimals), `goal 5.0`, the watts still needed (`55 W to go`; `"toGo": false` hides it), and a bar that runs from the series' **first** entry (empty) to the goal (full). When there's an earlier entry, the bar and number count up from it. Always set `asOf` to the episode's recording date so a later re-render keeps that episode's numbers (`./reels check` warns without it). `at` / `for` work as for stats; `label` overrides the text.

It takes the top of the frame, so it can't share the screen with stat pills or a route map (`./reels check` fails); top text and a top hook move below it automatically. Put it on the segment where the numbers are said, or on the hook segment. Don't show the user's weight on its own; W/kg and watts are the numbers for the series.

## Title card (series board)

A generated visual: a drawn wooden board with the series title in old-timey wood type, the episode and a two-to-three-word subtitle. It is fully up in frame 0, a nail flies into its top-left corner (~0.62 s, on the second hit of the showdown sting), it swings and hangs crooked on the nail, and fades out over the first shot (no dimming or blur under it).

```json
"titleCard": {"title": "The Down Week", "episode": 1, "subtitle": "rusty legs", "for": 3.0}
```

- Renders only for titles in config.json `titleCards.allowed`. Add a series there only after the user says yes to a board for it in the conversation (generated-visual rule); The Down Week was OK'd 2026-11-13.
- `for`: 1.5–6 s on screen (default 3). `subtitle`: the day's theme, two or three words, lowercase.
- Sounds come with it as cues on the board's frames: `theme` (default `sting-showdown`, picked by the user 2026-11-13, at 0 s), `nail` (`hit-nail`, on the hit) and `creak` (`creak-wood`, as it swings). Swap any for another `./reels sfx list` slug (`"theme": "sting-slide-down"`) or switch it off with `false`.
- A top `hook` would sit on the board, so it starts when the board is gone (check warns). Usually leave the hook out and let the board be the opening text.

## Colour grade

Every clip is first converted to BT.709 from what it really is (iPhone copies and shared clips are often Display P3, BT.601 and full range; staged as BT.709 they lost ~15% of their colour). Then each clip is graded while it's staged, so the fast path, the full render and the editor preview all show the same pixels (`scripts/reels/grade.py`):

1. **Per-shot fix (technical only):** removes part of a colour cast (measured on neutral surfaces below the sky; little under blue sky, where blue shadows are real skylight, and little at golden hour), sets black and white points, and nudges mid-tones. Measured once per clip on 8 frames, so nothing flickers. It does **not** try to make shots match each other.
2. **The house look**, the same on every reel so the feed reads as one: a gentle contrast curve, slightly warm highlights / cool shadows and mild vibrance that leaves skin and near-whites (clouds) alone. `moody` is the alternative (matte blacks, muted greens, kit blue kept).
3. **Grey-day rescue:** on overcast shots, local contrast and a pull on bright sky, scaled by how overcast the shot measures (`overcast` in the render log) and eased off on low-resolution sources so compression blocks don't jump out.

```json
"grade": {"look": "house", "strength": 0.8, "rescue": "auto", "warmth": 0.2, "exposure": 0}
```

| Field | Default | Notes |
| --- | --- | --- |
| `look` | `house` | `house`, `moody`, or `none` (colour fix only, no grade) |
| `strength` | 1.0 | 0–1.5: how much of the correction + look is applied |
| `rescue` | `auto` | `auto` (from the overcast score), `off` / `false`, `true` (= auto), or a fixed 0–1 |
| `warmth` | 0 | −1…1: cooler / warmer on top of the automatic white balance |
| `exposure` | 0 | −1…1 stops |

The project default lives in config `render.grade` (`look`, `strength`, `rescue`); a plan's `grade` overrides it and a segment's or cutaway's `grade` overrides the plan's (`"grade": "none"` on one clip leaves just that clip ungraded). The Reels Editor shows these as **Colour** sliders and toggles at reel, clip and project level. The user asked on 2026-11-15 for a mild, consistent look between reels — not saturated, and not uniform colour between shots; they may ask later to change the look itself, which is the `look()` function in grade.py (bump `VERSION`). Exhibits, charts, maps, title cards and text are never graded. The render log prints one `grade` line per clip, and `lineage.json` keeps what was measured.

## Photo segments

| Field | Default | Notes |
| --- | --- | --- |
| `asset` | – | Photo asset id |
| `duration` | 2.5 | 0.8–12 seconds |
| `motion` | `zoom-in` | `zoom-in`, `zoom-out`, `pan-left`, `pan-right`, `pan-up`, `pan-down`, `none` |
| `fit` | `cover` | `cover` fills the frame around `focusX/focusY`; `blurfill` shows the whole photo over a blurred copy of itself (landscape photos you don't want cropped) |
| `focusX`, `focusY` | from tags | |
| `transition`, `text`, `stats`, `sfx`, `payoff`, `grade` | | Same as video (photos have no audio, so no `nat`; the grey-day rescue's local contrast and sky pull are video only) |

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

## Retention warnings

`./reels check` (and `render`) also flag, as warnings — advice, not errors:

- **a photo as the first shot** (a still first frame is the easiest to scroll past), and **a crossfade in the first 2 s**;
- **an opening with no face and nobody talking**: the first shot's moment is tagged `face: false`, its role isn't `voice`, `reaction` or `crew`, and it has no speech captions. It follows the tentative lesson aud-person-01, so it is advice: an exceptional shot can stay. An untagged span, a moment with no `face` tag and a `spine: sound` reel are left alone;
- **mostly-fade edits** (more than half the cuts, at least two) — they read as a slideshow;
- **5 s or more where nothing changes on screen** — no cut, cutaway, punch-in, 360 keyframe, caption page, text beat, hook exit or nat window (fine for a deliberate held view);
- **speech captions cutting into a word**: an `in` inside a word shows a stray half-word caption, an `out` inside one clips it (with the time to move to), and **low-confidence transcript words** (often wind);
- **footage another reel already shows** (rendered, not discarded) — followers notice repeats;
- **a missing or under-voiced `spine`**, and a 7-reel batch that misses the weekly mix — see [Spine](#spine).

A plan that uses footage deleted by `./reels prune` is an error, not a warning, in both `check` and `render`.

## Timing tips

- Total length 7–30 s performs best; the CLI warns under 5 s and over 90 s. Voice-led reels may run 15–40 s ([recipes.md](recipes.md#start-from-the-voice)).
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
