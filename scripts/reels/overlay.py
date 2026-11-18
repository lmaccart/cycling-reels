"""Fast render path: Chrome only for the frames that need it.

The full render screenshots every frame in headless Chrome (Remotion). Most frames of most reels are plain
footage plus text, which holds still, so here Remotion instead renders
  * the sound (ReelAudio: the same clip audio, music and sound effects, no pictures),
  * one transparent still of the overlay layer (ReelOverlay) per distinct text state, plus each frame of the
    short entrance/exit animations, and
  * only the frames where something Chrome-only is on screen: either every frame of the overlay layer
    (exhibits with finger paths, route maps, progress bars, the title board, foregrounds) as a transparent PNG,
    or, where the footage itself is changed (punch-ins, blur patches, photos, crossfades, photo cutaways),
    the whole opaque reel frame, spliced in over the footage.
ffmpeg joins the staged clips, lays video cutaways and the stills over them, and adds the sound.
Only editor tracks (a placed timeline) and the safe-area guide still need the full render, and so does a reel
where nearly every frame needs Chrome anyway.
"""

from __future__ import annotations

import os
import struct
import subprocess
import zlib
from pathlib import Path
from typing import Any

from .config import Project
from .media import require, run
from .util import ReelsError, write_json

# Frames of animation, mirroring the template (TikTokText.useEntrance, Captions).
TEXT_IN = 4  # hook / text beat / end card: pop in over 4 frames (the 4th is fully in)
TEXT_OUT = 4  # and fade out over the last 4
CAPTION_IN = 2  # captions: 0.4 -> 1 opacity over 2 frames
EXHIBIT_IN = 7  # exhibits: enter over the first 7 frames, leave over the last 5 (Exhibit.tsx)
EXHIBIT_OUT = 5
CHART_DRAW = 34  # a chart draws itself on until this frame of the exhibit
BAR_IN = 6  # progress bars: fade in over 6 frames, fill after that, fade out over the last 5 (ProgressBar.tsx)
BAR_OUT = 5
CHIP_FADE = 4  # stat pills: fade in over the first 4 frames and out over the last 4 (StatChips)


MERGE_GAP = 2  # Chrome runs this close together are joined (the frames between are rendered too, harmlessly)
CHUNK = 60  # a Chrome run is read by ffmpeg in pieces this long, each its own input, so PNG decoding runs in parallel
MAX_CHROME_SHARE = 0.85  # above this share of Chrome frames the full render is as fast and simpler


def seg_starts(props: dict) -> list[int]:
    """First frame of each segment: back to back, a crossfade pulling the next one in early."""
    out, at = [], 0
    for i, s in enumerate(props["segments"]):
        if i > 0:
            at -= int(s.get("transitionInFrames") or 0)
        out.append(at)
        at += s["durationInFrames"]
    return out


def _punch_frames(keys: list, n: int) -> set[int]:
    """Frames (from the segment start) where the punch-in is not exactly 1x."""
    out: set[int] = set()
    if keys[0][1] != 1:
        out.update(range(0, keys[0][0] + 1))
    for (a, s0, *_), (b, s1, *_) in zip(keys, keys[1:]):
        if s0 != s1 or s0 != 1:
            out.update(range(a, b))
    if keys[-1][1] != 1:
        out.update(range(keys[-1][0], n))
    return {f for f in out if 0 <= f < n}


def _exhibit_moves(e: dict, local: int) -> bool:
    """True while an exhibit changes from frame to frame (entering, leaving, following the finger, a chart drawing)."""
    dur = e["endFrame"] - e["startFrame"]
    plain = dur < 8 and e.get("ownerSegmentIndex") is not None  # a short exhibit owned by its clip doesn't animate
    if e.get("depth") == "behind":  # the rider's cut-out video plays in front of it
        return True
    if not plain and e.get("enter") != "none" and local < EXHIBIT_IN:
        return True
    if not plain and e.get("exit") != "none" and local > dur - EXHIBIT_OUT:
        return True
    path = e.get("path") or []
    if len(path) > 1 and local < path[-1][0]:
        return True
    chart = e.get("chart") or {}
    return e.get("kind") == "chart" and bool(chart.get("draw")) and local < CHART_DRAW


def _bar_moves(b: dict, local: int) -> bool:
    dur = b["endFrame"] - b["startFrame"]
    return local < BAR_IN or local > dur - BAR_OUT or (b.get("fillFrames", 0) > 0 and local < BAR_IN + b["fillFrames"])


