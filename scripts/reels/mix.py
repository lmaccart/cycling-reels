from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import Project
from .util import ReelsError, clamp

# Sound design on top of the music + ride-audio mix: ride-sound moments ("nat"), music
# drops, and sound-effect cues (manual and automatic). All frames are reel frames unless
# a name says otherwise.

NAT_RAMP = 6  # frames: ride audio up / music down around a nat window
DROP_DOWN, DROP_UP = 2, 1  # frames: music cut and hard return around a drop
CLEAN_AF = "highpass=f=90,afftdn=nr=8:nf=-40:tn=1"  # gentle wind/rumble cleanup for staged ride audio
EDGE_FADE = 0.03  # seconds of fade at each staged clip's audio edges (no pops on hard cuts)
NAT_MAX = 4.0  # ride audio up to +12 dB in a nat window (freehub ticks are quiet)
NAT_QUIET_DB = -36.0  # mean level (after volume) under which a nat sound gets lost under the music
RECOGNITION_MAX = 0.25  # share of a trending song that may be ducked/dropped before Instagram may not match it
REPLACE_WITHIN = 0.3  # seconds: a manual cue this close to an auto cue replaces it
AUTO_EVENTS = ("transition", "cameraMove", "textBeat", "payoff", "payoffRiser", "punchIn")
_PRIORITY = {"payoff": 0, "transition": 1, "cameraMove": 2, "punchIn": 2, "textBeat": 3}


def _f(v: Any, where: str, key: str, errors: list[str], lo: float, hi: float) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        errors.append(f"{where}: {key} must be a number")
        return None
    if not lo <= x <= hi:
        errors.append(f"{where}: {key}={x:g} is outside {lo:g}–{hi:g}")
        return None
    return x


def check_segment(spec: dict, asset: dict, errors: list[str], where: str) -> None:
    """Validates a segment's nat / clean / payoff / sfx fields and normalizes nat to a list of
    {"at", "for" (None = to the end), "volume", "music" (None = config)} windows."""
    raw = spec.get("nat")
    wins: list[dict] = []
    if raw is True:
        wins = [{"at": 0.0, "for": None, "volume": None, "music": None}]
    elif isinstance(raw, (dict, list)):
        for j, w in enumerate(raw if isinstance(raw, list) else [raw]):
            ww = f"{where} nat" + (f"[{j}]" if isinstance(raw, list) else "")
            if not isinstance(w, dict):
                errors.append(f"{ww}: use true or {{\"at\": 1.2, \"for\": 1.5}}")
                continue
            at = _f(w.get("at", 0), ww, "at", errors, 0, 600)
            dur = None if w.get("for") is None else _f(w["for"], ww, "for", errors, 0.2, 600)
            vol = None if w.get("volume") is None else _f(w["volume"], ww, "volume", errors, 0, NAT_MAX)
            mus = None if w.get("music") is None else _f(w["music"], ww, "music", errors, 0, 1)
            if at is not None:
                wins.append({"at": at, "for": dur, "volume": vol, "music": mus})
    elif raw not in (None, False):
        errors.append(f"{where}: nat must be true or {{\"at\": s, \"for\": s, \"volume\": v}}")
    if wins:
        if asset["kind"] != "video":
            errors.append(f"{where}: nat needs a video clip with ride audio (this is a photo)")
        elif not asset["probe"].get("hasAudio"):
            errors.append(f"{where}: nat — {asset['id']} has no audio track")
        elif not 0.5 <= float(spec.get("speed") or 1) <= 2.0:
            errors.append(f"{where}: nat — clips sped outside 0.5–2× have no audio (speed {spec.get('speed'):g})")
    spec["nat"] = wins
    for key in ("clean", "payoff"):
        if not isinstance(spec.get(key, False), bool):
            errors.append(f"{where}: {key} must be true or false")
    spec["clean"] = spec.get("clean") is True
    spec["payoff"] = spec.get("payoff") is True
    if spec.get("sfx") is not None and not isinstance(spec["sfx"], list):
        errors.append(f"{where}: sfx must be a list like [{{\"sound\": \"hit-soft\", \"at\": 0.2}}]")
        spec["sfx"] = []


