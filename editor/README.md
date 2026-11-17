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
The compiler exposes the authored camera duration as `sphere.durationSeconds`,
separately from visible source in/out and speed (or photo duration). It is the
duration used to validate/resolve the path, including default photo endpoints,
and survives beat/music retiming for both primary footage and cutaways.
Camera slicing retains that duration and all playback keys in `cameraWindow`;
piece durations and editing handles never restart, shorten or quantize that
playback clock. Handles use finalized visible source bounds, not the authored
duration or rounded timeline frame count. Pieces shorter than the 100 ms
editable-key gap retain playback through the window without an editable path.
Live-camera handles and subsequent camera transactions use the same compiled
visible bounds, including music-retimed pieces. Windowed pieces may be shorter
than the video/photo authoring minima; their editable keys still obey the
100 ms gap, and sequential cutaways still obey the 0.4 s on-screen minimum.

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

Approved proxy acceptance (fixed before the common-reference measurement):
- Native **pre-encode average PSNR ≥35 dB at every sample**.
- Decode native and W5b and compare both against the **same uncompressed v360
  frame**. Native Y and average PSNR must each be ≥W5b minus **0.10 dB at every
  sample**, in both layouts; no averaging away failures. Exact equality and
  positive infinity pass; NaN never passes.
- The original ≥35 dB encoded-to-encoded proxy gate **failed**. The user approved
  this replacement to distinguish reprojection parity from independent lossy
  encoding while bounding final-quality regression against W5b. Both original-
  software and W5b encoded-to-encoded PSNR remain diagnostics, not passing gates.
  **W7 rendered-reel encoded ≥35 dB is unchanged.**
- Full cold aim preparation must still be ≥2× the contemporaneous original
  software path. Timing never includes proof readback.

The benchmark captures all eight frames (0, 30, 150, 300, 450, 600, 750, 899
for 30 seconds), including front/back Y regions and north/south pole/seam Y
regions. It replays the actual W5b input/trim/v360 graph without encoding and
records SHA-256 for every raw reference frame; every backend uses those same
bytes, full/limited range, 8-bit planar 4:2:0 and 4:1:1 sample-weighted MSE.
Y/U/V and average PSNR, source/encoder commands, profiles, bytes and times
survive in JSON. Native and W5b must both use hardware H.264 with equal profile
and the unchanged 8 Mb/s target, 12 Mb/s ceiling, GOP10/noB preset.

An additional **separately timed** sandboxed native run requests `proofSampleFrames`
(proxy-only, at most eight ordered unique frame indexes). Only those buffers
are CPU-locked/copied before encoding into tightly packed NV12 sibling files
of the new private destination; existing files cannot be overwritten. Normal
production requests omit the option and do no readback allocation/copy.
The proof request must match production except destination and this option.
Existing worker locks, reservations and sandbox grants remain intact; an extra
benchmark lease covers retained outputs/raw samples and preserves the 50 GB
floor. All temporary footage is removed on completion.

### Integrated-head 30-second measurement

Apple M2 Pro; original `260627-gs010096-7da3.360` (634,125,741 bytes,
identity quick hash `7da34d414a9a810ef7cb0856d5c95e3b416ff8d8`), unchanged
before/after. Source 0–30 s, 900 frames at 30 fps. Front/back 1440×1280;
aim 2048×1024. All three paths ran through `PreviewManager._run_worker` and
its production sandbox, with fresh media/CORI directories and identical
uncached telemetry conditions; OS source-page cache shared.

Every video PTS matched `n/30`; audio packet PTS/durations/sizes and decoded
PCM SHA-256 matched. All outputs preserve this source's full-range 8-bit
4:2:0; GOP ≤10, no B-frames. Native and W5b both encoded H.264 High, with
the same 8 Mb/s target/12 Mb/s ceiling preset. The original software
baseline remains ultrafast CRF21/23, Constrained Baseline, not that preset.

| Proxy | Actual backend / encoder | Full worker s | Encode job s | Output bytes |
| --- | --- | ---: | ---: | ---: |
| Front/back | v360 / libx264 ultrafast CRF21 | 14.144224 | 13.544424 | 47,857,792 |
| Front/back | v360 / h264_videotoolbox | 10.650018 | 10.212755 | 30,629,646 |
| Front/back | Metal / h264_videotoolbox | 5.704343 | 5.237022 | 30,628,341 |
| Aim | v360 / libx264 ultrafast CRF23 | 14.142493 | 13.778285 | 47,946,327 |
| Aim | v360 / h264_videotoolbox | 10.569670 | 10.196941 | 30,636,620 |
| Aim | Metal / h264_videotoolbox | 6.027713 | 5.629887 | 30,637,650 |

