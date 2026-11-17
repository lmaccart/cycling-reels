"""Real-footage v360/Metal stage parity. Temporary plan/media only; originals read-only."""
from __future__ import annotations

from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import json
import os
import sys
from pathlib import Path
import tempfile
import time

from ..config import Project
from ..media import ffprobe_json, run as run_tool
from ..util import ReelsError
from .bench_sphere_keys import _private_project, _psnr, piece


@contextmanager
def backend(name):
    old = os.environ.get("REELS_SPHERE_BACKEND")
    os.environ["REELS_SPHERE_BACKEND"] = name
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("REELS_SPHERE_BACKEND", None)
        else:
            os.environ["REELS_SPHERE_BACKEND"] = old


def compare(project, sg, fps: int, work: Path, samples: list[int]) -> dict:
    from ..render import stage_video

    src = project.abs(sg.asset["media"])
    report = {"source": src.name, "sourceBytes": src.stat().st_size,
              "sourceIn": sg.spec["in"], "sourceOut": sg.spec["out"], "speed": sg.spec["speed"],
              "frames": sg.frames, "fps": fps, "runs": {}, "samples": []}
    paths = {}
    timestamps = {}
    for name in ("v360", "metal"):
        path = work / f"{name}.mp4"
        paths[name] = path
        start = time.perf_counter()
        with backend(name):  # metal is mandatory; no both-sides fallback can pass.
            stage_video(project, sg, path, fps, audio=False, grade=None)
        elapsed = time.perf_counter() - start
        info = ffprobe_json(path)["streams"][0]
        report["runs"][name] = {"wallSeconds": elapsed, "fps": sg.frames / elapsed,
            "encoder": "h264_videotoolbox" if name == "metal" else "libx264 veryfast CRF16",
            "outputBytes": path.stat().st_size, "dimensions": [info["width"], info["height"]],
            "pixelFormat": info["pix_fmt"], "colour": {k: info.get(k) for k in ("color_range", "color_space", "color_transfer", "color_primaries")}}
        raw = run_tool(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "frame=pts_time", "-of", "json", path]).stdout
        timestamps[name] = [float(f["pts_time"]) for f in json.loads(raw)["frames"]]
    if report["runs"]["v360"]["dimensions"] != report["runs"]["metal"]["dimensions"]:
        raise ReelsError("Metal/v360 dimensions differ")
    expected = [n / fps for n in range(sg.frames)]
    for name, pts in timestamps.items():
        if len(pts) != len(expected) or any(abs(a-b) > 1e-6 for a, b in zip(pts, expected)):
            raise ReelsError(f"{name} frame timestamps/count differ from the staging contract")
    report["matchedTimestamps"] = True
    for n in sorted(set(samples)):
        if not 0 <= n < sg.frames:
            continue
        frames = []
        for name in ("v360", "metal"):
            frame = work / f"{name}-{n}.y4m"
            run_tool(["ffmpeg", "-v", "error", "-y", "-i", paths[name], "-vf", f"select=eq(n\\,{n})",
                      "-fps_mode", "passthrough", "-frames:v", 1, "-pix_fmt", "yuv420p", frame])
            frames.append(frame)
        y, avg = _psnr(*frames)
        report["samples"].append({"frame": n, "seconds": n / fps, "psnrY": y, "psnr": avg})
    report["pass"] = bool(report["samples"]) and min(row["psnr"] for row in report["samples"]) >= 35
    report["speedup"] = report["runs"]["v360"]["wallSeconds"] / report["runs"]["metal"]["wallSeconds"]
    return report


def benchmark(root: Path, plan_path: Path, segment: int = 1, seconds: float = 30, scale: float = 1) -> dict:
    from ..render import build

    original = json.loads(plan_path.read_text())
    segments = original.get("segments") or []
    if not 1 <= segment <= len(segments):
        raise ReelsError("segment index is outside the supplied plan")
    with tempfile.TemporaryDirectory(prefix="sphere-metal-bench-") as temporary:
        work = Path(temporary)
        project = _private_project(root, work / "project", str(segments[segment-1]["asset"]), scale)
        from ..library import load_asset, resolve_id
        asset = load_asset(project, resolve_id(project, str(segments[segment-1]["asset"])))
        seg = piece(deepcopy(segments[segment-1]), 0, seconds, asset=asset)
        seg["stabilize"] = 0.73  # explicit numeric stabilization; never changes the user's plan
        plan = {"slug": "temporary-sphere-metal-benchmark", "segments": [seg], "autoSfx": False}
        copy = work / "plan.json"
        copy.write_text(json.dumps(plan))
        built = build(project, json.loads(copy.read_text()))
        sg, fps = built["segs"][0], built["fps"]
        samples = [0, fps, 5*fps, 10*fps, round(10.5*fps)-1, round(10.5*fps), round(10.5*fps)+1,
                   15*fps, 20*fps, 25*fps, sg.frames-1]
        result = compare(project, sg, fps, work, samples)
        result["stabilizationSeconds"] = 0.73
        result["temporaryMediaRemoved"] = True  # context exits before caller gets the result
        return result


def command(project: Project, args) -> int:
    root = Path(args.root).expanduser().resolve() if args.root else project.root
    with redirect_stdout(sys.stderr):
        report = benchmark(root, (root / args.plan).resolve(), args.segment, args.seconds, args.scale)
    text = json.dumps(report, indent=2)
    if args.report:
        Path(args.report).write_text(text + "\n")
    print(text)
    return 0 if report["pass"] else 1
