"""Isolated renderer work. This module is launched in its own sandboxed process."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from ..config import Project
from ..util import ReelsError, quick_hash
from .proxy import PROXY_VERSION, PROXY_FPS as SPHERE_PROXY_FPS, SPHERE_PROXY_WIDTH, SPHERE_PROXY_MAX, encode_proxy
from .proxy_cache import proxy_job

_STAGE_SPEC_FIELDS = {
    "in", "out", "speed", "frame", "zoom", "focusX", "focusY", "fit", "motion",
    "sphere", "punch", "blur", "clean", "nat", "bleep", "tone", "volume", "_bleeps", "grade",
}
_OMIT_DATA_DIRS = {"out", ".cache", "__pycache__"}


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("xb") as stream:
        os.chmod(temporary, 0o600)
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _safe_tree(root: Path) -> None:
    if root.is_symlink():
        raise ReelsError(f"renderer input contains a symlink: {root}")
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        for name in dirs + files:
            path = base / name
            if path.is_symlink():
                raise ReelsError(f"renderer input contains a symlink: {path.relative_to(root)}")


def _copy_snapshot(source: Path, destination: Path) -> None:
    """Copy stable private project inputs, excluding disposable output and worker caches."""
    _safe_tree(source)
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        src_dir = Path(current)
        rel_dir = src_dir.relative_to(source)
        dirs[:] = [name for name in sorted(dirs) if name not in _OMIT_DATA_DIRS]
        target_dir = destination / rel_dir
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in sorted(files):
            src = src_dir / name
            if name in (".DS_Store",):
                continue
            if src.is_symlink() or not src.is_file():
                raise ReelsError(f"renderer input is not a regular file: {src}")
            before = src.stat()
            target = target_dir / name
            with src.open("rb") as inp, target.open("xb") as out:
                opened = os.fstat(inp.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                    before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns
                ):
                    raise ReelsError(f"renderer input changed while opening: {src.name}")
                shutil.copyfileobj(inp, out)
                after_open = os.fstat(inp.fileno())
                out.flush()
                os.fsync(out.fileno())
            after = src.stat()
            if (
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                or (after_open.st_dev, after_open.st_ino, after_open.st_size, after_open.st_mtime_ns)
                != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                or target.stat().st_size != before.st_size
            ):
                raise ReelsError(f"renderer input changed while copying: {src.name}")


def _revision(snapshot: dict, name: str) -> str:
    revisions = snapshot.get("revisions") or {}
    aliases = {"document": "document", "library": "library", "renderer": "renderer"}
    value = revisions.get(aliases[name], revisions.get({"document": "doc", "library": "library", "renderer": "renderer"}[name]))
    if not isinstance(value, (str, int)) or not str(value):
        raise ReelsError(f"preview snapshot is missing its {name} revision")
    return str(value)


def _stat_identity(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ReelsError(f"registered source is not a regular file: {path.name}")
    info = path.stat()
    return {
        "dev": info.st_dev,
        "ino": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "ctime_ns": info.st_ctime_ns,
        "quick_hash": quick_hash(path),
    }


def _source_record(project: Project, seg: Any) -> dict:
    source = project.abs(seg.asset["media"]).resolve(strict=True)
    expected = seg.asset.get("hash")
    identity = _stat_identity(source)
    if not expected or identity["quick_hash"] != expected:
        raise ReelsError(f"{seg.asset.get('id', 'asset')}: original source identity changed; preview is blocked")
    return {
        "asset": seg.asset.get("id"),
        "identifier": seg.asset.get("media"),
        "identity": identity,
        "probe": seg.asset.get("probe"),
    }


def _stage_spec(spec: dict) -> dict:
    return {key: spec[key] for key in sorted(_STAGE_SPEC_FIELDS) if key in spec}


def _pipeline_digest(code_root: Path) -> str:
    digest = hashlib.sha256()
    digest.update(Path(__file__).read_bytes())
    roots = (code_root / "scripts/reels", code_root / "remotion/src")
    for root in roots:
        if not root.exists():
            continue
        _safe_tree(root)
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in (".py", ".ts", ".tsx", ".css"):
                digest.update(path.relative_to(code_root).as_posix().encode())
                digest.update(path.read_bytes())
    for relative in (
        "remotion/editor-preview.tsx",
        "remotion/editor-preview.css",
        "remotion/editorStaticFile.ts",
        "remotion/editor-preview.html",
        "remotion/scripts/build-editor-preview.mjs",
    ):
        path = code_root / relative
        if path.is_symlink():
            raise ReelsError(f"renderer bridge source is a symlink: {relative}")
        if path.is_file():
            digest.update(relative.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _cache_identity(project: Project, built: dict, snapshot: dict, code_root: Path) -> tuple[str, dict]:
    renderer_revision = _revision(snapshot, "renderer")
    used: list[dict] = []
    for seg in built["segs"]:
        used.append({"source": _source_record(project, seg), "spec": _stage_spec(seg.spec), "kind": seg.kind,
                     "frames": int(seg.frames)})
        for cut in seg.spec.get("_cutaways") or []:
            cseg = cut["seg"]
            used.append({"source": _source_record(project, cseg), "spec": _stage_spec(cseg.spec), "kind": cseg.kind,
                         "frames": int(cseg.frames)})
    exhibits = []
    for exhibit in built.get("exhibits") or []:
        source = exhibit.get("source")
        if source is None:
            source_record = None
        else:
            source = Path(source).resolve(strict=True)
            source_record = {"path": str(source), "identity": _stat_identity(source)}
        exhibits.append({
            "source": source_record,
            "crop": exhibit.get("crop"),
            "width": (exhibit.get("props") or {}).get("width"),
        })
    identity = {
        "version": 1,
        "rendererRevision": renderer_revision,
        "pipeline": _pipeline_digest(code_root),
        "fps": built["fps"],
        "render": project.config.get("render"),
        "cleanNat": (built.get("sound") or {}).get("cleanNat"),
        "grade": built.get("grade"),
        "segments": used,
        "exhibits": exhibits,
        "foregrounds": built["props"].get("foregrounds") or [],
    }
    serialized = _json_bytes(identity)
    return hashlib.sha256(serialized).hexdigest(), identity


def alpha_foreground_fallback_reason(props: dict) -> str | None:
    """Interactive VP9/WebKit preview cannot safely preflight foreground alpha."""
    if props.get("foregrounds"):
        return (
            "Foreground overlays require exact rendering because the interactive "
            "preview cannot reliably preserve their alpha channel."
        )
    return None


def _readonly_tree(root: Path) -> None:
    for current, dirs, files in os.walk(root, topdown=False, followlinks=False):
        base = Path(current)
        for name in files:
            path = base / name
            if path.is_symlink():
                raise ReelsError("staged cache unexpectedly contains a symlink")
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        for name in dirs:
            path = base / name
            path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
    root.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def _cache_tree_safe(root: Path) -> None:
    if root.is_symlink():
        raise ReelsError("staged-media cache path is a symlink")
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        for name in dirs:
            path = base / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
                raise ReelsError("staged-media cache unexpectedly contains a symlink")
        for name in files:
            path = base / name
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ReelsError("staged-media cache contains a non-regular or hard-linked file")


def _product_kind_and_options(name: str, args: tuple, kwargs: dict) -> tuple[Any, Path, dict]:
    _, seg, out = args[:3]
    options: dict[str, Any]
    if name == "stage_video":
        fps = args[3] if len(args) > 3 else kwargs.get("fps")
        options = {
            "fps": fps,
            "clean": bool(kwargs.get("clean", False)),
            "scale": float(kwargs.get("scale", 1.0)),
            "edges": list(kwargs.get("edges", (True, True))),
            "audio": bool(kwargs.get("audio", True)),
        }
    elif name == "stage_photo_pan":
        fps = args[3] if len(args) > 3 else kwargs.get("fps")
        options = {"fps": fps}
    else:
        options = {}
    if kwargs.get("grade") is not None:  # resolved colour grade (config < plan < clip): a new grade is a new product
        options["grade"] = kwargs["grade"]
    return seg, Path(out), options


def _product_key(
    project: Project,
    seg: Any,
    stage_function: str,
    options: dict,
    built: dict,
    renderer_revision: str,
    pipeline_revision: str,
    runtime_revision: str,
) -> tuple[str, dict]:
    source = _source_record(project, seg)
    identity = {
        "version": 1,
        "renderer": renderer_revision,
        "pipeline": pipeline_revision,
        "runtime": runtime_revision,
        "source": source,
        "spec": _stage_spec(seg.spec),
        "kind": seg.kind,
        "frames": int(seg.frames),
        "fps": built["fps"],
        "render": project.config.get("render"),
        "cleanNat": (built.get("sound") or {}).get("cleanNat"),
        "stage": {"function": stage_function, **options},
    }
    return hashlib.sha256(_json_bytes(identity)).hexdigest(), source


def _product_roles(stage_function: str, out: Path) -> dict[str, Path]:
    roles = {"main": out}
    if stage_function in ("stage_video", "stage_photo_pan"):
        roles["camera"] = out.with_name(f"{out.stem}.cam.cmd")
    elif stage_function == "stage_photo":
        roles["background"] = out.with_name(f"{out.stem}.bg.jpg")
    return roles


def _product_manifest(entry: Path, key: str) -> dict | None:
    if entry.is_symlink():
        raise ReelsError("staged-media product cache path is a symlink")
    if not entry.exists():
        return None
    if not entry.is_dir():
        raise ReelsError("staged-media product cache entry is not a directory")
    _cache_tree_safe(entry)
    marker_path = entry / "cache.json"
    if marker_path.is_symlink():
        raise ReelsError("staged-media product cache marker is a symlink")
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(marker, dict) or marker.get("key") != key:
        return None
    files = marker.get("files")
    if not isinstance(files, list) or not files:
        return None
    expected: set[str] = {"cache.json"}
    roles: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            return None
        role, name = item.get("role"), item.get("name")
        if role not in ("main", "camera", "background") or role in roles:
            return None
        if name != {"main": "main", "camera": "camera.cmd", "background": "background.jpg"}[role]:
            return None
        path = entry / name
        if path.is_symlink() or not path.is_file():
            return None
        info = path.stat()
        if info.st_nlink != 1 or info.st_size != item.get("size"):
            return None
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != item.get("sha256"):
            return None
        expected.add(name)
        roles.add(role)
    if "main" not in roles:
        return None
    if {path.name for path in entry.iterdir()} != expected:
        return None
    return marker


def _restore_product(entry: Path, roles: dict[str, Path], marker: dict) -> None:
    by_role = {item["role"]: item["name"] for item in marker["files"]}
    for role, destination in roles.items():
        cached_name = by_role.get(role)
        if cached_name is not None:
            shutil.copy2(entry / cached_name, destination)


def _publish_product(entry: Path, key: str, roles: dict[str, Path]) -> None:
    if entry.exists() or entry.is_symlink():
        # An invalid immutable entry is a miss, but must never be replaced by a
        # concurrent worker or silently rewritten in place.
        return
    scratch = entry.parent / f".{entry.name}.{uuid.uuid4().hex}.tmp"
    scratch.mkdir(mode=0o700)
    try:
        files = []
        for role, source in roles.items():
            if not source.exists():
                continue
            if source.is_symlink() or not source.is_file() or source.stat().st_nlink != 1:
                raise ReelsError("renderer produced an unsafe staged-media product")
            name = {"main": "main", "camera": "camera.cmd", "background": "background.jpg"}[role]
            target = scratch / name
            shutil.copy2(source, target)
            files.append({
                "role": role,
                "name": name,
                "size": target.stat().st_size,
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            })
        if not any(item["role"] == "main" for item in files):
            raise ReelsError("renderer completed without a usable staged-media product")
        _atomic_json(scratch / "cache.json", {"key": key, "files": files})
        _readonly_tree(scratch)
        try:
            os.replace(scratch, entry)
        except OSError:
            # A concurrent publisher wins. Never replace its immutable entry;
            # the rendered output in this worker's stage tree remains usable.
            if not entry.exists() and not entry.is_symlink():
                raise
    finally:
        if scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)


def _staged_media_count(built: dict) -> int:
    return sum(1 + len(seg.spec.get("_cutaways") or []) for seg in built.get("segs") or [])


def _copy_current_audio(project: Project, built: dict, staging: Path) -> None:
    music = built.get("music")
    if music:
        path = Path(music["path"])
        shutil.copy2(path, staging / f"music{path.suffix.lower()}")
    for cue in (built.get("sound") or {}).get("cues") or []:
        name = Path(cue["path"]).name
        if name in ("", ".", ".."):
            raise ReelsError("sound-effect staging produced an unsafe filename")
        target = staging / "sfx" / name
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(cue["path"], target)


def _stage_cached(
    project: Project,
    built: dict,
    stage_dir: Path,
    cache_root: Path,
    cache_key: str,
    *,
    snapshot: dict | None = None,
    code_root: Path | None = None,
    pipeline_revision: str | None = None,
    media_stats: dict | None = None,
) -> bool:
    if cache_root.is_symlink():
        raise ReelsError("staged-media cache root is a symlink")
    cache_root = cache_root.resolve()
    cache = cache_root / cache_key
    stage_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    count = _staged_media_count(built)
    if cache.is_symlink():
        raise ReelsError("staged-media cache path is a symlink")
    if cache.is_dir() and (cache / "cache.json").is_file():
        _cache_tree_safe(cache)
        try:
            marker = json.loads((cache / "cache.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            marker = {}
        if marker.get("key") == cache_key:
            shutil.copytree(cache, stage_dir, ignore=shutil.ignore_patterns("cache.json"))
            # Cache assets are immutable, but the revision-owned staging root
            # still needs room for props and its audio copies.
            stage_dir.chmod(0o700)
            _copy_current_audio(project, built, stage_dir)
            if media_stats is not None:
                media_stats.update(hits=count, misses=0)
            return True
        raise ReelsError("staged-media cache identity is inconsistent; refusing to reuse it")
    if cache.exists():
        raise ReelsError("staged-media cache entry is not a regular directory")

    # Build the immutable cache artifact within its own write root so the
    # sandbox never needs to authorize cross-root rename semantics.
    cache_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = cache_root / f".stage-{uuid.uuid4().hex}"
    if temporary.exists():
        raise ReelsError("staging scratch path unexpectedly exists")
    from .. import render

    product_root = cache_root / "products"
    product_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if product_root.is_symlink():
        raise ReelsError("staged-media product cache root is a symlink")
    renderer_revision = _revision(snapshot or {}, "renderer") if snapshot is not None else "unknown"
    pipeline_revision = pipeline_revision or (
        _pipeline_digest(code_root) if code_root is not None else "unknown"
    )
    runtime_revision = str((snapshot or {}).get("runtimeRevision") or "")
    counts = {"hits": 0, "misses": 0}
    originals = {}
    try:
        for name in ("stage_video", "stage_photo", "stage_photo_pan"):
            original = getattr(render, name, None)
            if not callable(original):
                continue
            originals[name] = original

            def make_wrapper(stage_name, stage_original):
                def staged(*args, **kwargs):
                    seg, out, options = _product_kind_and_options(stage_name, args, kwargs)
                    key, source_before = _product_key(
                        args[0], seg, stage_name, options, built, renderer_revision,
                        pipeline_revision, runtime_revision,
                    )
                    entry = product_root / key
                    roles = _product_roles(stage_name, out)
                    marker = _product_manifest(entry, key)
                    if marker is not None:
                        _restore_product(entry, roles, marker)
                        if _source_record(args[0], seg) != source_before:
                            raise ReelsError(f"{seg.asset.get('id', 'asset')}: original changed during staged-media reuse.")
                        counts["hits"] += 1
                        return None
                    counts["misses"] += 1
                    try:
                        result = stage_original(*args, **kwargs)
                    finally:
                        if _source_record(args[0], seg) != source_before:
                            raise ReelsError(f"{seg.asset.get('id', 'asset')}: original changed during staging.")
                    _publish_product(entry, key, roles)
                    return result

                return staged

            setattr(render, name, make_wrapper(name, original))

        render.stage(project, built, temporary)
        for path in temporary.glob("music.*"):
            path.unlink()
        shutil.rmtree(temporary / "sfx", ignore_errors=True)
        _atomic_json(temporary / "cache.json", {"key": cache_key})
        _readonly_tree(temporary)
        try:
            os.replace(temporary, cache)
        except OSError:
            if not cache.is_dir() or not (cache / "cache.json").is_file():
                raise
            shutil.rmtree(temporary, ignore_errors=True)
    finally:
        for name, original in originals.items():
            setattr(render, name, original)
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    shutil.copytree(cache, stage_dir, ignore=shutil.ignore_patterns("cache.json"))
    stage_dir.chmod(0o700)
    _copy_current_audio(project, built, stage_dir)
    if media_stats is not None:
        media_stats.update(hits=counts["hits"], misses=counts["misses"])
    return False


def _bundle_key(code_root: Path, snapshot: dict) -> str:
    digest = hashlib.sha256()
    digest.update(_revision(snapshot, "renderer").encode())
    digest.update(_pipeline_digest(code_root).encode())
    package = code_root / "remotion/package-lock.json"
    if package.is_file():
        digest.update(package.read_bytes())
    return digest.hexdigest()


def _build_bundle(project: Project, code_root: Path, bundle_cache: Path, snapshot: dict) -> Path:
    key = _bundle_key(code_root, snapshot)
    destination = bundle_cache / key
    if (
        destination.is_dir()
        and (destination / "index.html").is_file()
        and (destination / "editor-preview.js").is_file()
        and (destination / "editor-preview.css").is_file()
    ):
        return destination
    remotion_dir = code_root / "remotion"
    esbuild = remotion_dir / "node_modules/.bin/esbuild"
    if not esbuild.is_file():
        raise ReelsError("Remotion preview dependencies are missing; install the isolated worktree's remotion dependencies.")
    bundle_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = bundle_cache / f".{key}.{uuid.uuid4().hex}.tmp"
    temporary.mkdir(mode=0o700)
    try:
        args = [
            str(esbuild),
            str(remotion_dir / "editor-preview.tsx"),
            "--bundle", "--minify", "--format=iife", "--platform=browser", "--target=es2022",
            "--loader:.woff2=file", "--asset-names=assets/[name]-[hash]", "--entry-names=editor-preview",
            f"--outdir={temporary}",
        ]
        proc = subprocess.run(args, cwd=remotion_dir, capture_output=True, text=True, check=False)
        if proc.returncode:
            raise ReelsError(f"editor preview bundle failed: {(proc.stdout + proc.stderr)[-4000:]}")
        shutil.copy2(remotion_dir / "editor-preview.html", temporary / "index.html")
        _readonly_tree(temporary)
        try:
            os.replace(temporary, destination)
        except OSError:
            if not (destination / "index.html").is_file():
                raise
    finally:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
    return destination


def _verify_sources(project: Project, built: dict, prior: dict[str, dict]) -> None:
    current: dict[str, dict] = {}
    for seg in built["segs"]:
        entries = [seg, *[cut["seg"] for cut in (seg.spec.get("_cutaways") or [])]]
        for item in entries:
            identifier = str(item.asset.get("media"))
            if identifier not in prior:
                continue
            path = project.abs(item.asset["media"]).resolve(strict=True)
            now = _stat_identity(path)
            if now != prior[identifier]:
                raise ReelsError(f"{item.asset.get('id')}: original changed during preview preparation.")
            current[identifier] = now


def _run_exact(project: Project, built: dict, stage_dir: Path, revision_dir: Path) -> dict:
    from .. import render

    browser = os.environ.get("REELS_BROWSER") or project.cfg("render", "browserExecutable")
    if not browser or not Path(str(browser)).is_file():
        raise ReelsError(
            "exact rendering requires an installed Remotion browser; no browser download was attempted"
        )
    props_path = stage_dir / "props.json"
    _atomic_json(props_path, built["props"])
    intermediate = revision_dir / "render.mp4"
    final = revision_dir / "exact.mp4"
    log_path = project.root / ".cache/editor-remotion-render.log"
    log_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    render.run_remotion(project, props_path, stage_dir, intermediate, log_path)
    boost = project.cfg("audio", "rideOnlyMaxBoost") if built.get("music") is None else None
    render.final_encode(project, intermediate, final, float(boost) if boost is not None else None)
    duration = built["total"] / built["fps"]
    preview = None
    ig = (built.get("music") or {}).get("instagram")
    if ig:
        preview = revision_dir / "preview.mp4"
        render.mix_preview(project, final, built["music"]["path"], ig, duration, preview, fade=not bool(built.get("loop")))
    return {
        "exactPath": str(final),
        "attachedAudioPreviewPath": str(preview) if preview else None,
        "movie": str(preview or final),
    }


def _usage_fields(data_root: Path) -> dict:
    assets: dict[str, list] = {}
    music: dict[str, list] = {}
    for path in sorted((data_root / "library/assets").glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ReelsError("export usage snapshot contains an unsafe asset record")
        value = json.loads(path.read_text(encoding="utf-8"))
        assets[path.stem] = list(value.get("used") or [])
    for path in sorted((data_root / "music").glob("*.json")):
        if path.is_symlink() or not path.is_file():
            raise ReelsError("export usage snapshot contains an unsafe music record")
        value = json.loads(path.read_text(encoding="utf-8"))
        music[path.stem] = list(value.get("usedIn") or [])
    return {"assets": assets, "music": music}


def _verify_export_sources(project: Project, post: dict, originals: dict) -> None:
    seen: set[str] = set()
    for entry in post.get("segments") or []:
        asset_id = entry.get("asset")
        if not isinstance(asset_id, str) or asset_id in seen:
            continue
        seen.add(asset_id)
        asset_path = project.assets_dir / f"{asset_id}.json"
        if asset_path.is_symlink() or not asset_path.is_file():
            raise ReelsError(f"{asset_id}: private export asset record disappeared")
        asset = json.loads(asset_path.read_text(encoding="utf-8"))
        record = originals.get(asset.get("media"))
        if not record or record.get("identity") is None:
            raise ReelsError(f"{asset_id}: registered original identity is unavailable")
        source = Path(record["path"]).resolve(strict=True)
        if _stat_identity(source) != record["identity"]:
            raise ReelsError(f"{asset_id}: original changed during export; the draft is not publishable")


def export(job: dict) -> dict:
    """Run the production renderer path only against an editor-private data clone."""
    from ..render import render_plan

    data_root = Path(job["dataRoot"]).resolve(strict=True)
    code_root = Path(job["codeRoot"]).resolve(strict=True)
    export_root = Path(job["exportRoot"]).resolve()
    if not export_root.is_relative_to(Path(job["workspaceRoot"]).resolve()):
        raise ReelsError("export output escaped the private editor workspace")
    if not data_root.is_relative_to(export_root):
        raise ReelsError("export data is not inside its revision-owned private directory")
    plan = job.get("plan")
    if not isinstance(plan, dict):
        raise ReelsError("export requires the frozen document snapshot's raw plan")
    slug = plan.get("slug")
    if not isinstance(slug, str) or not slug or "/" in slug or "\\" in slug:
        raise ReelsError("export plan has an invalid slug")
    plan_path = data_root / "plans" / f"{slug}.json"
    _atomic_json(plan_path, plan)
    originals = job.get("originals")
    if not isinstance(originals, dict):
        raise ReelsError("export has no registered-original manifest")
    project = Project(
        data_root,
        code_root=code_root,
        media_root=Path(job["mediaRoot"]),
    )
    before = _usage_fields(data_root)
    post = render_plan(project, plan_path)
    _verify_export_sources(project, post, originals)
    after = _usage_fields(data_root)
    usage_updates = {"assets": {}, "music": {}}
    for category in ("assets", "music"):
        for identifier in sorted(set(before[category]) | set(after[category])):
            previous = before[category].get(identifier, [])
            current = after[category].get(identifier, [])
            if current != previous:
                usage_updates[category][identifier] = current
    output_root = project.out_dir / slug
    return {
        "revisions": {
            "document": str((job.get("revisions") or {}).get("document")),
            "library": str((job.get("revisions") or {}).get("library")),
            "renderer": str((job.get("revisions") or {}).get("renderer")),
        },
        "slug": slug,
        "post": post,
        "outputDir": str(output_root),
        "reelPath": str(output_root / "reel.mp4"),
        "previewVideoPath": str(output_root / "preview.mp4") if (output_root / "preview.mp4").is_file() else None,
        "privateSnapshotDir": str(data_root),
        "usageUpdates": usage_updates,
        "sideEffects": "private-snapshot-only",
    }


def prepare(job: dict) -> dict:
    from ..render import build
    from ..util import read_json

    job_root = Path(job["jobRoot"]).resolve(strict=True)
    data_root = Path(job["dataRoot"]).resolve(strict=True)
    code_root = Path(job["codeRoot"]).resolve(strict=True)
    revision_dir = Path(job["revisionDir"])
    cache_root = Path(job["cacheRoot"])
    bundle_cache = Path(job["bundleCache"])
    if not revision_dir.is_relative_to(Path(job["workspaceRoot"]).resolve()):
        raise ReelsError("preview output escaped the private editor workspace")
    if revision_dir.exists() or revision_dir.is_symlink():
        raise ReelsError("preview revision output already exists")
    revision_dir.mkdir(parents=True, mode=0o700)
    plan = job.get("plan")
    if not isinstance(plan, dict):
        raise ReelsError("preview snapshot must contain the raw plan object")
    slug = plan.get("slug")
    if not isinstance(slug, str) or not slug or "/" in slug or "\\" in slug:
        raise ReelsError("preview plan has an invalid slug")
    plan_path = data_root / "plans" / f"{slug}.json"
    _atomic_json(plan_path, plan)

    originals = job.get("originals")
    if not isinstance(originals, dict):
        raise ReelsError("preview job has no registered-original manifest")
    project = Project(
        data_root,
        code_root=code_root,
        media_root=Path(job["mediaRoot"]),
    )
    if job.get("mode") not in ("interactive", "exact"):
        raise ReelsError("preview mode must be interactive or exact")
    built = build(project, plan)
    warnings = list(built.get("warnings") or [])
    requested_mode = job["mode"]
    fallback_reason = (
        alpha_foreground_fallback_reason(built.get("props") or {})
        if requested_mode == "interactive"
        else None
    )
    effective_mode = "exact" if fallback_reason else requested_mode
    if fallback_reason:
        warnings.append(f"Interactive preview switched to exact rendering: {fallback_reason}")
    revisions = {
        "document": _revision(job, "document"),
        "library": _revision(job, "library"),
        "renderer": _revision(job, "renderer"),
    }
    cache_key, cache_identity = _cache_identity(project, built, job, code_root)
    source_identities = {
        item["source"]["identifier"]: item["source"]["identity"]
        for item in cache_identity["segments"]
    }
    stage_dir = revision_dir / "public"
    media_cache = {"hits": 0, "misses": 0}
    reused = _stage_cached(
        project, built, stage_dir, cache_root, cache_key,
        snapshot=job, code_root=code_root, pipeline_revision=cache_identity.get("pipeline"),
        media_stats=media_cache,
    )
    _verify_sources(project, built, source_identities)
    bundle_dir = _build_bundle(project, code_root, bundle_cache, job)
    props_path = stage_dir / "props.json"
    _atomic_json(props_path, built["props"])
    exact = _run_exact(project, built, stage_dir, revision_dir) if effective_mode == "exact" else {}
    _verify_sources(project, built, source_identities)
    return {
        "revisions": revisions,
        "mode": effective_mode,
        "requestedMode": requested_mode,
        "fallbackReason": fallback_reason,
        "bundleDir": str(bundle_dir),
        "staticDir": str(stage_dir),
        "propsPath": str(props_path),
        "props": built["props"],
        "durationInFrames": built["total"],
        "fps": built["fps"],
        "width": built["props"]["width"],
        "height": built["props"]["height"],
        "warnings": warnings,
        "cacheKey": cache_key,
        "stagingReused": reused,
        "mediaCache": media_cache,
        **exact,
    }


def sandbox_probe(job_path: Path) -> int:
    job = json.loads(job_path.read_text(encoding="utf-8"))
    writable = Path(job["writable"])
    protected = Path(job["protected"])
    writable.mkdir(parents=True, exist_ok=True, mode=0o700)
    writable_probe = writable / "write-probe"
    writable_probe.write_text("sandbox-write-ok", encoding="ascii")
    if writable_probe.read_text(encoding="ascii") != "sandbox-write-ok":
        return 11
    try:
        protected.write_text("must-not-be-written", encoding="ascii")
    except OSError:
        pass
    else:
        return 12

    try:
        subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return 13
    try:
        subprocess.run(["/bin/sh", "-c", "exit 0"], check=True, timeout=5)
    except OSError:
        pass
    else:
        return 14

    # Confirm that the only network exception is loopback, used by Remotion's
    # local renderer. A TEST-NET address cannot reach a real service.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.settimeout(2)
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            with socket.create_connection(server.getsockname(), timeout=2) as client:
                client.sendall(b"loopback")
                conn, _ = server.accept()
                with conn:
                    if conn.recv(16) != b"loopback":
                        return 15
    except OSError:
        return 16
    try:
        with socket.create_connection(("203.0.113.1", 9), timeout=0.5):
            return 17
    except OSError as exc:
        if getattr(exc, "errno", None) not in (1, 13, 10013):
            return 18
    return 0


def validate_candidate(job: dict) -> dict:
    """Run fixed checks and a real exact render inside this worker's sandbox."""
    code_root = Path(job["codeRoot"]).resolve(strict=True)
    remotion = code_root / "remotion"
    tests = Path(job.get("trustedTestsRoot") or code_root / "tests").resolve(strict=True)
    env = dict(job["env"])
    env["PYTHONPATH"] = os.pathsep.join((str(code_root / "scripts"), str(tests)))
    checks: list[dict[str, Any]] = []

    def run(
        label: str,
        command: list[str],
        cwd: Path,
        timeout: int = 600,
        run_env: dict[str, str] | None = None,
    ) -> None:
        process = subprocess.run(
            command,
            cwd=cwd,
            env=run_env or env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        checks.append({"name": label, "returncode": process.returncode})
        if process.returncode:
            text = (process.stdout + "\n" + process.stderr)[-10000:]
            raise ReelsError(f"{label} failed:\n{text}")

    # Parse candidate Python without importing it into the broker or writing .pyc files.
    import ast

    for source in sorted((code_root / "scripts/reels").rglob("*.py")):
        ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    checks.append({"name": "python-syntax", "returncode": 0})

    tsc = remotion / "node_modules/.bin/tsc"
    if not tsc.is_file():
        raise ReelsError("renderer validation cannot find the isolated Remotion TypeScript compiler")
    run("remotion-typecheck", [str(tsc), "--noEmit", "--project", str(remotion / "tsconfig.json")], remotion)
    node = job["node"]
    build_env = dict(env)
    build_env["REELS_EDITOR_PREVIEW_OUT"] = str(Path(job["jobRoot"]) / "candidate-preview-bundle")
    run(
        "editor-preview-bundle",
        [node, str(remotion / "scripts/build-editor-preview.mjs")],
        remotion,
        run_env=build_env,
    )

    fixed = (
        "test_asset_updates",
        "test_search",
        "test_cutaways",
        "test_bleep",
        "test_exhibits",
        "test_blur",
        "test_titlecard",
        "test_moment_references",
        "test_kino",
        "test_mix_streams",
    )
    run(
        "renderer-contract-tests",
        [sys.executable, "-m", "unittest", *fixed],
        tests,
        timeout=900,
    )
    candidate_tests = code_root / "tests" / "renderer_effects"
    if candidate_tests.is_dir() and any(candidate_tests.glob("test_*.py")):
        run("candidate-effect-tests",
            [sys.executable, "-m", "unittest", "discover", "-s", str(candidate_tests), "-p", "test_*.py"],
            tests, timeout=900)

    exact_job = dict(job["previewJob"])
    exact_job.update(
        codeRoot=str(code_root),
        mode="exact",
        workspaceRoot=job["workspaceRoot"],
        cacheRoot=job["cacheRoot"],
        bundleCache=job["bundleCache"],
        env=env,
        node=node,
    )
    rendered = prepare(exact_job)
    return {"checks": checks, "render": rendered}


@proxy_job
def prepare_source_proxy(job: dict) -> dict:
    """Create/reuse a view-specific 360 or codec/container compatibility proxy."""
    source = Path(job["sourcePath"]).resolve(strict=True)
    media_root = Path(job["mediaRoot"]).resolve(strict=True)
    workspace_root = Path(job["workspaceRoot"]).resolve(strict=True)
    revision_dir = Path(job["revisionDir"]).resolve()
    if not source.is_relative_to(media_root) or source.is_symlink() or not source.is_file():
        raise ReelsError("source proxy input is not a registered original-media file")
    if not revision_dir.is_relative_to(workspace_root):
        raise ReelsError("source proxy output escaped the private editor workspace")
    expected_identity = job.get("sourceIdentity")
    if not isinstance(expected_identity, dict) or _stat_identity(source) != expected_identity:
        raise ReelsError("registered original changed before source proxy preparation")

    def verify_source_unchanged() -> None:
        if _stat_identity(source) != expected_identity:
            raise ReelsError("registered original changed during source proxy preparation")

    probe = job.get("probe")
    probe = probe if isinstance(probe, dict) else {}
    sphere = probe.get("sphere")
    source_kind = str(job.get("sourceKind") or "video")
    is_photo = source_kind in {"photo", "image"}
    if source_kind not in {"photo", "image", "video"}:
        verify_source_unchanged()
        return {"status": "unsupported", "reason": f"Unsupported source kind: {source_kind}."}
    if str(source_kind) == "image":
        is_photo = True
    is_spherical = isinstance(sphere, dict)
    if source.suffix.lower() == ".360" and not is_spherical:
        verify_source_unchanged()
        return {
            "status": "unsupported",
            "reason": "360 source metadata is unavailable; refusing to show an unreframed source.",
        }
    view = job.get("view") or {}
    if not isinstance(view, dict):
        verify_source_unchanged()
        return {"status": "unsupported", "reason": "Source view must be an object."}
    projection = str(view.get("projection", "flat"))
    if is_spherical and projection not in ("flat", "tiny-planet"):
        verify_source_unchanged()
        return {"status": "unsupported", "reason": "Source proxy projection must be flat or tiny-planet."}
    proxy_kind = "360-photo" if is_spherical and is_photo else (
        "360-video" if is_spherical else ("photo" if is_photo else "video")
    )
    width, height = (720, 1280) if is_spherical else (0, 0)
    # A 360 video with no requested view gets the browsing layout: level front and back views
    # side by side (front = the measured direction of travel; see stabilize.front_back_views).
    layout = "front-back" if is_spherical and not is_photo and not view else "single"
    front_back: list[dict] = []
    if is_spherical and not is_photo:
        try:
            from ..sphere import DEFAULT_VIEW, source_graph, v360_filter, view_params

            filter_graph, pad, input_projection = source_graph(probe)
            if layout == "front-back":
                from ..stabilize import front_back_views, read_orientation, travel_direction

                gravity = travel = None
                try:  # telemetry only levels and aims the views; unreadable telemetry keeps the defaults
                    ori = read_orientation(source, 0.0, float(probe.get("duration") or 0) + 1)
                    gravity = ori.gravity if ori is not None else None
                    travel = travel_direction(source, gravity) if gravity else None
                except (ReelsError, OSError, ValueError):
                    pass
                front_back = front_back_views(gravity, job.get("angles") if isinstance(job.get("angles"), dict) else None,
                                              travel=travel)
                cams = [v360_filter(input_projection, "flat", view_params(v, "flat", width, height), width, height)
                        for v in front_back]
                # browsing proxy: 30 fps and linear sampling keep a full clip to a few minutes
                cams = [c.replace(":interp=cubic", ":interp=linear") for c in cams]
                camera = f"fps=30,split=2[fbf][fbb];[fbf]{cams[0]}[fbfv];[fbb]{cams[1]}[fbbv];[fbfv][fbbv]hstack=2"
            else:
                view_value = {**DEFAULT_VIEW, **view}
                camera = v360_filter(
                    input_projection,
                    projection,
                    view_params(view_value, projection, width, height),
                    width,
                    height,
                )
        except (KeyError, TypeError, ValueError, OSError) as exc:
            verify_source_unchanged()
            return {"status": "unsupported", "reason": f"Invalid 360 source view or probe metadata: {exc}"}
    else:
        filter_graph = pad = camera = ""

    vcodec = str(probe.get("vcodec") or "")
    fps = probe.get("fps")

    revision_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if revision_dir.is_symlink():
        raise ReelsError("source proxy output directory cannot be a symlink")
    output = revision_dir / ("source.png" if is_photo else "source.mp4")
    marker = revision_dir / "source.json"
    if output.is_symlink() or marker.is_symlink():
        raise ReelsError("source proxy cache contains a symlink")
    expected_marker = {
        "version": PROXY_VERSION,
        "assetId": str(job.get("assetId", "")),
        "kind": proxy_kind,
        "sourceKind": source_kind,
        "sourceIdentity": expected_identity,
        "view": view,
        "projection": projection,
        "layout": layout,
        "views": front_back,
        "width": width,
        "height": height,
        "codec": {"video": "png" if is_photo else "h264-or-hevc", "audio": None if is_photo else "aac"},
        "sourceCodec": vcodec,
        "fps": fps,
        "probe": probe,
    }
    if output.is_file() and marker.is_file():
        try:
            if json.loads(marker.read_text(encoding="utf-8")) == expected_marker:
                if _stat_identity(source) != expected_identity:
                    raise ReelsError("registered original changed during source proxy reuse")
                return {
                    "status": "ready", "path": str(output), "proxy": True, "reused": True,
                    "sourceKind": "image" if is_photo else "video", "layout": layout,
                }
        except (OSError, ValueError):
            pass
    if is_spherical and not is_photo:
        job["reserveOutput"]()

    temporary = revision_dir / f".source-{uuid.uuid4().hex}{output.suffix}"
    from ..media import run

    try:
        if is_photo:
            if is_spherical:
                from ..sphere import DEFAULT_VIEW, photo_view

                try:
                    photo_view(source, temporary, {**DEFAULT_VIEW, **view}, width, height, projection)
                except (KeyError, TypeError, ValueError) as exc:
                    verify_source_unchanged()
                    return {"status": "unsupported", "reason": f"Invalid 360 photo view or probe metadata: {exc}"}
            else:
                command = [
                    "ffmpeg", "-v", "error", "-y", "-i", source, "-frames:v", "1",
                    "-vf", "scale='min(1920,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
                    temporary,
                ]
                run(command, timeout=300)
        elif is_spherical:
            encode_proxy(source, temporary, [
                "-filter_complex", f"{filter_graph};[{pad}]{camera}[source]",
                "-map", "[source]", "-map", "0:a:0?", "-fps_mode", "passthrough",
            ], seconds=float(probe.get("duration") or 0), crf=21)
        else:
            if not probe.get("width") or not probe.get("height"):
                verify_source_unchanged()
                return {
                    "status": "unsupported",
                    "reason": "Video dimensions are unavailable; a safe compatibility proxy cannot be prepared.",
                }
            command = [
                "ffmpeg", "-v", "error", "-y", "-i", source,
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "scale='min(1920,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,setsar=1",
                "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "ultrafast",
                "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", temporary,
            ]
            run(command, timeout=1800)
        if not temporary.is_file() or temporary.stat().st_size <= 0:
            raise ReelsError("proxy encoder completed without a usable source proxy")
        verify_source_unchanged()
        os.replace(temporary, output)
        output.chmod(0o400)
        _atomic_json(marker, expected_marker)
        marker.chmod(0o400)
        verify_source_unchanged()
    except (ReelsError, OSError, subprocess.SubprocessError) as exc:
        try:
            verify_source_unchanged()
        except ReelsError:
            raise ReelsError("registered original changed during source proxy preparation") from exc
        return {"status": "unsupported", "reason": f"Could not prepare a playable source proxy: {exc}"}
    finally:
        if temporary.exists():
            temporary.unlink()
    return {
        "status": "ready", "path": str(output), "proxy": True, "reused": False,
        "sourceKind": "image" if is_photo else "video", "layout": layout,
    }


@proxy_job
def prepare_sphere_proxy(job: dict) -> dict:
    """An equirectangular proxy of part of a 360 video for the editor's live 360 viewer,
    plus the per-frame stabilization rotations (and gravity) for that range.

    The picture is the whole sphere with no view applied, so aiming, keyframes and
    stabilization strength change instantly in the app without re-encoding."""
    source = Path(job["sourcePath"]).resolve(strict=True)
    media_root = Path(job["mediaRoot"]).resolve(strict=True)
    workspace_root = Path(job["workspaceRoot"]).resolve(strict=True)
    revision_dir = Path(job["revisionDir"]).resolve()
    if not source.is_relative_to(media_root) or source.is_symlink() or not source.is_file():
        raise ReelsError("360 proxy input is not a registered original-media file")
    if not revision_dir.is_relative_to(workspace_root):
        raise ReelsError("360 proxy output escaped the private editor workspace")
    expected_identity = job.get("sourceIdentity")
    if not isinstance(expected_identity, dict) or _stat_identity(source) != expected_identity:
        raise ReelsError("registered original changed before 360 proxy preparation")
    probe = job.get("probe") if isinstance(job.get("probe"), dict) else {}
    if not isinstance(probe.get("sphere"), dict):
        return {"status": "unsupported", "reason": "This clip isn't 360 footage."}
    duration = float(probe.get("duration") or 0)
    start = max(0.0, float(job["start"]))
    end = float(job["end"])
    if duration > 0:
        end = min(end, duration)
    if not end > start or end - start > SPHERE_PROXY_MAX + 1e-6:
        return {"status": "unsupported", "reason": f"A 360 proxy covers up to {SPHERE_PROXY_MAX:g} s of a clip."}

    from ..sphere import source_graph
    from ..stabilize import parse_setting
    from .proxy_stabilize import cached_correction_quats

    errors: list[str] = []
    setting = parse_setting(job.get("stabilize"), errors, "stabilize")
    if errors:
        return {"status": "unsupported", "reason": errors[0]}

    width, height = SPHERE_PROXY_WIDTH, SPHERE_PROXY_WIDTH // 2
    revision_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if revision_dir.is_symlink():
        raise ReelsError("360 proxy output directory cannot be a symlink")
    settings = {
        "version": PROXY_VERSION, "assetId": str(job.get("assetId", "")), "sourceIdentity": expected_identity,
        "fps": SPHERE_PROXY_FPS, "width": width, "height": height, "probe": probe,
    }
    # Choose the smallest covering range, including a prewarmed 180 s chunk.
    # Compare the actual request, not its optional five-second trim padding.
    requested_start = float(job.get("requestedStart", start))
    requested_end = float(job.get("requestedEnd", end))
    candidates = []
    for marker_path in revision_dir.glob("*/sphere.json"):
        if marker_path.parent.is_symlink() or marker_path.is_symlink():
            continue
        try:
            saved = json.loads(marker_path.read_text(encoding="utf-8"))
            lo, hi = float(saved["start"]), float(saved["end"])
            video = marker_path.with_name("sphere.mp4")
            if (all(saved.get(k) == v for k, v in settings.items())
                    and lo <= requested_start < requested_end <= hi
                    and video.is_file() and not video.is_symlink() and video.stat().st_size > 0):
                candidates.append((hi - lo, lo, hi, marker_path.parent))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    reused = bool(candidates)
    if reused:
        _, start, end, range_dir = min(candidates)
    else:
        range_dir = revision_dir / f"{start:.3f}-{end:.3f}"
    if range_dir.is_symlink():
        raise ReelsError("360 proxy range directory cannot be a symlink")
    range_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = range_dir / "sphere.mp4"
    marker = range_dir / "sphere.json"
    if output.is_symlink() or marker.is_symlink():
        raise ReelsError("360 proxy cache contains a symlink")
    expected_marker = {**settings, "start": start, "end": end}
    if not reused:
        job["reserveOutput"]()
        graph, pad, inproj = source_graph(probe)
        temporary = range_dir / f".sphere-{uuid.uuid4().hex}.mp4"
        try:
            encode_proxy(source, temporary, [
                "-filter_complex",
                f"{graph};[{pad}]fps={SPHERE_PROXY_FPS},v360=input={inproj}:output=e:w={width}:h={height}:interp=linear[sphere]",
                "-map", "[sphere]", "-map", "0:a:0?",
            ], inputs=["-ss", f"{start:.3f}", "-t", f"{end - start:.3f}"],
                seconds=end - start)
            if not temporary.is_file() or temporary.stat().st_size <= 0:
                raise ReelsError("360 proxy encoder produced no file")
            if _stat_identity(source) != expected_identity:
                raise ReelsError("registered original changed during 360 proxy preparation")
            os.replace(temporary, output)
            output.chmod(0o400)
            if marker.exists():
                marker.chmod(0o600)
            _atomic_json(marker, expected_marker)
            marker.chmod(0o400)
        except (ReelsError, OSError, subprocess.SubprocessError) as exc:
            return {"status": "unsupported", "reason": f"Could not prepare the 360 proxy: {exc}"}
        finally:
            if temporary.exists():
                temporary.unlink()

    correction = cached_correction_quats(source, revision_dir / "cori", expected_identity,
                                        requested_start, requested_end, SPHERE_PROXY_FPS, setting,
                                        reserve=job["reserveOutput"])
    if _stat_identity(source) != expected_identity:
        raise ReelsError("registered original changed during 360 proxy reuse")
    return {
        "status": "ready", "path": str(output), "reused": reused,
        "start": start, "end": end, "fps": SPHERE_PROXY_FPS, "width": width, "height": height,
        **correction, "stabilize": setting,
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "--sandbox-probe":
        return sandbox_probe(Path(args[1]))
    if len(args) != 1:
        print("usage: python -m reels.editor.worker JOB.json", file=sys.stderr)
        return 2
    try:
        job_path = Path(args[0]).resolve(strict=True)
        job = json.loads(job_path.read_text(encoding="utf-8"))
        if job.get("action") == "validate":
            result = validate_candidate(job)
        elif job.get("action") == "source_proxy":
            result = prepare_source_proxy(job)
        elif job.get("action") == "sphere_proxy":
            result = prepare_sphere_proxy(job)
        elif job.get("action") == "export":
            result = export(job)
        else:
            result = prepare(job)
        _atomic_json(Path(job["resultPath"]), result)
        return 0
    except Exception as exc:  # worker boundary serializes expected and unexpected failures
        try:
            job = json.loads(Path(args[0]).read_text(encoding="utf-8"))
            _atomic_json(Path(job["resultPath"]), {"error": f"{type(exc).__name__}: {exc}"})
        except Exception:
            pass
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
