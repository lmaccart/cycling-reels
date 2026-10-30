from __future__ import annotations

import hashlib
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from functools import lru_cache
from math import ceil
from pathlib import Path
from typing import Any

from .config import Project
from .media import BIN
from .tags import SOUND_KINDS
from .util import ReelsError, fmt_ts, log, now_iso, read_json, warn, write_json

# Sound events in ride audio, found with YAMNet (AudioSet, 521 classes) run through
# onnxruntime. Claude can't listen, so each candidate carries the AudioSet labels behind
# it, how windy that stretch is and how far it pokes out of the background, and
# `draw()` renders a loudness strip + spectrogram with the candidates boxed.

KINDS = SOUND_KINDS
VERSION = 3  # bump when rules change so caches are rebuilt

MODEL_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/yamnet/releases/v0.65.0/yamnet-onnx-float.zip"
MODEL_ZIP_SHA256 = "cf08212c3b9c03160d3c343a5cf99dd53edcb3176b49b6dd330d7d0480abd309"
MODEL_FILES = {
    "yamnet.onnx": "9261fbe1f196c8bb695933845af28df0a155845c9ec5e37ec96295e82c96f096",
    "yamnet.data": "0b4de0b14bf2b528d51e3cd84e2d3e2ae826b942fbec6001c654605f1f2ceb60",
    "labels.txt": "8e1267a120c1932b7273c0d0e0c5529edbb9a35512b437b1c8982baa59047051",
}

SR = 16000
HOP_S = 0.01  # mel frame hop
PATCH = 96  # frames per YAMNet patch (0.96 s)
PATCH_HOP = 48  # 0.48 s between patches
PATCH_S = PATCH * HOP_S
STEP_S = PATCH_HOP * HOP_S

# kind -> AudioSet label weights. Click families double as freehub (sustained) or
# shift/clip-in (one short click); see _events().
RULES: dict[str, dict[str, float]] = {
    "freehub": {"Ratchet, pawl": 1.0, "Clip-clop": 0.8, "Horse": 0.6, "Clickety-clack": 0.8, "Mechanisms": 0.7,
                "Tick-tock": 0.6, "Bicycle": 0.5},  # fmt: skip
    "click": {"Clicking": 1.0, "Tick": 0.9, "Mechanisms": 0.6, "Gears": 0.8, "Tap": 0.5, "Ratchet, pawl": 0.6},
    "bell": {"Bicycle bell": 1.0, "Bell": 0.7, "Jingle bell": 0.7, "Ding": 0.7, "Chime": 0.5, "Jingle, tinkle": 0.5},
    "brakes": {"Squeal": 1.0, "Squeak": 0.8, "Tire squeal": 0.9, "Skidding": 0.7, "Train wheels squealing": 0.6},
    "pass-by": {"Car passing by": 1.0, "Whoosh, swoosh, swish": 0.8},
    "car": {"Vehicle horn, car horn, honking": 1.0, "Motorcycle": 0.8, "Engine starting": 0.7, "Reversing beeps": 0.6,
            "Car alarm": 0.6, "Truck": 0.5, "Bus": 0.5},  # fmt: skip
    "gravel": {"Crunch": 1.0, "Crushing": 0.8, "Scrape": 0.6, "Rustle": 0.5},
    "chain": {"Rattle": 0.8, "Chink, clink": 0.7, "Clatter": 0.7, "Keys jangling": 0.6},
    "tire-hum": {"Hum": 0.6, "Buzz": 0.5},
    "cheer": {"Cheering": 1.0, "Applause": 0.9, "Clapping": 0.8, "Whoop": 0.8, "Yell": 0.6, "Shout": 0.6,
              "Children shouting": 0.5},  # fmt: skip
    "crowd": {"Crowd": 1.0, "Hubbub, speech noise, speech babble": 0.8, "Chatter": 0.8},
}
# Bar-mounted cams hear wind buffeting as wind, rain or thunder.
WIND = {"Wind noise (microphone)": 1.0, "Wind": 0.9, "Rain": 0.6, "Thunderstorm": 0.6, "Thunder": 0.4, "Rain on surface": 0.4}
# Road/wind roar on a moving bike scores as traffic; a pass-by has to rise above it.
VEHICLE = {"Vehicle": 0.6, "Motor vehicle (road)": 0.8, "Car": 0.8, "Traffic noise, roadway noise": 0.6}
THRESH = {"freehub": 0.12, "click": 0.12, "bell": 0.1, "brakes": 0.12, "pass-by": 0.12, "car": 0.15, "gravel": 0.12,
          "chain": 0.12, "tire-hum": 0.2, "cheer": 0.12, "crowd": 0.15}  # fmt: skip
