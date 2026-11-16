"""Versioned, declarative inspector inventory for native reel editing.

Descriptors describe editable JSON plan/config data only. They contain no Swift,
Python, JavaScript, executable expressions, or instructions for loading native
views. Renderer-provided additions are validated before being appended.
"""

from __future__ import annotations

import copy
import json
import math
import re
from typing import Any, Iterable, Mapping

from ..config import DEFAULTS

SCHEMA_VERSION = 1
CONTROL_TYPES = ("number", "boolean", "text", "enum", "color", "timing", "keyframe", "collection", "group")
TIMING_DOMAINS = ("source_seconds", "segment_seconds", "reel_seconds", "music_seconds")
_PLACEHOLDERS = {"{index}", "{item}", "{key}"}
_DENIED_PATH_TOKENS = {
    "__proto__", "prototype", "constructor", "__class__", "__dict__", "__globals__",
    "media", "originals", "workspace", "coderoot", "renderer", "script", "scripts",
    "command", "executable", "shell", "tool", "socket", "credential", "credentials",
    "permission", "permissions", "url", "filepath", "mediapath",
    "titlecards", "allowed", "consent", "approved", "approval", "upload",
    "schedule", "tools", "browserexecutable",
}
_FIELD_KEYS = {
    "id", "label", "path", "type", "default", "defaultSource", "required", "readOnly",
    "help", "unit", "precision", "min", "max", "step", "choices", "suggestions",
    "allowCustom", "timingDomain", "visibleWhen", "fields", "itemFields", "itemTarget",
    "itemTargets", "minItems", "maxItems", "acceptedShapes", "placeholder", "display",
}
DISPLAYS = ("slider",)  # presentation hints for number controls
_GROUP_KEYS = {"id", "label", "fields"}
_EXTRA_KEYS = {"target", "group", "field"}
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_POINTER_ESCAPE = re.compile(r"~(?![01])")


class DescriptorError(ValueError):
    """Raised when an extra inspector descriptor is not safe declarative data."""


def _field(
    field_id: str,
    label: str,
    path: str,
    control: str,
    **options: Any,
) -> dict[str, Any]:
    return {"id": field_id, "label": label, "path": path, "type": control, **options}


def _group(group_id: str, label: str, fields: list[dict[str, Any]]) -> dict[str, Any]:
    return {"id": group_id, "label": label, "fields": fields}


def _target(
    label: str,
    scope: str,
    groups: list[dict[str, Any]],
    *,
    extension_path: str | None = None,
) -> dict[str, Any]:
    target = {"label": label, "scopePath": scope, "groups": groups}
    if extension_path:
        target["extensionFields"] = {
            "control": "raw_json",
            "scopePath": extension_path,
            "label": "Extension fields",
            "policy": "Show existing unknown non-reserved properties here; do not create a new extension container.",
        }
    return target


def _textbeat_fields(path: str, default_at: float) -> list[dict[str, Any]]:
    return [
        _field("text", "Text", path + "/text", "text", required=True),
        _field("at", "Start", path + "/at", "timing", default=default_at, min=0, max=600,
               timingDomain="segment_seconds" if "{index}" in path else "reel_seconds", unit="s"),
        _field("for", "Duration", path + "/for", "timing", default=2.2, min=0, max=600,
               timingDomain="segment_seconds" if "{index}" in path else "reel_seconds", unit="s"),
        _field("position", "Position", path + "/position", "enum",
               default="lower" if not path.startswith("/segments/") else None,
               defaultSource="segment caption state" if path.startswith("/segments/") else None,
               choices=["top", "center", "lower"]),
        _field("style", "Style", path + "/style", "enum", defaultSource="config.brand.textStyle",
               choices=["light", "dark", "plain"]),
    ]


def _visual_fields(path: str, *, video: bool, cutaway: bool = False) -> list[dict[str, Any]]:
    label = "B-roll" if cutaway else "Video" if video else "Photo"
    fields = [
        _field("asset", "Footage", path + "/asset", "text", required=True),
        _field("moment", "Tagged moment", path + "/moment", "text",
               help="Moment ID or legacy index; selecting a moment supplies its source in/out."),
    ]
    if cutaway:
        fields.append(_field(
            "at", "Overlay start", path + "/at", "timing", default=0, min=0, max=600,
            timingDomain="segment_seconds", unit="s",
        ))
    if video:
        fields.extend([
            _field("in", "Source In", path + "/in", "timing", min=0,
                   timingDomain="source_seconds", unit="s",
                   visibleWhen={"path": path + "/moment", "exists": False}),
            _field("out", "Source Out", path + "/out", "timing", min=0,
                   timingDomain="source_seconds", unit="s",
                   visibleWhen={"path": path + "/moment", "exists": False},
                   help="Must be at least 0.3 seconds after In and no later than the source duration."),
            _field("speed", "Speed", path + "/speed", "number", default=1.0, min=0.25, max=4.0, step=0.05, unit="×"),
            _field("zoom", "Zoom", path + "/zoom", "number", default=1.0, min=1.0, max=3.0, step=0.01, unit="×"),
            _field("frame", "Frame mode", path + "/frame", "enum", default="crop",
                   choices=["crop", "blurfill", "fit"]),
        ])
        if not cutaway:
            fields.append(_field("captions", "Captions", path + "/captions", "enum", default="none",
                                 choices=["none", "transcript"]))
    else:
        fields.extend([
            _field("duration", "Duration", path + "/duration", "timing", default=2.5, min=0.8, max=12,
                   timingDomain="segment_seconds", unit="s"),
            _field("motion", "Photo motion", path + "/motion", "enum", default="zoom-in",
                   choices=["none", "zoom-in", "zoom-out", "pan-left", "pan-right", "pan-up", "pan-down"]),
            _field("fit", "Fit", path + "/fit", "enum", default="cover", choices=["cover", "blurfill"]),
        ])
    fields.extend([
        _field("focusX", "Horizontal focus", path + "/focusX", "number", default=0.5, min=0, max=1, step=0.01),
        _field("focusY", "Vertical focus", path + "/focusY", "number", default=0.5, min=0, max=1, step=0.01),
    ])
    if not cutaway:
        fields.append(_field("transition", "Transition in", path + "/transition", "enum", default="cut",
                             choices=["cut", "fade"], unit="frames",
                             help="Fade length is controlled by render.transitionFrames."))
    if video and not cutaway:
        fields.extend([
            _field("volume", "Clip audio volume", path + "/volume", "number", min=0, max=1.5, step=0.01,
                   defaultSource="config.audio.speechVolume / ambientVolume"),
            _field("clean", "Clean ride audio", path + "/clean", "boolean", default=False),
            _field("payoff", "Payoff moment", path + "/payoff", "boolean", default=False),
            _field("route", "Route overlay", path + "/route", "enum", default=False,
                   choices=[False, True, "strava"]),
            _field("progress", "Progress bar", path + "/progress", "group", default=False,
                   acceptedShapes=["boolean", "object"],
                   fields=[
                       _field("asOf", "As of date", path + "/progress/asOf", "text", unit="YYYY-MM-DD"),
                       _field("at", "Start", path + "/progress/at", "timing", default=0.3, min=0,
                              timingDomain="segment_seconds", unit="s"),
                       _field("for", "Duration", path + "/progress/for", "timing", min=0,
                              timingDomain="segment_seconds", unit="s"),
                       _field("label", "Label", path + "/progress/label", "text"),
                       _field("toGo", "Show remaining watts", path + "/progress/toGo", "boolean", default=True),
                   ]),
        ])
    return fields


