"""Own-footage cold proxy comparison through the production worker sandbox."""
from __future__ import annotations

from contextlib import ExitStack, redirect_stdout
from copy import deepcopy
import json
import hashlib
import math
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
from .proxy import _run_encoder, proxy_reservation
from .proxy_cache import budget_root, reserve_disk


def _selected_frames(frames: int) -> list[int]:
    return sorted({0, min(30, frames-1), frames//6, frames//3, frames//2, frames*2//3, frames*5//6, frames-1})


def _quality_gate(samples: list[dict]) -> dict:
    """Fixed, per-sample gates; +infinity is equality, NaN is never evidence."""
    raw_failures, common_failures = [], []
    for sample in samples:
        frame = sample["frame"]
        raw = float(sample["preEncodePSNR"]["average"])
        if math.isnan(raw) or raw < 35:
            raw_failures.append({"frame": frame, "plane": "average", "native": sample["preEncodePSNR"]["average"]})
        for plane in ("Y", "average"):
            native = float(sample["commonReferencePSNR"]["metal"][plane])
            baseline = float(sample["commonReferencePSNR"]["videotoolbox"][plane])
            # Avoid inf-inf (NaN): exact equality passes, finite vs +inf fails.
            valid = not (math.isnan(native) or math.isnan(baseline) or
                         native == -math.inf or baseline == -math.inf)
            passed = valid and (native == baseline or native == math.inf or
                                (math.isfinite(baseline) and native >= baseline - 0.10))
            if not passed:
                common_failures.append({"frame": frame, "plane": plane,
                                        "native": sample["commonReferencePSNR"]["metal"][plane],
                                        "W5b": sample["commonReferencePSNR"]["videotoolbox"][plane]})
    return {"preEncodeMinimumDB": 35, "commonReferenceMaximumDeficitDB": 0.10,
            "preEncodeFailures": raw_failures, "commonReferenceFailures": common_failures,
            "pass": bool(samples) and not raw_failures and not common_failures}


class _MeasuredWorker:
    """Instrument only this benchmark's child; production has no baseline switch."""
    def __init__(self, software: bool, log: Path, seconds: float, *, proof: bool = False):
        self.software, self.log, self.seconds, self.proof = software, log, seconds, proof

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
    command = list(map(str, args[0]))
    if "--check" in command:
        request_path = Path(command[-1])
        request = json.loads(request_path.read_text())
        if {self.proof!r}:
            request["proofSampleFrames"] = {_selected_frames(round(self.seconds * 30))!r}
            request_path.write_text(json.dumps(request, allow_nan=False))
        Path({str(self.log.with_suffix(".request.json"))!r}).write_text(json.dumps(request))
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


def _samples(paths: dict[str, Path], frames: int, work: Path, width: int, height: int,
             reference_command: list[str], proof_request: dict) -> tuple[list[dict], dict]:
    selected = _selected_frames(frames)
    extracted = {}
    expression = "+".join(f"eq(n\\,{n})" for n in selected)
    frame_bytes = width * height * 3 // 2
    # yuvj420p has the same planar byte layout but explicitly retains full-range
    # samples; asking ffmpeg for limited yuv420p could silently rescale them.
    pixel_format = "yuvj420p" if proof_request["fullRange"] else "yuv420p"
    # Replay the actual W5b input/trim/filter graph, stopping BEFORE its encoder.
    # One set of uncompressed frames is shared by every encoded comparison.
    reference = work / "common-reference"
    reference.mkdir(parents=True)
    first_map = reference_command.index("-map")
    command = reference_command[:first_map]
    graph_index = command.index("-filter_complex") + 1
    command[graph_index] += f";{reference_command[first_map+1]}select='{expression}'[proof]"
    command += ["-map", "[proof]", "-an", "-fps_mode", "passthrough", "-pix_fmt", pixel_format,
                "-c:v", "rawvideo", "-f", "image2", str(reference / "%02d.yuv")]
    _run_encoder(command, timeout=1800, max_output_bytes=frame_bytes + 1_000_000)
    for name, path in paths.items():
        directory = work / (name + "-samples")
        directory.mkdir(parents=True)
        _run_encoder(["ffmpeg", "-v", "error", "-i", path, "-vf", f"select='{expression}'",
                      "-fps_mode", "passthrough", "-pix_fmt", pixel_format, "-f", "image2",
                      "-c:v", "rawvideo", directory / "%02d.yuv"],
                     timeout=1800, max_output_bytes=frame_bytes + 1_000_000)
        extracted[name] = directory
    import numpy as np

    def read_frame(path):
        data = path.read_bytes()
        if len(data) != frame_bytes:
            raise ReelsError(f"wrong raw frame size: {path}: {len(data)} != {frame_bytes}")
        return data, np.frombuffer(data, dtype=np.uint8).astype(np.float64)

    def psnr(difference):
        mse = float(np.mean(difference**2))
        return float(10*np.log10(255**2/mse)) if mse else "inf"

    def metrics(difference):
        pixels = width * height
        luma = difference[:pixels].reshape(height, width)
        regions = ({"northPole": luma[:32], "southPole": luma[-32:],
                    "seam": np.concatenate((luma[:, :32], luma[:, -32:]), axis=1)}
                   if width == height*2 else
                   {"front": luma[:, :width//2], "back": luma[:, width//2:]})
        return {"average": psnr(difference), "Y": psnr(luma),
                "U": psnr(difference[pixels:pixels*5//4]), "V": psnr(difference[pixels*5//4:]),
                "regionsY": {name: psnr(values) for name, values in regions.items()},
                "maxComponentError": float(np.max(np.abs(difference)))}

    rows = []
    for i, n in enumerate(selected, 1):
        raw_bytes, raw = read_frame(reference / f"{i:02d}.yuv")
        arrays = {name: read_frame(directory / f"{i:02d}.yuv")[1] for name, directory in extracted.items()}
        native_bytes, native = read_frame(Path(proof_request["destination"] + f".proof-{n}.nv12"))
        pixels = width * height
        uv = native[pixels:].reshape(-1, 2)
        native = np.concatenate((native[:pixels], uv[:, 0], uv[:, 1]))
        row = {"frame": n, "commonReferenceSHA256": hashlib.sha256(raw_bytes).hexdigest(),
               "nativePreEncodeNV12SHA256": hashlib.sha256(native_bytes).hexdigest(),
               "preEncodePSNR": metrics(native - raw),
               "commonReferencePSNR": {name: metrics(array - raw) for name, array in arrays.items()},
               "softwareVideotoolboxPSNR": psnr(arrays["software"] - arrays["videotoolbox"])}
        for name in ("software", "videotoolbox"):
            diagnostic = metrics(arrays[name] - arrays["metal"])
            row[name + "PSNR"] = diagnostic["average"]
            row[name + "LumaPSNR"] = diagnostic["Y"]
            row[name + "RegionsPSNR"] = diagnostic["regionsY"]
        rows.append(row)
    return rows, {"command": command, "pixelFormat": pixel_format, "peak": 255,
                  "averageWeighting": "4:1:1 Y:U:V sample-count-weighted MSE, then PSNR",
                  "identity": "SHA-256 of each exact uncompressed planar frame; shared by all backends",
                  "nativeProofRequest": proof_request}


def benchmark(project: Project, asset_id: str, seconds: float = 30) -> dict:
    asset = load_asset(project, asset_id)
    source = project.abs(asset["media"]).resolve(strict=True)
    original_identity = _stat_identity(source)
    if not 0 < seconds <= min(180, float(asset["probe"]["duration"])):
        raise ReelsError("benchmark interval must fit the original and the 180-second aim limit")
    frames = round(seconds * 30)
    report = {"asset": asset["id"], "sourceBytes": source.stat().st_size,
              "start": 0, "end": seconds, "frames": frames, "fps": 30,
              "sourceIdentity": original_identity, "sourceAssetHash": asset.get("hash"),
              "approvedGate": "pre-encode average >=35 dB; native common-reference Y and average >= W5b minus 0.10 dB at every sample",
              "encodedToEncoded": "diagnostic only; former >=35 dB proxy gate failed, replaced by explicit user approval to separate reprojection from independent lossy encoding",
              "cacheConditions": "fresh media and CORI directories per worker; no telemetry cache; original OS page cache shared",
              "proxies": {}}
    # Keep derived media outside the read-only renderer tree, exactly as the
    # sandbox requires; no exception to its code-root write denial.
    with tempfile.TemporaryDirectory(prefix="proxy-metal-bench-") as td, ExitStack() as reservations:
        root = Path(td).resolve()
        workspace = SimpleNamespace(root=root / "workspace", code_root=project.code_root,
                                    project=project, _originals={asset["id"]: {"path": str(source)}})
        workspace.root.mkdir()
        manager = PreviewManager(workspace)
        # Account for retained outputs and bounded proof frames, in addition to
        # each production worker's own inherited lease. Never write into originals.
        reservations.enter_context(reserve_disk(budget_root(workspace.root),
                                                proxy_reservation(seconds) * 8 + 256_000_000))
        probe = {**deepcopy(asset["probe"]), "duration": seconds}
        for kind in ("source", "sphere"):
            paths, runs, requests = {}, {}, {}
            for name in ("software", "videotoolbox", "metal", "metal-proof"):
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
                measured = _MeasuredWorker(name == "software", job_root / "commands.json", seconds,
                                           proof=name == "metal-proof")
                started = time.perf_counter()
                with backend("metal" if name.startswith("metal") else "v360"):
                    result = manager._run_worker(job, context=measured, code_root=project.code_root, timeout=1800)
                elapsed = time.perf_counter() - started
                if result.get("status") != "ready" or result.get("reused"):
                    raise ReelsError(f"{kind}/{name}: expected cold ready output: {result}")
                if name.startswith("metal") and result.get("backend") != "metal":
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
                if name.startswith("metal"):
                    requests[name] = json.loads(measured.log.with_suffix(".request.json").read_text())
                if name == "videotoolbox" and result.get("encoder") != "h264_videotoolbox":
                    raise ReelsError(f"W5b benchmark did not use the required hardware H.264 preset: {result}")
                if name.startswith("metal") and (result.get("encoder") != "h264_videotoolbox" or
                                                  video.get("profile") != runs["videotoolbox"]["profile"]):
                    raise ReelsError("native/W5b H.264 encoder or profile mismatch")
                runs[name] = {"wallSeconds": elapsed, "outputBytes": path.stat().st_size,
                              "backend": result.get("backend"), "encoder": result.get("encoder"),
                              "native": result.get("native"), "muxSeconds": result.get("muxSeconds"),
                              "encodeSeconds": result.get("encodeSeconds"), "commands": commands,
                              "dimensions": expected_dimensions, "videoFrames": len(timestamps),
                              "profile": video.get("profile"), "level": video.get("level"),
                              "pixelFormat": video["pix_fmt"], "colour": {k: video.get(k) for k in ("color_range", "color_space", "color_transfer", "color_primaries")},
                              "audioPackets": audio, "audioPCMHash": audio_hash, "keyframes": keyframes}
                paths[name] = path
            production_request = {k: v for k, v in requests["metal"].items() if k != "destination"}
            proof_request = {k: v for k, v in requests["metal-proof"].items()
                             if k not in ("destination", "proofSampleFrames")}
            if production_request != proof_request:
                raise ReelsError("proof readback changed the production native request")
            proof_run = runs.pop("metal-proof")
            paths.pop("metal-proof")
            samples, reference = _samples(paths, frames, root / kind, *expected_dimensions,
                                          runs["videotoolbox"]["commands"][0]["command"],
                                          requests["metal-proof"])
            quality = _quality_gate(samples)
            audio_match = all(runs[name]["audioPackets"] == runs["software"]["audioPackets"] and
                              runs[name]["audioPCMHash"] == runs["software"]["audioPCMHash"]
                              for name in ("videotoolbox", "metal"))
            report["proxies"][kind] = {"runs": runs, "samples": samples, "matchedVideoPTS": True,
                                       "matchedAudioPackets": audio_match,
                                       "commonReference": reference, "qualityGate": quality,
                                       "proofReadbackRun": proof_run,
                                       "proofMatchesProductionRequest": True,
                                       "speedupOriginal": runs["software"]["wallSeconds"] / runs["metal"]["wallSeconds"],
                                       "speedupW5b": runs["videotoolbox"]["wallSeconds"] / runs["metal"]["wallSeconds"],
                                       "pass": audio_match and quality["pass"]}
        if _stat_identity(source) != original_identity:
            raise ReelsError("original changed during benchmark")
    report["temporaryMediaRemoved"] = True
    report["aimSpeedGate"] = {"minimum": 2, "pass": report["proxies"]["sphere"]["speedupOriginal"] >= 2}
    report["pass"] = report["aimSpeedGate"]["pass"] and all(p["pass"] for p in report["proxies"].values())
    return report


def command(project: Project, args) -> int:
    with redirect_stdout(sys.stderr):
        report = benchmark(project, args.asset, args.seconds)
    text = json.dumps(report, indent=2, allow_nan=False)
    if args.report:
        Path(args.report).write_text(text + "\n")
    print(text)
    return 0 if report["pass"] else 1