# Shown with candidates; these say little about which kind it is.
GENERIC = {"Vehicle", "Outside, rural or natural", "Outside, urban or manmade", "Silence", "Noise", "Environmental noise",
           "Inside, small room", "Inside, large room or hall", "Animal", "Field recording", "Sound effect"}  # fmt: skip
COLORS = {"freehub": (255, 214, 0), "shift": (0, 220, 255), "clip-in": (0, 220, 255), "bell": (255, 120, 220),
          "brakes": (255, 80, 60), "pass-by": (120, 255, 120), "car": (255, 160, 40), "gravel": (200, 170, 120),
          "chain": (180, 180, 255), "tire-hum": (160, 160, 160), "cheer": (255, 255, 255), "crowd": (230, 230, 230),
          "other": (200, 200, 200)}  # fmt: skip
MAX_EVENTS = 60


# --- paths / model ---------------------------------------------------------------


def sounds_dir(project: Project) -> Path:
    return project.library / "sounds"


def cache_file(project: Project, asset_id: str) -> Path:
    return sounds_dir(project) / f"{asset_id}.json"


def model_dir(project: Project) -> Path:
    d = project.root / "models" / "yamnet"
    bundled = Path(__file__).resolve().parents[2] / "models" / "yamnet"  # e.g. the selftest's temp project
    if not (d / "yamnet.onnx").exists() and (bundled / "yamnet.onnx").exists():
        return bundled
    return d


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ensure_model(project: Project) -> Path:
    """models/yamnet/ with verified files; downloads the pinned Qualcomm zip if missing."""
    d = model_dir(project)
    if all((d / n).exists() for n in MODEL_FILES):
        return d
    log(f"downloading the YAMNet sound model (14 MB) into {project.rel(d)} …")
    with tempfile.TemporaryDirectory() as td:
        z = Path(td) / "yamnet.zip"
        try:
            urllib.request.urlretrieve(MODEL_URL, z)
        except OSError as e:
            raise ReelsError(f"couldn't download the sound model ({e}); see models/yamnet/README.md") from e
        if _sha256(z) != MODEL_ZIP_SHA256:
            raise ReelsError("sound model download doesn't match its pinned sha256 — not using it")
        d.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(z) as zf:
            for info in zf.infolist():
                name = Path(info.filename).name
                if name in MODEL_FILES:
                    with zf.open(info) as src, open(Path(td) / name, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        for name, sha in MODEL_FILES.items():
            p = Path(td) / name
            if not p.exists() or _sha256(p) != sha:
                raise ReelsError(f"sound model file {name} is missing or doesn't match its sha256")
            shutil.move(str(p), str(d / name))
    return d


@lru_cache(maxsize=2)
def _session(path: str):
    try:
        import onnxruntime as ort
    except ImportError as e:
        raise ReelsError("onnxruntime is missing — run ./setup.sh (it's in requirements.txt)") from e
    opts = ort.SessionOptions()
    opts.log_severity_level = 3
    return ort.InferenceSession(path, opts, providers=["CPUExecutionProvider"])


def _labels(project: Project) -> list[str]:
    return [line.strip() for line in (ensure_model(project) / "labels.txt").read_text().splitlines() if line.strip()]


# --- signal ---------------------------------------------------------------------


def load_audio(src: Path, start: float = 0.0, end: float | None = None):
    """16 kHz mono float32 of [start, end) of the first audio track."""
    import numpy as np

    cmd = [BIN["ffmpeg"], "-nostdin", "-v", "error"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    if end is not None:
        cmd += ["-t", f"{max(0.05, end - start):.3f}"]
    cmd += ["-i", str(src), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SR), "-f", "f32le", "-"]
    proc = subprocess.run(cmd, capture_output=True)
    if proc.returncode != 0:
        raise ReelsError(f"couldn't decode audio from {Path(src).name}: {proc.stderr.decode(errors='replace')[-300:]}")
    return np.frombuffer(proc.stdout, dtype=np.float32).copy()


@lru_cache(maxsize=4)
def _mel_bank(n_mels: int, fmin: float, fmax: float):
    """HTK mel filterbank on 257 rfft bins, as torchaudio.MelScale builds it (norm=None)."""
    import numpy as np

    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    freqs = np.linspace(0, SR / 2, 257)
    f = 700.0 * (10 ** (np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2) / 2595.0) - 1.0)
    slopes = f[None, :] - freqs[:, None]
    fd = np.diff(f)
    return np.maximum(0, np.minimum(-slopes[:, :-2] / fd[:-1], slopes[:, 2:] / fd[1:])).astype(np.float32)


