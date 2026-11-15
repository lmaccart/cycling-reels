"""Immutable, metadata-only search for the native editor.

The CLI search remains token/prefix based.  This module snapshots the same
library metadata once per revision and adds bounded regex plus a small,
validated interface for an injected semantic-search runner.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import SimpleNamespace
from typing import Any, Callable, Iterable, Mapping, Protocol

import regex

from .. import search as cli_search
from ..config import Project
from ..library import all_assets, moment_used
from ..retention import LOW_CONFIDENCE
from ..util import read_json

MODEL_ID = "claude-haiku-5-5"
PROMPT_VERSION = "editor-search-v1"
SEMANTIC_DELAY = 0.600
MAX_PAGE_SIZE = 200
DEFAULT_REGEX_DEADLINE = 0.100
DEFAULT_MATCH_DEADLINE = 0.020
MAX_REGEX_LENGTH = 4096

_FILTER_KEYS = (
    "role", "mood", "speaker", "face", "opener", "line", "thread",
    "min_score", "type", "unused", "include_deleted",
)
_FILTER_ALIASES = {
    "minScore": "min_score",
    "includeDeleted": "include_deleted",
}


class _SemanticFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _json_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normal_filters(filters: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for key, value in (filters or {}).items():
        normalized[_FILTER_ALIASES.get(key, key)] = value
    return {key: normalized[key] for key in _FILTER_KEYS if key in normalized and normalized[key] is not None}


def _safe_filename(value: Any) -> str:
    # Original names are metadata, but should never accidentally expose a
    # stored absolute path when legacy records contain one.
    return PurePosixPath(str(value or "").replace("\\", "/")).name


def _flatten_strings(value: Any, prefix: str = "") -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        if value.strip():
            yield prefix, value
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            yield from _flatten_strings(child, f"{prefix}[{index}]")
    elif isinstance(value, Mapping):
        for key, child in sorted(value.items(), key=lambda item: str(item[0])):
            yield from _flatten_strings(child, f"{prefix}.{key}" if prefix else str(key))


@dataclass(frozen=True)
class Passage:
    id: str
    asset_id: str
    kind: str
    where: str
    field: str
    text: str
    moment_id: str | None
    source_in: float | None
    source_out: float | None
    confidence: float | None
    low_confidence: bool
    created: str | None
    score: int | float | None
    role: str | None
    used: bool
    deleted: bool
    untagged: bool
    owner: str
    asset_json: str
    moments_json: str
    word_ranges: tuple[tuple[int, int, float, float], ...] = ()
    word_confidences: tuple[tuple[int, int, float | None], ...] = ()
    keyterm_low_confidence: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "passageId": self.id,
            "assetId": self.asset_id,
            "kind": self.kind,
            "where": self.where,
            "field": self.field,
            "text": self.text,
            "momentId": self.moment_id,
            "sourceIn": self.source_in,
            "sourceOut": self.source_out,
            "confidence": self.confidence,
            "lowConfidence": self.low_confidence,
            "created": self.created,
            "score": self.score,
            "role": self.role,
            "used": self.used,
            "deleted": self.deleted,
            "untagged": self.untagged,
        }


class SearchIndex:
    """Read-only search snapshot for exactly one library revision.

    ``regex`` returns deterministic passage rows with codepoint highlight ranges.
    ``catalog``, ``search_passages`` and ``get_passages`` are safe to expose as
    read-only semantic tools: their results contain metadata and transcript
    evidence only, never media/transcript paths or image/frame content.
    """

    def __init__(
        self,
        project: Project,
        library_revision: str,
        *,
        regex_deadline: float = DEFAULT_REGEX_DEADLINE,
        match_deadline: float = DEFAULT_MATCH_DEADLINE,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.project = project
        self.library_revision = str(library_revision)
        self.regex_deadline = max(0.001, regex_deadline)
        self.match_deadline = max(0.001, match_deadline)
        self._clock = clock
        passages: list[Passage] = []
        assets = all_assets(project)
        self._assets = {str(asset["id"]): copy.deepcopy(asset) for asset in assets}
        keyterms = {
            token
            for keyterm in project.config.get("transcription", {}).get("keyterms") or []
            for token in cli_search.tokens(keyterm)
        }
        self._asset_rows: tuple[str, ...]
        catalog_rows = []
        for asset in assets:
            tags = asset.get("tags") or {}
            if asset.get("status") == "skip" or tags.get("skip"):
                continue
            moments = tags.get("moments") or []
            asset_id = str(asset["id"])
            deleted = cli_search.is_deleted(asset)
            asset_metadata = {
                "assetId": asset_id,
                "kind": asset.get("kind"),
                "filename": _safe_filename(asset.get("originalName")),
                "created": asset.get("created"),
                "deleted": deleted,
                "status": asset.get("status"),
                "type": tags.get("type"),
                "summary": tags.get("summary"),
                "score": tags.get("score", tags.get("quality")),
                "momentCount": len(moments),
                "_used": bool(asset.get("used")),
                "_momentFilters": [
                    {
                        **{key: value for key, value in moment.items()
                           if key in ("role", "mood", "speaker", "face", "opener", "line", "thread", "score", "type")},
                        "type": moment.get("type") or tags.get("type"),
                        "score": moment.get("score"),
                        "used": moment_used(asset, moment),
                    }
                    for moment in moments
                ],
            }
            catalog_rows.append(json.dumps(asset_metadata, ensure_ascii=False, separators=(",", ":")))
            asset_json = json.dumps(asset, ensure_ascii=False, default=str, separators=(",", ":"))
            moments_json = json.dumps(moments, ensure_ascii=False, default=str, separators=(",", ":"))

            def add(
                where: str,
                field: str,
                text: Any,
                *,
                moment: dict | None = None,
                source_in: float | None = None,
                source_out: float | None = None,
                owner: str = "clip",
                confidence: float | None = None,
                low_confidence: bool = False,
                word_ranges: tuple[tuple[int, int, float, float], ...] = (),
                word_confidences: tuple[tuple[int, int, float | None], ...] = (),
                keyterm_low_confidence: bool = False,
                ordinal: int = 0,
            ) -> None:
                if not isinstance(text, str) or not text.strip():
                    return
                resolved_moment = moment
                untagged = False
                timed_used = None
                if owner == "timed":
                    hit = cli_search._timed_hit(
                        asset, moments, where, text, source_in or 0.0, source_out or 0.0,
                        SimpleNamespace(),
                    )
                    if hit is not None:
                        untagged = bool(hit.get("untagged"))
                        timed_used = bool(hit.get("used"))
                        resolved_moment = next(
                            (m for m in moments if m.get("id") == hit.get("momentId")), None
                        )
                moment_id = (
                    str(resolved_moment["id"])
                    if resolved_moment and resolved_moment.get("id") is not None else None
                )
                score = resolved_moment.get("score") if resolved_moment else tags.get("score", tags.get("quality"))
                role = resolved_moment.get("role") if resolved_moment else None
                used = (
                    timed_used if timed_used is not None
                    else moment_used(asset, resolved_moment) if resolved_moment
                    else cli_search._asset_used(asset)[0]
                )
                identity = "\0".join(
                    (asset_id, where, field, moment_id or "", str(source_in), str(source_out), str(ordinal))
                )
                pid = "p_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                passages.append(Passage(
                    id=pid,
                    asset_id=asset_id,
                    kind=str(asset.get("kind") or ""),
                    where=where,
                    field=field,
                    text=text,
                    moment_id=moment_id,
                    source_in=source_in,
                    source_out=source_out,
                    confidence=confidence,
                    low_confidence=low_confidence,
                    created=asset.get("created"),
                    score=score,
                    role=role,
                    used=used,
                    deleted=deleted,
                    untagged=untagged,
                    owner=owner,
                    asset_json=asset_json,
                    moments_json=moments_json,
                    word_ranges=word_ranges,
                    word_confidences=word_confidences,
                    keyterm_low_confidence=keyterm_low_confidence,
                ))

            add("filename", "originalName", _safe_filename(asset.get("originalName")))
            add("summary", "summary", tags.get("summary"))
            add("notes", "notes", tags.get("notes"))
            for i, hook in enumerate(tags.get("hooks") or []):
                add("hook", f"hooks[{i}]", hook, ordinal=i)

            # Keep the taxonomy/schema text searchable without including IDs,
            # paths, or duplicate special fields above.
            excluded = {"summary", "notes", "hooks", "moments", "landmarks", "sounds"}
            for field, value in sorted(tags.items()):
                if field in excluded:
                    continue
                for ordinal, (leaf, text) in enumerate(_flatten_strings(value, str(field))):
                    add("tag", leaf or str(field), text, ordinal=ordinal)

            for moment_index, moment in enumerate(moments):
                mid = str(moment.get("id") or moment_index)
                start, end = moment.get("in"), moment.get("out")
                for field in ("label", "description", "notes"):
                    add(
                        "label" if field == "label" else "moment",
                        f"moments[{mid}].{field}",
                        moment.get(field),
                        moment=moment,
                        source_in=start,
                        source_out=end,
                        owner="moment",
                    )

            for i, landmark in enumerate(tags.get("landmarks") or []):
                at = landmark.get("at")
                add(
                    "landmark", f"landmarks[{i}].label", landmark.get("label"),
                    source_in=at, source_out=at, owner="timed", ordinal=i,
                )

            transcript_rel = asset.get("transcript")
            if transcript_rel:
                try:
                    transcript_path = project.abs(transcript_rel).resolve()
                    transcript_path.relative_to(project.root.resolve())
                    transcript = read_json(transcript_path)
                except (OSError, ValueError, TypeError):
                    transcript = {}
                lines = cli_search.transcript_lines(transcript)
                for line_index, line in enumerate(lines):
                    text = line["text"]
                    words = line.get("words")
                    word_ranges: tuple[tuple[int, int, float, float], ...] = ()
                    confidence_spans: tuple[tuple[int, int, float | None], ...] = ()
                    confidence = None
                    low_confidence = False
                    keyterm_low = False
                    if words:
                        spans, confidence_spans, cursor, probabilities = [], [], 0, []
                        for word in words:
                            token = str(word.get("w", "")).strip()
                            if not token:
                                continue
                            at = text.find(token, cursor)
                            if at < 0:
                                at = cursor
                            stop = at + len(token)
                            spans.append((at, stop, float(word["s"]), float(word["e"])))
                            probability = word.get("p")
                            confidence_spans.append((
                                at, stop,
                                float(probability) if isinstance(probability, (int, float)) else None,
                            ))
                            cursor = stop
                            if isinstance(word.get("p"), (int, float)):
                                probabilities.append(float(word["p"]))
                        word_ranges = tuple(spans)
                        confidence = sum(probabilities) / len(probabilities) if probabilities else None
                        line_tokens = cli_search.tokens(text)
                        keyterm_low = (
                            len(line_tokens) >= 3
                            and sum(token in keyterms for token in line_tokens) / len(line_tokens)
                            >= cli_search.KEYTERM_SHARE
                        )
                        low_confidence = cli_search._low_confidence(line, words, keyterms)
                    add(
                        "transcript", f"transcript[{line_index}]", text,
                        source_in=line.get("in"), source_out=line.get("out"),
                        owner="timed", confidence=confidence, low_confidence=low_confidence,
                        word_ranges=word_ranges, word_confidences=tuple(confidence_spans),
                        keyterm_low_confidence=keyterm_low, ordinal=line_index,
                    )
        self._passages = tuple(sorted(passages, key=lambda p: (p.asset_id, p.source_in or 0, p.where, p.id)))
        self._passage_by_id = {p.id: p for p in self._passages}
        self._asset_rows = tuple(catalog_rows)
        self._catalog_by_id = {
            json.loads(row)["assetId"]: row for row in self._asset_rows
        }

    @property
    def filter_hash(self) -> str:
        return _json_hash({})

    def _matches_filters(self, passage: Passage, filters: Mapping[str, Any] | None) -> bool:
        values = _normal_filters(filters)
        args = SimpleNamespace(**values)
        if passage.deleted and not values.get("include_deleted", False):
            return False
        if not any(key != "include_deleted" for key in values):
            return True
        # Metadata is immutable inside an index. Do not reparse an entire asset
        # JSON for every transcript passage on every keystroke.
        asset = self._assets[passage.asset_id]
        moments = (asset.get("tags") or {}).get("moments") or []
        if passage.owner == "moment":
            moment = next((m for m in moments if str(m.get("id")) == passage.moment_id), None)
            if moment is None or not cli_search._moment_passes(asset, moment, args):
                return False
            if values.get("unused") and passage.used:
                return False
            return True
        if passage.owner == "timed":
            hit = cli_search._timed_hit(
                asset, moments, passage.where, passage.text,
                passage.source_in or 0.0, passage.source_out or 0.0, args,
            )
            if hit is None or (values.get("unused") and hit.get("used")):
                return False
            return True
        if not cli_search._clip_passes(asset, passage.score, args):
            return False
        return not values.get("unused") or not passage.used

    def regex(self, query: str, filters: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Run case-insensitive Unicode regex with a whole-query and per-match budget."""
        query = str(query)
        normalized = _normal_filters(filters)
        filter_hash = _json_hash(normalized)
        result: dict[str, Any] = {
            "query": query,
            "filterHash": filter_hash,
            "libraryRevision": self.library_revision,
            "matches": [],
            "error": None,
            "timedOut": False,
        }
        if not query:
            result["catalog"] = self.catalog(0, MAX_PAGE_SIZE, normalized)
            return result
        if len(query) > MAX_REGEX_LENGTH:
            result["error"] = {"code": "invalid_regex", "message": f"Use at most {MAX_REGEX_LENGTH} regex characters."}
            return result
        deadline = self._clock() + self.regex_deadline
        try:
            pattern = regex.compile(query, regex.IGNORECASE | regex.UNICODE)
        except (regex.error, RecursionError, OverflowError) as error:
            result["error"] = {"code": "invalid_regex", "message": str(error)}
            return result
        matches = []
        try:
            for passage in self._passages:
                if self._clock() >= deadline:
                    raise TimeoutError("regex whole-query deadline exceeded")
                if not self._matches_filters(passage, normalized):
                    continue
                offsets: list[tuple[int, int]] = []
                cursor = 0
                while cursor <= len(passage.text):
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise TimeoutError("regex whole-query deadline exceeded")
                    allowance = min(remaining, self.match_deadline)
                    found = pattern.search(passage.text, cursor, timeout=allowance)
                    if found is None:
                        break
                    if found.start() != found.end():
                        offsets.append((found.start(), found.end()))
                    cursor = found.end() if found.end() > cursor else cursor + 1
                if not offsets:
                    continue
                row = passage.public()
                row["highlights"] = [
                    {"start": start, "end": end} for start, end in offsets
                ]
                row["highlightSourceRanges"] = [
                    self._source_ranges(passage, start, end) for start, end in offsets
                ]
                if passage.word_confidences:
                    matched_word_confidences = {
                        (word_start, word_end, probability)
                        for start, end in offsets
                        for word_start, word_end, probability in passage.word_confidences
                        if word_start < end and start < word_end
                    }
                    probabilities = [
                        probability for _, _, probability in matched_word_confidences
                        if probability is not None
                    ]
                    row["lowConfidence"] = (
                        sum(probabilities) / len(probabilities) < LOW_CONFIDENCE
                        if probabilities else passage.keyterm_low_confidence
                    )
                matches.append(row)
        except TimeoutError:
            result["timedOut"] = True
            result["error"] = {
                "code": "regex_timeout",
                "message": "Regular expression exceeded the search time limit.",
            }
            # A partial result is useful and deterministic, while the inline
            # error makes clear that the library scan was incomplete.
        result["matches"] = matches
        return result

    @staticmethod
    def _source_ranges(passage: Passage, start: int, end: int) -> list[dict[str, float]]:
        if passage.word_ranges:
            selected = [word for word in passage.word_ranges if word[0] < end and start < word[1]]
            if selected:
                return [{"in": min(w[2] for w in selected), "out": max(w[3] for w in selected)}]
        if passage.source_in is None or passage.source_out is None:
            return []
        return [{"in": passage.source_in, "out": passage.source_out}]

    def catalog(
        self, offset: int = 0, limit: int = 50, filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        offset, limit = max(0, int(offset)), min(MAX_PAGE_SIZE, max(1, int(limit)))
        normalized = _normal_filters(filters)
        rows = []
        for encoded in self._asset_rows:
            row = json.loads(encoded)
            if row["deleted"] and not normalized.get("include_deleted", False):
                continue
            moment_filters = {key: normalized[key] for key in (
                "role", "mood", "speaker", "face", "opener", "line", "thread",
            ) if key in normalized}
            has_moment_filters = bool(
                moment_filters or normalized.get("min_score")
                or normalized.get("type") or normalized.get("unused")
            )
            if has_moment_filters:
                matching_moments = []
                args = SimpleNamespace(**normalized)
                for moment in row.pop("_momentFilters", []):
                    if not cli_search._moment_passes({}, moment, args):
                        continue
                    if normalized.get("unused") and moment.get("used"):
                        continue
                    matching_moments.append(moment)
                if not matching_moments:
                    # Photos do not have moment-level shelves; their asset tags
                    # remain filterable by type/score/unused, as in library.
                    if row.get("kind") != "photo":
                        continue
                    if any(moment_filters):
                        continue
                    if normalized.get("type") and row.get("type") != normalized["type"]:
                        continue
                    if normalized.get("min_score") and (
                        row.get("score") is None or row["score"] < normalized["min_score"]
                    ):
                        continue
                    if normalized.get("unused") and row.get("_used", False):
                        continue
            else:
                row.pop("_momentFilters", None)
            row.pop("_used", None)
            rows.append(row)
        return {
            "items": rows[offset:offset + limit], "offset": offset, "limit": limit,
            "total": len(rows), "libraryRevision": self.library_revision,
        }

    def search_passages(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        offset: int = 0,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search grounded evidence for semantic tools using the CLI token rules."""
        terms, phrases = cli_search.parse_query(query)
        offset, limit = max(0, int(offset)), min(MAX_PAGE_SIZE, max(1, int(limit)))
        found = []
        for passage in self._passages:
            if not self._matches_filters(passage, filters):
                continue
            if cli_search.match(terms, phrases, cli_search.tokens(passage.text)) is not None:
                found.append(passage.public())
        return {
            "items": found[offset:offset + limit], "offset": offset, "limit": limit,
            "total": len(found), "libraryRevision": self.library_revision,
            "filterHash": _json_hash(_normal_filters(filters)),
        }

    def get_passages(
        self,
        ids: Iterable[str],
        filters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """Resolve only existing passages that still pass the requested filters."""
        out = []
        seen = set()
        for passage_id in ids:
            if passage_id in seen:
                continue
            seen.add(passage_id)
            passage = self._passage_by_id.get(str(passage_id))
            if passage is not None and self._matches_filters(passage, filters):
                out.append(passage.public())
        return out


class SemanticRunner(Protocol):
    def __call__(
        self,
        query: str,
        filters: Mapping[str, Any],
        index: SearchIndex,
        cancel_event: threading.Event,
    ) -> Mapping[str, Any]: ...


class SearchTools:
    """Narrow tool facade passed to the injected Haiku/Claude runner."""

    def __init__(self, index: SearchIndex, filters: Mapping[str, Any] | None):
        self._index = index
        self._filters = dict(filters or {})

    def catalog(
        self, offset: int = 0, limit: int = 50, filters: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._index.catalog(offset, limit, filters if filters is not None else self._filters)

    def search_passages(
        self, query: str, filters: Mapping[str, Any] | None = None, offset: int = 0, limit: int = 50,
    ) -> dict[str, Any]:
        return self._index.search_passages(
            query, filters if filters is not None else self._filters, offset, limit
        )

    def get_passages(
        self, ids: Iterable[str], filters: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return self._index.get_passages(ids, filters if filters is not None else self._filters)


class _TimerScheduler:
    @staticmethod
    def call_later(delay: float, callback: Callable[[], None]) -> threading.Timer:
        timer = threading.Timer(delay, callback)
        timer.daemon = True
        timer.start()
        return timer


@dataclass
class _Generation:
    number: int
    query: str
    filters: dict[str, Any]
    filter_hash: str
    cancel_event: threading.Event
    timer: Any = None
    regex_future: Future | None = None
    regex_result: dict[str, Any] | None = None


class SearchCoordinator:
    """Coordinates immediate background regex and cancellable 600 ms Haiku work.

    ``semantic_runner(query, filters, index, cancel_event)`` is injected by the
    parent service. It should launch exactly ``claude-haiku-5-5`` and return
    ``{"matches": [{"passageId", "reason", "evidence"}]}``. The index exposes
    the read-only MCP tool methods. The runner may expose ``cancel(generation)``
    to terminate its subprocess.
    """

    def __init__(
        self,
        index: SearchIndex,
        semantic_runner: SemanticRunner | Callable[..., Mapping[str, Any]] | None,
        emit: Callable[[dict[str, Any]], None],
        *,
        clock: Callable[[], float] = time.monotonic,
        scheduler: Any = None,
        executor: Any = None,
        prompt_version: str = PROMPT_VERSION,
    ):
        self.index = index
        self.semantic_runner = semantic_runner
        self.emit = emit
        self.clock = clock
        self.scheduler = scheduler or _TimerScheduler()
        self.semantic_delay = SEMANTIC_DELAY
        self.model = MODEL_ID
        self.prompt_version = prompt_version
        self._executor = executor or ThreadPoolExecutor(max_workers=4, thread_name_prefix="editor-search")
        self._owns_executor = executor is None
        self._generation = 0
        self._active: _Generation | None = None
        self._cache: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
        self._closed = False
        self._lock = threading.RLock()

    def query(
        self,
        query: str,
        filters: Mapping[str, Any] | None = None,
        generation: int | None = None,
        *,
        submitted_at: float | None = None,
    ) -> int:
        with self._lock:
            if self._closed:
                raise RuntimeError("search coordinator is closed")
            self._cancel_active_locked()
            if generation is None:
                self._generation += 1
                number = self._generation
            else:
                number = int(generation)
                self._generation = max(self._generation, number)
            normalized = _normal_filters(filters)
            active = _Generation(number, str(query), normalized, _json_hash(normalized), threading.Event())
            self._active = active

            # Submit before scheduling semantic work: the UI-facing caller never
            # executes a potentially expensive expression.
            active.regex_future = self._executor.submit(self._run_regex, active)
            if active.query and self.semantic_runner is not None:
                # Index loading is not a second debounce interval. The broker
                # records arrival before preparing a newly revised index.
                delay = max(0.0, self.semantic_delay - max(0.0, self.clock() - submitted_at)) if submitted_at is not None else self.semantic_delay
                active.timer = self.scheduler.call_later(
                    delay, lambda token=active: self._start_semantic(token)
                )
            return number

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            self._cancel_active_locked()
            self._active = None

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            self._cancel_active_locked()
            self._active = None
        if self._owns_executor:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _cancel_active_locked(self) -> None:
        active = self._active
        if active is None:
            return
        active.cancel_event.set()
        if active.timer is not None:
            active.timer.cancel()
        cancel = getattr(self.semantic_runner, "cancel", None)
        if callable(cancel):
            try:
                cancel(active.number)
            except Exception:
                pass

    def _is_current(self, active: _Generation) -> bool:
        with self._lock:
            return not self._closed and self._active is active and not active.cancel_event.is_set()

    def _tags(self, active: _Generation) -> dict[str, Any]:
        return {
            "generation": active.number,
            "filterHash": active.filter_hash,
            "libraryRevision": self.index.library_revision,
        }

    def _run_regex(self, active: _Generation) -> None:
        result = self.index.regex(active.query, active.filters)
        if not self._is_current(active):
            return
        active.regex_result = copy.deepcopy(result)
        self.emit({"type": "regex", **self._tags(active), **copy.deepcopy(result)})

    def _cache_key(self, active: _Generation) -> tuple[str, str, str, str, str]:
        return (
            active.query, active.filter_hash, self.index.library_revision,
            self.model, self.prompt_version,
        )

    def _start_semantic(self, active: _Generation) -> None:
        if not self._is_current(active) or self.semantic_runner is None:
            return
        key = self._cache_key(active)
        cached = self._cache.get(key)
        if cached is not None:
            self._publish_semantic(active, cached, cached=True)
            return
        self._executor.submit(self._run_semantic, active, key)

    def _run_semantic(
        self,
        active: _Generation,
        key: tuple[str, str, str, str, str],
    ) -> None:
        try:
            raw = self.semantic_runner(  # type: ignore[misc]
                active.query, dict(active.filters), self.index, active.cancel_event
            )
            if active.cancel_event.is_set():
                return
            validated = self._validate_semantic(raw, active)
            self._cache[key] = validated
            self._publish_semantic(active, validated, cached=False)
        except Exception as error:  # A semantic failure must not erase regex rows.
            if not self._is_current(active):
                return
            error_info = self._semantic_error(error)
            self.emit({"type": "semanticError", **self._tags(active), "error": error_info})

    def _validate_semantic(self, raw: Any, active: _Generation) -> dict[str, Any]:
        if not isinstance(raw, Mapping):
            raise ValueError("semantic runner returned a non-object result")
        if raw.get("error"):
            error = raw["error"]
            if isinstance(error, Mapping):
                raise _SemanticFailure(
                    str(error.get("code", "semantic_error")),
                    str(error.get("message", "")),
                )
            raise _SemanticFailure("semantic_error", str(error))
        rows = raw.get("matches", [])
        if not isinstance(rows, list):
            raise ValueError("semantic matches must be a list")
        ids = [row.get("passageId") for row in rows if isinstance(row, Mapping) and row.get("passageId")]
        local = {row["passageId"]: row for row in self.index.get_passages(ids, active.filters)}
        validated = []
        seen = set()
        for supplied in rows:
            if not isinstance(supplied, Mapping):
                continue
            passage_id = str(supplied.get("passageId") or "")
            passage = local.get(passage_id)
            if passage is None or passage_id in seen:
                continue
            quote = supplied.get("evidence", supplied.get("quote"))
            if not isinstance(quote, str) or not quote.strip() or quote not in passage["text"]:
                continue
            reason = supplied.get("reason", "")
            if not isinstance(reason, str):
                continue
            # All identity, timing, confidence and path fields come from our
            # immutable local passage, never from the model response.
            validated.append({
                **passage,
                "reason": reason[:500],
                "evidence": quote,
                "highlights": self._quote_offsets(passage["text"], quote),
            })
            seen.add(passage_id)
        return {"matches": validated}

    @staticmethod
    def _quote_offsets(text: str, quote: str) -> list[dict[str, int]]:
        at = text.find(quote)
        return [] if at < 0 else [{"start": at, "end": at + len(quote)}]

    def _publish_semantic(self, active: _Generation, validated: dict[str, Any], *, cached: bool) -> None:
        if not self._is_current(active):
            return
        validated = copy.deepcopy(validated)
        related, enriched = [], []
        # Merge exact asset/range duplicates into the regex row; do not change
        # result ordering or issue selection/playhead state.
        regex_result = active.regex_result or self.index.regex(active.query, active.filters)
        regex_rows = regex_result["matches"]
        related_by_asset: dict[str, dict[str, Any]] = {}
        related_ranges: dict[tuple[str, Any, Any], dict[str, Any]] = {}
        enriched_by_range: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
        regex_by_range: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
        regex_by_asset: dict[str, dict[str, Any]] = {}
        for row in regex_rows:
            regex_by_range.setdefault((row["assetId"], row["sourceIn"], row["sourceOut"]), row)
            regex_by_asset.setdefault(row["assetId"], row)
        for candidate in validated["matches"]:
            key = (candidate["assetId"], candidate["sourceIn"], candidate["sourceOut"])
            same = regex_by_range.get(key) or regex_by_asset.get(candidate["assetId"])
            if same is None:
                prior = related_ranges.get(key)
                if prior is None:
                    related_ranges[key] = candidate
                    asset_row = related_by_asset.get(candidate["assetId"])
                    if asset_row is None:
                        asset_row = {**candidate, "sourceRanges": [candidate]}
                        related_by_asset[candidate["assetId"]] = asset_row
                        related.append(asset_row)
                    elif (
                        asset_row["sourceIn"] != candidate["sourceIn"]
                        or asset_row["sourceOut"] != candidate["sourceOut"]
                    ):
                        asset_row["sourceRanges"].append(candidate)
                else:
                    prior.setdefault("additionalEvidence", []).append({
                        "passageId": candidate["passageId"],
                        "where": candidate["where"],
                        "evidence": candidate["evidence"],
                        "reason": candidate["reason"],
                    })
            else:
                # One enriched existing file row, with each semantic range
                # retained as draggable evidence, rather than a duplicate file
                # under Related when the matched range differs.
                enrichment_key = (candidate["assetId"], None, None)
                prior = enriched_by_range.get(enrichment_key)
                if prior is None:
                    prior = {**same, "related": candidate, "sourceRanges": [candidate]}
                    enriched_by_range[enrichment_key] = prior
                    enriched.append(prior)
                else:
                    if not any(row["sourceIn"] == candidate["sourceIn"] and row["sourceOut"] == candidate["sourceOut"]
                               for row in prior["sourceRanges"]):
                        prior["sourceRanges"].append(candidate)
                    prior["related"].setdefault("additionalEvidence", []).append({
                        "passageId": candidate["passageId"],
                        "where": candidate["where"],
                        "evidence": candidate["evidence"],
                        "reason": candidate["reason"],
                    })
        self.emit({
            "type": "semantic",
            **self._tags(active),
            "model": self.model,
            "cached": cached,
            "regexRows": regex_rows,
            "relatedRows": related,
            "enrichedRows": enriched,
        })

    @staticmethod
    def _semantic_error(error: Exception) -> dict[str, str]:
        text = str(error)
        lowered = text.lower()
        code = getattr(error, "code", None)
        if isinstance(code, str) and code:
            message = getattr(error, "message", text)
            return {"code": code, "message": str(message)}
        if any(term in lowered for term in ("auth", "login", "unauthorized", "credential")):
            code = "authentication"
        elif any(term in lowered for term in ("rate", "429", "limit")):
            code = "rate_limit"
        elif any(term in lowered for term in ("model", "not found", "unavailable")):
            code = "model"
        elif any(term in lowered for term in ("network", "connection", "timeout", "offline")):
            code = "network"
        else:
            code = "semantic"
        return {"code": code, "message": text or type(error).__name__}
