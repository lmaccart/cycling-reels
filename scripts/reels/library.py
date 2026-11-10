from __future__ import annotations

from pathlib import Path

from .config import Project
from .util import ReelsError, read_json, warn, write_json


def asset_file(project: Project, asset_id: str) -> Path:
    return project.assets_dir / f"{asset_id}.json"


def all_assets(project: Project) -> list[dict]:
    if not project.assets_dir.exists():
        return []
    out = []
    for p in sorted(project.assets_dir.glob("*.json")):
        try:
            out.append(read_json(p))
        except Exception as e:  # noqa: BLE001
            # Skipping it silently would make the clip vanish from the library (and its usage with it).
            warn(f"skipping unreadable asset record library/assets/{p.name} ({type(e).__name__}) — fix or restore it from git")
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


def resolve_moment(asset: dict, reference: int | str) -> dict:
    """Stable moment id, or a legacy numeric index for older plans."""
    moments = (asset.get("tags") or {}).get("moments") or []
    if type(reference) is int and 0 <= reference < len(moments):
        return moments[reference]
    if isinstance(reference, str):
        hits = [m for m in moments if m.get("id") == reference]
        if len(hits) == 1:
            return hits[0]
    raise ReelsError(f"moment {reference!r} doesn't uniquely identify a moment on {asset['id']} ({len(moments)} moments)")


def update_asset(project: Project, asset: dict, **fields) -> None:
    """Set fields on the record as it is on disk now, so slow jobs don't overwrite tags saved meanwhile."""
    path = asset_file(project, asset["id"])
    fresh = read_json(path) if path.exists() else dict(asset)
    fresh.update(fields)
    asset.update(fields)
    write_json(path, fresh)


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
