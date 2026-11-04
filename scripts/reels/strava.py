"""Strava rides: import what Claude pulled from the Strava MCP, link library clips to them,
and look up ride numbers at a clip's exact moment.

The CLI never talks to Strava itself. Claude saves the connector's responses to files and runs
`./reels strava import <file> [--activity ID]`, which stores them in strava/activities/<id>.json.
"""

from __future__ import annotations

import bisect
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Project
from .images import parse_date
from .library import all_assets, update_asset
from .util import ReelsError, fmt_ts, log, now_iso, read_json, write_json

SEARCH = 900  # s; how far off a camera clock may be when matching GPS to the ride
MATCH_M = 25.0  # mean GoPro-vs-Strava distance (m) that counts as a clock match
STREAM_GAP = 5.0  # s; no live value across a longer gap in the Strava streams
TRIM_M = 1000.0  # m trimmed off each end of route maps for rides in Maine
MAINE = (42.9, 47.5, -71.1, -66.9)  # south, north, west, east
_GOPRO = re.compile(r"^(G[A-Z])(\d\d)(\d{4})\.", re.I)

# value -> default pill label
RIDE_VALUES = {
    "strava:distance": "distance",
    "strava:moving-time": "moving time",
    "strava:elapsed-time": "ride time",
    "strava:elevation": "elevation",
    "strava:avg-speed": "avg",
    "strava:max-speed": "top speed",
    "strava:avg-power": "avg power",
    "strava:avg-hr": "avg HR",
    "strava:max-hr": "max HR",
}
CLIP_VALUES = {  # at the clip's moment
    "strava:mile": "mile",  # distance into the ride (km when metric)
    "strava:temp": "temp",
}
LIVE_VALUES = {  # updating ~5x/s
    "strava:speed": "speed",
    "strava:power": "power",
    "strava:hr": "HR",
    "strava:cadence": "cadence",
}
VALUES = {**RIDE_VALUES, **CLIP_VALUES, **LIVE_VALUES}
_STREAM = {"strava:speed": "velocity_smooth", "strava:power": "watts", "strava:hr": "heart_rate",
           "strava:cadence": "cadence", "strava:temp": "temp", "strava:mile": "distance"}


# --------------------------------------------------------------------------
# Storage
# --------------------------------------------------------------------------


def activities_dir(project: Project) -> Path:
    return project.root / "strava" / "activities"


def activity_path(project: Project, activity_id: str) -> Path:
    return activities_dir(project) / f"{activity_id}.json"


def load_activity(project: Project, activity_id: str) -> dict | None:
    p = activity_path(project, str(activity_id))
    return read_json(p) if p.exists() else None


def all_activities(project: Project) -> list[dict]:
    d = activities_dir(project)
    acts = [read_json(p) for p in sorted(d.glob("*.json"))] if d.exists() else []
    return sorted(acts, key=lambda a: a.get("start_local") or "")


def _unwrap(data: Any) -> Any:
    """MCP results may arrive as [{"type": "text", "text": "<json>"}] or {"text": "<json>"}."""
    import json

    if isinstance(data, list) and data and isinstance(data[0], dict) and "text" in data[0]:
        data = "".join(str(x.get("text") or "") for x in data)
    if isinstance(data, dict) and set(data) <= {"type", "text"} and "text" in data:
        data = data["text"]
    if isinstance(data, str):
        data = json.loads(data)
    return data


def kind_of(data: dict) -> str:
    if isinstance(data.get("activities"), list):
        return "activities"
    if "time" in data and isinstance(data.get("time"), list):
        return "streams"
    if "has_heartrate" in data or "laps" in data or "segment_efforts" in data:
        return "performance"
    raise ReelsError("not a Strava response I know: expected list_activities/search_activities, get_activity_streams or get_activity_performance output")


