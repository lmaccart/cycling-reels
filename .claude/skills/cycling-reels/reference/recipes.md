# Reel recipes

What gets reach on Instagram is watch time (people finish and rewatch), likes per view, and above all **sends** — someone DMing the reel to a friend. Cycling accounts that grow don't just post "me riding, nicely edited": what works is a person you can see, a little story with a payoff, something useful, or a joke a cyclist would forward to their riding group. Every recipe below is a shape, not a script — adapt it to what the footage actually has. Every clip and photo comes from the user's library; if a recipe needs a shot that doesn't exist, pick another one. [SHOTLIST.md](../../../../SHOTLIST.md) is what the user films each ride so these recipes have material.

## Every reel

1. **The first frame already works.** Open on the best visual, the moment of tension, or the face — never on a slow roll-out. The hook text is up from frame 0.
2. **A person in it.** A face, a reaction, the rider seen from outside (pass-by, 360 chase view, a friend filming), or a voice. Pure POV of the road is b-roll, not a reel.
3. **Show the payoff, don't just type it.** If the hook says the group ride wasn't chill, show the suffering: the grimace selfie, the computer at a silly number, the gap opening.
4. **Cut with the music.** Music-driven reels use `"cutOnBeat": true`. Montage shots 1–2 s (2–4 beats), story shots 2–4 s. Fewer long fades; hard cuts on the beat read as edited, fades read as a slideshow.
5. **Readable with the sound off.** Many people watch muted — the hook and beats alone should tell it.
6. **End on something:** a loop back into the first shot (same direction of travel, same framing — `"loop": true`, see [Loops](#loops)), a punchline, or a question worth answering.

## Formats

| Recipe | Length | Shape | Needs | Example hook |
| --- | --- | --- | --- | --- |
| **This week's trend** | 7–20 s | A trend from [music.md](music.md)'s lists, done with ride footage: the song, its format, our footage. Fit it to cycling (Later's notes help) | Whatever that trend's format needs | (the trend's own text format) |
| **Ride vlog timestamps** | 12–25 s | 5–8 shots, each with a time label: alarm, coffee, roll-out, the climb, café, home. Time labels come from each clip's real recording time (`created` in `./reels show`, local wall-clock) | Kit-up, roll-out, mid-ride, café, end | "6:12 am · legs not awake yet" |
| **Story with stakes** (make it a series) | 15–35 s | Setup → the effort → honest result. Same title each episode so people follow along | The before, the face during, the result | "can i hold the fast group's wheel · ep 2" |
| **Tips from your own riding** | 15–30 s | 3 tips, each over the shot that shows it; talking clip with captions or text beats | Talking clip or illustrating shots | "3 things that made group rides less scary" |
| **Relatable POV, with a visible punchline** | 6–12 s | Setup (POV) → cut to the proof (face, computer, the gap, the café) | POV + a reaction/proof shot | "pov: the group ride was supposed to be chill" |
| **Beat edit** | 8–15 s | 6–10 shots of 2–4 beats on a trending song: pass-bys, low angles, details, slow-mo, the view. One text beat at most | Pass-bys, details, 60 fps for slow-mo | "sunday loop, no notes" |
| **The climb** | 12–20 s | Approach → steepest bit (slow-mo the hardest 2 s) → the face at the top → view | Climb POV, face at the top, view | "this hill had no business being this steep" |
| **Descent / speed** | 7–12 s | Fastest stretch, end on the corner exit; a speed pill from a computer in frame or the GoPro's own GPS (`"value": "gps"`) | Descent POV, computer shot | "didn't touch the brakes once" |
| **Talking clip** | 15–40 s | One clip with `captions: "transcript"` + 1–2 cutaways; hook = the point | One sentence to camera | "the one thing that fixed my climbing" |
| **Before / after** | 8–14 s | A contrast pair: km 1 vs km 90, 6am vs 9am, start vs summit, summer vs now | Matching shots at both ends | "km 1 vs km 90" |
| **Question / debate** | 7–12 s | Strong footage + a question cyclists will argue about in the comments | Any good footage | "would you ride this road at 6am?" |
| **Ride montage** | 10–18 s | 5–7 short moments and photos, music-driven, cut on the beat | Variety from one ride | "come on a 6am ride with me" |
| **Bike check / gear** | 7–12 s | 3–5 static details, alternating zoom/pan; labels as beats | Bike details | "rate the bike" |
| **Scenery breather** | 6–10 s | 1–2 wide shots (`blurfill` for landscape), minimal text | A held view | "this is why i get up at 5am" |
| **Just the bike** | 7–10 s | No music: 2–4 shots with the best ride sounds, each a `nat` window, hard cuts — see below | Tagged sounds (`./reels library --sounds`) | "no music. just the freehub" |

**Speed ramps** without a new feature: split one moment into two back-to-back segments with a hard cut on the beat, normal speed into it and `speed: 0.5` after (slow-mo needs 60 fps footage — the GoPro MAX 360 files are 60 fps; current iPhone clips are 30 fps).

**Finding fast stretches with GPS:** GoPro clips know how fast you were going. `./reels telemetry <id> --fast 5` lists the fastest gap-free 5 s stretches, `--start A --end B` gives min/avg/max for a moment you like, `--at 120.5` the speed at one point. Put a live `{"value": "gps"}` pill on the fast bit (slow-mo still shows the real speed) and `"route": true` for a little map of the ride. Only real fixes are used: if `./reels check` says there's no fix there, pick another stretch — never type the number in instead. Houston rides are flat and GPS altitude is noisy, so speed is the number to show, not elevation.

**Flash-forward opener:** start with 1–2 s of the payoff (the summit view, the sprint), then cut to the start ("3 hours earlier"). Reusing a moment inside one reel is fine.

## Loops

Reels replay automatically, and a replay nobody notices is free watch time. `"loop": true` ([plan-schema.md](plan-schema.md#loops)) takes the fades and end card off and fits the length to whole bars of the song; the footage has to do the rest.

- **What loops well:** straight POV roads (one stretch of tarmac looks like the next), 360 spins (a full 360° yaw path ends where it started), pass-bys (car or rider in, out, road empty again), and beat edits where the first and last shots match in direction of travel and framing.
- **The swap-halves trick:** pick one 4–8 s shot, start the reel in its middle and end the reel with its first half — last segment = same clip, `out` = segment 1's `in`, same view or crop. The seam is then literally the next frame; `check` says "seamless". Anything else in between is fine.
- **Keep it short:** ≈6–12 s. The shorter the reel, the more of it is the seam and the more replays it collects.
- **The hook still has to land in the first second** — the first frame is also the one replays come back to, so open on motion, not a slow roll-out, and keep the hook short enough to reread every lap.
- Check the seam line in `./reels check` / `render`: an SSIM well under the shot's own frame step means a visible jump — move the last segment's `out` or switch to swap halves.

```json
"loop": true,
"segments": [{"asset": "<road clip>", "in": 209.5, "out": 212.5, "view": "road"}, {"asset": "<other shot>", "in": 2, "out": 4}, {"asset": "<road clip>", "in": 207.0, "out": 209.5, "view": "road"}],
"music": {"track": "<song>", "cutOnBeat": true}
```

**Punch-ins** on the beat or on the punchline word add energy to beat edits and talking clips: `"punchOnBeats": {"every": 4, "scale": 1.1}` or `"punch": {"words": ["no", "way"]}`. One idea per reel — punches on every beat *and* every word is a lot.

## Sound design

Sound is half of a reel that people rewatch. All of it is in [plan-schema.md](plan-schema.md#sound-design); the taste:

- **Ride-sound moments.** One or two `nat` windows per reel where the bike gets the stage: a car or rider passing, freehub buzz while coasting, clipping in at the start, a shift on the climb. Find them with `./reels library --sounds` (tagged sounds, best first) or `./reels sounds <id>` on a clip you're using. Keep each 0.8–2 s, right on the sound; if `check` says it's quiet, raise its `volume` (freehub ticks often need 2–4).
- **One drop at most**, right before the payoff: `musicDrops` ending exactly where the payoff shot starts (0.3–0.6 s). Two drops is a gimmick.
- **2–5 sound effects per reel**, never one on every cut. Whoosh on a fade or a 360 camera swing, a soft pop when text appears, a hit on the payoff (mark the segment `"payoff": true`; with a long enough shot before it, a riser lands on it too). The automatic rules do this sparsely — check the cue sheet in `./reels check` and add, move or replace cues there (a manual cue near an auto one replaces it). Talking reels: no whooshes over the voice (the auto rules already skip them).
- **Trending songs stay on top most of the time.** Instagram has to recognize the song — nat windows and the drop together under a quarter of the reel (`check` warns), the song's hook untouched.
- Wind is the enemy on iPhone clips: `"clean": true` on a windy segment you still want to hear (nat windows get it automatically). If it's still mostly wind, use the shot without its audio.

### Just the bike (no music)

7–10 s, `music` left out, `"autoSfx": false`. 2–4 shots chosen from tagged sounds (`./reels library --sounds --unused`): a freehub coast, a pass-by, a clip-in, gravel crunch. Each gets a `nat` window on its sound (`"nat": true` if the whole shot is the sound) and the cleanup comes with it; hard cuts. Minimal text: the hook and maybe one beat, in the [captions.md](captions.md) voice — "no music. just the freehub", "the only playlist i need", "turn your sound on for this one". The caption can ask what people hear ("best sound in cycling, wrong answers only"). The final mix is lifted gently rather than squashed to full loudness, so it stays natural.

```json
{"slug": "2026-11-05-just-the-freehub", "format": "just-the-bike", "hook": {"text": "no music. just the freehub"}, "autoSfx": false,
 "segments": [
   {"asset": "260701-img-2860-818a", "in": 6.4, "out": 10.2, "nat": {"at": 2.5, "for": 1.3, "volume": 2}},
   {"asset": "260630-img-2846-cdc4", "in": 5.4, "out": 7.6, "nat": true}]}
```

## 360 footage

A 360 clip is several shots in one — use that. Every angle still has to come from the user's footage; nothing here adds visuals.

| Recipe | Shape | Example hook |
| --- | --- | --- |
| **The look back** | start on the road, pan to the rider during the effort: `path` road → rider over ~2 s | "pov: my camera can see behind me" |
| **Chase cam** | the rider from behind and a bit above (camera on a pole: `yaw ±180, pitch -20, fov 90`) — looks like someone filmed you | "who's filming this (it's me)" |
| **Tiny planet** | 1.5–2.5 s opener or closer at a scenic stop or the summit; spin it slowly with a yaw path | "tiny planet, big climb" |
| **Whip pan** | two keyframes 0.3–0.5 s apart = a fast spin from road to rider, a transition in one clip — put it on a beat | "wait for it" |
| **Corner look** | look into a corner (yaw ±60) and swing forward on the exit | "this hairpin though" |
| **Group check** | in a group ride, alternate road ahead and the wheel behind | "pov: you're on the front" |

Keep pans slow (2–3 s for a half turn) unless it's a deliberate whip — fast spins feel dizzy. Don't pan on every clip; one camera move per reel lands better than five. The current under-the-bars mount mostly sees sky, the road and the stem; a pole or seatpost mount (see SHOTLIST.md) gives real third-person shots.

## Music

Songs come from this week's trending lists, mixed into the reel — see [music.md](music.md). Match the energy: driving for speed, racing and beat edits; warm or cinematic for scenery and fall rides; the trend's own song when doing its format. Not the same track two days running. Talking reels: the song ducks under speech automatically (or use a no-copyright track); often ride audio alone is better.

## Picking for a week (7 reels)

1. Start from `./reels library --unused --min-score 3`, then the trending lists.
2. Aim for a mix: 1–2 trend reels with this week's songs, 1 story/series episode, 1 tips or talking reel, 1 relatable POV with a punchline, 1 beat edit or vlog-timestamps reel, 1 before/after or question. The footage decides — don't force a format it can't support.
3. Give the strongest score-5 moment its own reel early in the week.
4. Spread one ride across 2–3 reels with different angles rather than one long recap.
5. Not enough material for 7? Make fewer and say so — and tell the user which SHOTLIST.md shots would have unlocked the formats you couldn't make. A weak filler reel hurts more than a skipped day, and nothing may be padded with clips from anywhere else.
