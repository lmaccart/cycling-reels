"""Fast render path for plain-footage reels.

The full render screenshots every frame in headless Chrome (Remotion). When a reel's picture is only
footage the CLI already staged — hard cuts, video cutaways — plus text (hook, text beats, speech captions,
end card), that's wasted work: the text holds still for most frames. Here Remotion instead renders
  * the sound (ReelAudio: the same clip audio, music and sound effects, no pictures), and
  * one transparent still of the text layer (ReelOverlay) per distinct text state, plus each frame of
    the short entrance/exit animations,
and ffmpeg joins the staged clips, lays the cutaways and the text stills over them, and adds the sound.
Anything else (punch-ins, blur, photos, crossfades, exhibits, stats, maps, progress bars, title boards,
highlight captions) goes through the full render.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .config import Project
from .media import require, run
from .util import ReelsError, write_json

# Frames of animation, mirroring the template (TikTokText.useEntrance, Captions).
TEXT_IN = 4  # hook / text beat / end card: pop in over 4 frames (the 4th is fully in)
TEXT_OUT = 4  # and fade out over the last 4
CAPTION_IN = 2  # captions: 0.4 -> 1 opacity over 2 frames


def why_not(props: dict) -> str | None:
    """None when the reel can take the fast path; otherwise what needs the full render."""
    if props.get("timeline"):
        return "explicit video tracks and gaps require the full layered composition"
    for i, s in enumerate(props["segments"]):
        if s["kind"] != "video":
            return f"segment {i + 1} is a photo"
        if i > 0 and s.get("transitionInFrames", 0) > 0:
            return f"segment {i + 1} crossfades in"
        if s.get("punch"):
            return f"segment {i + 1} has punch-ins"
        if s.get("blur"):
            return f"segment {i + 1} has blur patches"
        for c in s.get("cutaways") or []:
            if c["kind"] != "video":
                return f"segment {i + 1} has a photo cutaway"
    for key, what in (("stats", "stat pills"), ("routes", "a route map"), ("progress", "a progress bar"),
                      ("exhibits", "exhibits"), ("foregrounds", "exhibits"), ("titleCard", "a title board")):  # fmt: skip
        if props.get(key):
            return f"it has {what}"
    if props.get("safeArea"):
        return "the safe-area guide is on"
    if props["captions"] and (props.get("brand") or {}).get("captionStyle") == "highlight":
        return "highlight captions change colour word by word"
    return None


def _text_state(item: dict, f: int, enter: bool = True) -> Any:
    """'static' while a hook/beat/end card holds still, else the animation frame it's on."""
    local, dur = f - item["startFrame"], item["endFrame"] - item["startFrame"]
    if (enter and local < TEXT_IN) or local > dur - TEXT_OUT:
        return local
    return "static"


def frame_state(props: dict, f: int) -> tuple:
    """Everything that decides what the text layer looks like on frame `f`. Equal states draw identically."""
    state: list = []
    for i, b in enumerate(props["beats"]):
        if b["startFrame"] <= f < b["endFrame"]:
            state.append(("beat", i, _text_state(b, f)))
    page = next((i for i, p in enumerate(props["captions"]) if p["startFrame"] <= f < p["endFrame"]), None)
    if page is not None:
        local = f - props["captions"][page]["startFrame"]
        state.append(("caption", page, local if local < CAPTION_IN else "static"))
    hook = props.get("hook")
    if hook and hook["startFrame"] <= f < hook["endFrame"]:
        state.append(("hook", _text_state(hook, f, enter=hook["startFrame"] > 0)))
    card = props.get("endCard")
    if card and card["startFrame"] <= f < card["endFrame"]:
        state.append(("endCard", _text_state(card, f)))
    return tuple(state)


def spans(props: dict) -> list[tuple[int, int]]:
    """[(first frame, frames)] of runs where the text layer doesn't change; each is drawn once, from its first frame."""
    out: list[tuple[int, int]] = []
    prev = None
    for f in range(int(props["totalFrames"])):
        st = frame_state(props, f)
        if out and st == prev:
            out[-1] = (out[-1][0], out[-1][1] + 1)
        else:
            out.append((f, 1))
        prev = st
    return out


def concat_list(spans_: list[tuple[int, int]], fps: int) -> str:
    """ffconcat list showing each span's still for its frames (the last file repeated so its duration counts)."""
    # `option framerate`: a still's timestamps are otherwise counted in 1/25 s, which moves text changes by a frame
    lines = ["ffconcat version 1.0"]
    for start, n in spans_:
        lines += [f"file 'stills/{start}.png'", f"option framerate {fps}", f"duration {n / fps:.6f}"]
    lines += [f"file 'stills/{spans_[-1][0]}.png'", f"option framerate {fps}"]
    return "\n".join(lines) + "\n"


