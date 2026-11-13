"""Cut lists for long talking takes: `./reels takes <id>`.

A talking reel is cut from one long recording where every line is said two or three times with
dead air in between. This turns the word-level transcript into phrases split at the pauses, gives
each a tight in/out (word edges, moved onto where the sound really starts and stops when the audio
is clean enough to tell), groups repeated takes of the same line so the last one is easy to pick,
and flags false starts, trailing words and filler. It proposes; the plan still decides.
"""

from __future__ import annotations

import json
from typing import Any

from .config import Project
from .library import load_asset
from .retention import LOW_CONFIDENCE, WORD_EDGE
from .search import tokens
from .util import ReelsError, fmt_ts, log, read_json

PAUSE = 0.5  # s of quiet between words that ends a phrase
LEAD = 0.06  # s kept before the first word
TAIL = 0.15  # s kept after the last word, so the cut doesn't clip its release
CLEAR = 0.02  # s kept away from the neighbouring phrase's words
FRAME = 0.02  # s per loudness frame
MIN_SILENCE = 0.25  # s below the threshold to count as a pause
MIN_CONTRAST = 10.0  # dB between speech and the quiet parts; under this (wind, traffic) levels can't find pauses
THRESHOLD_SHARE = 0.3  # the pause threshold sits this far up from the floor towards the speech level
ONSET_REACH = 0.4  # s after a word edge in which a pause edge may replace it
ONSET_EARLY = 0.25  # s before a word's start: sound further ahead of it is a breath, not the word
RUN = 3  # words in a row said again later to count as a repeat
RETAKE_SHARE = 0.6  # of the shorter phrase: this much repeated makes it another take of the same line
LOOKAHEAD = 8  # phrases ahead to look for a repeat; further apart is a callback, not a retake
FRAGMENT_WORDS = 2