def import_file(project: Project, path: str | Path, activity: str | None = None, *, relink: bool = True) -> list[str]:
    try:
        data = _unwrap(read_json(project.abs(path) if not Path(path).is_absolute() else path))
    except (OSError, ValueError) as e:
        raise ReelsError(f"can't read {path}: {e}") from e
    if not isinstance(data, dict):
        raise ReelsError(f"{path}: expected a JSON object")
    kind = kind_of(data)
    activities_dir(project).mkdir(parents=True, exist_ok=True)
    touched: list[str] = []
    if kind == "activities":
        for a in data["activities"]:
            aid = str(a["id"])
            cur = load_activity(project, aid) or {}
            cur.update({k: v for k, v in a.items()})
            cur["id"] = aid
            cur["units"] = data.get("value_units") or cur.get("units")
            cur["importedAt"] = now_iso()
            write_json(activity_path(project, aid), cur)
            touched.append(aid)
        log(f"strava: {len(touched)} ride(s) saved: " + ", ".join(touched))
    else:
        if not activity:
            raise ReelsError(f"{kind} don't say which ride they belong to — add --activity <id>")
        aid = str(activity)
        cur = load_activity(project, aid)
        if cur is None:
            raise ReelsError(f"ride {aid} not imported yet — import its list_activities entry first")
        cur[kind] = data
        cur["importedAt"] = now_iso()
        write_json(activity_path(project, aid), cur)
        touched.append(aid)
        what = f"{len(data['time'])} samples of {', '.join(k for k in data if k != 'time')}" if kind == "streams" else "summary/segments/laps"
        log(f"strava: {kind} for {aid} saved ({what})")
    if relink:
        link_assets(project, touched)
    return touched


# --------------------------------------------------------------------------
# Time alignment
# --------------------------------------------------------------------------


def window(act: dict) -> tuple[datetime, datetime]:
    start = parse_date(act.get("start_local"))
    if start is None:
        raise ReelsError(f"ride {act.get('id')} has no start_local")
    return start, start + timedelta(seconds=float((act.get("summary") or {}).get("elapsed_time") or 0))


def camera(asset: dict) -> str:
    m = _GOPRO.match(str(asset.get("originalName") or ""))
    return f"gopro-{m.group(1).upper()}" if m else "phone"


def _duration(asset: dict) -> float:
    return float((asset.get("probe") or {}).get("duration") or 0) if asset.get("kind") == "video" else 0.0


def capture_start(asset: dict, assets: list[dict]) -> datetime | None:
    """When the clip's first frame was filmed, by the camera's clock (local time).
    GoPro chapters (GS02…, GS03…) carry chapter 1's time, so earlier chapters' lengths are added."""
    created = parse_date(asset.get("created"))
    if created is None:
        return None
    m = _GOPRO.match(str(asset.get("originalName") or ""))
    if m and int(m.group(2)) > 1:
        before = 0.0
        for a in assets:
            n = _GOPRO.match(str(a.get("originalName") or ""))
            if n and n.group(1).upper() == m.group(1).upper() and n.group(3) == m.group(3) and int(n.group(2)) < int(m.group(2)) \
                    and str(a.get("created")) == str(asset.get("created")):
                before += _duration(a)
        created += timedelta(seconds=before)
    return created


