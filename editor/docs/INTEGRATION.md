# Native editor implementation contract

The approved development plan is `PLAN.md` alongside this file. The native app
now lives in `editor/`, with the shared service in `scripts/reels/editor/`.
The installer creates a code-only runtime, independent dependencies and private
workspace outside the live checkout. Editing/rendering must never write back to
the production project or its originals.

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
`renderer.submit`, and `shutdown`.

Snapshots include `documentId`, `revisions: {document,library,renderer}`, `plan`
(raw, never normalized), `nodes`, `fps`, `durationFrames`, `warnings`, and
`preview`, merged `projectConfig`/`config`, and scoped permission records. Nodes
have stable `id`, `kind`, `lane`, `parentId`, `startFrame`,
`endFrame`, `frames`, and `raw` values; source nodes also include `assetId`,
`sourceIn`, `sourceOut`. Times on the wire are frames unless explicitly source
seconds or schema-local `at`/`for` seconds. `assetKind` distinguishes video/photos;
`generated`, `readOnly` and `timingState` describe projected nodes. A `viewOnly`
offline snapshot cannot be exported. The renderer is timing authority.

Batches contain `expected` (all three revisions), `reason`, `origin` (manual/ai),
`operations`, and optional `lease`. Operations use `op`:

- `insert_segment`: `value`, optional `beforeId`
- `remove_segments`: `ids`
- `reorder_segments`: `ids`, optional `beforeId`
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

`Workspace.load(path, code_root=None)` / `init_workspace(source, workspace,
code_root=None)` in `editor/workspace.py`. A Workspace exposes `root` (workspace),
`data_root`, `code_root`, `project`, `media_status()`, `library_revision()`.

`DocumentStore(workspace, plan_path)` in `editor/transactions.py` exposes
`snapshot()`, `apply(batch)`, `undo(expected=None)`, `redo(expected=None)`,
`acquire_lease(owner, ttl=15)`, `release_lease(token)`, `reload_external()`.
Snapshot compilation is renderer-based; raw plan data stays independent.

Other modules may add adapters, but must not silently create a second model.
Expensive/candidate renderer code must execute in workers, not the service broker.

## Parallel write ownership

- Workspace agent: `config.py`, `cli.py`, `reels`, `editor/workspace.py`,
  `editor/__init__.py`, `test_editor_workspace.py`; CLI registers editor lazily
  through `editor.commands.register_parser` / `editor.commands.run`.
- Document agent: `editor/transactions.py`, `editor/timing.py`,
  `editor/document.py`, focused transaction/timing tests.
- Native UI: `editor/ReelsEditor/`, product/design/surface context only.
- Preview agent: Remotion preview entry/build, `editor/preview.py`,
  `editor/renderer.py`, `editor/worker.py`, preview/renderer tests.
- Search agent: `editor/search.py`, search tests.
- Integrator: protocol/service/commands/jobs/Claude/MCP, dependencies,
  integration tests, documentation, and cross-boundary fixes after handoff.

Agents must not commit, change production, copy credentials, or start other
agents. The user's approved ceiling is two Luna and one Astra concurrently.
Keep contexts focused and summarize handoffs.