def features(audio, n_mels: int = 64, fmin: float = 125.0, fmax: float = 7500.0):
    """(log-mel [frames, n_mels], rms dBFS per frame). YAMNet's frontend: 25 ms Hann, 10 ms hop,
    512-pt FFT magnitude, centred frames, log(mel + 0.001)."""
    import numpy as np

    x = audio if len(audio) > 512 else np.pad(audio, (0, 513 - len(audio)))
    x = np.pad(x, 256, mode="reflect")
    n = 1 + (len(x) - 512) // 160
    win = np.zeros(512, np.float32)
    win[56:456] = 0.5 - 0.5 * np.cos(2 * np.pi * np.arange(400) / 400)
    bank = _mel_bank(n_mels, fmin, fmax)
    mel = np.empty((n, n_mels), np.float32)
    rms = np.empty(n, np.float32)
    for a in range(0, n, 20000):  # chunks keep long rides in memory
        b = min(n, a + 20000)
        idx = np.arange(512)[None, :] + 160 * np.arange(a, b)[:, None]
        fr = x[idx]
        mel[a:b] = np.log(np.abs(np.fft.rfft(fr * win, axis=1)).astype(np.float32) @ bank + 0.001)
        rms[a:b] = 10 * np.log10(np.mean(fr[:, 56:456] ** 2, axis=1) + 1e-10)
    return mel, rms


def ticks(mel, fmin: float = 125.0, fmax: float = 7500.0):
    """Frames with a sharp broadband onset above 2 kHz — the vertical stripes of freehub
    pawls, shifts and clip-ins. Thresholded per 1 s block (median + 4 MAD) so steady wind doesn't count."""
    import numpy as np

    def hz2mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    n_mels = mel.shape[1]
    centres = 700.0 * (10 ** (np.linspace(hz2mel(fmin), hz2mel(fmax), n_mels + 2)[1:-1] / 2595.0) - 1.0)
    hb = mel[:, centres > 2000]
    flux = np.concatenate([[0.0], np.maximum(np.diff(hb, axis=0), 0).mean(axis=1)])
    out = np.zeros(len(flux), bool)
    for a in range(0, len(flux), 100):
        blk = flux[max(0, a - 50) : a + 150]
        med = np.median(blk)
        mad = np.median(np.abs(blk - med)) + 1e-6
        seg = flux[a : a + 100]
        out[a : a + 100] = (seg > med + 4 * mad) & (seg > 0.3)
    out &= flux >= np.roll(flux, 1)
    out &= flux >= np.roll(flux, -1)
    return out


def classify(project: Project, mel):
    """Per-patch AudioSet probabilities [patches, 521]."""
    import numpy as np

    sess = _session(str(ensure_model(project) / "yamnet.onnx"))
    if len(mel) < PATCH:
        mel = np.pad(mel, ((0, PATCH - len(mel)), (0, 0)), constant_values=np.log(0.001))
    starts = range(0, len(mel) - PATCH + 1, PATCH_HOP)
    name = sess.get_inputs()[0].name
    logits = np.stack([sess.run(None, {name: mel[None, None, s : s + PATCH]})[0][0] for s in starts])
    return 1.0 / (1.0 + np.exp(-np.clip(logits, -50, 50)))


# --- detection ------------------------------------------------------------------


def _lane(probs, labels: list[str], weights: dict[str, float]):
    import numpy as np

    cols = [(labels.index(k), w) for k, w in weights.items() if k in labels]
    return np.max(np.stack([probs[:, i] * w for i, w in cols]), axis=0)


