"""Finding footage: text search, shelf filters and the shelf and story overviews. Plain files only."""

from __future__ import annotations

import argparse
import json
import re
from typing import Any, Iterator

from .config import Project
from .library import all_assets, moment_used
from .retention import LOW_CONFIDENCE
from .tags import BEATS, LINES, MOODS, ROLES, SPEAKERS
from .util import ReelsError, fmt_ts, log, read_json, warn

# A transcript "line" is a run of words: breaks on a pause, a sentence end or this length. Whisper's
# own segments can't be used for the span: some bridge minutes of silence between two words.
LINE_GAP = 1.0
LINE_MAX = 10.0
KEYTERM_SHARE = 0.6  # a "line" mostly made of config keyterms is Whisper reciting its hotwords

# Below these, a shelf is flagged low: about a week's use at one reel a day.
LOW_ROLE = 5
LOW_OPENERS = 7
LOW_VOICE_ME = 3
STRONG_SCORE = 4
STRONG_SHOWN = 5

STRONG_MATCHES = ("transcript", "label")


def is_deleted(asset: dict) -> bool:
    return bool(asset.get("deleted"))


def visible_assets(assets: list[dict], include_deleted: bool = False) -> list[dict]:
    return [a for a in assets if include_deleted or not is_deleted(a)]


def add_shelf_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--role", choices=ROLES)
    parser.add_argument("--mood", choices=MOODS)
    parser.add_argument("--speaker", choices=SPEAKERS)
    face = parser.add_mutually_exclusive_group()
    face.add_argument("--face", dest="face", action="store_const", const=True, default=None, help="the rider's face is on screen")
    face.add_argument("--no-face", dest="face", action="store_const", const=False, help="moments tagged as not showing it")
    parser.add_argument("--opener", action="store_true", help="only moments that work as the first second")
    parser.add_argument("--line", choices=LINES)
    parser.add_argument("--thread", help="story thread slug, e.g. road-to-5-ep1")


def add_deleted_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--include-deleted", action="store_true", help="also show footage whose media was deleted, flagged")


def shelf_fields(moment: dict) -> dict:
    """The fields a library row carries so it can be filtered and read as JSON."""
    keys = ("role", "mood", "speaker", "face", "opener", "line", "thread", "beat")
    return {"momentId": moment.get("id"), **{k: moment[k] for k in keys if k in moment}}


def newest_first(rows: list[dict]) -> None:
    """Newest footage first, in clip order within a file. Sorts in place."""
    rows.sort(key=lambda r: r.get("in", r.get("at", 0)))
    rows.sort(key=lambda r: r.get("created") or "", reverse=True)


def has_shelf_filter(args: Any) -> bool:
    return any(getattr(args, k, None) is not None for k in ("role", "mood", "speaker", "face", "line", "thread")) or bool(getattr(args, "opener", False))


def passes_shelves(item: dict, args: Any) -> bool:
    """item is a moment, or anything carrying the same keys. A moment with no value for a shelf
    fails that shelf's filter: untagged isn't the same as --no-face."""
    for key in ("role", "mood", "speaker", "line", "thread"):
        want = getattr(args, key, None)
        if want is not None and item.get(key) != want:
            return False
    if getattr(args, "face", None) is not None and item.get("face") is not args.face:
        return False
    return not getattr(args, "opener", False) or item.get("opener") is True


# --- matching ------------------------------------------------------------------------------------


def tokens(text: str) -> list[str]:
    """Lowercase words without punctuation; apostrophes are dropped so don't and dont are the same."""
    found = re.findall(r"[^\W_]+(?:'[^\W_]+)*", str(text).lower().replace("’", "'"))
    return [t.replace("'", "") for t in found]


def parse_query(words: str, phrase: bool = False) -> tuple[list[str], list[list[str]]]:
    """(loose terms, exact phrases). Double quotes inside the query mark a phrase; --phrase makes it one."""
    words = words.replace("“", '"').replace("”", '"')
    terms: list[str] = []
    phrases: list[list[str]] = []
    if phrase:
        phrases.append(tokens(words))
    else:
        for quoted, loose in re.findall(r'"([^"]*)"|(\S+)', words):
            if quoted:
                phrases.append(tokens(quoted))
            else:
                terms.extend(tokens(loose))
    phrases = [p for p in phrases if p]
    if not terms and not phrases:
        raise ReelsError("give at least one word to search for")
    return terms, phrases


