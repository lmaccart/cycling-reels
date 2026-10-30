from __future__ import annotations

import functools
import http.server
import re
import shutil
import tempfile
import threading
import time
from copy import deepcopy
from pathlib import Path

from PIL import Image, ImageDraw

from .config import Project
from .ingest import ingest
from .library import save_asset
from .media import extract_frame, ffprobe_json, load_audio_16k, run, which
from .music import add as add_track
from .render import build, check_plan, render_plan
from .sphere import extract_equirect
from .tags import validate_tags
from .util import ReelsError, log, read_json, write_json

# Everything here happens in a throwaway temp folder that is deleted afterwards.
# The test clips, photo and tone below only exist to exercise the pipeline;
# they never touch your library, your music folder or your reels.


def _test_fixtures(root: Path, served: Path) -> None:
    inbox = root / "inbox"
    run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=8",
        "-f", "lavfi", "-i", "sine=frequency=330:duration=8",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", inbox / "ride-landscape.mp4",
    ])  # fmt: skip
    run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=1080x1920:rate=60:duration=6",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-an", inbox / "ride-vertical.mp4",
    ])  # fmt: skip
    img = Image.new("RGB", (3000, 2000))
    d = ImageDraw.Draw(img)
    for y in range(2000):
        d.line([(0, y), (3000, y)], fill=(40 + y // 20, 90 + y // 30, 160 - y // 25))
    d.ellipse([1200, 700, 1800, 1300], fill=(250, 210, 80))
    img.save(inbox / "photo.jpg", quality=92)
    # A test tone with a quiet 6 s intro, served over local HTTP so the yt-dlp
    # download path runs exactly as it does for a YouTube link (minus YouTube).
    run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=6,volume=0.04",
        "-f", "lavfi", "-i", "sine=frequency=220:duration=34,volume='0.6+0.3*sin(2*PI*2*t)':eval=frame",
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[o]", "-map", "[o]",
        "-c:a", "aac", "-b:a", "128k", served / "selftest-tone.m4a",
    ])  # fmt: skip
    # A kick drum at exactly 120 bpm (a beat every 0.5 s from 0.5 s) for the beat detector and cut-on-beat.
    run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi",
        "-i", "aevalsrc=0.8*sin(2*PI*60*t)*exp(-14*mod(t-0.5\\,0.5))*gte(t\\,0.5):s=44100:d=24",
        "-c:a", "libmp3lame", "-b:a", "192k", root / "selftest-beat.mp3",
    ])  # fmt: skip


def _low_band_db(path: Path) -> float:
    """Mean level under 100 Hz — where the test kick lives and the test clips' 330 Hz tone doesn't."""
    proc = run(["ffmpeg", "-hide_banner", "-i", path, "-vn", "-af", "lowpass=f=100,lowpass=f=100,volumedetect", "-f", "null", "-"], check=False)
    m = re.search(r"mean_volume:\s*(-?[0-9.]+|-inf) dB", proc.stderr or "")
    return -120.0 if not m or m.group(1) == "-inf" else float(m.group(1))


def _sphere_pattern(path: Path) -> None:
    """Labelled test sphere: red = front (yaw 0), green = right (+90), blue = behind (±180), yellow = left (-90)."""
    from .sheets import load_font

    w, h = 2048, 1024
    img = Image.new("RGB", (w, h))
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / h
        sky = (int(70 + 90 * (1 - t)), int(110 + 60 * (1 - t)), int(200 - 120 * t))
        d.line([(0, y), (w, y)], fill=sky if y < h / 2 else (int(60 + 40 * t), int(90 + 30 * t), 50))
    font = load_font(46)
    for yaw, label, col in ((0, "FRONT", (220, 40, 40)), (90, "RIGHT", (40, 180, 60)), (-90, "LEFT", (230, 200, 30)),
                            (180, "BACK", (40, 80, 220)), (-180, "BACK", (40, 80, 220))):  # fmt: skip
        cx = (yaw + 180) / 360 * w
        d.rectangle([cx - 150, h / 2 - 110, cx + 150, h / 2 + 110], fill=col)
        d.text((cx - d.textlength(label, font=font) / 2, h / 2 - 28), label, fill=(255, 255, 255), font=font)
    for yaw in range(-180, 181, 30):
        x = (yaw + 180) / 360 * w
        d.line([(x, 0), (x, h)], fill=(255, 255, 255), width=5 if yaw % 90 == 0 else 2)
    d.line([(0, h / 2), (w, h / 2)], fill=(255, 255, 255), width=4)
    img.save(path)


