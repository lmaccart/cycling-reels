"""A closer look at a rendered reel, for whoever reviews it from stills and numbers rather than
by watching: a dense strip of frames (every half second, plus the frames where something
changes — cuts, text, exhibits) and a sound report (the mix over time; the voice and the ride
sound against the music under them)."""

from __future__ import annotations

import math
import re
import tempfile
from pathlib import Path
from typing import Any

from PIL import Image

from .config import Project
from .media import probe_video, require, run
from .sheets import build_sheet
from .util import ReelsError, log, read_json, warn

PER_SHEET, COLS, TILE_W = 24, 6, 300
NEAR = 3  # frames: an event this close to a regular sample is shown on that sample
VOICE_TIGHT, VOICE_LOW = 10.0, 6.0  # dB the voice sits over the music under it: below = tight / crowded
NAT_LOW = 3.0  # dB a ride-sound moment should sit over its music
PEAK_MAX = -0.5  # dBTP (the encoder aims for -1; AAC overshoots a little)
QUIET_UNDER = 12.0  # LU under the reel's loudness, for a second or more = a hole in the mix


def locate(project: Project, target: str) -> tuple[str, Path, Path | None]:
    """(slug, reel.mp4, plan.json or None) for a slug, a plan file or a rendered file."""
    t = Path(target)
    if t.suffix.lower() == ".mp4" and project.abs(t).exists():
        reel = project.abs(t).resolve()
        slug = reel.parent.name
    else:
        slug = read_json(project.abs(t)).get("slug") or t.stem if t.suffix.lower() == ".json" and project.abs(t).exists() else t.name
        reel = project.out_dir / slug / "reel.mp4"
    if not reel.exists():
        raise ReelsError(f"no rendered reel for {target!r} — render it first (looked for {project.rel(reel)})")
    plan = reel.parent / "plan.json"
    return slug, reel, plan if plan.exists() else None


def rebuild(project: Project, plan_path: Path | None) -> dict | None:
    """The reel's timeline as the renderer built it (from the plan copy saved with the render)."""
    if plan_path is None:
        return None
    from .render import build, load_plan

    try:
        return build(project, load_plan(project, plan_path))
    except ReelsError as e:
        warn(f"couldn't rebuild the timeline from {project.rel(plan_path)} (the strip has no event frames, the sound report no stems): "
             f"{str(e).splitlines()[0]}")  # fmt: skip
        return None


# --------------------------------------------------------------------------
# Frames
# --------------------------------------------------------------------------


def events(built: dict) -> list[tuple[int, str]]:
    """(reel frame, tag) just after each thing that changes the picture."""
    props, out = built["props"], []
    for sg in built["segs"]:
        if sg.index:
            out.append((sg.start + 2, f"cut {sg.index + 1}"))
        for c in sg.spec.get("_cutaways") or []:
            out.append((sg.start + c["start"] + 2, "cutaway"))
            out.append((sg.start + c["start"] + c["seg"].frames + 2, "back"))
        for p in sg.spec.get("_punches") or []:
            out.append((p["start"] + 5, "punch"))
    if props.get("hook"):
        out.append((max(0, props["hook"]["endFrame"] - 6), "hook"))
    out += [(b["startFrame"] + 6, "text") for b in props.get("beats") or []]
    out += [(e["startFrame"] + 10, "exhibit") for e in props.get("exhibits") or []]
    out += [(s["startFrame"] + 6, "stat") for s in props.get("stats") or []]
    if props.get("endCard"):
        out.append((props["endCard"]["startFrame"] + 6, "end card"))
    return sorted(out)