def chrome_frames(props: dict) -> tuple[set[int], set[int], dict[str, int]]:
    """(full, over, why): reel frames Chrome must draw whole (the footage itself changes), those where only the overlay
    layer needs Chrome (exhibits, maps, bars, title board), and how many frames each kind of effect asked for.
    Blur patches are drawn by ffmpeg (blur_filters), so they cost Chrome nothing, except where a punch-in zooms the
    footage under them: those frames are already in `full` for the punch."""
    total = int(props["totalFrames"])
    full: set[int] = set()
    over: set[int] = set()
    why: dict[str, int] = {}

    def add(target: set, lo: int, hi: int, what: str) -> None:
        r = {f for f in range(max(0, lo), min(total, hi))}
        if r:
            target |= r
            why[what] = why.get(what, 0) + len(r)

    starts = seg_starts(props)
    for i, (s, at) in enumerate(zip(props["segments"], starts)):
        n = s["durationInFrames"]
        if s["kind"] != "video":
            add(full, at, at + n, "photos")
        if i > 0 and s.get("transitionInFrames", 0) > 0:
            add(full, at, at + s["transitionInFrames"], "crossfades")
        if s.get("punch"):
            for f in _punch_frames(s["punch"], n):
                add(full, at + f, at + f + 1, "punch-ins")
        for c in s.get("cutaways") or []:
            if c["kind"] != "video":
                add(full, at + c["startFrame"], at + c["startFrame"] + c["durationInFrames"], "photo cutaways")
    for e in props.get("exhibits") or []:
        for f in range(e["startFrame"], e["endFrame"]):
            if _exhibit_moves(e, f - e["startFrame"]):
                add(over, f, f + 1, "exhibits")
    for b in props.get("progress") or []:
        for f in range(b["startFrame"], b["endFrame"]):
            if _bar_moves(b, f - b["startFrame"]):
                add(over, f, f + 1, "progress bars")
    for r in props.get("routes") or []:  # the dot moves every frame
        add(over, r["startFrame"], r["endFrame"], "route maps")
    for fg in props.get("foregrounds") or []:
        add(over, fg["startFrame"], fg["startFrame"] + fg["durationInFrames"], "exhibits")
    if props.get("titleCard"):
        add(over, props["titleCard"]["startFrame"], props["titleCard"]["endFrame"], "title board")
    over -= full
    return _bridge(full), _bridge(over) - _bridge(full), why


def _bridge(frames: set[int]) -> set[int]:
    """`frames` plus the short gaps (<= MERGE_GAP) between runs, so Chrome renders fewer, longer ranges."""
    out, ordered = set(frames), sorted(frames)
    for a, b in zip(ordered, ordered[1:]):
        if 1 < b - a <= MERGE_GAP + 1:
            out.update(range(a + 1, b))
    return out


def runs(frames: set[int]) -> list[tuple[int, int]]:
    """Contiguous [first, last+1) ranges of a set of frames."""
    out: list[tuple[int, int]] = []
    for f in sorted(frames):
        if out and out[-1][1] == f:
            out[-1] = (out[-1][0], f + 1)
        else:
            out.append((f, f + 1))
    return out


def chunks(props: dict) -> list[dict]:
    """Chrome's frame ranges, cut into CHUNK-frame pieces: [{kind: 'full'|'over', start, end, dir}], dir under stills/."""
    full, over, _ = chrome_frames(props)
    out = []
    for kind, frames in (("full", full), ("over", over)):
        for a, b in runs(frames):
            for lo in range(a, b, CHUNK):
                out.append({"kind": kind, "start": lo, "end": min(b, lo + CHUNK), "dir": f"{kind}-{lo}"})
    return sorted(out, key=lambda c: c["start"])


def why_not(props: dict) -> str | None:
    """None when the reel can take the fast path; otherwise what needs the full render."""
    if props.get("timeline"):
        return "explicit video tracks and gaps require the full layered composition"
    if props.get("safeArea"):
        return "the safe-area guide is on"
    full, over, why = chrome_frames(props)
    share = (len(full) + len(over)) / max(1, int(props["totalFrames"]))
    if share > MAX_CHROME_SHARE:
        return f"{share:.0%} of its frames need Chrome ({', '.join(sorted(why))})"
    return None


def _text_state(item: dict, f: int, enter: bool = True) -> Any:
    """'static' while a hook/beat/end card holds still, else the animation frame it's on."""
    local, dur = f - item["startFrame"], item["endFrame"] - item["startFrame"]
    if (enter and local < TEXT_IN) or local > dur - TEXT_OUT:
        return local
    return "static"


