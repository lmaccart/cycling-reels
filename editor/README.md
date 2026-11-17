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
digest participates in both whole-tree and per-clip staged-cache keys: changing
the helper rebuilds staged media; the same runtime identity remains reusable.
Source/sphere proxy keys still use the explicit `PROXY_VERSION`, independent of
ordinary Python source edits and this staging runtime identity. Older renderer
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
measurements, not proxy-prep measurements; see the separate proxy measurement below.

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

## Metal proxy preparation

The installed service's source/aim workers and CLI `prewarm-360` use the same
trusted native adapter, cache keys, publication lock and disk reservations.
No editor UI or plan/config changes are required. Missing helpers and unsupported
colour/layout/codec/timing select the existing accurate ffmpeg/v360 pipeline,
with an explicit `fallbackReason`; it prefers VideoToolbox, then libx264.
Native H.264 failure tries native HEVC before that fallback. Required
`REELS_SPHERE_BACKEND=metal` fails rather than silently measuring ffmpeg.
Worker results, cache markers and prewarm logs expose backend and encoder.

- **Front/back:** 1440×1280, two 720×1280 rectilinear views packed horizontally,
  30 fps. The worker supplies its existing resolved travel-direction/gravity/
  tagged fallback views. One source decode and GPU pass serve both viewports.
- **Aim:** 2048×1024 equirectangular, 30 fps, at most 180 source seconds.
  Whole-sphere pixels remain unstabilized; aiming and requested stabilization
  stay live. Covering-cache reuse never changes the request-relative CORI clock.
- **Proxy preset:** hardware H.264 preferred, 8 Mb/s target, 12 Mb/s ceiling,
  GOP 10, no B-frames, 8-bit 4:2:0 BT.709 SDR, preserving source full/limited
  range. This is not W7's quality-first limited-range render preset.
- **Audio:** the native helper is silent; bounded ffmpeg copies the native video
  and retains the existing input audio seek/trim, first-audio mapping and AAC
  160 kb/s mux. Video PTS remain `n/30`.
- **Cache version 5:** version 4 is retained but not reused under the new pixel/
  codec contract. Compatible v5 ranges remain reusable regardless of ordinary
  Python edits or frozen renderer revision. Helper hashes identify execution,
  not proxy keys; requiring Metal cannot count a cached ffmpeg result as native.
- **Safety:** both native and mux processes inherit the same key-lock and lease
  descriptors. The existing 50 GB floor and finite reservation cover simultaneous
  video/mux outputs. Hard per-process file limits and final size checks reject
  truncation before atomic publication. Interrupted writers retain their inputs
  and partials; startup cleanup cannot acquire their inherited lock. A cached
  covering range still plays below the floor; missing CORI is computed in memory.

The v1 helper request additionally accepts `preset: "proxy"` with
`projection: "equirect"` or `"front-back"`, `speed: 1`, `fps: 30`, empty `keys`,
no corrections and positive `maxOutputBytes`. Front/back requires exactly two
resolved `views` with `at: 0`, yaw/pitch/roll/fov; aim takes no views.
Omitting `preset` preserves the W7 render contract.

Sandbox grants remain exact helper/metallib reads and execution plus named
AGX/IOSurface, Metal compiler and codec services—no broad filesystem or network
grant. The trusted `reels.editor` modules work even when the frozen snapshot
lacks all proxy helpers. `tests/test_proxy_metal.py` requires real Metal for both
actual worker actions, checks old snapshots, and kills Python with SIGINT/SIGKILL
while real native and audio-mux writers retain their key lock and reservation.

Reproduce cold preparation (only the user's original is read; temporary derived
media is deleted, optional numeric JSON survives):

```bash
./reels bench-proxy-metal --asset 260627-gs010096-7da3 --seconds 30 \
  --report /tmp/proxy-metal-results.json
```

Each backend gets fresh media/CORI directories. The original software path and
merged W5b VideoToolbox+v360 path use the same dimensions, fps, range and GOP as
native. The source-only benchmark bounds the browsing encoder to 30 seconds;
normal browsing preparation still covers the original. Full worker time includes
sandbox setup/probe, telemetry, encoding/mux, CORI and publication. Process times
are reported separately; none of these runs is a warm-media hit.

### Local 30-second proxy measurement