def _term_matches(term: str, token: str) -> bool:
    return token == term or (len(term) >= 3 and token.startswith(term))


def match(terms: list[str], phrases: list[list[str]], toks: list[str]) -> set[int] | None:
    """The positions matched when every term and phrase is present in toks, else None."""
    hit: set[int] = set()
    for term in terms:
        at = {i for i, tok in enumerate(toks) if _term_matches(term, tok)}
        if not at:
            return None
        hit |= at
    for phrase in phrases:
        at = [i for i in range(len(toks) - len(phrase) + 1) if toks[i : i + len(phrase)] == phrase]
        if not at:
            return None
        hit |= {i + k for i in at for k in range(len(phrase))}
    return hit


# --- transcripts ---------------------------------------------------------------------------------


def transcript_lines(data: dict) -> list[dict]:
    """Spoken lines with word-accurate spans. Built from words, not segment text, because corrected
    words are only edited in the words list."""
    words = sorted((w for w in data.get("words") or [] if str(w.get("w", "")).strip()), key=lambda w: w["s"])
    if not words:
        return [{"text": str(s["text"]).strip(), "in": s["start"], "out": s["end"], "words": None}
                for s in data.get("segments") or [] if str(s.get("text", "")).strip()]  # fmt: skip
    groups: list[list[dict]] = [[]]
    for w in words:
        cur = groups[-1]
        if cur and (w["s"] - cur[-1]["e"] > LINE_GAP or cur[-1]["w"].strip()[-1] in ".?!" or w["e"] - cur[0]["s"] > LINE_MAX):
            groups.append(cur := [])
        cur.append(w)
    return [{"text": " ".join(w["w"].strip() for w in g), "in": g[0]["s"], "out": g[-1]["e"], "words": g} for g in groups]


def _line_tokens(line: dict) -> tuple[list[str], list[int]]:
    """Tokens of a line and, per token, the index of the word it came from."""
    if line["words"] is None:
        toks = tokens(line["text"])
        return toks, [0] * len(toks)
    toks, owners = [], []
    for n, w in enumerate(line["words"]):
        for tok in tokens(w["w"]):
            toks.append(tok)
            owners.append(n)
    return toks, owners


def _low_confidence(line: dict, matched: list[dict], keyterms: set[str]) -> bool:
    """Whisper invents text from wind, often a list of its hotwords with tiny probabilities."""
    known = [w["p"] for w in matched if isinstance(w.get("p"), (int, float))]
    if known and sum(known) / len(known) < LOW_CONFIDENCE:
        return True
    toks = tokens(line["text"])
    return len(toks) >= 3 and sum(t in keyterms for t in toks) / len(toks) >= KEYTERM_SHARE


def _overlapping(moments: list[dict], start: float, end: float) -> list[dict]:
    """Moments sharing time with a span, most overlap first. A zero-length span is a point in time."""
    pairs = []
    for m in moments:
        shared = min(end, m["out"]) - max(start, m["in"])
        if shared > 0 or (start == end and m["in"] <= start <= m["out"]):
            pairs.append((shared, m))
    return [m for _, m in sorted(pairs, key=lambda p: -p[0])]


# --- search --------------------------------------------------------------------------------------


def _asset_used(asset: dict) -> tuple[bool, bool]:
    """(used, partly used). A clip with moments still free counts as unused for a clip-level hit."""
    if not asset.get("used"):
        return False, False
    if asset["kind"] != "video":
        return True, False
    free = any(not moment_used(asset, m) for m in (asset.get("tags") or {}).get("moments") or [])
    return not free, free


def _moment_passes(asset: dict, moment: dict, args: Any) -> bool:
    if not passes_shelves(moment, args):
        return False
    if getattr(args, "min_score", None) and moment["score"] < args.min_score:
        return False
    want = getattr(args, "type", None)
    return not want or (moment.get("type") or (asset.get("tags") or {}).get("type")) == want


def _clip_passes(asset: dict, score: int | None, args: Any) -> bool:
    """Hits that belong to no moment have no shelves, so shelf filters rule them out."""
    if has_shelf_filter(args):
        return False
    if getattr(args, "min_score", None) and (score is None or score < args.min_score):
        return False
    want = getattr(args, "type", None)
    return not want or (asset.get("tags") or {}).get("type") == want


def _base(asset: dict, where: str, text: str) -> dict:
    return {"asset": asset["id"], "kind": asset["kind"], "momentId": None, "in": None, "out": None, "role": None, "score": None, "used": False,
            "where": where, "text": text, "created": asset.get("created"), "deleted": is_deleted(asset)}  # fmt: skip


