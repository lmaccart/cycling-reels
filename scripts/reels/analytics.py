from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .config import Project
from .library import asset_file, resolve_id
from .queue import posts
from .util import ReelsError, log, now_iso, read_json, warn, write_json

# Metricool getAnalyticsDataByMetrics fields -> row keys (default order when a file has no "fields")
FIELD_KEYS = {
    "IGRE02": "timestamp", "IGRE03": "content", "IGRE04": "reelId", "IGRE06": "url",
    "IGRE07": "comments", "IGRE10": "likes", "IGRE11": "reach", "IGRE12": "saved",
    "IGRE21": "shares", "IGRE23": "views", "IGRE24": "avgWatch", "IGRE25": "totalWatch",
    "IGRE27": "retention", "IGRE28": "holdRate", "IGRE29": "reposts",
}
DEFAULT_FIELDS = list(FIELD_KEYS)
NUMERIC = {"comments", "likes", "reach", "saved", "shares", "views", "avgWatch", "totalWatch", "retention", "holdRate", "reposts"}
MATCH_WINDOW = timedelta(hours=3)
EARLY_HOURS = 48
FUTURE_SLACK = timedelta(minutes=10)  # clock skew allowed on a pull's pulledAt


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_rows(data: Any) -> tuple[str | None, list[dict]]:
    """Validate a pulled file ({"pulledAt","fields","rows"} or bare {"rows"}) into row dicts."""
    if isinstance(data, list):
        data = {"rows": data}
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ReelsError('analytics file needs a "rows" list: {"pulledAt": "...", "fields": [...], "rows": [[...]]}')
    fields = data.get("fields") or DEFAULT_FIELDS
    unknown = [f for f in fields if f not in FIELD_KEYS]
    if unknown:
        raise ReelsError(f"unknown field(s) {', '.join(map(str, unknown))} — expected codes like IGRE23")
    keys = [FIELD_KEYS[f] for f in fields]
    for need in ("timestamp", "content", "reelId"):
        if need not in keys:
            raise ReelsError(f"fields must include {need} ({[k for k, v in FIELD_KEYS.items() if v == need][0]})")
    pulled = data.get("pulledAt")
    if pulled is not None:
        try:
            _parse_iso(pulled)
        except ValueError:
            raise ReelsError(f"pulledAt {pulled!r} is not an ISO time like 2026-10-27T15:04:05Z")
    rows = []
    for i, r in enumerate(data["rows"]):
        if not isinstance(r, list) or len(r) != len(keys):
            raise ReelsError(f"row {i + 1}: expected {len(keys)} values ({', '.join(fields)}), got {len(r) if isinstance(r, list) else type(r).__name__}")
        d: dict[str, Any] = {k: (None if v in (None, "") else v) for k, v in zip(keys, r)}
        try:
            d["postedAt"] = datetime.strptime(str(d["timestamp"]), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            raise ReelsError(f"row {i + 1}: timestamp {d['timestamp']!r} is not YYYYMMDDHHMMSS")
        if not d["reelId"]:
            raise ReelsError(f"row {i + 1}: no reelId")
        d["reelId"] = str(d["reelId"])
        d["content"] = d["content"] or ""
        for k in NUMERIC & set(d):
            d[k] = _num(d[k])
        rows.append(d)
    return pulled, rows


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _scheduled_utc(post: dict) -> datetime | None:
    s = post.get("scheduledFor")
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=ZoneInfo(post.get("timezone") or "UTC"))
    except Exception:  # noqa: BLE001
        return None


def match_row(row: dict, plist: list[tuple[Any, dict]]) -> tuple[Any, dict] | None:
    """The post whose reelId matches, else whose caption starts the row's content and posting time is within a few hours."""
    for item in plist:
        if item[1].get("reelId") and str(item[1]["reelId"]) == row["reelId"]:
            return item
    content, when = _norm(row["content"]), _parse_iso(row["postedAt"])
    best, best_gap = None, None
    for item in plist:
        cap = _norm(item[1].get("caption", ""))
        if not cap or not content.startswith(cap[:60]):
            continue
        sched = _scheduled_utc(item[1])
        if sched is None:
            continue
        gap = abs(sched - when)
        if gap <= MATCH_WINDOW and (best_gap is None or gap < best_gap):
            best, best_gap = item, gap
    return best