def _pack_gopro_row(src: str, out: str, face: int, tag: str) -> str:
    """Inverse of the decoder: one 3-face EAC row -> a GoPro-style track with duplicated seam bands."""
    o, g = 64, 32
    cs, p = face - g, (face - g - 64) // 2

    def side(x: int, s: str) -> str:
        return (f"[{s}in]crop={face}:{face}:{x}:0,scale={cs}:{face}:flags=bicubic,split=3[{s}1][{s}2][{s}3];"
                f"[{s}1]crop={p}:{face}:0:0[{s}a];[{s}2]crop={o}:{face}:{p}:0,split=2[{s}b][{s}c];"
                f"[{s}3]crop={cs - p - o}:{face}:{p + o}:0[{s}d];[{s}a][{s}b][{s}c][{s}d]hstack=4[{s}f]")  # fmt: skip

    return (f"[{src}]split=3[{tag}lin][{tag}m][{tag}rin];" + side(0, f"{tag}l") + ";"
            + f"[{tag}m]crop={face}:{face}:{face}:0[{tag}mf];" + side(2 * face, f"{tag}r") + ";"
            + f"[{tag}lf][{tag}mf][{tag}rf]hstack=3[{out}]")  # fmt: skip


def _gopro_fixture(tmp: Path, inbox: Path) -> Path:
    """A 3 s GoPro MAX-style .360 (two 4096x1344 EAC tracks + audio) made from the test sphere."""
    pattern, eac = tmp / "sphere-pattern.png", tmp / "sphere-eac.png"
    _sphere_pattern(pattern)
    face = 1344
    run(["ffmpeg", "-v", "error", "-y", "-i", pattern, "-vf", f"v360=input=e:output=eac:w={3 * face}:h={2 * face}:interp=cubic", eac])
    graph = (f"[0:v]split=2[r0][r1];[r0]crop={3 * face}:{face}:0:0[top];[r1]crop={3 * face}:{face}:0:{face}[bot];"
             + _pack_gopro_row("top", "t0", face, "a") + ";" + _pack_gopro_row("bot", "t1", face, "b"))  # fmt: skip
    t0, t1 = tmp / "track0.png", tmp / "track1.png"
    run(["ffmpeg", "-v", "error", "-y", "-i", eac, "-filter_complex", graph, "-map", "[t0]", t0, "-map", "[t1]", t1])
    out = inbox / "GS019999.360"
    run(["ffmpeg", "-v", "error", "-y", "-loop", "1", "-framerate", "30", "-t", "3", "-i", t0,
         "-loop", "1", "-framerate", "30", "-t", "3", "-i", t1, "-f", "lavfi", "-t", "3", "-i", "anoisesrc=color=brown:amplitude=0.2",
         "-map", "0:v", "-map", "2:a", "-map", "1:v", "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", "-f", "mp4", out])  # fmt: skip
    return pattern


_PANELS = {"red": (220, 40, 40), "green": (40, 180, 60), "blue": (40, 80, 220), "yellow": (230, 200, 30)}


