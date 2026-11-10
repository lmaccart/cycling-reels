"""The Footage section of `./reels stats`: reel numbers grouped by what went into the reel
(out/<slug>/lineage.json). Descriptive only: nothing here writes to tags, scores or plans.
Every credit rule below is written up in .claude/skills/cycling-reels/reference/analytics.md"""

from __future__ import annotations

from typing import Callable

from .analytics import _f, _hold, _mean
from .config import Project
from .util import read_json, warn

MIN_REELS = 5  # groups smaller than this are flagged, not hidden
LENGTH_BANDS = ("under 11 s", "11–15 s", "over 15 s", "unknown length")
SHARE_BANDS = ("0%", "under 50%", "50% or more")
SHOT_BANDS = ("under 3 s", "3–5 s", "over 5 s", "unknown")
SCORE_BANDS = ("under 3.5", "3.5 to under 4", "4 or more", "unknown")


def length_band(seconds: float | None) -> str:
    if seconds is None:
        return "unknown length"
    return "under 11 s" if seconds < 11 else "11–15 s" if seconds <= 15 else "over 15 s"


def _share_band(share: float) -> str:
    return "0%" if share == 0 else "under 50%" if share < 0.5 else "50% or more"


def _shot_band(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    return "under 3 s" if seconds < 3 else "3–5 s" if seconds <= 5 else "over 5 s"


def _score_band(score: float | None) -> str:
    if score is None:
        return "unknown"
    return "under 3.5" if score < 3.5 else "3.5 to under 4" if score < 4 else "4 or more"


def _label(value: object, missing: str = "unknown") -> str:
    if value is None:
        return missing
    if isinstance(value, bool):
        return "yes" if value else "no"
    return f"{value:g}" if isinstance(value, float) else str(value)


def _weighted_score(entries: list[dict]) -> float | None:
    """Mean score weighted by share of runtime; entries with no score don't count."""
    scored = [(e["share"], e["tags"]["score"]) for e in entries if e["tags"].get("score") is not None and e["share"] > 0]
    weight = sum(w for w, _ in scored)
    return sum(w * s for w, s in scored) / weight if weight else None


def rows(project: Project, reels: list[dict]) -> tuple[list[dict], dict]:
    """Measured reels joined to their lineage, and the counts of what was left out."""
    joined, counts = [], {"early": 0, "noLineage": [], "backfilled": 0}
    for r in reels:
        if r["early"]:
            counts["early"] += 1
            continue
        path = project.out_dir / r["slug"] / "lineage.json"
        try:
            lineage = read_json(path)
            entries = lineage["segments"]
        except (OSError, ValueError, KeyError, TypeError) as e:
            if path.exists():
                warn(f"skipping unreadable {path.name} for {r['slug']}: {e}")
            counts["noLineage"].append(r["slug"])
            continue
        opening = next((e for e in entries if e["position"] == "first"), entries[0] if entries else None)
        if opening is None:
            counts["noLineage"].append(r["slug"])
            continue
        counts["backfilled"] += bool(lineage.get("backfilled"))
        joined.append({**r, "band": length_band(r["length"]), "opening": opening["tags"], "voiceShare": lineage["voiceShare"],
                       "faceShare": lineage["faceShare"], "avgShotSeconds": lineage["avgShotSeconds"], "thread": lineage.get("thread"),
                       "meanScore": _weighted_score(entries)})  # fmt: skip
    return joined, counts


def _table(data: list[dict], key: Callable[[dict], str], metrics: list[tuple[str, str, Callable]], title: str, *, spread: bool = False, order: tuple[str, ...] = ()) -> list[str]:
    """One row per (length band, group). A group counts the reels that have the first metric."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in data:
        if r[metrics[0][0]] is not None:
            groups.setdefault((r["band"], key(r)), []).append(r)
    head = ["length", title, "reels", *[h for _, h, _ in metrics], *(["range"] if spread else []), "sample"]
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for (band, value), rs in sorted(groups.items(), key=lambda kv: (LENGTH_BANDS.index(kv[0][0]), order.index(kv[0][1]) if kv[0][1] in order else -1, kv[0][1])):
        cells = [band, value, str(len(rs)), *[fmt(_mean([r[m] for r in rs])) for m, _, fmt in metrics]]
        if spread:
            m, _, fmt = metrics[0]
            vals = [r[m] for r in rs]
            cells.append(f"{fmt(min(vals))} to {fmt(max(vals))}")
        cells.append(f"too small to act on (under {MIN_REELS})" if len(rs) < MIN_REELS else "ok")
        out.append("| " + " | ".join(cells) + " |")
    return out if groups else ["No reels with this metric yet."]


def section(project: Project, reels: list[dict]) -> list[str]:
    data, counts = rows(project, reels)
    pct = lambda v: _f(v, pct=True)  # noqa: E731
    hold, watch = [("hold", "3s hold", _hold)], [("watchThrough", "watch-thru", pct)]
    response = [("sharesReach", "shares/reach", pct), ("commentsReach", "comments/reach", pct)]
    lines = ["## Footage (reels older than 48 h)", "",
             f"{len(data)} reel(s) with lineage. Left out: {counts['early']} under 48 h old, {len(counts['noLineage'])} without lineage"
             + (f" ({', '.join(counts['noLineage'])})" if counts["noLineage"] else "") + ". "
             f"{counts['backfilled']} use backfilled lineage: their tags are from the day of the backfill, not the render.",
             "",
             "Descriptive numbers only; nothing here changes a tag or score. Compare rows within one length band. "
             f"Groups under {MIN_REELS} reels are marked too small to act on.", ""]  # fmt: skip
    if not data:
        return lines + ["No measured reels with lineage yet (./reels stats backfill-lineage)."]

    lines += ["### 3 s hold, credited to the opening shot only", ""]
    for field, title in (("score", "opening score"), ("role", "opening role"), ("opener", "tagged opener"),
                         ("face", "opening face"), ("speaker", "opening speaker")):  # fmt: skip
        lines += _table(data, lambda r, f=field: _label(r["opening"].get(f)), hold, title) + [""]

    lines += ["### Watch-through, credited to the reel's mix", ""]
    lines += _table(data, lambda r: _share_band(r["voiceShare"]), watch, "voice share", order=SHARE_BANDS) + [""]
    lines += _table(data, lambda r: _share_band(r["faceShare"]), watch, "face share", order=SHARE_BANDS) + [""]
    lines += _table(data, lambda r: _shot_band(r["avgShotSeconds"]), watch, "avg shot", order=SHOT_BANDS) + [""]
    lines += _table(data, lambda r: _label(r["thread"], "no thread"), watch, "thread") + [""]

    lines += ["### Shares and comments, by format and thread only", ""]
    lines += _table(data, lambda r: _label(r["format"]), response, "format") + [""]
    lines += _table(data, lambda r: _label(r["thread"], "no thread"), response, "thread") + [""]

    lines += ["### Score calibration", ""]
    lines += _table(data, lambda r: _label(r["opening"].get("score")), hold, "opening score vs 3s hold", spread=True) + [""]
    lines += _table(data, lambda r: _score_band(r["meanScore"]), watch, "mean runtime-weighted score vs watch-thru", spread=True, order=SCORE_BANDS)
    return lines
