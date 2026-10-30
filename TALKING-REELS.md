# Talking reels: week of Nov 2–8

First try at talking to the camera with a mic. There are two reels: one you film standing still, which plays over footage already in the library, and one mic'd up on a ride this week. You talk. I cut the pauses, caption every word, keep your voice going over the ride footage and put the text on top.

Say every line your own way, and swap any point that isn't true for you. The captions come from what you actually say.

## Reel 1 · things nobody tells you before your first group ride

Stopped, talking to camera: at home with the bike behind you, or roadside after a ride. About 25 s. All the cycling footage is already in the library.

1. **Hook:** "things nobody tells you before your first group ride."
   Your face. Hook text: *things nobody tells you before your first group ride*
2. "one. 'chill pace' doesn't exist. everyone says it, nobody means it."
   "one" on your face, then the group strung out down a tree-lined road. Text: *1. "chill" is a lie*
3. "two. stay on a wheel. it's so much easier in there. lose it and you're on your own."
   On a wheel under the oaks. Text: *2. don't lose the wheel*
4. "three. they'll wait for you at the top." *(hold the look for a second)* "…usually."
   Rolling up to the group, then back to your face for "usually". Text: *3. they wait (usually)*
5. "what would you add?"
   Your face. The post caption asks the same question, so people answer in the comments.

B-roll (unused so far): `261003-gs020098-0bd2` #0 group down a tree-lined road · `261003-gs010098-0cb5` #0 on a wheel under the oaks · `260627-gs010096-7da3` #2 rolling up to the group. Spares: `261003-gs020098-0bd2` #2, `261003-gs010098-0cb5` #5, `261025-img-3503-659c` #0.

## Reel 2 · mic'd up on the group ride · ep 1

On a group ride this week: before, during and after. 20–35 s. "ep 1" makes it a series that keeps the same title every week.

- **Before** (stopped at the meet-up): tap the mic. "mic check. group ride today. the plan is to not get dropped. let's see."
  Hook text: *mic'd up on the group ride · ep 1*
- **During** (phone on a mount facing you): no script. Say what's actually happening, in short bits: "okay we're picking it up", "this is… fine", "who's on the front". The breathing counts too. Get 5–10 bits across the ride, only on quiet stretches. The GoPro keeps filming the ride as usual.
- **After** (stopped, on your face): the honest result in one line, whether you made it, got dropped at ___, or "okay it was actually chill." Then "ep 2 next week."
- I cut between your face and the ride. The text says what happened (*still attached*, *lost the wheel*), and the caption is something like "send this to whoever said 'easy spin'".

No group ride this week? Same shape on a solo hill: "this hill again" at the bottom, mic'd up on the way up, your face at the top.

## Filming the talking bits

- **Mic:** clip it to your jersey zip, a hand's width below your chin. Put the windscreen (the furry cover) on whenever you're outside. **Plug the receiver into your phone** so your voice is recorded in the video file. If the mic can also record to its own memory, turn that on as a backup.
- **Test it every session:** record 5 s and play it back. You should hear the mic (not the phone's built-in mic), with no distortion.
- **Phone:** vertical, same settings as the shot list (4K, HDR Video off). Use the rear camera if you can prop the phone up, the front camera if you need to see yourself.
- **Framing:** chest up, face a little above the middle, some room above your head (the hook text goes there). Face toward the light, never with the sun behind you.
- **Delivery:** start straight on the first line, no "hey guys". Pause for a beat between lines, because that's where I cut. If you fumble a line, say it again and I'll keep the best take. Do the hook 2–3 times with different energy. Use a bit more energy than feels normal.
- **On the bike:** phone on a mount, never in your hand. The mic picks up your friends too, so check they're fine being in it.
- Drop everything in `inbox/` by Sunday.

## The edit (Sunday, with the first November batch)

1. Ingest, then transcribe the talking clips with a bigger Whisper model (`./reels transcribe <id> --model large-v3-turbo`) and fix any misheard words.
2. Pick the best take of each line and cut the pauses, breaths and restarts. Every other cut gets a slight punch-in (`zoom` about 1.1) so the jump cuts look deliberate.
3. Captions on every word, with the hook up from the first frame.
4. Your voice keeps going over the ride footage (cutaways), with the text on top.
5. Music: either a quiet no-copyright bed that ducks while you talk, or ride audio only. Instagram's song matching doesn't work well when you talk over most of a trending song. I'll render both and you pick.
6. Render, check the frames, and re-transcribe the render to catch any clipped words. Then I show you previews to approve. Posting dates get set once we've settled the posting frequency.

## To build before Sunday

- **Cutaways (needed).** The renderer can already jump-cut and caption a talking clip. But each clip plays only its own sound, so your voice stops every time the picture cuts to the ride. The fix is a new plan field, `cutaways`: library footage laid over the talking track at set times, muted, while your voice and captions keep going. It also covers usage tracking (so those clips aren't reused), `./reels check` output, docs and the selftest. I'll test it on an old clip that has speech in it; nothing gets posted.
- **Maybe:** voice cleanup (cutting wind rumble, light denoise). I'll decide after hearing the real takes.
- **Only if your mic can't plug into the phone:** syncing the mic's own recording to the video.