def _grade_fields(path: str, *, project: bool = False) -> list[dict[str, Any]]:
    """Colour grade controls (scripts/reels/grade.py). Reel and clip levels override the project default."""
    base = path + "/grade"
    inherited = {} if project else {"defaultSource": "project grade" if not path else "reel grade"}
    fields = [
        _field("look", "Look", base + "/look", "enum", default="house", choices=["house", "moody", "none"],
               help="house = the shared look; none = colour fix only (no grade).", **inherited),
        _field("strength", "Strength", base + "/strength", "number", default=1.0, min=0, max=1.5, step=0.05,
               display="slider", unit="×", **inherited),
        _field("rescue", "Grey-day rescue", base + "/rescue", "boolean", default=True,
               help="Local contrast and a pull on bright sky, scaled by how overcast each shot is.", **inherited),
    ]
    if not project:
        fields += [
            _field("warmth", "Warmth", base + "/warmth", "number", default=0, min=-1, max=1, step=0.05,
                   display="slider", help="Cooler (−) or warmer (+) on top of the automatic white balance."),
            _field("exposure", "Exposure", base + "/exposure", "number", default=0, min=-1, max=1, step=0.05,
                   display="slider", unit="stops"),
        ]
    return [_field("grade", "Colour grade", base, "group", acceptedShapes=["object"], fields=fields)]


def _punch_common(path: str, default_scale: float) -> list[dict[str, Any]]:
    return [
        _field("scale", "Punch scale", path + "/scale", "number", default=default_scale, min=1.05, max=1.4, step=0.01, unit="×"),
        _field("for", "Hold", path + "/for", "timing", min=0.1, max=600,
               timingDomain="segment_seconds", unit="s", help="Unset holds to the word caption or next punch."),
        _field("x", "Origin X", path + "/x", "number", default=0.5, min=0, max=1, step=0.01),
        _field("y", "Origin Y", path + "/y", "number", default=0.5, min=0, max=1, step=0.01),
        _field("release", "Release", path + "/release", "enum", default="ease", choices=["ease", "cut"]),
    ]


def _sphere_fields(path: str) -> list[dict[str, Any]]:
    return [
        _field("projection", "Projection", path + "/projection", "enum", default="flat",
               choices=["flat", "tiny-planet"]),
        _field("view", "Default view", path + "/view", "group", default={"yaw": 0, "pitch": 0, "fov": 75, "roll": 0},
               fields=[
                   _field("yaw", "Yaw", path + "/view/yaw", "number", default=0, min=-360, max=360, unit="°"),
                   _field("pitch", "Pitch", path + "/view/pitch", "number", default=0, min=-90, max=90, unit="°"),
                   _field("fov", "Field of view", path + "/view/fov", "number", default=75, min=20, max=150, unit="°"),
                   _field("roll", "Roll", path + "/view/roll", "number", default=0, min=-180, max=180, unit="°"),
               ]),
        _field("path", "Camera path", path + "/path", "keyframe", minItems=2,
               help="At least two keyframes, at least 0.1 seconds apart; times are from segment start.",
               itemFields=[
                   _field("at", "At", path + "/path/{item}/at", "timing", min=0,
                          timingDomain="segment_seconds", unit="s"),
                   _field("yaw", "Yaw", path + "/path/{item}/yaw", "number", min=-360, max=360, unit="°"),
                   _field("pitch", "Pitch", path + "/path/{item}/pitch", "number", min=-90, max=90, unit="°"),
                   _field("fov", "Field of view", path + "/path/{item}/fov", "number", min=20, max=150, unit="°"),
                   _field("roll", "Roll", path + "/path/{item}/roll", "number", min=-180, max=180, unit="°"),
               ]),
    ]