def _panel_at(video: Path, t: float, tmp: Path) -> str:
    """Which coloured test-sphere panel fills the middle of the reel frame at time t."""
    frame = tmp / f"panel-{t:.2f}.png"
    extract_frame(video, t, frame)
    with Image.open(frame) as im:
        w, h = im.size
        box = im.convert("RGB").crop((w // 4, int(h * 0.4), 3 * w // 4, int(h * 0.6))).resize((60, 40))
    counts = dict.fromkeys(_PANELS, 0)
    for px in box.getdata():
        name, dist = min(((n, sum((a - b) ** 2 for a, b in zip(px, c))) for n, c in _PANELS.items()), key=lambda x: x[1])
        if dist < 90**2:
            counts[name] += 1
    best = max(counts, key=lambda n: counts[n])
    return best if counts[best] >= 0.3 * 60 * 40 else "nothing clear"


def _psnr(a: Path, b: Path) -> float:
    proc = run(["ffmpeg", "-hide_banner", "-i", a, "-i", b, "-lavfi", "[0:v]format=rgb24[x];[1:v]format=rgb24[y];[x][y]psnr",
                "-f", "null", "-"], check=False)  # fmt: skip
    m = re.search(r"average:([0-9.]+|inf)", proc.stderr or "")
    return float("inf") if m and m.group(1) == "inf" else float(m.group(1)) if m else 0.0


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401 - silence request logs
        pass


def _serve(folder: Path) -> tuple[http.server.ThreadingHTTPServer, str]:
    handler = functools.partial(_QuietHandler, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


def _check_transcription(project: Project, tmp: Path) -> str:
    """Uses macOS `say` to make real speech, then runs the VAD and Whisper on it."""
    if not which("say"):
        return "skipped (needs macOS `say` to synthesise test speech)"
    from .speech import detect_speech
    from .transcribe import load_model

    speech = tmp / "speech.aiff"
    run(["say", "-o", speech, "This climb is going to hurt. Let's go."])
    found = detect_speech(speech, 10) or {}
    if not isinstance(found.get("speechSeconds"), (int, float)) or found["speechSeconds"] < 1:
        raise ReelsError(f"speech detection missed the spoken test line: {found}")
    model = load_model(project)
    segments, _ = model.transcribe(load_audio_16k(speech), language="en", word_timestamps=True, vad_filter=True)
    words = [w.word.strip().lower() for s in segments for w in (s.words or [])]
    if not any("climb" in w for w in words):
        raise ReelsError(f"Whisper check failed — heard: {' '.join(words)!r}")
    return f"speech ~{found['speechSeconds']:.1f}s detected; Whisper heard: \"{' '.join(words)}\""


def _check_stats(project: Project) -> str:
    """stats import matches a fake Metricool row to a fake post.json and builds the report."""
    from .analytics import build_report, import_file

    d = project.out_dir / "9999-01-01-stats-fake"
    write_json(d / "post.json", {"slug": d.name, "caption": "stats fake caption", "duration": 10.0, "scheduledFor": "2030-01-01T18:30:00",
                                 "timezone": "America/Chicago", "segments": []})
    row = ["20300102003100", "stats fake caption\n\n#cycling", "123", "https://www.instagram.com/reel/x/", "1", "2", "100", "3", "4", "150", "5.0", "700", None, "40.0", None]
    f = project.root / "stats-fake.json"
    write_json(f, {"pulledAt": "2030-01-05T00:00:00Z", "rows": [row]})
    import_file(project, str(f))
    reels, unmatched = build_report(project)
    r = reels[0]
    post = read_json(d / "post.json")
    if unmatched or r["slug"] != d.name or abs(r["watchThrough"] - 0.5) > 1e-6 or abs(r["sharesReach"] - 0.04) > 1e-6 or post.get("reelId") != "123":
        raise ReelsError(f"stats check failed: {r} unmatched={len(unmatched)} post={post}")
    return "stats: fake Metricool row matched, watch-through 50%, shares/reach 4%"


def _check_sfx(project: Project, tmp: Path) -> str:
    from . import sfx

    long_, short = tmp / "sfx-long.wav", tmp / "sfx-short.wav"
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=3,adelay=300,apad=pad_dur=0.3,volume=-30dB", long_])
    run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=880:duration=0.3,volume=-20dB", short])
    a = sfx.add(project, str(long_), name="selftest-tone", category="other", license="test-only", source_page="https://example.invalid/tone", author="selftest")
    b = sfx.add(project, str(short), name="selftest-blip", category="pop", license="test-only", source_page="https://example.invalid/blip")
    problems = []
    if a["loudness"] is None or abs(a["loudness"] - sfx.TARGET_LUFS) > 1.0 or a["peak"] > sfx.CEILING_DBTP + 0.1:
        problems.append(f"3 s tone should land at -20 LUFS under -1.5 dBTP, got {a['loudness']} LUFS / {a['peak']} dBTP")
    if abs(b["peak"] - sfx.PEAK_DBFS) > 0.6:
        problems.append(f"0.3 s blip should be peak-normalized to -6 dBFS, got {b['peak']}")
    if not (a["duration"] < 3.15 and a["license"] == "test-only" and a["source"] and (project.root / a["file"]).exists()):
        problems.append(f"sfx record looks wrong: {a}")
    info = ffprobe_json(project.root / a["file"])["streams"][0]
    if info.get("sample_rate") != "48000" or info.get("codec_name") != "pcm_s16le":
        problems.append(f"sfx should be 48 kHz 16-bit WAV, got {info.get('codec_name')} {info.get('sample_rate')}")
    try:
        sfx.add(project, str(short), name="selftest-nolicense", category="pop")
        problems.append("adding a sound without a license/source should be refused")
    except ReelsError:
        pass
    try:
        sfx.resolve_sfx(project, "selftest-tonne")
        problems.append("resolve_sfx should refuse an unknown name")
    except ReelsError as e:
        if "selftest-tone" not in str(e):
            problems.append(f"resolve_sfx error should list close matches: {e}")
    if not sfx.resolve_sfx(project, "selftest-tone")[0].exists() or len(sfx.list_sfx(project)) != 2:
        problems.append("list/resolve_sfx didn't find the added sounds")
    if problems:
        raise ReelsError("sfx check failed:\n  - " + "\n  - ".join(problems))
    return f"sfx: tone {a['loudness']} LUFS / {a['peak']} dBTP, blip peak {b['peak']} dBFS"


def _band(path: Path, a: float, b: float, af: str) -> tuple[float, float]:
    """(mean, max) dB of one band of a file's audio between a and b seconds."""
    proc = run(["ffmpeg", "-hide_banner", "-ss", f"{a:.3f}", "-t", f"{b - a:.3f}", "-i", path, "-vn", "-af", f"{af},volumedetect", "-f", "null", "-"], check=False)
    got = [re.search(rf"{k}_volume:\s*(-?[0-9.]+|-inf) dB", proc.stderr or "") for k in ("mean", "max")]
    return tuple(-120.0 if not m or m.group(1) == "-inf" else float(m.group(1)) for m in got)  # type: ignore[return-value]


def _check_sound(project: Project, land: dict, vert: dict, photo: dict) -> str:
    """Sound design on the test clips (330 Hz ride tone, 60 Hz kick song, 880 Hz blip sfx): a nat window
    lifts the ride audio, a drop silences the song, cues land on time, auto rules and the song guard work,
    and staged clips fade their audio edges."""
    import numpy as np

    from .sfx import _decode

    project.config["audio"]["attachSongs"] = False
    project.config["sfx"].update(auto=True, autoRules={"transition": "selftest-blip", "payoff": "selftest-blip", "textBeat": None,
                                                       "cameraMove": None, "payoffRiser": None, "punchIn": None})  # fmt: skip
    plan = {
        "slug": "selftest-sound",
        "segments": [
            {"asset": land["id"], "in": 0.5, "out": 3.5, "nat": {"at": 1.0, "for": 1.0}},
            {"asset": vert["id"], "in": 0.5, "out": 2.5, "transition": "fade"},
            {"asset": photo["id"], "duration": 2.5, "payoff": True},
        ],
        "music": {"track": "selftest-beat"},
        "musicDrops": [{"at": 4.0, "for": 0.5}],
        "sfx": [{"sound": "selftest-blip", "at": 6.2, "gain": 1.0}],
        "post": {"caption": "sound test", "hashtags": ["#cycling"]},
    }
    problems = []

    def built(**over) -> dict:
        return build(project, {**plan, **over})

    b = built()
    srcs = [c["source"] for c in b["sound"]["cues"]]
    if srcs != ["auto: transition", "auto: payoff", "plan"]:
        problems.append(f"expected cues auto transition, auto payoff, plan — got {srcs}")
    if [c["source"] for c in built(autoSfx=False)["sound"]["cues"]] != ["plan"]:
        problems.append("autoSfx: false should leave only the plan's own cue")
    near = built(sfx=[{"sound": "selftest-tone", "at": 4.8}])["sound"]["cues"]  # payoff starts at 4.73 s
    if [c["source"] for c in near] != ["auto: transition", "plan"]:
        problems.append(f"a manual cue within 0.3 s of an auto cue should replace it, got {[c['source'] for c in near]}")
    if any("recognizes" in w for w in b["warnings"]) or not any("recognizes" in w for w in built(segments=[dict(plan["segments"][0], nat=True)] + plan["segments"][1:])["warnings"]):
        problems.append("the trending-song guard should warn only when nat + drops cover over 25% of the reel")
    try:
        built(segments=[dict(plan["segments"][0], speed=3)] + plan["segments"][1:])
        problems.append("nat on a clip sped up 3x (no audio) should be refused")
    except ReelsError:
        pass
    try:
        built(sfx=[{"sound": "selftest-blop", "at": 1}])
        problems.append("an unknown sound effect should be refused")
    except ReelsError as e:
        if "selftest-blip" not in str(e):
            problems.append(f"unknown sound error should list close matches: {e}")

    pp = project.plans_dir / "selftest-sound.json"
    write_json(pp, plan)
    reel = project.abs(render_plan(project, pp, keep_staging=True)["file"])
    ride = "bandpass=f=330:width_type=q:w=4,bandpass=f=330:width_type=q:w=4"
    kick = "lowpass=f=100,lowpass=f=100"
    blip = "bandpass=f=880:width_type=q:w=4,bandpass=f=880:width_type=q:w=4"
    nat_up = _band(reel, 1.1, 1.9, ride)[0] - _band(reel, 0.1, 0.8, ride)[0]
    dropped = _band(reel, 3.4, 3.9, kick)[0] - _band(reel, 4.03, 4.47, kick)[0]
    cue = _band(reel, 6.18, 6.5, blip)[1] - _band(reel, 5.6, 6.1, blip)[1]
    if nat_up < 10:
        problems.append(f"nat window should lift the ride audio ~16 dB, got {nat_up:+.1f} dB")
    if dropped < 25:
        problems.append(f"music drop should silence the song, only {dropped:.1f} dB down")
    if cue < 15:
        problems.append(f"the 6.2 s blip should stand out, only {cue:+.1f} dB")
    x, _ = _decode(project.staging_dir / "selftest-sound" / "seg0.mp4")
    edge = float(np.abs(x[:24]).max()), float(np.abs(x[-24:]).max()), float(np.abs(x[4800:9600]).max())
    if max(edge[:2]) > 0.1 * edge[2]:
        problems.append(f"staged clip audio should fade in/out at its edges (first/last 0.5 ms peak {edge[0]:.3f}/{edge[1]:.3f} vs {edge[2]:.3f})")
    if problems:
        raise ReelsError("sound design check failed:\n  - " + "\n  - ".join(problems))
    return f"sound: nat {nat_up:+.0f} dB ride audio, drop -{dropped:.0f} dB song, blip +{cue:.0f} dB at its cue, edges faded, auto cues + guard ok"


def _check_punch_loop(project: Project, land: dict, vert: dict, photo: dict) -> str:
    """Punch-ins (manual, transcript words, on beats) become template keyframes and swish cues; a
    swap-halves loop snaps to whole bars, drops the fades and end card, and keeps the song full to the end."""
    from types import SimpleNamespace

    from .punch import stage_scale
    from .render import discard

    project.config["audio"]["attachSongs"] = False
    project.config["sfx"].update(auto=True, autoSpacing=0.5, autoMax=8, autoRules={"punchIn": "selftest-blip"})
    problems = []
    plan = {
        "slug": "selftest-punch",
        "segments": [
            {"asset": land["id"], "in": 0.5, "out": 3.5, "punch": [{"at": 1.0, "scale": 1.2, "for": 0.5}], "text": [{"text": "stays put", "at": 0.2}]},
            {"asset": land["id"], "in": 1.5, "out": 6.0, "captions": "transcript", "punch": {"words": ["climb", "go"]}},
            {"asset": vert["id"], "in": 0.2, "out": 3.0},
        ],
        "music": {"track": "selftest-beat", "start": 0.25, "cutOnBeat": True},
        "punchOnBeats": {"every": 2, "scale": 1.1},
    }
    b = build(project, plan)
    segs, props = b["segs"], b["props"]["segments"]
    want = [[0, 1.0, 0.5, 0.5], [30, 1.0, 0.5, 0.5], [34, 1.2, 0.5, 0.5], [45, 1.2, 0.5, 0.5], [51, 1.0, 0.5, 0.5]]
    if props[0].get("punch", [])[:5] != want:
        problems.append(f"manual punch keyframes: expected {want}, got {props[0].get('punch')}")
    climb = segs[1].start + round((2.3 - 1.5) * 30)
    if [p["start"] for p in segs[1].spec["_punches"]][:1] != [climb]:
        problems.append(f"word punch on 'climb' should start at frame {climb}, got {segs[1].spec['_punches']}")
    beat = segs[2].spec["_punches"]
    if not beat or any(p["scale"] != 1.1 for p in beat) or (len(beat) > 1 and beat[0]["end"] is None):
        problems.append(f"punchOnBeats should alternate 1.1x punches in and out on the vertical clip, got {beat}")
    cues = [c for c in b["sound"]["cues"] if c["source"] == "auto: punchIn"]
    if not cues or cues[0]["startFrame"] != 30 or any(segs[1].start <= c["startFrame"] < segs[1].end for c in cues):
        problems.append(f"punchIn swishes should start with each punch (first at frame 30), none over the captions: {[c['startFrame'] for c in cues]}")
    if b["props"]["beats"][0]["startFrame"] != 6 or any("punch" in x for x in b["props"]["beats"]):
        problems.append("text beats keep their timing and never get punch keyframes")
    try:
        build(project, {**plan, "segments": [{"asset": photo["id"], "duration": 2, "punch": [{"at": 0.5}]}]})
        problems.append("a punch on a photo should be refused")
    except ReelsError:
        pass
    four_k = SimpleNamespace(asset={"probe": {"width": 2160, "height": 3840}}, spec={"frame": "crop", "zoom": 1.0, "_punches": [{"scale": 1.3}]})
    hd = SimpleNamespace(asset={"probe": {"width": 1920, "height": 1080}}, spec={"frame": "crop", "zoom": 1.0, "_punches": [{"scale": 1.3}]})
    if (stage_scale(four_k, 1080, 1920, 1.4), stage_scale(hd, 1080, 1920, 1.4), stage_scale(four_k, 1080, 1920, 1)) != (1.3, 1.0, 1.0):
        problems.append("hi-res staging: 4K portrait should stage at the punch scale, 1080p sources and punchStageScale 1 at 1x")

    plain = build(project, {**plan, "segments": [dict(x, punch=None) for x in plan["segments"]], "punchOnBeats": None})
    if any("punch" in x for x in plain["props"]["segments"]) or "fadeInFrames" in (plain["props"]["music"] or {}):
        problems.append("reels without punches or loop must not get the new prop fields")

    loop = {
        "slug": "selftest-loop", "loop": True, "endCard": True,
        "segments": [{"asset": land["id"], "in": 3.0, "out": 5.0}, {"asset": vert["id"], "in": 0.5, "out": 2.0}, {"asset": land["id"], "in": 1.0, "out": 3.0}],
        "music": {"track": "selftest-beat", "start": 0.25, "cutOnBeat": True},
        "post": {"caption": "loop test", "hashtags": ["#cycling"]},
    }  # fmt: skip
    lb = build(project, loop)
    m = lb["props"]["music"]
    if abs(lb["total"] - 60 * round(lb["total"] / 60)) > 1 or lb["props"]["endCard"] or (m["fadeInFrames"], m["fadeOutFrames"]) != (0, 0):
        problems.append(f"loop: expected whole 2 s bars, no end card, no music fades — got {lb['total']} frames, {lb['props']['endCard']}, {m}")
    if lb["segs"][-1].spec["out"] != 3.0 or not lb["loop"]["swap"][0] or not any("end card" in w for w in lb["warnings"]):
        problems.append(f"loop: the swap-halves seam (last out = 3.0) should be kept and detected: {lb['segs'][-1].spec}, {lb['loop']}")
    pp = project.plans_dir / "selftest-loop.json"
    write_json(pp, loop)
    post = render_plan(project, pp)
    reel = project.abs(post["file"])
    dur = float(ffprobe_json(reel)["format"]["duration"])
    kick = "lowpass=f=100,lowpass=f=100"
    tail = _band(reel, dur - 0.55, dur, kick)[0] - _band(reel, dur - 2.55, dur - 2.0, kick)[0]
    if tail < -1.5:
        problems.append(f"loop: the song should play at full level to the last frame, the last kick is {tail:+.1f} dB down")
    if not (post.get("loop") or {}).get("swapHalves") or "seamSsim" not in post["loop"]:
        problems.append(f"loop: post.json should carry the measured seam, got {post.get('loop')}")
    if "selftest-loop" not in (read_json(project.music_dir / "selftest-beat.json").get("usedIn") or []):
        problems.append("render should record the song's usage")
    discard(project, "selftest-loop")
    if "selftest-loop" in (read_json(project.music_dir / "selftest-beat.json").get("usedIn") or []):
        problems.append("discard should free the song's usage")
    if problems:
        raise ReelsError("punch / loop check failed:\n  - " + "\n  - ".join(problems))
    return (f"punch: keyframes, word + beat punches, {len(cues)} swish cue(s); loop: {lb['total'] / 30:.0f} s = whole bars, "
            f"last kick {tail:+.1f} dB, seam SSIM {post['loop']['seamSsim']:.2f}, discard frees the song")  # fmt: skip


def selftest(project: Project, *, keep: bool = False) -> bool:
    """End-to-end check in a throwaway project folder."""
    tmp = Path(tempfile.mkdtemp(prefix="reels-selftest-"))
    passed = False
    server = None
    t0 = time.time()
    try:
        cfg = deepcopy(project.config)
        cfg["brand"]["handle"] = "@selftest"
        cfg["ingest"]["moveOriginals"] = True
        cfg["audio"]["attachSongs"] = False  # step 8 switches it on
        cfg["sfx"]["auto"] = False  # the sound check turns it on with test sounds
        write_json(tmp / "config.json", cfg)
        (tmp / "remotion").symlink_to(project.remotion_dir, target_is_directory=True)
        test = Project(tmp)
        test.ensure_dirs()
        served = tmp / "served"
        served.mkdir()

        log("1/9 making throwaway test clips, a photo, a GoPro-style .360 and test music…")
        _test_fixtures(tmp, served)
        pattern = _gopro_fixture(tmp, tmp / "inbox")

        log("2/9 ingesting…")
        new = ingest(test)
        by_name = {a["originalName"]: a for a in new}
        if len(by_name) != 4:
            raise ReelsError(f"expected 4 new assets, got {len(by_name)}")
        land, vert, photo = by_name["ride-landscape.mp4"], by_name["ride-vertical.mp4"], by_name["photo.jpg"]
        s360 = by_name["GS019999.360"]
        for a in (land, vert, s360):
            if not a.get("sheets"):
                raise ReelsError(f"no contact sheet for {a['id']}")
        audio = land.get("audio") or {}
        if not isinstance(audio.get("speechSeconds"), (int, float)):
            raise ReelsError(f"speech detection didn't run: {audio.get('speechError') or 'is faster-whisper installed?'}")
        from .sounds import draw as draw_sounds
        from .sounds import load_cache as load_sounds

        snd = load_sounds(test, land["id"])  # sound candidates are made on ingest
        if snd is None or "timeline" not in snd or not draw_sounds(test, land, snd)[0].is_file():
            raise ReelsError("sound detection didn't run on ingest (library/sounds/) — see models/yamnet/README.md")

        log("3/9 checking the 360 decoder (seams, geometry)…")
        sph = s360["probe"].get("sphere") or {}
        if sph.get("projection") != "gopro-eac" or (s360["probe"]["width"], s360["probe"]["height"]) != (5376, 2688):
            raise ReelsError(f".360 not recognised as GoPro 360: {s360['probe']}")
        recon = tmp / "sphere-recon.png"
        extract_equirect(test.abs(s360["media"]), 1.0, recon, s360["probe"], width=2048)
        quality = _psnr(recon, pattern)
        if quality < 27:
            raise ReelsError(f"360 reconstruction is off (PSNR {quality:.1f} dB, expected ≥ 27)")
        log(f"    sphere rebuilt from the two tracks at {quality:.1f} dB PSNR")

        log("4/9 tagging and adding a transcript…")
        tag_sets = {
            land["id"]: {"type": "climb", "shot": "pov", "quality": 4, "summary": "test pattern stand-in for a climb",
                         "moments": [{"in": 1.5, "out": 6.0, "label": "test climb", "score": 4, "focusX": 0.4, "speech": True}],
                         "sounds": [{"at": 1.0, "for": 1.5, "kind": "freehub", "score": 3, "note": "test tone stand-in"}]},
            vert["id"]: {"type": "descent", "shot": "pov", "quality": 4, "summary": "vertical test pattern",
                         "moments": [{"in": 0.5, "out": 2.5, "label": "test descent", "score": 4}]},
            photo["id"]: {"type": "scenery", "quality": 4, "summary": "gradient sky", "focusX": 0.5, "focusY": 0.5},
            s360["id"]: {"type": "solo-ride", "shot": "pov", "quality": 4, "summary": "360 test sphere",
                         "angles": {"road": {"yaw": 0}, "rider": {"yaw": 180, "fov": 70}},
                         "moments": [{"in": 0.2, "out": 2.2, "label": "look back test", "score": 4, "view": "road"}]},
        }  # fmt: skip
        for a in (land, vert, photo, s360):
            a["tags"] = validate_tags(a, tag_sets[a["id"]])
            a["status"] = "tagged"
        words = [("This", 2.0, 2.3), ("climb", 2.3, 2.7), ("is", 2.75, 2.9), ("going", 2.9, 3.2), ("to", 3.2, 3.3),
                 ("hurt,", 3.3, 3.8), ("let's", 4.2, 4.5), ("go.", 4.5, 4.9)]  # fmt: skip
        tr = test.transcripts_dir / f"{land['id']}.json"
        write_json(tr, {"asset": land["id"], "segments": [], "words": [{"w": w, "s": s, "e": e, "p": 0.99} for w, s, e in words]})
        land["transcript"] = test.rel(tr)
        for a in (land, vert, photo, s360):
            save_asset(test, a)

        log("5/9 downloading a track through yt-dlp (from a local test server) and finding beats…")
        server, base = _serve(served)
        track = add_track(test, f"{base}/selftest-tone.m4a", name="selftest-track", credit="Music: selftest tone")
        if not test.abs(track["file"]).is_file() or not track["file"].endswith(".mp3"):
            raise ReelsError(f"music download didn't produce an mp3: {track}")
        if not 4 <= float(track["suggestedStart"]) <= 8:
            raise ReelsError(f"auto start should skip the 6 s quiet intro, got {track['suggestedStart']}s")
        beat = add_track(test, str(tmp / "selftest-beat.mp3"), name="selftest-beat", instagram="Selftest Song Selftest Band")
        found = beat.get("beats") or []
        err = sorted(abs(b - (0.5 + round((b - 0.5) / 0.5) * 0.5)) for b in found)
        if abs(float(beat.get("bpm") or 0) - 120) > 1.5 or len(found) < 40 or err[len(err) // 2] > 0.02:
            raise ReelsError(f"beat detection is off on a 120 bpm kick: {beat.get('bpm')} bpm, {len(found)} beats, median error {err[len(err) // 2] if err else '?'}s")
        log(f"    {beat['bpm']:g} bpm found, beats within {err[len(err) // 2] * 1000:.0f} ms (median) of the true ones")

        log("6/9 rendering a reel (this is the slow part)…")
        plan = {
            "slug": "selftest-reel",
            "hook": {"text": "pov: the self-test actually passed"},
            "segments": [
                {"asset": land["id"], "moment": 0, "captions": "transcript", "stats": [{"label": "grade", "value": "8%"}]},
                {"asset": vert["id"], "moment": 0, "speed": 0.5, "transition": "fade",
                 "text": [{"text": "legs said no 💀", "at": 0.3, "style": "dark"}]},
                {"asset": photo["id"], "duration": 2.5, "fit": "blurfill", "motion": "zoom-out", "transition": "fade",
                 "text": [{"text": "worth it tho", "at": 0.3, "style": "plain", "position": "center"}]},
                {"asset": land["id"], "in": 6.0, "out": 7.9, "frame": "blurfill"},
                {"asset": s360["id"], "moment": 0, "path": [{"at": 0, "view": "road"}, {"at": 1.6, "view": "rider"}]},
            ],
            "music": {"track": "selftest-track", "volume": 0.6},
            "post": {"caption": "self-test reel", "hashtags": ["#cycling"]},
        }
        write_json(test.plans_dir / "selftest-reel.json", plan)
        post = render_plan(test, test.plans_dir / "selftest-reel.json")

        log("7/9 checking the output (Instagram spec, 360 camera move, credit line, footage guard)…")
        reel = test.abs(post["file"])
        info = ffprobe_json(reel)
        v = next(s for s in info["streams"] if s["codec_type"] == "video")
        a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
        problems = []
        if v["codec_name"] != "h264" or v["pix_fmt"] != "yuv420p":
            problems.append(f"video is {v['codec_name']}/{v['pix_fmt']}, expected h264/yuv420p")
        if (v["width"], v["height"]) != (1080, 1920):
            problems.append(f"video is {v['width']}x{v['height']}, expected 1080x1920")
        if v.get("r_frame_rate") not in ("30/1", "30000/1000"):
            problems.append(f"frame rate {v.get('r_frame_rate')}, expected 30")
        if a is None:
            problems.append("no audio track")
        elif a["codec_name"] != "aac" or int(a.get("sample_rate", 0)) != 48000 or int(a.get("bit_rate", 0)) > 140_000:
            problems.append(f"audio is {a['codec_name']} {a.get('sample_rate')} Hz {a.get('bit_rate')} bps")
        expected = (4.5 + 4.0 + 2.5 + 1.9 + 2.0) - 2 * 8 / 30
        duration = float(info["format"]["duration"])
        if abs(duration - expected) > 0.25:
            problems.append(f"duration {duration:.2f}s, expected ~{expected:.2f}s")
        # The last shot is the 360 clip: it should start on the road (front = red panel)
        # and end looking back at the rider (behind = blue panel).
        vdur = float(v.get("duration") or duration)
        for at, want in ((vdur - 2.0 + 0.1, "red"), (vdur - 0.15, "blue")):
            seen = _panel_at(reel, at, tmp)
            if seen != want:
                problems.append(f"360 camera move: expected the {want} panel at {at:.2f}s, saw {seen}")
        for f in ("cover.jpg", "preview.jpg", "post.json", "caption.txt"):
            if not (reel.parent / f).exists():
                problems.append(f"missing {f}")
        if "Music: selftest tone" not in (reel.parent / "caption.txt").read_text(encoding="utf-8"):
            problems.append("music credit missing from caption.txt")
        # Footage guard: a changed library file must be refused.
        media = test.abs(vert["media"])
        original = media.read_bytes()
        media.write_bytes(original + b"\0")
        try:
            check_plan(test, test.plans_dir / "selftest-reel.json")
            problems.append("a modified library file was accepted")
        except ReelsError:
            pass
        finally:
            media.write_bytes(original)
        if problems:
            raise ReelsError("output check failed:\n  - " + "\n  - ".join(problems))

        log("8/9 rendering beat-synced reels with a trending song: mixed in (default), then attached by Instagram…")
        plan2 = {
            "slug": "selftest-beats",
            "segments": [
                {"asset": vert["id"], "in": 0.2, "out": 1.6},
                {"asset": photo["id"], "duration": 1.3, "transition": "fade"},
                {"asset": land["id"], "in": 1.0, "out": 2.9},
            ],
            "music": {"track": "selftest-beat", "start": 0.25, "cutOnBeat": True},
            "post": {"caption": "beat test", "hashtags": ["#cycling"]},
        }
        write_json(test.plans_dir / "selftest-beats.json", plan2)
        cuts = (check_plan(test, test.plans_dir / "selftest-beats.json")["music"].get("beatSync") or {}).get("cuts") or []
        post2 = render_plan(test, test.plans_dir / "selftest-beats.json")
        # 0.25 s into the track, its beats fall at 0.25 s + n × 0.5 s of reel time.
        off = [abs(c - 0.25 - round((c - 0.25) / 0.5) * 0.5) for c in cuts]
        if len(cuts) != 3 or max(off) > 1.5 / 30:
            problems.append(f"cuts should land on the song's beats, 0.25 s + n × 0.5 s (the reel end too), got {cuts}")
        if post2.get("instagramAudio") or post2.get("previewVideo") or post2.get("musicCredit"):
            problems.append("a mixed-in trending song shouldn't get attach data, a preview.mp4 or a caption credit")
        baked = _low_band_db(test.abs(post2["file"]))

        test.config["audio"]["attachSongs"] = True
        plan2.update(slug="selftest-attach", music={"track": "selftest-beat", "cutOnBeat": True})
        write_json(test.plans_dir / "selftest-attach.json", plan2)
        post3 = render_plan(test, test.plans_dir / "selftest-attach.json")
        reel3, preview3 = test.abs(post3["file"]), test.abs(post3.get("previewVideo") or "missing.mp4")
        ig = post3.get("instagramAudio") or {}
        if ig.get("audio") != "Selftest Song Selftest Band" or ig.get("audioVolume") != 100 or ig.get("manual"):
            problems.append(f"post.json instagramAudio is wrong: {ig}")
        if post3.get("musicCredit"):
            problems.append("a reel with Instagram audio shouldn't add a credit line to the caption")
        if not preview3.exists():
            problems.append("no preview.mp4 for the Instagram-audio reel")
        else:
            clean, mixed = _low_band_db(reel3), _low_band_db(preview3)
            if min(baked, mixed) - clean < 15:
                problems.append(
                    "the song should be in the mixed-in reel and the attach preview, not the attach upload "
                    f"(low band: mixed-in {baked:.0f} dB, attach upload {clean:.0f} dB, preview {mixed:.0f} dB)"
                )
        if problems:
            raise ReelsError("beat-sync / trending song check failed:\n  - " + "\n  - ".join(problems))
        log(f"    cuts at {', '.join(f'{c:.2f}s' for c in cuts)} · song in reel.mp4; attached mode: only in preview.mp4")

        log("9/9 checking speech detection + Whisper on a spoken test line…")
        log(f"    {_check_transcription(test, tmp)}")
        log(f"    {_check_stats(test)}")
        log(f"    {_check_sfx(test, tmp)}")
        log(f"    {_check_sound(test, land, vert, photo)}")
        log(f"    {_check_punch_loop(test, land, vert, photo)}")
        passed = True
        log(f"\nPASS ✓  ({time.time() - t0:.0f}s) — preview: {reel.parent / 'preview.jpg'}")
        return True
    finally:
        if server:
            server.shutdown()
        if passed and not keep:
            shutil.rmtree(tmp, ignore_errors=True)
        else:
            log(f"test folder kept at {tmp}")