def _chip_state(chip: dict, f: int) -> tuple:
    """A stat pill's look on frame `f`: its fade frame (or 'static' while fully in) and which live value it shows."""
    local, dur = f - chip["startFrame"], chip["endFrame"] - chip["startFrame"]
    short_owned = dur < 2 * CHIP_FADE and chip.get("ownerSegmentIndex") is not None
    fade = "static" if short_owned or CHIP_FADE <= local <= dur - CHIP_FADE else local
    live = chip.get("live")
    value = 0
    if live and live["values"]:
        value = min(len(live["values"]) - 1, max(0, local // live["everyFrames"]))
    return ("stat", fade, value)


def frame_state(props: dict, f: int, chrome: tuple[set[int], set[int]] | None = None) -> tuple:
    """Everything that decides what the overlay layer looks like on frame `f`. Equal states draw identically.
    Frames Chrome draws itself (whole, or as the overlay layer) show nothing from the still stream."""
    full, over = chrome if chrome is not None else chrome_frames(props)[:2]
    if f in full or f in over:
        return ("chrome",)
    state: list = []
    for key, tag in (("exhibits", "exhibit"), ("progress", "bar")):  # holding still (the moving frames went to Chrome above)
        for i, e in enumerate(props.get(key) or []):
            if e["startFrame"] <= f < e["endFrame"]:
                state.append((tag, i))
    for i, c in enumerate(props.get("stats") or []):
        if c["startFrame"] <= f < c["endFrame"]:
            state.append((i,) + _chip_state(c, f))
    for i, b in enumerate(props["beats"]):
        if b["startFrame"] <= f < b["endFrame"]:
            state.append(("beat", i, _text_state(b, f)))
    page = next((i for i, p in enumerate(props["captions"]) if p["startFrame"] <= f < p["endFrame"]), None)
    if page is not None:
        local = f - props["captions"][page]["startFrame"]
        lit: tuple = ()
        if (props.get("brand") or {}).get("captionStyle") == "highlight":  # the word being spoken is in the accent colour
            lit = tuple(i for i, w in enumerate(props["captions"][page]["words"]) if w["startFrame"] <= f < w["endFrame"])
        state.append(("caption", page, local if local < CAPTION_IN else "static", lit))
    hook = props.get("hook")
    if hook and hook["startFrame"] <= f < hook["endFrame"]:
        state.append(("hook", _text_state(hook, f, enter=hook["startFrame"] > 0)))
    card = props.get("endCard")
    if card and card["startFrame"] <= f < card["endFrame"]:
        state.append(("endCard", _text_state(card, f)))
    return tuple(state)


def spans(props: dict) -> list[tuple[int, int]]:
    """[(first frame, frames)] of runs where the overlay layer doesn't change; each is drawn once, from its first frame."""
    chrome = chrome_frames(props)[:2]
    out: list[tuple[int, int]] = []
    prev = None
    for f in range(int(props["totalFrames"])):
        st = frame_state(props, f, chrome)
        if out and st == prev:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((f, 1))
        prev = st
    return out


def concat_list(spans_: list[tuple[int, int]], fps: int, blank: set[int] | None = None) -> str:
    """ffconcat list showing each span's still for its frames (the last file repeated so its duration counts).
    Spans starting in `blank` (Chrome draws those frames itself) show a transparent still."""
    # `option framerate`: a still's timestamps are otherwise counted in 1/25 s, which moves text changes by a frame
    blank = blank or set()

    def name(start: int) -> str:
        return "stills/blank.png" if start in blank else f"stills/{start}.png"

    lines = ["ffconcat version 1.0"]
    for start, n in spans_:
        lines += [f"file '{name(start)}'", f"option framerate {fps}", f"duration {n / fps:.6f}"]
    lines += [f"file '{name(spans_[-1][0])}'", f"option framerate {fps}"]
    return "\n".join(lines) + "\n"


def timeline(props: dict) -> list[dict]:
    """Where each staged clip sits, in reel frames: a segment shows until the next one starts (a crossfade pulls it in
    early; Chrome draws those frames), video cutaways sit over their segment. Photo segments are `src: None` (black
    underneath: Chrome draws the frames) and photo cutaways are left out for the same reason."""
    out = []
    starts = seg_starts(props)
    for i, (s, at) in enumerate(zip(props["segments"], starts)):
        shown = (starts[i + 1] - at) if i + 1 < len(starts) else s["durationInFrames"]
        seg = {"src": s["src"] if s["kind"] == "video" else None, "start": at, "frames": max(0, shown), "cutaway": False}
        if s.get("blur") and s["kind"] == "video":
            seg["blur"] = s["blur"]
        out.append(seg)
        for c in s.get("cutaways") or []:
            if c["kind"] == "video":
                out.append({"src": c["src"], "start": at + c["startFrame"], "frames": c["durationInFrames"], "cutaway": True})
    return out


def _piecewise(keys: list, col: int) -> str:
    """ffmpeg expression in N: column `col` of the keys [[frame, ...], ...], linear between keys, held after."""
    vals = [k[col] for k in keys]
    if len(set(vals)) == 1:
        return repr(float(vals[0]))
    e = repr(float(vals[-1]))
    for i in range(len(keys) - 2, -1, -1):
        (a, *_), (b, *_) = keys[i], keys[i + 1]
        v0, v1 = vals[i], vals[i + 1]
        e = f"if(lt(N,{b}),{v0!r}+({v1!r}-({v0!r}))*(N-{a})/{max(1, b - a)},{e})"
    return e


def blur_filters(label: str, patches: list[dict], frames: int, w: int, h: int, fps: int, tag: str) -> list[str]:
    """Filter-graph parts that draw a segment's blur patches on `[label]` (frames from 0), ending in `[<tag>]`.
    Same look as SegmentView: a blurred copy of the clip, cut to a soft-edged ellipse (opaque to 78% of its radius,
    clear at 100%) that moves along the keys, shown from the first key to the last."""
    n = len(patches)
    parts = [f"[{label}]split={n + 1}[{tag}m]" + "".join(f"[{tag}b{j}]" for j in range(n))]
    last = f"{tag}m"
    mw, mh = max(2, w // 10), max(2, h // 10)  # the mask is smooth, so it is computed small and scaled up
    for j, b in enumerate(patches):
        keys, sigma = b["keys"], float(b["strength"])
        cx, cy, bw, bh = (_piecewise(keys, c) for c in (1, 2, 3, 4))
        d = f"hypot(((X+0.5)/W-({cx}))/(({bw})/2),((Y+0.5)/H-({cy}))/(({bh})/2))"
        parts.append(f"[{tag}b{j}]scale=iw/2:ih/2,gblur=sigma={sigma / 2:g},scale={w}:{h}:flags=bicubic,format=yuv420p[{tag}x{j}]")
        parts.append(f"color=c=black:s={mw}x{mh}:r={fps},format=gray,geq=lum='255*clip((1-{d})/0.22,0,1)',"
                     f"trim=end_frame={frames},scale={w}:{h}:flags=bicubic[{tag}k{j}]")  # fmt: skip
        parts.append(f"[{tag}x{j}][{tag}k{j}]alphamerge[{tag}y{j}]")
        nxt = f"{tag}n{j}" if j < n - 1 else tag
        parts.append(f"[{last}][{tag}y{j}]overlay=format=auto:eof_action=pass:"
                     f"enable='between(n,{keys[0][0]},{keys[-1][0]})'[{nxt}]")  # fmt: skip
        last = nxt
    return parts


def compose_cmd(props: dict, staging: Path, audio: Path, out: Path) -> list[Any]:
    fps, total = int(props["fps"]), int(props["totalFrames"])
    clips = timeline(props)
    cmd: list[Any] = ["ffmpeg", "-v", "error", "-y"]
    inputs = [c for c in clips if c["src"]]
    for c in inputs:
        cmd += ["-i", staging / c["src"]]
    index = {id(c): n for n, c in enumerate(inputs)}
    k_text, k_audio = len(inputs), len(inputs) + 1
    cmd += ["-f", "concat", "-safe", "0", "-i", staging / "overlay.ffconcat", "-i", audio]
    pieces = chunks(props)
    for c in pieces:  # Chrome's frames: one PNG sequence per chunk, so each decodes on its own thread
        cmd += ["-framerate", str(fps), "-start_number", str(c["start"]), "-i", staging / "stills" / c["dir"] / "%d.png"]
    parts, segs = [], [i for i, c in enumerate(clips) if not c["cutaway"]]
    for i in segs:
        c = clips[i]
        if c["src"]:
            # punch-in clips are staged larger than the frame; Chrome shows them covering it, centred (a no-op for the rest)
            w, h = props["width"], props["height"]
            piece = f"s{i}p" if c.get("blur") else f"s{i}"
            parts.append(f"[{index[id(c)]}:v]trim=end_frame={c['frames']},setpts=PTS-STARTPTS,"
                         f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}[{piece}]")  # fmt: skip
            if c.get("blur"):
                parts += blur_filters(piece, c["blur"], c["frames"], w, h, fps, f"s{i}")
        else:  # a photo: Chrome draws these frames over black
            parts.append(f"color=c=black:s={props['width']}x{props['height']}:r={fps},trim=end_frame={c['frames']},"
                         f"setpts=PTS-STARTPTS[s{i}]")  # fmt: skip
    parts.append("".join(f"[s{i}]" for i in segs) + f"concat=n={len(segs)}:v=1:a=0,fps={fps}[base0]")
    last = "base0"
    for i, c in enumerate(clips):
        if not c["cutaway"]:
            continue
        a, b = c["start"], c["start"] + c["frames"]
        parts.append(f"[{index[id(c)]}:v]trim=end_frame={c['frames']},setpts=PTS-STARTPTS+{a}/({fps}*TB)[c{i}]")
        parts.append(f"[{last}][c{i}]overlay=eof_action=pass:enable='between(n,{a},{b - 1})'[base{i + 1}]")
        last = f"base{i + 1}"
    parts.append(f"[{k_text}:v]fps={fps},trim=end_frame={total},format=rgba[text]")
    parts.append(f"[{last}][text]overlay=format=auto:eof_action=repeat[t0]")
    last = "t0"
    for n, c in enumerate(pieces):
        k = k_audio + 1 + n
        a, b = c["start"], c["end"]
        parts.append(f"[{k}:v]format=rgba,setpts=PTS-STARTPTS+{a}/({fps}*TB)[k{n}]")
        parts.append(f"[{last}][k{n}]overlay=format=auto:eof_action=pass:enable='between(n,{a},{b - 1})'[t{n + 1}]")
        last = f"t{n + 1}"
    parts.append(f"[{last}]trim=end_frame={total},setsar=1,format=yuv420p[v]")
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", f"{k_audio}:a"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-af", "apad", "-c:a", "aac", "-b:a", "192k", "-t", f"{total / fps:.4f}", "-movflags", "+faststart", out]  # fmt: skip
    return cmd


def _blank_png(path: Path, w: int, h: int) -> None:
    """A fully transparent RGBA PNG (what the still stream shows while Chrome draws the frame itself)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    raw = zlib.compress((b"\x00" + bytes(4 * w)) * h, 1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def render(project: Project, props: dict, props_path: Path, staging: Path, out: Path, log_path: Path) -> None:
    """The fast path: sound + stills from Remotion, the picture from ffmpeg. Writes `out` (like the full render)."""
    require("node")
    script = project.remotion_dir / "scripts" / "overlay.mjs"
    if not (project.remotion_dir / "node_modules" / "@remotion" / "renderer").exists():
        raise ReelsError("Remotion isn't installed yet — run ./setup.sh (or: cd remotion && npm install)")
    fps = int(props["fps"])
    sp = spans(props)
    full, over, _ = chrome_frames(props)
    audio = staging / "audio.wav"
    job = {
        "props": str(props_path), "publicDir": str(staging), "audioOut": str(audio),
        "stillsDir": str(staging / "stills"),
        "frames": [s for s, _ in sp if s not in full and s not in over],  # text states: one transparent still each
        "chunks": chunks(props),  # PNG sequences of the frames Chrome draws whole ("full") or as the overlay layer ("over")
        "concurrency": project.cfg("render", "concurrency"),
        "browserExecutable": os.environ.get("REELS_BROWSER") or project.cfg("render", "browserExecutable"),
    }  # fmt: skip
    write_json(staging / "overlay-job.json", job)
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"fast path: {len(sp)} stills for {props['totalFrames']} frames ({len(full)} full, {len(over)} overlay)\n")
        lf.flush()
        proc = subprocess.run(["node", script, staging / "overlay-job.json"], cwd=project.remotion_dir,
                              stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)  # fmt: skip
    if proc.returncode != 0 or not audio.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        raise ReelsError(f"Remotion (stills / sound) failed (full log: {project.rel(log_path)}):\n{tail}")
    missing = [f for f in job["frames"] if not (staging / "stills" / f"{f}.png").exists()]
    if missing:
        raise ReelsError(f"Remotion didn't write the stills for frames {missing[:5]} (log: {project.rel(log_path)})")
    for c in chunks(props):
        absent = [f for f in range(c["start"], c["end"]) if not (staging / "stills" / c["dir"] / f"{f}.png").exists()]
        if absent:
            raise ReelsError(f"Remotion didn't write Chrome frames {absent[:5]} (log: {project.rel(log_path)})")
    _blank_png(staging / "stills" / "blank.png", int(props["width"]), int(props["height"]))
    blank = {s_ for s_, _ in sp if s_ in full or s_ in over}
    (staging / "overlay.ffconcat").write_text(concat_list(sp, fps, blank), encoding="utf-8")
    run(compose_cmd(props, staging, audio, out), cwd=staging)