class Streams:
    def __init__(self, data: dict):
        self.data = data
        self.t: list[float] = [float(x) for x in data.get("time") or []]

    def has(self, name: str) -> bool:
        v = self.data.get(name)
        return isinstance(v, list) and len(v) == len(self.t) and len(v) > 0

    def _i(self, t: float) -> tuple[int, int, float] | None:
        ts = self.t
        if not ts or t < ts[0] - 0.5 or t > ts[-1] + 0.5:
            return None
        i = bisect.bisect_left(ts, t)
        if i < len(ts) and abs(ts[i] - t) < 1e-6:
            return i, i, 0.0
        if i == 0:
            return 0, 0, 0.0
        if i == len(ts):
            return i - 1, i - 1, 0.0
        a, b = ts[i - 1], ts[i]
        if b - a > STREAM_GAP:
            return None
        return i - 1, i, (t - a) / (b - a)

    def at(self, name: str, t: float) -> float | None:
        if not self.has(name):
            return None
        hit = self._i(t)
        if hit is None:
            return None
        i, j, f = hit
        v = self.data[name]
        if v[i] is None or v[j] is None:
            return None
        return float(v[i]) + (float(v[j]) - float(v[i])) * f

    def mean(self, name: str, a: float, b: float) -> float | None:
        """Mean over [a, b] (b > a), at least one sample."""
        if not self.has(name):
            return None
        lo, hi = bisect.bisect_left(self.t, a), bisect.bisect_right(self.t, b)
        vals = [float(x) for x in self.data[name][lo:hi] if x is not None]
        if not vals:
            v = self.at(name, (a + b) / 2)
            return v
        return sum(vals) / len(vals)

    def location(self, t: float) -> tuple[float, float] | None:
        if not self.has("location"):
            return None
        hit = self._i(t)
        if hit is None:
            return None
        i, j, f = hit
        p, q = self.data["location"][i], self.data["location"][j]
        if not p or not q:
            return None
        return p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f


def _m(a: tuple[float, float], b: tuple[float, float]) -> float:
    k = math.cos(math.radians(a[0]))
    return math.hypot((a[0] - b[0]) * 111_195, (a[1] - b[1]) * 111_195 * k)