def _runs(mask) -> list[tuple[int, int]]:
    """[start, end] index pairs of True stretches, bridging gaps of up to two patches."""
    out: list[list[int]] = []
    for i, v in enumerate(mask):
        if not v:
            continue
        if out and i - out[-1][1] <= 3:
            out[-1][1] = i
        else:
            out.append([i, i])
    return [(a, b) for a, b in out]


def detect(project: Project, src: Path, duration: float) -> dict:
    import numpy as np

    labels = _labels(project)
    audio = load_audio(src)
    if len(audio) < SR // 4:
        return {"version": VERSION, "analysedAt": now_iso(), "duration": duration, "events": [], "note": "no usable audio"}
    mel, rms = features(audio)
    probs = classify(project, mel)
    n = len(probs)
    long_clip = duration > 60

    wind = _lane(probs, labels, WIND)
    speech = probs[:, labels.index("Speech")]
    vehicle = _lane(probs, labels, VEHICLE)
    lanes = {k: _lane(probs, labels, w) for k, w in RULES.items()}
    # pass-by: also a vehicle swell above this clip's usual road roar
    lanes["pass-by"] = np.maximum(lanes["pass-by"], np.clip(vehicle - np.percentile(vehicle, 75) - 0.1, 0, None))
    if long_clip:  # long rides: only what stands out from the clip's own background
        lanes = {k: np.clip(v - np.median(v), 0, None) for k, v in lanes.items()}

    # loudness per patch, and the background around it (±15 s)
    rms_p = np.array([rms[i * PATCH_HOP : i * PATCH_HOP + PATCH].max() for i in range(n)])
    k = max(1, int(15 / STEP_S))
    bg = np.array([np.median(rms_p[max(0, i - k) : i + k + 1]) for i in range(n)])
    prom = rms_p - bg
    tk = ticks(mel)
    tick_rate = np.array([tk[i * PATCH_HOP : i * PATCH_HOP + PATCH].sum() / PATCH_S for i in range(n)])

    events: list[dict] = []
    for kind, lane in lanes.items():
        for a, b in _runs(lane >= THRESH[kind]):
            seg = slice(a, b + 1)
            peak_i = a + int(np.argmax(lane[seg]))
            w = float(wind[seg].mean())
            sp = float(speech[seg].max())
            pr = float(prom[seg].max())
            kk = kind
            if kind == "click":
                kk = "freehub" if b - a >= 3 else "shift"  # a long run of clicks is a coasting freehub
            conf = float(lane[peak_i]) * (1 - min(0.9, 1.2 * w)) * (0.75 + 0.25 * min(1.0, max(0.0, pr) / 8))
            if sp > 0.5:
                conf *= 0.7
            if kind == "pass-by" and pr < 2:
                continue  # no rise and fall: just road roar
            rate = float(tk[a * PATCH_HOP : b * PATCH_HOP + PATCH].sum()) / ((b - a) * STEP_S + PATCH_S)
            if kind in ("freehub", "click"):
                conf *= 0.75 + 0.5 * min(1.0, rate / 8)  # visible clicks back the label up
            top = [(labels[i], float(probs[peak_i, i])) for i in np.argsort(-probs[peak_i])[:8]]
            top = [(lab, p) for lab, p in top if lab not in GENERIC and p >= 0.05][:4]
            events.append(
                {
                    "at": round(a * STEP_S, 2),
                    "for": round(min(duration, b * STEP_S + PATCH_S) - a * STEP_S, 2),
                    "kind": kk,
                    "conf": round(min(1.0, conf), 3),
                    "peak": round(peak_i * STEP_S + PATCH_S / 2, 2),
                    "wind": round(w, 2),
                    "speech": sp > 0.5,
                    "riseDb": round(pr, 1),
                    "ticks": round(rate, 1),
                    "labels": [[lab, round(p, 2)] for lab, p in top],
                }
            )
    # same stretch found by two rules: keep the stronger
    family = {"shift": "click", "clip-in": "click", "freehub": "click"}
    events.sort(key=lambda e: -e["conf"])
    kept: list[dict] = []
    for e in events:
        fam = family.get(e["kind"], e["kind"])
        if any(fam == family.get(o["kind"], o["kind"]) and e["at"] < o["at"] + o["for"] and o["at"] < e["at"] + e["for"] for o in kept):
            continue
        kept.append(e)
    kept = [e for e in kept if e["conf"] >= 0.04][:MAX_EVENTS]
    kept.sort(key=lambda e: e["at"])
    for i, e in enumerate(kept):
        e["n"] = i + 1
        e["at"] = round(min(e["at"], max(0.0, duration - 0.2)), 2)
        e["for"] = round(max(0.2, min(e["for"], duration - e["at"])), 2)

    def q(v) -> list[int]:
        return [int(round(min(1.0, max(0.0, float(x))) * 100)) for x in v]

    return {
        "version": VERSION,
        "model": "yamnet-qaihm-0.65.0",
        "analysedAt": now_iso(),
        "duration": duration,
        "meanDb": round(float(np.median(rms)), 1),
        "windShare": round(float((wind > 0.15).mean()), 2),
        "events": kept,
        # 0–100 per 0.48 s patch, for drawing
        "timeline": {"step": STEP_S, "wind": q(wind), "speech": q(speech), "ticks": q(tick_rate / 20),
                     **{k: q(v) for k, v in lanes.items()}},  # fmt: skip
    }


