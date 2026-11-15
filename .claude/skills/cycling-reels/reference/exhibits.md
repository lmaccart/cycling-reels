# Exhibits: a study, diagram or chart on screen that the rider points at

An exhibit is an image or chart laid over a talking clip. It is the one exception to "every visual is the user's footage", with hard edges:

- Images come **only from `exhibits/`**, and only the user puts files there (`./reels exhibits add <file>` copies one they name). Never download, screenshot or generate one. If the reel needs a study the user hasn't supplied, ask them for the screenshot.
- Overlay only: an exhibit can't be a segment, cutaway or background.
- Charts are drawn by the template from numbers **the user gave** (`points` / `values`) or from **their own Strava ride** (`"data": "strava:power"`). Same rule as stat pills: never type in a number the user didn't give.
- No source label is added. A screenshot that shows the authors is preferred, not required.

```json
{"asset": "261112-img-3601", "in": 2.0, "out": 9.0, "captions": "transcript",
 "exhibits": [{"image": "seiler-polarized.png", "at": 1.2, "for": 4.5, "x": 0.66, "y": 0.34, "width": 0.55}]}
```

`./reels exhibits` lists the folder · `./reels exhibits preview <plan> [--at S]` renders one still per exhibit into `out/<slug>/exhibits/` (about 20 s) — look at it and nudge before a full render · `./reels check` prints each exhibit's size, spot and how it was placed.

## Fields

On a segment (`at` = seconds from the segment start) or at the top level of the plan (`at` = reel seconds). An exhibit may run on past a cut.

| Field | Default | |
|---|---|---|
| `image` *or* `chart` | – | File in `exhibits/` (name, name without extension, or a unique prefix) / a chart spec (below) |
| `at`, `for` | 0.3 s, to the end of its segment | At least 0.5 s on screen |
| `width` | 0.6 | Fraction of the frame width; height follows the image. Tall images are shrunk to fit |
| `x`, `y` | 0.5, 0.36 | Centre, as fractions of the frame. Kept inside the safe area (under Instagram's top bar, above its caption; one that reaches the lower half also stays 130 px from the right edge, clear of the like/comment/share buttons on Instagram and TikTok) |
| `style` | `card` | `card` = plain rectangle with a soft shadow (a see-through PNG gets a white backing; `background` changes it, `padding` px adds a border). `cutout` = the PNG's own shape, shadow follows it |
| `shadow` | true | |
| `tilt` | 0 | Degrees, −20…20 |
| `safeArea` | true | `false` = a decorative overlay (a pile of stickers) may sit anywhere on the frame, edges and the app's button column included. Placed exhibits only |
| `crop` | – | `[left, top, right, bottom]` fractions: show one part of a big screenshot (one figure of a paper) |
| `enter` / `exit` | `pop` / `fade` | `pop`, `fade`, `slide`, `none` |
| `anchor` | – | `"hand"`: place it from the pointing fingertip (B) |
| `depth` | `front` | `behind`: the rider passes in front of it (C) |

## Three ways to place it

**A · placed (default).** `x`/`y`/`width`. The user films themselves pointing at a spot; you put the exhibit there. Look at the clip's frames (`./reels frames <id> --at …`) for where the finger is, then `exhibits preview` and nudge. Works on any footage.

**B · anchored to the hand.** `"anchor": "hand"` finds the extended index finger in the frames the exhibit is up (on-device, Apple Vision) and puts the exhibit just beyond the fingertip, on the side it points to.
- `side`: `auto` (the pointing direction), `left`, `right`, `above`, `below` · `gap`: space between finger and exhibit (fraction of frame width, 0.03).
- `point: [x, y]`: instead, put **that spot of the image** under the fingertip ("this dip right here"). Pairs with `depth: "behind"` so the finger sits on top.
- `follow: true`: the exhibit tracks the hand (smoothed) instead of sitting at its steady position.
- `./reels check` says what it found (`at the fingertip (0.55, 0.37; hand seen in 92% of frames)`). When no pointing hand is found it warns and falls back to `x`/`y`, so always give those a sensible value.
- It takes the hand with the index finger out, so a hand holding a mic or the bars can be mistaken for pointing. Start the exhibit when the point starts, and check the preview.

**C · depth.** `"depth": "behind"` cuts the rider out of the same clip (on-device person mask) and lays them back over the exhibit, so the hand and head pass in front. Adds ~5 s of render per second of exhibit. Needs the rider on screen: video segments only, and not while a cutaway is up. Punch-ins are followed. Hair and helmet edges are good but not perfect; watch the render.

B and C need macOS with Xcode's command line tools (the helper is compiled once, on first use). A works everywhere.

## Text moves out of the way

Captions, the hook and text beats that would sit on an exhibit move to the nearest free space above or below it while it's up (captions keep one position through a run, so they don't hop). `./reels check` warns when there's no room: make the exhibit smaller or move it. Stat pills, the route map and the progress bar don't move; keep exhibits off the top-left/top-right corners when those are up.

## Charts

```json
{"chart": {"type": "line", "title": "retention", "unit": "%", "points": [[0, 100], [1, 95], [3, 40], [30, 25]],
           "mark": {"x": 3, "label": "0:03"}, "xLabel": "seconds"}, "at": 1.0, "for": 4.0, "width": 0.62}
{"chart": {"type": "bar", "values": [310, 322, 335], "labels": ["sep", "oct", "nov"], "title": "ftp", "unit": "W"}}
{"chart": {"data": "strava:power", "from": 12, "to": 52, "mark": {"x": 31, "label": "blew up here"}}}
```

| Field | |
|---|---|
| `type` | `line` (default; draws itself on) or `bar` (≤ 12 bars; starts at zero) |
| `points` / `values` + `labels` | Numbers the user gave |
| `data` | `strava:power`, `strava:hr`, `strava:speed`, `strava:cadence` from the ride linked to the clip under it (or `"ride": "<id>"`); `from`/`to` = minutes into the ride; `smooth` = seconds (power 30) |
| `title`, `unit`, `xLabel` | Shown; Strava charts fill them in |
| `mark` | One labelled point: `{"x": …, "label": "…"}` |
| `theme`, `color`, `aspect`, `draw` | `light`/`dark` (dark uses the brand accent), series colour, width/height (1.25), `false` = no draw-on |

One series per chart. Two things to compare = two exhibits, one after the other.

## Filming for it

Tell the user: hold the point for the whole line (2–4 s), keep the pointing hand clear of the face, and leave empty wall or sky where the exhibit should go — upper half of the frame, since captions sit low. For `point` (finger on the chart), point at mid-air at arm's length to the side.
