"""Checks that a staged 360 clip follows its camera keyframes.

Stages one 360 segment of a plan through `render.stage_video` (the function the
editor's fast preview and the final render both stage clips with), grabs frames
at the keyframe times and halfway between them, and compares each (PSNR) with a
single-frame v360 of the view `sphere.path_value` gives for that frame. A frame
that spun the wrong way, or ignored the keys, scores far below a matching one.
Midpoints also get the old per-component (linear yaw/pitch/roll) view, so the
report shows where the shortest-arc path differs from it.

The plan file is only read. Everything is written to a private temp workspace
(config and the one asset record copied in, the plan kept in memory under a
temporary slug; originals read in place, never written) and the report's
frames stay in --out.

    PYTHONPATH=scripts python -m reels.editor.bench_sphere_keys \\
        --root ~/rice/cycling --plan plans/2026-11-16-sprint-zone-nov-26.json --from 7.6 --to 13.6
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != _HERE]  # editor/http.py shadows the stdlib's http
sys.path.insert(0, str(_HERE.parents[1]))

from reels.config import Project  # noqa: E402
from reels.util import ReelsError  # noqa: E402

PASS_DB = 35.0  # libx264 crf 16 against a lossless reference sits well above this; a wrong view sits far below
CAMERA_FIELDS = ("asset", "in", "out", "speed", "moment", "view", "path", "cameraWindow", "projection",
                 "stabilize", "yaw", "pitch", "fov", "roll")


def piece(segment: dict, start: float | None, end: float | None, *, asset: dict) -> dict:
    """The segment's camera fields, cut to [start, end) local seconds the way a timeline
    split does: the piece keeps the whole move in its `cameraWindow`."""
    from reels.editor.timing import _slice_keys

    seg = {k: deepcopy(segment[k]) for k in CAMERA_FIELDS if k in segment}
    if start is None and end is None:
        return seg
    if "moment" in seg:
        from reels.library import resolve_moment

        moment = resolve_moment(asset, seg["moment"])
        seg.setdefault("in", moment["in"])
        seg.setdefault("out", moment["out"])
    speed = float(seg.get("speed") or 1.0)
    source_in, source_out = float(seg["in"]), float(seg["out"])
    seconds = (source_out - source_in) / speed
    a = max(0.0, start or 0.0)
    b = min(seconds, end if end is not None else seconds)
    if b - a < 0.2:
        raise ReelsError(f"--from/--to leave {b - a:.2f}s of a {seconds:.2f}s segment")
    seg["in"], seg["out"] = source_in + a * speed, source_in + b * speed
    window = seg.get("cameraWindow")
    if not window and isinstance(seg.get("path"), list):
        window = {"offsetSeconds": 0.0, "durationSeconds": seconds, "path": deepcopy(seg["path"])}
    if window:
        seg["cameraWindow"] = {**window, "offsetSeconds": float(window["offsetSeconds"]) + a}
        seg["path"] = _slice_keys(window["path"], float(window["offsetSeconds"]) + a,
                                  float(window["offsetSeconds"]) + b, rebase=True)
    return seg


def _linear(keys: list[dict], t: float) -> dict:
    """The per-component blend path_value used before slerp (yaw the short way, pitch/roll linear)."""
    from reels.sphere import _ease, wrap_yaw

    if t <= keys[0]["at"]:
        return dict(keys[0])
    for a, b in zip(keys, keys[1:]):
        if b.get("cut"):
            if t < b["at"]:
                return dict(a)
            continue
        if t <= b["at"]:
            e = _ease((t - a["at"]) / max(1e-6, b["at"] - a["at"]))
            return {"yaw": wrap_yaw(a["yaw"] + wrap_yaw(b["yaw"] - a["yaw"]) * e),
                    **{k: a[k] + (b[k] - a[k]) * e for k in ("pitch", "fov", "roll")}}
    return dict(keys[-1])


def _frame_params(sphere: dict, stab: list | None, view: dict, n: int, w: int, h: int) -> dict:
    """v360 parameters for frame n, exactly as `sphere.camera_filter` builds them."""
    from reels.sphere import view_params
    from reels.stabilize import mmul, v360_angles, v360_matrix

    p = view_params(view, sphere["projection"], w, h)
    if stab:
        a = stab[min(n, len(stab) - 1)]
        p["yaw"], p["pitch"], p["roll"] = v360_angles(mmul(a, v360_matrix(p["yaw"], p["pitch"], p["roll"])))
    return p


def _ffmpeg(args: list[Any]) -> str:
    """ffmpeg as the renderer runs it (config tools.ffmpeg / ffmpeg-full when installed); returns stderr."""
    from reels.media import run as run_tool

    return run_tool(["ffmpeg", "-hide_banner", *args]).stderr


def _psnr(a: Path, b: Path) -> tuple[float, float]:
    """(Y, average) PSNR in dB of two same-size yuv420p frames."""
    log = _ffmpeg(["-i", a, "-i", b, "-lavfi", "psnr", "-f", "null", "-"])
    found = re.search(r"PSNR y:(\S+) u:\S+ v:\S+ average:(\S+)", log)
    if not found:
        raise ReelsError(f"no PSNR in ffmpeg output: {log[-300:]}")
    return tuple(float("inf") if v == "inf" else float(v) for v in found.groups())  # type: ignore[return-value]


def run(project: Project, plan: dict, segment: int, out_dir: Path, *,
        start: float | None = None, end: float | None = None) -> dict:
    """Stages the piece, measures every sample and returns the report."""
    from reels import render
    from reels.grade import source_colour, to_bt709
    from reels.sphere import path_value, source_graph, v360_filter
    from reels.library import load_asset, resolve_id

    segments = plan.get("segments") or []
    if not 0 <= segment < len(segments):
        raise ReelsError(f"the plan has no segment {segment + 1}")
    asset = load_asset(project, resolve_id(project, str(segments[segment].get("asset", ""))))
    bench_plan = {"slug": f"bench-sphere-{uuid.uuid4().hex[:8]}", "autoSfx": False,
                  "segments": [piece(segments[segment], start, end, asset=asset)]}
    built = render.build(project, bench_plan)
    sg, fps = built["segs"][0], int(built["fps"])
    sphere = sg.spec.get("sphere")
    if sg.kind != "video" or not sphere:
        raise ReelsError(f"segment {segment + 1} isn't 360 video")
    keys = sphere.get("path") or []
    out_dir = out_dir.expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    staged = out_dir / "staged.mp4"
    started = time.perf_counter()
    render.stage_video(project, sg, staged, fps, audio=False, grade=None)
    stage_seconds = time.perf_counter() - started

    tw, th = int(project.cfg("render", "width")), int(project.cfg("render", "height"))
    src = project.abs(sg.asset["media"])
    probe, spec = sg.asset["probe"], sg.spec
    speed = spec["speed"]
    stab = None
    if sphere.get("stabilize"):
        from reels.stabilize import segment_corrections

        stab = segment_corrections(src, spec["in"], speed, fps, sg.frames, sphere["stabilize"])
    src_graph, pad, inproj = source_graph(probe)
    tail = f"setsar=1,{to_bt709(source_colour(probe, src))}"

    def reference(view: dict, n: int, out: Path) -> None:
        """One frame, with a fixed v360 at this frame's view (same decode, timing and colour as staging)."""
        cam = v360_filter(inproj, sphere["projection"], _frame_params(sphere, stab, view, n, tw, th), tw, th)
        graph = f"{src_graph};[{pad}]setpts=(PTS-STARTPTS)/{speed:g},fps={fps},select=eq(n\\,{n}),{cam},{tail}[v]"
        _ffmpeg(["-v", "error", "-y", "-ss", f"{spec['in']:.12f}", "-t", f"{spec['out'] - spec['in']:.12f}", "-i", src,
                 "-filter_complex", graph, "-map", "[v]", "-frames:v", 1, "-pix_fmt", "yuv420p", out])

    last = sg.frames - 1
    samples: list[tuple[str, int]] = []
    inside = [k for k in keys if 0 <= k["at"] <= last / fps]
    for i, key in enumerate(inside):
        samples.append((f"key {i + 1}", min(last, round(key["at"] * fps))))
    for a, b in zip(keys, keys[1:]):
        mid = (a["at"] + b["at"]) / 2
        if 0 <= mid <= last / fps and not b.get("cut"):
            samples.append((f"mid {a['at']:g}-{b['at']:g}s", min(last, round(mid * fps))))
    if not samples:
        samples = [("start", 0), ("end", last)]
    samples.sort(key=lambda s: s[1])

    rows = []
    for label, n in samples:
        t = n / fps
        view = path_value(keys, t, sphere["projection"]) if keys else dict(sphere["view"])
        got, want = out_dir / f"f{n:05d}.staged.y4m", out_dir / f"f{n:05d}.path.y4m"
        _ffmpeg(["-v", "error", "-y", "-i", staged, "-vf", f"select=eq(n\\,{n})", "-fps_mode", "passthrough",
                 "-frames:v", 1, "-pix_fmt", "yuv420p", got])
        reference(view, n, want)
        y, avg = _psnr(got, want)
        row = {"label": label, "frame": n, "t": round(t, 4), "view": {k: round(view[k], 2) for k in ("yaw", "pitch", "roll", "fov")},
               "psnrY": round(y, 2), "psnr": round(avg, 2)}
        if keys and label.startswith("mid"):
            old = _linear(keys, t)
            linear = out_dir / f"f{n:05d}.linear.y4m"
            reference(old, n, linear)
            row["linearPsnr"] = round(_psnr(got, linear)[1], 2)
        for path in (got, want):
            _ffmpeg(["-v", "error", "-y", "-i", path, path.with_suffix(".png")])
        rows.append(row)
    worst = min(r["psnr"] for r in rows)
    return {"segment": segment + 1, "asset": sg.asset["id"], "in": round(spec["in"], 3), "out": round(spec["out"], 3),
            "frames": sg.frames, "fps": fps, "size": [tw, th], "keys": len(keys), "stabilize": sphere.get("stabilize"),
            "stageSeconds": round(stage_seconds, 2), "rows": rows, "minPsnr": worst, "pass": worst >= PASS_DB,
            "dir": str(out_dir)}