def nat_windows(project: Project, segs: list, fps: int) -> list[dict]:
    """Nat windows in reel frames, clipped to their segment."""
    out = []
    default = float(project.cfg("audio", "natVolume"))
    music = float(project.cfg("audio", "natMusicVolume"))
    for sg in segs:
        for w in sg.spec.get("nat") or []:
            s = sg.start + round(w["at"] * fps)
            e = sg.end if w["for"] is None else s + round(w["for"] * fps)
            s, e = max(s, sg.start), min(e, sg.end)
            if e - s < 3:
                continue
            vol = default if w["volume"] is None else w["volume"]
            out.append({"seg": sg.index, "startFrame": s, "endFrame": e, "volume": vol, "music": music if w["music"] is None else w["music"]})
    return out


def quiet_nat(project: Project, segs: list, nat: list[dict], fps: int, warnings: list[str]) -> None:
    """Warns when a nat window's ride sound (as staged: cleaned, at its volume) would be lost under the music."""
    import math
    import re

    from .media import run

    for w in nat:
        sg = segs[w["seg"]]
        a = sg.spec["in"] + (w["startFrame"] - sg.start) / fps * sg.spec["speed"]
        t = (w["endFrame"] - w["startFrame"]) / fps * sg.spec["speed"]
        proc = run(["ffmpeg", "-nostdin", "-hide_banner", "-ss", f"{a:.3f}", "-t", f"{t:.3f}", "-i", project.abs(sg.asset["media"]),
                    "-map", "0:a:0", "-vn", "-sn", "-dn", "-af", f"{CLEAN_AF},volumedetect", "-f", "null", "-"], check=False)  # fmt: skip  (the stream the reel uses)
        m = re.search(r"mean_volume:\s*(-?[0-9.]+) dB", proc.stderr or "")
        if not m or w["volume"] <= 0:
            continue
        level = float(m.group(1)) + 20 * math.log10(w["volume"])
        if level < NAT_QUIET_DB:
            need = min(NAT_MAX, round(10 ** ((NAT_QUIET_DB + 4 - float(m.group(1))) / 20), 1))
            tip = f'try "volume": {need:g}' if need > w["volume"] + 0.2 else 'it stays quiet even at the maximum volume 4 — lower "music" for that window or pick a louder moment'
            warnings.append(f"segment {sg.index + 1} nat at {w['startFrame'] / fps:.1f}s: the ride sound is quiet ({level:.0f} dB) and will sit under the music — {tip}")


def volume_keys(sg, base: float, wins: list[dict]) -> list[list[float]] | None:
    """[[frame, volume], …] relative to the segment: the ride audio rises to each nat level
    with NAT_RAMP-frame ramps. None when the segment has no nat window."""
    mine = sorted(([w["startFrame"] - sg.start, w["endFrame"] - sg.start, w["volume"]] for w in wins if w["seg"] == sg.index))
    if not mine:
        return None
    merged: list[list[float]] = []
    for a, b, v in mine:
        if merged and a <= merged[-1][1] + 2 * NAT_RAMP:
            merged[-1][1], merged[-1][2] = max(merged[-1][1], b), max(merged[-1][2], v)
        else:
            merged.append([a, b, v])
    dur = sg.frames
    keys: list[list[float]] = []

    def put(f: float, v: float) -> None:
        f = clamp(f, 0, dur)
        if keys and f <= keys[-1][0]:
            if f == keys[-1][0]:
                keys[-1][1] = v
            return
        keys.append([int(f), round(v, 3)])

    for a, b, v in merged:
        if a > 0:
            put(max(0, a - NAT_RAMP), base)
        put(a, v)
        put(b, v)
        if b < dur:
            put(min(dur, b + NAT_RAMP), base)
    if keys[0][0] > 0:
        keys.insert(0, [0, base])
    if keys[-1][0] < dur:
        keys.append([dur, keys[-1][1]])
    return keys


