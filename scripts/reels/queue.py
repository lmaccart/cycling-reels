from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Project
from .util import ReelsError, log, now_iso, read_json, warn, write_json

STATUSES = ("rendered", "approved", "queued", "scheduled", "posted", "rejected")
LEAD = timedelta(minutes=15)  # a slot must be at least this far ahead: Metricool refuses past times, and scheduling takes a moment
# Instagram usernames: letters, digits, '.' and '_', up to 30, not starting/ending with '.' (and no '..').
HANDLE = re.compile(r"^@?([A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?)$")
MAX_COLLABORATORS = 5  # Instagram's limit per reel
# What a re-render keeps from the previous post.json: the Metricool post it may already be scheduled as
# (update that one instead of scheduling a duplicate) and the live reel's identity (analytics).
KEEP_ON_RERENDER = ("metricoolId", "reelId", "instagramUrl")


def handles(raw: object, field: str, errors: list[str], *, own: str | None = None, limit: int | None = None) -> list[str]:
    """Instagram usernames from a plan's post block, lowercased and without the '@'.
    Only accounts the user named — never guess a handle."""
    if raw in (None, [], ""):
        return []
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list):
        errors.append(f'post.{field} must be a list of Instagram handles like ["@yourclub"]')
        return []
    out: list[str] = []
    for h in items:
        m = HANDLE.match(h.strip()) if isinstance(h, str) else None
        if not m or ".." in m.group(1):
            errors.append(f"post.{field}: {h!r} isn't an Instagram handle (letters, digits, '.' and '_', up to 30)")
            continue
        name = m.group(1).lower()
        if own and name == own:
            errors.append(f"post.{field}: @{name} is this account")
            continue
        if name not in out:
            out.append(name)
    if limit is not None and len(out) > limit:
        errors.append(f"post.{field}: Instagram allows at most {limit}, got {len(out)}")
    return out


def post_extras(project: Project, post: dict, errors: list[str]) -> dict:
    """Validated collab and people-tag handles for post.json (both optional)."""
    own = str(project.cfg("brand", "handle") or "").strip().lstrip("@").lower() or None
    return {
        "collaborators": handles(post.get("collaborators"), "collaborators", errors, own=own, limit=MAX_COLLABORATORS),
        "peopleTags": handles(post.get("peopleTags"), "peopleTags", errors, own=own),
    }


def carry_over(prev: dict, post: dict) -> dict:
    """A re-render replaces the reel's media, not its paper trail: the status history, the Metricool
    post it may already be scheduled as, and (once posted) the live reel's Instagram id stay."""
    for k in KEEP_ON_RERENDER:
        if prev.get(k) and not post.get(k):
            post[k] = prev[k]
    post["history"] = list(prev.get("history") or []) + [{"status": "rendered", "at": post.get("renderedAt") or now_iso()}]
    return post


def handle_list(names: list[str] | None) -> str:
    return ", ".join(f"@{n}" for n in names or [])


def posts(project: Project) -> list[tuple[Path, dict]]:
    out = []
    for p in sorted(project.out_dir.glob("*/post.json")):
        try:
            out.append((p, read_json(p)))
        except Exception:  # noqa: BLE001
            continue
    return out


def _times(times: list[str]) -> list[str]:
    out = []
    for t in times:
        t = t.strip()
        if not re.fullmatch(r"([01]?\d|2[0-3]):[0-5]\d", t):
            raise ReelsError(f"bad time {t!r} — use 24h HH:MM, e.g. 18:30")
        h, m = t.split(":")
        out.append(f"{int(h):02d}:{m}")
    return sorted(set(out))


def song_note(p: dict) -> str | None:
    """How the reel's song gets on: attached by Metricool, or added by hand in the Instagram app."""
    ig = p.get("instagramAudio")
    if not ig:
        return None
    song = " — ".join(x for x in (ig.get("title"), ig.get("artist")) if x) or ig.get("audio")
    if ig.get("manual"):
        start = ig.get("start") or 0
        return f"add in the Instagram app: {song}, starting at {int(start // 60)}:{start % 60:04.1f}"
    return f"Instagram audio: {song} (attached when scheduled — song {ig.get('audioVolume')}, ride audio {ig.get('videoVolume')})"


