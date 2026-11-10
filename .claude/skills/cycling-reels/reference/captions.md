# Writing the text: natural, like a real person on TikTok

Everything on screen should read like the rider typed it into the TikTok text tool on their phone after the ride — not like an ad, a highlight package or a motivational poster. The template renders text exactly as written (no uppercasing, no restyling), so the writing *is* the style.

Who is typing matters as much as how: [character.md](character.md) describes Leif, with lines in their own words. Start from those. The formats below are shapes to fall back on, not the voice.

## Voice

- **Lowercase by default.** Capitals only where a person would bother (names, "I" is optional).
- **Talk, don't announce.** First person, casual, specific to this ride. Fragments are fine.
- **Funny > hype.** Self-deprecating, relatable, a little dry. Cyclists laugh at suffering, coffee stops, gear obsession, Strava, the group ride that was "supposed to be chill".
- **One thought per line.** Short. Minimal punctuation; no period at the end of a single line.
- **No hype words or ad copy:** epic, conquer, crush, beast mode, unleash, journey, elevate, unstoppable, "Ride or Die", "No Days Off", exclamation marks everywhere, ALL CAPS.
- **Emoji: zero or one**, and only the ones people actually use like this (💀 😭 🫠 ☕ 🚴). Never as decoration on every line.
- **Never invent facts.** Numbers, places, results and names come from the user, the footage (bike computer, signs) or a transcript. If you don't know the grade, don't say "12%" — say "this hill".

## Hook (first ~3 s, top of the frame)

4–12 words. It has to make someone stop scrolling: a relatable situation, a little tension, or a joke the footage pays off.

Formats that work:
- `pov: …` — "pov: the group ride was supposed to be chill"
- `me when / me at …` — "me at km 80 pretending i'm fine"
- understatement — "this hill had no business being this steep"
- tension / payoff — "wait for the descent"
- relatable confession — "i said i'd take it easy today"
- the group chat — "the group chat said 'easy spin'"
- question — "would you ride this in the rain?"
- day-in-the-life — "come on a 6am ride with me"
- receipts (real numbers only) — "first time breaking 40 km/h on this road"
- series — "can i hold the fast group's wheel · ep 2" (same title every episode)
- tips from experience — "3 things that made group rides less scary"
- timestamps (real clip times only) — "6:12 am · legs not awake yet"

The hook promises something the footage pays off. If the payoff isn't on screen, pick a different hook.

## Text beats (during the reel)

2–8 words. Reactions or narration that move the story along, timed to what's on screen:
"and then he attacked" · "legs said no" · "worth it tho" · "the coffee stop was the real goal" · "one more interval (lie)" · "okay that was fun" · "same time next week?"

Use 1–3 per reel. Silence is fine; let good footage breathe.

## Speech captions

Come straight from the transcript; just fix misheard words in the transcript JSON. Don't rewrite what the rider said.

## Before → after

| Too produced | Natural |
| --- | --- |
| CONQUERING THE CLIMB! 💪🔥 | this climb humbles me every single time |
| Epic Sunday Ride Through Scenic Hills | sunday loop hits different when the roads are empty |
| No Pain, No Gain! | my legs have filed a complaint |
| Unleashing The Beast On The Descent | wait for the descent |
| Group Ride Highlights 2026 | pov: the group ride was supposed to be chill |
| Speed: 58 KM/H ⚡ | didn't touch the brakes once (real number? put it in a stat pill) |
| Thanks For Watching! Follow For More! | same time next week? |

## Instagram caption (`post.caption`)

- 1–2 short lines in the same voice. The first line can echo or extend the hook; don't repeat it word for word.
- Work in the plain words someone would search for — Instagram search reads captions: "group ride", "road bike", "first century", a place name the user gave you. Only true ones.
- One reason to respond — a light question ("where's your quiet loop?"), a send prompt when the reel is about a type of riding buddy ("send this to whoever says 'easy pace'"), a pick, or a series hook. See [Give people a reason to answer](#give-people-a-reason-to-answer).
- Under ~200 characters. No essays, no "link in bio" unless the user asks.
- Music credits are added automatically from the track's record — don't write them yourself. Trending songs (added with `--instagram`) get no credit line — Instagram shows the song on the reel.

## Give people a reason to answer

All six reels measured so far (posted Oct 26–31, last pull Nov 3) got 0 comments and 0 saves, and 0–3 likes on ~130–510 reach: people watched, almost nobody responded. Each reel gets **one** reason to respond — rotate them across the week, and never more than one per reel:

| Reason | Where | Natural | Not this |
| --- | --- | --- | --- |
| A question only cyclists can answer | caption (or the last text beat) | "what's your 6am excuse?" · "be honest, how early is too early for intervals" | "comment below!!" |
| A take to argue with | hook or last beat, from the user's real opinion | "zwift is better than riding outside in a houston summer" | ragebait the user doesn't believe |
| A send | caption | "send this to whoever said 'easy spin'" | "share if you agree" |
| A series hook | last beat + caption | "ep 3 next saturday: can i hold the wheel" | "follow for part 2" |
| A pick | caption | "left loop or right loop?" | "like for left, comment for right" |

- Meta demotes engagement bait ("comment YES", "like if…", "tag 3 friends"). Ask something a person would genuinely answer.
- `post.firstComment` can start the thread with a follow-up question or a detail ("the coffee was $7, for the record") — not a repeat of the caption.
- When someone besides the rider is in it and the user OKs it: `post.peopleTags` notifies them, and a Collab (`post.collaborators`, e.g. the club's account) puts the reel in front of their followers too. Only handles the user gives you.
- The other half is the user answering the first comments within the hour (QUEUE.md reminds them): replies count as comments too, and people keep talking under a reel that answers back.

## Hashtags (`post.hashtags`)

3–5 (Instagram allows at most 5 per post since December 2025). Start from config `hashtags.always`, add 1–3 that match this reel (`#hillrepeats`, `#crit`, `#gravel`…). No spammy tags (#follow4follow) and no brands that don't appear.

## Alt text (`post.altText`)

One plain sentence describing what's visible, for screen-reader users: "POV of a cyclist descending a twisting road past pine trees."

## Style picks

- `light` (white box, black text) — the classic TikTok look; default for hooks and beats.
- `dark` (black box, white text) — for a punchline or a contrasting second voice.
- `plain` (white with outline) — over darker footage, or when boxes feel heavy.
Keep one style per reel unless switching makes a joke land.
