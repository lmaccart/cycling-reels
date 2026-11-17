# Reels Editor

A native macOS editor that is part of this codebase. It edits the **real**
`plans/*.json`, so Claude Code in the terminal, `./reels render` and the app all
see the same files. Your original footage in `library/media/` is read-only to it.

## Install (once, and after source changes)

```bash
cd ~/rice/cycling
./editor/reels-editor setup      # or: bash setup.sh --editor
```

This installs the repo's Python and Remotion dependencies, builds the app and
copies it to `~/Applications/Reels Editor.app`. Open it from Finder or Spotlight
and keep it in the Dock (right-click its icon, Options, Keep in Dock). It remembers
this folder, so it launches with no terminal and no arguments.

Requires macOS 14+, full Xcode, Python 3.10–3.13, Node 18+, ffmpeg and
`sandbox-exec`. Claude Code is optional for manual editing; sign in to it locally
for chat and semantic search. No API key is needed.

From the terminal you can also use `./editor/reels-editor open [plans/x.json]`
(or double-click **Open Editor.command**) and `./editor/reels-editor info`.

## Using it

1. The welcome window shows your reels (newest first), **New Reel** and
   **Import Footage**.
2. Import copies files into `inbox/` (your originals stay where they are) and
   runs `./reels ingest`; the library refreshes when it finishes.
3. Edit with the library, timeline, inspector and chat. Every change is an undoable
   edit written straight to the plan file; edits made in the terminal appear in the app.
4. **Export** renders the plan to `out/<slug>/` exactly like `./reels render plans/<file>`.
   Nothing is ever approved, scheduled or published by the editor.

## Safety

- The editor never deletes or modifies anything in `library/media/`, and never
  deletes from `library/` or `plans/`. Footage import copies only.
- Editor state (undo history, previews, chat, jobs, lock) lives in
  `.cache/editor/live/`, which git ignores.
- Renderer and preview workers run in a macOS sandbox: no network, no `.env` or
  credential files, no writes anywhere in `library/`.
- Renderer code changes are not available in the app. Ask Claude Code in the
  terminal for those.

## Claude Code in the terminal

While the app is open it writes `.cache/editor/live/context.json` with the open
plan, the selected nodes and the playhead, so a terminal Claude Code session can
read it to see what you are looking at.

[Full usage and recovery](docs/USAGE.md)

## Offline 360 renderer

`setup` also builds `reels-sphere` and its precompiled Metal library into
`.cache/editor/bin/`. To build only the helper, without installing or launching
the app:

```bash
editor/tools/reels-sphere/build
swift test --package-path editor/tools/reels-sphere --scratch-path .cache/editor/sphere-tests
```

Xcode's optional Metal compiler is required; if `metal` reports a missing
toolchain, install it with `xcodebuild -downloadComponent MetalToolchain`.

`render.stage_video` capability-checks the helper **before** starting a render.
Supported inputs: 8-bit SDR BT.709 H.264/HEVC equirectangular MP4/MOV, and GoPro
two-track EAC with matching track dimensions and chroma-aligned seam crops.
The latter uses `sphere._track_chain`'s seam blend and bicubic side-face scale,
then v360's padded EAC mapping and 16-tap cubic planar interpolation. It does
not use a generic GoPro face table or the viewer's RGB bilinear sampler.
Both projections (flat and tiny planet), custom pitch/roll, shortest-arc paths,
holds/cuts, exterior cameraWindow keys, numeric stabilization and speed/fps
retiming share W4's math/clock conventions. SwiftPM links the **same**
`SphereMath.swift` used by the viewer, not a copied implementation.

Direct CLI: `.cache/editor/bin/reels-sphere --check request.json`, then
`--render request.json`. Exit codes: 2 invalid inputs, 3 unsupported capability,
1 runtime/decode/GPU/encode failure. JSON version 1 fields:

- Absolute `source`, new absolute `destination`; `sourceProjection` = `gopro-eac`
  or `equirect`; `trackIDs` are MP4 IDs (`ffprobe` stream `id`), **not** stream
  indexes. The GoPro example's stream indexes 0/5 have track IDs 1/6.
- `sourceIn`, `sourceOut`: source seconds; positive `speed`; even `width`/`height`;
  integer `fps` and exact `frames`; `codec` = `h264` or `hevc`.
- `projection` = `flat` or `tiny-planet`; nonempty ordered `keys` containing
  `at` (output seconds), `yaw`, `pitch`, `roll`, `fov` (degrees), optional `cut`.
  Pass compiler-resolved exterior keys unchanged for a split window.
- `fullRange`: source range; optional `corrections` containing `sourceStart`,
  `sourceStep = speed/fps`, and exactly `frames` unit `quaternions` in w/x/y/z
  order. These are Python's smoothed **correction** quaternions, not raw CORI.
  The clock must begin at `sourceIn`; wrong clocks/coverage are rejected.

