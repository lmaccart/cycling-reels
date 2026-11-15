# Implementation verification

The original development and evidence below were produced in the isolated
`cycling-editor` worktree. At the user's request, the application has now been
integrated into the main `cycling` codebase: native sources/docs/launchers in
`editor/`, shared broker in `scripts/reels/editor/`, shared Remotion composition.
The latest main hook/exhibit work was preserved.

Only implementation files were merged. Live plans, library metadata, exhibits,
analytics, learning records, originals and production dependencies are protected
from editor writes. Nothing was approved, uploaded, scheduled or published.
The integration is on the dedicated `feature/native-editor-integration` branch;
nothing was pushed. Private runtimes, workspaces and evidence are not committed.

The integrated launcher builds a code-only, versioned runtime and independent
dependencies outside the checkout, under
`~/Library/Application Support/CyclingReelsEditor/`. Private editable snapshots
and exports also stay there. Setup/refresh never replace an existing snapshot.
Native location validation accepts this explicit installed-runtime layout
without allowing the main checkout to become a writable editing workspace.

## Integrated-build verification

- Installed-runtime full Python suite: **606 tests run, one opt-in skip**, no failures.
- Sandboxed Exact-preview/private-export fixture passed separately using the
  installed runtime's own browser and dependencies.
- Native macOS suite: **25 passed**, including installed-runtime protection.
- Integrated app's Interactive Program advanced frames **15→30** on the real
  laundry project; the actual composed video was captured separately.
- Setup completed, then ran again without reinstalling dependencies or replacing
  the existing private snapshot.
- Fresh snapshot: **136 registered assets, zero offline**.
- **461 live data/dependency records** were verified unchanged. All newer main
  renderer/exhibit/type changes remained byte-identical to pre-integration HEAD.
- Main HEAD was not changed, and the previous feature worktree was preserved.
- The newer `2026-11-15-this-is-my-art` plan opens with its big multiline hook
  and 57 decorative exhibit nodes intact.
- Authenticated metadata search from the installed runtime returned six regex
  hits and two semantic enriched rows, with no error.

Integration logs/captures are retained at
`/Users/leif/rice/cycling-editor/.cache/editor/main-integration/`.

## Original development automated checks

- Full Python suite: **590 tests run, one opt-in test skipped**, no failures.
- The opt-in sandboxed Exact-preview/private-export test passed separately.
- Native macOS suite: **24 passed**, no failures or skips (xcresult summary saved).
- Remotion typecheck and protected preview bundle build passed.
- Setup shell syntax, tracked diff checks and whitespace checks on 70 new text
  files passed.

The full Python suite uses disposable projects. Its deliberate error,
approval/queue and analytics messages are fixture output, not production actions.
Full log: `.cache/editor/acceptance/python-suite.log`.

## Real-media and authenticated checks

| Check | Result / local evidence |
| --- | --- |
| Private library | 136 registered assets; the refreshed snapshot includes `library/progress.json`. |
| Road-to-5 | Opens with 182 compiled nodes, including progress, captions, B-roll and audio events. |
| Source transport | Registered original served with HTTP 206 byte ranges; native paused frame decoded. |
| Interactive Program | Native WK playback advanced through revision-tagged frames; actual composed viewport captured in `native-final.program.png`. |
| Exact versus export | Real laundry reel: all decoded video frames and decoded audio are identical. Peak −1.76 dBFS; zero clipped samples. |
| Other source types | Short real 360 reframe and photo drafts rendered through Exact; original plan hashes remained unchanged. |
| Claude edit | Authenticated CLI applied the requested volume in one typed operation on its first try; undo restored the omitted original value. |
| Manual conflict | Claude's attempted conflicting operation was rejected; the manual value survived, then undo restored the original. |
| Stop | Cancelled the real Claude turn, drained its owned launchers and left project revisions unchanged. |
| Semantic search | Final authenticated Haiku run returned six immediate regex hits and two grounded enriched rows, with no semantic error. |
| Renderer candidate | A bounded existing-footage punch-strength parameter, its first use and matching Exact preview activated together. Descriptor appeared; parameter adjustment and undo passed. |
| Failed candidate | Syntax error rejected; active source revisions and working Exact preview retained. |
| Clip reuse | Unchanged staged products are reused across other clips' edits, with source/effect/audio/renderer invalidation tests. Real candidate parameter adjustment reports cache hits/misses. |
| Safety | Actual macOS fixtures verify original-write denial, external-network denial, protected-data reads and other-renderer cache-write denial. No candidate ran unsandboxed. |

Original development evidence remains in the preserved
`/Users/leif/rice/cycling-editor/.cache/editor/acceptance/`:

- `real-program-export.json`, `exact-export-frames.jpg`
- `real-format-smoke.json`, `real-360-frame.jpg`, `real-photo-frame.jpg`
- `live-conflict-stop.json`, `renderer-candidate-proof.json`
- `native-final.png`, `native-final.default.png`, `native-final.program.png`,
  `native-final.playback.json`
- Authenticated semantic-search reports, including final paginated-tool checks.

The native whole-window bitmap can omit WebKit's GPU-backed video layer.
The separate Program snapshot contains the actual video and overlays; the
playback JSON records observed frame advancement, not merely preparation success.

## Integration issues caught and fixed

- Automatic/native preview requests could cancel each other, including the
  reservation-before-job-ID interval.
- The file watcher treated unchanged snapshots as changes and restarted preview
  every second.
- Source discovery did not request all compatibility proxies supported by the
  preparation pipeline.
- Node/Browserslist tried to read an ancestor home-directory configuration.
  Lookup now uses explicit targets/root scope instead of broader sandbox reads.
- Renderer caches needed separate write namespaces so failed candidate code
  could not alter the active Program bundle.
- Expanded project responses exceeded Claude Code's display limit and spilled
  to inaccessible files. Bounded project/node/asset/source/evidence/log reads
  solve this without adding filesystem access.
- Candidate activation scheduled an unnecessary Interactive preparation that
  could replace its already validated Exact preview.
- Native caption pages were mistaken for B-roll because they have a parent;
  they now use a separate Captions lane without duplicate word strips.

## Still requires human review

- Subjective audio quality and sustained hands-on editing/drag/keyboard usability.
- Full end-to-end foreground-alpha, attached-Instagram-song and loop-seam review
  across the user's longer projects. Their contracts/fallback rules are tested;
  the short real-format smoke is not a claim that every project was watched.
- The consent UI records explicit scoped decisions; it does not itself generate
  material or bypass the renderer's protected title-card/content policies.

See [USAGE.md](USAGE.md) for build/run, refresh and recovery.