def _timed_hit(asset: dict, moments: list[dict], where: str, text: str, start: float, end: float, args: Any, **extra) -> dict | None:
    """A transcript line or landmark: named after the moment it falls in, or marked untagged."""
    span = {"in": start, "out": end}
    hit = {**_base(asset, where, text), "in": start, "out": end, **extra}
    around = _overlapping(moments, start, end)
    if not around:
        if not _clip_passes(asset, None, args):
            return None
        hit.update(untagged=True, used=moment_used(asset, span))
        return hit
    moment = next((m for m in around if _moment_passes(asset, m, args)), None)
    if moment is None:
        return None
    hit.update(momentId=moment.get("id"), momentLabel=moment["label"], role=moment.get("role"), score=moment["score"],
               used=moment_used(asset, span) or moment_used(asset, moment), untagged=False)  # fmt: skip
    return hit


def _clip_hits(asset: dict, tags: dict, terms: list[str], phrases: list[list[str]], args: Any) -> Iterator[dict]:
    """Matches on the whole clip: summary, hooks, notes."""
    fields = [("summary", tags.get("summary")), *(("hook", h) for h in tags.get("hooks") or []), ("notes", tags.get("notes"))]
    score = tags.get("score", tags.get("quality"))
    if not _clip_passes(asset, score, args):
        return
    used, partly = _asset_used(asset)
    for where, text in fields:
        if text and match(terms, phrases, tokens(text)) is not None:
            yield {**_base(asset, where, text), "score": score, "used": used, "partlyUsed": partly}


def _moment_hits(asset: dict, moments: list[dict], terms: list[str], phrases: list[list[str]], args: Any) -> Iterator[dict]:
    for m in moments:
        if match(terms, phrases, tokens(m["label"])) is not None and _moment_passes(asset, m, args):
            yield {**_base(asset, "label", m["label"]), "momentId": m.get("id"), "in": m["in"], "out": m["out"], "role": m.get("role"),
                   "score": m["score"], "used": moment_used(asset, m)}  # fmt: skip


def _landmark_hits(asset: dict, tags: dict, moments: list[dict], terms: list[str], phrases: list[list[str]], args: Any) -> Iterator[dict]:
    for lm in tags.get("landmarks") or []:
        if match(terms, phrases, tokens(lm["label"])) is not None:
            hit = _timed_hit(asset, moments, "landmark", lm["label"], lm["at"], lm["at"], args, landmark=True)
            if hit:
                yield hit


def _transcript_hits(project: Project, asset: dict, moments: list[dict], terms: list[str], phrases: list[list[str]], args: Any, keyterms: set[str]) -> Iterator[dict]:
    if not asset.get("transcript"):
        return
    try:
        data = read_json(project.abs(asset["transcript"]))
    except (OSError, ValueError) as e:
        warn(f"skipping unreadable transcript {asset['transcript']} ({type(e).__name__})")
        return
    for line in transcript_lines(data):
        toks, owners = _line_tokens(line)
        found = match(terms, phrases, toks)
        if found is None:
            continue
        extra: dict[str, Any] = {"lowConfidence": False, "matchIn": line["in"], "matchOut": line["out"]}
        if line["words"] is not None:
            matched = [line["words"][n] for n in sorted({owners[i] for i in found})]
            extra.update(matchIn=matched[0]["s"], matchOut=max(w["e"] for w in matched), lowConfidence=_low_confidence(line, matched, keyterms))
        hit = _timed_hit(asset, moments, "transcript", line["text"], line["in"], line["out"], args, **extra)
        if hit:
            yield hit


def _rank(hit: dict) -> tuple:
    tier = 2 if hit.get("lowConfidence") else 0 if hit["where"] in STRONG_MATCHES else 1
    return (tier, hit["used"], -(hit["score"] or 0))


