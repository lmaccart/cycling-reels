# Tagging assets

Save with `./reels tag <id> --json '<object>'`. The CLI rejects unknown values and lists every problem at once, so fix and re-run. Tagging again replaces the previous tags.

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
| `score`, `focusX`, `focusY` (photo) | 1–5, 0–1, 0–1 | Photo appeal and where its subject sits |

## Moments (video)

A moment is a stretch you'd actually put in a reel.

- `in`, `out` — seconds in the source. 2–8 s is typical (cap 60). Start on movement, not on dead air; end before the camera jolts.
- `label` — what happens ("bridges to the front group", "sprint out of the last corner").
- `score` — 1–5 using the rubric below.
- `focusX` / `focusY` — subject position (0 = left/top, 1 = right/bottom). For landscape footage the 9:16 crop window is centred on `focusX`, so put it on the rider, the road ahead, or the action. Default 0.5.
- `speech` — true if someone talks in it.
- `type` — optional override when one clip contains different scenes (e.g. a long ride with a climb and a descent).

**Score rubric**

- 5 — scroll-stopping: peak effort, speed, drama, a stunning view, a great line spoken to camera
- 4 — strong: clear action, good light, steady
- 3 — usable filler/b-roll
- 2 — weak: only if nothing better exists
- 1 — don't use

**Finding moments in long rides**

A 2-hour GoPro file gets several contact sheets. Skim them for change (corners, other riders, a summit, the sprint, a view), then zoom: `./reels frames <id> --start 3120 --end 3180`. Ten good moments from one ride is a win; a reel uses ~3.

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

## Examples

```json
{"type":"climb","shot":"pov","quality":4,"people":"me","issues":["shaky"],
 "summary":"Out-of-the-saddle POV up a steep wooded ramp, computer visible",
 "moments":[
   {"in":42.0,"out":48.5,"label":"stands up as the grade kicks","score":5,"focusX":0.5},
   {"in":120.0,"out":125.0,"label":"crests the top, view opens up","score":4,"focusX":0.55}
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
