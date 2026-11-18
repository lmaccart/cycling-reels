from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from .util import ReelsError

DEFAULTS: dict[str, Any] = {
    "brand": {
        "handle": "@yourhandle",
        "textStyle": "light",
        "captionStyle": "native",
        "accent": "#FFE14D",
        "endCard": False,
        "endCardCta": "follow for more rides",
    },
    "ingest": {"moveOriginals": True},
    "render": {
        "fps": 30,
        "width": 1080,
        "height": 1920,
        "crf": 20,
        "preset": "medium",  # x264 preset for the final upload; "slow" took ~45% longer for the same size and picture (2026-11-14 test)
        "remotionPreset": "veryfast",
        "fastPath": True,  # plain-footage reels: text stills + ffmpeg instead of a Chrome screenshot per frame (overlay.py)  # Remotion's intermediate file is re-encoded by the final encode, so encode it fast
        "maxBitrate": "20M",
        "audioBitrate": "128k",
        "loudness": -14,
        "hookSeconds": 2.6,
        "endCardSeconds": 1.6,
        "transitionFrames": 8,
        "concurrency": None,
        "browserExecutable": None,
        "keepStaging": False,
        "stageWorkers": None,  # clips staged at once (ffmpeg jobs); null = min(4, cores/2)
        "stageSphereWorkers": 1,  # of those, Metal 360 renders at once (they share the GPU + hardware encoder)
        "stageCache": True,  # reuse staged clips across `./reels render` runs (.cache/render/staged); --no-stage-cache bypasses
        "stageCacheGB": 10,  # size cap of that cache (least recently used go first); 0 = off
        "punchStageScale": 1.4,  # punch-in segments are staged at their punch scale, up to this (when the source has the detail); 1 = off
        # colour grade on every clip (grade.py): look none|house|moody (default none: colour-space fix only), strength 0–1.5, rescue auto|off|0–1 (grey-day local contrast + sky)
        "grade": {"look": "none", "strength": 1.0, "rescue": "auto"},
    },
    "audio": {
        "musicVolume": 0.5,
        "ambientVolume": 0.15,
        "ambientVolumeNoMusic": 0.8,
        "speechVolume": 1.0,
        "duckVolume": 0.18,
        "attachSongs": False,
        "instagramRideVolume": 20,
        "instagramSongUnderSpeech": 25,
        "natVolume": 1.0,  # ride audio inside a nat window
        "natMusicVolume": 0.2,  # music under a nat window (x musicVolume)
        "cleanNat": True,  # highpass + light denoise on clips with a nat window
        "rideOnlyMaxBoost": 12,  # dB: reels with no music get one linear gain (at most this) + a peak limiter
    },
    "sfx": {
        "gain": 0.7,
        "auto": False,  # user 2026-11-16: no automatic sound effects unless asked for (plan "autoSfx": true)
        "autoSpacing": 1.5,  # seconds: at most one automatic cue this often
        "autoMax": 4,  # automatic cues per reel (a payoff riser doesn't count)
        # event -> sound slug (or {"sound", "gain"}); null turns that event off
        "autoRules": {
            "transition": "whoosh-medium",
            "cameraMove": "whoosh-long",
            "textBeat": {"sound": "pop-soft", "gain": 0.45},
            "payoff": "hit-big",
            "payoffRiser": {"sound": "riser-1-5s", "gain": 0.5},
            "punchIn": "swish-whip",
        },
    },
    "captions": {"maxWordsPerPage": 6, "maxCharsPerPage": 32},
    "transcription": {
        "model": "small.en",
        "language": "en",
        "device": "auto",
        "computeType": "int8",
        "keyterms": [],
    },
    "schedule": {"timezone": "America/Chicago", "times": ["18:30"]},
    "hashtags": {"always": [], "pool": []},
    "upload": {"endpoint": None, "bucket": None, "prefix": "reels/", "publicBaseUrl": None, "region": "auto"},
    "tools": {"ffmpeg": None, "ffprobe": None},
    "prune": {"minFreeGB": 100},  # ./reels prune --apply refuses unless free disk is under this
    "editor": {"previewCacheGB": 8},  # budget for the editor's regenerable preview revisions + staged-media cache
    "units": "imperial",  # GPS numbers on screen: imperial (mph, ft) or metric (km/h, m)
}