def music_drops(plan: dict, total: int, fps: int, errors: list[str], warnings: list[str]) -> list[dict]:
    raw = plan.get("musicDrops") or []
    if not isinstance(raw, list):
        errors.append("musicDrops must be a list like [{\"at\": 6.2, \"for\": 0.5}]")
        return []
    out = []
    for j, d in enumerate(raw):
        where = f"musicDrops[{j}]"
        if not isinstance(d, dict):
            errors.append(f"{where}: use {{\"at\": seconds, \"for\": seconds}}")
            continue
        at = _f(d.get("at"), where, "at", errors, 0, 600)
        dur = _f(d.get("for", 0.5), where, "for", errors, 0.1, 3)
        if at is None or dur is None:
            continue
        s, e = round(at * fps), min(total, round((at + dur) * fps))
        if s >= total - 2:
            warnings.append(f"{where}: starts after the reel ends — skipped")
            continue
        out.append({"startFrame": s, "endFrame": e})
    if len(out) > 1:
        warnings.append(f"{len(out)} music drops — one per reel, right before the payoff, lands best")
    return out


def music_levels(nat: list[dict], drops: list[dict]) -> list[dict]:
    """Per-range music levels on top of the speech duck: nat windows duck to their level, drops go silent."""
    out = [{"startFrame": w["startFrame"], "endFrame": w["endFrame"], "level": w["music"], "rampIn": NAT_RAMP, "rampOut": NAT_RAMP} for w in nat]
    out += [{"startFrame": d["startFrame"], "endFrame": d["endFrame"], "level": 0, "rampIn": DROP_DOWN, "rampOut": DROP_UP} for d in drops]
    return sorted(out, key=lambda r: r["startFrame"])


def covered_share(ranges: list[dict], total: int) -> float:
    spans = sorted((r["startFrame"], r["endFrame"]) for r in ranges)
    n, cur = 0, None
    for s, e in spans:
        if cur and s <= cur[1]:
            cur[1] = max(cur[1], e)
        else:
            if cur:
                n += cur[1] - cur[0]
            cur = [s, e]
    if cur:
        n += cur[1] - cur[0]
    return n / max(1, total)


# --------------------------------------------------------------------------
# Sound-effect cues
# --------------------------------------------------------------------------


def _rule(rules: dict, event: str) -> tuple[str | None, float | None]:
    r = rules.get(event)
    if isinstance(r, dict):
        return r.get("sound"), r.get("gain")
    return (r or None), None


def _overlaps_speech(s: int, e: int, pages: list[dict]) -> bool:
    return any(s < p["endFrame"] and p["startFrame"] < e for p in pages)


def _move_mids(path: list[dict]) -> list[float]:
    """Seconds (from the segment start) at the middle of each real camera move in a 360 path."""
    mids = []
    for a, b in zip(path, path[1:]):
        dyaw = abs((float(b.get("yaw", 0)) - float(a.get("yaw", 0)) + 180) % 360 - 180)
        dpitch = abs(float(b.get("pitch", 0)) - float(a.get("pitch", 0)))
        dfov = abs(float(b.get("fov", 0)) - float(a.get("fov", 0)))
        if dyaw >= 30 or dpitch >= 20 or dfov >= 25:
            mids.append((a["at"] + b["at"]) / 2)
    return mids