TRAILING = {"the", "a", "an", "and", "so", "but", "or", "to", "because", "um", "uh"}
FILLERS = {"um", "uh", "er", "erm", "ah", "hmm", "mm", "umm", "uhh"}
FILLER_PAIRS = {("you", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of")}
NUMBERS = {w: str(i) for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}  # fmt: skip
NUMBERS.update(thirty="30", forty="40", fifty="50", sixty="60")


def norm(word: str) -> list[str]:
    """Comparable tokens for a spoken word: "Five," and "5" are the same."""
    return [NUMBERS.get(t, t) for t in tokens(word)]


# --- pauses from the audio level -----------------------------------------------------------------


def silences(audio: Any, words: list[dict], rate: int = 16000) -> list[tuple[float, float]] | None:
    """Quiet stretches of at least MIN_SILENCE, from the loudness of FRAME-long frames. None when
    the talking isn't clearly louder than the rest (wind), so word times are all there is."""
    import numpy as np

    hop = int(rate * FRAME)
    n = len(audio) // hop
    if n < 10 or not words:
        return None
    frames = np.asarray(audio[: n * hop], dtype=np.float64).reshape(n, hop)
    db = 20 * np.log10(np.sqrt((frames**2).mean(axis=1)) + 1e-7)
    spoken = np.zeros(n, dtype=bool)
    for w in words:
        spoken[int(w["s"] / FRAME) : int(w["e"] / FRAME) + 1] = True
    if spoken.sum() < 10:
        return None
    speech, floor = float(np.percentile(db[spoken], 90)), float(np.percentile(db, 10))
    if speech - floor < MIN_CONTRAST:
        return None
    quiet = db < floor + THRESHOLD_SHARE * (speech - floor)
    out, start = [], None
    for i, q in enumerate([*quiet, False]):
        if q and start is None:
            start = i
        elif not q and start is not None:
            if (i - start) * FRAME >= MIN_SILENCE:
                out.append((round(start * FRAME, 2), round(i * FRAME, 2)))
            start = None
    return out


def tighten(words: list[dict], sil: list[tuple[float, float]] | None) -> list[dict]:
    """Words with `s`/`e` moved onto the sound: Whisper stretches a word over the pause after it
    (or starts it early), which hides the pause. The transcript's own times stay in `ws`/`we`."""
    out = []
    for w in words:
        s, e = float(w["s"]), float(w["e"])
        t = {**w, "s": s, "e": e, "ws": s, "we": e}
        for a, b in sil or []:
            if a <= s < b and b < e - 0.1:  # starts inside a pause
                t["s"] = b
            if t["s"] + 0.1 < a < e:  # a pause begins inside the word
                t["e"] = a
                break
        out.append(t)
    return out


# --- phrases -------------------------------------------------------------------------------------


def split(words: list[dict], pause: float = PAUSE) -> list[list[dict]]:
    groups: list[list[dict]] = []
    for w in words:
        if groups and w["s"] - groups[-1][-1]["e"] < pause:
            groups[-1].append(w)
        else:
            groups.append([w])
    return groups


def _in_point(first: dict, prev_end: float, sil: list[tuple[float, float]] | None) -> float:
    onset = first["s"]
    for a, b in sil or []:
        if -ONSET_EARLY <= b - first["s"] <= ONSET_REACH and b < first["e"] and b >= prev_end:
            onset = b  # the sound starts where this pause ends
    return round(max(onset - LEAD, prev_end + CLEAR if prev_end else 0.0, 0.0), 2)


def _out_point(last: dict, next_start: float | None, sil: list[tuple[float, float]] | None, duration: float | None) -> float:
    end = last["e"]
    for a, _b in sil or []:
        if last["s"] + 0.08 <= a <= end + ONSET_REACH and (next_start is None or a <= next_start):
            end = a  # the sound stops where this pause starts
            break
    out = end + TAIL
    if next_start is not None:
        out = min(out, next_start - CLEAR)
    if duration:
        out = min(out, duration)
    return round(max(out, last["s"] + 0.05), 2)


def _common_run(a: list[str], b: list[str]) -> tuple[int, int]:
    """(length, start in a) of the longest run of tokens that appears in both."""
    best, at = 0, 0
    prev = [0] * (len(b) + 1)
    for i, x in enumerate(a):
        cur = [0] * (len(b) + 1)
        for j, y in enumerate(b):
            if x == y:
                cur[j + 1] = prev[j] + 1
                if cur[j + 1] > best:
                    best, at = cur[j + 1], i - cur[j + 1] + 1
        prev = cur
    return best, at


def build(words: list[dict], *, pause: float = PAUSE, sil: list[tuple[float, float]] | None = None, duration: float | None = None) -> list[dict]:
    """Phrases with snapped in/out, takes grouped into lines, and notes on what to trim."""
    words = sorted((w for w in words if str(w.get("w", "")).strip()), key=lambda w: w["s"])
    groups = split(tighten(words, sil), pause)
    phrases: list[dict] = []
    for k, g in enumerate(groups):
        prev_end = groups[k - 1][-1]["e"] if k else 0.0
        next_start = groups[k + 1][0]["s"] if k + 1 < len(groups) else None
        toks, owner = [], []  # owner: which word each token came from
        for i, w in enumerate(g):
            for t in norm(w["w"]):
                toks.append(t)
                owner.append(i)
        p = {"n": k + 1, "in": _in_point(g[0], prev_end, sil), "out": _out_point(g[-1], next_start, sil, duration),
             "text": " ".join(str(w["w"]).strip() for w in g), "words": g, "tokens": toks, "owner": owner, "notes": [],
             "line": None, "take": None, "takes": None, "last": None, "pauseAfter": None if next_start is None else round(next_start - g[-1]["e"], 2)}  # fmt: skip
        last = g[-1]
        if p["out"] < last["we"] - WORD_EDGE:
            p["notes"].append(f"transcript ends '{str(last['w']).strip()}' at {last['we']:.2f}, the sound stops at {last['e']:.2f}: "
                              f"set its \"e\" to {p['out']:.2f} or check calls it clipped")  # fmt: skip
        phrases.append(p)

    _group_takes(phrases, sil, duration)
    for p in phrases:
        _trim_notes(p, sil, duration)
    return phrases


def _cut_before(p: dict, word_index: int, sil: list[tuple[float, float]] | None, duration: float | None) -> float | None:
    """Out-point that ends the phrase before its word `word_index`."""
    if word_index <= 0:
        return None
    return _out_point(p["words"][word_index - 1], p["words"][word_index]["s"], sil, duration)


def _group_takes(phrases: list[dict], sil: list[tuple[float, float]] | None, duration: float | None) -> None:
    parent = list(range(len(phrases)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, p in enumerate(phrases):
        again: dict[tuple[str, float | None], list[int]] = {}
        for j in range(i + 1, min(i + 1 + LOOKAHEAD, len(phrases))):
            q = phrases[j]
            run, at = _common_run(p["tokens"], q["tokens"])
            if run < RUN:
                continue
            if run >= RETAKE_SHARE * min(len(p["tokens"]), len(q["tokens"])):
                parent[find(j)] = find(i)
                continue
            said = " ".join(p["tokens"][at : at + run])
            cut = _cut_before(p, p["owner"][at], sil, duration) if at + run >= len(p["tokens"]) - 2 else None
            again.setdefault((said, cut), []).append(q["n"])
        for (said, cut), where in again.items():
            note = f"says '{said}' again in " + ", ".join(f"#{n}" for n in where)
            p["notes"].append(note + (f": to drop it here, out {cut:.2f}" if cut else ""))

    lines: dict[int, list[dict]] = {}
    for i, p in enumerate(phrases):
        lines.setdefault(find(i), []).append(p)
    letter = 0
    for members in lines.values():
        if len(members) < 2:
            continue
        name = _letters(letter)
        letter += 1
        for t, p in enumerate(members, 1):
            p.update(line=name, take=t, takes=len(members), last=t == len(members))


def _letters(i: int) -> str:
    s = ""
    while True:
        s = chr(65 + i % 26) + s
        i = i // 26 - 1
        if i < 0:
            return s


def _trim_notes(p: dict, sil: list[tuple[float, float]] | None, duration: float | None) -> None:
    g, toks = p["words"], p["tokens"]
    if len(toks) <= FRAGMENT_WORDS:
        p["notes"].append("fragment: likely a false start")
    else:
        k = len(g)
        while k > 1 and (t := norm(g[k - 1]["w"])) and all(x in TRAILING for x in t):
            k -= 1
        if k < len(g) and p["pauseAfter"] is not None:
            cut = _cut_before(p, k, sil, duration)
            dropped = " ".join(str(w["w"]).strip() for w in g[k:])
            p["notes"].append(f"trails off on '{dropped}': to end before it, out {cut:.2f}")
    fillers = []
    for i, w in enumerate(g):
        t = norm(w["w"])
        nxt = norm(g[i + 1]["w"]) if i + 1 < len(g) else []
        if len(t) == 1 and t[0] in FILLERS:
            fillers.append(f"'{t[0]}' {w['s']:.2f}–{w['e']:.2f}")
        elif len(t) == 1 and len(nxt) == 1 and (t[0], nxt[0]) in FILLER_PAIRS:
            fillers.append(f"'{t[0]} {nxt[0]}' {w['s']:.2f}–{g[i + 1]['e']:.2f}")
    if fillers:
        p["notes"].append("filler: " + ", ".join(fillers))
    known = [w["p"] for w in g if isinstance(w.get("p"), (int, float))]
    if known and sum(known) / len(known) < LOW_CONFIDENCE:
        p["notes"].append("low-confidence transcript: may be wind, not words")


# --- command -------------------------------------------------------------------------------------


def parse_pick(text: str, count: int) -> list[int]:
    """'2,5-7,3' → [2, 5, 6, 7, 3], in the order given (the reel's order, not the recording's)."""
    picked: list[int] = []
    for part in text.split(","):
        a, _, b = part.strip().partition("-")
        try:
            lo, hi = int(a), int(b or a)
        except ValueError:
            raise ReelsError(f"--pick takes phrase numbers like 2,5-7 (got '{part.strip()}')") from None
        if not 1 <= lo <= hi <= count:
            raise ReelsError(f"--pick {part.strip()}: phrases are numbered 1–{count}")
        picked.extend(range(lo, hi + 1))
    return picked


def public(p: dict) -> dict:
    """A phrase as --json prints it."""
    out = {k: p[k] for k in ("n", "in", "out", "text", "notes", "pauseAfter")}
    out["seconds"] = round(p["out"] - p["in"], 2)
    if p["line"]:
        out.update(line=p["line"], take=p["take"], takes=p["takes"], last=p["last"])
    return out


def segments(asset_id: str, phrases: list[dict]) -> list[dict]:
    return [{"asset": asset_id, "in": p["in"], "out": p["out"], "captions": "transcript"} for p in phrases]


def analyse(project: Project, asset: dict, *, pause: float = PAUSE, use_audio: bool = True) -> tuple[list[dict], str]:
    """(phrases, how the edges were found)."""
    if not asset.get("transcript"):
        raise ReelsError(f"{asset['id']} has no transcript yet — ./reels transcribe {asset['id']}")
    words = read_json(project.abs(asset["transcript"])).get("words") or []
    if not words:
        raise ReelsError(f"the transcript of {asset['id']} has no word timings")
    duration = (asset.get("probe") or {}).get("duration")
    sil, edges = None, "word times only (--no-audio)"
    if use_audio:
        media = project.abs(asset["media"])
        if asset.get("deleted") or not media.exists():
            edges = "word times only (the media file is gone)"
        else:
            from .media import load_audio_16k

            sil = silences(load_audio_16k(media), words)
            edges = "edges on the audio level" if sil is not None else "word times only (too noisy to find pauses by level)"
    return build(words, pause=pause, sil=sil, duration=duration), edges


def cmd_takes(project: Project, args) -> int:
    asset = load_asset(project, args.id)
    phrases, edges = analyse(project, asset, pause=args.pause, use_audio=not args.no_audio)
    if args.pick:
        chosen = [phrases[n - 1] for n in parse_pick(args.pick, len(phrases))]
        print(json.dumps(segments(asset["id"], chosen), indent=2))
        if not args.json:
            log(f"{len(chosen)} segments, {sum(p['out'] - p['in'] for p in chosen):.1f}s before cutaways and beat snapping")
        return 0
    shown = [p for p in phrases if (args.start is None or p["out"] > args.start) and (args.end is None or p["in"] < args.end)]
    if args.json:
        print(json.dumps({"asset": asset["id"], "edges": edges, "pause": args.pause, "phrases": [public(p) for p in shown]}, indent=2))
        return 0
    n_words = sum(len(p["words"]) for p in phrases)
    log(f"{asset['id']} · {fmt_ts((asset.get('probe') or {}).get('duration') or phrases[-1]['out'])} · {n_words} words · "
        f"{len(phrases)} phrases (pauses of {args.pause:g}s or more) · {edges}")  # fmt: skip
    for p in shown:
        take = ""
        if p["line"]:
            take = f"  [line {p['line']}: " + ("★ last take" if p["last"] else f"take {p['take']} of {p['takes']}") + "]"
        log(f"#{p['n']:<3} {p['in']:>7.2f} → {p['out']:<7.2f} {p['out'] - p['in']:>5.1f}s{take}")
        log(f"      {p['text']}")
        for note in p["notes"]:
            log(f"      · {note}")
    log("\nSegments for a plan: ./reels takes " + asset["id"] + " --pick 2,5-7 (in the reel's order)")
    return 0


def add_commands(sub) -> None:
    s = sub.add_parser("takes", help="cut list for a long talking take: phrases split at pauses with tight in/out, retakes grouped, filler flagged")
    s.add_argument("id")
    s.add_argument("--start", type=float, help="only phrases after this second")
    s.add_argument("--end", type=float, help="only phrases before this second")
    s.add_argument("--pause", type=float, default=PAUSE, help=f"seconds of quiet that end a phrase (default {PAUSE}); lower splits finer")
    s.add_argument("--no-audio", action="store_true", help="word times only; don't read the audio level")
    s.add_argument("--pick", help="phrase numbers, e.g. 2,5-7: print them as plan segments")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_takes)
