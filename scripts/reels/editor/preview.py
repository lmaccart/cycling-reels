"""Revision-frozen Remotion previews, isolated from the editor service broker."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import platform
import shutil
import site
import subprocess
import sys
import uuid
from pathlib import PurePosixPath
from pathlib import Path
from typing import Any, Callable

from ..util import ReelsError


def empty_preview(snapshot: dict, mode: str) -> dict | None:
    """Empty footage has no preview artifact, including when every timeline clip is disabled."""
    plan = snapshot.get("plan") or {}
    segments = plan.get("segments") or []
    if any(segment.get("enabled") is not False for segment in segments):
        return None
    if not segments and plan.get("timeline"):
        return None  # an explicitly positioned blank canvas can still render
    revisions = snapshot.get("revisions") or {}
    return {"state": "empty", "mode": mode, "documentId": snapshot.get("documentId"),
            "revisions": dict(revisions), "revision": str(revisions.get("document", "")),
            "reason": "Enable footage to preview this reel." if segments else "Add footage to preview this reel."}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _stable_signature(path: Path) -> tuple[int, int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def source_proxy_reason(asset: dict, path: Path) -> str | None:
    """Use the same native-playability decision for discovery and preparation."""
    probe = asset.get("probe") or {}
    suffix = path.suffix.lower()
    if suffix in {".360", ".insv"} or probe.get("sphere"):
        return "360 footage requires a view-specific source proxy."
    if asset.get("kind") == "video" and not (
        suffix in {".mp4", ".mov", ".m4v"}
        and str(probe.get("vcodec") or "").lower() in {"h264", "hevc", "prores", "mjpeg"}
    ):
        return "This video format requires an H.264/AAC source proxy."
    if asset.get("kind") == "photo" and suffix not in {".jpg", ".jpeg", ".png", ".heic", ".heif"}:
        return "This photo format requires a native image proxy."
    return None


def _allowed_by(only, rel: Path, is_dir: bool) -> bool:
    if only is None:
        return True
    text = rel.as_posix()
    for entry in only:
        if text == entry or text.startswith(entry + "/"):
            return True
        if is_dir and entry.startswith(text + "/"):
            return True
    return False


def _copy_input_tree(source: Path, destination: Path, only=None) -> None:
    """Copy private inputs without following links or accepting a concurrent rewrite.

    ``only`` limits the copy to those repo-relative files/folders (live mode:
    the render inputs, never library/media, .env, .venv or node_modules)."""
    if source.is_symlink() or not source.is_dir():
        raise ReelsError("editor data root is not a regular private directory")
    if destination.exists() or destination.is_symlink():
        raise ReelsError("worker input snapshot already exists")
    destination.mkdir(parents=True, mode=0o700)
    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        source_dir = Path(current)
        rel = source_dir.relative_to(source)
        dirs[:] = sorted(d for d in dirs if d not in ("out", ".cache", "__pycache__")
                         and _allowed_by(only, rel / d, True))
        files = [f for f in files if _allowed_by(only, rel / f, False)]
        for name in dirs + files:
            if (source_dir / name).is_symlink():
                raise ReelsError(f"private renderer input contains a symlink: {(rel / name).as_posix()}")
        target_dir = destination / rel
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in sorted(files):
            original = source_dir / name
            if name == ".DS_Store":
                continue
            if not original.is_file():
                raise ReelsError(f"private renderer input is not a regular file: {original.name}")
            before = _stable_signature(original)
            target = target_dir / name
            with original.open("rb") as src, target.open("xb") as dst:
                opened = os.fstat(src.fileno())
                if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns) != before:
                    raise ReelsError(f"private renderer input changed while opening: {original.name}")
                shutil.copyfileobj(src, dst)
                after_open = os.fstat(src.fileno())
                dst.flush()
                os.fsync(dst.fileno())
            after = _stable_signature(original)
            if after != before or (
                after_open.st_dev, after_open.st_ino, after_open.st_size, after_open.st_mtime_ns
            ) != before[:4] or target.stat().st_size != before[2]:
                raise ReelsError(f"private renderer input changed while copying: {original.name}")
            target.chmod(0o600)


def _apply_snapshot_overrides(snapshot: dict, data_root: Path) -> None:
    """Materialize frozen document-private JSON overrides into the worker clone."""
    merged: dict[str, Any] = {}
    for key in ("files", "privateFiles", "overrides"):
        values = snapshot.get(key)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise ReelsError(f"preview snapshot {key} must be a relative-path JSON object")
        merged.update(values)
    for relative, value in merged.items():
        if not isinstance(relative, str) or "\\" in relative or "\x00" in relative:
            raise ReelsError("preview snapshot contains an invalid private file path")
        parsed = PurePosixPath(relative)
        if parsed.is_absolute() or any(part in ("", ".", "..") for part in parsed.parts):
            raise ReelsError("preview snapshot private file path cannot traverse directories")
        if parsed.suffix != ".json" or parsed.parts[0] in {"out", ".cache"}:
            raise ReelsError("preview snapshot overrides may only replace private JSON inputs")
        target = data_root.joinpath(*parsed.parts)
        if not _inside(target.resolve(strict=False), data_root.resolve()):
            raise ReelsError("preview snapshot override escaped the private data clone")
        if target.is_symlink():
            raise ReelsError("preview snapshot override target cannot be a symlink")
        _atomic_json(target, value)


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with tmp.open("x", encoding="utf-8") as stream:
        os.chmod(tmp, 0o600)
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


def _sbpl_quote(path: Path | str) -> str:
    value = str(path).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{value}"'


def _path_roots(workspace, code_root: Path, job_root: Path, revision_dir: Path,
                 cache_root: Path, bundle_cache: Path) -> tuple[list[Path], list[Path]]:
    reads: set[Path] = {
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path("/System/Library"),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path("/Library/Frameworks"),
        Path("/opt/homebrew"),
        Path("/usr/local"),
        Path("/dev"),
        Path("/private/etc"),
    }
    for package_path in site.getsitepackages():
        path = Path(package_path)
        if path.exists():
            reads.add(path.resolve())
    try:
        import site as site_module

        user_site = Path(site_module.getusersitepackages())
        # The worker does not enable user-site imports; do not grant user-site reads.
        del user_site
    except (AttributeError, OSError):
        pass
    # Never grant an entire checkout: it may contain unrelated editor workspaces,
    # chat/MCP credentials, .env or .git. The worker uses its frozen data clone,
    # not the live private data tree. Include the actual trusted runner/contracts
    # even when workspace.code_root names a different renderer checkout.
    application_roots = {
        Path(code_root).resolve(),
        Path(workspace.code_root).resolve(),
        Path(__file__).resolve().parents[3],
    }
    for application_root in application_roots:
        for relative in ("scripts", "tests", "remotion"):
            root = application_root / relative
            if root.is_dir():
                reads.add(root.resolve())
        # Recorded renderer sources link to read-only shared dependencies.
        dependencies = application_root / "remotion/node_modules"
        if dependencies.is_dir():
            reads.add(dependencies.resolve())
    # Project.abs() and the trusted worker still verify registered identities.
    # Reading a media directory must not expose unregistered sibling originals.
    for record in workspace._originals.values():
        reads.add(Path(record["path"]).resolve())
    # Chromium from Remotion/Puppeteer is read-only and may launch helper binaries.
    for base in (
        Path.home() / "Library/Caches/puppeteer",
        Path.home() / ".cache/puppeteer",
        Path.home() / "Library/Caches/ms-playwright",
    ):
        if base.is_dir() and not base.is_symlink():
            reads.add(base.resolve())
    writes = {
        job_root.resolve(),
        revision_dir.resolve(),
        cache_root.resolve(),
        bundle_cache.resolve(),
    }
    # Remotion keeps its webpack cache in remotion/node_modules/.cache (shared with
    # ./reels render) and clears it when the bundler config changes. It is a build
    # cache, never footage or project data.
    for application_root in application_roots:
        dependencies = application_root / "remotion/node_modules"
        if dependencies.is_dir():
            cache = dependencies.resolve() / ".cache"
            cache.mkdir(exist_ok=True)
            writes.add(cache)
    # The isolated worker must read the job JSON, revision output it is
    # creating, and reusable cache entries as well as write there.
    reads.update(writes)
    return sorted(reads, key=str), sorted(writes, key=str)


_BROWSER_MACH_SERVICES = (
    "com.apple.windowserver.active",
    "com.apple.system.notification_center",
    "com.apple.coreservices.launchservicesd",
    "com.apple.CoreServices.coreservicesd",
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.logd",
    "com.apple.CARenderServer",
    "com.apple.audio.AudioComponentRegistrar",
    "com.apple.audio.AudioConverterService",
    "com.apple.audio.audiohald",
    "com.apple.audio.AudioSession",
    "com.apple.audio.coreaudiod",
    "com.apple.SystemConfiguration.configd",
    "com.apple.SystemConfiguration.DNSConfiguration",
)
_BROWSER_IOKIT_CLIENTS = (
    "IOAudioControlUserClient",
    "IOAudioEngineUserClient",
    "RootDomainUserClient",
)


def _browser_preference_files() -> list[Path]:
    """Only the global/accessibility preference plists Chrome queries at boot."""
    preferences = Path.home() / "Library/Preferences"
    candidates = [
        preferences / ".GlobalPreferences.plist",
        preferences / "com.apple.universalaccess.plist",
        preferences / "com.apple.Accessibility.plist",
        *preferences.joinpath("ByHost").glob(".GlobalPreferences.*.plist"),
    ]
    result = []
    for path in candidates:
        if path.is_file() and not path.is_symlink():
            result.append(path.resolve(strict=True))
    return result


def _profile_text(
    read_roots: list[Path],
    write_roots: list[Path],
    executables: set[Path],
    *,
    browser: bool = False,
    videotoolbox: bool = False,
) -> str:
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow sysctl-read)",
        "(allow process-fork)",
        "(allow file-read-metadata (subpath \"/System/Library\") (subpath \"/usr/lib\") (subpath \"/dev\"))",
        '(allow file-read-metadata (literal "/etc") (literal "/private/etc"))',
        '(allow file-read-data (literal "/etc/hosts") (literal "/private/etc/hosts"))',
        '(allow file-read-data (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))',
        '(allow file-write-data (literal "/dev/null"))',
    ]
    for root in read_roots:
        lines.append(f"(allow file-read* (subpath {_sbpl_quote(root)}))")
        lines.append(f"(allow file-map-executable (subpath {_sbpl_quote(root)}))")
    # macOS/dyld enumerate exact ancestor directories while resolving Python's
    # framework and the shared cache. Literal grants permit reading those
    # directory entries only; unlike subpath grants they expose no child files.
    metadata_ancestors: set[Path] = set()
    for root in read_roots:
        metadata_ancestors.update(root.parents)
    for executable in executables:
        metadata_ancestors.update(executable.parents)
    for ancestor in sorted(metadata_ancestors, key=str):
        lines.append(f"(allow file-read* (literal {_sbpl_quote(ancestor)}))")
    for root in write_roots:
        lines.append(f"(allow file-write* (subpath {_sbpl_quote(root)}))")
    for executable in sorted(executables, key=str):
        lines.append(f"(allow process-exec (literal {_sbpl_quote(executable)}))")
    if videotoolbox:
        # Pixel buffers shared with the codec XPC services; no GPU/device-wide grant.
        lines.extend([
            '(allow iokit-open-user-client (iokit-user-client-class "IOSurfaceRootUserClient"))',
            '(allow mach-lookup (xpc-service-name "com.apple.coremedia.videoencoder"))',
            '(allow mach-lookup (xpc-service-name "com.apple.coremedia.videodecoder"))',
        ])
    if browser:
        # Chrome's renderer processes use a PID-scoped local rendezvous port;
        # signal is limited to this sandbox's own process tree for Stop/cleanup.
        lines.extend([
            '(allow mach-lookup (global-name-prefix "org.chromium.Chromium.MachPortRendezvousServer."))',
            '(allow mach-register (global-name-prefix "org.chromium.Chromium.MachPortRendezvousServer."))',
            "(allow signal (target same-sandbox))",
            "(allow iokit-open-user-client "
            '(iokit-user-client-class "IOAudioControlUserClient") '
            '(iokit-user-client-class "IOAudioEngineUserClient") '
            '(iokit-user-client-class "RootDomainUserClient"))',
        ])
        lines.extend(
            f"(allow mach-lookup (global-name {_sbpl_quote(service)}))"
            for service in _BROWSER_MACH_SERVICES
        )
    # Workers need Remotion's loopback render server, never arbitrary remote hosts.
    lines.extend([
        '(allow network-outbound (remote ip "localhost:*"))',
        '(allow network-inbound (local ip "localhost:*"))',
    ])
    return "\n".join(lines) + "\n"


_BROWSER_CACHE: dict[str, Path] = {}


def _browser_executable(workspace) -> Path | None:
    """The Remotion browser binary. Finding it walks the browser caches (tens of thousands of folders), so the
    answer is kept per code root for the life of the process and looked up again if that file disappears."""
    configured = workspace.project.cfg("render", "browserExecutable")
    if configured:
        return _find_browser(workspace, configured)
    key = str(Path(workspace.code_root).resolve())
    hit = _BROWSER_CACHE.get(key)
    if hit is not None and hit.is_file() and os.access(hit, os.X_OK):
        return hit
    found = _find_browser(workspace, None)
    if found is not None:  # not found is looked up again next time: setup may install it meanwhile
        _BROWSER_CACHE[key] = found
    return found


def _find_browser(workspace, configured) -> Path | None:
    if configured:
        path = Path(str(configured)).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path.resolve()
        raise ReelsError(f"configured Remotion browser is unavailable: {path}")
    local_cache = Path(workspace.code_root) / "remotion/node_modules/.remotion"
    if local_cache.is_dir() and not local_cache.is_symlink():
        local = [
            path for name in ("chrome-headless-shell", "chrome", "Chromium")
            for path in local_cache.rglob(name) if path.is_file() and os.access(path, os.X_OK)
        ]
        if local:
            return max(local, key=lambda path: path.stat().st_mtime_ns).resolve()
    candidates: list[Path] = []
    for root in (
        Path.home() / "Library/Caches/puppeteer",
        Path.home() / ".cache/puppeteer",
        Path.home() / "Library/Caches/ms-playwright",
    ):
        if not root.is_dir() or root.is_symlink():
            continue
        for pattern in ("chrome", "chrome-headless-shell", "Chromium", "Google Chrome for Testing"):
            candidates.extend(path for path in root.rglob(pattern) if path.is_file() and os.access(path, os.X_OK))
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime_ns).resolve()


def _executables(
    workspace,
    code_root: Path,
    node: str | None = None,
    *,
    browser: bool = False,
) -> set[Path]:
    found: set[Path] = set()
    names = ("python", "ffmpeg", "ffprobe", "node", "env", "sysctl") if browser else (
        "python", "ffmpeg", "ffprobe", "node", "env"
    )
    for name in names:
        value = shutil.which(name)
        if value:
            found.add(Path(value))
            found.add(Path(value).resolve())
    # media.py switches to Homebrew's keg-only ffmpeg-full (zimg, accurate HDR colour) when installed.
    from ..media import FULL_FFMPEG_DIRS
    for directory in FULL_FFMPEG_DIRS:
        for name in ("ffmpeg", "ffprobe"):
            item = Path(directory) / name
            if item.is_file():
                found.add(item)
                found.add(item.resolve())
    found.add(Path(sys.executable))
    python_executable = Path(sys.executable).resolve()
    found.add(python_executable)
    python_app = python_executable.parent.parent / "Resources/Python.app/Contents/MacOS/Python"
    if python_app.is_file():
        found.add(python_app.resolve())
    if node:
        found.add(Path(node).resolve())
    for root in {Path(code_root).resolve(), Path(workspace.code_root).resolve()}:
        remotion = root / "remotion"
        for relative in (
            "node_modules/.bin/esbuild",
            "node_modules/.bin/tsc",
            "node_modules/.bin/remotion",
        ):
            item = remotion / relative
            if item.exists():
                found.add(item.resolve())
        # esbuild's JS shim starts its platform-specific native binary.
        for item in remotion.glob("node_modules/@esbuild/*/bin/esbuild"):
            if item.is_file():
                found.add(item.resolve())
    browser = _browser_executable(workspace)
    if browser:
        found.add(browser)
    if browser:
        compositor_dir = Path(code_root) / "remotion/node_modules/@remotion/compositor-darwin-arm64"
        for name in ("remotion", "ffmpeg", "ffprobe"):
            executable = compositor_dir / name
            if executable.is_file():
                found.add(executable.resolve())
    for root in (
        Path.home() / "Library/Caches/puppeteer",
        Path.home() / ".cache/puppeteer",
        Path.home() / "Library/Caches/ms-playwright",
    ):
        if root.is_dir() and not root.is_symlink():
            found.update(_cached_browser_binaries(root))
    return found


_BINARY_CACHE: dict[str, list[Path]] = {}


def _cached_browser_binaries(root: Path) -> list[Path]:
    """Browser binaries under a shared browser cache. The walk is slow and the sandbox profile asks for it on
    every preview job, so it's kept for the process and redone when a remembered binary is gone."""
    key = str(root)
    hit = _BINARY_CACHE.get(key)
    if hit and all(item.is_file() for item in hit):
        return hit
    items = [
        item.resolve()
        for pattern in ("chrome", "chrome-headless-shell", "Chromium", "Google Chrome for Testing")
        for item in root.rglob(pattern)
        if item.is_file() and os.access(item, os.X_OK)
    ]
    if items:  # none found is looked up again next time: a browser may be installed meanwhile
        _BINARY_CACHE[key] = items
    return items