def build_cues(
    project: Project, plan: dict, segs: list, beats: list[dict], pages: list[dict], total: int, fps: int, warnings: list[str]
) -> list[dict]:
    """Manual cues (plan and segment `sfx`) plus the automatic ones from config sfx.autoRules.
    Each cue: sound, path, startFrame (may be < 0: trimmed), durationInFrames, gain, source."""
    from .sfx import resolve_sfx

    scfg = project.config["sfx"]
    default_gain = float(scfg.get("gain", 0.7))
    errors: list[str] = []
    cache: dict[str, tuple[Path, dict] | str] = {}  # slug -> (wav, record) or the lookup error

    def sound(name: str, where: str, *, manual: bool) -> tuple[Path, dict] | None:
        if name not in cache:
            try:
                cache[name] = resolve_sfx(project, name)
            except ReelsError as e:
                cache[name] = str(e)
                if not manual:
                    warnings.append(f"auto sfx: {e} (config sfx.autoRules) — skipped")
        got = cache[name]
        if isinstance(got, str):
            if manual:
                errors.append(f"{where}: {got}")
            return None
        return got

    def make(name: str, where: str, *, start: float | None = None, end: float | None = None, centre: float | None = None,
             gain: float | None, source: str, manual: bool, anchor: float | None = None) -> dict | None:  # fmt: skip
        got = sound(name, where, manual=manual)
        if not got:
            return None
        path, meta = got
        dur = max(1, round(float(meta.get("duration") or 0.5) * fps))
        if start is None:
            start = end - dur if end is not None else centre - dur / 2
        start = int(round(start))
        if start >= total - 1 or start + dur <= 0:
            warnings.append(f"{where}: {name} at {start / fps:.2f}s falls outside the reel — skipped")
            return None
        g = default_gain if gain is None else gain
        return {"sound": meta.get("slug") or name, "path": path, "startFrame": start, "durationInFrames": dur,
                "gain": round(float(g), 3), "source": source, "anchor": start if anchor is None else int(round(anchor))}  # fmt: skip

    # manual cues
    manual: list[dict] = []

    def add_manual(items: Any, base: int, where: str, source: str, host=None) -> None:
        if items is None:
            return
        if not isinstance(items, list):
            errors.append(f"{where}: must be a list of {{\"sound\", \"at\" or \"endAt\", \"gain\"}}")
            return
        for j, c in enumerate(items):
            w = f"{where}[{j}]"
            if not isinstance(c, dict) or not c.get("sound"):
                errors.append(f"{w}: needs \"sound\" (a slug from ./reels sfx list) and \"at\" or \"endAt\"")
                continue
            if ("at" in c) == ("endAt" in c):
                errors.append(f"{w}: give either at or endAt (seconds)")
                continue
            t = _f(c.get("at", c.get("endAt")), w, "at" if "at" in c else "endAt", errors, 0, 600)
            gain = None if c.get("gain") is None else _f(c["gain"], w, "gain", errors, 0, 2)
            if t is None:
                continue
            f = base + t * fps
            cue = make(str(c["sound"]), w, start=f if "at" in c else None, end=f if "endAt" in c else None, gain=gain,
                       source=source, manual=True)  # fmt: skip
            if cue:
                if host is not None and host.spec.get("timeline"):
                    cue["trimBefore"] = round(float(c.get("_timelineOffset", 0)) * fps)
                    cue["durationInFrames"] = min(
                        round(float(c.get("_timelineDuration", cue["durationInFrames"] / fps)) * fps),
                        host.end-cue["startFrame"])
                    cue["ownerSegmentIndex"] = host.index
                    if cue["durationInFrames"] <= 0:
                        continue
                manual.append(cue)

    from .titlecard import card_sfx

    add_manual(card_sfx(plan), 0, "titleCard sound", "title card")
    add_manual(plan.get("sfx"), 0, "sfx", "plan")
    for sg in segs:
        add_manual(sg.spec.get("sfx"), sg.start, f"segment {sg.index + 1} sfx", f"segment {sg.index + 1}", host=sg)
    if errors:
        raise ReelsError("plan problems:\n  - " + "\n  - ".join(errors))

    auto_on = plan.get("autoSfx", scfg.get("auto", False))
    if not isinstance(auto_on, bool):
        warnings.append("autoSfx must be true or false — using false")
        auto_on = False
    autos: list[dict] = []
    if auto_on:
        rules = scfg.get("autoRules") or {}
        talking = {sg.index for sg in segs if sg.spec.get("captions") == "transcript"}

        def auto(event: str, where: str, **kw: Any) -> dict | None:
            name, gain = _rule(rules, event)
            if not name:
                return None
            cue = make(name, where, gain=gain, source=f"auto: {event}", manual=False, **kw)
            if cue:
                cue["rule"] = event
                autos.append(cue)
            return cue

        for sg in segs:
            if sg.trans and not ({sg.index, sg.index - 1} & talking):
                mid = sg.start + sg.trans / 2
                auto("transition", f"segment {sg.index + 1} fade", centre=mid, anchor=mid)
            path = (sg.spec.get("sphere") or {}).get("path")
            if path and sg.index not in talking:
                for m in _move_mids(path):
                    f = sg.start + m * fps
                    if f < sg.end:
                        auto("cameraMove", f"segment {sg.index + 1} camera move", centre=f, anchor=f)
        for sg in segs:
            for p in sg.spec.get("_punches") or []:  # the swish lands with the zoom
                auto("punchIn", f"segment {sg.index + 1} punch ({p['why']})", start=p["start"], anchor=p["start"])
        for b in beats:
            auto("textBeat", f"text '{b['text']}'", start=b["startFrame"])
        payoffs = [sg for sg in segs if sg.spec.get("payoff")]
        if len(payoffs) > 1:
            warnings.append(f"{len(payoffs)} segments marked payoff — a reel usually has one")
        for sg in payoffs:
            auto("payoff", f"segment {sg.index + 1} payoff", start=sg.start + sg.trans / 2)
            prev = segs[sg.index - 1] if sg.index > 0 else None
            name, _ = _rule(rules, "payoffRiser")
            got = sound(name, "payoffRiser", manual=False) if name else None
            if prev and got:
                rdur = round(float(got[1].get("duration") or 0) * fps)
                if sg.start + sg.trans / 2 - rdur >= prev.start + round(0.3 * fps):
                    auto("payoffRiser", f"segment {sg.index + 1} payoff riser", end=sg.start + sg.trans / 2)

        # tasteful: nothing over speech (except the payoff), at most one per autoSpacing seconds
        spacing = float(scfg.get("autoSpacing", 1.5)) * fps
        most = int(scfg.get("autoMax", 4))
        kept: list[dict] = []
        for c in sorted(autos, key=lambda c: (_PRIORITY.get(c["rule"], 9), c["anchor"])):
            if c["rule"] not in ("payoff", "payoffRiser") and _overlaps_speech(c["startFrame"], c["startFrame"] + c["durationInFrames"], pages):
                continue
            if c["rule"] != "payoffRiser" and any(abs(c["anchor"] - k["anchor"]) < spacing for k in kept if k["rule"] != "payoffRiser"):
                continue
            if c["rule"] != "payoffRiser" and sum(k["rule"] != "payoffRiser" for k in kept) >= most:
                continue
            kept.append(c)
        autos = kept

    # a manual cue close to an auto cue replaces it
    near = REPLACE_WITHIN * fps

    def close(a: dict, b: dict) -> bool:
        ca, cb = a["startFrame"] + a["durationInFrames"] / 2, b["startFrame"] + b["durationInFrames"] / 2
        return min(abs(a["startFrame"] - b["startFrame"]), abs(ca - cb), abs(a["anchor"] - b["anchor"])) <= near

    autos = [a for a in autos if not any(close(a, m) for m in manual)]
    cues = sorted(manual + autos, key=lambda c: c["startFrame"])
    for c in cues:
        c.pop("rule", None)
    return cues


