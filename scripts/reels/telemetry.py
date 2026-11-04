"""GPS from GoPro telemetry (the GPMF 'gpmd' track inside .360 / GoPro MP4 files).

Only real, quality-checked fixes are kept: no estimates, and nothing is ever
interpolated across a gap longer than MAX_GAP seconds. Output per asset:
library/telemetry/<id>.json with ~5 Hz samples and a summary.
"""

from __future__ import annotations

import bisect
import json
import math
import os
import struct
import tempfile
from pathlib import Path
from typing import Iterator

from .config import Project
from .library import all_assets, load_asset, update_asset
from .media import ffprobe_json, run
from .util import ReelsError, fmt_ts, log, now_iso, warn

MAX_DOP = 5.0  # GPSP 500 = DOP 5; GoPro calls under 500 a good fix
MAX_GAP = 1.5  # s; never bridge a longer gap
MAX_SPEED = 40.0  # m/s; above this a fix is a glitch, not a bike
RATE = 5  # Hz kept on disk
SPEED_SMOOTH = 1.0  # s, centred moving average on speed
ALT_SMOOTH = 10.0  # s, for elevation gain/loss
ALT_DEADBAND = 2.0  # m; GPS altitude wobble below this isn't climbing
MOVING = 1.0  # m/s; slower than this counts as stopped
FIELDS = ["t", "lat", "lon", "alt", "speed", "fix", "dop"]

# --------------------------------------------------------------------------
# GPMF (KLV: 4-char key, 1-byte type, 1-byte struct size, 2-byte repeat; 4-byte aligned)
# --------------------------------------------------------------------------

_FMT = {"b": "b", "B": "B", "s": "h", "S": "H", "l": "i", "L": "I", "f": "f", "d": "d", "j": "q", "J": "Q"}


def klv(buf: bytes) -> Iterator[tuple[str, str, int, int, bytes]]:
    i = 0
    while i + 8 <= len(buf):
        key = buf[i : i + 4].decode("latin-1")
        typ, size, repeat = buf[i + 4], buf[i + 5], struct.unpack(">H", buf[i + 6 : i + 8])[0]
        n = size * repeat
        if key == "\0\0\0\0":
            break
        yield key, chr(typ) if typ else "\0", size, repeat, buf[i + 8 : i + 8 + n]
        i += 8 + ((n + 3) & ~3)


def _values(typ: str, size: int, repeat: int, data: bytes) -> list:
    f = _FMT.get(typ)
    if not f:
        return []
    count = size * repeat // struct.calcsize(f)
    return list(struct.unpack(f">{count}{f}", data[: count * struct.calcsize(f)]))


def gps_payload(buf: bytes) -> list[tuple[float, float, float, float, int, float]]:
    """(lat, lon, alt, speed2d, fix, dop) for every GPS sample in one gpmd packet."""
    out: list[tuple[float, float, float, float, int, float]] = []
    for key, typ, _, _, data in klv(buf):
        if key != "DEVC" or typ != "\0":
            continue
        for skey, styp, _, _, sdata in klv(data):
            if skey != "STRM" or styp != "\0":
                continue
            scal: list = []
            fix, dop, types = 0, 99.0, ""
            for k, t, size, rep, d in klv(sdata):
                if k == "SCAL":
                    scal = _values(t, size, rep, d)
                elif k == "GPSF":
                    fix = int((_values(t, size, rep, d) or [0])[0])
                elif k == "GPSP":
                    dop = float((_values(t, size, rep, d) or [9999])[0]) / 100
                elif k == "TYPE":
                    types = d.decode("latin-1").rstrip("\0")
                elif k == "GPS5" and size == 20:
                    sc = (scal * 5)[:5] if len(scal) == 1 else scal
                    if len(sc) < 5:
                        continue
                    for row in struct.iter_unpack(">5i", d[: rep * 20]):
                        out.append((row[0] / sc[0], row[1] / sc[1], row[2] / sc[2], row[3] / sc[3], fix, dop))
                elif k == "GPS9" and types:
                    # lat, lon, alt, 2D speed, 3D speed, days, secs, DOP, fix — each sample carries its own fix/DOP
                    fmt = ">" + "".join(_FMT[c] for c in types)
                    if struct.calcsize(fmt) != size or len(scal) < 9:
                        continue
                    for row in struct.iter_unpack(fmt, d[: rep * size]):
                        v = [row[j] / scal[j] for j in range(9)]
                        out.append((v[0], v[1], v[2], v[3], int(row[8]), v[7]))
    return out


