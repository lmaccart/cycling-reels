"""Music workflow for the editor: browse music/, search YouTube, add a track.

Music only ever comes from YouTube downloads (through ``reels.music.add``, the same code path as
``./reels music add``, which records source and credit) or files already in music/. Nothing is generated.
"""
from __future__ import annotations

import itertools
import json
import re
import subprocess
import threading
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


def fast_search(project, query: str, *, limit: int = 8, free_only: bool = False,
                register: Callable[[subprocess.Popen], None] | None = None, timeout: float = 45) -> list[dict]:
    """YouTube search with --flat-playlist: one results page instead of one page per video, so it
    answers in a second or two. ``register`` gets the process so a newer search can kill it."""
    cmd = music_lib.ytdlp_cmd() + ["--flat-playlist", "--dump-json", "--ignore-errors", f"ytsearch{limit}:{query}"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if register:
        register(proc)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise ReelsError("YouTube search took too long. Try again.")
    results = []
    for line in (out or "").splitlines():
        try:
            info = json.loads(line)
        except json.JSONDecodeError:
            continue
        results.append(music_lib.summarize(info))
    if not results and proc.returncode not in (0, None) and proc.returncode > 0:
        raise ReelsError(music_lib.ytdlp_error(subprocess.CompletedProcess(cmd, proc.returncode, out, err), "ytsearch"))
    if free_only:
        results = [r for r in results if r["freeUse"] and not r["caution"]]
    return results


class BackgroundSearch:
    """Runs YouTube searches off the request threads. ``start`` returns at once with a searchId; the
    results arrive as a ``music.searchResults`` event. A new search kills the one still running, so
    stale results never land and typing fast never piles up yt-dlp processes."""

    def __init__(self, project, emit: Callable, *, searcher: Callable | None = None):
        self.project, self.emit = project, emit
        self.searcher = searcher
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._current: str | None = None
        self._proc: subprocess.Popen | None = None

    def start(self, params: dict) -> dict:
        query = str(params.get("query") or "").strip()
        if not query:
            raise ProtocolError("invalid_query", "Type something to search YouTube for.")
        try:
            limit = max(1, min(int(params.get("limit", 8)), 20))
        except (TypeError, ValueError):
            limit = 8
        free = bool(params.get("free"))
        sid = f"search-{next(self._ids)}"
        with self._lock:
            self._current = sid
            old, self._proc = self._proc, None
        if old is not None and old.poll() is None:
            old.kill()
        threading.Thread(target=self._run, args=(sid, query, limit, free), name="reels-music-search", daemon=True).start()
        return {"searchId": sid, "query": query}

    def cancel(self) -> None:
        with self._lock:
            self._current = None
            old, self._proc = self._proc, None
        if old is not None and old.poll() is None:
            old.kill()

    def _register(self, sid: str, proc: subprocess.Popen) -> None:
        with self._lock:
            if self._current == sid:
                self._proc = proc
                return
        proc.kill()

    def _run(self, sid: str, query: str, limit: int, free: bool) -> None:
        payload: dict[str, Any] = {"searchId": sid, "query": query}
        try:
            if self.searcher:
                results = self.searcher(self.project, query, limit=limit, free_only=free)
            else:
                results = fast_search(self.project, query, limit=limit, free_only=free,
                                      register=lambda proc: self._register(sid, proc))
            payload["results"] = [{**item, "id": item["url"]} for item in results if item.get("url")]
        except Exception as exc:  # reported to the panel, never raised into the service
            payload["error"] = str(exc) or "YouTube search failed."
        with self._lock:
            if self._current != sid:
                return  # superseded by a newer search
            self._proc = None
        self.emit("music.searchResults", payload, {})


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