def search(project: Project, query: str, args: Any) -> list[dict]:
    """Every hit, best first: spoken and label matches, then the rest, with invented-looking speech
    last. Within each, unused before used, then score, then newest."""
    terms, phrases = parse_query(query, getattr(args, "phrase", False))
    keyterms = {t for k in project.config["transcription"].get("keyterms") or [] for t in tokens(k)}
    hits: list[dict] = []
    for a in visible_assets(all_assets(project), getattr(args, "include_deleted", False)):
        tags = a.get("tags") or {}
        if a.get("status") == "skip" or tags.get("skip"):
            continue
        moments = tags.get("moments") or []
        hits.extend(_moment_hits(a, moments, terms, phrases, args))
        hits.extend(_clip_hits(a, tags, terms, phrases, args))
        hits.extend(_landmark_hits(a, tags, moments, terms, phrases, args))
        hits.extend(_transcript_hits(project, a, moments, terms, phrases, args, keyterms))
    if getattr(args, "unused", False):
        hits = [h for h in hits if not h["used"]]
    hits.sort(key=lambda h: (h["asset"], h["in"] or 0))
    hits.sort(key=lambda h: h.get("created") or "", reverse=True)
    hits.sort(key=_rank)
    return hits


def _ts(seconds: float) -> str:
    m, s = divmod(max(0.0, float(seconds)), 60)
    return f"{int(m)}:{s:05.2f}"


def _span(hit: dict) -> str:
    if hit.get("landmark"):
        return f"@{_ts(hit['in'])}"
    if hit["in"] is None:
        return "photo" if hit["kind"] == "photo" else "whole clip"
    return f"{_ts(hit['in'])}-{_ts(hit['out'])}"


def _hit_lines(hit: dict) -> list[str]:
    moment = hit["momentId"] or ("untagged" if hit.get("untagged") else "-")
    status = "part-used" if hit.get("partlyUsed") else "used" if hit["used"] else "unused"
    score = f"{hit['score']}★" if hit["score"] is not None else "-"
    flags = (" [landmark]" if hit.get("landmark") else "") + (" [low-conf]" if hit.get("lowConfidence") else "") + (" [deleted]" if hit["deleted"] else "")
    head = f"  {hit['asset']:<26} {moment:<9} {_span(hit):<20} {hit['role'] or '-':<8} {score:<3} {status:<9} {hit['where']}{flags}"
    if hit["where"] == "transcript":
        words = f" (words {_ts(hit['matchIn'])}-{_ts(hit['matchOut'])})"
        inside = f"  in {hit['momentId']}: {hit['momentLabel']}" if hit.get("momentId") else ""
        return [head, f'      "{hit["text"]}"{words}{inside}']
    if hit.get("momentLabel"):
        return [head, f'      {hit["text"]}  in {hit["momentId"]}: {hit["momentLabel"]}']
    return [head, f"      {hit['text']}"]


def cmd_search(project: Project, args) -> int:
    query = " ".join(args.query)
    hits = search(project, query, args)
    if args.json:
        print(json.dumps(hits[: args.limit], indent=2))
        return 0
    if not hits:
        log(f"no matches for {query}")
        return 0
    for hit in hits[: args.limit]:
        for line in _hit_lines(hit):
            log(line)
    if len(hits) > args.limit:
        log(f"  … {len(hits) - args.limit} more (use --limit)")
    return 0


# --- shelves and stories -------------------------------------------------------------------------


def _moments(assets: list[dict]) -> Iterator[tuple[dict, dict]]:
    for a in assets:
        if a.get("status") == "tagged" and a["kind"] == "video":
            for m in (a.get("tags") or {}).get("moments") or []:
                yield a, m


def shelf_counts(assets: list[dict]) -> dict:
    """Counts of what's available. Deleted footage is only ever counted separately."""
    live = [(a, m) for a, m in _moments(assets) if not is_deleted(a)]
    free = [(a, m) for a, m in live if not moment_used(a, m)]
    roles = {}
    for role in [*ROLES, None]:
        total = sum(1 for _, m in live if m.get("role") == role)
        if role is not None or total:
            roles[role] = {"total": total, "unused": sum(1 for _, m in free if m.get("role") == role),
                           "good": sum(1 for _, m in free if m.get("role") == role and m["score"] >= 3)}  # fmt: skip
    strong = sorted(((a, m) for a, m in free if m["score"] >= STRONG_SCORE), key=lambda p: p[0].get("created") or "", reverse=True)
    strong.sort(key=lambda p: -p[1]["score"])
    return {
        "roles": roles,
        "openers": sum(1 for _, m in free if m.get("opener") is True),
        "voiceMe": sum(1 for _, m in free if m.get("role") == "voice" and m.get("speaker") == "me"),
        "strong": strong,
        "deleted": sum(1 for a, _ in _moments(assets) if is_deleted(a)),
    }