def _base_schema() -> dict[str, Any]:
    pseg, pcut = "/segments/{index}", "/segments/{index}/cutaways/{item}"
    targets: dict[str, Any] = {
        "document": _target("Reel", "", [
            _group("identity", "Project", [
                _field("slug", "Slug", "/slug", "text", required=True),
                _field("format", "Format", "/format", "text"),
                _field("spine", "Story spine", "/spine", "enum", choices=["voice", "story", "sound", "trend"]),
                _field("loop", "Loop", "/loop", "boolean", default=False),
                _field("postDate", "Post date", "/postDate", "text", unit="YYYY-MM-DD"),
                _field("cover", "Cover frame time", "/cover", "timing", min=0,
                       timingDomain="reel_seconds", unit="s"),
            ]),
            _group("composition", "Composition", [
                _field("segments", "Segments", "/segments", "collection", required=True, minItems=1,
                       itemTargets=["segment.video", "segment.photo"]),
                _field("text", "Text beats", "/text", "collection", itemTarget="textBeat.reel"),
                _field("exhibits", "Exhibits", "/exhibits", "collection", itemTarget="exhibit.reel"),
            ]),
            _group("rhythm", "Music and timing", [
                _field("music", "Music", "/music", "group", acceptedShapes=["string", "object"],
                       fields=[
                           _field("track", "Track", "/music/track", "text", required=True),
                           _field("file", "Track file (legacy)", "/music/file", "text"),
                           _field("start", "Track start", "/music/start", "timing", min=0,
                                  timingDomain="music_seconds", unit="s",
                                  help="Accepts seconds or m:ss; absent uses the track's recommended start."),
                           _field("volume", "Music volume", "/music/volume", "number",
                                  default=DEFAULTS["audio"]["musicVolume"], step=0.01),
                           _field("cutOnBeat", "Cut on beat", "/music/cutOnBeat", "boolean", default=False),
                           _field("instagram", "Attach Instagram audio", "/music/instagram", "boolean",
                                  default=DEFAULTS["audio"]["attachSongs"]),
                           _field("audioVolume", "Attached song volume", "/music/audioVolume", "number", min=0, max=100, step=1),
                           _field("videoVolume", "Ride audio volume", "/music/videoVolume", "number", min=0, max=100, step=1),
                       ]),
                _field("musicDrops", "Music drops", "/musicDrops", "collection", itemTarget="musicDrop"),
                _field("punchOnBeats", "Punch on beats", "/punchOnBeats", "group",
                       acceptedShapes=["boolean", "object"],
                       fields=[
                           _field("every", "Every N beats", "/punchOnBeats/every", "number", default=4, min=1, max=32, step=1),
                           *_punch_common("/punchOnBeats", 1.1),
                       ]),
                _field("sfx", "Sound effects", "/sfx", "collection", itemTarget="sfxCue.reel"),
                _field("autoSfx", "Automatic sound effects", "/autoSfx", "boolean", default=True),
            ]),
            _group("titles", "Titles", [
                _field("hook", "Opening hook", "/hook", "group", acceptedShapes=["string", "object"],
                       fields=[
                            _field("text", "Text", "/hook/text", "text"),
                            _field("size", "Size", "/hook/size", "enum", default="normal",
                                   choices=["normal", "big"]),
                           _field("seconds", "Duration", "/hook/seconds", "timing", min=0,
                                  timingDomain="reel_seconds", unit="s",
                                  defaultSource="render.hookSeconds plus text length"),
                           _field("position", "Position", "/hook/position", "enum", default="top",
                                  choices=["top", "center"]),
                           _field("style", "Style", "/hook/style", "enum", choices=["light", "dark", "plain"]),
                       ]),
                _field("endCard", "End card", "/endCard", "group", default=False,
                       acceptedShapes=["boolean", "object"],
                       fields=[
                           _field("seconds", "Duration", "/endCard/seconds", "timing",
                                  default=DEFAULTS["render"]["endCardSeconds"], min=0,
                                  timingDomain="reel_seconds", unit="s"),
                           _field("handle", "Handle", "/endCard/handle", "text",
                                  defaultSource="config.brand.handle"),
                           _field("cta", "Call to action", "/endCard/cta", "text",
                                  defaultSource="config.brand.endCardCta"),
                           _field("style", "Style", "/endCard/style", "enum", choices=["light", "dark", "plain"]),
                       ]),
                _field("titleCard", "Series title card", "/titleCard", "group",
                       acceptedShapes=["null", "object"],
                       fields=[
                           _field("title", "Approved series title", "/titleCard/title", "text", required=True),
                           _field("episode", "Episode", "/titleCard/episode", "number", min=1, step=1),
                           _field("subtitle", "Subtitle", "/titleCard/subtitle", "text"),
                           _field("for", "Duration", "/titleCard/for", "timing", default=3, min=1.5, max=6,
                                  timingDomain="reel_seconds", unit="s"),
                           *[
                               _field(sound, label, f"/titleCard/{sound}", "text",
                                      help="Use false to omit this sound; otherwise provide a sound slug.")
                               for sound, label in (("theme", "Theme sound"), ("nail", "Nail sound"), ("creak", "Creak sound"))
                           ],
                       ]),
            ]),
            _group("colour", "Colour", _grade_fields("")),
            _group("publishing", "Post metadata", [
                _field("post", "Post", "/post", "group",
                       fields=[
                           _field("caption", "Caption", "/post/caption", "text"),
                           _field("altText", "Alt text", "/post/altText", "text"),
                           _field("hashtags", "Hashtags", "/post/hashtags", "collection",
                                  itemFields=[_field("hashtag", "Hashtag", "/post/hashtags/{item}", "text")]),
                           _field("collaborators", "Collaborators", "/post/collaborators", "collection",
                                  maxItems=5, itemFields=[_field("handle", "Handle", "/post/collaborators/{item}", "text")]),
                           _field("peopleTags", "People tags", "/post/peopleTags", "collection",
                                  itemFields=[_field("handle", "Handle", "/post/peopleTags/{item}", "text")]),
                       ]),
            ]),
        ], extension_path="/extensions"),
        "segment.video": _target("Video segment", pseg, [
            _group("source", "Source and framing", _visual_fields(pseg, video=True)),
            _group("overlays", "Overlays", [
                _field("text", "Text beats", pseg + "/text", "collection", itemTarget="textBeat.segment"),
                _field("stats", "Stat overlays", pseg + "/stats", "collection", itemTarget="stat"),
                _field("exhibits", "Exhibits", pseg + "/exhibits", "collection", itemTarget="exhibit.segment"),
            ]),
            _group("audio", "Clip audio", [
                _field("nat", "Natural sound windows", pseg + "/nat", "group",
                       acceptedShapes=["boolean", "object", "array"],
                       fields=[
                           _field("at", "Start", pseg + "/nat/{item}/at", "timing", default=0, min=0, max=600,
                                  timingDomain="segment_seconds", unit="s"),
                           _field("for", "Duration", pseg + "/nat/{item}/for", "timing", min=0.2, max=600,
                                  timingDomain="segment_seconds", unit="s"),
                           _field("volume", "Ride audio level", pseg + "/nat/{item}/volume", "number", min=0, max=4, step=0.05,
                                  defaultSource="config.audio.natVolume"),
                           _field("music", "Music level", pseg + "/nat/{item}/music", "number", min=0, max=1, step=0.01,
                                  defaultSource="config.audio.natMusicVolume"),
                       ]),
                _field("bleep", "Bleep / mute", pseg + "/bleep", "group",
                       acceptedShapes=["null", "boolean", "string", "object", "array"],
                       fields=[
                           _field("words", "Words", pseg + "/bleep/words", "collection",
                                  itemFields=[_field("word", "Word", pseg + "/bleep/words/{item}", "text")]),
                           _field("windows", "Timed windows", pseg + "/bleep/windows", "collection", itemTarget="bleep.window"),
                           _field("sound", "Bleep sound", pseg + "/bleep/sound", "enum", default="tone",
                                  choices=["tone", "mute"]),
                           _field("pad", "Padding", pseg + "/bleep/pad", "number", default=0.04, min=0, max=0.3, step=0.01, unit="s"),
                       ]),
                _field("sfx", "Sound effects", pseg + "/sfx", "collection", itemTarget="sfxCue.segment"),
            ]),
            _group("effects", "Punch, blur and 360", [
                _field("punch", "Punch-ins", pseg + "/punch", "collection",
                       acceptedShapes=["array", "word-selector"],
                       itemTargets=["punch.manual", "punch.words"]),
                _field("blur", "Blur patches", pseg + "/blur", "collection", itemTarget="blur.patch"),
                *_sphere_fields(pseg),
            ]),
            _group("colour", "Colour", _grade_fields(pseg)),
        ], extension_path=pseg + "/extensions"),
        "segment.photo": _target("Photo segment", pseg, [
            _group("source", "Photo source and framing", _visual_fields(pseg, video=False)),
            _group("overlays", "Overlays", [
                _field("text", "Text beats", pseg + "/text", "collection", itemTarget="textBeat.segment"),
                _field("stats", "Stat overlays", pseg + "/stats", "collection", itemTarget="stat"),
                _field("exhibits", "Exhibits", pseg + "/exhibits", "collection", itemTarget="exhibit.segment"),
            ]),
            _group("effects", "360 view", _sphere_fields(pseg)),
            _group("colour", "Colour", _grade_fields(pseg)),
        ], extension_path=pseg + "/extensions"),
        "cutaway.video": _target("Video cutaway", pcut, [
            _group("source", "B-roll source", _visual_fields(pcut, video=True, cutaway=True)),
            _group("effects", "360 view", _sphere_fields(pcut)),
            _group("colour", "Colour", _grade_fields(pcut)),
        ], extension_path=pcut + "/extensions"),
        "cutaway.photo": _target("Photo cutaway", pcut, [
            _group("source", "B-roll source", _visual_fields(pcut, video=False, cutaway=True)),
            _group("effects", "360 view", _sphere_fields(pcut)),
            _group("colour", "Colour", _grade_fields(pcut)),
        ], extension_path=pcut + "/extensions"),
    }

    targets["textBeat.reel"] = _target("Reel text beat", "/text/{index}", [
        _group("content", "Text", _textbeat_fields("/text/{index}", 0)),
    ])
    targets["textBeat.segment"] = _target("Segment text beat", pseg + "/text/{item}", [
        _group("content", "Text", _textbeat_fields(pseg + "/text/{item}", 0.15)),
    ])
    targets["punch.manual"] = _target("Manual punch", pseg + "/punch/{item}", [
        _group("zoom", "Zoom", [
            _field("at", "At", pseg + "/punch/{item}/at", "timing", min=0, max=600,
                   timingDomain="segment_seconds", unit="s"),
            *_punch_common(pseg + "/punch/{item}", 1.15),
        ]),
    ])
    targets["punch.words"] = _target("Word punch", pseg + "/punch", [
        _group("words", "Transcript words", [
            _field("words", "Trigger words", pseg + "/punch/words", "collection",
                   required=True, itemFields=[_field("word", "Word", pseg + "/punch/words/{item}", "text")]),
            *_punch_common(pseg + "/punch", 1.15),
        ]),
    ])
    targets["punch.onBeats"] = _target("Beat punches", "/punchOnBeats", [
        _group("rhythm", "Beat rhythm", [
            _field("every", "Every N beats", "/punchOnBeats/every", "number", default=4, min=1, max=32, step=1),
            *_punch_common("/punchOnBeats", 1.1),
        ]),
    ])
    targets["blur.patch"] = _target("Blur patch", pseg + "/blur/{item}", [
        _group("mask", "Blur mask", [
            _field("strength", "Strength", pseg + "/blur/{item}/strength", "number", default=28, min=4, max=80, step=1, unit="px"),
            _field("keys", "Tracked bounds", pseg + "/blur/{item}/keys", "keyframe",
                   minItems=1, itemFields=[
                       _field("at", "At", pseg + "/blur/{item}/keys/{item}/at", "timing", min=0,
                              timingDomain="segment_seconds", unit="s"),
                       _field("x", "Center X", pseg + "/blur/{item}/keys/{item}/x", "number", min=-0.5, max=1.5),
                       _field("y", "Center Y", pseg + "/blur/{item}/keys/{item}/y", "number", min=-0.5, max=1.5),
                       _field("w", "Width", pseg + "/blur/{item}/keys/{item}/w", "number", min=0, max=1.5),
                       _field("h", "Height", pseg + "/blur/{item}/keys/{item}/h", "number", min=0, max=1.5),
                   ]),
        ]),
    ])
    targets["stat"] = _target("Stat overlay", pseg + "/stats/{item}", [
        _group("content", "Stat", [
            _field("label", "Label", pseg + "/stats/{item}/label", "text"),
            _field("value", "Value or data source", pseg + "/stats/{item}/value", "text",
                   suggestions=[
                       "gps", "gps:max", "gps:avg", "strava:distance", "strava:moving-time",
                       "strava:elapsed-time", "strava:elevation", "strava:avg-speed", "strava:max-speed",
                       "strava:avg-power", "strava:avg-hr", "strava:max-hr", "strava:mile", "strava:temp",
                       "strava:speed", "strava:power", "strava:hr", "strava:cadence",
                   ]),
            _field("at", "Start", pseg + "/stats/{item}/at", "timing", default=0.3,
                   timingDomain="segment_seconds", unit="s"),
            _field("for", "Duration", pseg + "/stats/{item}/for", "timing", min=0,
                   timingDomain="segment_seconds", unit="s"),
        ]),
    ])
    targets["route"] = _target("Route overlay", pseg, [
        _group("map", "Route source", [
            _field("route", "Route", pseg + "/route", "enum", default=False,
                   choices=[False, True, "strava"]),
        ]),
    ])
    targets["progress"] = _target("Progress bar", pseg + "/progress", [
        _group("bar", "Progress", [
            _field("asOf", "As of", pseg + "/progress/asOf", "text", unit="YYYY-MM-DD"),
            _field("at", "Start", pseg + "/progress/at", "timing", default=0.3,
                   timingDomain="segment_seconds", unit="s"),
            _field("for", "Duration", pseg + "/progress/for", "timing", min=0,
                   timingDomain="segment_seconds", unit="s"),
            _field("label", "Label", pseg + "/progress/label", "text"),
            _field("toGo", "Show watts to goal", pseg + "/progress/toGo", "boolean", default=True),
        ]),
    ])
    targets["bleep.window"] = _target("Bleep window", pseg + "/bleep/windows/{item}", [
        _group("timing", "Mute window", [
            _field("at", "Start", pseg + "/bleep/windows/{item}/at", "timing", min=0,
                   timingDomain="segment_seconds", unit="s"),
            _field("for", "Duration", pseg + "/bleep/windows/{item}/for", "timing", min=0.05, max=10,
                   timingDomain="segment_seconds", unit="s"),
        ]),
    ])

    targets["hook"] = _target("Opening hook", "/hook", [
        _group("content", "Hook", [
            _field("text", "Text", "/hook/text", "text"),
            _field("size", "Size", "/hook/size", "enum", default="normal", choices=["normal", "big"]),
            _field("seconds", "Duration", "/hook/seconds", "timing", min=0,
                   timingDomain="reel_seconds", unit="s", defaultSource="render.hookSeconds plus text length"),
            _field("position", "Position", "/hook/position", "enum", default="top", choices=["top", "center"]),
            _field("style", "Style", "/hook/style", "enum", choices=["light", "dark", "plain"]),
        ]),
    ])
    targets["music"] = _target("Music", "/music", [
        _group("track", "Track", [
            _field("track", "Track", "/music/track", "text", required=True,
                   help="A track slug from the Music tab (music/ folder)."),
            _field("start", "Start", "/music/start", "timing", min=0,
                   timingDomain="music_seconds", unit="s",
                   help="Seconds or m:ss into the song; empty uses the track's recommended start."),
            _field("volume", "Volume", "/music/volume", "number", min=0, max=1.5, step=0.01,
                   default=DEFAULTS["audio"]["musicVolume"],
                   help="Leave at the default; 0.8 was too loud over ride sounds."),
            _field("cutOnBeat", "Cut on beat", "/music/cutOnBeat", "boolean", default=False),
        ]),
    ])
    targets["endCard"] = _target("End card", "/endCard", [
        _group("content", "End card", [
            _field("seconds", "Duration", "/endCard/seconds", "timing",
                   default=DEFAULTS["render"]["endCardSeconds"], min=0,
                   timingDomain="reel_seconds", unit="s"),
            _field("handle", "Handle", "/endCard/handle", "text", defaultSource="config.brand.handle"),
            _field("cta", "Call to action", "/endCard/cta", "text", defaultSource="config.brand.endCardCta"),
            _field("style", "Style", "/endCard/style", "enum", choices=["light", "dark", "plain"]),
        ]),
    ])
    targets["titleCard"] = _target("Series title card", "/titleCard", [
        _group("content", "Approved series title", [
            _field("title", "Title", "/titleCard/title", "text", required=True),
            _field("episode", "Episode", "/titleCard/episode", "number", min=1, step=1),
            _field("subtitle", "Subtitle", "/titleCard/subtitle", "text"),
            _field("for", "Duration", "/titleCard/for", "timing", default=3, min=1.5, max=6,
                   timingDomain="reel_seconds", unit="s"),
        ]),
        _group("sound", "Title card sounds", [
            _field(sound, label, f"/titleCard/{sound}", "text", help="False omits this sound; otherwise set a sound slug.")
            for sound, label in (("theme", "Theme"), ("nail", "Nail"), ("creak", "Creak"))
        ]),
    ])
    targets["exhibit.reel"] = _exhibit_target("/exhibits/{index}", "Reel exhibit")
    targets["exhibit.segment"] = _exhibit_target(pseg + "/exhibits/{item}", "Segment exhibit")
    targets["exhibit.chart"] = _chart_target("/exhibits/{item}/chart")
    targets["musicDrop"] = _target("Music drop", "/musicDrops/{index}", [
        _group("silence", "Music drop", [
            _field("at", "Start", "/musicDrops/{index}/at", "timing", min=0, max=600,
                   timingDomain="reel_seconds", unit="s"),
            _field("for", "Duration", "/musicDrops/{index}/for", "timing", default=0.5, min=0.1, max=3,
                   timingDomain="reel_seconds", unit="s"),
        ]),
    ])
    targets["sfxCue.reel"] = _sfx_target("/sfx/{index}", "Reel sound cue", "reel_seconds")
    targets["sfxCue.segment"] = _sfx_target(pseg + "/sfx/{item}", "Segment sound cue", "segment_seconds")

    targets["post"] = _target("Post metadata", "/post", [
        _group("copy", "Caption and accessibility", [
            _field("caption", "Caption", "/post/caption", "text"),
            _field("altText", "Alt text", "/post/altText", "text"),
            _field("hashtags", "Hashtags", "/post/hashtags", "collection",
                   itemFields=[_field("hashtag", "Hashtag", "/post/hashtags/{item}", "text")]),
        ]),
        _group("people", "People", [
            _field("collaborators", "Collaborators", "/post/collaborators", "collection", maxItems=5,
                   itemFields=[_field("handle", "Handle", "/post/collaborators/{item}", "text")]),
            _field("peopleTags", "People tags", "/post/peopleTags", "collection",
                   itemFields=[_field("handle", "Handle", "/post/peopleTags/{item}", "text")]),
        ]),
    ])

    render_defaults = DEFAULTS["render"]
    targets["config.render"] = _target("Project render defaults", "/render", [
        _group("format", "Frame format", [
            _field("fps", "Frames per second", "/render/fps", "number", default=render_defaults["fps"], step=1, unit="fps"),
            _field("width", "Width", "/render/width", "number", default=render_defaults["width"], step=2, unit="px"),
            _field("height", "Height", "/render/height", "number", default=render_defaults["height"], step=2, unit="px"),
            _field("transitionFrames", "Fade length", "/render/transitionFrames", "number",
                   default=render_defaults["transitionFrames"], step=1, unit="frames"),
            _field("hookSeconds", "Hook default duration", "/render/hookSeconds", "timing",
                   default=render_defaults["hookSeconds"], timingDomain="reel_seconds", unit="s"),
            _field("endCardSeconds", "End-card default duration", "/render/endCardSeconds", "timing",
                   default=render_defaults["endCardSeconds"], timingDomain="reel_seconds", unit="s"),
        ]),
        _group("colour", "Colour grade for every reel", _grade_fields("/render", project=True)),
    ])
    audio_defaults = DEFAULTS["audio"]
    targets["config.audio"] = _target("Project audio defaults", "/audio", [
        _group("mix", "Mix levels", [
            _field(key, label, f"/audio/{key}", "number", default=audio_defaults[key], step=0.01)
            for key, label in (
                ("musicVolume", "Music volume"), ("ambientVolume", "Ambient with music"),
                ("ambientVolumeNoMusic", "Ambient without music"), ("speechVolume", "Speech volume"),
                ("duckVolume", "Music under speech"), ("natVolume", "Natural sound volume"),
                ("natMusicVolume", "Music under natural sound"), ("rideOnlyMaxBoost", "Maximum ride-only boost"),
            )
        ] + [
            _field("attachSongs", "Attach Instagram songs by default", "/audio/attachSongs", "boolean",
                   default=audio_defaults["attachSongs"]),
            _field("instagramRideVolume", "Attached-song ride volume", "/audio/instagramRideVolume", "number",
                   default=audio_defaults["instagramRideVolume"], step=1),
            _field("instagramSongUnderSpeech", "Attached song under speech", "/audio/instagramSongUnderSpeech", "number",
                   default=audio_defaults["instagramSongUnderSpeech"], step=1),
            _field("cleanNat", "Clean natural sound", "/audio/cleanNat", "boolean", default=audio_defaults["cleanNat"]),
        ]),
    ])
    brand_defaults = DEFAULTS["brand"]
    targets["config.brand"] = _target("Project brand defaults", "/brand", [
        _group("identity", "Brand", [
            _field("handle", "Handle", "/brand/handle", "text", default=brand_defaults["handle"]),
            _field("textStyle", "Default text style", "/brand/textStyle", "enum",
                   default=brand_defaults["textStyle"], choices=["light", "dark", "plain"]),
            _field("captionStyle", "Caption style", "/brand/captionStyle", "enum",
                   default=brand_defaults["captionStyle"], choices=["native", "highlight"]),
            _field("accent", "Accent color", "/brand/accent", "color", default=brand_defaults["accent"]),
            _field("endCard", "Enable end card by default", "/brand/endCard", "boolean",
                   default=brand_defaults["endCard"]),
            _field("endCardCta", "End-card call to action", "/brand/endCardCta", "text",
                   default=brand_defaults["endCardCta"]),
        ]),
    ])
    sfx_defaults = DEFAULTS["sfx"]
    targets["config.sfx"] = _target("Project sound-effect defaults", "/sfx", [
        _group("automatic", "Automatic cues", [
            _field("gain", "Default gain", "/sfx/gain", "number", default=sfx_defaults["gain"], step=0.01),
            _field("auto", "Automatic cues enabled", "/sfx/auto", "boolean", default=sfx_defaults["auto"]),
            _field("autoSpacing", "Minimum spacing", "/sfx/autoSpacing", "timing",
                   default=sfx_defaults["autoSpacing"], timingDomain="reel_seconds", unit="s"),
            _field("autoMax", "Maximum automatic cues", "/sfx/autoMax", "number",
                   default=sfx_defaults["autoMax"], step=1),
            _field("autoRules", "Cue rules", "/sfx/autoRules", "group",
                   fields=[
                       _field(event, event, f"/sfx/autoRules/{event}", "group",
                              acceptedShapes=["string", "object", "null"],
                              fields=[
                                  _field("sound", "Sound slug", f"/sfx/autoRules/{event}/sound", "text"),
                                  _field("gain", "Gain", f"/sfx/autoRules/{event}/gain", "number"),
                              ])
                       for event in ("transition", "cameraMove", "textBeat", "payoff", "payoffRiser", "punchIn")
                   ]),
        ]),
    ])
    targets["config.captions"] = _target("Project caption defaults", "/captions", [
        _group("layout", "Caption layout", [
            _field("maxWordsPerPage", "Maximum words per page", "/captions/maxWordsPerPage", "number",
                   default=DEFAULTS["captions"]["maxWordsPerPage"], step=1),
            _field("maxCharsPerPage", "Maximum characters per page", "/captions/maxCharsPerPage", "number",
                   default=DEFAULTS["captions"]["maxCharsPerPage"], step=1),
        ]),
    ])
    targets["transcript.word"] = _target("Transcript word", "/words/{index}", [
        _group("word", "Recorded word", [
            _field("w", "Word", "/words/{index}/w", "text", required=True,
                   help="Submit with correct_words; keep all recorded word timings unchanged."),
            _field("s", "Recorded start", "/words/{index}/s", "timing", readOnly=True,
                   timingDomain="source_seconds", unit="s"),
            _field("e", "Recorded end", "/words/{index}/e", "timing", readOnly=True,
                   timingDomain="source_seconds", unit="s"),
            _field("p", "Confidence", "/words/{index}/p", "number", readOnly=True, min=0, max=1),
        ]),
    ])
    return {
        "version": SCHEMA_VERSION,
        "targets": targets,
        "extensionPolicy": {
            "control": "raw_json",
            "description": "Raw JSON is for unknown extension fields only. Known plan/config fields are typed above.",
            "reservedPrefixes": ["_", "$"],
        },
    }


