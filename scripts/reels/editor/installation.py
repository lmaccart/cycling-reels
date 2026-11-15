"""Install/launch the in-repository app with an independent, versioned runtime.

Only source is copied from the main checkout. Dependencies, build products,
private data and outputs live outside it; old runtimes/workspaces are retained.
This module intentionally needs only Python's standard library.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import time
import uuid


RUNTIME_MARKER = ".reels-editor-runtime.json"
RUNTIME_KIND = "cycling-reels-editor-runtime"
_SKIP = {".git", ".cache", ".venv", "node_modules", "dist", "__pycache__", "xcuserdata"}
_CODE_SUFFIXES = {".py", ".swift", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".html", ".json"}
_TOP_FILES = ("reels", "setup.sh", "requirements.txt", "config.example.json")


class InstallationError(RuntimeError):
    pass


def _within(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def _plain_path(path: Path) -> Path:
    supplied = path.expanduser().absolute()
    if ".." in supplied.parts:
        raise InstallationError(f"Use a normalized path without '..': {supplied}")
    if any(part.is_symlink() for part in (supplied, *supplied.parents)):
        raise InstallationError(f"Editor installation paths cannot contain symlinks: {supplied}")
    return supplied.resolve()


def _source_files(repository: Path, *, native: bool = False) -> list[Path]:
    folders = ("editor/ReelsEditor", "editor/ReelsEditorTests", "editor/ReelsEditor.xcodeproj") if native else (
        "scripts", "tests", "remotion",
    )
    files = []
    for relative in folders:
        folder = repository / relative
        if folder.is_symlink() or not folder.is_dir():
            raise InstallationError(f"Required editor source is unavailable: {folder}")
        for current, directories, names in os.walk(folder, followlinks=False):
            base = Path(current)
            directories[:] = sorted(name for name in directories if name not in _SKIP and not name.startswith("."))
            for name in directories:
                if (base / name).is_symlink():
                    raise InstallationError(f"Editor source cannot follow a symlink: {base / name}")
            for name in sorted(names):
                path = base / name
                include = path.suffix in _CODE_SUFFIXES or (native and path.suffix in {".pbxproj", ".xcscheme", ".xcworkspacedata"})
                if name.startswith(".") or not include:
                    continue
                if path.is_symlink() or not path.is_file():
                    raise InstallationError(f"Editor source is not a regular file: {path}")
                files.append(path)
    if not native:
        for name in _TOP_FILES:
            path = repository / name
            if path.is_symlink() or not path.is_file():
                raise InstallationError(f"Required editor source is unavailable: {path}")
            files.append(path)
    return sorted(files)


def source_digest(repository: Path, *, native: bool = False) -> str:
    digest = hashlib.sha256()
    for path in _source_files(repository, native=native):
        digest.update(path.relative_to(repository).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(value, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise InstallationError(f"Missing regular editor installation record: {path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise InstallationError(f"Invalid editor installation record: {path}") from exc
    if not isinstance(value, dict):
        raise InstallationError(f"Invalid editor installation record: {path}")
    return value


def support_root(repository: Path) -> Path:
    value = os.environ.get("CYCLING_EDITOR_HOME")
    root = _plain_path(Path(value) if value else Path.home() / "Library/Application Support/CyclingReelsEditor")
    if _within(root, repository) or _within(repository, root):
        raise InstallationError("Editor support files must be outside the live checkout, not inside or above it.")
    return root


def _project_root(repository: Path, support: Path) -> Path:
    key = hashlib.sha256(str(repository).encode()).hexdigest()[:12]
    return support / "projects" / f"{repository.name}-{key}"


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    for name in (
        "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "PIP_TARGET", "PIP_PREFIX", "PIP_USER", "NODE_PATH",
        "npm_config_prefix", "NPM_CONFIG_PREFIX", "npm_config_global", "NPM_CONFIG_GLOBAL",
        "NPM_CONFIG_USERCONFIG",
    ):
        env.pop(name, None)
    env.update(REELS_EDITOR_RUNTIME="1", PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1",
               PIP_REQUIRE_VIRTUALENV="1", PIP_CONFIG_FILE=os.devnull, npm_config_userconfig=os.devnull)
    return env


@contextmanager
def _setup_lock(project: Path):
    project.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = project / "setup.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallationError("Another editor setup/refresh is already running.") from exc
        yield
    finally:
        os.close(fd)


def copy_runtime(repository: Path, destination: Path, expected: str) -> None:
    """Copy a new code-only runtime, verifying the source before and after."""
    if source_digest(repository) != expected:
        raise InstallationError("Editor source changed before installation; retry setup.")
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    for path in _source_files(repository):
        target = destination / path.relative_to(repository)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with path.open("rb") as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
        target.chmod(0o700 if path.name in {"reels", "setup.sh"} else 0o600)
    if source_digest(repository) != expected or source_digest(destination) != expected:
        raise InstallationError("Editor source changed while copying; incomplete runtime retained for inspection.")
    _write_json(destination / RUNTIME_MARKER, {
        "version": 1, "kind": RUNTIME_KIND, "sourceRoot": str(repository),
        "codeDigest": expected, "status": "preparing",
    })


def _assert_runtime(runtime: Path, repository: Path, digest: str, *, ready: bool = True) -> dict:
    runtime = _plain_path(runtime)
    if _within(runtime, repository):
        raise InstallationError("An editor runtime cannot use the live checkout.")
    marker = _read_json(runtime / RUNTIME_MARKER)
    if (marker.get("version") != 1 or marker.get("kind") != RUNTIME_KIND
            or marker.get("sourceRoot") != str(repository) or marker.get("codeDigest") != digest
            or marker.get("status") not in ({"ready"} if ready else {"ready", "preparing"})):
        raise InstallationError("This editor runtime is not the expected installed source version.")
    if source_digest(runtime) != digest:
        raise InstallationError("Installed runtime source changed; it was retained rather than overwritten.")
    for relative in (".venv", "remotion/node_modules"):
        if (runtime / relative).is_symlink():
            raise InstallationError("Editor dependencies must be independent directories, not symlinks.")
    if ready and not _dependencies_ready(runtime):
        raise InstallationError("Editor dependencies or preview bundle are missing. Run ./editor/reels-editor setup.")
    return marker


def _dependencies_ready(runtime: Path) -> bool:
    return (
        os.access(runtime / ".venv/bin/python", os.X_OK)
        and os.access(runtime / "remotion/node_modules/.bin/remotion", os.X_OK)
        and (runtime / "remotion/dist/editor-preview/index.html").is_file()
    )


def _run(command: list[str | Path], *, cwd: Path) -> None:
    subprocess.run([str(value) for value in command], cwd=cwd, env=_environment(), check=True)


def setup(repository: Path, support: Path, workspace: Path | None = None) -> dict:
    if platform.system() != "Darwin":
        raise InstallationError("The native editor requires macOS.")
    for name in ("xcodebuild", "sandbox-exec", "ffmpeg", "ffprobe", "node", "npm"):
        if not shutil.which(name):
            raise InstallationError(f"Missing {name}; install the system prerequisite, then retry setup.")
    project = _project_root(repository, support)
    with _setup_lock(project):
        install_path = project / "install.json"
        previous = _read_json(install_path) if install_path.exists() else {}
        digest = source_digest(repository)
        # Include the source checkout so two unrelated repositories cannot
        # accidentally share a marker or mutate each other's installation.
        runtime = project / "runtimes" / digest
        if not runtime.exists():
            copy_runtime(repository, runtime, digest)
        marker = _assert_runtime(runtime, repository, digest, ready=False)
        if marker["status"] != "ready" or not _dependencies_ready(runtime):
            marker["status"] = "preparing"
            _write_json(runtime / RUNTIME_MARKER, marker)
            print("Installing the editor's independent Python/Remotion runtime…", flush=True)
            _run(["bash", runtime / "setup.sh", "--editor"], cwd=runtime)
            _assert_runtime(runtime, repository, digest, ready=False)
            if not _dependencies_ready(runtime):
                raise InstallationError("Dependency setup did not produce a complete editor runtime; retry setup.")
            marker["status"] = "ready"
            _write_json(runtime / RUNTIME_MARKER, marker)
        native_digest = source_digest(repository, native=True)
        build = project / "builds" / native_digest
        app = build / "Build/Products/Debug/ReelsEditor.app"
        if not (app / "Contents/MacOS/ReelsEditor").is_file():
            print("Building the native macOS app…", flush=True)
            _run([
                "xcodebuild", "-quiet", "-project", repository / "editor/ReelsEditor.xcodeproj",
                "-scheme", "ReelsEditor", "-configuration", "Debug", "-destination", "platform=macOS,arch=arm64"
                if platform.machine() == "arm64" else "platform=macOS,arch=x86_64",
                "-derivedDataPath", build, "build",
            ], cwd=repository)
        if source_digest(repository, native=True) != native_digest:
            raise InstallationError("Native app sources changed while building; retry setup.")
        private = _plain_path(workspace if workspace is not None else Path(
            previous.get("workspace", project / "workspaces/current")))
        if _within(private, repository) or _within(private, runtime) or _within(repository, private) or _within(runtime, private):
            raise InstallationError("Choose a private workspace outside the checkout and installed runtime.")
        if not private.exists():
            print("Creating a private snapshot; original footage remains read-only…", flush=True)
            _run([runtime / "reels", "editor", "init", "--source", repository, "--workspace", private], cwd=runtime)
        else:
            _run([runtime / "reels", "--editor-workspace", private, "editor", "info"], cwd=runtime)
        if source_digest(repository) != digest:
            raise InstallationError("Shared editor sources changed during setup; retry to install the new version.")
        record = {
            "version": 1, "repository": str(repository), "runtime": str(runtime),
            "codeDigest": digest, "nativeDigest": native_digest, "app": str(app), "workspace": str(private),
        }
        _write_json(install_path, record)
        print(f"\nReady. Open with:\n  {repository / 'editor/reels-editor'} open\n", flush=True)
        return record


def installation(repository: Path, support: Path, *, current: bool = True) -> dict:
    path = _project_root(repository, support) / "install.json"
    if not path.exists():
        raise InstallationError("Editor is not set up yet. Run: ./editor/reels-editor setup")
    value = _read_json(path)
    if value.get("version") != 1 or value.get("repository") != str(repository):
        raise InstallationError("This installation record belongs to another checkout.")
    _assert_runtime(Path(value["runtime"]), repository, value["codeDigest"])
    if current and (source_digest(repository) != value["codeDigest"]
                    or source_digest(repository, native=True) != value["nativeDigest"]):
        raise InstallationError("Editor sources have changed. Run ./editor/reels-editor setup; existing projects are preserved.")
    return value


def launch_arguments(record: dict, plan: str | None = None, workspace: Path | None = None) -> list[str]:
    runtime = Path(record["runtime"])
    private = _plain_path(workspace if workspace is not None else Path(record["workspace"]))
    source = Path(record["repository"])
    if _within(private, source) or _within(private, runtime) or _within(runtime, private):
        raise InstallationError("The native app requires a private workspace outside the live checkout/runtime.")
    if not (private / "state.json").is_file():
        raise InstallationError(f"No initialized private workspace at {private}")
    args = [str(Path(record["app"]) / "Contents/MacOS/ReelsEditor"), "--repo", str(runtime), "--workspace", str(private)]
    if plan:
        relative = Path(plan)
        if relative.is_absolute():
            try:
                relative = relative.relative_to(source)
            except ValueError as exc:
                raise InstallationError("Choose a plans/*.json path from this project.") from exc
        if relative.parts[:1] != ("plans",) or ".." in relative.parts or relative.suffix != ".json":
            raise InstallationError("Choose a plans/*.json path from this project.")
        if not (private / "data" / relative).is_file():
            raise InstallationError("That plan is not in the private snapshot. Refresh the snapshot to import new plans.")
        args += ["--plan", relative.as_posix()]
    return args


def _require_closed(workspace: Path) -> None:
    lock = workspace / "service.lock"
    if not lock.exists():
        return
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise InstallationError("Close the editor before refreshing its private workspace.") from exc
    finally:
        os.close(fd)


def refresh(repository: Path, support: Path, *, keep_private: bool = False, resolutions: Path | None = None) -> dict:
    project = _project_root(repository, support)
    with _setup_lock(project):
        record = installation(repository, support)
        old = Path(record["workspace"])
        _require_closed(old)
        target = project / "workspaces" / f"snapshot-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
        runtime = Path(record["runtime"])
        args: list[str | Path] = [
            runtime / "reels", "editor", "refresh", "--source", repository,
            "--workspace", old, "--into", target,
        ]
        if keep_private:
            args.append("--keep-private")
        if resolutions is not None:
            args += ["--resolutions", resolutions.expanduser().resolve()]
        _run(args, cwd=runtime)
        record["workspace"] = str(target)
        _write_json(project / "install.json", record)
        print(f"New snapshot is active. Previous workspace retained at:\n  {old}", flush=True)
        return record


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="editor/reels-editor", description="Set up and open the isolated native Reels Editor.")
    parser.add_argument("--repository", type=Path, default=Path(__file__).resolve().parents[3], help=argparse.SUPPRESS)
    sub = parser.add_subparsers(dest="command")
    create = sub.add_parser("setup", help="install independent runtime, build app, initialize private snapshot")
    create.add_argument("--workspace", type=Path, help="use an existing private workspace, or initialize a new one here")
    start = sub.add_parser("open", help="open the native editor; reopen the previous plan by default")
    start.add_argument("plan", nargs="?", help="optional plans/name.json")
    start.add_argument("--workspace", type=Path, help="open another initialized private workspace")
    sub.add_parser("info", help="print installed app, runtime and private workspace paths")
    update = sub.add_parser("refresh", help="import project changes into a NEW snapshot; retain old history")
    choices = update.add_mutually_exclusive_group()
    choices.add_argument("--keep-private", action="store_true", help="keep conflicting private files; still import new source files")
    choices.add_argument("--resolutions", type=Path, help="JSON mapping conflicting relative paths to source/private")
    cli = sub.add_parser("cli", help="forward existing ./reels editor commands to the private runtime")
    cli.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        repository = args.repository.expanduser().resolve()
        support = support_root(repository)
        command = args.command or "open"
        if command == "setup":
            setup(repository, support, args.workspace)
        elif command == "refresh":
            refresh(repository, support, keep_private=args.keep_private, resolutions=args.resolutions)
        else:
            record = installation(repository, support, current=command != "info")
            if command == "info":
                print(json.dumps(record, indent=2))
            elif command == "cli":
                forwarded = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
                binary = str(Path(record["runtime"]) / "reels")
                os.execve(binary, [binary, *forwarded], _environment())
            else:
                native_args = launch_arguments(record, getattr(args, "plan", None), getattr(args, "workspace", None))
                os.execve(native_args[0], native_args, _environment())
        return 0
    except (InstallationError, OSError, subprocess.CalledProcessError) as exc:
        print(f"Editor: {exc}", file=sys.stderr)
        return exc.returncode if isinstance(exc, subprocess.CalledProcessError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
