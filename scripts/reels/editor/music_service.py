"""Music workflow for the editor: browse music/, search YouTube, add a track.

Music only ever comes from YouTube downloads (through ``reels.music.add``, the same code path as
``./reels music add``, which records source and credit) or files already in music/. Nothing is generated.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .. import music as music_lib
from ..util import ReelsError
from .protocol import ProtocolError


def _track(project, meta: dict) -> dict:
    start = meta.get("defaultStart")
    if start is None:
        start = meta.get("suggestedStart")
    file = meta.get("file") or ""
    path = project.abs(file) if file else None
    return {
        "slug": meta.get("slug"),
        "title": meta.get("title") or meta.get("slug"),
        "artist": meta.get("artist") or meta.get("channel") or "",
        "duration": meta.get("duration"),
        "defaultStart": start,
        "source": meta.get("source") or ("file" if meta.get("untracked") else None),
        "url": meta.get("url"),
        "credit": meta.get("credit"),
        "trending": bool(meta.get("instagram")),
        "caution": meta.get("caution"),
        "freeUse": bool(meta.get("freeUse")),
        "path": str(path) if path else None,
        "bpm": meta.get("bpm"),
    }


def list_tracks(project) -> dict:
    tracks = [_track(project, m) for m in music_lib.list_tracks(project)]
    tracks = [t for t in tracks if t["slug"]]
    return {"tracks": tracks}


def search(project, params: dict, *, searcher: Callable | None = None) -> dict:
    query = str(params.get("query") or "").strip()
    if not query:
        raise ProtocolError("invalid_query", "Type something to search YouTube for.")
    try:
        limit = max(1, min(int(params.get("limit", 8)), 20))
    except (TypeError, ValueError):
        limit = 8
    try:
        results = (searcher or music_lib.search)(project, query, limit=limit, free_only=bool(params.get("free")))
    except ReelsError as exc:
        raise ProtocolError("music_search_failed", str(exc)) from exc
    out = []
    for item in results:
        url = item.get("url")
        if not url:
            continue
        out.append({**item, "id": url})
    return {"query": query, "results": out}


_URL = re.compile(r"(?i)^https?://")


def validate_add(params: dict) -> dict:
    """Music comes from a YouTube/web URL only; the editor never takes local files or generates audio."""
    source = str(params.get("url") or params.get("id") or "").strip()
    if not _URL.match(source):
        raise ProtocolError("invalid_url", "Pass the YouTube link of the song (a search result's url).")
    instagram = params.get("instagram")
    if instagram is True:
        instagram = str(params.get("title") or "").strip() or None
    elif isinstance(instagram, str):
        instagram = instagram.strip() or None
    else:
        instagram = None
    name = str(params["name"]).strip() if params.get("name") else None
    return {"source": source, "instagram": instagram, "name": name}


def add_work(project, args: dict, *, adder: Callable | None = None) -> Callable:
    """Background job body. Progress is reported as log lines (yt-dlp itself gives no percentage)."""
    def work(context):
        context.log("Downloading audio from YouTube… 10%")
        context.check_cancelled()
        try:
            meta = (adder or music_lib.add)(project, args["source"], name=args["name"], instagram=args["instagram"])
        except ReelsError as exc:
            raise ProtocolError("music_add_failed", str(exc)) from exc
        context.log("Added to library 100%")
        return {"track": _track(project, meta)}
    return work
