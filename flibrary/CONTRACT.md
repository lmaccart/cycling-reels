# flibrary — app ↔ backend contract

flibrary is a lightweight native macOS app (SwiftUI, `flibrary/`) for browsing the
footage in `library/` and dragging it into Premiere, plus 360 tools. The app does
no media processing itself: it calls the Python backend through the repo's
`./reels flibrary <sub>` CLI (`scripts/reels/flibrary.py`), started with
`Process` from the repo root.

## Hard rules (both sides)

- **Read-only library.** Never write, move or delete anything under `library/`
  or `plans/`. `library/media/` holds the user's originals.
- Exports go to `flibrary-exports/` at the repo root (git-ignored). App state
  (keyframes, settings) lives in `.cache/flibrary/state.json`. Temporary/cache
  files in `.cache/flibrary/`.
- **Disk guard:** the Mac is nearly full (~19 GB free). Every export estimates
  its size first and refuses (error, no partial file) if free space after the
  export would be under 10 GB. Writes go to a temp name in `flibrary-exports/`
  and are renamed on success; a failed or cancelled export deletes its temp file.
- No network, no generated visuals, no 360 preview proxies (`prewarm-360`).

## CLI (all JSON on stdout, UTF-8; errors: `{"error": "..."}` + exit code 1)

### `./reels flibrary list`
```json
{"root": "/abs/repo", "exportsDir": "/abs/repo/flibrary-exports", "freeBytes": 20401094656,
 "assets": [{
   "id": "261121-gs010119-c867", "kind": "video" | "photo",
   "media": "/abs/.../library/media/261121-gs010119-c867.360",
   "originalName": "GS010119.360", "created": "2026-11-21T07:06:14",
   "duration": 185.49, "width": 2944, "height": 1472, "fps": 59.94,
   "is360": true, "projection": "gopro-eac" | "equirect" | null,
   "thumb": "/abs/.../library/thumbs/<id>.jpg" | null,
   "sheets": ["/abs/.../library/sheets/<id>.jpg", ...],
   "sheetGrid": {"cols": 4, "rows": 3, "times": [..seconds per tile..]} | null,
   "tags": {"type": "...", "shot": "...", "quality": 3, "people": "...", "summary": "...",
            "hooks": ["..."], "location": "...", "notes": "..."},
   "angles": {"forward": {"yaw": 180, "pitch": 40, "fov": 90, "roll": 180}, ...},
   "exports": [{"kind": "equirect" | "flat", "path": "/abs/...mp4", "bytes": 123, "label": "equirect" | "flat 1080x1920"}]
 }]}
```
Sorted newest first. `sheetGrid` describes how a contact sheet is tiled (if the
sheet layout can be determined) so the app can hover-scrub 360 clips.

### `./reels flibrary frame <id> --t SECONDS [--width 2048] [--view YAW,PITCH,ROLL,FOV --w W --h H]`
Writes a JPEG to `.cache/flibrary/frames/` (reuses an existing cached file) and prints
`{"path": "/abs/...jpg"}`. Without `--view`: the **levelled equirect still** (2:1, see "Orientation convention"):
exactly the picture an equirect export contains (unstabilized), so the app's aim viewer can draw from it.
With `--view`: a flat reframed still using exactly the same view convention (levelled frame) as the
flat export, for checking that the app's preview matches the export.

### `./reels flibrary estimate <job.json>` → `{"bytes": N, "freeBytes": N, "ok": true|false, "reason": "..."}`

