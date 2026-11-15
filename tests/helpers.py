"""Fixtures shared by the tests: a throwaway project folder with library assets whose "media" are
small placeholder files. The footage guard only fingerprints files, so plans can be checked and
built (`render.build`) without decoding any video; staging/rendering tests make real clips instead."""

from __future__ import annotations

import tempfile
from pathlib import Path

from reels.config import Project
from reels.library import save_asset
from reels.util import quick_hash, write_json


class TempProject:
    def __init__(self, case):
        self.temp = tempfile.TemporaryDirectory()
        case.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project = Project(self.root)
        self.project.ensure_dirs()

    def asset(self, aid: str, kind: str = "video", duration: float = 10.0, *, width: int = 1080, height: int = 1920,
              audio: bool = True, tags: dict | None = None, words: list[dict] | None = None, used: list[dict] | None = None,
              media: Path | None = None) -> dict:  # fmt: skip
        if media is None:
            media = self.project.media_dir / f"{aid}{'.mp4' if kind == 'video' else '.jpg'}"
            media.write_bytes(aid.encode() * 64)
        probe: dict = {"width": width, "height": height}
        if kind == "video":
            probe.update(duration=duration, fps=30.0, hasAudio=audio)
        asset = {"id": aid, "kind": kind, "media": self.project.rel(media), "hash": quick_hash(media), "originalName": f"{aid}.MOV",
                 "created": "2026-11-01T08:00:00", "probe": probe, "status": "tagged",
                 "tags": tags or {"type": "solo-ride", "shot": "pov", "quality": 4, "summary": aid, "moments": []},
                 "transcript": None, "used": used or []}  # fmt: skip
        if words is not None:
            rel = f"library/transcripts/{aid}.json"
            write_json(self.root / rel, {"asset": aid, "segments": [], "words": words})
            asset["transcript"] = rel
        save_asset(self.project, asset)
        return asset

    def plan(self, slug: str, segments: list[dict], **extra) -> Path:
        path = self.project.plans_dir / f"{slug}.json"
        write_json(path, {"slug": slug, "segments": segments, "autoSfx": False, **extra})
        return path


def words(*items: tuple[str, float, float], p: float = 0.95) -> list[dict]:
    return [{"w": w, "s": s, "e": e, "p": p} for w, s, e in items]


def live_workspace(fixture: "TempProject"):
    """A live editor workspace on a throwaway repo, rendering with this checkout's code."""
    from reels.editor.workspace import Workspace

    (fixture.root / "remotion").mkdir(exist_ok=True)
    return Workspace.live(fixture.root, code_root=Path(__file__).resolve().parents[1])
