# Kino: review and fine-tuning in a timeline editor (optional)

[Kino](https://kino.ai) (kino.ai, YC S23, launched for agents in late October 2026) is a browser/desktop
video editor and media manager built for people and coding agents working on the same project. It
matters here for three things, all optional:

1. **Review links with time-coded notes** — the user watches a reel on their phone and leaves notes at
   exact moments instead of describing changes in chat. (Share links are a Kino Pro feature.)
2. **Fine-tuning on a real timeline** — Kino imports a Remotion project folder and keeps editing it as a
   Kino composition. `./reels project <plan>` writes exactly that for one reel.
3. **Footage search in plain language** — Kino transcribes, describes and face-detects what's imported.
   Only with the user's say-so (see Privacy): the contact-sheet tagging here already covers planning.

Nothing in the pipeline needs Kino. The CLI never talks to it; Claude does, through Kino's MCP server
when the user has connected it.

## Rules that still apply

- **The plan is the source of truth, and `./reels render` makes the reel that posts.** Changes made in
  Kino come back as plan edits (trims, order, text, cutaways), then the reel is re-rendered locally, where
  every clip is checked against the original it was ingested from. Never schedule, upload or export a
  file that came out of Kino.
- **Only the user's footage.** Never use Kino's motion graphics, generative fills, AI B-roll, AI voices
  or stock libraries in a reel — on screen, text styling is this project's template; everything else is
  library footage. If the user adds something like that in Kino, say it can't go into the posted reel.
- **Approval is unchanged.** A note in Kino is feedback, not approval: approval is the user saying yes in
  the conversation to the reel rendered here.
- **Facts are unchanged.** Numbers on screen still come only from `gps` / `strava:` stats or the user.

## Privacy

- Ask before uploading anything to Kino: it's a third-party cloud, and it face-detects what it indexes
  (teammates and strangers in group-ride footage).
- `./reels project` folders hold only one reel's staged clips — never the library, transcripts, GPS
  tracks or Strava files. Prefer sharing those over importing raw footage.
- Route maps in an exported reel are already trimmed the way the plan renders them (home rides lose their
  first and last km). Don't upload `library/telemetry/` or `strava/`.

## Setup (once, by the user)

- Kino account; the free tier includes the editor and agent access via MCP (with a weekly cap on MCP
  calls); Pro adds share links and unlimited MCP calls.
- Connect it to Claude Code — either the hosted MCP server: `claude mcp add kino https://kino.ai/mcp`,
  or the desktop app's local connector via the plugin (`kino-ai/kino-plugin` marketplace, plugin
  `kino@kino-plugin`; Kino must be running, and Node/npm installed). Tools appear in a new session.
- Tool names and arguments vary between versions: read the connector's tool schemas rather than assuming
  them. If a Kino call fails, show the user the error and carry on without Kino.

## The workflow

1. Render and check the reel as usual (`./reels check`, `./reels render`), read `out/<slug>/preview.jpg`.
2. If the user wants to review in Kino: `./reels project plans/<file>.json` → `out/<slug>/project/`
   (a standalone Remotion project: template, the reel's exact props, its staged clips/music/sfx).
   - Kino: import that folder as a Remotion project (or have the user do it), and with Pro, create a
     share link for them.
   - Without Kino: `cd out/<slug>/project && npm install && npm run studio` opens it in Remotion Studio.
3. Read the user's notes (Kino comments via the connector, or what they say). Record each actual change
   request with `./reels review feedback --category preference --kind revision --slug <slug> --note "…"`.
4. Apply them to the plan, `./reels check`, `./reels render`, and show the new preview. If they trimmed or
   moved something in Kino's timeline, read the timeline (or ask) and translate it into the plan's
   `in`/`out`/order — don't copy Kino's export.
5. Approval, queueing and scheduling go on exactly as in the main skill.

Kino's import of Remotion projects and its MCP tools are new; if an import doesn't look like the local
render, trust `out/<slug>/reel.mp4` and tell the user what differed.