def print_report(report: dict) -> None:
    print(f"360 keyframe check — segment {report['segment']} ({report['asset']}), source {report['in']}–{report['out']} s, "
          f"{report['frames']} frames at {report['fps']} fps, {report['size'][0]}x{report['size'][1]}, "
          f"{report['keys']} keys, stabilize {report['stabilize'] or 'off'}")
    print(f"staged in {report['stageSeconds']} s; frames in {report['dir']}")
    print(f"{'sample':<22}{'frame':>6}{'t (s)':>9}  {'yaw':>8}{'pitch':>8}{'roll':>8}{'fov':>7}  {'PSNR Y':>7}{'PSNR':>7}{'old path':>9}")
    for r in report["rows"]:
        v = r["view"]
        old = f"{r['linearPsnr']:>9.2f}" if "linearPsnr" in r else f"{'':>9}"
        print(f"{r['label']:<22}{r['frame']:>6}{r['t']:>9.3f}  {v['yaw']:>8.2f}{v['pitch']:>8.2f}{v['roll']:>8.2f}{v['fov']:>7.2f}"
              f"  {r['psnrY']:>7.2f}{r['psnr']:>7.2f}{old}")
    print(f"min PSNR {report['minPsnr']:.2f} dB — {'PASS' if report['pass'] else 'FAIL'} (threshold {PASS_DB:g} dB). "
          "'old path' = the same frame aimed with the old per-component blend.")