Apple M2 Pro; original `260627-gs010096-7da3.360` (634,125,741 bytes),
source 0–30 s, 900 frames at 30 fps. Front/back 1440×1280; aim 2048×1024.
All three paths ran through `PreviewManager._run_worker` and its production
sandbox, with fresh media/CORI directories and the same uncached telemetry work.
The operating-system source-page cache was shared. Every video PTS matched
`n/30`; audio packet PTS/durations/sizes and decoded PCM SHA-256 matched.
All outputs preserve this source's full-range 8-bit 4:2:0; GOP ≤10, no B-frames.

| Proxy | Actual backend / encoder | Full worker s | Encode job s | Output bytes |
| --- | --- | ---: | ---: | ---: |
| Front/back | v360 / libx264 ultrafast CRF 21 | 14.814253 | 14.225456 | 47,857,801 |
| Front/back | v360 / h264_videotoolbox | 10.643911 | 10.209209 | 30,629,646 |
| Front/back | Metal / h264_videotoolbox | 5.652135 | 5.181799 | 30,628,341 |
| Aim | v360 / libx264 ultrafast CRF 23 | 14.001434 | 13.643501 | 47,938,683 |
| Aim | v360 / h264_videotoolbox | 10.591574 | 10.230247 | 30,636,620 |
| Aim | Metal / h264_videotoolbox | 5.993431 | 5.603202 | 30,637,650 |

Encode job includes capability/startup, decode, reprojection, encoding and mux,
not telemetry/CORI/publication. Native internal decode/GPU/encode times:
4.482362 s front/back, 4.951560 s aim. Native subprocess wall times:
4.604780 / 5.075779 s; separate audio-mux subprocesses: 0.400874 / 0.406782 s.
No number here is isolated hardware-encoder time.

Full preparation speedup: **2.621001× front/back and 2.336130× aim** against
the original software path; **1.883166× / 1.767197×** against W5b.
The ≥2× original-baseline aim timing target is met on this run, **not** a 2×
claim against W5b and **not** an overall acceptance pass. Earlier W5b
measurements were 16.595→11.750 s front/back and 16.056→11.625 s aim; those are
historical runs, not replacements for the contemporaneous baselines above.

Encoded sample PSNR (dB):

| Frame | Front/back vs software | Front/back vs W5b | Aim vs software | Aim vs W5b |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 39.638094 | 42.412696 | 37.927270 | 40.606686 |
| 30 | 35.826417 | 39.226808 | 31.539648 | 37.110133 |
| 150 | 31.599655 | 37.175137 | 29.745059 | 34.613933 |
| 300 | 37.609411 | 41.251904 | 34.450745 | 38.371885 |
| 450 | 37.074983 | 40.671032 | 34.251974 | 37.889927 |
| 600 | 36.839791 | 40.733835 | 34.143688 | 38.056415 |
| 750 | 37.343167 | 40.717300 | 33.293326 | 38.373758 |
| 899 | 29.796037 | 32.730961 | 29.241763 | 32.594121 |

### Proxy parity investigation

The encoded-output ≥35 dB acceptance gate is **not met** on every sample.
Do not treat the speedup or raw projection parity as a substitute for that gate;
`bench-proxy-metal` retains `pass: false` and exits 1.

To isolate the cause, temporary instrumentation captured native NV12 immediately
before the encoder and compared it with the existing v360 filter's uncompressed
NV12 on the same original, source frame, range and output dimensions. No visuals
were synthesized. Instrumentation and derived media were removed afterwards.

| Frame | Front/back raw PSNR dB | Aim raw PSNR dB |
| ---: | ---: | ---: |
| 0 | 69.402028 | 68.306652 |
| 30 | 70.148443 | 69.023920 |
| 150 | 69.495863 | 68.484035 |
| 300 | 70.695137 | 69.182277 |
| 450 | 70.641323 | 69.447897 |
| 600 | 70.481341 | 69.516303 |
| 750 | 70.182068 | 69.333131 |
| 899 | 70.240142 | 69.176652 |

Maximum raw component error: 1/255 front/back, 2/255 aim. Geometry/horizon,
whole-sphere poles/seams and source-frame selection agree closely. Compression
dominates the remaining difference: at frame 899, native-vs-own-raw /
W5b-vs-own-raw PSNR is 30.939830 / 30.963732 dB front/back and
30.201032 / 30.212758 dB aim. Nevertheless, comparing the two encoded outputs
gives 32.730961 / 32.594121 dB respectively. The original software and W5b
encoders also disagree below 35 dB; this does not prove the requested threshold
impossible and does not waive it. Bitrate, dimensions and GOP were not relaxed.
