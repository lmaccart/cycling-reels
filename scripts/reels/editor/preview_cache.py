"""Bounded eviction of the editor's regenerable preview data.

Everything under ``<workspace>/preview/revisions`` and ``<workspace>/preview/cache`` is derived from the
plan, the originals and the renderer, so it can be rebuilt; it only has to stay while something still
points at it. This module trims those two folders to a byte budget without ever touching what is live:

* the folders named by any preview manifest, the service's current preview and its HTTP registry;
* the newest few revisions of each document, anything younger than a grace period, any in-flight job;
* the staged media a kept manifest was built from (current renderer).

Scratch left behind by cancelled or killed workers (``.stage-*``) is removed once it is past the grace
period, whatever the budget: it can never be reused.

It never reads or deletes ``preview/sources`` and ``preview/spheres`` (expensive GPU proxies), proxy
reservations or ``.proxy.lock`` files, and never leaves ``<workspace>/preview``.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

DEFAULT_BUDGET_GB = 8.0
GRACE_SECONDS = 600.0  # a younger entry may belong to a prepare that is still running
KEEP_PER_DOCUMENT = 3
DOCUMENT_MARKER = "document.json"  # PreviewManager.prepare records which document a revision folder belongs to
REVISION_NAME = re.compile(r"[0-9a-f]{20}-[0-9a-f]{8}")  # renderer-candidate snapshots are not preview revisions
EVICT_PREFIX = ".evict-"


def budget_bytes(config: dict | None) -> int:
    """`editor.previewCacheGB` from config.json (default 8); anything unusable falls back to the default."""
    value = ((config or {}).get("editor") or {}).get("previewCacheGB")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        value = DEFAULT_BUDGET_GB
    return int(value * 1_000_000_000)


def renderer_namespace(revision: str) -> str:
    """The folder name PreviewManager gives one renderer revision under cache/*/."""
    return "renderer-" + hashlib.sha256(str(revision).encode("utf-8")).hexdigest()


def manifest_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from manifest_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from manifest_strings(item)


@dataclass
class Entry:
    path: Path
    key: tuple[str, ...]  # location relative to preview/, down to the evictable folder
    kind: str  # revision | staged | product | bundle | scratch
    namespace: str | None
    stamp: float  # last use: folder mtime (cache hits touch it)
    size: int
    document: str | None = None


@dataclass
class Report:
    skipped: str | None = None
    before: int = 0
    after: int = 0
    budget: int = 0
    evicted: list[Path] = field(default_factory=list)
    dropped_intermediates: list[Path] = field(default_factory=list)

    @property
    def freed(self) -> int:
        return self.before - self.after


def tree_size(path: Path) -> int:
    """Logical bytes of a tree, never following symlinks. Cloned files count in full, so this is an
    upper bound on the disk they use."""
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        else:
                            total += entry.stat(follow_symlinks=False).st_size
                    except FileNotFoundError:
                        continue
        except (FileNotFoundError, NotADirectoryError):
            continue
    return total


def remove_tree(path: Path) -> None:
    """Delete a tree that may hold read-only directories (the staged-media cache is chmod 555)."""
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            os.chmod(current, 0o700, follow_symlinks=False)
            with os.scandir(current) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(Path(entry.path))
        except OSError:
            continue
    shutil.rmtree(path, ignore_errors=True)


class PreviewCache:
    """One workspace's preview cache. Passes never cross ``workspace_root``."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        budget: int,
        protected: Callable[[], Iterable[Any]] | None = None,
        in_flight: Callable[[], Iterable[Path]] | None = None,
        current_renderers: Callable[[], Iterable[str]] | None = None,
        clock: Callable[[], float] = time.time,
        grace: float = GRACE_SECONDS,
        keep_per_document: int = KEEP_PER_DOCUMENT,
    ):
        self.workspace_root = Path(workspace_root)
        self.preview_root = self.workspace_root / "preview"
        self.revisions = self.preview_root / "revisions"
        self.cache = self.preview_root / "cache"
        self.budget = int(budget)
        self._protected = protected or (lambda: ())
        self._in_flight = in_flight or (lambda: ())
        self._current_renderers = current_renderers or (lambda: ())
        self._clock = clock
        self._grace = grace
        self._keep = keep_per_document
        self._bases = sorted({os.path.abspath(self.preview_root), os.path.realpath(self.preview_root)})

    # -- one pass ---------------------------------------------------------

    def run(self) -> Report:
        report = Report(budget=self.budget)
        if not self._contained():
            report.skipped = "preview folder is not a private directory of this workspace"
            return report
        lock = self._take_lock()
        if lock is None:
            report.skipped = "another pass is running"
            return report
        try:
            self._pass(report)
        finally:
            os.close(lock)  # closing releases the flock; the lock file itself is never unlinked
        return report

    def _contained(self) -> bool:
        root = self.workspace_root.resolve()
        for path in (self.preview_root, self.revisions, self.cache):
            if path.is_symlink():
                return False
            if path.exists() and not path.resolve().is_relative_to(root):
                return False
        return self.preview_root.is_dir()

    def _take_lock(self) -> int | None:
        fd = os.open(self.preview_root / ".evict.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        return fd

    def _pass(self, report: Report) -> None:
        now = self._clock()
        self._finish_interrupted_evictions()
        refs = self._references()
        revisions, others, fixed = self._scan()
        self._drop_intermediates(revisions, refs, now, report)
        for entry in (*revisions, *others):
            entry.size = tree_size(entry.path)
        report.before = fixed + sum(entry.size for entry in (*revisions, *others))
        report.after = report.before
        kept = self._keep_set(revisions, others, refs, now)
        # A worker that was cancelled or killed mid-staging leaves its .stage-*/.*.tmp scratch behind: never
        # reusable, and gigabytes in practice. Gone once past the grace period and not in a running job's folders.
        for entry in sorted((e for e in others if e.kind == "scratch" and e.key not in kept), key=_oldest):
            if self._evict(entry.path):
                report.evicted.append(entry.path)
                report.after -= entry.size
        if report.after <= self.budget:
            return
        others = [e for e in others if e.kind != "scratch"]
        current = self._current_namespaces()
        old_renderer = [e for e in others if current and e.namespace not in current and e.key not in kept]
        old_ids = {id(e) for e in old_renderer}
        stale_revisions = [e for e in revisions if e.key not in kept]
        recent = [e for e in others if e.key not in kept and id(e) not in old_ids]
        order = (*sorted(old_renderer, key=_oldest), *sorted(stale_revisions, key=_oldest), *sorted(recent, key=_oldest))
        for entry in order:
            if report.after <= self.budget:
                break
            if self._evict(entry.path):
                report.evicted.append(entry.path)
                report.after -= entry.size

    # -- what is referenced ------------------------------------------------

    def _relative(self, text: Any) -> tuple[str, ...] | None:
        if not isinstance(text, str) or not text.startswith("/"):
            return None
        for base in self._bases:
            if text == base or text.startswith(base + "/"):
                parts = tuple(part for part in text[len(base):].split("/") if part)
                if parts and parts[0] in ("revisions", "cache") and ".." not in parts:
                    return parts
        return None

    @staticmethod
    def _entry_key(parts: tuple[str, ...]) -> tuple[str, ...]:
        """Truncate a path under preview/ to the folder that is evicted as a unit."""
        if parts[0] == "revisions":
            return parts[:2]
        if len(parts) > 3 and parts[3] == "products":
            return parts[:5]
        return parts[:4]

    def _references(self) -> "_References":
        refs = _References()
        manifests: list[Any] = []
        last_ready = self.preview_root / "last-ready.json"
        records = sorted((self.preview_root / "documents").glob("*.json")) if (self.preview_root / "documents").is_dir() else []
        for path in (last_ready, *records):
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                manifests.append(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                continue
        manifests.extend(self._protected())
        for manifest in manifests:
            self._add_reference(refs, manifest)
        for path in self._in_flight():
            parts = self._relative(str(path))
            if parts:
                refs.keys.add(self._entry_key(parts))
                refs.busy.add(self._entry_key(parts))
        return refs

    def _add_reference(self, refs: "_References", manifest: Any) -> None:
        document = None
        paths: list[tuple[str, ...]] = []
        if isinstance(manifest, dict):
            document = manifest.get("documentId") if isinstance(manifest.get("documentId"), str) else None
            cache_key = manifest.get("cacheKey")
            renderer = (manifest.get("revisions") or {}).get("renderer") if isinstance(manifest.get("revisions"), dict) else None
            if isinstance(cache_key, str) and cache_key and renderer:
                refs.staged.add(("cache", "staged-media", renderer_namespace(str(renderer)), cache_key))
            items: Iterable[str] = manifest_strings(manifest)
        elif isinstance(manifest, str):
            items = (manifest,)
        else:
            items = manifest_strings(manifest)
        for text in items:
            parts = self._relative(text)
            if parts:
                paths.append(parts)
                refs.files.add(parts)
                refs.keys.add(self._entry_key(parts))
        if document:
            for parts in paths:
                if parts[0] == "revisions" and len(parts) > 1:
                    refs.documents.setdefault(parts[1], document)

    # -- what exists --------------------------------------------------------

    def _scan(self) -> tuple[list[Entry], list[Entry], int]:
        revisions: list[Entry] = []
        others: list[Entry] = []
        fixed = 0
        for child in _children(self.revisions):
            if child.name.startswith(".") or not REVISION_NAME.fullmatch(child.name) or not _real_dir(child):
                fixed += tree_size(child)
                continue
            revisions.append(Entry(child, ("revisions", child.name), "revision", None, _stamp(child), 0, self._document_of(child)))
        for kind_dir in _children(self.cache):
            if not _real_dir(kind_dir):
                continue
            kind = {"staged-media": "staged", "bundles": "bundle"}.get(kind_dir.name)
            for namespace in _children(kind_dir):
                if not _real_dir(namespace):
                    continue
                for child in _children(namespace):
                    base = ("cache", kind_dir.name, namespace.name)
                    if kind == "staged" and _is_scratch(child.name) and _real_dir(child):
                        others.append(Entry(child, (*base, child.name), "scratch", namespace.name, _stamp(child), 0))
                    elif kind is None or child.name.startswith(".") or not _real_dir(child):
                        fixed += tree_size(child)
                    elif kind == "staged" and child.name == "products":
                        for product in _children(child):
                            if _is_scratch(product.name) and _real_dir(product):
                                others.append(Entry(product, (*base, "products", product.name), "scratch", namespace.name, _stamp(product), 0))
                            elif product.name.startswith(".") or not _real_dir(product):
                                fixed += tree_size(product)
                            else:
                                others.append(Entry(product, (*base, "products", product.name), "product", namespace.name, _stamp(product), 0))
                    else:
                        others.append(Entry(child, (*base, child.name), kind, namespace.name, _stamp(child), 0))
        return revisions, others, fixed

    def _document_of(self, revision: Path) -> str | None:
        try:
            marker = revision / DOCUMENT_MARKER
            if marker.is_symlink():
                return None
            value = json.loads(marker.read_text(encoding="utf-8")).get("documentId")
            return value if isinstance(value, str) and value else None
        except (OSError, ValueError, AttributeError):
            return None

    def _current_namespaces(self) -> set[str]:
        return {renderer_namespace(revision) for revision in self._current_renderers() if revision}

    # -- the keep set -------------------------------------------------------

    def _keep_set(self, revisions: list[Entry], others: list[Entry], refs: "_References", now: float) -> set[tuple[str, ...]]:
        kept: set[tuple[str, ...]] = set()
        current = self._current_namespaces()
        groups: dict[str | None, list[Entry]] = {}
        for entry in revisions:
            entry.document = entry.document or refs.documents.get(entry.path.name)
            groups.setdefault(entry.document, []).append(entry)
        for group in groups.values():
            kept.update(entry.key for entry in sorted(group, key=lambda e: e.stamp, reverse=True)[:self._keep])
        for entry in (*revisions, *others):
            if now - entry.stamp < self._grace or self._referenced(entry.key, refs):
                kept.add(entry.key)
        for entry in others:
            in_current = not current or entry.namespace in current
            if entry.kind == "staged" and in_current and entry.key in refs.staged:
                kept.add(entry.key)
            if entry.kind == "bundle" and in_current:
                kept.add(entry.key)  # tiny, and every manifest of the live renderer loads it
        return kept

    @staticmethod
    def _referenced(key: tuple[str, ...], refs: "_References") -> bool:
        return any(key[:length] in refs.keys for length in range(1, len(key) + 1))

    # -- deleting ----------------------------------------------------------------

    def _evict(self, path: Path) -> bool:
        """Rename out of the way (atomic: a reader sees the whole folder or a miss), then delete."""
        if path.is_symlink() or not path.resolve().is_relative_to(self.preview_root.resolve()):
            return False
        parked = path.with_name(f"{EVICT_PREFIX}{uuid.uuid4().hex}")
        try:
            os.rename(path, parked)
        except OSError:
            return False  # already gone, or not ours to move
        remove_tree(parked)
        return True

    def _finish_interrupted_evictions(self) -> None:
        """A pass killed between rename and delete leaves .evict-* folders; they are already unreachable."""
        for parent in self._eviction_parents():
            for child in _children(parent):
                if child.name.startswith(EVICT_PREFIX) and _real_dir(child):
                    remove_tree(child)

    def _eviction_parents(self) -> list[Path]:
        parents = [self.revisions]
        for kind_dir in _children(self.cache):
            if not _real_dir(kind_dir):
                continue
            for namespace in _children(kind_dir):
                if _real_dir(namespace):
                    parents.append(namespace)
                    parents.append(namespace / "products")
        return [parent for parent in parents if _real_dir(parent)]

    def _drop_intermediates(self, revisions: list[Entry], refs: "_References", now: float, report: Report) -> None:
        """exact.mp4 supersedes Remotion's render.mp4; older revisions still carry both."""
        for entry in revisions:
            intermediate = entry.path / "render.mp4"
            final = entry.path / "exact.mp4"
            if now - entry.stamp < self._grace or entry.key in refs.busy or ("revisions", entry.path.name, "render.mp4") in refs.files:
                continue
            try:
                if intermediate.is_symlink() or not intermediate.is_file() or not final.is_file() or final.stat().st_size == 0:
                    continue
                intermediate.unlink()
            except OSError:
                continue
            report.dropped_intermediates.append(intermediate)


@dataclass
class _References:
    keys: set[tuple[str, ...]] = field(default_factory=set)  # folders pointed at by a manifest, the registry or a job
    files: set[tuple[str, ...]] = field(default_factory=set)  # exact files they name
    busy: set[tuple[str, ...]] = field(default_factory=set)  # folders an in-flight job is writing
    staged: set[tuple[str, ...]] = field(default_factory=set)  # staged-media entries a manifest was built from
    documents: dict[str, str] = field(default_factory=dict)  # revision folder -> document, for unmarked folders


def _oldest(entry: Entry) -> float:
    return entry.stamp


def _stamp(path: Path) -> float:
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return 0.0


def _real_dir(path: Path) -> bool:
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _children(path: Path) -> list[Path]:
    try:
        return sorted(Path(entry.path) for entry in os.scandir(path))
    except (FileNotFoundError, NotADirectoryError):
        return []


def _is_scratch(name: str) -> bool:
    """Worker scratch: a staged-media build (.stage-<uuid>) or a half-published product (.<key>.<uuid>.tmp)."""
    return name.startswith(".stage-") or (name.startswith(".") and name.endswith(".tmp"))
