"""One reel as a self-contained Remotion project: the hand-off to Kino or Remotion Studio.

Kino (kino.ai, an agent-first browser/desktop video editor) imports a Remotion project folder and
keeps editing it as a Kino composition; Remotion Studio opens the same folder locally. Either way
the folder holds this project's template, the reel's exact props, and only the reel's own staged
media — cut from the user's footage by the same staging step `./reels render` uses. Never the
library, never anything else.

The plan stays the source of truth: changes made in Kino or Studio come back as plan edits, and the
reel that gets approved and scheduled is always the one `./reels render` makes (footage guard and
all). See .claude/skills/cycling-reels/reference/kino.md.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from .config import Project
from .media import require
from .util import ReelsError, log, now_iso, warn, write_json

MARKER = ".reels-project.json"
TEMPLATE_FILES = ("package.json", "package-lock.json", "tsconfig.json", "remotion.config.ts")
OWNED = ("src", "public", "props.json", "README.md", MARKER, *TEMPLATE_FILES)  # what a re-export may replace


def _prepare(dest: Path) -> None:
    """An empty (or new) folder, or one an earlier export wrote — then only our own files are replaced."""
    if dest.exists() and not dest.is_dir():
        raise ReelsError(f"{dest} is a file — give a folder")
    if dest.is_dir() and any(dest.iterdir()):
        if not (dest / MARKER).exists():
            raise ReelsError(f"{dest} isn't empty and wasn't written by ./reels project — pick an empty folder")
        for name in OWNED:
            p = dest / name
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            elif p.exists() or p.is_symlink():
                p.unlink()
        left = sorted(p.name for p in dest.iterdir() if p.name != "node_modules")
        if left:
            warn(f"left alone in {dest}: {', '.join(left)}")
    dest.mkdir(parents=True, exist_ok=True)


def props_ts(slug: str, props: dict) -> str:
    """src/defaultProps.ts for the export: the composition opens on this reel instead of the text-style demo."""
    body = json.dumps(props, indent=1, ensure_ascii=False)
    return (
        f"// Written by ./reels project for {slug}: the reel exactly as ./reels render builds it.\n"
        "// The media in public/ are this reel's own staged clips (the user's footage). Try changes here or in\n"
        "// Kino / Remotion Studio, but carry them back into the plan: the reel that gets posted comes from ./reels render.\n"
        "import type { ReelProps } from './types';\n\n"
        f"export const defaultProps: ReelProps = {body};\n"
    )


def readme(slug: str, plan_rel: str) -> str:
    return f"""# {slug} — an editable copy of one reel

This folder is one reel from cycling-reels as a standalone [Remotion](https://www.remotion.dev) project:
the template (`src/`), the reel's exact timeline (`src/defaultProps.ts`, also `props.json`) and its
staged clips, music and sound effects in `public/` — cut from your own footage, nothing else.

## Open it

- **Remotion Studio** (on this computer): `npm install`, then `npm run studio`. Set `safeArea: true` in
  `src/defaultProps.ts` to see where Instagram's buttons and caption sit.
- **Kino** ([kino.ai](https://kino.ai)): import this folder as a Remotion project. Good for watching it on
  a big timeline, leaving time-coded notes, sending a review link, or trying a different trim.

## Then

Nothing made here gets posted. Say what to change (or have Claude read your Kino notes): it edits
`{plan_rel}`, re-renders with `./reels render`, and that reel — checked against your original
footage — is the one you approve and schedule. Running `./reels project` again replaces `src/`,
`public/` and the files next to them here (a `node_modules/` you installed stays).
"""


def export_project(project: Project, plan_path: str | Path, *, out: str | Path | None = None) -> Path:
    from .render import build, load_plan, stage

    require("ffmpeg")
    require("ffprobe")
    plan = load_plan(project, plan_path)
    built = build(project, plan)  # every footage, rights and fact check a render does
    for w in built["warnings"]:
        warn(w)
    slug = plan["slug"]
    dest = (Path(out).expanduser() if out else project.out_dir / slug / "project").resolve()
    rdir = project.remotion_dir
    if not (rdir / "src" / "index.ts").exists():
        raise ReelsError(f"no Remotion template at {project.rel(rdir)} — re-run ./setup.sh")
    _prepare(dest)
    log(f"staging {slug} ({built['total'] / built['fps']:.1f}s) into {dest} …")
    stage(project, built, dest / "public")
    shutil.copytree(rdir / "src", dest / "src")
    for name in TEMPLATE_FILES:
        if (rdir / name).exists():
            shutil.copy2(rdir / name, dest / name)
    (dest / "src" / "defaultProps.ts").write_text(props_ts(slug, built["props"]), encoding="utf-8")
    write_json(dest / "props.json", built["props"])
    plan_rel = project.rel(project.abs(plan_path))
    (dest / "README.md").write_text(readme(slug, plan_rel), encoding="utf-8")
    write_json(dest / MARKER, {"slug": slug, "plan": plan_rel, "createdAt": now_iso(), "fps": built["fps"], "frames": built["total"],
                               "composition": "CyclingReel"})  # fmt: skip
    size = sum(p.stat().st_size for p in (dest / "public").rglob("*") if p.is_file()) / 1e6
    log(f"✓ {dest}  ({size:.0f} MB of staged media)")
    log("  Remotion Studio: cd there, npm install, npm run studio · Kino: import the folder as a Remotion project")
    log("  changes made there come back as plan edits; the reel that posts is the one ./reels render makes")
    return dest