def caption_block(p: dict) -> str:
    """Instagram caption as it should be pasted: caption, music credit (if the track needs one), hashtags."""
    caption = (p.get("caption") or "").strip()
    parts = [caption]
    credit = (p.get("musicCredit") or "").strip()
    if credit and credit.lower() not in caption.lower():
        parts.append(f"🎵 {credit}")
    tags = " ".join(p.get("hashtags") or [])
    if tags:
        parts.append(tags)
    return "\n\n".join(x for x in parts if x)




def write_queue_md(project: Project) -> Path:
    tz = ZoneInfo(project.cfg("schedule", "timezone"))
    items = [p for _, p in posts(project) if p.get("scheduledFor") and p.get("status") in ("queued", "scheduled", "posted")]
    items.sort(key=lambda p: p["scheduledFor"])
    now = datetime.now(tz)
    lines = [
        "# Posting queue",
        "",
        f"_Updated {now:%a %b} {now.day}, {now:%I:%M %p} ({tz.key}). "
        "queued = ready to schedule · scheduled = waiting in Metricool/Instagram · posted = live._",
        "",
    ]
    if not items:
        lines.append("Nothing queued yet.")
    for p in items:
        dt = datetime.fromisoformat(p["scheduledFor"])
        cover_at = f" (frame at {p['coverMs'] / 1000:.1f} s)" if isinstance(p.get("coverMs"), int) else ""
        lines += [
            f"## {dt:%a %b} {dt.day}, {dt:%I:%M %p} — {p['slug']} [{p['status']}]",
            "",
            f"- File: `{p['file']}` ({p.get('duration')} s, {p.get('sizeMB')} MB)",
            f"- Cover: `{p['cover']}`{cover_at}",
        ]
        if p.get("mediaUrl"):
            lines.append(f"- Media URL: {p['mediaUrl']}")
        if song_note(p):
            lines.append(f"- Song: {song_note(p)}")
        if p.get("collaborators"):
            lines.append(f"- Collab with: {handle_list(p['collaborators'])} (each accepts the invite in the Instagram app)")
        if p.get("peopleTags"):
            lines.append(f"- Tag: {handle_list(p['peopleTags'])}")
        if p.get("metricoolId"):
            lines.append(f"- Metricool post id: {p['metricoolId']}")
        lines += ["", "```", caption_block(p), "```", ""]
        if p.get("altText"):
            lines += [f"Alt text: {p['altText']}", ""]
    path = project.out_dir / "QUEUE.md"
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return path


def queue(project: Project, *, start: str | None = None, times: list[str] | None = None) -> list[dict]:
    """Gives every rendered reel a posting slot, one slot per configured time per day."""
    tz = ZoneInfo(project.cfg("schedule", "timezone"))
    slot_times = _times(times or project.cfg("schedule", "times"))
    now = datetime.now(tz)
    all_posts = posts(project)

    taken: set[tuple[str, str]] = set()
    for path, p in all_posts:
        when = p.get("scheduledFor")
        if not when:
            continue
        if p.get("status") == "queued" and datetime.fromisoformat(when).replace(tzinfo=tz) <= now:
            warn(f"{p['slug']}: its slot ({when}) passed before it was scheduled — re-queueing")
            p.update(status="approved", scheduledFor=None)
            write_json(path, p)
            continue
        if p.get("status") in ("queued", "scheduled", "posted"):
            taken.add((when[:10], when[11:16]))

    # Only reels the user approved get slots; rendered-but-unreviewed ones wait.
    waiting = [p["slug"] for _, p in all_posts if p.get("status") == "rendered"]
    if waiting:
        log(f"not queued (not approved yet): {', '.join(waiting)} — approve with ./reels mark <slug> approved")
    candidates = [(path, p) for path, p in all_posts if p.get("status") == "approved"]
    candidates.sort(key=lambda x: (x[1].get("postDate") or "9999-12-31", x[0].parent.name))
    day = date.fromisoformat(start) if start else now.date() + timedelta(days=1)
    queued: list[dict] = []

    def open_slot(d: str, t: str) -> bool:
        """Free, and not already past (a postDate of today, or --start today, can't use this morning's slot)."""
        return (d, t) not in taken and datetime.fromisoformat(f"{d}T{t}:00").replace(tzinfo=tz) > now + LEAD

    for path, p in candidates:
        if not (p.get("caption") or "").strip():
            warn(f"{p['slug']}: no caption — skipped (add post.caption to its plan and re-render)")
            continue
        slot = None
        pd = p.get("postDate")
        if pd:
            try:
                pd_date = date.fromisoformat(pd)
            except ValueError:
                pd_date = None
            if pd_date and pd_date >= now.date():
                slot = next(((pd, t) for t in slot_times if open_slot(pd, t)), None)
        while slot is None:
            slot = next(((day.isoformat(), t) for t in slot_times if open_slot(day.isoformat(), t)), None)
            if slot is None:
                day += timedelta(days=1)
        taken.add(slot)
        p.update(status="queued", scheduledFor=f"{slot[0]}T{slot[1]}:00", timezone=tz.key)
        write_json(path, p)
        queued.append(p)

    write_queue_md(project)
    for p in queued:
        log(f"queued {p['slug']} for {p['scheduledFor']} ({tz.key})")
    if not queued:
        log("no approved reels waiting for a slot")
    return queued