def gps_shift(project: Project, asset: dict, base: float, streams: Streams) -> tuple[float, float] | None:
    """Camera clock error (s, add to the camera time) found by sliding the clip's own GPS along the
    ride's GPS. `base` = ride seconds at clip t=0 by the camera clock. Returns (shift, mean metres)."""
    from .telemetry import load_track

    tr = load_track(project, asset)
    if tr is None or not streams.has("location"):
        return None
    pts = [(s[0], (s[1], s[2])) for s in tr.samples if len(s) > 4 and s[4] >= 3.0]  # moving, so position pins the time
    if len(pts) < 10:
        return None
    pts = pts[:: max(1, len(pts) // 60)]

    def cost(shift: float) -> float:
        tot, n = 0.0, 0
        for t, p in pts:
            q = streams.location(base + t + shift)
            if q is None:
                continue
            tot += min(_m(p, q), 500.0)
            n += 1
        return tot / n if n >= max(5, len(pts) // 2) else float("inf")

    best = min((cost(float(s)), float(s)) for s in range(-SEARCH, SEARCH + 1, 2))
    if best[0] == float("inf"):
        return None
    lo = best[1]
    best = min((cost(lo + d / 10), lo + d / 10) for d in range(-20, 21))
    return (round(best[1], 1), round(best[0], 1)) if best[0] <= MATCH_M else None


def link_assets(project: Project, activity_ids: list[str] | None = None) -> list[tuple[str, str]]:
    """Link every library asset filmed during an imported ride. Returns (asset id, activity id) pairs."""
    acts = [a for a in all_activities(project) if not activity_ids or a["id"] in activity_ids]
    assets = all_assets(project)
    linked: list[tuple[str, str]] = []
    for act in acts:
        start, end = window(act)
        streams = Streams(act.get("streams") or {})
        found: list[tuple[dict, datetime]] = []
        for a in assets:
            if a.get("kind") not in ("video", "photo"):
                continue
            cs = capture_start(a, assets)
            if cs is None or cs > end + timedelta(seconds=SEARCH) or cs + timedelta(seconds=_duration(a)) < start - timedelta(seconds=SEARCH):
                continue
            found.append((a, cs))
        near = {a["id"] for a, _ in found}
        for a in assets:  # stale links: clip no longer anywhere near this ride
            if a["id"] not in near and (a.get("strava") or {}).get("activity") == act["id"]:
                update_asset(project, a, strava=None)
        # Camera clock per camera body: from its own GPS where possible, shared with its other clips.
        shifts: dict[str, list[float]] = {}
        own: dict[str, tuple[float, float]] = {}
        for a, cs in found:
            if camera(a).startswith("gopro"):
                got = gps_shift(project, a, (cs - start).total_seconds(), streams)
                if got:
                    own[a["id"]] = got
                    shifts.setdefault(camera(a), []).append(got[0])
        for a, cs in found:
            cam = camera(a)
            if a["id"] in own:
                shift, fit, clock = own[a["id"]][0], own[a["id"]][1], "gps"
            elif shifts.get(cam):
                vals = sorted(shifts[cam])
                shift, fit, clock = vals[len(vals) // 2], None, "gps-same-camera"
            else:
                shift, fit, clock = 0.0, None, "phone" if cam == "phone" else "none"
            offset = (cs - start).total_seconds() + shift
            dur = _duration(a)
            elapsed = (end - start).total_seconds()
            inside = offset + dur >= 0 and offset <= elapsed
            prev = a.get("strava")
            if not inside:
                if prev and prev.get("activity") == act["id"]:
                    update_asset(project, a, strava=None)
                continue
            link = {"activity": act["id"], "offset": round(offset, 1), "clock": clock, "shift": shift}
            if fit is not None:
                link["fitMetres"] = fit
            update_asset(project, a, strava=link)
            linked.append((a["id"], act["id"]))
    for act in acts:
        ids = [x for x, y in linked if y == act["id"]]
        log(f"  {act['id']} {act.get('start_local', '')[:16]} {act.get('name', '')!r}: {len(ids)} clip(s) linked")
        for x in ids:
            lk = next(a for a in all_assets(project) if a["id"] == x)["strava"]
            note = {"gps": f"GoPro clock {lk['shift']:+.0f}s from its GPS", "gps-same-camera": f"GoPro clock {lk['shift']:+.0f}s (same camera)",
                    "phone": "phone clock", "none": "camera clock unchecked"}[lk["clock"]]
            log(f"    {x}  at {fmt_ts(lk['offset'])} into the ride  ({note})")
    return linked


# --------------------------------------------------------------------------
# Numbers for reels
# --------------------------------------------------------------------------


def linked_activity(project: Project, asset: dict) -> dict | None:
    lk = asset.get("strava") or {}
    return load_activity(project, lk["activity"]) if lk.get("activity") else None


def _hm(seconds: float) -> str:
    s = int(round(seconds))
    h, m = s // 3600, (s % 3600) // 60
    return f"{h}:{m:02d}" if h else f"{m} min"


def temp_in(project: Project, c: float) -> float:
    from .telemetry import units

    return c * 9 / 5 + 32 if units(project) == "imperial" else c


def ride_value(project: Project, act: dict, kind: str) -> tuple[str, str] | str:
    """(value, unit) for a whole-ride number, or an error string."""
    from .telemetry import alt_in, dist_in, speed_in, unit_names

    u = unit_names(project)
    s = act.get("summary") or {}
    perf = act.get("performance") or {}

    def need(d: dict, k: str, what: str):
        v = d.get(k)
        return v if v is not None else f"ride {act['id']} has no {what}"

    if kind == "strava:distance":
        v = need(s, "distance", "distance")
        return v if isinstance(v, str) else (f"{dist_in(project, v):.1f}", u["dist"])
    if kind == "strava:moving-time":
        v = need(s, "moving_time", "moving time")
        return v if isinstance(v, str) else (_hm(v), "")
    if kind == "strava:elapsed-time":
        v = need(s, "elapsed_time", "elapsed time")
        return v if isinstance(v, str) else (_hm(v), "")
    if kind == "strava:elevation":
        v = need(s, "elevation_gain", "elevation gain")
        return v if isinstance(v, str) else (str(int(round(alt_in(project, v)))), u["alt"])
    if kind in ("strava:avg-speed", "strava:max-speed"):
        v = need(s, "avg_speed" if kind == "strava:avg-speed" else "max_speed", "speed")
        return v if isinstance(v, str) else (f"{speed_in(project, v):.1f}", u["speed"])
    if not perf:
        return f"ride {act['id']} has no performance data — import get_activity_performance with --activity {act['id']}"
    if kind == "strava:avg-power":
        if not perf.get("has_device_watts"):
            return f"ride {act['id']} has no power meter data"
        return str(int(round(perf["average_watts"]))), "W"
    if kind in ("strava:avg-hr", "strava:max-hr"):
        if not perf.get("has_heartrate"):
            return f"ride {act['id']} has no heart rate"
        return str(int(round(perf["average_heartrate" if kind == "strava:avg-hr" else "max_heartrate"]))), "bpm"
    return f"unknown value {kind}"


def clip_ride_time(asset: dict, clip_t: float) -> float:
    return float(asset["strava"]["offset"]) + clip_t


def moment_value(project: Project, act: dict, kind: str, ride_a: float, ride_b: float) -> tuple[str, str] | str:
    """Value for a clip span [ride_a, ride_b] in ride seconds: distance into the ride at the start, mean temp."""
    from .telemetry import dist_in, units, unit_names

    st = Streams(act.get("streams") or {})
    name = _STREAM[kind]
    if not st.has(name):
        return f"ride {act['id']} has no {name} stream — import get_activity_streams with --activity {act['id']}"
    if kind == "strava:mile":
        v = st.at(name, ride_a)
        if v is None:
            return f"no Strava data at {fmt_ts(ride_a)} into ride {act['id']}"
        return f"{dist_in(project, v):.1f}", ""
    v = st.mean(name, ride_a, ride_b)
    if v is None:
        return f"no Strava temperature at {fmt_ts(ride_a)} into ride {act['id']}"
    return str(int(round(temp_in(project, v)))), "°F" if units(project) == "imperial" else "°C"


def live_value(project: Project, act: dict, kind: str, ride_t: float) -> str | None:
    """Rounded value shown at one moment; power is a 3 s average like a head unit."""
    from .telemetry import speed_in

    st = Streams(act.get("streams") or {})
    name = _STREAM[kind]
    if kind == "strava:power":
        if st._i(ride_t) is None:
            return None
        v = st.mean(name, ride_t - 2.5, ride_t)  # last 3 one-second samples
    else:
        v = st.at(name, ride_t)
    if v is None:
        return None
    if kind == "strava:speed":
        v = speed_in(project, v)
    return str(int(round(v)))


LIVE_UNITS = {"strava:power": "W", "strava:hr": "bpm", "strava:cadence": "rpm"}


# --------------------------------------------------------------------------
# Route map
# --------------------------------------------------------------------------


def in_maine(p: tuple[float, float] | list[float]) -> bool:
    s, n, w, e = MAINE
    return s <= p[0] <= n and w <= p[1] <= e


def route_points(act: dict) -> tuple[list[tuple[float, float, float]], bool]:
    """[(ride seconds, lat, lon)] of the ride's line and whether the ends were trimmed.
    Rides that start or end in Maine (home) lose their first and last TRIM_M metres."""
    st = Streams(act.get("streams") or {})
    if not st.has("location"):
        return [], False
    pts = [(t, p[0], p[1]) for t, p in zip(st.t, st.data["location"]) if p]
    if not pts:
        return [], False
    trim = in_maine(pts[0][1:]) or in_maine(pts[-1][1:])
    if not trim:
        return pts, False
    cum = [0.0]
    for a, b in zip(pts, pts[1:]):
        cum.append(cum[-1] + _m(a[1:], b[1:]))
    keep = [p for p, d in zip(pts, cum) if TRIM_M <= d <= cum[-1] - TRIM_M]
    return keep, True


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def describe(project: Project, act: dict) -> list[str]:
    from .telemetry import alt_in, dist_in, speed_in, unit_names

    u = unit_names(project)
    s = act.get("summary") or {}
    perf = act.get("performance") or {}
    parts = []
    if s.get("distance") is not None:
        parts.append(f"{dist_in(project, s['distance']):.1f} {u['dist']}")
    if s.get("moving_time") is not None:
        parts.append(f"{_hm(s['moving_time'])} moving")
    if s.get("elevation_gain") is not None:
        parts.append(f"{int(round(alt_in(project, s['elevation_gain'])))} {u['alt']} up")
    if s.get("avg_speed") is not None:
        parts.append(f"avg {speed_in(project, s['avg_speed']):.1f} / max {speed_in(project, s.get('max_speed') or 0):.1f} {u['speed']}")
    if perf.get("has_device_watts"):
        parts.append(f"{int(round(perf['average_watts']))} W avg")
    if perf.get("has_heartrate"):
        parts.append(f"HR {int(round(perf['average_heartrate']))} avg / {int(round(perf['max_heartrate']))} max")
    w = act.get("weather") or {}
    if w.get("temperature") is not None:
        parts.append(f"{int(round(temp_in(project, w['temperature'])))}° {w.get('condition', '')}".strip())
    have = [k for k in ("performance", "streams") if act.get(k)]
    lines = [f"{act['id']}  {str(act.get('start_local', ''))[:16]}  {act.get('name', '')!r}  ({act.get('location_summary', '')})",
             "    " + "  ·  ".join(parts),
             f"    data: summary{''.join(' + ' + k for k in have)}"
             + ("" if "streams" in have else f"  — live stats/route need streams (--activity {act['id']})")]
    if s.get("pr_count"):
        lines.append(f"    {s['pr_count']} PR(s) on this ride (Strava doesn't say which segments)")
    return lines


def print_list(project: Project) -> None:
    acts = all_activities(project)
    if not acts:
        log("no Strava rides imported yet — see reference/strava.md")
        return
    assets = all_assets(project)
    for act in acts:
        for line in describe(project, act):
            log(line)
        ids = [a["id"] for a in assets if (a.get("strava") or {}).get("activity") == act["id"]]
        log(f"    clips: {', '.join(ids) if ids else 'none linked'}")
    first = min(parse_date(a.get("start_local")) for a in acts)
    loose = [a["id"] for a in assets if a.get("kind") == "video" and not a.get("strava") and (a.get("tags") or {}).get("type") not in (None, "talking", "home", "cafe")
             and (parse_date(a.get("created")) or first) >= first.replace(hour=0, minute=0, second=0)]
    if loose:
        log(f"ride-looking clips since {first:%Y-%m-%d} with no Strava ride: {', '.join(loose)} (fetch that day's ride, or they were filmed off the ride)")


def show_lines(project: Project, asset: dict) -> list[str]:
    lk = asset.get("strava")
    if not lk:
        return []
    act = load_activity(project, lk["activity"])
    if act is None:
        return [f"  strava: linked to {lk['activity']} but that ride file is missing"]
    lines = [f"  strava: {fmt_ts(lk['offset'])} into ride {act['id']} {act.get('name', '')!r} ({lk['clock']} clock, shift {lk['shift']:+.0f}s)"]
    lines += ["  " + x.strip() for x in describe(project, act)[1:2]]
    st = Streams(act.get("streams") or {})
    if st.t:
        dur = _duration(asset)
        bits = []
        for kind in ("strava:speed", "strava:power", "strava:hr"):
            vals = [live_value(project, act, kind, lk["offset"] + t) for t in (0, dur / 2, dur)]
            vals = [v for v in vals if v is not None]
            if vals:
                bits.append(f"{VALUES[kind]} {'/'.join(vals)}{' ' + LIVE_UNITS.get(kind, '') if kind != 'strava:speed' else ''}")
        if bits:
            lines.append("  at the clip (start/mid/end): " + "  ·  ".join(bits))
    return lines
