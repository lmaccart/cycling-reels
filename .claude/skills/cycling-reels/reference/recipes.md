# Reel recipes

What gets reach on Instagram is watch time (people finish and rewatch), likes per view, and above all **sends** — someone DMing the reel to a friend. Cycling accounts that grow don't just post "me riding, nicely edited": what works is a person you can see, a little story with a payoff, something useful, or a joke a cyclist would forward to their riding group. Every recipe below is a shape, not a script — adapt it to what the footage actually has. Every clip and photo comes from the user's library; if a recipe needs a shot that doesn't exist, pick another one. [SHOTLIST.md](../../../../SHOTLIST.md) is what the user films each ride so these recipes have material.

## Start from the voice

Reels around 10 s feel chopped when nobody talks and there's no real story. So plan in this order:

1. **The rider's line or a story comes first.** A spoken line (`./reels library --role voice --speaker me --unused`, `./reels search "<words>"`), or a story with a setup, a turn and a payoff that a stranger could retell in a sentence (`./reels stories`).
2. **Then cut footage under it** with [cutaways](plan-schema.md#cutaways): the voice keeps going, the picture moves, and the face comes back for the punchline.
3. **Only then** the text, the song and the sound effects. Don't pick the best moments and write text over them.

**Weekly mix:** at least 4 of 7 reels voice-led (`"spine": "voice"`), at most 1 pure montage (`ride-montage` or `beat-edit`). `./reels check` on a 7-reel batch warns when it misses either ([spine](plan-schema.md#spine)).

**Length:** voice-led reels may run 15–40 s. The 7–13 s lesson (aud-length-01) doesn't cap them: it came mostly from comparing against montages. It still applies to reels nobody talks in.

## Series

People follow an account to see what happens next, so the week is built around a few running series before any one-off. Each has the same title on screen every episode, the same day, and the same kind of opening shot, so a viewer who has seen one recognizes the next. It is the offseason (November 2026): Leif is building toward spring collegiate racing, which starts mid-February, so no series depends on a race ([character.md](character.md)).

| Series | Title on screen | Day | An episode is | Thread |
| --- | --- | --- | --- | --- |
| **Road to 5 W/kg** | `road to 5 w/kg · ep N` | Friday | The week's key workout: the plan said to camera, the numbers said out loud as they happen, the honest result, the progress bar. 25–45 s, `spine: voice` | `road-to-5-epN` |
| **Getting Trey on a bike** | `what my roommate wakes up to · ep N` | Wednesday | Opens the same way every time: Pokey's freehub coming down the hallway (a `nat` window; it's the series' theme tune). Then that morning's wake-up, the ask ("you riding today?") and Trey's real answer. The arc ends the day he says yes; the finale is his first ride. 8–20 s | `roommate-epN` |
| **Before class** | `before class · ep N` | Monday | What got done before the first class of the day, with real clip times, ending back at school. 10–25 s | `before-class-epN` |

- **An episode needs its footage.** If the week has no roommate reaction or no workout to camera, skip that episode and say so on the filming card; never stage or pad one.
- **Trey's answers are never scripted or staged.** If he says no forty times, that's the series; if he says yes in week two, the series becomes his first rides.
- **End on what's next only when it's true**: the next workout Leif named, or the question the episode left open. Not "follow for part 2".
- **Count them like any other reel**: a series episode told in Leif's voice is one of the week's four voice-led reels.
- **Tag as you go.** Moments that belong to an episode get its `thread` and a `beat` ([taxonomy.md](taxonomy.md#shelves)), so `./reels stories` shows what each next episode still needs.
- **Evidence so far is thin.** Each series has one posted reel at most (the 2026-11-04 roommate reel, which counts as ep 1, has the best 3 s hold, 75.8%; ep 1 of Road to 5 posts 2026-11-13). Compare series by `thread` in `out/PERFORMANCE.md` once each has three episodes, and retire a series that keeps losing viewers; adding, renaming or retiring one needs Leif's OK.

## Every reel

1. **The first frame already works.** Open on the best visual, the moment of tension, or the face — never on a slow roll-out. The hook text is up from frame 0.
2. **A person in it.** A face, a reaction, the rider seen from outside (pass-by, 360 chase view, a friend filming), or a voice. Pure POV of the road is b-roll, not a reel.
3. **Show the payoff, don't just type it.** If the hook says the group ride wasn't chill, show the suffering: the grimace selfie, the computer at a silly number, the gap opening.
4. **Cut with the music.** Music-driven reels use `"cutOnBeat": true`. Montage shots 1–2 s (2–4 beats), story shots 2–4 s. Fewer long fades; hard cuts on the beat read as edited, fades read as a slideshow.
5. **Readable with the sound off.** Many people watch muted — the hook and beats alone should tell it.
6. **End on something:** a loop back into the first shot (same direction of travel, same framing — `"loop": true`, see [Loops](#loops)), a punchline, or a question worth answering.
7. **One reason to respond.** A question only cyclists can answer, a take to argue with, a send prompt, a pick, or the next episode — one per reel, rotated through the week ([captions.md](captions.md#give-people-a-reason-to-answer)). Comments and sends are where the account is weakest.
8. **Keep it moving.** Something should change on screen at least every few seconds — a cut, a [cutaway](plan-schema.md#cutaways), a punch-in or a text beat. `./reels check` flags 5 s+ stretches where nothing does, photo openings and slideshow fades.

## Formats

| Recipe | Length | Shape | Needs | Example hook |
| --- | --- | --- | --- | --- |
| **This week's trend** | 7–20 s | A trend from [music.md](music.md)'s lists, done with ride footage: the song, its format, our footage. Fit it to cycling (Later's notes help) | Whatever that trend's format needs | (the trend's own text format) |
| **Ride vlog timestamps** | 12–25 s | 5–8 shots, each with a time label: alarm, coffee, roll-out, the climb, café, home. Time labels come from each clip's real recording time (`created` in `./reels show`, local wall-clock) | Kit-up, roll-out, mid-ride, café, end | "6:12 am · legs not awake yet" |
| **Story with stakes** (make it a series) | 15–35 s | Setup → the effort → honest result. Same title each episode so people follow along | The before, the face during, the result | "can i hold the fast group's wheel · ep 2" |
| **Tips from your own riding** | 15–30 s | 3 tips, each over the shot that shows it: one talking clip with captions and a `cutaway` per tip (the voice keeps going), or text beats over ride shots | Talking clip or illustrating shots | "3 things that made group rides less scary" |
| **Relatable POV, with a visible punchline** | 6–12 s | Setup (POV) → cut to the proof (face, computer, the gap, the café) | POV + a reaction/proof shot | "pov: the group ride was supposed to be chill" |
| **Beat edit** | 8–15 s | 6–10 shots of 2–4 beats on a trending song: pass-bys, low angles, details, slow-mo, the view. One text beat at most | Pass-bys, details, 60 fps for slow-mo | "sunday loop, no notes" |
| **The climb** | 12–20 s | Approach → steepest bit (slow-mo the hardest 2 s) → the face at the top → view | Climb POV, face at the top, view | "this hill had no business being this steep" |
| **Descent / speed** | 7–12 s | Fastest stretch, end on the corner exit; a speed pill from a computer in frame or the GoPro's own GPS (`"value": "gps"`) | Descent POV, computer shot | "didn't touch the brakes once" |
| **Talking clip** | 15–40 s | One clip with `captions: "transcript"` + a `cutaway` every 3–5 s to what's being talked about (the voice and captions carry on — [Cutaways](plan-schema.md#cutaways)); come back to the face for the punchline; hook = the point | One sentence to camera + B-roll of it | "the one thing that fixed my climbing" |
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

## Reach beyond the followers

- **Collab with the club.** A group-ride reel the club would happily share can go out as an Instagram Collab (`post.collaborators`, the club's or a teammate's handle — the user gives it and they accept in the app): it shows on both profiles and to both sets of followers. Ask the user once which accounts are open to it.
- **Tag who's in it** (`post.peopleTags`) when they're clearly in the shot and fine with it — tagged riders get notified and often share it to their story.
- **Trial Reels** (shown to non-followers first) suit hook tests, but the account has to be eligible, which it isn't yet, and a trial can't have collaborators ([publishing.md](publishing.md#trial-reels)).
- **Pinned reels** are what a new visitor sees first: keep three pinned that say who this is ([publishing.md](publishing.md#pinned-reels)).

## Music

Songs come from this week's trending lists, mixed into the reel — see [music.md](music.md). Match the energy: driving for speed, racing and beat edits; warm or cinematic for scenery and fall rides; the trend's own song when doing its format. Not the same track two days running. Talking reels: the song ducks under speech automatically (or use a no-copyright track); often ride audio alone is better.

## Picking for a week (7 reels)

1. Place the [series](#series) episodes on their days first. Then start from the lines and the stories ([Start from the voice](#start-from-the-voice)), then `./reels library --unused --min-score 3` for the footage under them, then the trending lists.
2. At least 4 of the 7 are voice-led (talking clip, tips, a story or a POV joke told by the rider) and at most 1 is a pure montage (ride montage or beat edit). Fill the rest from: 1–2 trend reels with this week's songs, 1 sound reel, 1 before/after or question. The footage decides — don't force a format it can't support, and if there aren't 4 usable lines, make fewer reels and say which lines to film ([SHOTLIST.md](../../../../SHOTLIST.md)).
3. Give the strongest score-5 moment its own reel early in the week.
4. Spread one ride across 2–3 reels with different angles rather than one long recap.
5. Not enough material for 7? Make fewer and say so — and tell the user which SHOTLIST.md shots would have unlocked the formats you couldn't make. A weak filler reel hurts more than a skipped day, and nothing may be padded with clips from anywhere else.
