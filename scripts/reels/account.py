"""Account-level numbers by day (followers, account reach, accounts engaged) from Metricool's
Instagram "evolution" metrics, kept next to the per-reel analytics so the weekly review can see
whether the account is growing, not only whether single reels were watched.

Metricool returns one row per day: the requested metrics in order, then the day as YYYYMMDD.
Days come back unordered and most values are null on most days; null is unknown, never zero.
Instagram gives no follows-per-reel number through Metricool, so a day's follower change is shown
beside the reel posted that day as a description, not as proof that the reel caused it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .config import Project
from .util import ReelsError, log, now_iso, read_json, warn, write_json

FIELD_KEYS = {
    "IGEV01": "followers", "IGEV43": "gained", "IGEV44": "lost", "IGEV06": "reach", "IGEV42": "engaged", "IGEV05": "views",
}  # fmt: skip
DEFAULT_FIELDS = ["IGEV01", "IGEV43", "IGEV44", "IGEV06", "IGEV42"]
FUTURE_SLACK_SECONDS = 600


def is_account_pull(data: Any) -> bool:
    """True for a pull whose fields are all account (IGEV…) metrics."""
    fields = data.get("fields") if isinstance(data, dict) else None
    return bool(fields) and all(isinstance(f, str) and f.startswith("IGEV") for f in fields)


def _num(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def parse_days(data: Any) -> tuple[str | None, list[dict]]:
    """Validate {"pulledAt", "fields", "rows"} into [{"date": "YYYY-MM-DD", "followers": …}], oldest first."""
    if not isinstance(data, dict) or not isinstance(data.get("rows"), list):
        raise ReelsError('account file needs a "rows" list: {"pulledAt": "...", "fields": ["IGEV01", ...], "rows": [[..., "YYYYMMDD"]]}')
    fields = data.get("fields") or DEFAULT_FIELDS
    unknown = [f for f in fields if f not in FIELD_KEYS]
    if unknown:
        raise ReelsError(f"unknown account field(s) {', '.join(map(str, unknown))} — known: {', '.join(FIELD_KEYS)}")
    pulled = data.get("pulledAt")
    if pulled is not None:
        try:
            _parse_iso(pulled)
        except ValueError:
            raise ReelsError(f"pulledAt {pulled!r} is not an ISO time like 2026-10-27T15:04:05Z") from None
    days = []
    for i, r in enumerate(data["rows"]):
        if not isinstance(r, list) or len(r) != len(fields) + 1:
            raise ReelsError(f"row {i + 1}: expected {len(fields)} values ({', '.join(fields)}) and then the day, got {len(r) if isinstance(r, list) else type(r).__name__}")
        try:
            day = datetime.strptime(str(r[-1]), "%Y%m%d").date().isoformat()
        except ValueError:
            raise ReelsError(f"row {i + 1}: day {r[-1]!r} is not YYYYMMDD") from None
        days.append({"date": day, **{FIELD_KEYS[f]: _num(v) for f, v in zip(fields, r)}})
    return pulled, sorted(days, key=lambda d: d["date"])


def import_file(project: Project, path: str) -> None:
    try:
        data = read_json(path)
    except Exception as e:  # noqa: BLE001
        raise ReelsError(f"can't read {path}: {e}") from e
    pulled, days = parse_days(data)
    if not pulled:
        pulled = now_iso()
        warn("no pulledAt in the file — using now")
    if (_parse_iso(pulled) - datetime.now(timezone.utc)).total_seconds() > FUTURE_SLACK_SECONDS:
        raise ReelsError(f"pulledAt {pulled} is in the future — write when the pull happened in UTC, e.g. {now_iso()}")
    stamp = _parse_iso(pulled).astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    snap = project.root / "analytics" / "account" / f"{stamp}.json"
    write_json(snap, {"pulledAt": pulled, "fields": data.get("fields") or DEFAULT_FIELDS, "rows": data["rows"]})
    known = [d for d in days if d.get("followers") is not None]
    log(f"saved {len(days)} day(s) of account numbers to {project.rel(snap)}")
    log(f"  followers known on {len(known)} day(s)" + (f"; latest {known[-1]['followers']:.0f} on {known[-1]['date']}" if known else " — Metricool returned none"))


def days(project: Project) -> list[dict]:
    """Every imported day, oldest first. A later pull's value replaces an earlier one for the same
    day, but an unknown (null) never erases a value an earlier pull had."""
    merged: dict[str, dict] = {}
    for f in sorted((project.root / "analytics" / "account").glob("*.json")):
        try:
            _, rows = parse_days(read_json(f))
        except Exception as e:  # noqa: BLE001
            warn(f"skipping account/{f.name}: {e}")
            continue
        for d in rows:
            slot = merged.setdefault(d["date"], {"date": d["date"]})
            for k, v in d.items():
                if v is not None or k not in slot:
                    slot[k] = v
    return [merged[k] for k in sorted(merged)]


def growth(all_days: list[dict], since: str | None = None) -> dict:
    """Latest known follower count and the change over the known counts on or after `since`.
    Everything is None when no day has a follower count."""
    known = [d for d in all_days if d.get("followers") is not None and (not since or d["date"] >= since)]
    if not known:
        return {"followers": None, "asOf": None, "from": None, "change": None, "knownDays": 0}
    first, last = known[0], known[-1]
    return {"followers": last["followers"], "asOf": last["date"], "from": first["date"],
            "change": last["followers"] - first["followers"] if len(known) > 1 else None, "knownDays": len(known)}  # fmt: skip


def _n(v: float | None, sign: bool = False) -> str:
    if v is None:
        return "—"
    return f"{v:+.0f}" if sign else f"{v:.0f}"


def posted_reels(project: Project) -> list[dict]:
    """Every scheduled or posted reel with its local posting day, whether or not analytics has it yet."""
    from .queue import posts

    out = []
    for _, p in posts(project):
        day = p.get("postDate") or str(p.get("scheduledFor") or "")[:10]
        if day and p.get("status") in ("scheduled", "posted"):
            out.append({"date": day, "slug": p["slug"]})
    return out


def section(project: Project, reels: list[dict] | None = None) -> list[str]:
    """Markdown for out/PERFORMANCE.md. `reels` are {"date", "slug"} rows (default: scheduled/posted reels)."""
    reels = posted_reels(project) if reels is None else reels
    all_days = days(project)
    if not all_days:
        return ["## Account", "", "No account numbers imported yet — pull the IGEV metrics and run ./reels stats import <file> (reference/analytics.md).", ""]
    g = growth(all_days)
    lines = ["## Account", ""]
    if g["followers"] is None:
        lines.append("Followers: unknown — Metricool returned no follower count for these days.")
    else:
        change = f", {_n(g['change'], sign=True)} since {g['from']}" if g["change"] is not None else ""
        lines.append(f"Followers, as Metricool reports them: {_n(g['followers'])} on {g['asOf']}{change} (known on {g['knownDays']} of {len(all_days)} days).")
    lines += ["", "A follower change sits beside the reel posted that day; that's a description, not proof the reel caused it. "
              "— means Metricool gave no value (unknown, not zero).", "",
              "| day | reel posted | account reach | accounts engaged | followers | change | gained | lost |", "|---|---|---|---|---|---|---|---|"]  # fmt: skip
    by_day: dict[str, list[str]] = {}
    for r in reels:
        by_day.setdefault(r["date"], []).append(r["slug"])
    previous = None
    for d in all_days:
        followers = d.get("followers")
        change = (followers - previous) or None if followers is not None and previous is not None else None
        if followers is not None:
            previous = followers
        if all(d.get(k) in (None, 0) for k in ("reach", "engaged", "followers", "gained", "lost")) and d["date"] not in by_day:
            continue  # nothing happened and nothing was posted
        lines.append(f"| {d['date']} | {', '.join(by_day.get(d['date'], [])) or '—'} | {_n(d.get('reach'))} | {_n(d.get('engaged'))} | "
                     f"{_n(followers)} | {_n(change, sign=True)} | {_n(d.get('gained'))} | {_n(d.get('lost'))} |")  # fmt: skip
    return lines + [""]
