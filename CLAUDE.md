# cycling-reels

This folder turns ride footage into Instagram Reels. For anything involving footage, clips, reels, captions or posting, use the `cycling-reels` skill (`.claude/skills/cycling-reels/SKILL.md`) and run tools through `./reels` from this folder.

- Every visual comes from the user's own footage in `library/` — never generate, download or synthesize clips, images or backgrounds.
- Outside editors such as Kino are for review and fine-tuning only (`./reels project`, `.claude/skills/cycling-reels/reference/kino.md`): never their generative/AI features, and nothing exported from them is posted — edits come back as plan changes and `./reels render` makes the reel. Ask before uploading footage to one.
- Music comes from YouTube via `./reels music` / `./reels yt2mp3` — never generated. Songs are picked from the current trending Reels lists and mixed into the reel (`.claude/skills/cycling-reels/reference/music.md`).
- Sound effects come from the free library in `sfx/` (human-made, CC0 / Pixabay licence, recorded per file) or the user's own ride audio — never AI-generated unless the user explicitly asks.
- What to film each ride: `SHOTLIST.md`.
- On-screen text and captions are written in a natural TikTok voice (`.claude/skills/cycling-reels/reference/captions.md`).
- Never schedule or publish a reel the user hasn't approved in the conversation.
- Collab invites (`post.collaborators`) and people tags (`post.peopleTags`) only for Instagram handles the user gives you, of people who are in the reel or agreed — never guess a handle.
- Don't put numbers or facts on screen or in captions that the user didn't give you or the footage doesn't show. GPS recorded by the camera counts as the footage (via the `gps` stats, never typed in by hand). Strava data from the user's own account counts as the user giving it, heart rate and power included (via `strava:` stats only). Strava route maps: full route is fine, except rides starting/ending in Maine lose the first/last 1 km. Never show other athletes' Strava data.
- `library/media/` holds the user's originals — never edit them, and never delete from it except whole chapters through `./reels prune --apply`, one round at a time, after the user approves that exact list in the conversation.
- Settings live in `config.json` (handle, text style, posting times and timezone, hashtags, Whisper model).
- Every weekly batch starts with `./reels review` after refreshing analytics when available. Read `learning/LESSONS.md`, this/last week's review JSON and `learning/experiments.json`; keep audience response, user preferences and production issues separate. Follow `.claude/skills/cycling-reels/reference/learning.md` for feedback, pre-render experiments and closing the review after scheduling. Permanent skill/code changes need user approval; code changes need tests. Learning never overrides the rules above. A session without `out/` (a cloud checkout) doesn't run `./reels review` — it would match none of the posted reels — and saves its notes to `learning/notes/` for the next local run to apply (learning.md).