def _exhibit_target(path: str, label: str) -> dict[str, Any]:
    return _target(label, path, [
        _group("placement", "Timing and placement", [
            _field("at", "Start", path + "/at", "timing", default=0.3, min=0, max=600,
                   timingDomain="segment_seconds" if "{index}" not in path else "reel_seconds", unit="s"),
            _field("for", "Duration", path + "/for", "timing", min=0.3, max=600,
                   timingDomain="segment_seconds" if "{index}" not in path else "reel_seconds", unit="s"),
            _field("width", "Width", path + "/width", "number", default=0.6, min=0.12, max=1.0, step=0.01, unit="frame fraction"),
            _field("x", "Center X", path + "/x", "number", default=0.5, min=0, max=1, step=0.01),
            _field("y", "Center Y", path + "/y", "number", default=0.36, min=0, max=1, step=0.01),
            _field("tilt", "Tilt", path + "/tilt", "number", default=0, min=-20, max=20, step=0.5, unit="°"),
            _field("safeArea", "Keep inside safe area", path + "/safeArea", "boolean", default=True,
                   help="Turn off only for manually placed decorative overlays, not hand-anchored exhibits."),
        ]),
        _group("content", "Exhibit content", [
            _field("image", "Exhibit image", path + "/image", "text",
                   help="A user-provided image in the private exhibits/ folder; no remote URL."),
            _field("chart", "Chart", path + "/chart", "group",
                   fields=_chart_fields(path + "/chart")),
            _field("style", "Style", path + "/style", "enum", default="card", choices=["card", "cutout"]),
            _field("crop", "Image crop", path + "/crop", "collection", minItems=4, maxItems=4,
                   itemFields=[_field("fraction", "Fraction", path + "/crop/{item}", "number", min=0, max=1, step=0.01)],
                   help="[left, top, right, bottom]; left < right and top < bottom."),
            _field("background", "Card background", path + "/background", "color", default="#ffffff"),
            _field("padding", "Card padding", path + "/padding", "number", default=0, min=0, max=80, step=1, unit="px"),
        ]),
        _group("motion", "Animation and depth", [
            _field("enter", "Enter", path + "/enter", "enum", default="pop", choices=["pop", "fade", "slide", "none"]),
            _field("exit", "Exit", path + "/exit", "enum", default="fade", choices=["pop", "fade", "slide", "none"]),
            _field("shadow", "Shadow", path + "/shadow", "boolean", default=True),
            _field("depth", "Depth", path + "/depth", "enum", default="front", choices=["front", "behind"]),
            _field("anchor", "Anchor", path + "/anchor", "enum", choices=["hand"],
                   help="Omit to use x/y placement."),
            _field("follow", "Follow pointing hand", path + "/follow", "boolean", default=False,
                   visibleWhen={"path": path + "/anchor", "equals": "hand"}),
            _field("side", "Side of hand", path + "/side", "enum", default="auto",
                   choices=["auto", "left", "right", "above", "below"],
                   visibleWhen={"path": path + "/anchor", "equals": "hand"}),
            _field("gap", "Gap from fingertip", path + "/gap", "number", default=0.03, min=0, max=0.3, step=0.01,
                   visibleWhen={"path": path + "/anchor", "equals": "hand"}),
            _field("point", "Point on image", path + "/point", "collection", minItems=2, maxItems=2,
                   itemFields=[_field("fraction", "Fraction", path + "/point/{item}", "number", min=0, max=1, step=0.01)],
                   visibleWhen={"path": path + "/anchor", "equals": "hand"}),
        ]),
    ])