### `./reels flibrary export <job.json>`
Job:
```json
{"id": "<asset id>", "kind": "equirect" | "flat", "stabilize": false,
 "in": 0.0, "out": 185.49,
 "width": 1080, "height": 1920, "fps": 30,
 "keys": [{"at": 0.0, "yaw": 180, "pitch": 40, "roll": 180, "fov": 90}]}
```
- `in`/`out` optional (default whole clip). `width/height/fps/keys` are for `flat` only;
  `keys[].at` is seconds relative to `in`, strictly increasing, eased between keys
  (same easing as the reels renderer's camera paths).
- Equirect: full source resolution, source fps, HEVC (`hevc_videotoolbox`, `hvc1`
  tag), source audio kept, upside-down mount corrected so the horizon is level, with
  Spherical Video V1 metadata (GSpherical XMP `uuid` box) so Premiere detects it as VR.
  `stabilize: true` applies the camera's CORI orientation correction per frame.
- Flat: HEVC, source audio for the range, keyframed camera. Prefer the Metal
  `reels-sphere` helper (`.cache/editor/bin/reels-sphere`), falling back to ffmpeg v360.
- Progress: one JSON object per line on stdout while running:
  `{"progress": 0.42}`; final line `{"done": true, "path": "/abs/flibrary-exports/....mp4"}`
  or `{"error": "..."}` (exit 1). SIGTERM cancels and cleans up the temp file.
- Output names: `<id>-equirect[-stab].mp4`, `<id>-flat-<W>x<H>-<8-char hash of job>.mp4`.
  An existing identical output is reused (prints done immediately).

## App state file `.cache/flibrary/state.json` (owned by the app)
```json
{"reframe": {"<asset id>": {"in": 0, "out": 30, "width": 1080, "height": 1920, "fps": 30,
  "stabilize": false, "keys": [{"at": 0, "yaw": 0, "pitch": 0, "roll": 0, "fov": 90}]}}}
```

## Orientation convention (levelled frame)
The GoPro MAX is bar-mounted upside down and tilted, so the camera's own sphere is not level. The backend
therefore defines one **levelled frame** per clip and everything the app sees or sends is in it:
- **Equirect still / export:** centre column = the bike's forward direction, horizon a straight horizontal line at
  exactly mid-height, sky up. Pixel (x, y) of a w x h still is the ray
  `yaw = (x / w - 0.5) * 360` (positive = to the RIGHT, range -180..180) and `pitch = (0.5 - y / h) * 180`
  (positive = UP, range -90..90): the picture centre is yaw 0 / pitch 0, the left/right edges are yaw -180/+180.
  Verified on footage: yaw +40 shows the scene right of forward, pitch +30 looks up at the sky.
- **Flat keys (`keys[]`, `--view`):** yaw/pitch/roll/fov are in this same levelled frame and use the same signs. `yaw 0,
  pitch 0, roll 0` looks straight ahead along the bike with a level horizon (the default key; the old default of the
  tagged forward angle yaw 180/pitch 40/roll 180 is obsolete). To aim at a point (x, y) of the still, send
  yaw = (x/w - 0.5)*360, pitch = (0.5 - y/h)*180, roll 0; roll turns the picture about the viewing axis (0 keeps the
  horizon level). `fov` is the horizontal field of view in degrees (10..150) as before. The backend composes the
  levelling rotation onto each key (`in_source_frame`) for both the Metal helper and the ffmpeg fallback; easing
  between keys is unchanged.
- **How the levelling is found** (`level_rotation`, cached per file in `.cache/flibrary/level/<id>.json`): the camera's
  mean gravity vector over the whole clip (GPMF GRAV) gives "down"; the forward direction is the accelerometer/GPS
  measured travel direction (`stabilize.travel_direction`), else the tagged `forward` view projected onto the horizon
  (`stabilize.front_back_views`). Without orientation data it falls back to the full tagged `forward` angle (yaw, pitch
  AND roll), and to the sphere as shot when there is no tag. The same rotation is used for every range of the clip
  so a still at any t and an export always agree. `stabilize: true` composes the per-frame CORI correction on top.
- Cached stills/names carry `LEVEL_VERSION` (currently 1), so older cached frames and flat exports are not reused.
  Equirect export names have no hash: delete an `<id>-equirect[-stab].mp4` made before this change to re-export it.

## Backend notes (additions, as implemented in scripts/reels/flibrary.py)
- `list`: numeric fields are `0` (never null) when unknown (photos: duration/fps). `tags` omits `angles`/`moments`.
  `sheetGrid` also carries `perSheet`, `tileW`, `tileH`, `gutter`, `header`, `sheetWidth`, `sheetHeight`:
  tile i is on sheet `i / perSheet`, at col/row `divmod(i % perSheet, cols)`, pixel origin
  `x = gutter + col*(tileW+gutter)`, `y = header + row*(tileH+gutter)`. `null` if the sheet layout can't be inferred.
  Exports labelled `equirect`, `equirect stabilized`, `flat WxH`.
- `frame --view` values are validated (pitch ±90, roll ±180, fov 10..150). Cached name encodes t, width/view.
- Job `keys[]` may carry `"cut": true`. Errors from `estimate`/`export` before work starts are `{"error"}` on stdout, exit 1.
- Equirect orientation: the levelled frame above (gravity-levelled, forward in the centre).
- Equirect and flat exports render with the Metal helper when it is built (`REELS_SPHERE_BACKEND=v360` forces ffmpeg);
  equirect through Metal is full resolution at the source's NTSC rate, ~0.15 bits/pixel/frame, stabilized or not
  (3 s stabilized 60 fps clip: ~3 s wall). Audio is copied from the source, hvc1 tag and the Spherical V1 box are kept.
- Cancel: SIGTERM prints `{"error": "cancelled"}`, exit 1, removes `.<name>.partial`. The ffmpeg fallback for stabilized equirect is slow
  (~17 s of compute per second of 60 fps footage); Metal is faster than real time.
- Estimate `ok` guards the peak: every export counts 2x size (Metal video-only file plus the muxed file).
