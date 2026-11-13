"""Series title card: a generated wooden board that hangs over the first shot.

The board is drawn by the renderer (it's a generated visual), so it only renders for
series titles the user has said yes to, listed in config.json `titleCards.allowed`.

Plan field:
    "titleCard": {"title": "The Down Week", "episode": 1, "subtitle": "rusty legs",
                  "for": 3.0, "theme": "sting-showdown"}

Timeline (seconds from the start of the reel): the board is fully on screen at frame 0
(the feed's first impression), a nail flies in and hits at HIT, the board swings on the
nail from SWING, and it fades out over FADE before `for` ends. The sounds land on the
same frames: theme sting at 0, nail hit at HIT, creak at SWING.
"""

from __future__ import annotations

from typing import Any

from .config import Project

NAIL_FLIGHT = 0.27  # seconds the nail is in the air
HIT = 0.62  # lands on the showdown sting's second hit (0.6 s)
SWING = 0.72
FADE = 0.45
DEFAULT_FOR = 3.0
SOUNDS = {"theme": "sting-showdown", "nail": "hit-nail", "creak": "creak-wood"}
GAINS = {"theme": 0.6, "nail": 0.9, "creak": 0.5}


def _allowed(project: Project) -> list[str]:
    return [str(t).strip().lower() for t in ((project.config.get("titleCards") or {}).get("allowed") or [])]


def title_card(project: Project, plan: dict, total: int, fps: int, errors: list[str], warnings: list[str]) -> dict | None:
    """Props for the TitleCard component, or None when the plan has no titleCard."""
    from .render import screen_text

    tc = plan.get("titleCard")
    if tc is None:
        return None
    if not isinstance(tc, dict) or not str(tc.get("title", "")).strip():
        errors.append('titleCard needs a title, like {"title": "The Down Week", "episode": 1, "subtitle": "rusty legs"}')
        return None
    title = screen_text(tc["title"], warnings, "titleCard")
    if title.lower() not in _allowed(project):
        errors.append(
            f"titleCard: {title!r} isn't in config.json titleCards.allowed — the board is a generated visual, "
            "so ask the user first and add the series title there once they say yes"
        )
        return None
    ep = tc.get("episode")
    if ep is not None and not (isinstance(ep, int) and ep > 0):
        errors.append("titleCard.episode must be a whole number like 1")
        return None
    subtitle = screen_text(tc.get("subtitle") or "", warnings, "titleCard")
    if len(subtitle.split()) > 4:
        warnings.append(f"titleCard subtitle '{subtitle}' is long — two or three words fit the board")
    try:
        dur = float(tc.get("for", DEFAULT_FOR))
    except (TypeError, ValueError):
        errors.append("titleCard.for must be seconds, like 3")
        return None
    if not 1.5 <= dur <= 6:
        errors.append("titleCard.for must be between 1.5 and 6 seconds")
        return None
    end = min(total, round(dur * fps))
    if end < round((SWING + FADE + 0.3) * fps):
        errors.append("titleCard: the reel is too short for the board to swing and fade")
        return None
    return {
        "title": title,
        "episode": f"Ep {ep}." if ep else "",
        "subtitle": subtitle,
        "startFrame": 0,
        "endFrame": int(end),
        "nailFrame": int(round((HIT - NAIL_FLIGHT) * fps)),
        "hitFrame": int(round(HIT * fps)),
        "swingFrame": int(round(SWING * fps)),
        "fadeFrames": int(round(FADE * fps)),
    }


def card_sfx(plan: dict) -> list[dict[str, Any]]:
    """Manual-style sfx cues for the board (seconds), each sound overridable or switched off with false."""
    tc = plan.get("titleCard")
    if not isinstance(tc, dict):
        return []
    at = {"theme": 0.0, "nail": HIT - 0.02, "creak": SWING + 0.05}
    out = []
    for key, default in SOUNDS.items():
        name = tc.get(key, default)
        if name is False or name is None:
            continue
        out.append({"sound": str(name), "at": round(at[key], 3), "gain": GAINS[key]})
    return out


def push_hook(hook: dict | None, card: dict | None, total: int, warnings: list[str]) -> None:
    """A top hook would sit on the board: it starts once the board is gone."""
    if not hook or not card or hook["startFrame"] >= card["endFrame"]:
        return
    length = hook["endFrame"] - hook["startFrame"]
    hook["startFrame"] = card["endFrame"]
    hook["endFrame"] = min(total, card["endFrame"] + length)
    warnings.append("hook now starts after the title card (they'd overlap)")