def import_file(project: Project, path: str) -> None:
    try:
        data = read_json(path)
    except Exception as e:  # noqa: BLE001
        raise ReelsError(f"can't read {path}: {e}")
    pulled, rows = parse_rows(data)
    if not pulled:
        pulled = now_iso()
        warn("no pulledAt in the file — using now")
    if _parse_iso(pulled) > datetime.now(timezone.utc) + FUTURE_SLACK:
        # Saving it would only have the weekly review skip it later as a "future snapshot".
        raise ReelsError(f"pulledAt {pulled} is in the future — that's usually local time written with a Z (or no offset). "
                         f"Write when the pull happened in UTC, e.g. {now_iso()}")  # fmt: skip
    stamp = _parse_iso(pulled).astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    snap = project.root / "analytics" / f"{stamp}.json"
    plist = posts(project)
    matched, unmatched = [], []
    for row in rows:
        hit = match_row(row, plist)
        if not hit:
            unmatched.append(row)
            continue
        pp, post = hit
        changed = False
        if row.get("url") and post.get("instagramUrl") != row["url"]:
            post["instagramUrl"], changed = row["url"], True
        if post.get("reelId") != row["reelId"]:
            post["reelId"], changed = row["reelId"], True
        if changed:
            write_json(pp, post)
        matched.append((post["slug"], row))
    write_json(snap, {"pulledAt": pulled, "fields": DEFAULT_FIELDS if not isinstance(data, dict) or not data.get("fields") else data["fields"], "rows": data["rows"] if isinstance(data, dict) else data})
    log(f"saved {len(rows)} row(s) to {project.rel(snap)}")
    for slug, row in matched:
        log(f"  matched  {slug}  <- reel {row['reelId']}")
    for row in unmatched:
        log(f"  UNMATCHED reel {row['reelId']} ({row['postedAt'][:16]}Z) {row['content'][:50]!r}")
    if unmatched:
        log("unmatched rows are kept in the snapshot; they're not reels from out/ (or the caption/time differs)")


# ---------------------------------------------------------------- report