AVAssetReader decodes into two-plane IOSurfaces; CVMetalTextureCache wraps their
planes. One GPU frame is in flight; the encoder pool is capped at eight buffers.
Shaders compile at build time, never per frame or inside the editor sandbox.
AVAssetWriter requires a hardware H.264 encoder (direct CLI requests also accept
HEVC). Output is published by rename only after the writer finishes. Audio is
not handled by the helper: staging retains the existing source trim, atempo,
bleep, cleaning, fades, padding and AAC mux. The helper outputs limited BT.709;
staging never converts it a second time. An explicitly requested grade remains
a separate pass; the default remains **no look**.

HDR, wide-gamut/other matrices, untagged SD (staging assumes BT.601),
10-bit/non-4:2:0, unsupported codecs, rotated/nonzero-origin tracks, and unaligned
GoPro seam crops select v360 with an explicit reason. Missing/non-macOS helper does likewise. Decoder/Metal/writer
errors **after** a successful capability check fail the job, never silently
render another image. `REELS_SPHERE_BACKEND=metal` requires actual Metal execution;
`v360` selects the reference; `auto` is the default.

Frozen editor Python snapshots receive the trusted helper's absolute path,
read/execute access to that binary and its metallib only, specific AGX/IOSurface
and codec services, and no new private-file/network access. Its binary/library
digest participates in the staged-cache runtime identity. Older renderer
snapshots without the new route retain their existing v360 behavior. The
sandbox integration test executes frozen `stage_video` on own real footage and
requires Metal evidence plus the resulting video/audio tracks.

Benchmark (originals and plan read-only; plan copies and media outputs are
temporary and removed; only an optional numeric JSON report survives):

```bash
./reels bench-sphere-metal --plan plans/2026-11-16-sprint-zone-nov-26.json \
  --seconds 30 --report /tmp/sphere-metal-results.json
```

The benchmark forces each backend separately, uses numeric stabilization 0.73 s,
checks every frame timestamp/dimensions and reports wall time, throughput,
encoders, sizes and all sampled PSNRs, including either side of the 10.5 s key.
`tests/test_sphere_metal.py` additionally compares dynamic poles/seams, holds,
cuts, fractional split windows/speed, tiny planet, and an equirectangular
conversion made solely from the user's real GoPro frames. It never manufactures
synthetic footage or passes by falling back on both sides. These are render
measurements, not proxy-prep measurements: W5b's requested 2× prep target remains
open; this helper is not integrated into proxies.

### Local 30-second sprint measurement

Apple M2 Pro; own `261031-gs010110-08e8.360`, two 2272×736 HEVC
tracks, 4,007,728,498 input bytes. Source 9.133333333333333–39.13333333333333 s;
speed 1; numeric stabilization 0.73 s; 900 frames at 30 fps, 1080×1920.
All output PTS matched `n/30`, both outputs were limited-range BT.709 yuv420p.

| Actual staging route | Wall seconds | Frames/s | Encoder | Output bytes |
| --- | ---: | ---: | --- | ---: |
| Existing v360 | 157.607849 | 5.710376 | libx264 veryfast CRF 16 | 57,561,666 |
| Metal | 5.600742 | 160.692996 | hardware h264_videotoolbox | 113,036,082 |

28.140531× end-to-end staging speedup for this run. This includes CORI
preparation, decoding, reprojection, encoding and silent mux; it is **not** an
isolated GPU comparison. Encoders/rate control differ; Metal used the quality-first
31.104 Mbps hardware setting and wrote 1.96× as many bytes. No proxy-prep claim.
The helper's internal decode/GPU/encode wall time was 4.760 s.

Every sampled PSNR (dB; v360 and actual Metal, never a fallback):

| Frame | Output seconds | Y | Average |
| ---: | ---: | ---: | ---: |
| 0 | 0 | 46.799023 | 47.507484 |
| 30 | 1 | 46.635609 | 47.335546 |
| 150 | 5 | 46.147734 | 46.930390 |
| 300 | 10 | 46.254933 | 47.165861 |
| 314 | 10.466667 | 46.483310 | 47.283577 |
| 315 | 10.5 | 45.956050 | 46.826419 |
| 316 | 10.533333 | 46.733253 | 47.496929 |
| 450 | 15 | 48.192085 | 48.686382 |
| 600 | 20 | 47.185972 | 47.800587 |
| 750 | 25 | 48.995703 | 49.388468 |
| 899 | 29.966667 | 48.678037 | 49.103761 |

All temporary plan copies, encoded comparisons and sampled footage were deleted
after collecting these numbers. Native equirectangular camera originals and
10-bit/HDR paths were not visually measured: equirectangular parity uses only an
own-footage conversion, while unsupported 10-bit/HDR sources explicitly use v360.
