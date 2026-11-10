# Tagging assets

Save with `./reels tag <id> --json '<object>'`. The CLI rejects unknown values and lists every problem at once, so fix and re-run. Tagging again replaces the previous tags, except that moments keep their `id` when their span matches (see [Moment IDs](#moment-ids)).

To change part of a saved record, use merge mode instead: `./reels tag <id> --merge --json '{"moments":[{"id":"m-1a2b3c","mood":"calm"}]}'` (or `--file`). Only what you supply changes; moments are addressed by id and keep every other field; `sounds`, `angles` and `hooks` are untouched unless given (and then replaced whole); a `null` removes an optional field. Status and `taggedAt` don't move. To add moments, use `addMoments`: `./reels tag <id> --merge --json '{"addMoments":[{"in":40,"out":46,"label":"…","score":3,…}]}'`. They go through the same validation as a full tag, get new ids (don't supply one), and are refused if they overlap a moment on the clip or each other; nothing else changes. Merge can't remove moments, so re-tag in full for that. `./reels tag-batch --file batch.json` applies `{"<asset id>": {<merge payload>}, ...}` to many assets and saves nothing unless every one is valid. `./reels show <id>` lists the moment ids.

## Fields

| Field | Values | Notes |
| --- | --- | --- |
| `type` | `race`, `climb`, `descent`, `sprint`, `group-ride`, `solo-ride`, `scenery`, `bike-check`, `gear`, `indoor`, `talking`, `cafe`, `travel`, `other` | The clip's main content |
| `shot` | `pov`, `chase`, `roadside`, `selfie`, `handheld`, `static`, `aerial`, `screen`, `photo` | Photos default to `photo` |
| `quality` | 1–5 | Technical + visual quality of the asset as a whole |
| `issues` | any of `shaky`, `dark`, `overexposed`, `blurry`, `wind-noise`, `obstructed`, `low-res`, `boring` | Optional |
| `people` | `none`, `me`, `others`, `group` | `others` = identifiable people besides the rider |
| `summary` | one line | What's in the shot, concretely ("Seated grind up a steep wooded switchback") |
| `hooks` | up to 6 short strings | Hook ideas this footage suggests, in the natural voice from captions.md ("wait for the descent") |
| `location`, `notes` | text | Only when known from the user or visible on screen |
| `moments` (video) | list | See below |
| `sounds` (video) | list | Optional: clean ride sounds worth hearing — see [Sounds](#sounds-video) |
| `landmarks` (video) | list | Optional: rough signposts along a long clip — see [Landmarks](#landmarks-video) |
| `score`, `focusX`, `focusY` (photo) | 1–5, 0–1, 0–1 | Photo appeal and where its subject sits |

## Moments (video)

A moment is a stretch you'd actually put in a reel.

- `in`, `out` — seconds in the source. 2–8 s is typical (cap 60). Start on movement, not on dead air; end before the camera jolts.
- `label` — what's in the shot, concretely enough to find it later by reading: the subject, the action and anything that tells it apart ("teal bike in the steel elevator, doors opening", "rider in red slides past under a big blue sky"), not a mood or a verdict ("great moment", "nice shot"). Name people and places only when you know them from the user, the screen or a clearly heard transcript; otherwise describe them ("teammate in blue", "the brick arch"). Teammate names only go on screen when clearly heard, so don't add a name on a guess.
- `score` — 1–5 using the rubric below.
- `focusX` / `focusY` — subject position (0 = left/top, 1 = right/bottom). For landscape footage the 9:16 crop window is centred on `focusX`, so put it on the rider, the road ahead, or the action. Default 0.5.
- `speech` — true if someone talks in it.
- `type` — optional override when one clip contains different scenes (e.g. a long ride with a climb and a descent).
- `id` — `m-` + 6 hex, assigned by the tool. Don't invent one.
- `role`, `speaker`, `face`, `mood`, `opener` — standard on every new moment; `line`, `thread`, `beat` when they apply. See [Shelves](#shelves).

**Score rubric**

- 5 — scroll-stopping: peak effort, speed, drama, a stunning view, a great line spoken to camera
- 4 — strong: clear action, good light, steady
- 3 — usable filler/b-roll
- 2 — weak: only if nothing better exists
- 1 — don't use

**Finding moments in long rides**

A 2-hour GoPro file gets several contact sheets. Skim them for change (corners, other riders, a summit, the sprint, a view), then zoom: `./reels frames <id> --start 3120 --end 3180`. Ten good moments from one ride is a win; a reel uses ~3.

### Moment IDs

Later steps reference moments by id, so ids have to last. `./reels tag` gives each new moment an id and keeps the old one when a moment's span matches a saved moment (exactly, or overlapping by 75%+ so a tweaked trim counts as the same moment). To keep an id through a bigger re-trim, include it: `{"id": "m-1a2b3c", "in": …}`. Ids of dropped moments are never reused.

### Shelves

Every moment gets one value per shelf, so a plan can ask for "a suffering face that works as an opener". These definitions exist to keep tags consistent between sessions: judge the moment in front of you against the wording here, not against what past sessions did.

Check the transcript (word timings: `library/transcripts/<id>.json`) and the frames. A label alone isn't evidence for `speaker`, `face` or `opener`.

**`role`: what job this moment does in a reel. Exactly one.**

| Value | Definition | Borderline cases |
| --- | --- | --- |
| `voice` | The rider saying something worth hearing. The words carry the moment. | A plan said to camera is `voice` + `line: plan`, not `setup`. A teammate's line is `crew`, never `voice`. Mumbled filler over a road shot is `texture`. |
| `reaction` | The rider's face does the work: suffering, laughing, deadpan. | A grin with "let's go" is `reaction`: the words add nothing. If you'd caption the line, it's `voice`. |
| `setup` | Getting ready, before the story starts: alarm, kit-up, elevator, rollout, an off-bike routine, or the plan when it's shown rather than said. | The bike in the elevator is `setup`; the bike leaning on a wall for its looks is `texture`. |
| `effort` | Visible work: intervals, climbs, sprints, hanging on, fast POV. | On a wheel at speed is `effort` when the work is the point, `crew` when the people are. Easy cruising isn't effort. |
| `proof` | The payoff you can check: a readable computer screen, the summit, the gap, the result. | A computer that's in frame but unreadable is `texture`, or `setup` if it's the pre-ride screen. Don't use it for a nice view. |
| `crew` | Teammates, banter, overheard lines, the group as the subject. | The roommate or a friend on screen counts. A group far ahead with the road as the subject is `texture`. Rider's voice with the crew as the subject stays `crew`, speaker `me`. |
| `texture` | Roads, light, views, gear details: b-roll for cutaways. | The fallback when nothing else is true. A big view is still `texture`; capture how big it is with `mood: awe`. |

**`speaker`: whose voice is on this moment.**

- `me` needs real evidence: a selfie or to-camera line, a line that fits the rider alone with nobody else around, or the phone holder on a solo clip. Being the likeliest voice in a group isn't enough.
- `teammate` is anyone else's voice, and the answer when group audio can't be told apart.
- `none` is no voice worth counting: wind, music, a one-word mumble, or a transcript that's gibberish. Whisper invents text from wind and keyterms (Zwift, KOM, FTP lists), so a low `p` or a keyterm list means `none`, whatever `speech` says. `speech` stays as it is: other code reads it.

**`face`: bool. The rider's face is visibly on screen for a real part of the moment.**

Sunglasses and a helmet still count. A teammate's face doesn't. A whip that shows the face for a few frames of blur doesn't. A long shot where you can't make out the face doesn't. For 360 use the moment's `view`: under-saddle and bar mounts almost never show it.

**`mood`: the feeling it gives a viewer. The most subjective tag, so expect drift and pick the plainest fit.**

`funny` (playful, joking, grinning, chaos) · `suffering` (visibly or audibly hurting) · `calm` (easy, unhurried, the default for b-roll) · `fast` (speed or urgency is the point) · `awe` (a view or scene that's actually big; most pretty roads are `calm`) · `honest` (level, reflective, unglamorous truth).

**`opener`: bool. This works as the first second of a reel.** Separate from `score`: a score-5 payoff often makes a bad opener because it needs what came before.

True only if the first second is instantly legible with no context and would stop a scroll: a strong line landing immediately, a striking face, or a shot you can read at a glance. Keep it selective. If a clip takes a beat to make sense or starts on a blur, it's false, however good the rest is.

**`line`: only with `role: voice`, and optional.** What kind of line it is: `confession` (admits something unflattering) · `plan` (what's about to happen) · `complaint` (grumbling about the world or the wind) · `joke` · `result` (what happened or the numbers) · `tip` (advice someone could use). Leave it out if none fit.

**`thread` / `beat`: only for genuine stories or series across days, or threads go stale.** `thread` is a kebab-case slug (known series: `road-to-5-ep1`, the failed 2026-11-03 4x8). Tag a moment only if it could appear in that story; don't add a thread to a one-off. `beat` needs a thread: `setup` (the plan, the stakes) · `effort` (the work itself) · `turn` (where it goes right or wrong) · `result` (how it ended, what it meant).

## Sounds (video)

Real ride sounds a reel can bring up for a moment (music dipped) or reuse as a sound effect: freehub buzz while coasting, a clip-in, a shift, a car or rider passing, gravel crunch, a bell, brakes, cheering. Optional — only tag what's actually good.

- `at`, `for` — seconds in the source (start, length; 0.3–4 s is typical). Must sit inside the clip.
- `kind` — `freehub`, `clip-in`, `shift`, `pass-by`, `gravel`, `bell`, `brakes`, `chain`, `tire-hum`, `cheer`, `crowd`, `car`, `other`.
- `score` — 1–5: how satisfying it would be with the music dipped for it. 5 = crisp and instantly recognisable (a loud clean freehub buzz, a bell ding); 3 = clearly there but modest; 1–2 = only if nothing else.
- `note` — required: what's audible and how clean ("freehub ticking as I sit up, light wind", "car whoosh left to right, some talking").

**Only tag clean sounds.** Skip anything buried in wind roar, talked over, or that you're guessing at. You can't listen, so tag from evidence:

1. `./reels sounds <id>` lists candidates (start, length, kind, conf 0–1, wind 0–1, HF clicks per second, how far it rises above the background, and the AudioSet labels behind it) and writes `library/sounds/<id>.jpg`. Read the image: loudness on top, spectrogram 60 Hz–8 kHz (low at bottom), then lanes for HF clicks (white ticks), wind (red) and speech (blue); candidates are boxed `#n kind conf` and ticks use the same m:ss.s times as contact sheets. Long clips get an overview — zoom with `--start A --end B`; `--best` sorts by confidence, `--kind K` filters.
2. What things look like: freehub = a run of thin evenly spaced vertical stripes in the upper half with dense white ticks, usually while loudness drops (coasting); shift / clip-in = one or two sharp stripes; bell = a few bright horizontal lines high up that ring out; brakes = a bright thin horizontal whine; pass-by = broadband swell that rises and falls over 1–3 s; wind = heavy smear at the bottom and a red lane — down-rank.
3. Labels such as `Clip-clop`/`Horse` (rhythmic clicks), `Bicycle`, `Ratchet, pawl` back up a freehub; `Clicking`/`Tick` a shift or clip-in; `Train`/`Rail transport` often means rhythmic rattling or a whine — check the image. Big full-height stripes are usually road bumps, not shifts.
4. Cross-check with the contact sheet / `./reels frames <id> --at …`: is the rider coasting, at a light, unclipping? A detection with conf under ~0.2, wind over ~0.3 or `speech` is usually not usable on its own.
5. Tag it in `sounds` with the timing you see in the image (tighten `at`/`for` to the stripes, not the 1 s detector boxes). `./reels library --sounds [--kind K] [--unused]` lists tagged sounds best first.

```json
"sounds": [
  {"at": 264.4, "for": 2.6, "kind": "freehub", "score": 4, "note": "freehub ticking while coasting after the chat, almost no wind"},
  {"at": 12.1, "for": 0.4, "kind": "clip-in", "score": 3, "note": "sharp clip-in click at the light, a car idling under it"}
]
```

## Landmarks (video)

Rough signposts along a long ride, so stretches nobody tagged as moments can still be found: `[{"at": 1250, "label": "regroup at the traffic light"}]`. `./reels search` covers them.

- `at` — seconds in the source, roughly where it changes. `label` — required, up to 120 characters: what's there, concretely (the climb, a regroup, the café stop, a bridge, a sprint).
- About one every few minutes where something actually changes. They aren't moments: no scores, no shelves, no need to be usable.
- A full `./reels tag` replaces landmarks unless you supply them again. `--merge` keeps them unless `landmarks` is given, and then replaces the list whole (`[]` or `null` clears it).

## Examples

```json
{"type":"climb","shot":"pov","quality":4,"people":"me","issues":["shaky"],
 "summary":"Out-of-the-saddle POV up a steep wooded ramp, computer visible",
 "moments":[
   {"in":42.0,"out":48.5,"label":"stands up as the grade kicks","score":5,"focusX":0.5,
    "role":"effort","speaker":"none","face":false,"mood":"suffering","opener":false},
   {"in":120.0,"out":125.0,"label":"crests the top, view opens up","score":4,"focusX":0.55,
    "role":"proof","speaker":"none","face":false,"mood":"awe","opener":false}
 ],
 "hooks":["the last 500m of this climb","pov: the grade just won't stop"]}
```

```json
{"type":"bike-check","quality":5,"people":"none","summary":"Bike against a brick wall in golden light, drive side","score":5,"focusX":0.5,"focusY":0.55}
```

```json
{"skip":true,"reason":"camera pointed at the stem the whole time"}
```

## 360 footage (GoPro MAX / MAX 2 `.360`, 360 MP4s, 360 photos)

The sheet title says `360°`. Each tile is the whole sphere unwrapped: yaw labels run along the bottom (0 = camera front, +90 right, ±180 behind, -90 left), the middle row is the horizon, the top edge is straight up and the bottom edge is straight down (the bike/mount).

1. Find what's worth pointing at — usually the road ahead and the rider. On a handlebar or seatpost mount the rider is around ±180; on a helmet or chest mount the road is around 0 and the rider is below (pitch -50 to -70).
2. Look closer with `./reels frames <id> --views --at 12,40` (front / right / behind / left), then confirm a framing with `./reels frames <id> --view 180,-15,85 --at 12` (yaw, pitch, fov) or `--planet` for the tiny-planet look.
3. Save named angles in the tags; plans refer to them by name:

```json
"angles": {"road": {"yaw": 0, "pitch": -8}, "rider": {"yaw": 180, "pitch": -15, "fov": 85}}
```

- yaw -180…180; pitch -90…90 (negative looks down); fov 20–150 = horizontal field of view of the 9:16 frame (60 tight, 75 natural, 95 wide — wider bends the edges); roll levels a tilted horizon.
- The first angle is the default view for the clip, so list the most useful one first.
4. Moments work as usual; add `"view": "rider"` (an angle name) when a moment is best seen from one direction. `focusX`/`focusY` don't apply to 360.

```json
{"type":"group-ride","shot":"pov","quality":4,"people":"group",
 "summary":"360 cam on the bars in a paceline, riders behind me",
 "angles":{"road":{"yaw":0,"pitch":-8},"rider":{"yaw":180,"pitch":-15,"fov":85},"group":{"yaw":160,"pitch":-5,"fov":100}},
 "moments":[
   {"in":31.0,"out":36.5,"label":"pulls through, wheel behind","score":4,"view":"rider"},
   {"in":88.0,"out":94.0,"label":"descent into a left-hander","score":5,"view":"road"}
 ],
 "hooks":["pov: my camera can see behind me"]}
```