def gpmd_stream(info: dict) -> int | None:
    for s in info.get("streams") or []:
        if s.get("codec_tag_string") == "gpmd":
            return int(s["index"])
    return None


def _packets(path: Path, index: int) -> list[tuple[float, float, int, int]]:
    out = run(
        ["ffprobe", "-v", "error", "-select_streams", str(index), "-show_entries",
         "packet=pts_time,duration_time,pos,size", "-of", "json", path]
    ).stdout  # fmt: skip
    pk = []
    for p in json.loads(out or "{}").get("packets") or []:
        try:
            pk.append((float(p["pts_time"]), float(p["duration_time"]), int(p["pos"]), int(p["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return pk


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(min(1.0, h)))


def _runs(ts: list[float]) -> list[tuple[int, int]]:
    """[start, end) index ranges with no gap longer than MAX_GAP."""
    runs, s = [], 0
    for i in range(1, len(ts) + 1):
        if i == len(ts) or ts[i] - ts[i - 1] > MAX_GAP:
            runs.append((s, i))
            s = i
    return runs if ts else []


def _smooth(ts: list[float], vs: list[float], window: float) -> list[float]:
    """Centred moving average, never reaching across a gap."""
    out = list(vs)
    for a, b in _runs(ts):
        lo = hi = a
        acc = 0.0
        for i in range(a, b):
            while hi < b and ts[hi] <= ts[i] + window / 2:
                acc += vs[hi]
                hi += 1
            while ts[lo] < ts[i] - window / 2:
                acc -= vs[lo]
                lo += 1
            out[i] = acc / (hi - lo)
    return out


def extract(path: Path, video_start: float = 0.0) -> dict | None:
    info = ffprobe_json(path)
    index = gpmd_stream(info)
    if index is None:
        return None
    for s in info.get("streams") or []:
        if s.get("codec_type") == "video":
            video_start = float(s.get("start_time") or 0)
            break
    raw: list[tuple[float, float, float, float, float, int, float]] = []
    total = 0
    with open(path, "rb") as f:
        for pts, dur, pos, size in _packets(path, index):
            f.seek(pos)
            samples = gps_payload(f.read(size))
            total += len(samples)
            for i, (lat, lon, alt, spd, fix, dop) in enumerate(samples):
                raw.append((pts - video_start + i * dur / len(samples), lat, lon, alt, spd, fix, dop))

    good = [r for r in raw if r[5] >= 3 and r[6] <= MAX_DOP and 0 <= r[4] <= MAX_SPEED and (r[1] or r[2])]
    # drop position jumps a bike can't make
    clean: list = []
    for r in good:
        if clean and r[0] - clean[-1][0] <= MAX_GAP:
            dt = max(r[0] - clean[-1][0], 1e-3)
            if _dist(clean[-1][1:3], r[1:3]) / dt > MAX_SPEED:
                continue
        clean.append(r)

    ts = [r[0] for r in clean]
    speed = _smooth(ts, [r[4] for r in clean], SPEED_SMOOTH)

    # ~5 Hz: average the samples in each 0.2 s bin (empty bins stay empty)
    bins: dict[int, list[int]] = {}
    for i, t in enumerate(ts):
        bins.setdefault(int(round(t * RATE)), []).append(i)
    samples = []
    for k in sorted(bins):
        idx = bins[k]
        n = len(idx)
        samples.append(
            [
                round(k / RATE, 2),
                round(sum(clean[i][1] for i in idx) / n, 7),
                round(sum(clean[i][2] for i in idx) / n, 7),
                round(sum(clean[i][3] for i in idx) / n, 1),
                round(sum(speed[i] for i in idx) / n, 2),
                min(clean[i][5] for i in idx),
                round(max(clean[i][6] for i in idx), 2),
            ]
        )
    return {
        "version": 1,
        "source": "gpmf",
        "stream": index,
        "fields": FIELDS,
        "rawSamples": total,
        "goodSamples": len(clean),
        "summary": summarize(samples),
        "samples": samples,
    }


def summarize(samples: list[list]) -> dict:
    ts = [s[0] for s in samples]
    runs = _runs(ts)
    good = sum(ts[b - 1] - ts[a] + 1 / RATE for a, b in runs)
    dist = 0.0
    moving_t = moving_d = 0.0
    for a, b in runs:
        for i in range(a + 1, b):
            dt = ts[i] - ts[i - 1]
            v = (samples[i][4] + samples[i - 1][4]) / 2
            dist += v * dt
            if v >= MOVING:
                moving_t += dt
                moving_d += v * dt
    alt = _smooth(ts, [s[3] for s in samples], ALT_SMOOTH)
    alt_ok = altitude_ok(ts, samples, runs)
    gain = loss = 0.0
    for a, b in runs:
        if b <= a:
            continue
        ref = alt[a]
        for i in range(a + 1, b):
            d = alt[i] - ref
            if d >= ALT_DEADBAND:
                gain += d
                ref = alt[i]
            elif d <= -ALT_DEADBAND:
                loss -= d
                ref = alt[i]
    return {
        "goodSeconds": round(good, 1),
        "gaps": max(0, len(runs) - 1),
        "first": ts[0] if ts else None,
        "last": ts[-1] if ts else None,
        "maxSpeed": round(max((s[4] for s in samples), default=0.0), 2),
        "movingAvgSpeed": round(moving_d / moving_t, 2) if moving_t else 0.0,
        "movingSeconds": round(moving_t, 1),
        "distance": round(dist, 1),
        # GPS altitude drifts a lot (tens of metres while standing still); gain/loss only when it held together
        "altitudeOk": alt_ok,
        "elevationGain": round(gain, 1) if alt_ok else None,
        "elevationLoss": round(loss, 1) if alt_ok else None,
        "altMin": round(min((s[3] for s in samples), default=0.0), 1) if samples else None,
        "altMax": round(max((s[3] for s in samples), default=0.0), 1) if samples else None,
    }


def altitude_ok(ts: list[float], samples: list[list], runs: list[tuple[int, int]], window: float = 10.0) -> bool:
    """False when altitude moves in ways a bike can't (>2 m/s vertically, >4 m while standing
    still, a >25% grade over 10 s) or there's too little fix to tell. Then elevation isn't a fact."""
    alt = _smooth(ts, [s[3] for s in samples], 2.0)
    bad = total = 0
    for a, b in runs:
        for i in range(a + 1, b):  # sample to sample: no bike climbs 2 m/s
            if abs(alt[i] - alt[i - 1]) > 2.0 * max(ts[i] - ts[i - 1], 1 / RATE):
                bad += 1
        cum = [0.0]
        for i in range(a + 1, b):
            cum.append(cum[-1] + (samples[i][4] + samples[i - 1][4]) / 2 * (ts[i] - ts[i - 1]))
        j = a
        for i in range(a, b):
            while j < b and ts[j] < ts[i] + window:
                j += 1
            if j >= b:
                break
            total += 1
            dist, climb = cum[j - a] - cum[i - a], abs(alt[j] - alt[i])
            if (dist < 10 and climb > 4) or (dist >= 10 and climb / dist > 0.25):
                bad += 1
    return total >= 30 * RATE and bad <= 0.02 * total  # needs 30 s of steady fix to judge


# --------------------------------------------------------------------------
# Reading it back
# --------------------------------------------------------------------------


class Track:
    """5 Hz GPS samples of one clip; lookups never bridge a gap over MAX_GAP."""

    def __init__(self, data: dict):
        self.data = data
        self.samples: list[list] = data.get("samples") or []
        self.ts = [s[0] for s in self.samples]

    def at(self, t: float) -> dict | None:
        ts, s = self.ts, self.samples
        if not ts:
            return None
        i = bisect.bisect_left(ts, t)
        if i < len(ts) and abs(ts[i] - t) < 1e-6:
            return _row(s[i])
        if i == 0 or i == len(ts):
            j = 0 if i == 0 else len(ts) - 1
            return _row(s[j]) if abs(ts[j] - t) <= 0.5 / RATE else None
        a, b = s[i - 1], s[i]
        if b[0] - a[0] > MAX_GAP:
            return None
        f = (t - a[0]) / (b[0] - a[0])
        return _row([t] + [a[k] + (b[k] - a[k]) * f for k in range(1, 5)] + [min(a[5], b[5]), max(a[6], b[6])])

    def gaps(self, a: float, b: float) -> list[tuple[float, float]]:
        """Stretches of [a, b] without a usable fix."""
        ts = self.ts
        lo, hi = bisect.bisect_left(ts, a - 0.5 / RATE), bisect.bisect_right(ts, b + 0.5 / RATE)
        inside = ts[lo:hi]
        if not inside:
            return [(a, b)]
        out = []
        if inside[0] - a > 0.5 / RATE:
            out.append((a, inside[0]))
        for x, y in zip(inside, inside[1:]):
            if y - x > MAX_GAP:
                out.append((x, y))
        if b - inside[-1] > 0.5 / RATE:
            out.append((inside[-1], b))
        return out

    def window(self, a: float, b: float) -> list[list]:
        lo, hi = bisect.bisect_left(self.ts, a), bisect.bisect_right(self.ts, b)
        return self.samples[lo:hi]

    def range(self, a: float, b: float) -> dict | None:
        rows = self.window(a, b)
        if not rows:
            return None
        sp = [r[4] for r in rows]
        alt = [r[3] for r in rows]
        return {"min": min(sp), "max": max(sp), "avg": sum(sp) / len(sp), "altMin": min(alt), "altMax": max(alt), "gaps": self.gaps(a, b)}


def _row(s: list) -> dict:
    return dict(zip(FIELDS, s))


def telemetry_dir(project: Project) -> Path:
    return project.library / "telemetry"


def load_track(project: Project, asset: dict) -> Track | None:
    rel = asset.get("telemetry")
    if not rel:
        return None
    p = project.abs(rel)
    if not p.exists():
        return None
    return Track(json.loads(p.read_text(encoding="utf-8")))


# --------------------------------------------------------------------------
# Units
# --------------------------------------------------------------------------


def units(project: Project) -> str:
    u = project.config.get("units") or "imperial"
    return u if u in ("imperial", "metric") else "imperial"


def speed_in(project: Project, ms: float) -> float:
    return ms * (2.236936 if units(project) == "imperial" else 3.6)


def dist_in(project: Project, m: float) -> float:
    return m / (1609.344 if units(project) == "imperial" else 1000)


def alt_in(project: Project, m: float) -> float:
    return m * (3.28084 if units(project) == "imperial" else 1)


def unit_names(project: Project) -> dict[str, str]:
    return {"speed": "mph", "dist": "mi", "alt": "ft"} if units(project) == "imperial" else {"speed": "km/h", "dist": "km", "alt": "m"}


def summary_line(project: Project, s: dict) -> str:
    u = unit_names(project)
    if not s.get("goodSeconds"):
        return "GPS: no usable fix in this clip"
    return (
        f"GPS: {fmt_ts(s['goodSeconds'])[:-2]} with a good fix ({fmt_ts(s['first'])}-{fmt_ts(s['last'])}"
        + (f", {s['gaps']} gap{'s' if s['gaps'] != 1 else ''}" if s["gaps"] else "")
        + f")  ·  max {speed_in(project, s['maxSpeed']):.1f} {u['speed']}  ·  moving avg {speed_in(project, s['movingAvgSpeed']):.1f} {u['speed']}"
        + f"  ·  {dist_in(project, s['distance']):.2f} {u['dist']}  ·  "
        + (
            f"+{alt_in(project, s['elevationGain']):.0f}/-{alt_in(project, s['elevationLoss']):.0f} {u['alt']}"
            if s.get("altitudeOk")
            else "altitude unreliable (GPS drift) — no elevation numbers"
        )
    )


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def _write_compact(path: Path, data: dict) -> None:
    """Atomic; one sample per line keeps the file small but diffable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    head = {k: v for k, v in data.items() if k != "samples"}
    body = json.dumps(head, indent=2)[:-2] + ',\n  "samples": [\n'
    body += ",\n".join("    " + json.dumps(s, separators=(",", ":")) for s in data["samples"]) + "\n  ]\n}\n"
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(body)
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(tmp, 0o666 & ~umask)
    os.replace(tmp, path)


def has_gpmd(project: Project, asset: dict) -> bool:
    if asset.get("kind") != "video":
        return False
    return gpmd_stream(ffprobe_json(project.abs(asset["media"]))) is not None


def extract_asset(project: Project, asset: dict) -> dict | None:
    data = extract(project.abs(asset["media"]))
    if data is None:
        return None
    data["asset"] = asset["id"]
    data["extractedAt"] = now_iso()
    out = telemetry_dir(project) / f"{asset['id']}.json"
    if data["samples"]:
        _write_compact(out, data)
        update_asset(project, asset, telemetry=project.rel(out), gpsSummary=data["summary"])
    else:
        out.unlink(missing_ok=True)
        update_asset(project, asset, telemetry=None, gpsSummary={"goodSeconds": 0})
    return data


def run_telemetry(project: Project, ids: list[str] | None = None, *, force: bool = False, quiet_none: bool = False) -> list[dict]:
    """Extracts GPS for the given assets (default: every GoPro clip without telemetry yet)."""
    if ids:
        assets = [load_asset(project, i) for i in ids]
    else:
        assets = [a for a in all_assets(project) if a.get("kind") == "video" and (force or "gpsSummary" not in a)]
    done = []
    for a in assets:
        if a.get("kind") != "video":
            if ids:
                warn(f"{a['id']}: not a video — no telemetry")
            continue
        if "gpsSummary" in a and not force and not ids:
            continue
        if not has_gpmd(project, a):
            if ids and not quiet_none:
                log(f"  {a['id']}: no GoPro telemetry track")
            continue
        log(f"  reading GPS from {a['id']}…")
        try:
            data = extract_asset(project, a)
        except (OSError, struct.error, ReelsError) as e:
            warn(f"{a['id']}: couldn't read its telemetry ({e})")
            continue
        if data is None:
            continue
        log(f"  {a['id']}  {summary_line(project, data['summary'])}")
        done.append(a)
    return done


def print_at(project: Project, asset: dict, t: float) -> None:
    tr = load_track(project, asset)
    if tr is None:
        raise ReelsError(f"{asset['id']} has no GPS telemetry (./reels telemetry {asset['id']})")
    r = tr.at(t)
    u = unit_names(project)
    if r is None:
        raise ReelsError(f"{asset['id']}: no good GPS fix at {fmt_ts(t)} — nothing to show there")
    log(
        f"{asset['id']} @ {fmt_ts(t)}  speed {speed_in(project, r['speed']):.1f} {u['speed']}  "
        f"alt {alt_in(project, r['alt']):.0f} {u['alt']}{_alt_note(asset)}  ({r['lat']:.5f}, {r['lon']:.5f})  dop {r['dop']:g}"
    )


def _alt_note(asset: dict) -> str:
    return "" if (asset.get("gpsSummary") or {}).get("altitudeOk") else " (GPS altitude unreliable here)"


def print_range(project: Project, asset: dict, a: float, b: float) -> None:
    tr = load_track(project, asset)
    if tr is None:
        raise ReelsError(f"{asset['id']} has no GPS telemetry (./reels telemetry {asset['id']})")
    r = tr.range(a, b)
    u = unit_names(project)
    if r is None:
        raise ReelsError(f"{asset['id']}: no good GPS fix between {fmt_ts(a)} and {fmt_ts(b)}")
    log(
        f"{asset['id']} {fmt_ts(a)}-{fmt_ts(b)}  speed min {speed_in(project, r['min']):.1f} / avg {speed_in(project, r['avg']):.1f} / "
        f"max {speed_in(project, r['max']):.1f} {u['speed']}  ·  alt {alt_in(project, r['altMin']):.0f}-{alt_in(project, r['altMax']):.0f} {u['alt']}{_alt_note(asset)}"
    )
    for x, y in r["gaps"]:
        log(f"  no fix {fmt_ts(x)}-{fmt_ts(y)} — speed can't be shown there")


def fast_stretches(project: Project, asset: dict, seconds: float = 5.0, top: int = 5) -> list[tuple[float, float, float]]:
    """Non-overlapping windows with the highest average speed (gap-free only)."""
    tr = load_track(project, asset)
    if tr is None or not tr.ts:
        return []
    cands = []
    t = tr.ts[0]
    while t + seconds <= tr.ts[-1]:
        r = tr.range(t, t + seconds)
        if r and not r["gaps"]:
            cands.append((r["avg"], t))
        t += 1.0
    out: list[tuple[float, float, float]] = []
    for avg, t in sorted(cands, reverse=True):
        if all(abs(t - o[0]) >= seconds for o in out):
            out.append((t, t + seconds, avg))
        if len(out) >= top:
            break
    return out