def deep_merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if k.startswith("_"):
            continue
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def find_root() -> Path:
    env = os.environ.get("REELS_HOME")
    if env:
        return Path(env).resolve()
    here = Path.cwd().resolve()
    for p in [here, *here.parents]:
        if (p / "config.json").exists() and (p / "remotion").is_dir():
            return p
    return Path(__file__).resolve().parents[2]


class Project:
    """Paths and settings for one cycling-reels folder."""

    def __init__(
        self,
        root: Path | str | None = None,
        *,
        code_root: Path | str | None = None,
        media_root: Path | str | None = None,
        media_read_only: bool = False,
    ):
        """media_read_only: the editor may read library/media but never write it.

        media_root is only for the editor's sandboxed worker copies, whose data
        root is a private clone while the originals stay in the real library.
        """
        self.root = Path(root or find_root()).resolve()
        self.code_root = Path(code_root).resolve() if code_root is not None else self.root
        self._external_media_root = Path(os.path.abspath(media_root)) if media_root is not None else None
        self.read_only_media = bool(media_read_only) or self._external_media_root is not None
        cfg_path = self.root / "config.json"
        user: dict = {}
        if cfg_path.exists():
            try:
                user = json.loads(cfg_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                raise ReelsError(f"config.json is not valid JSON: {e}") from e
        self.config = deep_merge(deepcopy(DEFAULTS), user)
        from .media import configure

        configure(self.config["tools"].get("ffmpeg"), self.config["tools"].get("ffprobe"))

    # --- folders -----------------------------------------------------------
    @property
    def inbox(self) -> Path:
        return self.root / "inbox"

    @property
    def library(self) -> Path:
        return self.root / "library"

    @property
    def assets_dir(self) -> Path:
        return self.library / "assets"

    @property
    def media_dir(self) -> Path:
        return self._external_media_root or self.library / "media"

    @property
    def sheets_dir(self) -> Path:
        return self.library / "sheets"

    @property
    def transcripts_dir(self) -> Path:
        return self.library / "transcripts"

    @property
    def thumbs_dir(self) -> Path:
        return self.library / "thumbs"

    @property
    def plans_dir(self) -> Path:
        return self.root / "plans"

    @property
    def out_dir(self) -> Path:
        return self.root / "out"

    @property
    def music_dir(self) -> Path:
        return self.root / "music"

    @property
    def exhibits_dir(self) -> Path:
        """Study screenshots and diagrams the user dropped in, shown only as overlays (exhibits.py)."""
        return self.root / "exhibits"

    @property
    def remotion_dir(self) -> Path:
        return self.code_root / "remotion"

    @property
    def staging_dir(self) -> Path:
        return self.root / ".cache" / "staging"

    def ensure_dirs(self) -> None:
        dirs = [
            self.inbox,
            self.assets_dir,
            self.sheets_dir,
            self.transcripts_dir,
            self.thumbs_dir,
            self.plans_dir,
            self.out_dir,
            self.music_dir,
            self.exhibits_dir,
        ]
        # The editor never creates (or writes) the originals folder.
        if not self.read_only_media:
            dirs.append(self.media_dir)
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)

    # --- helpers -----------------------------------------------------------
    def rel(self, path: Path | str) -> str:
        p = Path(path).resolve()
        if self._external_media_root is not None:
            media = self._external_media_root.resolve()
            if _is_within(p, media):
                return (Path("library/media") / p.relative_to(media)).as_posix()
        return os.path.relpath(p, self.root)

    def abs(self, rel: Path | str) -> Path:
        p = Path(rel)
        if self._external_media_root is not None and not p.is_absolute() and p.parts[:2] == ("library", "media"):
            return self._external_media_root.joinpath(*p.parts[2:])
        return p if p.is_absolute() else (self.root / p)

    def assert_media_writable(self) -> None:
        """Fail closed in APIs that would modify files under library/media/."""
        if self.read_only_media:
            raise ReelsError("original media is read-only in the editor")

    def cfg(self, section: str, key: str) -> Any:
        return self.config.get(section, {}).get(key)


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False