def _chart_fields(path: str) -> list[dict[str, Any]]:
    return [
        _field("type", "Chart type", path + "/type", "enum", default="line", choices=["line", "bar"]),
        _field("theme", "Theme", path + "/theme", "enum", default="light", choices=["light", "dark"]),
        _field("title", "Title", path + "/title", "text"),
        _field("unit", "Unit", path + "/unit", "text"),
        _field("xLabel", "X-axis label", path + "/xLabel", "text"),
        _field("data", "Strava data source", path + "/data", "text",
               suggestions=["strava:power", "strava:hr", "strava:speed", "strava:cadence"],
               help="Clear this field for typed points/values."),
        _field("ride", "Strava ride ID", path + "/ride", "text"),
        _field("from", "From minute", path + "/from", "number", default=0, min=0, unit="ride minutes"),
        _field("to", "To minute", path + "/to", "number", min=0, unit="ride minutes"),
        _field("smooth", "Smoothing", path + "/smooth", "number", min=0, unit="s"),
        _field("points", "Typed points", path + "/points", "collection",
               itemFields=[
                   _field("x", "X", path + "/points/{item}/0", "number"),
                   _field("y", "Y", path + "/points/{item}/1", "number"),
               ]),
        _field("values", "Typed values", path + "/values", "collection",
               itemFields=[_field("value", "Value", path + "/values/{item}", "number")]),
        _field("labels", "Bar labels", path + "/labels", "collection",
               itemFields=[_field("label", "Label", path + "/labels/{item}", "text")]),
        _field("zero", "Include zero baseline", path + "/zero", "boolean", default=False),
        _field("draw", "Draw chart", path + "/draw", "boolean", default=True),
        _field("color", "Series color", path + "/color", "color"),
        _field("mark", "Marked point", path + "/mark", "group",
               fields=[
                   _field("x", "X position", path + "/mark/x", "number", required=True),
                   _field("label", "Label", path + "/mark/label", "text"),
               ]),
    ]