def has_cache(project: Project, asset: dict) -> bool:
    p = cache_file(project, asset["id"])
    if not p.exists():
        return False
    try:
        return read_json(p).get("version") == VERSION
    except Exception:  # noqa: BLE001
        return False


def analyse(project: Project, asset: dict, *, force: bool = False) -> dict:
    """Cached detection for one video asset."""
    if asset["kind"] != "video" or not asset["probe"].get("hasAudio"):
        raise ReelsError(f"{asset['id']} has no audio")
    p = cache_file(project, asset["id"])
    if not force and has_cache(project, asset):
        return read_json(p)
    data = detect(project, project.abs(asset["media"]), float(asset["probe"]["duration"]))
    data["asset"] = asset["id"]
    write_json(p, data)
    return data


def load_cache(project: Project, asset_id: str) -> dict | None:
    p = cache_file(project, asset_id)
    if not p.exists():
        return None
    try:
        return read_json(p)
    except Exception:  # noqa: BLE001
        return None


def on_ingest(project: Project, asset: dict) -> None:
    """Ingest hook: never fails the ingest."""
    try:
        data = analyse(project, asset)
    except Exception as e:  # noqa: BLE001
        warn(f"{asset['id']}: sound detection failed ({e}); retry with ./reels sounds {asset['id']}")
        return
    if data["events"]:
        log(f"    sounds: {summary(data)}")


# --- text -----------------------------------------------------------------------


def describe_event(e: dict) -> str:
    kind = "shift/clip-in" if e["kind"] in ("shift", "clip-in") else e["kind"]
    bits = [f"#{e['n']:<3} {fmt_ts(e['at'])}  {e['for']:4.1f}s  {kind:<13} conf {e['conf']:.2f}"]
    bits.append(f"wind {e['wind']:.2f}  ticks {e.get('ticks', 0):.0f}/s  rise {e['riseDb']:+.0f} dB" + ("  speech" if e.get("speech") else ""))
    if e.get("labels"):
        bits.append("· " + ", ".join(f"{lab} {p:.2f}" for lab, p in e["labels"]))
    return "  ".join(bits)


def summary(data: dict, top: int = 3) -> str:
    ev = data.get("events") or []
    counts: dict[str, int] = {}
    for e in ev:
        counts[e["kind"]] = counts.get(e["kind"], 0) + 1
    best = sorted(ev, key=lambda e: -e["conf"])[:top]
    return (
        f"{len(ev)} candidate(s) (" + ", ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])) + ")"
        + ("; best " + ", ".join(f"{e['kind']} {fmt_ts(e['at'])} ({e['conf']:.2f})" for e in best) if best else "")
    )  # fmt: skip


# --- image ----------------------------------------------------------------------

_CMAP = [(0.0, (0, 0, 4)), (0.25, (60, 15, 110)), (0.5, (180, 55, 120)), (0.75, (250, 140, 90)), (1.0, (252, 253, 190))]


def _colormap(v):
    import numpy as np

    v = np.clip(v, 0, 1)
    xs = [p for p, _ in _CMAP]
    return np.stack([np.interp(v, xs, [c[i] for _, c in _CMAP]) for i in range(3)], axis=-1).astype(np.uint8)


def _tick_step(span: float, px: int) -> float:
    for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300):
        if px * s / span >= 70:
            return s
    return 600