def sample(total: int, fps: float, every: float, marks: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Frames to show: one every `every` seconds, the last frame, and the event frames — an event
    next to a regular sample is tagged onto it instead of adding a near-duplicate."""
    step = max(1, round(every * fps))
    picked: dict[int, list[str]] = {f: [] for f in range(0, total, step)}
    picked.setdefault(total - 1, [])
    for f, tag in marks:
        f = min(max(0, f), total - 1)
        near = min(picked, key=lambda g: (abs(g - f), g))
        target = near if abs(near - f) <= NEAR and near >= f else f
        tags = picked.setdefault(target, [])
        if tag not in tags:
            tags.append(tag)
    return [(f, " · ".join(tags)) for f, tags in sorted(picked.items())]


def strips(reel: Path, out_dir: Path, frames: list[tuple[int, str]], fps: float, title: str) -> list[Path]:
    for old in out_dir.glob("strip-*.jpg"):
        old.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="reels-look-") as td:
        pick = "+".join(f"eq(n\\,{f})" for f, _ in frames)
        run(["ffmpeg", "-v", "error", "-y", "-i", reel, "-vf", f"select='{pick}',scale={TILE_W * 2}:-2", "-fps_mode", "passthrough",
             "-q:v", "3", Path(td) / "%05d.jpg"])  # fmt: skip
        files = sorted(Path(td).glob("*.jpg"))
        tiles = []
        for (f, tag), fp in zip(frames, files):
            with Image.open(fp) as im:
                tiles.append((f"{f / fps:.2f}s" + (f"  {tag}" if tag else ""), im.convert("RGB")))
        made = []
        n = math.ceil(len(tiles) / PER_SHEET)
        for i in range(n):
            part = tiles[i * PER_SHEET : (i + 1) * PER_SHEET]
            out = out_dir / f"strip-{i + 1:02d}.jpg"
            build_sheet(part, out, f"{title}  ·  {part[0][0].split()[0]}–{part[-1][0].split()[0]}  ({i + 1}/{n})", cols=COLS, tile_w=TILE_W)
            made.append(out)
    return made


def timeline(built: dict) -> list[str]:
    """Cuts as numbers: where each shot starts, how long it holds and how far the cut is from a beat."""
    fps, music = built["fps"], built["music"] or {}
    beats = music.get("beats")
    if beats is None and (music.get("meta") or {}).get("beats"):
        beats = [b - music["start"] for b in music["meta"]["beats"]]
    out = []
    for sg in built["segs"]:
        t = sg.start / fps
        line = f"  {sg.index + 1:>2}  {t:6.2f}s  holds {sg.frames / fps:4.1f}s  {sg.asset['id']}"
        if beats and sg.index:
            off = min((t - b for b in beats), key=abs)
            line += f"   cut is {abs(off) * 1000:.0f} ms {'after' if off > 0 else 'before'} a beat" if abs(off) >= 0.04 else "   cut on the beat"
        out.append(line)
    return out


# --------------------------------------------------------------------------
# Sound
# --------------------------------------------------------------------------


def mean_db(src: Path, start: float, seconds: float, filters: str = "") -> float | None:
    """Level of a stretch of a file's audio: gated loudness (LUFS — pauses between words don't
    drag a voice down), or the plain mean (dBFS) for stretches too short or quiet to gate.
    None when there's no audio."""
    chain = ",".join(f for f in (filters, "ebur128", "volumedetect") if f)
    proc = run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-ss", f"{max(0.0, start):.3f}", "-t", f"{seconds:.3f}", "-i", src,
                "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", chain, "-f", "null", "-"], check=False)  # fmt: skip  (the stream the renderer uses)
    text = proc.stderr or ""
    gated = re.findall(r"^\s+I:\s*(-?[0-9.]+) LUFS", text, flags=re.M)
    if gated and float(gated[-1]) > -69:
        return float(gated[-1])
    m = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", text)
    return float(m.group(1)) if m else None


def _gain_db(g: float) -> float:
    return 20 * math.log10(g) if g > 0 else -120.0


