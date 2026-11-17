# Native editor protocol

The app (`editor/ReelsEditor/`) launches `<repo>/reels editor serve` with the repo
as its working directory and talks to it over stdin/stdout. The service edits the
real `plans/*.json`; its own state (undo history, jobs, preview cache, chat) lives
in `.cache/editor/live/`. Nothing in the service deletes or rewrites files under
`library/` (the only writes there are transcript word corrections and the usage
records a render makes, same as `./reels render`); `tests/test_editor_live.py`
audits every delete/rename in the package.

## Local protocol (version 1)

Newline-delimited UTF-8 JSON. Requests are
`{"version":1,"id":"…","method":"…","params":{…}}`. Responses retain `id` and
contain `result` or `error: {code,message,details}`. Events contain `event`,
`sequence`, `revisions`, and `payload`. Standard output is protocol-only.

Methods: `workspace.info`, `plans.list`, `document.open` (`path`),
`document.snapshot`, `document.apply` (the batch below), `document.undo`,
`document.redo`, `document.reload`, `lease.acquire` (`owner`), `lease.release`
(`token`), `selection.set` (`ids`,`playhead`), `library.list`,
`library.asset` (`id`), `search.query` (`query`,`filters`,`generation`),
`search.cancel`, `preview.prepare` (`mode`: interactive/exact), `source.prepare`
(`id`,`view` optional), `job.status` (`id`), `job.cancel` (`id`), `export.start`,
`export.save` (`id`,`destination`,`artifact`: reel/review, `overwrite`),
`chat.send` (`text`,`model` optional), `chat.stop`, `chat.history`,
`permission.request`, `permission.respond`, `inspector.schema`, `renderer.read`,
`renderer.submit` (disabled: returns `renderer_changes_unavailable`), and `shutdown`.

Projects and footage:

- `plans.list` → `{plans:[{path,slug,title,postDate,format,segmentCount,durationSeconds,status,thumbnailURL,modified}]}`, newest first. `status` comes from `out/<slug>/post.json` (else `draft`).
- `projects.create` (`title?`, `postDate?`) → `{path}`; writes a new zero-segment plan, never overwrites.
- `library.import` (`paths`, `ingest`) → `{jobId,copied,skipped}`; copies media into `inbox/` (sources are never moved or deleted), then runs `./reels ingest` when `ingest` is true.
- `library.ingest` → `{jobId}`; `library.inbox` → `{count,files}`.
- Events: `job.changed` (`kind:"ingest"` carries `payload.log`), `library.changed`, `plans.changed` (`{paths,added,removed}`, polled every 2 s).
- `document.open` / `selection.set` write `.cache/editor/live/context.json` (`plan`, `selection`, `playheadSeconds`) so a Claude Code session in the terminal can see what is open in the app.
- `export.start` renders the open plan into `out/<slug>/` like `./reels render`. Nothing is approved, scheduled or published.

Snapshots include `documentId`, `revisions: {document,library,renderer}`, `plan`
(raw, never normalized), `nodes`, `fps`, `durationFrames`, `editingDurationFrames`,
`warnings`, and `preview`, merged `projectConfig`/`config`, and scoped permission records. Nodes
have stable `id`, `kind`, `lane`, `parentId`, `startFrame`,
`endFrame`, `frames`, and `raw` values; source nodes also include `assetId`,
`sourceIn`, `sourceOut`. Times on the wire are frames unless explicitly source
seconds or schema-local `at`/`for` seconds. `assetKind` distinguishes video/photos;
`generated`, `readOnly` and `timingState` describe projected nodes. A `viewOnly`
offline snapshot cannot be exported. The renderer is timing authority.

`durationFrames` remains the rendered duration. `editingDurationFrames` is the maximum of that
duration and every primary segment's endpoint, including disabled ghosts: use it for the timeline
viewport/scroll extent, not preview playback or export. Compiled payloads carry the same
`editingDurationFrames` next to `total`. Ghost starts use final beat/loop-snapped timing.

Batches contain `expected` (all three revisions), `reason`, `origin` (manual/ai),
`operations`, and optional `lease`. Operations use `op`:

- `insert_segment`: `value`, optional `beforeId`
- `remove_segments`: `ids`
- `reorder_segments`: `ids`, optional `beforeId`
- `duplicate_segments`: `ids` (primary segments). Copies get new IDs (linked cutaways, text,
  sfx… are copied with them). Sequential plans: inserted directly after the last original in
  plan order, later clips ripple right. Positioned plans: placed on the same tracks, shifted by
  the span of the selection (so a single clip lands right after itself); nothing else moves and
  an occupied range is rejected (disabled segments do not occupy ranges).