def draw(project: Project, asset: dict, data: dict, start: float = 0.0, end: float | None = None) -> list[Path]:
    """Loudness + spectrogram rows with wind/speech lanes and numbered candidate boxes."""
    import numpy as np
    from PIL import Image, ImageDraw

    from .sheets import load_font

    dur = float(asset["probe"]["duration"])
    end = dur if end is None else min(end, dur)
    start = max(0.0, min(start, end - 0.5))
    span = end - start
    zoomed = start > 0 or end < dur
    if span <= 20:
        row_s = span
    elif span <= 80:
        row_s = 20.0
    elif span <= 240:
        row_s = 30.0
    elif span <= 480:
        row_s = 60.0
    else:
        row_s = float(ceil(span / 8 / 60) * 60)  # overview: 8 rows max, zoom in with --start/--end
    rows = max(1, ceil(span / row_s - 1e-6))
    per_img = 8

    audio = load_audio(project.abs(asset["media"]), start, end)
    mel, rms = features(audio, n_mels=96, fmin=60.0, fmax=8000.0)
    tk = ticks(features(audio)[0])
    lo, hi = np.percentile(mel, 2), np.percentile(mel, 99.7)
    spec_norm = (mel - lo) / max(1e-6, hi - lo)
    db_hi = float(np.ceil(rms.max() / 6) * 6)
    db_lo = max(db_hi - 48, float(np.floor(np.percentile(rms, 2) / 6) * 6))  # relative scale so wind roar doesn't fill it

    W, LM, RM = 1500, 92, 16
    LOUD_H, SPEC_H, LANE_H, TICK_H, GAP = 44, 150, 13, 26, 22
    TK_H = 14
    row_h = LOUD_H + SPEC_H + TK_H + 2 * LANE_H + TICK_H + GAP
    f_title, f_sub, f_lab, f_small = load_font(24), load_font(16, bold=False), load_font(15), load_font(13, bold=False)
    f_tiny = load_font(11, bold=False)
    tl = data.get("timeline") or {}
    step = tl.get("step", STEP_S)
    events = [e for e in data.get("events") or [] if e["at"] < end and e["at"] + e["for"] > start]

    outs: list[Path] = []
    n_imgs = ceil(rows / per_img)
    for ii in range(n_imgs):
        r0, r1 = ii * per_img, min(rows, (ii + 1) * per_img)
        H = 76 + (r1 - r0) * row_h
        img = Image.new("RGB", (LM + W + RM, H), (16, 16, 16))
        d = ImageDraw.Draw(img)
        title = f"{asset['id']}  ·  sounds {fmt_ts(start)}–{fmt_ts(end)}" + (f"  ·  part {ii + 1}/{n_imgs}" if n_imgs > 1 else "")
        d.text((10, 10), title, fill=(240, 240, 240), font=f_title)
        d.text((10, 44), "top: loudness (dBFS, scale at left) · middle: spectrogram 60 Hz–8 kHz, low at bottom · lanes: HF clicks (white), wind (red), speech (blue) · "
               "boxes: #n kind conf (see ./reels sounds list)", fill=(170, 170, 170), font=f_sub)  # fmt: skip
        for r in range(r0, r1):
            t0 = start + r * row_s
            t1 = min(end, t0 + row_s)
            y = 76 + (r - r0) * row_h
            px = lambda t: LM + (t - t0) / row_s * W  # noqa: E731
            xw = int(round((t1 - t0) / row_s * W))
            f0, f1 = int((t0 - start) / HOP_S), int((t1 - start) / HOP_S)
            f1 = max(f0 + 1, min(f1, len(mel)))
            # spectrogram
            sp = _colormap(spec_norm[f0:f1].T[::-1])
            sp_img = Image.fromarray(sp).resize((max(1, xw), SPEC_H), Image.BILINEAR)
            ys = y + LOUD_H
            img.paste(sp_img, (LM, ys))
            # loudness
            seg = rms[f0:f1]
            cols = max(1, xw)
            bins = np.array_split(seg, cols) if len(seg) >= cols else [seg[min(len(seg) - 1, int(i * len(seg) / cols)):][:1] for i in range(cols)]
            d.rectangle([LM, y, LM + W, y + LOUD_H - 2], fill=(28, 28, 28))
            yy = y + LOUD_H - 2 - 0.5 * (LOUD_H - 4)
            d.line([(LM, yy), (LM + W, yy)], fill=(55, 55, 55))
            for i, bseg in enumerate(bins):
                v = float(bseg.max()) if len(bseg) else db_lo
                hgt = max(0.0, min(1.0, (v - db_lo) / (db_hi - db_lo))) * (LOUD_H - 4)
                d.line([(LM + i, y + LOUD_H - 2), (LM + i, y + LOUD_H - 2 - hgt)], fill=(120, 200, 255))
            # lanes from the cached timeline
            yk = ys + SPEC_H + 1
            d.rectangle([LM, yk, LM + W, yk + TK_H - 2], fill=(26, 26, 26))
            for fi in np.flatnonzero(tk[f0:f1]):
                x = px(start + (f0 + fi) * HOP_S)
                d.line([(x, yk), (x, yk + TK_H - 2)], fill=(255, 255, 255))
            yl = yk + TK_H
            for li, (key, col) in enumerate((("wind", (255, 70, 60)), ("speech", (80, 150, 255)))):
                vals = tl.get(key) or []
                yy = yl + li * LANE_H
                d.rectangle([LM, yy, LM + W, yy + LANE_H - 2], fill=(26, 26, 26))
                for j, v in enumerate(vals):
                    ta = j * step + PATCH_S / 2 - step / 2
                    if ta + step < t0 or ta > t1 or v < 8:
                        continue
                    a = v / 100
                    c = tuple(int(26 + (cc - 26) * a) for cc in col)
                    d.rectangle([max(LM, px(ta)), yy, min(LM + W, px(ta + step)), yy + LANE_H - 2], fill=c)
            # left labels
            d.text((8, y + 2), fmt_ts(t0), fill=(255, 225, 77), font=f_lab)
            d.text((8, y + 22), f"{db_lo:.0f}…{db_hi:.0f}dB", fill=(150, 150, 150), font=f_small)
            d.text((8, ys + 4), "8 kHz", fill=(150, 150, 150), font=f_small)
            d.text((8, ys + SPEC_H // 2 - 8), "1 kHz", fill=(150, 150, 150), font=f_small)
            d.text((8, ys + SPEC_H - 18), "60 Hz", fill=(150, 150, 150), font=f_small)
            d.text((8, yk - 1), "HF clicks", fill=(220, 220, 220), font=f_tiny)
            d.text((8, yl - 1), "wind", fill=(255, 110, 100), font=f_tiny)
            d.text((8, yl + LANE_H - 1), "speech", fill=(120, 170, 255), font=f_tiny)
            # ticks
            yt = yl + 2 * LANE_H + 2
            ts = _tick_step(row_s, W)
            t = ceil(t0 / ts - 1e-9) * ts
            while t <= t1 + 1e-6:
                x = px(t)
                d.line([(x, yt), (x, yt + 6)], fill=(220, 220, 220), width=2)
                lab = fmt_ts(t)
                tw = d.textlength(lab, font=f_small)
                d.text((min(max(x - tw / 2, LM), LM + W - tw), yt + 7), lab, fill=(220, 220, 220), font=f_small)
                t += ts
            # candidates
            for e in events:
                a, b = max(t0, e["at"]), min(t1, e["at"] + e["for"])
                if b <= a:
                    continue
                col = COLORS.get(e["kind"], (200, 200, 200))
                x0, x1 = px(a), max(px(a) + 3, px(b))
                d.rectangle([x0, y, x1, ys + SPEC_H], outline=col, width=2)
                if e["at"] >= t0:  # label once, where it starts
                    lab = f"#{e['n']} {e['kind']} {e['conf']:.2f}"
                    tw = d.textlength(lab, font=f_lab)
                    lx = min(x0 + 2, LM + W - tw - 4)
                    d.rectangle([lx - 2, ys + 2, lx + tw + 3, ys + 20], fill=(0, 0, 0))
                    d.text((lx, ys + 3), lab, fill=col, font=f_lab)
        name = asset["id"] + (f"-z{start:g}-{end:g}" if zoomed else "") + ("" if n_imgs == 1 else f"-{ii + 1}") + ".jpg"
        out = sounds_dir(project) / name
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out, quality=88)
        outs.append(out)
    return outs