def cue_props(cues: list[dict]) -> list[dict]:
    out = []
    for c in cues:
        s = c["startFrame"]
        p: dict[str, Any] = {"src": f"sfx/{c['path'].name}", "startFrame": max(0, s), "durationInFrames": c["durationInFrames"] - max(0, -s), "volume": c["gain"]}
        if s < 0:
            p["trimBefore"] = -s
        if c.get("trimBefore"):
            p["trimBefore"] = p.get("trimBefore", 0) + c["trimBefore"]
        out.append(p)
    return out


def cue_sheet(sound: dict, fps: int, segs: list) -> list[str]:
    """Lines for ./reels check: nat windows, drops, then every cue in time order."""
    lines = []
    for w in sound["nat"]:
        mus = f", music {w['music']:g}" if sound["music"] else ""
        lines.append(f"  nat     {w['startFrame'] / fps:6.2f}-{w['endFrame'] / fps:.2f}s  ride audio {w['volume']:g}{mus}  (segment {w['seg'] + 1})")
    for d in sound["drops"]:
        lines.append(f"  drop    {d['startFrame'] / fps:6.2f}-{d['endFrame'] / fps:.2f}s  music silent")
    for sg in segs:
        if sg.spec.get("clean") or (sg.spec.get("nat") and sound["cleanNat"]):
            lines.append(f"  clean   segment {sg.index + 1}: highpass + light denoise on its ride audio")
    for c in sound["cues"]:
        lines.append(f"  sfx     {c['startFrame'] / fps:6.2f}s  {c['sound']:<16} gain {c['gain']:<5g} {c['source']}")
    return lines