def timeline(props: dict) -> list[dict]:
    """Where each staged clip sits: segments back to back, and cutaways over their segment, in reel frames."""
    out, at = [], 0
    for s in props["segments"]:
        out.append({"src": s["src"], "start": at, "frames": s["durationInFrames"], "cutaway": False})
        for c in s.get("cutaways") or []:
            out.append({"src": c["src"], "start": at + c["startFrame"], "frames": c["durationInFrames"], "cutaway": True})
        at += s["durationInFrames"]
    return out


def compose_cmd(props: dict, staging: Path, audio: Path, out: Path) -> list[Any]:
    fps, total = int(props["fps"]), int(props["totalFrames"])
    clips = timeline(props)
    cmd: list[Any] = ["ffmpeg", "-v", "error", "-y"]
    for c in clips:
        cmd += ["-i", staging / c["src"]]
    k_text, k_audio = len(clips), len(clips) + 1
    cmd += ["-f", "concat", "-safe", "0", "-i", staging / "overlay.ffconcat", "-i", audio]
    parts, segs = [], [i for i, c in enumerate(clips) if not c["cutaway"]]
    for i in segs:
        parts.append(f"[{i}:v]trim=end_frame={clips[i]['frames']},setpts=PTS-STARTPTS[s{i}]")
    parts.append("".join(f"[s{i}]" for i in segs) + f"concat=n={len(segs)}:v=1:a=0,fps={fps}[base0]")
    last = "base0"
    for i, c in enumerate(clips):
        if not c["cutaway"]:
            continue
        a, b = c["start"], c["start"] + c["frames"]
        parts.append(f"[{i}:v]trim=end_frame={c['frames']},setpts=PTS-STARTPTS+{a}/({fps}*TB)[c{i}]")
        parts.append(f"[{last}][c{i}]overlay=eof_action=pass:enable='between(n,{a},{b - 1})'[base{i + 1}]")
        last = f"base{i + 1}"
    parts.append(f"[{k_text}:v]fps={fps},trim=end_frame={total},format=rgba[text]")
    parts.append(f"[{last}][text]overlay=format=auto:eof_action=repeat,trim=end_frame={total},"
                 f"setsar=1,format=yuv420p[v]")  # fmt: skip
    cmd += ["-filter_complex", ";".join(parts), "-map", "[v]", "-map", f"{k_audio}:a"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "16", "-pix_fmt", "yuv420p",
            "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
            "-af", "apad", "-c:a", "aac", "-b:a", "192k", "-t", f"{total / fps:.4f}", "-movflags", "+faststart", out]  # fmt: skip
    return cmd


def render(project: Project, props: dict, props_path: Path, staging: Path, out: Path, log_path: Path) -> None:
    """The fast path: sound + text stills from Remotion, the picture from ffmpeg. Writes `out` (like the full render)."""
    require("node")
    script = project.remotion_dir / "scripts" / "overlay.mjs"
    if not (project.remotion_dir / "node_modules" / "@remotion" / "renderer").exists():
        raise ReelsError("Remotion isn't installed yet — run ./setup.sh (or: cd remotion && npm install)")
    fps = int(props["fps"])
    sp = spans(props)
    audio = staging / "audio.wav"
    job = {
        "props": str(props_path), "publicDir": str(staging), "audioOut": str(audio),
        "stillsDir": str(staging / "stills"), "frames": [s for s, _ in sp],
        "concurrency": project.cfg("render", "concurrency"),
        "browserExecutable": os.environ.get("REELS_BROWSER") or project.cfg("render", "browserExecutable"),
    }  # fmt: skip
    write_json(staging / "overlay-job.json", job)
    with open(log_path, "w", encoding="utf-8") as lf:
        lf.write(f"fast path: {len(sp)} text stills for {props['totalFrames']} frames\n")
        lf.flush()
        proc = subprocess.run(["node", script, staging / "overlay-job.json"], cwd=project.remotion_dir,
                              stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)  # fmt: skip
    if proc.returncode != 0 or not audio.exists():
        tail = log_path.read_text(encoding="utf-8", errors="replace")[-3000:]
        raise ReelsError(f"Remotion (text stills / sound) failed (full log: {project.rel(log_path)}):\n{tail}")
    missing = [f for f, _ in sp if not (staging / "stills" / f"{f}.png").exists()]
    if missing:
        raise ReelsError(f"Remotion didn't write the text stills for frames {missing[:5]} (log: {project.rel(log_path)})")
    (staging / "overlay.ffconcat").write_text(concat_list(sp, fps), encoding="utf-8")
    run(compose_cmd(props, staging, audio, out), cwd=staging)
