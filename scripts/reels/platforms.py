"""TikTok and YouTube Shorts numbers from Metricool, beside the Instagram report.

Every reel goes to all three networks on one Metricool post (reference/publishing.md). Instagram stays the
main report (analytics.py); this module stores the other two networks' per-video pulls in
`analytics/<network>/`, matches each video to its reel in out/ and adds a short section per network to
out/PERFORMANCE.md. Missing numbers are unknown, never zero.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .analytics import MATCH_WINDOW, FUTURE_SLACK, EARLY_HOURS, _norm, _num, _parse_iso, _scheduled_utc
from .config import Project
from .queue import posts
from .util import ReelsError, log, now_iso, read_json, warn, write_json

# Metricool getAnalyticsDataByMetrics field ids -> row keys, per network.
NETWORKS: dict[str, dict[str, Any]] = {
    "tiktok": {
        "label": "TikTok",
        "prefixes": ("TKPO",),
        "fields": {
            "TKPO02": "timestamp", "TKPO05": "content", "TKPO03": "url", "TKPO06": "duration", "TKPO07": "views",
            "TKPO08": "likes", "TKPO09": "comments", "TKPO10": "shares", "TKPO11": "reach", "TKPO13": "fullWatchRate",
            "TKPO14": "totalWatch", "TKPO15": "avgWatch", "TKPO16": "forYou", "TKPO17": "follow", "TKPO18": "hashtag",
            "TKPO19": "sound", "TKPO20": "profile", "TKPO21": "search",
        },  # fmt: skip
        "default": ["TKPO02", "TKPO05", "TKPO03", "TKPO06", "TKPO07", "TKPO08", "TKPO09", "TKPO10", "TKPO11", "TKPO13",
                    "TKPO14", "TKPO15", "TKPO16", "TKPO17", "TKPO19", "TKPO20", "TKPO21"],  # fmt: skip
        "evolution": {
            "TKEV01": "videos", "TKEV02": "views", "TKEV03": "likes", "TKEV04": "comments", "TKEV05": "shares", "TKEV06": "interactions",
            "TKEV07": "followers", "TKEV08": "newFollowers", "TKEV09": "profileViews", "TKEV11": "reach", "TKEV12": "accountViews",
            "TKEV13": "accountLikes", "TKEV14": "accountComments", "TKEV15": "accountShares", "TKEV16": "gained", "TKEV17": "lost",
        },  # fmt: skip
        "evPrefix": "TKEV",
        "post": ("tiktokUrl", "tiktokId"),
    },
    "youtube": {
        "label": "YouTube Shorts",
        "prefixes": ("YTVV", "YTVP"),
        "fields": {
            f"{p}{k}": v
            for p in ("YTVV", "YTVP")
            for k, v in {"02": "timestamp", "17": "content", "04": "videoId", "05": "url", "06": "views", "07": "watchMinutes",
                         "08": "avgWatch", "09": "likes", "10": "dislikes", "11": "comments", "12": "shares"}.items()  # fmt: skip
        },
        "default": ["YTVV02", "YTVV17", "YTVV04", "YTVV05", "YTVV06", "YTVV07", "YTVV08", "YTVV09", "YTVV11", "YTVV12"],
        "evolution": {
            "YTEV01": "followers", "YTEV02": "views", "YTEV04": "videos", "YTEV05": "gained", "YTEV06": "lost",
            "YTEV10": "publishedLikes", "YTEV12": "publishedComments", "YTEV13": "publishedViews", "YTEV19": "publishedShares",
        },  # fmt: skip
        "evPrefix": "YTEV",
        "post": ("youtubeUrl", "youtubeId"),
    },
}
TEXT = {"timestamp", "content", "url", "videoId"}


def network_of(data: Any) -> str | None:
    """'tiktok' / 'youtube' when every field of a pull (or of its evolution part) belongs to that network."""
    if not isinstance(data, dict):
        return None
    fields = data.get("fields") or (data.get("evolution") or {}).get("fields") or []
    if not fields or not all(isinstance(f, str) for f in fields):
        return None
    for name, net in NETWORKS.items():
        if all(f.startswith(net["prefixes"]) or f.startswith(net["evPrefix"]) for f in fields):
            return name
    return None


def video_id(row: dict) -> str | None:
    if row.get("videoId"):
        return str(row["videoId"])
    url = str(row.get("url") or "")
    m = re.search(r"/video/(\d+)", url) or re.search(r"(?:v=|shorts/|youtu\.be/)([\w-]{6,})", url)
    return m.group(1) if m else (url.split("?")[0] or None)


def _clean_url(url: Any) -> str | None:
    return str(url).split("?")[0] if url else None  # drops Metricool's utm tracking


def parse_rows(network: str, data: Any) -> tuple[str | None, list[dict], list[dict]]:
    """Validate a pull into (pulledAt, video rows, evolution day rows)."""
    net = NETWORKS[network]
    if not isinstance(data, dict):
        raise ReelsError(f'{net["label"]} file needs {{"pulledAt", "fields", "rows"}}')
    pulled = data.get("pulledAt")
    if pulled is not None:
        try:
            _parse_iso(pulled)
        except ValueError:
            raise ReelsError(f"pulledAt {pulled!r} is not an ISO time like 2026-10-27T15:04:05Z")
    videos: list[dict] = []
    fields = data.get("fields")
    rows = data.get("rows")
    if fields and all(str(f).startswith(net["evPrefix"]) for f in fields):
        data = {"evolution": {"fields": fields, "rows": rows or []}}  # an evolution-only pull
        rows = None
    if rows is not None:
        if not isinstance(rows, list):
            raise ReelsError('"rows" must be a list')
        fields = fields or net["default"]
        unknown = [f for f in fields if f not in net["fields"]]
        if unknown:
            raise ReelsError(f"unknown {net['label']} field(s) {', '.join(map(str, unknown))}")
        keys = [net["fields"][f] for f in fields]
        for need in ("timestamp", "content", "url"):
            if need not in keys:
                raise ReelsError(f"{net['label']} fields must include {need}")
        for i, r in enumerate(rows):
            if not isinstance(r, list) or len(r) != len(keys):
                raise ReelsError(f"row {i + 1}: expected {len(keys)} values ({', '.join(fields)})")
            d: dict[str, Any] = {k: (None if v in (None, "") else v) for k, v in zip(keys, r)}
            try:
                d["postedAt"] = datetime.strptime(str(d["timestamp"]), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                raise ReelsError(f"row {i + 1}: timestamp {d['timestamp']!r} is not YYYYMMDDHHMMSS")
            for k in set(d) - TEXT - {"postedAt"}:
                d[k] = _num(d[k])
            d["content"] = d.get("content") or ""
            d["url"] = _clean_url(d.get("url"))
            d["id"] = video_id(d)
            if not d["id"]:
                raise ReelsError(f"row {i + 1}: no video url or id")
            if d.get("watchMinutes") is not None and d.get("avgWatch") is None and d.get("views"):
                d["avgWatch"] = d["watchMinutes"] * 60 / d["views"]
            videos.append(d)
    days: list[dict] = []
    ev = data.get("evolution")
    if ev:
        efields = ev.get("fields") or []
        unknown = [f for f in efields if f not in net["evolution"]]
        if unknown:
            raise ReelsError(f"unknown {net['label']} account field(s) {', '.join(map(str, unknown))}")
        for i, r in enumerate(ev.get("rows") or []):
            if not isinstance(r, list) or len(r) != len(efields) + 1 or not re.fullmatch(r"\d{8}", str(r[-1])):
                raise ReelsError(f"account row {i + 1}: expected {len(efields)} values and then the day as YYYYMMDD")
            day = {net["evolution"][f]: _num(v) for f, v in zip(efields, r)}
            day["date"] = f"{r[-1][:4]}-{r[-1][4:6]}-{r[-1][6:]}"
            days.append(day)
        days.sort(key=lambda d: d["date"])
    if rows is None and not ev:
        raise ReelsError('file needs "rows" (videos) and/or "evolution" (account by day)')
    return pulled, videos, days


def match_video(row: dict, plist: list[tuple[Any, dict]], network: str) -> tuple[Any, dict] | None:
    """The post that already carries this video's id, else the one whose caption shares the row's opening words
    (TikTok's description is the caption; a Short's title is its first line) posted within a few hours."""
    _, id_key = NETWORKS[network]["post"]
    for item in plist:
        if item[1].get(id_key) and str(item[1][id_key]) == row["id"]:
            return item
    content, when = _norm(re.sub(r"#\w+", "", row["content"])), _parse_iso(row["postedAt"])
    best, best_gap = None, None
    for item in plist:
        cap = _norm(re.sub(r"#\w+", "", item[1].get("caption", "")))
        n = min(60, len(cap), len(content))
        if n < 8 or cap[:n] != content[:n]:
            continue
        sched = _scheduled_utc(item[1])
        if sched is None:
            continue
        gap = abs(sched - when)
        if gap <= MATCH_WINDOW and (best_gap is None or gap < best_gap):
            best, best_gap = item, gap
    return best


def import_file(project: Project, path: str, network: str | None = None) -> None:
    try:
        data = read_json(path)
    except Exception as e:  # noqa: BLE001
        raise ReelsError(f"can't read {path}: {e}")
    network = network or network_of(data)
    if network not in NETWORKS:
        raise ReelsError("not a TikTok (TKPO/TKEV) or YouTube (YTVV/YTVP/YTEV) pull")
    net = NETWORKS[network]
    pulled, videos, days = parse_rows(network, data)
    if not pulled:
        pulled = now_iso()
        warn("no pulledAt in the file — using now")
    if _parse_iso(pulled) > datetime.now(timezone.utc) + FUTURE_SLACK:
        raise ReelsError(f"pulledAt {pulled} is in the future — write when the pull happened in UTC, e.g. {now_iso()}")
    stamp = _parse_iso(pulled).astimezone(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    snap = project.root / "analytics" / network / f"{stamp}.json"
    saved = {k: v for k, v in data.items() if k != "pulledAt"}
    write_json(snap, {"pulledAt": pulled, **saved})
    url_key, id_key = net["post"]
    plist = posts(project)
    log(f"saved {len(videos)} {net['label']} video(s)" + (f" and {len(days)} account day(s)" if days else "") + f" to {project.rel(snap)}")
    for row in videos:
        hit = match_video(row, plist, network)
        if not hit:
            log(f"  UNMATCHED {net['label']} {row['id']} ({row['postedAt'][:16]}Z) {row['content'][:50]!r}")
            continue
        pp, post = hit
        if post.get(id_key) != row["id"] or (row.get("url") and post.get(url_key) != row["url"]):
            post[id_key] = row["id"]
            if row.get("url"):
                post[url_key] = row["url"]
            write_json(pp, post)
        log(f"  matched  {post['slug']}  <- {net['label']} {row['id']}")


# ---------------------------------------------------------------- report


def latest(project: Project, network: str) -> tuple[dict[str, tuple[dict, str]], list[dict]]:
    """Latest snapshot per video -> {id: (row, pulledAt)}, and account days (a later pull's known value wins)."""
    vids: dict[str, tuple[dict, str]] = {}
    days: dict[str, dict] = {}
    folder = project.root / "analytics" / network
    for f in sorted(folder.glob("*.json")) if folder.exists() else []:
        try:
            pulled, rows, evs = parse_rows(network, read_json(f))
        except Exception as e:  # noqa: BLE001
            warn(f"skipping {network}/{f.name}: {e}")
            continue
        pulled = pulled or datetime.strptime(f.stem, "%Y-%m-%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()
        for r in rows:
            vids[r["id"]] = (r, pulled)
        for d in evs:
            cur = days.setdefault(d["date"], {"date": d["date"]})
            cur.update({k: v for k, v in d.items() if v is not None})
    return vids, [days[k] for k in sorted(days)]


def _ratio(a: float | None, b: float | None) -> float | None:
    return None if a is None or not b else a / b


def report(project: Project, network: str, ig_views: dict[str, float | None] | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    vids, days = latest(project, network)
    plist = posts(project)
    reels, unmatched, seen = [], [], set()
    for _, (row, pulled) in vids.items():
        hit = match_video(row, plist, network)
        if not hit or hit[1]["slug"] in seen:
            unmatched.append(row)
            continue
        post = hit[1]
        seen.add(post["slug"])
        length = post.get("duration") or row.get("duration")
        age = (_parse_iso(pulled) - _parse_iso(row["postedAt"])).total_seconds() / 86400
        reels.append({
            "slug": post["slug"], "date": post.get("postDate") or row["postedAt"][:10], "length": length,
            "views": row.get("views"), "igViews": (ig_views or {}).get(post["slug"]), "reach": row.get("reach"),
            "avgWatch": row.get("avgWatch"), "watchThrough": _ratio(row.get("avgWatch"), length),
            "fullWatch": row.get("fullWatchRate"), "likesView": _ratio(row.get("likes"), row.get("views")),
            "likes": row.get("likes"), "comments": row.get("comments"), "shares": row.get("shares"),
            "forYou": row.get("forYou"), "ageDays": age, "early": age * 24 < EARLY_HOURS, "url": row.get("url"),
        })  # fmt: skip
    reels.sort(key=lambda r: r["date"])
    return reels, unmatched, days


def _n(v: float | None, spec: str = ".0f") -> str:
    return "—" if v is None else format(v, spec)


def _pct(v: float | None, scale: float = 100) -> str:
    return "—" if v is None else f"{v * scale:.1f}%"


def section(project: Project, network: str, ig_views: dict[str, float | None] | None = None) -> list[str]:
    net = NETWORKS[network]
    reels, unmatched, days = report(project, network, ig_views)
    head = f"## {net['label']}"
    if not reels and not unmatched and not days:
        return [head, "", f"No {net['label']} numbers imported yet — pull them and run ./reels stats import <file> (reference/analytics.md).", ""]
    out = [head, "", f"Metricool's numbers for the same reels on {net['label']}, latest snapshot per video. — = Metricool sent nothing (unknown, not zero).", ""]
    known = [d for d in days if d.get("followers") is not None]
    if known:
        out += [f"Followers: {known[-1]['followers']:.0f} on {known[-1]['date']} (Metricool's count).", ""]
    if reels:
        out += ["| posted | slug | len s | views | IG views | reach | avg s | watch-thru | full watch | likes/view | likes | cmts | shares | age d |",
                "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]  # fmt: skip
        for r in reels:
            out.append(f"| {r['date']} | {r['slug']} | {_n(r['length'], '.1f')} | {_n(r['views'])} | {_n(r['igViews'])} | {_n(r['reach'])} | "
                       f"{_n(r['avgWatch'], '.1f')} | {_pct(r['watchThrough'])} | {_pct(r['fullWatch'], 1)} | {_pct(r['likesView'])} | "
                       f"{_n(r['likes'])} | {_n(r['comments'])} | {_n(r['shares'])} | {r['ageDays']:.1f}{'*' if r['early'] else ''} |")  # fmt: skip
        out.append("")
    if unmatched:
        out += [f"Unmatched {net['label']} videos (no reel in out/ with that caption and time):", ""]
        out += [f"- {r['id']} {r['postedAt'][:16]}Z {r['content'][:60]!r}" for r in unmatched] + [""]
    return out
