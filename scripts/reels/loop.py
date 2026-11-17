from __future__ import annotations

import re
import tempfile
from dataclasses import replace
from pathlib import Path
from .config import Project
from .media import run

# Seamless loops: Reels replay on their own, so a reel whose last frame flows into its
# first keeps people watching. The cleanest seam is the "swap halves" edit: start mid-shot
# and end with the lead-in to that same shot (last segment's out == first segment's in).

SEAMLESS_DROP = 0.04  # SSIM: a seam within this of the shot's own frame-to-frame step reads as seamless
CLOSE_SSIM = 0.6  # above this the jump is small (same kind of shot, similar framing)
_TOL = 0.02


def check_loop(plan: dict, errors: list[str]) -> bool:
    raw = plan.get("loop", False)
    if not isinstance(raw, bool):
        errors.append("loop must be true or false")
        return False
    return raw


def _view_at_end(sg, fps: int) -> dict | None:
    from .sphere import path_value

    sph = sg.spec.get("sphere")
    if not sph:
        return None
    return path_value(sph["path"], (sg.frames - 1) / fps, sph["projection"]) if sph["path"] else sph["view"]


def _view_at_start(sg) -> dict | None:
    sph = sg.spec.get("sphere")
    if not sph:
        return None
    return sph["path"][0] if sph["path"] else sph["view"]


def swap_halves(segs: list, fps: int) -> tuple[bool, str]:
    """(True, how) when the last segment runs straight into the first: same clip, its `out` at the
    first segment's `in`, same framing. Otherwise (False, why not) — empty when it's a different clip."""
    first, last = segs[0], segs[-1]
    if len(segs) < 2 or first.asset["id"] != last.asset["id"] or first.kind != "video":
        return False, ""
    a, b = first.spec, last.spec
    step = a["speed"] / fps
    if abs(b["out"] - a["in"]) > 1.5 * step:
        return False, f"segment {len(segs)} ends at {b['out']:.2f}s but segment 1 starts at {a['in']:.2f}s — set its out to {a['in']:g}"
    if abs(a["speed"] - b["speed"]) > _TOL:
        return False, f"the two halves play at different speeds ({b['speed']:g}× → {a['speed']:g}×)"
    va, vb = _view_at_start(first), _view_at_end(last, fps)
    if va or vb:
        if not (va and vb) or (a["sphere"]["projection"] != b["sphere"]["projection"]):
            return False, "the two halves use different 360 projections"
        dyaw = abs((va["yaw"] - vb["yaw"] + 180) % 360 - 180)
        if dyaw > 1 or abs(va["pitch"] - vb["pitch"]) > 1 or abs(va["fov"] - vb["fov"]) > 1:
            return False, (f"the 360 view changes at the seam (yaw {vb['yaw']:g}→{va['yaw']:g}, pitch {vb['pitch']:g}→{va['pitch']:g}, "
                           f"fov {vb['fov']:g}→{va['fov']:g}) — end the last segment on the view the first one starts with")  # fmt: skip
    else:
        for k in ("frame", "zoom", "focusX", "focusY"):
            if a[k] != b[k] and not (isinstance(a[k], float) and abs(a[k] - b[k]) <= _TOL):
                return False, f"the framing changes at the seam ({k} {b[k]} → {a[k]}) — use the same {k} on both halves"
    return True, f"swap halves: segment {len(segs)} ends at {b['out']:.2f}s, where segment 1 starts"


def ssim(a: Path, b: Path) -> float | None:
    """Structural similarity of two frames (grey, 270x480) — 1 = identical."""
    graph = "[0:v]scale=270:480,format=gray[a];[1:v]scale=270:480,format=gray[b];[a][b]ssim"
    proc = run(["ffmpeg", "-hide_banner", "-i", a, "-i", b, "-lavfi", graph, "-f", "null", "-"], check=False)
    m = re.search(r"All:([0-9.]+)", proc.stderr or "")
    return float(m.group(1)) if m else None