Encode job includes capability/startup, decode, reprojection, encoding and mux,
not telemetry/CORI/publication. Native internal decode/GPU/encode:
4.495284 s front/back, 4.956753 s aim. Native render subprocess wall:
4.617628 / 5.079091 s; audio mux subprocess: 0.433739 / 0.428756 s.
These are not isolated hardware-encoder times. Separate proof-readback worker
times: 5.602423 / 6.014945 s; excluded from speedup.

Full preparation speedup: **2.479553× front/back, 2.346245× aim** against
the original software path; **1.867002× / 1.753513×** against W5b.
The ≥2× original-baseline aim target passes on this integrated code path;
the earlier 2.336130× result is not substituted for this measurement.

Encoded-to-encoded sample PSNR (dB), **diagnostic only**:

| Frame | Front/back vs software | Front/back vs W5b | Aim vs software | Aim vs W5b |
| ---: | ---: | ---: | ---: | ---: |
| 0 | 39.638094 | 42.412696 | 37.927270 | 40.606686 |
| 30 | 35.826417 | 39.226808 | 31.539648 | 37.110133 |
| 150 | 31.599655 | 37.175137 | 29.745059 | 34.613933 |
| 300 | 37.609411 | 41.251904 | 34.450745 | 38.371885 |
| 450 | 37.074983 | 40.671032 | 34.251974 | 37.889927 |
| 600 | 36.839791 | 40.733835 | 34.143688 | 38.056415 |
| 750 | 37.343167 | 40.717300 | 33.293326 | 38.373758 |
| 899 | 29.807844 | 32.730961 | 29.485515 | 32.594121 |

### Approved common-reference proof

`bench-proxy-metal` exited **0**, `pass: true`; temporary media removed.
Full-precision per-frame Y/U/V/average/region values, raw frame hashes,
actual encoder/reference commands, profiles, packet clocks, byte counts and
all worker/subprocess timings are retained locally in
`.cache/editor/w8-common-reference.json`. Nothing was generated/downloaded:
all pixels derive from the user's unchanged original.

The following values compare each decoded proxy to the **same uncompressed
v360 reference**, not its own pre-encode image. Raw avg compares native
pre-encode NV12 (deinterleaved without range conversion) to that reference.
All PSNR values are dB; average uses 4:1:1 Y:U:V sample-weighted MSE.
Full-range raw extraction explicitly uses `yuvj420p` to avoid accidental
full-to-limited rescaling; planar layout and 255 peak are unchanged.

**Front/back**

| Frame | Raw avg | Native Y | W5b Y | Native avg | W5b avg | Software Y | Software avg |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 69.402028 | 37.946204 | 37.952116 | 39.475007 | 39.478085 | 46.037799 | 47.327140 |
| 30 | 70.148443 | 33.805374 | 33.812391 | 35.438027 | 35.447720 | 40.869147 | 42.379821 |
| 150 | 69.495863 | 32.117043 | 32.103540 | 33.786315 | 33.773652 | 29.955558 | 31.561609 |
| 300 | 70.695137 | 36.291045 | 36.259181 | 37.894054 | 37.865554 | 37.431392 | 38.981283 |
| 450 | 70.641323 | 35.450420 | 35.456290 | 37.101809 | 37.107648 | 38.121164 | 39.692408 |
| 600 | 70.481341 | 35.391045 | 35.385320 | 37.036044 | 37.029044 | 37.315662 | 38.875051 |
| 750 | 70.182068 | 36.004967 | 36.000844 | 37.587192 | 37.584740 | 38.013132 | 39.495744 |
| 899 | 70.240142 | 29.258937 | 29.285664 | 30.939955 | 30.963732 | 28.875200 | 30.536397 |

**Aim**

| Frame | Raw avg | Native Y | W5b Y | Native avg | W5b avg | Software Y | Software avg |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 68.306652 | 36.176010 | 36.173161 | 37.733896 | 37.730052 | 43.992517 | 45.265805 |
| 30 | 69.023920 | 29.249424 | 29.249366 | 30.931827 | 30.932046 | 36.601807 | 38.155298 |
| 150 | 68.484035 | 28.748619 | 28.770972 | 30.440239 | 30.462147 | 28.098982 | 29.754864 |
| 300 | 69.182277 | 33.330421 | 33.349957 | 34.969510 | 34.988350 | 32.991315 | 34.586542 |
| 450 | 69.447897 | 32.215558 | 32.223719 | 33.901311 | 33.909379 | 35.206363 | 36.817712 |
| 600 | 69.516303 | 32.662359 | 32.667379 | 34.324642 | 34.329819 | 33.230546 | 34.839459 |
| 750 | 69.333131 | 33.009714 | 33.004611 | 34.628172 | 34.621377 | 31.776674 | 33.316328 |
| 899 | 69.176652 | 28.525736 | 28.538401 | 30.200913 | 30.212758 | 28.733861 | 30.375051 |