def mark(project: Project, slug: str, status: str, *, metricool_id: str | None = None, url: str | None = None, note: str | None = None) -> dict:
    if status not in STATUSES:
        raise ReelsError(f"status must be one of {', '.join(STATUSES)}")
    path = project.out_dir / slug / "post.json"
    if not path.exists():
        raise ReelsError(f"no rendered reel called {slug}")
    p = read_json(path)
    p["status"] = status
    if metricool_id:
        p["metricoolId"] = str(metricool_id)
    if url:
        p["mediaUrl"] = url
    if note:
        p["note"] = note
    if status in ("rendered", "approved", "rejected"):
        p["scheduledFor"] = None if status == "rejected" else p.get("scheduledFor")
    p.setdefault("history", []).append({"status": status, "at": now_iso()})
    write_json(path, p)
    write_queue_md(project)
    log(f"{slug} → {status}")
    from .review import auto_event

    # Status is an operational fact, not an inferred user preference or approval.
    auto_event(project, "status-change", f"Status recorded: {status}", slug=slug)
    return p


def export(project: Project, dest: str, slugs: list[str] | None = None) -> list[Path]:
    """Copies reels + caption files into a folder (e.g. iCloud Drive) for posting from a phone."""
    import shutil

    target = Path(dest).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    items = posts(project)
    if slugs:
        wanted = set(slugs)
        chosen = [p for _, p in items if p["slug"] in wanted]
        missing = wanted - {p["slug"] for p in chosen}
        if missing:
            raise ReelsError(f"no rendered reel called {', '.join(sorted(missing))}")
    else:
        chosen = [p for _, p in items if p.get("status") in ("approved", "queued", "scheduled")]
    if not chosen:
        log("nothing to export (approve or queue reels first, or name slugs)")
        return []
    written = []
    for p in sorted(chosen, key=lambda p: (p.get("scheduledFor") or "~", p["slug"])):
        stem = f"{p['scheduledFor'][:10]}-{p['slug']}" if p.get("scheduledFor") and not p["slug"].startswith(p["scheduledFor"][:10]) else p["slug"]
        video = target / f"{stem}.mp4"
        shutil.copy2(project.abs(p["file"]), video)
        when = ""
        if p.get("scheduledFor"):
            dt = datetime.fromisoformat(p["scheduledFor"])
            when = f"Post: {dt:%a %b} {dt.day}, {dt:%I:%M %p} ({p.get('timezone') or ''})\n\n"
        if p.get("instagramAudio"):  # posting by hand: the exported video has no song, so add it in the app
            ig = p["instagramAudio"]
            song = " — ".join(x for x in (ig.get("title"), ig.get("artist")) if x) or ig.get("audio")
            start = ig.get("start") or 0
            when += f"Add music in the Instagram app: {song}, from {int(start // 60)}:{start % 60:04.1f}\n\n"
        if p.get("collaborators"):
            when += f"Invite collaborators: {handle_list(p['collaborators'])}\n\n"
        if p.get("peopleTags"):
            when += f"Tag: {handle_list(p['peopleTags'])}\n\n"
        extra = f"\n\nAlt text: {p['altText']}" if p.get("altText") else ""
        (target / f"{stem}.txt").write_text(when + caption_block(p) + extra + "\n", encoding="utf-8")
        written.append(video)
        log(f"→ {video}")
    return written


def list_posts(project: Project) -> None:
    items = posts(project)
    if not items:
        log("no rendered reels yet")
        return
    for _, p in sorted(items, key=lambda x: (x[1].get("scheduledFor") or "~", x[1]["slug"])):
        when = (p.get("scheduledFor") or "")[:16].replace("T", " ") or "unscheduled"
        log(f"  {p.get('status', '?'):<9} {when:<16}  {p['slug']}  ({p.get('duration')}s)")