def _safe_env(workspace, job_root: Path, code_root: Path, node: str | None = None) -> dict[str, str]:
    paths = ["/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/usr/local/bin"]
    if node:
        paths.insert(0, str(Path(node).resolve().parent))
    config = workspace.project.config
    for key in ("ffmpeg", "ffprobe"):
        value = config.get("tools", {}).get(key)
        if value:
            paths.insert(0, str(Path(value).expanduser().resolve().parent))
    env = {
        "PATH": os.pathsep.join(dict.fromkeys(paths)),
        "HOME": str(job_root / "home"),
        "TMPDIR": str(job_root / "tmp"),
        "PYTHONPATH": str(Path(code_root) / "scripts"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        # Browserslist otherwise walks to existing package/config files in the
        # person's home directory. Pin both targets and lookup scope instead
        # of granting that unrelated data to Node.
        "BROWSERSLIST": "defaults",
        "BROWSERSLIST_ROOT_PATH": str(Path(code_root) / "remotion"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    browser = _browser_executable(workspace)
    if browser:
        env["REELS_BROWSER"] = str(browser)
    Path(env["HOME"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    Path(env["TMPDIR"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return env


def _run(context, args: list[str], *, cwd: Path, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    if context is not None:
        return context.run(args, cwd=cwd, env=env, timeout=timeout)
    proc = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode:
        raise ReelsError(f"Sandboxed renderer worker failed ({proc.returncode}):\n{(proc.stdout + proc.stderr)[-12000:]}")
    return proc


def _worker_command(code_root: Path, *arguments: str) -> list[str]:
    """The runner is trusted application code, not a candidate source module.

    Only renderer imports resolve against the recorded source revision. Fixes
    to validation/protocol boundaries must not depend on an old copied runner.
    """
    trusted = Path(__file__).with_name("worker.py").resolve(strict=True)
    bootstrap = (
        "import importlib.util,sys;from pathlib import Path;"
        "sys.path.insert(0,sys.argv[1]);"
        "import reels.editor;reels.editor.__path__=[str(Path(sys.argv[2]).parent)];"
        "s=importlib.util.spec_from_file_location('reels.editor._trusted_worker',sys.argv[2]);"
        "m=importlib.util.module_from_spec(s);sys.modules[s.name]=m;"
        "s.loader.exec_module(m);raise SystemExit(m.main(sys.argv[3:]))"
    )
    return [sys.executable, "-c", bootstrap, str(code_root / "scripts"), str(trusted), *map(str, arguments)]


def _probe_boundary(context, sandbox: str, code_root: Path, job_root: Path,
                    profile: Path, env: dict[str, str], protected: Path) -> None:
    code_root = Path(code_root).resolve(strict=True)
    job_root = Path(job_root).resolve(strict=True)
    profile = Path(profile).resolve(strict=True)
    protected = Path(protected).resolve(strict=True)
    probe_root = job_root / "sandbox-probe"
    writable = probe_root / "writable"
    writable.mkdir(parents=True, mode=0o700, exist_ok=True)
    if protected.is_symlink() or not protected.is_file():
        raise ReelsError("isolated original-write probe fixture is missing")
    protected_before = _stable_signature(protected)
    content_before = protected.read_bytes()
    probe_json = probe_root / "probe.json"
    _atomic_json(probe_json, {"writable": str(writable), "protected": str(protected)})
    probe_args = [sandbox, "-f", str(profile), *_worker_command(code_root, "--sandbox-probe", str(probe_json))]
    _run(context, probe_args, cwd=code_root, env=env, timeout=30)
    if _stable_signature(protected) != protected_before or protected.read_bytes() != content_before:
        raise ReelsError("original-write sandbox probe failed; candidate source activation is disabled.")


class PreviewManager:
    """Prepares content-addressed staged media without calling render_plan()."""

    def __init__(self, workspace):
        self.workspace = workspace
        self.worker_root = Path(workspace.root) / ".cache/editor/renderer-workers"
        self.preview_root = Path(workspace.root) / "preview"
        self.cache_root = self.preview_root / "cache/staged-media"
        self.bundle_cache = self.preview_root / "cache/bundles"
        for directory in (self.worker_root, self.cache_root, self.bundle_cache, self.preview_root / "revisions"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _copy_inputs(self, snapshot: dict, job_root: Path) -> Path:
        expected = str((snapshot.get("revisions") or {}).get("library", ""))
        refresh_revision = getattr(self.workspace, "refresh_library_revision", None)
        fresh_revision = refresh_revision if callable(refresh_revision) else self.workspace.library_revision
        before = str(fresh_revision())
        if expected and before != expected:
            raise ReelsError("preview snapshot library revision is stale; prepare the latest snapshot")
        data = job_root / "data"
        from .workspace import CLONE_EXTRA_DIRS, INPUT_DIRS, INPUT_FILES

        only = (*INPUT_DIRS, *CLONE_EXTRA_DIRS, *INPUT_FILES) if getattr(self.workspace, "live_mode", False) else None
        _copy_input_tree(Path(self.workspace.data_root), data, only)
        _apply_snapshot_overrides(snapshot, data)
        after = str(fresh_revision())
        if before != after or (expected and after != expected):
            raise ReelsError("library metadata changed while its preview snapshot was frozen")
        return data

    def _renderer_cache_roots(self, revision: str) -> tuple[Path, Path]:
        """Give one renderer only its own caches, retaining all prior URL targets."""
        if not isinstance(revision, str) or not revision:
            raise ReelsError("renderer job is missing its exact source revision")
        namespace = "renderer-" + hashlib.sha256(revision.encode("utf-8")).hexdigest()
        roots = []
        for base in (self.cache_root, self.bundle_cache):
            root = base / namespace
            if root.is_symlink() or root.resolve().parent != base.resolve():
                raise ReelsError("renderer cache namespace cannot be a symlink")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            roots.append(root.resolve(strict=True))
        return roots[0], roots[1]

    def _run_worker(self, job: dict, *, context=None, code_root: Path | None = None,
                    timeout: float = 900) -> dict:
        revision = (job.get("revisions") or {}).get("renderer")
        if not isinstance(revision, str) or not revision:
            raise ReelsError("renderer job is missing its exact source revision")
        if code_root is None:
            from .renderer import RendererManager

            code_root = RendererManager(self.workspace).resolve_renderer(revision)
        code_root = Path(code_root).resolve(strict=True)
        job_root = Path(job["jobRoot"]).resolve(strict=True)
        revision_dir = Path(job["revisionDir"]).resolve()
        result_path = Path(job["resultPath"]).resolve()
        job["jobRoot"] = str(job_root)
        job["jobPath"] = str(Path(job["jobPath"]).resolve())
        job["revisionDir"] = str(revision_dir)
        job["resultPath"] = str(result_path)
        for field in ("dataRoot", "exportRoot", "workspaceRoot", "mediaRoot"):
            if job.get(field):
                job[field] = str(Path(job[field]).resolve())
        env = _safe_env(self.workspace, job_root, code_root, node=shutil.which("node"))
        job["env"] = env
        job["node"] = shutil.which("node") or ""
        job["codeRoot"] = str(code_root)
        cache_root, bundle_cache = self._renderer_cache_roots(revision)
        job["cacheRoot"] = str(cache_root)
        job["bundleCache"] = str(bundle_cache)
        job["workspaceRoot"] = str(Path(self.workspace.root).resolve())
        if job.get("action") not in ("source_proxy", "sphere_proxy"):
            job["runtimeRevision"] = hashlib.sha256(Path(__file__).with_name("worker.py").read_bytes()).hexdigest()
        job["trustedTestsRoot"] = str(Path(__file__).resolve().parents[3] / "tests")
        _atomic_json(Path(job["jobPath"]), job)

        if platform.system() != "Darwin":
            raise ReelsError("renderer sandbox is unavailable on this platform; preview/export workers fail closed.")
        sandbox = shutil.which("sandbox-exec")
        if not sandbox:
            raise ReelsError("macOS sandbox-exec is unavailable; renderer workers fail closed.")

        fixture_root = self.worker_root / f"original-write-fixture-{uuid.uuid4().hex}"
        protected = fixture_root / "library/media/probe-original.bin"
        protected.parent.mkdir(parents=True, mode=0o700)
        protected.write_bytes(b"isolated-original-write-denial-probe")
        protected.chmod(0o600)
        # prepare() may discover alpha media and switch Interactive to Exact
        # inside the isolated worker. The broker must not import candidate code
        # to predict that decision.
        browser_worker = job.get("mode") == "exact" or job.get("action") in ("prepare", "export", "validate")
        reads, writes = _path_roots(self.workspace, code_root, job_root, revision_dir, cache_root, bundle_cache)
        if job.get("action") in ("source_proxy", "sphere_proxy"):
            from .proxy_cache import budget_root

            reservations = budget_root(Path(self.workspace.root).resolve())
            reads.append(reservations)
            writes.append(reservations)
        reads.append(fixture_root.resolve())
        if browser_worker:
            reads.extend(_browser_preference_files())
        executables = _executables(self.workspace, code_root, node=job["node"], browser=browser_worker)
        profile = job_root / "worker.sb"
        profile_text = _profile_text(reads, writes, executables, browser=browser_worker,
                                     videotoolbox=job.get("action") in ("source_proxy", "sphere_proxy"))
        # Candidate source/tests are inputs even when nested inside a writable
        # job directory. Candidate Python must not rewrite trusted contracts or
        # the runner while those contracts are executing.
        profile_text += f"\n(deny file-write* (subpath {_sbpl_quote(code_root)}))\n"
        profile.write_text(profile_text, encoding="utf-8")
        profile.chmod(0o600)

        # This target is outside every write root in the same profile used by
        # the renderer. The profile can write its revision-owned work area but
        # cannot modify this isolated original-media fixture.
        try:
            _probe_boundary(context, sandbox, code_root, job_root, profile, env, protected)
            worker_args = [sandbox, "-f", str(profile), *_worker_command(code_root, str(job["jobPath"]))]
            _run(context, worker_args, cwd=code_root, env=env, timeout=timeout)
            if result_path.is_symlink() or not result_path.is_file():
                raise ReelsError("renderer worker exited without an immutable result manifest")
            try:
                result = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ReelsError(f"renderer worker returned an invalid manifest: {exc}") from exc
            if "error" in result:
                raise ReelsError(result["error"])
            return result
        finally:
            shutil.rmtree(fixture_root, ignore_errors=True)

    def verify_sandbox_boundary(self, context=None) -> dict:
        """Run the mandatory original-write-denial probe against an isolated fixture."""
        if platform.system() != "Darwin":
            raise ReelsError("renderer sandbox is unavailable on this platform; renderer workers fail closed.")
        sandbox = shutil.which("sandbox-exec")
        if not sandbox:
            raise ReelsError("macOS sandbox-exec is unavailable; renderer workers fail closed.")
        job_root = self.worker_root / f"probe-{uuid.uuid4().hex}"
        job_root.mkdir(parents=True, mode=0o700)
        code_root = Path(self.workspace.code_root).resolve(strict=True)
        env = _safe_env(self.workspace, job_root, code_root, node=shutil.which("node"))
        fixture_root = self.worker_root / f"original-write-fixture-{uuid.uuid4().hex}"
        protected = fixture_root / "library/media/probe-original.bin"
        fixture_root.mkdir(parents=True, mode=0o700)
        protected.parent.mkdir(parents=True, mode=0o700)
        protected.write_bytes(b"isolated-original-write-denial-probe")
        reads, writes = _path_roots(
            self.workspace, code_root, job_root, job_root / "output",
            job_root / "cache/staged-media", job_root / "cache/bundles",
        )
        reads.append(fixture_root.resolve())
        profile = job_root / "worker.sb"
        profile.write_text(_profile_text(reads, writes, _executables(self.workspace, code_root, shutil.which("node"))), encoding="utf-8")
        profile.chmod(0o600)
        try:
            _probe_boundary(context, sandbox, code_root, job_root, profile, env, protected)
            return {"status": "verified", "fixture": "isolated-original-write-denial"}
        finally:
            shutil.rmtree(job_root, ignore_errors=True)
            shutil.rmtree(fixture_root, ignore_errors=True)

    def prepare(self, snapshot: dict, mode: str = "interactive", context=None, *, code_root: Path | None = None) -> dict:
        if mode not in ("interactive", "exact"):
            raise ReelsError("preview mode must be interactive or exact")
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("plan"), dict):
            raise ReelsError("preview requires the immutable document snapshot with its raw plan")
        revisions = snapshot.get("revisions")
        if not isinstance(revisions, dict) or not all(revisions.get(key) is not None for key in ("document", "library", "renderer")):
            raise ReelsError("preview requires document, library, and renderer revisions")
        empty = empty_preview(snapshot, mode)
        if empty is not None:
            return empty
        if code_root is None:
            from .renderer import RendererManager

            code_root = RendererManager(self.workspace).resolve_renderer(str(revisions["renderer"]))
        job_root = self.worker_root / uuid.uuid4().hex
        job_root.mkdir(parents=True, mode=0o700)
        try:
            data_root = self._copy_inputs(snapshot, job_root)
            revision_hash = uuid.uuid5(
                uuid.NAMESPACE_URL,
                "|".join(str(revisions[key]) for key in ("document", "library", "renderer")) + "|" + mode,
            ).hex[:20]
            revision_dir = self.preview_root / "revisions" / f"{revision_hash}-{uuid.uuid4().hex[:8]}"
            job_path = job_root / "job.json"
            result_path = job_root / "result.json"
            job = {
                "action": "prepare",
                "jobPath": str(job_path),
                "jobRoot": str(job_root),
                "dataRoot": str(data_root),
                "revisionDir": str(revision_dir),
                "resultPath": str(result_path),
                "mediaRoot": str(self.workspace.project.media_dir.resolve()),
                "originals": copy.deepcopy(self.workspace._originals),
                "plan": copy.deepcopy(snapshot["plan"]),
                "revisions": dict(revisions),
                "mode": mode,
            }
            result = self._run_worker(job, context=context, code_root=code_root, timeout=1200 if mode == "exact" else 900)
            self._validate_preview_manifest(result)
            # The result is revision-tagged and contains only private immutable paths.
            return result
        except Exception:
            if "revision_dir" in locals() and revision_dir.exists() and _inside(revision_dir.resolve(), self.preview_root.resolve()):
                shutil.rmtree(revision_dir, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(job_root, ignore_errors=True)

    def _validate_preview_manifest(self, result: dict) -> None:
        private_root = self.preview_root.resolve()
        for field in ("bundleDir", "staticDir", "propsPath", "exactPath", "attachedAudioPreviewPath"):
            value = result.get(field)
            if value is None:
                continue
            path = Path(str(value)).resolve(strict=True)
            if not _inside(path, private_root):
                raise ReelsError(f"preview manifest {field} is outside the private editor workspace")
        static = Path(result["staticDir"]).resolve(strict=True)
        if not _inside(Path(result["propsPath"]).resolve(strict=True), static):
            raise ReelsError("preview props are outside revision-owned staged media")

    def export(self, snapshot: dict, context=None, *, code_root: Path | None = None) -> dict:
        """Render a frozen document through render_plan() in a private data clone.

        The real renderer's post/lineage/usage writes land only in this export's
        private snapshot. ``usageUpdates`` is field-scoped data for the
        transaction facade; this method never merges it into live workspace
        metadata or production.
        """
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get("plan"), dict):
            raise ReelsError("export requires the immutable document snapshot with its raw plan")
        revisions = snapshot.get("revisions")
        if not isinstance(revisions, dict) or not all(revisions.get(key) is not None for key in ("document", "library", "renderer")):
            raise ReelsError("export requires document, library, and renderer revisions")
        if code_root is None:
            from .renderer import RendererManager

            code_root = RendererManager(self.workspace).resolve_renderer(str(revisions["renderer"]))
        export_id = uuid.uuid4().hex
        # Private scratch; the finished files are published to out/<slug>/ below.
        export_root = Path(self.workspace.root) / "exports" / export_id
        export_root.mkdir(parents=True, mode=0o700, exist_ok=False)
        job_root = self.worker_root / f"export-{export_id}"
        job_root.mkdir(parents=True, mode=0o700, exist_ok=False)
        try:
            data_root = self._copy_inputs(snapshot, export_root)
            self._seed_previous_post(snapshot, data_root)
            job_path = job_root / "job.json"
            result_path = job_root / "result.json"
            job = {
                "action": "export",
                "jobPath": str(job_path),
                "jobRoot": str(job_root),
                "dataRoot": str(data_root),
                "exportRoot": str(export_root),
                "revisionDir": str(export_root),
                "resultPath": str(result_path),
                "mediaRoot": str(self.workspace.project.media_dir.resolve()),
                "originals": copy.deepcopy(self.workspace._originals),
                "plan": copy.deepcopy(snapshot["plan"]),
                "revisions": dict(revisions),
            }
            result = self._run_worker(job, context=context, code_root=code_root, timeout=1800)
            output_dir = Path(result["outputDir"]).resolve(strict=True)
            root = export_root.resolve(strict=True)
            reel_path = Path(result["reelPath"]).resolve(strict=True)
            if not _inside(output_dir, root) or not _inside(reel_path, output_dir) or not reel_path.is_file():
                raise ReelsError("export worker returned an output outside its revision-owned private snapshot")
            preview_path = result.get("previewVideoPath")
            if preview_path is not None:
                preview_file = Path(preview_path).resolve(strict=True)
                if not _inside(preview_file, output_dir) or not preview_file.is_file():
                    raise ReelsError("export worker returned a review movie outside its private output directory")
            result["exportId"] = export_id
            result["exportRoot"] = str(export_root)
            result["status"] = "rendered-private"
            if getattr(self.workspace, "live_mode", False):
                result = self._publish_to_out(result, output_dir)
            return result
        except Exception:
            shutil.rmtree(export_root, ignore_errors=True)
            raise
        finally:
            shutil.rmtree(job_root, ignore_errors=True)

    def _seed_previous_post(self, snapshot: dict, data_root: Path) -> None:
        """Give the worker the existing out/<slug>/post.json so history/Metricool ids carry over."""
        slug = (snapshot.get("plan") or {}).get("slug")
        if not isinstance(slug, str) or not slug or "/" in slug or "\\" in slug:
            return
        previous = Path(self.workspace.project.out_dir) / slug / "post.json"
        if previous.is_file() and not previous.is_symlink():
            target = data_root / "out" / slug / "post.json"
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(previous, target)

    def _publish_to_out(self, result: dict, output_dir: Path) -> dict:
        """Copy the finished render into the real out/<slug>/ like ./reels render does.

        Each file is staged beside its destination and swapped in with an atomic
        replace. Nothing outside out/<slug>/ is touched and no existing file is
        deleted except a superseded review movie in that same folder.
        """
        slug = str(result["slug"])
        destination = (Path(self.workspace.project.out_dir) / slug)
        out_root = Path(self.workspace.project.out_dir).resolve()
        destination.mkdir(parents=True, exist_ok=True)
        if not _inside(destination.resolve(), out_root):
            raise ReelsError("export destination escaped out/")
        names = [path.name for path in output_dir.iterdir() if path.is_file() and not path.is_symlink()]
        for name in sorted(names):
            if name.startswith("."):
                continue
            temporary = destination / f".{name}.{uuid.uuid4().hex}.tmp"
            try:
                shutil.copyfile(output_dir / name, temporary)
                os.replace(temporary, destination / name)
            finally:
                if temporary.exists():
                    temporary.unlink()
        if "preview.mp4" not in names and (destination / "preview.mp4").is_file():
            (destination / "preview.mp4").unlink()
        published = dict(result)
        published["outputDir"] = str(destination)
        published["reelPath"] = str(destination / "reel.mp4")
        published["previewVideoPath"] = str(destination / "preview.mp4") if "preview.mp4" in names else None
        published["publishedTo"] = f"out/{slug}"
        shutil.rmtree(Path(result["exportRoot"]), ignore_errors=True)
        return published

    def merge_usage_fields(
        self,
        receipt: dict,
        expected_revisions: dict,
        current_revisions: dict,
        commit_private_updates: Callable[[dict, dict], Any],
    ) -> Any:
        """Hand field-only usage deltas to the transaction facade after freshness checks.

        The callback owns the journal/atomic commit. No export worker or renderer
        writes these deltas to the live workspace on its own.
        """
        if receipt.get("sideEffects") != "private-snapshot-only":
            raise ReelsError("export receipt is missing the private-snapshot side-effect marker")
        export_root = Path(str(receipt.get("exportRoot", ""))).resolve()
        private_roots = ((Path(self.workspace.project.out_dir) / "exports").resolve(),
                         (Path(self.workspace.root) / "exports").resolve())
        if not any(_inside(export_root, root) for root in private_roots):
            raise ReelsError("export receipt is not owned by this private editor workspace")
        if receipt.get("revisions") != expected_revisions or current_revisions != expected_revisions:
            raise ReelsError("export revisions are stale; usage updates were not merged")
        updates = receipt.get("usageUpdates")
        if not isinstance(updates, dict) or set(updates) != {"assets", "music"}:
            raise ReelsError("export receipt has malformed usage field updates")
        for category, values in updates.items():
            if not isinstance(values, dict):
                raise ReelsError("export usage delta is malformed")
            if any(not isinstance(key, str) or not isinstance(value, list) for key, value in values.items()):
                raise ReelsError("export usage delta contains an invalid field value")
        return commit_private_updates(updates, expected_revisions)

    def prepare_sphere(self, asset_id: str, start: float, end: float, stabilize=None, context=None) -> dict:
        """An equirectangular proxy of [start, end] of a 360 clip for the live 360 viewer,
        with the stabilization rotations for `stabilize` (a plan's stabilize value)."""
        from .proxy import SPHERE_PROXY_MAX, sphere_cache_key

        from ..util import quick_hash, read_json

        asset_path = self.workspace.project.assets_dir / f"{asset_id}.json"
        if asset_path.is_symlink() or not asset_path.is_file():
            raise ReelsError("360 asset is not present in the private editor library")
        asset = read_json(asset_path)
        if asset.get("id") != asset_id:
            raise ReelsError("360 asset identity does not match its private library record")
        registered = self.workspace._originals.get(str(asset.get("media") or ""))
        if not registered:
            raise ReelsError("360 media is not registered in this editor workspace")
        path = Path(registered["path"])
        if not path.is_file() or path.is_symlink():
            return {"status": "offline", "assetId": asset_id, "reason": "The registered original is offline."}
        actual = _stable_signature(path)
        identity = registered.get("identity") or {
            "dev": actual[0], "ino": actual[1], "size": actual[2],
            "mtime_ns": actual[3], "ctime_ns": actual[4], "quick_hash": quick_hash(path),
        }
        if actual != (identity.get("dev"), identity.get("ino"), identity.get("size"), identity.get("mtime_ns"), identity.get("ctime_ns")):
            return {"status": "offline", "assetId": asset_id, "reason": "The registered original changed identity."}
        probe = asset.get("probe") or {}
        if not isinstance(probe.get("sphere"), dict) or asset.get("kind") != "video":
            return {"status": "unsupported", "assetId": asset_id, "reason": "Only 360 video has a live 360 view."}
        # proxies cover whole 5 s blocks around the clip, so small trims reuse them
        duration = float(probe.get("duration") or end)
        lo = max(0.0, math.floor(max(0.0, float(start) - 1.0) / 5.0) * 5.0)
        hi = min(duration, math.ceil((float(end) + 1.0) / 5.0) * 5.0)
        if hi - lo > SPHERE_PROXY_MAX:
            lo, hi = max(0.0, float(start)), min(duration, float(end))
        from .renderer import RendererManager

        renderer_manager = RendererManager(self.workspace)
        renderer_revision = renderer_manager._active_revision()
        key = sphere_cache_key(asset_id, identity, probe)
        job_root = self.worker_root / f"sphere-{uuid.uuid4().hex}"
        job_root.mkdir(parents=True, mode=0o700)
        proxy_root = self.preview_root / "spheres" / key
        proxy_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # the sandbox may write only inside proxy_root
        revisions = {"document": "source", "library": str(self.workspace.library_revision()), "renderer": renderer_revision}
        try:
            result = self._run_worker(
                {
                    "action": "sphere_proxy",
                    "jobPath": str(job_root / "job.json"),
                    "jobRoot": str(job_root),
                    "revisionDir": str(proxy_root),
                    "resultPath": str(job_root / "result.json"),
                    "sourcePath": str(path),
                    "sourceIdentity": identity,
                    "assetId": asset_id,
                    "probe": copy.deepcopy(probe),
                    "start": lo,
                    "end": hi,
                    "requestedStart": max(0.0, float(start)),
                    "requestedEnd": min(duration, float(end)),
                    "stabilize": copy.deepcopy(stabilize),
                    "mediaRoot": str(self.workspace.project.media_dir.resolve()),
                    "workspaceRoot": str(Path(self.workspace.root).resolve()),
                    "revisions": revisions,
                },
                context=context,
                code_root=renderer_manager.resolve_renderer(renderer_revision),
                timeout=1800,
            )
        finally:
            shutil.rmtree(job_root, ignore_errors=True)
        if result.get("status", "ready") != "ready":
            return {"status": result.get("status", "unsupported"), "assetId": asset_id,
                    "reason": result.get("reason") or "The 360 proxy could not be prepared."}
        proxy = Path(result.get("path", "")).resolve(strict=True)
        if not _inside(proxy, self.preview_root.resolve()):
            raise ReelsError("360 proxy escaped the private preview workspace")
        return {**result, "status": "ready", "assetId": asset_id, "path": str(proxy)}

    def prepare_source(self, asset_id: str, view: dict | None = None, context=None) -> dict:
        """Return a playable original or a private view-specific proxy."""
        from .proxy import source_cache_key

        from ..util import read_json, quick_hash

        asset_path = self.workspace.project.assets_dir / f"{asset_id}.json"
        if asset_path.is_symlink() or not asset_path.is_file():
            raise ReelsError("source asset is not present in the private editor library")
        asset = read_json(asset_path)
        if asset.get("id") != asset_id:
            raise ReelsError("source asset identity does not match its private library record")
        identifier = str(asset.get("media") or "")
        registered = self.workspace._originals.get(identifier)
        if not registered:
            raise ReelsError("source media is not registered in this editor workspace")
        path = Path(registered["path"])
        if not path.is_file() or path.is_symlink():
            return {"status": "offline", "assetId": asset_id, "reason": "The registered original is offline."}
        identity = registered.get("identity")
        actual = _stable_signature(path)
        if identity and (
            actual != (identity.get("dev"), identity.get("ino"), identity.get("size"), identity.get("mtime_ns"), identity.get("ctime_ns"))
            or quick_hash(path) != identity.get("quick_hash")
        ):
            return {"status": "offline", "assetId": asset_id, "reason": "The registered original changed identity."}
        probe = asset.get("probe") or {}
        source_kind = str(asset.get("kind") or "")
        if source_kind not in {"video", "photo"}:
            return {"status": "unsupported", "assetId": asset_id, "reason": "The registered asset has an unsupported media kind."}
        if source_proxy_reason(asset, path):
            from .renderer import RendererManager

            renderer_manager = RendererManager(self.workspace)
            identity_value = identity or {
                "dev": actual[0], "ino": actual[1], "size": actual[2],
                "mtime_ns": actual[3], "ctime_ns": actual[4], "quick_hash": quick_hash(path),
            }
            view_value = copy.deepcopy(view or {})
            renderer_revision = renderer_manager._active_revision()
            proxy_id = source_cache_key(asset, identity_value, view_value)
            job_root = self.worker_root / f"source-{uuid.uuid4().hex}"
            job_root.mkdir(parents=True, mode=0o700)
            proxy_root = self.preview_root / "sources" / proxy_id
            proxy_root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)  # the sandbox may write only inside proxy_root
            revisions = {
                "document": "source",
                "library": str(self.workspace.library_revision()),
                "renderer": renderer_revision,
            }
            try:
                result = self._run_worker(
                    {
                        "action": "source_proxy",
                        "jobPath": str(job_root / "job.json"),
                        "jobRoot": str(job_root),
                        "revisionDir": str(proxy_root),
                        "resultPath": str(job_root / "result.json"),
                        "sourcePath": str(path),
                        "sourceIdentity": identity_value,
                        "assetId": asset_id,
                        "sourceKind": source_kind,
                        "probe": copy.deepcopy(probe),
                        "view": view_value,
                        "angles": copy.deepcopy((asset.get("tags") or {}).get("angles") or {}),
                        "mediaRoot": str(self.workspace.project.media_dir.resolve()),
                        "workspaceRoot": str(Path(self.workspace.root).resolve()),
                        "revisions": revisions,
                    },
                    context=context,
                    code_root=renderer_manager.resolve_renderer(revisions["renderer"]),
                    timeout=1800,
                )
            finally:
                shutil.rmtree(job_root, ignore_errors=True)
            if _stable_signature(path) != actual or quick_hash(path) != identity_value["quick_hash"]:
                return {"status": "offline", "assetId": asset_id, "reason": "The registered original changed during proxy preparation."}
            if result.get("status", "ready") != "ready":
                return {
                    "status": result.get("status", "unsupported"),
                    "assetId": asset_id,
                    "reason": result.get("reason") or "A playable source proxy could not be prepared.",
                }
            proxy = Path(result.get("path", "")).resolve(strict=True)
            if not _inside(proxy, self.preview_root.resolve()):
                raise ReelsError("source proxy escaped the private preview workspace")
            return {
                "status": "ready",
                "assetId": asset_id,
                "path": str(proxy),
                "mediaPath": str(proxy),
                "identity": identity_value,
                "view": view_value,
                "layout": result.get("layout") or "single",
                "sourceKind": result.get("sourceKind") or ("image" if source_kind == "photo" else "video"),
                "proxy": True,
                "reused": bool(result.get("reused", False)),
            }
        # Standard AVFoundation-compatible files remain original registered paths.
        if _stable_signature(path) != actual or (identity and quick_hash(path) != identity.get("quick_hash")):
            return {"status": "offline", "assetId": asset_id, "reason": "The registered original changed during source preparation."}
        return {
            "status": "ready",
            "assetId": asset_id,
            "path": str(path),
            "mediaPath": str(path),
            "identity": identity,
            "view": view,
            "sourceKind": "image" if source_kind == "photo" else source_kind,
            "proxy": False,
            "reused": False,
        }
