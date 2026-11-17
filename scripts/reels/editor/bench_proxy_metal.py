"""Own-footage cold proxy comparison through the production worker sandbox."""
from __future__ import annotations

from contextlib import redirect_stdout
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

from ..config import Project
from ..library import load_asset
from ..media import ffprobe_json, run
from ..util import ReelsError
from .bench_sphere_metal import backend
from .preview import PreviewManager
from .worker import _stat_identity


class _MeasuredWorker:
    """Instrument only this benchmark's child; production has no baseline switch."""
    def __init__(self, software: bool, log: Path, seconds: float):
        self.software, self.log, self.seconds = software, log, seconds

    def run(self, args, **kwargs):
        args = list(args)
        if "--sandbox-probe" not in args:
            index = args.index("-c") + 1
            hook = f'''
import time
from reels.editor import proxy
_original_encoder = m.encode_proxy
_original_run = proxy._run_encoder
_measurements = []
def _measured_run(*args, **kwargs):
    started = time.perf_counter()
    try:
        return _original_run(*args, **kwargs)
    finally:
        _measurements.append({{"command": [str(x) for x in args[0]], "wallSeconds": time.perf_counter()-started}})
        Path({str(self.log)!r}).write_text(json.dumps(_measurements))
proxy._run_encoder = _measured_run
def _baseline_encoder(*args, **kwargs):
    # The production browsing job covers a whole original. This measurement
    # constrains that same pipeline to the approved first 30 seconds only.
    if not kwargs.get("inputs"):
        kwargs["inputs"] = ["-t", {str(self.seconds)!r}]
    old = proxy.sys.platform
    try:
        if {self.software!r}: proxy.sys.platform = "linux"
        return _original_encoder(*args, **kwargs)
    finally:
        proxy.sys.platform = old
m.encode_proxy = _baseline_encoder
'''
            args[index] = args[index].replace("raise SystemExit(m.main(sys.argv[3:]))", f"import json;exec({hook!r});raise SystemExit(m.main(sys.argv[3:]))")
        result = subprocess.run(args, capture_output=True, text=True, check=False, **kwargs)
        if result.returncode:
            raise ReelsError(f"benchmark worker failed ({result.returncode}): {result.stdout}\n{result.stderr}")
        return result


def _timestamps(path: Path, selector: str) -> list[dict]:
    result = run(["ffprobe", "-v", "error", "-select_streams", selector,
                  "-show_entries", "packet=pts_time,duration_time,size,flags", "-of", "json", path])
    return json.loads(result.stdout)["packets"]


