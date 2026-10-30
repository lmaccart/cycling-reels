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
        "maxBitrate": "20M",
        "audioBitrate": "128k",
        "loudness": -14,
        "hookSeconds": 2.6,
        "endCardSeconds": 1.6,
        "transitionFrames": 8,
        "concurrency": None,
        "browserExecutable": None,
        "keepStaging": False,
        "punchStageScale": 1.4,  # punch-in segments are staged at their punch scale, up to this (when the source has the detail); 1 = off
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
        "auto": True,
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

    def __init__(self, root: Path | None = None):
        self.root = (root or find_root()).resolve()
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
        return self.library / "media"

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
    def remotion_dir(self) -> Path:
        return self.root / "remotion"

    @property
    def staging_dir(self) -> Path:
        return self.root / ".cache" / "staging"

    def ensure_dirs(self) -> None:
        for d in (
            self.inbox,
            self.assets_dir,
            self.media_dir,
            self.sheets_dir,
            self.transcripts_dir,
            self.thumbs_dir,
            self.plans_dir,
            self.out_dir,
            self.music_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    # --- helpers -----------------------------------------------------------
    def rel(self, path: Path | str) -> str:
        return os.path.relpath(Path(path).resolve(), self.root)

    def abs(self, rel: Path | str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else (self.root / p)

    def cfg(self, section: str, key: str) -> Any:
        return self.config.get(section, {}).get(key)