Worst native-minus-W5b common-reference deltas: front/back frame 899
**−0.026727 dB Y / −0.023777 dB average**; aim frame 150
**−0.022353 / −0.021908 dB**. All exceed the fixed −0.10 dB floor.
Pre-encode average minima: **69.402028 dB front/back, 68.306652 dB aim**,
both frame 0; maximum raw component error **1/255 / 2/255**.

Geometry evidence, raw Y PSNR over each half and 32-pixel pole/seam strips:

| Frame | Front | Back | Aim north | Aim south | Aim seam |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 0 | 67.763652 | 68.315146 | 66.653007 | 66.083710 | 69.340786 |
| 30 | 68.735568 | 68.774196 | 67.734358 | 67.783019 | 69.583874 |
| 150 | 68.894995 | 67.419530 | 67.226868 | 65.149493 | 70.210343 |
| 300 | 69.108040 | 69.532366 | 66.459340 | 67.882008 | 69.861076 |
| 450 | 69.802860 | 68.677995 | 69.420313 | 67.330341 | 67.200043 |
| 600 | 69.279578 | 68.992159 | 68.653842 | 65.562086 | 71.285010 |
| 750 | 70.972873 | 67.525219 | 69.367134 | 64.344913 | 71.855155 |
| 899 | 69.990375 | 68.238148 | 69.483191 | 64.788039 | 70.805570 |

Common-reference identity: SHA-256 of the exact planar bytes consumed by
**all three** encoded comparisons for that frame. Native pre-encode samples
use these references too; hashes are not reconstructed from own-reference
compression scores.

| Frame | Front/back raw SHA-256 | Aim raw SHA-256 |
| ---: | --- | --- |
| 0 | `620e4578c98f3d70adc4347c810d2ee590661ea2fa27406e0a96fd836328555b` | `a18d3730243d15afd3218ed871e83a0cfd0bc2d1ebd769f492743aef3bdcd925` |
| 30 | `e31558484ed78cc9c834b6e42e23a71f0dc5558ad8c7ec133dde853e48a7b00a` | `44081fd391b8bbbb7cc3f5a4d8d1cb1904c60ebfcc5207a2162a881d3bcdab39` |
| 150 | `170a8b16786c767e5e562a5075e74d496f425b3d7206440d5462147fe96cb35e` | `b4d9d2efaa03d10a148315069150ec782549eb48ad5f9aa1a6bfd4f18dd3574b` |
| 300 | `d559d0e3184bc61b060d1074e12a3db2e6870975109ce6c81c42db0c6d44b430` | `29eef9771272057addfaf14386deb80d4734820879018da21c6e41414d34b3d0` |
| 450 | `47cce69782e34626eb974dbd237fd52840175ffdf40845202aec5715fe9cdb53` | `07c9e1b94f3d9d448e95586ecd7ceba0d1e088c509e0fac65bfb998d9ef41036` |
| 600 | `de6922cc09b87f9cadc62c17e4a6c2975fc5123c8faf286cef0769ec0401799e` | `4d66c116a88ae7d37bd5f96ed083e06cd32e36a74f926d0aaaa8c9e5af6fd804` |
| 750 | `383a1266fda0580e3d0b5ddfb2e5089b8a34b98854b6f63ed814e5427cf96f56` | `01ce3dd0db8cfeff490c312ec2ea5f73472907a55eca74fc05b9099b28b4869b` |
| 899 | `30fa8a31ba3b2e1a745c0b356015210e1fecd9baf0cac30dab322ba5f88ca688` | `7e2629f0be7b3e34b4907cff9018fe65113b1e5f01ab05b017617f71341acfb1` |

### Integrated local verification and limits

After the numerical and actual common-reference proof:
- Full Python suite: **990 passed**, zero failures/errors/skips, 450.670 s;
  main checkout Python, `REELS_RUN_SANDBOX_RENDER_PROOF=1`.
- CLI SwiftPM suite: **9 passed**, zero failures/skips.
- Full macOS editor suite after rebasing onto `b879553` (W3b #32):
  **143 passed**, zero failures/skips. Main checkout Python explicitly supplied;
  W3b real-service edit/preview/undo/redo and real-compiler prediction tests,
  plus aim service/context round-trip, passed.
- Numerical gate tests: **4 passed**, including isolated failed samples and
  either Y/average deficit, exact equality/infinity and NaN rejection.

CI billing is blocked; these are local results, not CI certification.
One M2 Pro/30-second source/eight fidelity samples is not an all-footage
guarantee. HEVC proxy fallback was not forced on real hardware; the 12 Mb/s
ceiling is configured, not a separately measured rolling-window bound.
No installed-app setup/launch or whole-library backfill was performed.