def _samples(paths: dict[str, Path], frames: int, work: Path, width: int, height: int) -> list[dict]:
    selected = sorted({0, min(30, frames-1), frames//6, frames//3, frames//2, frames*2//3, frames*5//6, frames-1})
    extracted = {}
    for name, path in paths.items():
        directory = work / (name + "-samples")
        directory.mkdir(parents=True)
        expression = "+".join(f"eq(n\\,{n})" for n in selected)
        run(["ffmpeg", "-v", "error", "-i", path, "-vf", f"select='{expression}'",
             "-fps_mode", "passthrough", "-pix_fmt", "yuv420p", "-f", "image2", "-c:v", "rawvideo", directory / "%02d.yuv"])
        extracted[name] = directory
    # Raw frame comparisons avoid repeatedly decoding the 30-second files.
    import numpy as np

    def psnr(difference):
        mse = float(np.mean(difference**2))
        return float(10*np.log10(255**2/mse)) if mse else 999.0

    rows = []
    for i, n in enumerate(selected, 1):
        arrays = {name: np.fromfile(directory / f"{i:02d}.yuv", dtype=np.uint8).astype(np.float64)
                  for name, directory in extracted.items()}
        row = {"frame": n, "softwareVideotoolboxPSNR": psnr(arrays["software"] - arrays["videotoolbox"])}
        for reference in ("software", "videotoolbox"):
            difference = arrays[reference] - arrays["metal"]
            luma = difference[:width*height].reshape(height, width)
            row[reference + "PSNR"] = psnr(difference)
            row[reference + "LumaPSNR"] = psnr(luma)
            regions = ({"northPole": luma[:32], "southPole": luma[-32:],
                        "seam": np.concatenate((luma[:, :32], luma[:, -32:]), axis=1)}
                       if width == height*2 else
                       {"front": luma[:, :width//2], "back": luma[:, width//2:]})
            row[reference + "RegionsPSNR"] = {name: psnr(values) for name, values in regions.items()}
        rows.append(row)
    return rows


def benchmark(project: Project, asset_id: str, seconds: float = 30) -> dict:
    asset = load_asset(project, asset_id)
    source = project.abs(asset["media"]).resolve(strict=True)
    original_identity = _stat_identity(source)
    if not 0 < seconds <= min(180, float(asset["probe"]["duration"])):
        raise ReelsError("benchmark interval must fit the original and the 180-second aim limit")
    frames = round(seconds * 30)
    report = {"asset": asset["id"], "sourceBytes": source.stat().st_size,
              "start": 0, "end": seconds, "frames": frames, "fps": 30,
              "cacheConditions": "fresh media and CORI directories per worker; no telemetry cache; original OS page cache shared",
              "proxies": {}}
    # Keep derived media outside the read-only renderer tree, exactly as the
    # sandbox requires; no exception to its code-root write denial.
    with tempfile.TemporaryDirectory(prefix="proxy-metal-bench-") as td:
        root = Path(td).resolve()
        workspace = SimpleNamespace(root=root / "workspace", code_root=project.code_root,
                                    project=project, _originals={asset["id"]: {"path": str(source)}})
        workspace.root.mkdir()
        manager = PreviewManager(workspace)
        probe = {**deepcopy(asset["probe"]), "duration": seconds}
        for kind in ("source", "sphere"):
            paths, runs = {}, {}
            for name in ("software", "videotoolbox", "metal"):
                job_root = manager.worker_root / f"{kind}-{name}"
                job_root.mkdir()
                directory = manager.preview_root / f"{kind}-{name}"
                job = {"action": kind + "_proxy", "jobRoot": str(job_root),
                       "jobPath": str(job_root / "job.json"), "resultPath": str(job_root / "result.json"),
                       "revisionDir": str(directory), "sourcePath": str(source), "sourceIdentity": original_identity,
                       "assetId": asset["id"], "sourceKind": "video", "probe": probe,
                       "mediaRoot": str(source.parent), "workspaceRoot": str(workspace.root),
                       "view": {}, "angles": (asset.get("tags") or {}).get("angles") or {},
                       "start": 0, "end": seconds, "stabilize": "medium",
                       "revisions": {"renderer": "proxy-benchmark"}}
                measured = _MeasuredWorker(name == "software", job_root / "commands.json", seconds)
                started = time.perf_counter()
                with backend("metal" if name == "metal" else "v360"):
                    result = manager._run_worker(job, context=measured, code_root=project.code_root, timeout=1800)
                elapsed = time.perf_counter() - started
                if result.get("status") != "ready" or result.get("reused"):
                    raise ReelsError(f"{kind}/{name}: expected cold ready output: {result}")
                if name == "metal" and result.get("backend") != "metal":
                    raise ReelsError(f"native benchmark silently fell back: {result}")
                path = Path(result["path"])
                streams = ffprobe_json(path)["streams"]
                video = next(s for s in streams if s["codec_type"] == "video")
                timestamps = _timestamps(path, "v:0")
                expected_dimensions = [1440, 1280] if kind == "source" else [2048, 1024]
                if ([video["width"], video["height"]] != expected_dimensions or len(timestamps) != frames
                        or any(abs(float(p["pts_time"])-n/30) > 1e-6 for n, p in enumerate(timestamps))):
                    raise ReelsError(f"{kind}/{name}: dimensions/count/PTS differ: {video}, {len(timestamps)} packets")
                if runs and video.get("color_range") != runs["software"]["colour"]["color_range"]:
                    raise ReelsError(f"{kind}/{name}: output range differs from the existing proxy contract")
                keyframes = [n for n, p in enumerate(timestamps) if "K" in p.get("flags", "")]
                if video.get("has_b_frames") != 0 or not keyframes or keyframes[0] != 0 or any(
                        b-a > 10 for a, b in zip(keyframes, keyframes[1:] + [frames])):
                    raise ReelsError(f"{kind}/{name}: scrub GOP/B-frame contract differs")
                audio = _timestamps(path, "a:0")
                if bool(audio) != bool(probe.get("hasAudio")):
                    raise ReelsError(f"{kind}/{name}: missing/unexpected audio")
                audio_hash = (run(["ffmpeg", "-v", "error", "-i", path, "-map", "0:a:0",
                                   "-c:a", "pcm_s16le", "-f", "hash", "-hash", "sha256", "-"]).stdout.strip()
                              if audio else None)
                commands = json.loads(measured.log.read_text()) if measured.log.exists() else []
                runs[name] = {"wallSeconds": elapsed, "outputBytes": path.stat().st_size,
                              "backend": result.get("backend"), "encoder": result.get("encoder"),
                              "native": result.get("native"), "muxSeconds": result.get("muxSeconds"),
                              "encodeSeconds": result.get("encodeSeconds"), "commands": commands,
                              "dimensions": expected_dimensions, "videoFrames": len(timestamps),
                              "profile": video.get("profile"), "level": video.get("level"),
                              "pixelFormat": video["pix_fmt"], "colour": {k: video.get(k) for k in ("color_range", "color_space", "color_transfer", "color_primaries")},
                              "audioPackets": audio, "audioPCMHash": audio_hash, "keyframes": keyframes}
                paths[name] = path
            samples = _samples(paths, frames, root / kind, *expected_dimensions)
            audio_match = all(runs[name]["audioPackets"] == runs["software"]["audioPackets"] and
                              runs[name]["audioPCMHash"] == runs["software"]["audioPCMHash"]
                              for name in ("videotoolbox", "metal"))
            report["proxies"][kind] = {"runs": runs, "samples": samples, "matchedVideoPTS": True,
                                        "matchedAudioPackets": audio_match,
                                        "speedupOriginal": runs["software"]["wallSeconds"] / runs["metal"]["wallSeconds"],
                                        "speedupW5b": runs["videotoolbox"]["wallSeconds"] / runs["metal"]["wallSeconds"],
                                        "pass": audio_match and min(min(s["softwarePSNR"], s["videotoolboxPSNR"]) for s in samples) >= 35}
        if _stat_identity(source) != original_identity:
            raise ReelsError("original changed during benchmark")
    report["temporaryMediaRemoved"] = True
    report["pass"] = all(p["pass"] for p in report["proxies"].values())
    return report


def command(project: Project, args) -> int:
    with redirect_stdout(sys.stderr):
        report = benchmark(project, args.asset, args.seconds)
    text = json.dumps(report, indent=2)
    if args.report:
        Path(args.report).write_text(text + "\n")
    print(text)
    return 0 if report["pass"] else 1