def _latest_rows(project: Project) -> dict[str, tuple[dict, str]]:
    """Latest snapshot wins per reel -> {reelId: (row, pulledAt)}."""
    latest: dict[str, tuple[dict, str]] = {}
    for f in sorted((project.root / "analytics").glob("*.json")):
        try:
            data = read_json(f)
            pulled, rows = parse_rows(data)
        except Exception as e:  # noqa: BLE001
            warn(f"skipping {f.name}: {e}")
            continue
        for r in rows:
            latest[r["reelId"]] = (r, pulled or datetime.strptime(f.stem, "%Y-%m-%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat())
    return latest


def _ratio(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else a / b


def engagement(row: dict) -> float | None:
    """(likes + comments + shares + saves) per account reached — Metricool's "interactions" over reach.
    Unknown when any part is missing (missing is not zero)."""
    parts = [row.get(k) for k in ("likes", "comments", "shares", "saved")]
    if any(v is None for v in parts):
        return None
    return _ratio(sum(parts), row.get("reach"))


def interaction_metrics(row: dict) -> dict:
    """Ratios for how people responded, beyond watching: comments and all interactions per reach, and
    views per account reached (> 1 = people replayed it: what loops are for)."""
    return {"commentsReach": _ratio(row.get("comments"), row.get("reach")), "engagement": engagement(row),
            "viewsReach": _ratio(row.get("views"), row.get("reach"))}  # fmt: skip


def build_report(project: Project, since: str | None = None) -> tuple[list[dict], list[dict]]:
    latest = _latest_rows(project)
    if not latest:
        raise ReelsError("no analytics yet — pull from Metricool and run: ./reels stats import <file.json> (see reference/analytics.md)")
    plist = posts(project)
    reels, unmatched, seen = [], [], set()
    for rid, (row, pulled) in latest.items():
        hit = match_row(row, plist)
        if not hit or hit[1]["slug"] in seen:
            unmatched.append(row)
            continue
        seen.add(hit[1]["slug"])
        post = hit[1]
        slug = post["slug"]
        plan_p = project.out_dir / slug / "plan.json"
        plan = read_json(plan_p) if plan_p.exists() else {}
        hook = plan.get("hook")
        hook = hook.get("text") if isinstance(hook, dict) else hook
        music = post.get("music") or plan.get("music")
        track = music.get("track") if isinstance(music, dict) else music
        song = "none"
        if track:
            mp = project.music_dir / f"{track}.json"
            kind = "?"
            if mp.exists():
                kind = "trending" if read_json(mp).get("instagram") else "free"
            song = f"{track} ({kind})"
        sphere = False
        for seg in post.get("segments") or plan.get("segments") or []:
            try:  # older plans use short ids
                ap = asset_file(project, resolve_id(project, str(seg.get("asset", ""))))
            except ReelsError:
                continue
            if (read_json(ap).get("probe") or {}).get("sphere"):
                sphere = True
        length = post.get("duration")
        age = (_parse_iso(pulled) - _parse_iso(row["postedAt"])).total_seconds() / 86400
        r = {
            "slug": slug, "date": post.get("postDate") or row["postedAt"][:10], "format": plan.get("format") or "—",
            "hook": hook or "—", "song": song, "length": length, "segments": len(post.get("segments") or []),
            "sphere": sphere, "views": row.get("views"), "reach": row.get("reach"), "avgWatch": row.get("avgWatch"),
            "watchThrough": _ratio(row.get("avgWatch"), length), "hold": row.get("holdRate"),
            "likesReach": _ratio(row.get("likes"), row.get("reach")), "sharesReach": _ratio(row.get("shares"), row.get("reach")),
            "savesReach": _ratio(row.get("saved"), row.get("reach")), "comments": row.get("comments"),
            **interaction_metrics(row),
            "ageDays": age, "early": age * 24 < EARLY_HOURS, "url": row.get("url"),
        }
        reels.append(r)
    if since:
        reels = [r for r in reels if r["date"] >= since]
    reels.sort(key=lambda r: r["date"])
    return reels, unmatched


def _f(v: float | None, spec: str = ".0f", pct: bool = False, dash: str = "—") -> str:
    if v is None:
        return dash
    return f"{v * 100:.1f}%" if pct else format(v, spec)


def _hold(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}%"


COLS = [
    ("date", "posted"), ("slug", "slug"), ("format", "format"), ("hook", "hook"), ("song", "song"), ("length", "len s"),
    ("segments", "segs"), ("sphere", "360"), ("views", "views"), ("reach", "reach"), ("viewsReach", "views/reach"),
    ("avgWatch", "avg s"), ("watchThrough", "watch-thru"), ("hold", "3s hold"), ("likesReach", "likes/reach"),
    ("sharesReach", "shares/reach"), ("savesReach", "saves/reach"), ("comments", "cmts"), ("engagement", "eng/reach"),
    ("ageDays", "age d"),
]


def _times(v: float | None) -> str:
    return "—" if v is None else f"{v:.2f}×"


def _cell(r: dict, k: str) -> str:
    v = r[k]
    if k == "sphere":
        return "yes" if v else "no"
    if k == "hold":
        return _hold(v)
    if k == "viewsReach":
        return _times(v)
    if k in ("watchThrough", "likesReach", "sharesReach", "savesReach", "commentsReach", "engagement"):
        return _f(v, pct=True)
    if k in ("length", "avgWatch"):
        return _f(v, ".1f")
    if k == "ageDays":
        return f"{v:.1f}" + ("*" if r["early"] else "")
    if k in ("views", "reach", "comments"):
        return _f(v)
    return str(v)


def _table(reels: list[dict], md: bool, width: int | None = None) -> list[str]:
    rows = [[(_cell(r, k) if k != "hook" or not width else _cell(r, k)[:width]) for k, _ in COLS] for r in reels]
    heads = [h for _, h in COLS]
    if md:
        out = ["| " + " | ".join(heads) + " |", "|" + "---|" * len(heads)]
        out += ["| " + " | ".join(c.replace("|", "/") for c in row) + " |" for row in rows]
        return out
    ws = [max(len(h), *(len(row[i]) for row in rows)) for i, h in enumerate(heads)]
    out = ["  ".join(h.ljust(ws[i]) for i, h in enumerate(heads))]
    out += ["  ".join(c.ljust(ws[i]) for i, c in enumerate(row)) for row in rows]
    return out


def _mean(vals: list[float | None]) -> float | None:
    v = [x for x in vals if x is not None]
    return sum(v) / len(v) if v else None


def _summary(reels: list[dict]) -> list[str]:
    judged = [r for r in reels if not r["early"]]
    base = judged or reels
    lines = [f"Account averages over {len(base)} reel(s)" + ("" if judged else " (all under 48 h old, too early to judge)") + ":"]
    lines.append(
        f"  views {_f(_mean([r['views'] for r in base]))} · reach {_f(_mean([r['reach'] for r in base]))} · "
        f"avg watch {_f(_mean([r['avgWatch'] for r in base]), '.1f')} s · watch-through {_f(_mean([r['watchThrough'] for r in base]), pct=True)} · "
        f"3 s hold {_hold(_mean([r['hold'] for r in base]))} · likes/reach {_f(_mean([r['likesReach'] for r in base]), pct=True)} · "
        f"shares/reach {_f(_mean([r['sharesReach'] for r in base]), pct=True)} · saves/reach {_f(_mean([r['savesReach'] for r in base]), pct=True)}"
    )
    lines.append(
        f"  views/reach {_times(_mean([r['viewsReach'] for r in base]))} (replays) · comments/reach "
        f"{_f(_mean([r['commentsReach'] for r in base]), pct=True)} · engagement/reach {_f(_mean([r['engagement'] for r in base]), pct=True)}"
    )
    lines.append("")
    if len(judged) < 2:
        lines.append("Best/worst: need at least 2 reels older than 48 h.")
        return lines
    pct = lambda v: _f(v, pct=True)  # noqa: E731
    for key, label, fmt in (("hold", "3 s hold rate", _hold), ("watchThrough", "watch-through", pct), ("sharesReach", "shares per reach", pct),
                            ("engagement", "engagement per reach", pct)):  # fmt: skip
        have = [r for r in judged if r[key] is not None]
        if len(have) < 2:
            lines.append(f"{label}: not enough data")
            continue
        hi, lo = max(have, key=lambda r: r[key]), min(have, key=lambda r: r[key])
        lines.append(f"{label}: best {hi['slug']} ({fmt(hi[key])}), worst {lo['slug']} ({fmt(lo[key])})")
    return lines


def _by_group(reels: list[dict], key: str) -> list[str]:
    judged = [r for r in reels if not r["early"]]
    groups: dict[str, list[dict]] = {}
    for r in judged:
        groups.setdefault(r[key], []).append(r)
    if len(groups) < 2:
        return []
    lines = []
    for g, rs in sorted(groups.items()):
        lines.append(
            f"| {g} | {len(rs)} | {_hold(_mean([r['hold'] for r in rs]))} | {_f(_mean([r['watchThrough'] for r in rs]), pct=True)} | "
            f"{_f(_mean([r['sharesReach'] for r in rs]), pct=True)} | {_f(_mean([r['savesReach'] for r in rs]), pct=True)} | "
            f"{_f(_mean([r['engagement'] for r in rs]), pct=True)} | {_f(_mean([r['reach'] for r in rs]))} |"
        )
    return lines


def stats(project: Project, since: str | None = None) -> None:
    if since and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", since):
        raise ReelsError("--since takes YYYY-MM-DD")
    reels, unmatched = build_report(project, since)
    if not reels:
        raise ReelsError("no matched reels" + (f" since {since}" if since else ""))
    summary = _summary(reels)
    for line in _table(reels, md=False, width=28) + [""] + summary:
        log(line)
    if any(r["early"] for r in reels):
        log("\n* under 48 h old at pull time: too early to judge")
    if unmatched:
        log(f"\n{len(unmatched)} row(s) matched no reel in out/: " + ", ".join(f"{r['reelId']} ({r['postedAt'][:10]})" for r in unmatched))
    md = ["# Performance", "", f"Instagram reel analytics from Metricool, latest snapshot per reel. Generated {now_iso()}.", "",
          "`*` after age = under 48 h old at pull time, too early to judge. Watch-through = avg watch time / reel length. 3s hold = share of views that watched past 3 s. Likes, shares (sends) and saves are per reach. Small-account numbers are noisy.", ""]
    md += _table(reels, md=True) + [""] + ["## Averages and extremes", ""] + [("- " + l.strip()) if l.strip() else "" for l in summary] + [""]
    for key, title in (("format", "format"), ("song", "song"), ("sphere", "360 footage")):
        if key == "sphere":
            for r in reels:
                r["_s"] = "uses 360" if r["sphere"] else "no 360"
            rows = _by_group(reels, "_s")
        else:
            rows = _by_group(reels, key)
        if rows:
            md += [f"## By {title} (reels older than 48 h)", "", f"| {title} | reels | 3s hold | watch-thru | shares/reach | saves/reach | eng/reach | avg reach |",
                   "|---|---|---|---|---|---|---|---|"] + rows + [""]  # fmt: skip
    from .footage_report import section  # imports this module's formatters

    footage = section(project, reels)
    md += footage + [""]
    log("")
    for line in footage:
        log(line)
    if unmatched:
        md += ["## Unmatched rows", ""] + [f"- reel {r['reelId']} {r['postedAt'][:16]}Z {r['url'] or ''} {r['content'][:60]!r}" for r in unmatched] + [""]
    out = project.out_dir / "PERFORMANCE.md"
    out.write_text("\n".join(md), encoding="utf-8")
    log(f"\nwritten to {project.rel(out)}")
