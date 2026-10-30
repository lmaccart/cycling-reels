from __future__ import annotations

from pathlib import Path

from .config import Project
from .util import ReelsError, read_json, write_json


def asset_file(project: Project, asset_id: str) -> Path:
    return project.assets_dir / f"{asset_id}.json"


def all_assets(project: Project) -> list[dict]:
    if not project.assets_dir.exists():
        return []
    out = []
    for p in sorted(project.assets_dir.glob("*.json")):
        try:
            out.append(read_json(p))
        except Exception:  # noqa: BLE001
            continue
    return sorted(out, key=lambda a: (a.get("created") or "", a["id"]))


def resolve_id(project: Project, token: str) -> str:
    """Exact asset id, or a unique prefix / substring of one."""
    token = token.strip()
    if not token:
        raise ReelsError("missing asset id")
    if asset_file(project, token).exists():
        return token
    ids = [p.stem for p in project.assets_dir.glob("*.json")] if project.assets_dir.exists() else []
    hits = [i for i in ids if i.startswith(token)] or [i for i in ids if token in i]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ReelsError(f"no asset matches '{token}'")
    raise ReelsError(f"'{token}' matches several assets: {', '.join(hits[:6])}")


def load_asset(project: Project, token: str) -> dict:
    return read_json(asset_file(project, resolve_id(project, token)))


def save_asset(project: Project, asset: dict) -> None:
    write_json(asset_file(project, asset["id"]), asset)


def find_by_hash(project: Project, h: str) -> str | None:
    for a in all_assets(project):
        if a.get("hash") == h:
            return a["id"]
    return None


def moment_used(asset: dict, moment: dict) -> bool:
    for u in asset.get("used") or []:
        if "in" not in u:  # photo usage
            return True
        if u["in"] < moment["out"] and moment["in"] < u["out"]:
            return True
    return False
