# Weekly learning loop

Run this loop for every weekly batch, including unattended preparation. Improve the
whole project: keep audience response, user preferences and production quality separate.
Performance never overrides approval, footage ownership, rights, privacy or factual accuracy.

## Before planning

1. Pull/import analytics per [analytics.md](analytics.md); run `./reels stats` when data is
   available. Without the connector, retain old snapshots and disclose the gap. Missing
   analytics should not block preparing a batch.
2. Run `./reels review`. It creates dated JSON history and a Markdown summary under
   `learning/reviews/`. Weeks start Monday in the configured posting timezone.
3. Read `learning/LESSONS.md`, this week's review JSON, `learning/experiments.json`, and
   the previous week's review if present. Markdown shows the latest checkpoint only;
   JSON retains all checkpoints and original notes.
4. Identify evidence-backed strengths, weaknesses and next actions across all three
   areas. Record missing footage and unresolved failures, not just successes. Save the
   interpretation with `./reels review --file <notes.json>`; the CLI gathers evidence,
   but the planner interprets it without inventing observations or causes.
5. Aim for five familiar approaches and two experimental reels in a seven-reel batch.
   This is a target, not a quota: little evidence means no proven winners, and insufficient
   footage means fewer reels. Test one deliberate change, keeping other factors comparable.
   Observational comparisons are not randomized A/B tests.

## Evidence and maturity

- Under 48 hours **at collection time**: don't judge. Two to seven days: preliminary.
- Prefer the earliest snapshot collected at 7–14 days. Weekly polling cannot guarantee
  exactly seven days: inspect actual ages in JSON and note timing differences. Never
  substitute lifetime totals for a missed window.
- Review JSON records stale/missing data, unmatched IDs, missing metrics and experiment
  samples. Missing is unknown, not zero. Historical window samples remain valid historical
  observations even when the latest connector pull is stale.
- Require at least three measured reels per group before reviewing a pattern. This is
  only a floor, not statistical proof. Favor repeated large differences; inspect reach,
  ages and confounders. Don't declare winners from one reel or a tiny change.
- Experiment status is `awaiting-data` or `ready-for-review`, never an automatic win.
  Review the predeclared success criterion, medians and individual samples; record
  supported/unsupported/inconclusive and the next decision in review notes.

## Register experiments before rendering

Write an object, then run `./reels review experiment --file <experiment.json>`:

```json
{
  "id": "2026-w45-face-first",
  "hypothesis": "opening on the rider's face may improve first-three-second hold",
  "change": "first shot only: rider's face instead of road POV",
  "primaryMetric": "hold",
  "successCriterion": "a repeated material improvement over comparable POV openings; otherwise inconclusive",
  "testSlugs": ["2026-11-02-face-climb", "2026-11-04-face-group"],
  "controlSlugs": ["2026-11-03-pov-climb", "2026-11-05-pov-group"]
}
```

Metric keys: `hold` (percentage points), `watchThrough`, `sharesReach`, `savesReach`,
`likesReach`, `commentsReach`, `engagement` (likes+comments+shares+saves per reach) and
`viewsReach` (views per account reached: replays) — all ratios but `hold`. IDs are immutable: identical registration is
a no-op; a changed design needs a new ID. Test reels cannot already have post.json;
historical controls are allowed but disclose confounders. Two tests can stay inconclusive;
register enough future test slugs for replications rather than rewriting an old design.

## Record feedback and friction as it happens

```bash
./reels review feedback --category preference --kind revision --slug <slug> --note "user asked for a shorter, less polished hook"
./reels review feedback --category production --kind audio --slug <slug> --note "wind hid the freehub; replaced the nat window"
./reels review feedback --category production --kind scheduling-error --slug <slug> --note "audio rejected; awaiting user's choice"
```

Record actual user edits, approvals/rejections and reasons, not inferred tastes. Before
discarding rejected reels, record feedback so the reason survives deletion. `mark` records
a production status event automatically; this isn't a preference or proof of user approval.
Add actual preference/reason explicitly when supplied. CLI renders record attempts,
successes and failures; top-level failures record sanitized command events. Connector
failures and visual/audio issues need explicit entries. Record resolutions too. Never
store credentials, signed URLs or unsanitized remote error payloads in learning files.

## Save interpretations and lessons

Review input accepts these optional lists. Keep lessons concise and evidence verifiable:

```json
{
  "strengths": ["production: checks passed first time; cite this week's actual events"],
  "weaknesses": ["audience: too few seven-day samples to compare hooks"],
  "decisions": ["<slug>: apply lesson audio-01; <other-slug>: face-first test"],
  "proposals": ["propose audio validation if quiet-nat issues recur; don't apply without approval"],
  "lessons": [
    {
      "id": "audio-01",
      "category": "production",
      "observation": "the selected freehub window was covered by wind",
      "evidence": ["learning/feedback.jsonl: <actual timestamp and slug>"],
      "confidence": "low",
      "state": "tentative",
      "nextAction": "listen to the chosen nat window before rendering",
      "reviewDate": "2026-11-09"
    }
  ]
}
```

These are schema examples, not facts to import. Use actual evidence. Categories:
`audience`, `preference`, `production`. Confidence: `low`, `medium`, `high`. State:
`tentative`, `active`, `retired`. Reuse a lesson ID to revise/retire it in a subsequent
checkpoint; old versions remain in JSON. Review overdue lessons and contradictions.
`learning/LESSONS.md` is generated: change lessons through review input, not that file.

Maintain tentative lessons, explicit preferences and experiment outcomes during weekly
runs. Moving lessons into permanent skills, recipes, SHOTLIST.md, validation or renderer
code needs a proposed diff and user approval; code changes also need tests. Repeated
evidence is a reason to propose a change, not permission to apply it. Never weaken safeguards.

## After approval and scheduling

Confirm scheduled posts with Metricool, record IDs with `./reels mark`, then explicitly close:

```bash
./reels review close --slugs <slug1> <slug2> --file <notes.json>
```

Use `--week YYYY-MM-DD` after `close` for an earlier batch week. Include all intended
batch reels, including queued/approved ones whose scheduling failed. The checkpoint
captures statuses, slots, IDs, plan paths, history, experiment assignments and lessons/actions
in notes. Incomplete scheduling is reported honestly; review never changes statuses,
approves, uploads or schedules. For manual scheduling, only mark scheduled when the
user confirms it really was scheduled.

Scheduling closes production review, not audience evaluation or proof of publication.
Revisit results next week. Keep publication/mute failures separate from creative performance;
record unresolved errors in the closing checkpoint.

## Unattended runs

Run the planning review, record lessons/experiments, then prepare/render only. Don't
close scheduling, approve, queue, upload or schedule. Include the review path, stale-data
warnings, experiments and proposed changes in the completion report. These commands
don't create an external recurring task: the existing weekly task follows the skill.