def _chart_target(path: str) -> dict[str, Any]:
    return _target("Chart", path, [_group("data", "Chart data and style", _chart_fields(path))])


def _sfx_target(path: str, label: str, domain: str) -> dict[str, Any]:
    return _target(label, path, [
        _group("cue", "Sound cue", [
            _field("sound", "Sound slug", path + "/sound", "text", required=True),
            _field("at", "Start", path + "/at", "timing", min=0, max=600, timingDomain=domain, unit="s",
                   visibleWhen={"path": path + "/endAt", "exists": False}),
            _field("endAt", "End at", path + "/endAt", "timing", min=0, max=600, timingDomain=domain, unit="s",
                   visibleWhen={"path": path + "/at", "exists": False}),
            _field("gain", "Gain", path + "/gain", "number", min=0, max=2, step=0.01),
        ]),
    ])


def _walk_fields(groups: Iterable[Mapping[str, Any]]) -> Iterable[dict[str, Any]]:
    for group in groups:
        for field in group.get("fields", []):
            yield field
            yield from _walk_nested(field)


def _walk_nested(field: Mapping[str, Any]) -> Iterable[dict[str, Any]]:
    for key in ("fields", "itemFields"):
        for child in field.get(key, []):
            yield child
            yield from _walk_nested(child)


