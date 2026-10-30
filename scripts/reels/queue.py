from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Project
from .util import ReelsError, log, now_iso, read_json, warn, write_json

STATUSES = ("rendered", "approved", "queued", "scheduled", "posted", "rejected")


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
        lines += [
            f"## {dt:%a %b} {dt.day}, {dt:%I:%M %p} — {p['slug']} [{p['status']}]",
            "",
            f"- File: `{p['file']}` ({p.get('duration')} s, {p.get('sizeMB')} MB)",
            f"- Cover: `{p['cover']}`",
        ]
        if p.get("mediaUrl"):
            lines.append(f"- Media URL: {p['mediaUrl']}")
        if song_note(p):
            lines.append(f"- Song: {song_note(p)}")
        if p.get("metricoolId"):
            lines.append(f"- Metricool post id: {p['metricoolId']}")
        lines += ["", "```", caption_block(p), "```", ""]
        if p.get("altText"):
            lines += [f"Alt text: {p['altText']}", ""]
        if p.get("firstComment"):
            lines += [f"First comment: {p['firstComment']}", ""]
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
                slot = next(((pd, t) for t in slot_times if (pd, t) not in taken), None)
        while slot is None:
            slot = next(((day.isoformat(), t) for t in slot_times if (day.isoformat(), t) not in taken), None)
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