def cmd_shelves(project: Project, args) -> int:
    counts = shelf_counts(visible_assets(all_assets(project), args.include_deleted))
    log(f"  {'role':<10}{'total':>6}{'unused':>8}{'3+':>5}")
    for role, c in counts["roles"].items():
        low = "  LOW" if role is not None and c["good"] < LOW_ROLE else ""
        log(f"  {role or '(no role)':<10}{c['total']:>6}{c['unused']:>8}{c['good']:>5}{low}")
    log(f"\n  unused openers {counts['openers']}" + ("  LOW" if counts["openers"] < LOW_OPENERS else "")
        + f"  ·  unused voice by me {counts['voiceMe']}" + ("  LOW" if counts["voiceMe"] < LOW_VOICE_ME else ""))  # fmt: skip
    if args.include_deleted:
        log(f"  deleted moments, not counted above: {counts['deleted']}")
    strong = counts["strong"]
    log(f"\n  strong unused ({STRONG_SCORE}+): {len(strong)}" + (", top:" if strong else ""))
    for a, m in strong[:STRONG_SHOWN]:
        log(f"  {m['score']}★ {m.get('role') or '-':<8} {a['id']:<26} {m.get('id', ''):<9} {fmt_ts(m['in'])}-{fmt_ts(m['out'])}  {m['label']}")
    return 0


def threads(assets: list[dict]) -> dict[str, list[tuple[dict, dict]]]:
    out: dict[str, list[tuple[dict, dict]]] = {}
    for a, m in _moments(assets):
        if m.get("thread"):
            out.setdefault(m["thread"], []).append((a, m))
    return dict(sorted(out.items()))


def _beats_line(name: str, items: list[tuple[dict, dict]], width: int) -> str:
    # Deleted moments can be listed, but they can't fill a beat.
    present = {m.get("beat") for a, m in items if not is_deleted(a)}
    return f"{name:<{width}}   " + "  ".join(f"{b} {'✓' if b in present else '✗'}" for b in BEATS)


def _story_detail(name: str, items: list[tuple[dict, dict]]) -> None:
    log(_beats_line(name, items, len(name)))
    ordered = sorted(items, key=lambda p: (p[0].get("created") or "", p[1]["in"]))
    for beat in [*BEATS, None]:
        rows = [p for p in ordered if p[1].get("beat") == beat]
        if not rows:
            continue
        log(f"\n  {beat or '(no beat)'}")
        for a, m in rows:
            flags = (" [used]" if moment_used(a, m) else "") + (" [deleted]" if is_deleted(a) else "")
            log(f"    {m['score']}★ {m.get('role') or '-':<8} {a['id']:<26} {m.get('id', ''):<9} {fmt_ts(m['in'])}-{fmt_ts(m['out'])}  {m['label']}{flags}")
    present = {m.get("beat") for a, m in items if not is_deleted(a)}
    missing = [b for b in BEATS if b not in present]
    log(f"\n  missing: {', '.join(missing) if missing else 'nothing'}")


def cmd_stories(project: Project, args) -> int:
    found = threads(visible_assets(all_assets(project), args.include_deleted))
    if args.thread:
        if args.thread not in found:
            raise ReelsError(f"no thread '{args.thread}' (known: {', '.join(found) or 'none yet'})")
        _story_detail(args.thread, found[args.thread])
        return 0
    if not found:
        log("no threads tagged yet")
        return 0
    width = max(len(n) for n in found)
    for name, items in found.items():
        log(_beats_line(name, items, width))
    return 0


def add_commands(sub) -> None:
    """Registers search, shelves and stories on the CLI."""
    s = sub.add_parser("search", help="find footage by words in moment labels, summaries, hooks, notes, transcripts and landmarks")
    s.add_argument("query", nargs="+", help='all words must match; "double quotes" inside the query mark an exact phrase')
    s.add_argument("--phrase", action="store_true", help="treat the whole query as one exact phrase")
    s.add_argument("--unused", action="store_true")
    s.add_argument("--min-score", type=int)
    s.add_argument("--type")
    add_shelf_args(s)
    add_deleted_arg(s)
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("shelves", help="stock per role: total / unused / unused 3+, openers, voice, low shelves, strong unused moments")
    add_deleted_arg(s)
    s.set_defaults(fn=cmd_shelves)

    s = sub.add_parser("stories", help="story threads and which beats they have (setup, effort, turn, result)")
    s.add_argument("thread", nargs="?", help="one thread's moments, grouped by beat")
    add_deleted_arg(s)
    s.set_defaults(fn=cmd_stories)