def _decode_pointer(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise DescriptorError("descriptor path must be an absolute JSON pointer")
    tokens = path[1:].split("/")
    decoded = []
    for token in tokens:
        if _POINTER_ESCAPE.search(token):
            raise DescriptorError(f"invalid JSON pointer escape in {path!r}")
        token = token.replace("~1", "/").replace("~0", "~")
        if not token:
            raise DescriptorError("descriptor paths cannot contain empty tokens")
        if token in (".", ".."):
            raise DescriptorError("descriptor paths cannot traverse with '.' or '..'")
        if token.startswith("_") or token.startswith("$"):
            raise DescriptorError("reserved/internal JSON pointer tokens are not editable")
        if token.lower() in _DENIED_PATH_TOKENS:
            raise DescriptorError(f"descriptor path token {token!r} is protected")
        if token.startswith("{") and token.endswith("}") and token not in _PLACEHOLDERS:
            raise DescriptorError(f"unsupported descriptor path placeholder {token!r}")
        decoded.append(token)
    return decoded


def _safe_in_scope(path: Any, scope: str) -> str:
    tokens = _decode_pointer(path)
    scope_tokens = _decode_pointer(scope) if scope else []
    if tokens[:len(scope_tokens)] != scope_tokens or len(tokens) <= len(scope_tokens):
        raise DescriptorError(f"descriptor path must be a child of target scope {scope!r}")
    return path


def _json_value(value: Any, where: str) -> None:
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise DescriptorError(f"{where} must contain JSON data only") from error


def _validate_condition(condition: Any, scope: str) -> None:
    if not isinstance(condition, dict):
        raise DescriptorError("visibleWhen must be a declarative condition object")
    allowed = {"path", "equals", "notEquals", "oneOf", "exists"}
    if set(condition) - allowed or "path" not in condition:
        raise DescriptorError("visibleWhen supports only path, equals, notEquals, oneOf, and exists")
    _safe_in_scope(condition["path"], scope)
    operators = set(condition) - {"path"}
    if len(operators) != 1:
        raise DescriptorError("visibleWhen requires exactly one comparison")
    operator = next(iter(operators))
    if operator == "oneOf" and not isinstance(condition[operator], list):
        raise DescriptorError("visibleWhen.oneOf must be a list")
    if operator == "exists" and not isinstance(condition[operator], bool):
        raise DescriptorError("visibleWhen.exists must be boolean")
    _json_value(condition, "visibleWhen")


def _validate_field(field: Any, scope: str) -> dict[str, Any]:
    if not isinstance(field, dict):
        raise DescriptorError("descriptor fields must be objects")
    unknown = set(field) - _FIELD_KEYS
    if unknown:
        raise DescriptorError(f"unsupported descriptor field keys: {', '.join(sorted(unknown))}")
    if not isinstance(field.get("id"), str) or not _ID.fullmatch(field["id"]):
        raise DescriptorError("descriptor id must be a short, safe identifier")
    if not isinstance(field.get("label"), str) or not field["label"].strip():
        raise DescriptorError("descriptor label must be non-empty text")
    if field.get("type") not in CONTROL_TYPES:
        raise DescriptorError(f"descriptor type must be one of {', '.join(CONTROL_TYPES)}")
    _safe_in_scope(field.get("path"), scope)
    control = field["type"]
    if control == "timing" and field.get("timingDomain") not in TIMING_DOMAINS:
        raise DescriptorError("timing controls require a supported timingDomain")
    if control == "enum":
        choices = field.get("choices")
        if not isinstance(choices, list) or not choices:
            raise DescriptorError("enum controls require non-empty choices")
        if any(not isinstance(choice, (str, int, float, bool)) or isinstance(choice, float) and not math.isfinite(choice)
               for choice in choices):
            raise DescriptorError("enum choices must be JSON scalar values")
    if control in ("collection", "keyframe") and not (field.get("itemFields") or field.get("itemTarget") or field.get("itemTargets")):
        if not field.get("acceptedShapes"):
            raise DescriptorError(f"{control} controls require itemFields, itemTarget(s), or acceptedShapes")
    if control == "group" and not field.get("fields") and not field.get("acceptedShapes"):
        raise DescriptorError("group controls require child fields or acceptedShapes")
    for key in ("min", "max", "step"):
        if key in field and (not isinstance(field[key], (int, float)) or isinstance(field[key], bool)
                             or not math.isfinite(field[key])):
            raise DescriptorError(f"descriptor {key} must be a finite number")
    if "min" in field and "max" in field and field["min"] > field["max"]:
        raise DescriptorError("descriptor min cannot be greater than max")
    if "display" in field:
        if field["display"] not in DISPLAYS:
            raise DescriptorError(f"descriptor display must be one of {', '.join(DISPLAYS)}")
        if control != "number" or "min" not in field or "max" not in field:
            raise DescriptorError("a slider needs a number control with min and max")
    for key in ("fields", "itemFields"):
        children = field.get(key, [])
        if not isinstance(children, list):
            raise DescriptorError(f"descriptor {key} must be a list")
        for child in children:
            _validate_field(child, scope)
    condition = field.get("visibleWhen")
    if condition is not None:
        _validate_condition(condition, scope)
    if "acceptedShapes" in field and (
        not isinstance(field["acceptedShapes"], list)
        or any(not isinstance(item, str) for item in field["acceptedShapes"])
    ):
        raise DescriptorError("acceptedShapes must be a list of strings")
    if "choices" in field:
        _json_value(field["choices"], "choices")
    _json_value(field, f"descriptor {field['id']}")
    return copy.deepcopy(field)


def validate_descriptors(
    descriptors: Iterable[Mapping[str, Any]] | None,
    *,
    schema: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Validate renderer-provided, declarative field additions.

    Each addition is ``{"target": "segment.video", "group": "Effect",
    "field": {"id", "label", "path", "type", ...}}``. The path must be a
    non-reserved child of that target's plan scope. Arbitrary code/native-view
    keys, traversal, internal fields, and protected media/runtime paths fail
    closed.
    """
    if descriptors is None:
        return []
    base = schema or _base_schema()
    targets = base.get("targets", {})
    known_paths = {
        field["path"]
        for target in targets.values()
        for field in _walk_fields(target.get("groups", []))
    }
    validated: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for index, extra in enumerate(descriptors):
        if index >= 256:
            raise DescriptorError("a renderer revision may add at most 256 inspector fields")
        if not isinstance(extra, Mapping) or set(extra) != _EXTRA_KEYS:
            raise DescriptorError("extra descriptors require exactly target, group, and field")
        target_name, group_name, field = extra["target"], extra["group"], extra["field"]
        if not isinstance(target_name, str) or target_name not in targets:
            raise DescriptorError(f"unknown inspector target {target_name!r}")
        if target_name.startswith(("config.", "transcript.")):
            raise DescriptorError("renderer fields may extend plan effects, not project policy or recorded transcripts")
        if not isinstance(group_name, str) or not group_name.strip() or len(group_name) > 80:
            raise DescriptorError("extra descriptor group must be non-empty text (max 80 characters)")
        scope = targets[target_name].get("scopePath", "")
        cleaned = _validate_field(field, scope)
        if cleaned["path"] in known_paths:
            raise DescriptorError(f"extra descriptor cannot override built-in path {cleaned['path']!r}")
        key = (target_name, cleaned["path"])
        if key in seen:
            raise DescriptorError(f"duplicate extra descriptor path {cleaned['path']!r}")
        seen.add(key)
        validated.append({"target": target_name, "group": group_name.strip(), "field": cleaned})
    return validated


def inspector_schema(extra_descriptors: Iterable[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Return the complete versioned inventory and append validated source fields."""
    schema = _base_schema()
    for extra in validate_descriptors(extra_descriptors, schema=schema):
        target = schema["targets"][extra["target"]]
        group = next((g for g in target["groups"] if g["label"] == extra["group"]), None)
        if group is None:
            group = _group(
                re.sub(r"[^A-Za-z0-9_.-]+", "-", extra["group"]).strip("-")[:64] or "extensions",
                extra["group"],
                [],
            )
            target["groups"].append(group)
        group["fields"].append(extra["field"])
    return schema