def mix_stats(reel: Path) -> dict | None:
    """Loudness of the finished file: integrated LUFS, true peak, and momentary loudness per second."""
    proc = run(["ffmpeg", "-nostdin", "-hide_banner", "-nostats", "-i", reel, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"], check=False)
    text = proc.stderr or ""
    rows = re.findall(r"t:\s*([0-9.]+)\s+TARGET.*?M:\s*(-?[0-9.]+|-inf)", text)
    integ = re.findall(r"I:\s*(-?[0-9.]+) LUFS", text)
    peak = re.findall(r"Peak:\s*(-?[0-9.]+) dBFS", text)
    if not integ:
        return None
    per: dict[int, float] = {}
    for t, m in rows:
        if m not in ("-inf",) and float(m) > -100:
            per[int(float(t))] = max(per.get(int(float(t)), -200.0), float(m))
    return {"lufs": float(integ[-1]), "peak": float(peak[-1]) if peak else None, "seconds": per}


def _runs(ranges: list[list[int]] | list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(int(s), int(e)) for s, e in ranges if e > s]


def pieces(built: dict) -> list[dict]:
    """Stretches of the reel to compare against the music: speech (the captioned runs), nat
    (ride-sound moments) and open (everything else at least a second long), each cut at
    segment boundaries. {"kind", "startFrame", "endFrame", "seg", "gain", "music" (× music volume)}."""
    props, segs = built["props"], built["segs"]
    music = props.get("music") or {}
    nat = built["sound"]["nat"]
    speech = _runs(music.get("duckRanges") or [])
    if not speech:  # no music: still report the spoken runs
        from .render import _duck_ranges

        speech = _runs(_duck_ranges(props.get("captions") or []))
    drops = [(d["startFrame"], d["endFrame"]) for d in built["sound"]["drops"]]
    out: list[dict] = []

    def add(kind: str, s: int, e: int, gain_of, music_factor: float) -> None:
        for sg in segs:
            a, b = max(s, sg.start), min(e, sg.end)
            if b - a < 6 or sg.kind != "video":
                continue
            out.append({"kind": kind, "startFrame": a, "endFrame": b, "seg": sg.index, "gain": gain_of(sg), "music": music_factor})

    vol = {i: float(p.get("volume") or 0) for i, p in enumerate(props["segments"])}
    for w in nat:
        add("nat", w["startFrame"], w["endFrame"], lambda sg, w=w: float(w["volume"]), float(w["music"]))
    for s, e in speech:
        factor = float(music.get("duckVolume", 1.0))
        for w in nat:
            if w["startFrame"] < e and s < w["endFrame"]:
                factor = min(factor, float(w["music"]))
        add("speech", s, e, lambda sg: vol[sg.index], factor)
    busy = sorted([(p["startFrame"], p["endFrame"]) for p in out] + drops)
    at = 0
    for s, e in busy + [(built["total"], built["total"])]:
        if s - at >= built["fps"]:
            add("open", at, s, lambda sg: vol[sg.index], 1.0)
        at = max(at, e)
    return sorted(out, key=lambda p: (p["startFrame"], p["kind"]))


def stems(project: Project, built: dict) -> list[dict]:
    """Each piece with the level of its own sound and of the music under it, measured on the
    source files at the gains the mix uses (dB before the final loudness pass, which moves
    both together). "over" = how far the clip's sound sits above the music."""
    from .mix import CLEAN_AF

    fps, music, mp = built["fps"], built["props"].get("music"), None
    if music and built["music"]:
        mp = built["music"]["path"]
    out = []
    for p in pieces(built):
        sg = built["segs"][p["seg"]]
        row = dict(p, clip=None, song=None, over=None)
        speed = sg.spec["speed"]
        if sg.asset["probe"].get("hasAudio") and 0.5 <= speed <= 2.0 and p["gain"] > 0:
            a = sg.spec["in"] + (p["startFrame"] - sg.start) / fps * speed
            clean = bool(sg.spec.get("clean") or (built["sound"]["cleanNat"] and sg.spec.get("nat")))
            lvl = mean_db(project.abs(sg.asset["media"]), a, (p["endFrame"] - p["startFrame"]) / fps * speed, CLEAN_AF if clean else "")
            if lvl is not None:
                row["clip"] = round(lvl + _gain_db(p["gain"]), 1)
        if mp is not None and music["volume"] * p["music"] > 0:
            a = (music["trimBefore"] + p["startFrame"]) / fps
            lvl = mean_db(mp, a, (p["endFrame"] - p["startFrame"]) / fps)
            if lvl is not None:
                row["song"] = round(lvl + _gain_db(music["volume"] * p["music"]), 1)
        if row["clip"] is not None and row["song"] is not None:
            row["over"] = round(row["clip"] - row["song"], 1)
        out.append(row)
    return out


def judge(row: dict) -> str:
    """One word on a piece's balance, or '' when it's fine or can't be judged."""
    if row["over"] is None:
        return ""
    if row["kind"] == "speech":
        return "MUSIC CROWDS THE VOICE" if row["over"] < VOICE_LOW else "tight" if row["over"] < VOICE_TIGHT else ""
    if row["kind"] == "nat":
        return "RIDE SOUND UNDER THE MUSIC" if row["over"] < NAT_LOW else ""
    return ""


def sound_report(project: Project, reel: Path, built: dict | None) -> tuple[list[str], list[str]]:
    """(lines, flags) — the mix as rendered, then the voice / ride sound against the music."""
    lines, flags = [], []
    mix = mix_stats(reel)
    if mix is None:
        return ["  sound: no audio could be measured in the file"], ["no audio measured"]
    peak = "" if mix["peak"] is None else f", true peak {mix['peak']:.1f} dB"
    lines.append(f"  mix: {mix['lufs']:.1f} LUFS{peak}")
    if mix["peak"] is not None and mix["peak"] > PEAK_MAX:
        flags.append(f"true peak {mix['peak']:.1f} dB is over {PEAK_MAX:g} — it may clip after Instagram re-encodes")
    secs = mix["seconds"]
    if secs:
        row = "  ".join(f"{t}s {v:.0f}" for t, v in sorted(secs.items()))
        lines.append(f"  loudest moment of each second (LUFS): {row}")
        quiet = [t for t, v in sorted(secs.items()) if v < mix["lufs"] - QUIET_UNDER]
        drops = [(d["startFrame"], d["endFrame"]) for d in (built["sound"]["drops"] if built else [])]
        fps = built["fps"] if built else 30
        quiet = [t for t in quiet if not any(s / fps - 1 <= t <= e / fps for s, e in drops)]
        if quiet:
            flags.append(f"the mix goes quiet at {', '.join(f'{t}s' for t in quiet[:8])} (over {QUIET_UNDER:g} LU under the rest) and it isn't a planned drop")
    if built is None:
        return lines, flags
    music = built["music"]
    if music and music.get("instagram"):
        ig = music["instagram"]
        lines.append(f"  the song isn't in this file — Instagram adds it (song {ig['audioVolume']} / ride audio {ig['videoVolume']}); "
                     "the balance below can't be measured")  # fmt: skip
    elif not music:
        lines.append("  no music in this reel")
    rows = stems(project, built)
    fps = built["fps"]
    names = {"speech": "voice", "nat": "ride sound", "open": "between lines"}
    if rows:
        lines.append("  against the music (dB, measured on the sources at the mix's gains):")
    for r in rows:
        clip = "silent" if r["clip"] is None else f"{r['clip']:6.1f}"
        song = "  none" if r["song"] is None else f"{r['song']:6.1f}"
        over = "" if r["over"] is None else f"  {abs(r['over']):4.1f} dB {'over' if r['over'] >= 0 else 'UNDER'} the music"
        verdict = judge(r)
        lines.append(f"    {r['startFrame'] / fps:6.2f}-{r['endFrame'] / fps:6.2f}s  {names[r['kind']]:<13} clip {clip}  music {song}{over}"
                     + (f"   ← {verdict}" if verdict else ""))  # fmt: skip
        if verdict.isupper():
            what = "voice" if r["kind"] == "speech" else "ride sound"
            flags.append(f"{r['startFrame'] / fps:.1f}s: the {what} is {abs(r['over']):.1f} dB {'over' if r['over'] >= 0 else 'under'} the music playing with it "
                         f"(want {VOICE_LOW if r['kind'] == 'speech' else NAT_LOW:g}+ over) — raise the segment's \"volume\" or lower the music there")  # fmt: skip
        if r["kind"] == "speech" and r["clip"] is None:
            flags.append(f"{r['startFrame'] / fps:.1f}s: captions run here but segment {r['seg'] + 1}'s audio is silent or muted")
    return lines, flags


# --------------------------------------------------------------------------


def look(project: Project, target: str, *, every: float = 0.5, audio: bool = True, frames: bool = True) -> dict[str, Any]:
    require("ffmpeg")
    slug, reel, plan_path = locate(project, target)
    probe = probe_video(reel)
    fps = float(probe.get("fps") or 30)
    total = max(1, round(float(probe["duration"]) * fps))
    built = rebuild(project, plan_path)
    if built and abs(built["total"] - total) > 2:
        warn(f"the plan saved with this render now builds to {built['total'] / built['fps']:.2f}s but the file is {total / fps:.2f}s "
             "(footage, music or settings changed since) — event frames and stems may be off; re-render to be sure")  # fmt: skip
    out_dir = reel.parent / "look"
    report: list[str] = [f"{slug}  ·  {total / fps:.1f}s"]
    made: list[Path] = []
    if frames:
        marks = events(built) if built else []
        picked = sample(total, fps, every, marks)
        made = strips(reel, out_dir, picked, fps, slug)
        report.append(f"  frames: {len(picked)} (every {every:g}s + {sum(1 for _, t in picked if t)} at cuts/text/exhibits) → "
                      + ", ".join(project.rel(p) for p in made))  # fmt: skip
    if built:
        report.append("  shots:")
        report += ["  " + line for line in timeline(built)]
    flags: list[str] = []
    if audio:
        lines, flags = sound_report(project, reel, built)
        report += lines
    report.append("  flags: " + ("none" if not flags else ""))
    report += [f"    - {f}" for f in flags]
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")
    for line in report:
        log(line)
    return {"slug": slug, "strips": made, "flags": flags, "report": report}