def frame_of(video: Path, n: int, out: Path) -> Path:
    run(["ffmpeg", "-v", "error", "-y", "-i", video, "-vf", f"select=eq(n\\,{n})", "-fps_mode", "passthrough", "-frames:v", "1", out])
    return out


def seam_frames(project: Project, segs: list, fps: int, folder: Path) -> tuple[Path, Path, Path] | None:
    """Stages the last two frames of the last segment and the first frame of the first, footage only
    (no text), at the planned times and framing. None when either end is a photo."""
    from .render import stage_video
    from .sphere import path_value

    first, last = segs[0], segs[-1]
    if first.kind != "video" or last.kind != "video":
        return None
    two = 2 * last.spec["speed"] / fps
    head = replace(first, spec=dict(first.spec, out=first.spec["in"] + two), frames=2)
    tail_spec = dict(last.spec, **{"in": max(0.0, last.spec["out"] - two)})
    sph = last.spec.get("sphere")
    if sph and sph["path"]:  # hold the view the camera move ends on
        tail_spec["sphere"] = dict(sph, view=path_value(sph["path"], (last.frames - 1) / fps, sph["projection"]), path=None)
    tail = replace(last, spec=tail_spec, frames=2)
    stage_video(project, head, folder / "seam-head.mp4", fps)
    stage_video(project, tail, folder / "seam-tail.mp4", fps)
    return (frame_of(folder / "seam-tail.mp4", 0, folder / "tail0.png"), frame_of(folder / "seam-tail.mp4", 1, folder / "tail1.png"),
            frame_of(folder / "seam-head.mp4", 0, folder / "head0.png"))  # fmt: skip


def staged_seam(staging: Path, segs: list) -> tuple[Path, Path, Path] | None:
    """The same three frames from a render's staged clips."""
    first, last = segs[0], segs[-1]
    a, b = staging / f"seg{first.index}.mp4", staging / f"seg{last.index}.mp4"
    if first.kind != "video" or last.kind != "video" or not a.exists() or not b.exists():
        return None
    return (frame_of(b, last.frames - 2, staging / "tail0.png"), frame_of(b, last.frames - 1, staging / "tail1.png"),
            frame_of(a, 0, staging / "head0.png"))  # fmt: skip


def seam_score(frames: tuple[Path, Path, Path] | None) -> dict | None:
    """{"seam": SSIM last→first frame, "step": SSIM of the last shot's own last frame step}."""
    if not frames:
        return None
    t0, t1, h0 = frames
    seam, step = ssim(t1, h0), ssim(t0, t1)
    if seam is None or step is None:
        return None
    return {"seam": round(seam, 3), "step": round(step, 3)}


def describe_seam(score: dict | None, swap: tuple[bool, str]) -> tuple[str, list[str]]:
    """One line for check/render, plus advice when the seam shows."""
    ok, how = swap
    if score is None:
        return ("seamless (" + how + ")" if ok else "not measured (the first or last segment is a photo)"), []
    txt = f"SSIM {score['seam']:.2f} last → first frame (a normal frame step in that shot: {score['step']:.2f})"
    if ok:
        return f"seamless ({how}) · {txt}", []
    if score["seam"] >= score["step"] - SEAMLESS_DROP:
        return f"seamless · {txt}", []
    advice = [
        "loop seam: the last frame jumps to the first"
        + (" (small jump — same kind of shot)" if score["seam"] >= CLOSE_SSIM else " (a visible cut)")
        + ". For a seamless loop, use the swap-halves edit: start mid-shot and end the reel with that shot's lead-in "
        "(last segment = same clip, out = segment 1's in, same view/crop)"
    ]
    if how:
        advice.append(f"loop seam: same clip at both ends, but {how}")
    return txt, advice


def check_seam(project: Project, segs: list, fps: int) -> tuple[dict | None, tuple[bool, str]]:
    swap = swap_halves(segs, fps)
    if swap[0]:
        return None, swap
    with tempfile.TemporaryDirectory(prefix="reels-seam-") as td:
        return seam_score(seam_frames(project, segs, fps, Path(td))), swap