- `ripple_remove`: `ids`. Like `remove_segments`, but later items close the gap in positioned
  plans too (every track shifts left by the removed length; a clip crossing the removed range
  is rejected). Global text, sfx, music drops and exhibits use those same merged removed intervals:
  events starting inside a removed interval are deleted; later events move left, crossing spans shrink.
- `paste_segments`: `values` (segment dicts as copied from `get_project`; `timeline`, `_editor`
  and ID metadata are stripped and reissued), `atFrame` (global frame), optional `track`.
  Sequential plans insert at the nearest cut at or before `atFrame`. Positioned plans anchor the
  earliest copied clip at `atFrame` regardless of `values` order, preserving relative offsets, on
  `track` (default the first video track) with `move_segments` overwrite rules. Values without
  timeline starts are laid back-to-back in input order.
- `set_enabled`: `ids`, `enabled`. Stores `"enabled": false` on the segment (absent = enabled).
  A disabled segment stays on the timeline at full length (compiled segs and nodes carry
  `disabled: true` / `enabled: false`) but renders no video or audio; sequential plans close up
  around it. Global events inside a disabled sequential segment retain their definitions and IDs
  but are suspended from rendering. Enabling restores their owner-relative timing, including after
  other edits; suspension is persisted privately in `_editor.nodes[*].disabledTiming`.
- `trim_segment` / `trim_cutaway`: `id`, `sourceIn`, `sourceOut`
- `split_segment`: `id`, `atFrame` (global frame)
- `insert_overlay`: `parentId` (or document), `collection`, `value`
- `move_overlay`: `id`, `at` (schema-local seconds)
- `resize_overlay`: `id`, optional `at`, `for`
- `remove_overlay`: `id`
- `set_field` / `set_keyframes`: `id` (or document), `path` (JSON pointer), `value`
- `correct_words`: `assetId`, `corrections: [{index,word}]` (preserve timings)

Every mutation returns the resulting snapshot. Conflicts and validation errors
must be structured and leave HEAD untouched. Undo/redo create new revisions.
Field paths are relative to a stable node, e.g. `/volume`, never
`/segments/0/volume`. AI batches cannot borrow a manual lease token. The MCP
operation inventory is a discriminated JSON schema in `operations.py`.

Preview manifests carry `revisions`, `revision`, `mode`, `props`, `url` and
`staticBaseUrl`. Exact also provides `movie`/`movieURL`. An Interactive→Exact
fallback retains `requestedMode` and explains `fallbackReason`. The protected
bridge receives `window.postMessage({channel:"cycling-reel-preview",command}, origin)`
and emits revision-tagged events through the `reelsPreview` WK handler. Web content
has no editing bridge.

`source.ready` provides `assetId`, `sourceKind`, `url` and `sourceURL`;
`source.unavailable` provides `status` and `reason`. Library discovery returns
`sourceState=proxy_required` rather than a falsely playable original URL.
`chat.history` returns `{sessionId,records:[{role,text,state}]}`; `chat.delta`
streams prose, while `chat.record` carries completed prose/tool activity.
`export.ready` is emitted only after a completed job and includes its `jobId`.

Native `document.snapshot` remains complete. The editing MCP `get_project`
returns a bounded, paginated metadata view instead: preview props and duplicated
compiled data otherwise exceed Claude Code's tool-output limit. Follow `next`;
`get_node(id,path,offset,limit)` / `document.read` retrieve raw node, document or
config fields. `summaryOnly` explicitly marks non-raw values. Asset collections
and renderer source have bounded reads too (source offsets are characters).
AI operation responses use the same compact view; no unrestricted file reader
is added to work around CLI spill files.

## Python boundaries

`Workspace.live(repo)` in `editor/workspace.py` exposes `root` (`.cache/editor/live`),
`data_root` and `code_root` (both the repo), `project` (a `Project` whose
`library/media` is read-only), `media_status()`, `library_revision()` and
`renderer_revision()`.

`DocumentStore(workspace, plan_path)` in `editor/transactions.py` exposes
`snapshot()`, `apply(batch)`, `undo(expected=None)`, `redo(expected=None)`,
`acquire_lease(owner, ttl=15)`, `release_lease(token)`, `reload_external()`.
Snapshot compilation is renderer-based; raw plan data stays independent.
Edits made outside the app (Claude Code in a terminal, a text editor) are
picked up by `reload_external()` as new revisions.

Expensive renderer work runs in sandboxed workers, not the service broker.