def _private_project(root: Path, work: Path, asset_id: str, scale: float) -> Project:
    """A throwaway project holding copies of config.json and one asset record; media read in place."""
    (work / "library" / "assets").mkdir(parents=True)
    if (root / "config.json").is_file():
        config = json.loads((root / "config.json").read_text(encoding="utf-8"))
        shutil.copy2(root / "config.json", work / "config.json")
    else:
        config = {}
    record = root / "library" / "assets" / f"{asset_id}.json"
    if record.is_symlink() or not record.is_file():
        raise ReelsError(f"no library record for {asset_id}")
    shutil.copy2(record, work / "library" / "assets" / record.name)
    project = Project(work, code_root=Path(__file__).resolve().parents[3],
                      media_root=root / "library" / "media", media_read_only=True)
    if scale != 1:
        from reels.util import even

        render_cfg = config.get("render") or {}
        for key in ("width", "height"):
            project.config["render"][key] = even(int(render_cfg.get(key) or project.cfg("render", key)) * scale)
    return project


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="cycling-reels folder holding the plan and library")
    parser.add_argument("--plan", required=True, help="plan file, relative to --root (read only)")
    parser.add_argument("--segment", type=int, default=1, help="1-based segment number (default 1)")
    parser.add_argument("--from", dest="start", type=float, help="stage only from this many seconds into the segment")
    parser.add_argument("--to", dest="end", type=float, help="…to this many seconds into it")
    parser.add_argument("--scale", type=float, default=1.0, help="render size factor (default 1 = the reel's size)")
    parser.add_argument("--out", type=Path, help="where frames go (default: a new temp folder)")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    args = parser.parse_args(argv)
    root = args.root.expanduser().resolve()
    plan = json.loads((root / args.plan).read_text(encoding="utf-8"))
    segments = plan.get("segments") or []
    if not 1 <= args.segment <= len(segments):
        parser.error(f"the plan has {len(segments)} segments")
    out = args.out or Path(tempfile.mkdtemp(prefix="bench-sphere-keys-"))
    with tempfile.TemporaryDirectory(prefix="bench-sphere-project-") as work:
        project = _private_project(root, Path(work), str(segments[args.segment - 1].get("asset")), args.scale)
        report = run(project, plan, args.segment - 1, out, start=args.start, end=args.end)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print_report(report)
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
