"""Series progress bar ("road to 5 W/kg"): the user's own FTP and weight, logged with a date in
library/progress.json, drawn as a bar from the series' first entry to the goal.

The numbers are ones the user gave (FTP and weight). The CLI only does arithmetic on them
(W/kg, watts still needed), so nothing on screen is typed into a plan by hand.
"""

from __future__ import annotations

import datetime as dt
import math
from typing import Any

from .config import Project
from .util import ReelsError, log, read_json, write_json

# Layout of the 1080x1920 frame (px): a full-width card under Instagram's top bar.
# The template (ProgressBar.tsx) uses the same numbers; top text and the hook move below it.
BAR_TOP, BAR_HEIGHT = 240, 200
FILL_FRAMES = 24  # fill animates from the previous entry to this one over 0.8 s at 30 fps
DEFAULT_GOAL = 5.0


def _path(project: Project):
    return project.library / "progress.json"


def load(project: Project) -> dict:
    p = _path(project)
    data = read_json(p) if p.exists() else {}
    wkg = data.setdefault("wkg", {})
    wkg.setdefault("goal", DEFAULT_GOAL)
    wkg.setdefault("entries", [])
    return data


def _date(value: str, what: str) -> str:
    try:
        return dt.date.fromisoformat(str(value)).isoformat()
    except ValueError as e:
        raise ReelsError(f"{what} must be a date like 2026-11-07, got {value!r}") from e


def set_entry(project: Project, *, ftp: float | None, weight: float | None, date: str | None = None,
              goal: float | None = None) -> dict:  # fmt: skip
    """Adds or replaces the entry for a date (default today). Both numbers come from the user."""
    data = load(project)
    wkg = data["wkg"]
    if goal is not None:
        if goal <= 0:
            raise ReelsError("goal must be above 0")
        wkg["goal"] = float(goal)
    if ftp is None and weight is None:
        if goal is None:
            raise ReelsError("give --ftp and --weight (and/or --goal)")
        write_json(_path(project), data)
        return {}
    if ftp is None or weight is None:
        raise ReelsError("give both --ftp (W) and --weight (kg) — W/kg needs both")
    if not (50 <= ftp <= 700) or not (30 <= weight <= 200):
        raise ReelsError(f"FTP {ftp} W / weight {weight} kg look wrong — FTP in watts, weight in kg")
    day = _date(date or dt.date.today().isoformat(), "--date")
    entry = {"date": day, "ftp": round(float(ftp), 1), "weightKg": round(float(weight), 1), "source": "user"}
    entries = [e for e in wkg["entries"] if e["date"] != day] + [entry]
    wkg["entries"] = sorted(entries, key=lambda e: e["date"])
    write_json(_path(project), data)
    return entry


def wkg_of(entry: dict) -> float:
    return float(entry["ftp"]) / float(entry["weightKg"])


def print_progress(project: Project) -> None:
    wkg = load(project)["wkg"]
    goal = float(wkg["goal"])
    if not wkg["entries"]:
        log(f"no entries yet (goal {goal:.1f} W/kg) — ./reels progress set --ftp <W> --weight <kg>")
        return
    log(f"road to {goal:g} W/kg — {_path(project).relative_to(project.root)}")
    for e in wkg["entries"]:
        v = wkg_of(e)
        log(f"  {e['date']}  FTP {e['ftp']:g} W  ·  {e['weightKg']:g} kg  →  {v:.2f} W/kg  ·  {_to_go(e, goal)}")


def _to_go(entry: dict, goal: float) -> str:
    need = math.ceil(goal * float(entry["weightKg"]) - float(entry["ftp"]) - 1e-9)
    return f"{need} W to go" if need > 0 else "goal hit"


def _fill(v: float, start: float, goal: float) -> float:
    if goal <= start:
        return 1.0
    return round(min(1.0, max(0.0, (v - start) / (goal - start))), 4)


def bars(project: Project, segs: list, total: int, fps: int, limit: int, warnings: list[str]) -> list[dict]:
    """Props for every segment with "progress": true or {"asOf", "at", "for", "label", "toGo"}."""
    errors: list[str] = []
    out: list[dict] = []
    data: dict | None = None
    for sg in segs:
        raw: Any = sg.spec.get("progress")
        if raw in (None, False):
            continue
        where = f"segment {sg.index + 1} progress"
        if raw is True:
            raw = {}
        if not isinstance(raw, dict):
            errors.append(f'{where}: use true or {{"asOf": "2026-11-07", "at": 0.3, "for": 3}}')
            continue
        if data is None:
            data = load(project)
        wkg = data["wkg"]
        entries = wkg["entries"]
        if not entries:
            errors.append(f"{where}: no FTP/weight logged yet — ./reels progress set --ftp <W> --weight <kg> --date <day>")
            continue
        if raw.get("asOf"):
            try:
                as_of = _date(raw["asOf"], f"{where} asOf")
            except ReelsError as e:
                errors.append(str(e))
                continue
            upto = [e for e in entries if e["date"] <= as_of]
            if not upto:
                errors.append(f"{where}: no entry on or before {as_of} (first is {entries[0]['date']})")
                continue
        else:
            upto = entries
            warnings.append(f"{where}: no asOf — showing the latest entry ({entries[-1]['date']}); "
                            "pin it with asOf so a later re-render shows the same numbers")  # fmt: skip
        cur, prev = upto[-1], (upto[-2] if len(upto) > 1 else upto[-1])
        goal, start = float(wkg["goal"]), wkg_of(entries[0])
        at = float(raw.get("at", 0.3))
        s = sg.start + round(at * fps)
        e = sg.end - round(0.15 * fps) if raw.get("for") is None else s + round(float(raw["for"]) * fps)
        s, e = int(max(0, min(s, total - 1))), int(max(0, min(e, total, limit)))
        if e - s < 15:
            warnings.append(f"{where}: too short to read — dropped")
            continue
        label = str(raw.get("label") or f"road to {goal:g} w/kg").strip().lower()
        v, pv = wkg_of(cur), wkg_of(prev)
        out.append({
            "startFrame": s, "endFrame": e, "label": label,
            "value": round(v, 2), "prevValue": round(pv, 2),
            "fill": _fill(v, start, goal), "prevFill": _fill(pv, start, goal),
            "goalText": f"goal {goal:.1f}", "unit": "w/kg",
            "note": _to_go(cur, goal) if raw.get("toGo", True) else "",
            "fillFrames": FILL_FRAMES if prev is not cur else 0,
            "asOf": cur["date"],
        })  # fmt: skip
    if errors:
        raise ReelsError("plan problems:\n  - " + "\n  - ".join(errors))
    return out


def clashes(bar_props: list[dict], stats: list[dict], routes: list[dict], fps: int) -> list[str]:
    """The bar spans the top of the frame where stat pills and the route map sit."""
    def hit(a: dict, b: dict) -> bool:
        return a["startFrame"] < b["endFrame"] and b["startFrame"] < a["endFrame"]

    out = []
    for b in bar_props:
        if any(hit(b, s) for s in stats):
            out.append(f"progress bar {b['startFrame'] / fps:.1f}s overlaps stat pills — they share the top of the frame; show one at a time")
        if any(hit(b, r) for r in routes):
            out.append(f"progress bar {b['startFrame'] / fps:.1f}s overlaps the route map — show one at a time")
    return out
