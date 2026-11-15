"""Editor commands registered by the existing reels CLI."""
from __future__ import annotations

import json
from pathlib import Path

from .protocol import ProtocolError


def register_parser(subparsers):
    parser = subparsers.add_parser("editor", help="isolated native macOS editor workspace and local editing service")
    parser.set_defaults(fn=run)
    commands = parser.add_subparsers(dest="editor_cmd", required=True)
    init = commands.add_parser("init", help="snapshot private editing data, sharing registered originals read-only")
    init.add_argument("--source", required=True, help="source cycling-reels checkout (read-only)")
    init.add_argument("--workspace", default=".cache/editor/workspace", help="new private workspace, never an existing project")
    refresh = commands.add_parser("refresh", help="reconcile into a NEW private snapshot without overwriting the existing workspace")
    refresh.add_argument("--source", required=True, help="production data source, read-only")
    refresh.add_argument("--workspace", required=True, help="existing private workspace to preserve")
    refresh.add_argument("--into", required=True, help="new workspace folder; must not exist")
    choices = refresh.add_mutually_exclusive_group()
    choices.add_argument("--resolutions", help="JSON file mapping conflicting relative paths to private or source")
    choices.add_argument("--keep-private", action="store_true", help="explicitly keep all conflicting private files while importing new source files")
    commands.add_parser("serve", help="versioned NDJSON editing service on stdin/stdout; requires --editor-workspace")
    commands.add_parser("info", help="show isolated workspace roots and offline original-media status")
    relay = commands.add_parser("mcp", help="internal official-SDK MCP stdio relay")
    relay.add_argument("--socket", required=True)
    relay.add_argument("--token-file", required=True)
    relay.add_argument("--scope", choices=("editing", "search"), required=True)
    return parser


def run(project, args) -> int:
    from .workspace import Workspace, init_workspace

    code_root = Path(__file__).resolve().parents[3]
    command = args.editor_cmd
    if command == "init":
        workspace = init_workspace(Path(args.source), Path(args.workspace), code_root=code_root)
        _print_workspace(workspace)
        return 0
    if command == "refresh":
        from .refresh import refresh_workspace
        resolutions = None
        if args.resolutions:
            resolutions = json.loads(Path(args.resolutions).read_text())
        workspace = refresh_workspace(Path(args.source), Path(args.workspace), Path(args.into),
                                      resolutions=resolutions, keep_private=args.keep_private, code_root=code_root)
        _print_workspace(workspace)
        return 0
    if command == "mcp":
        from .mcp import serve_mcp
        serve_mcp(args.socket, args.token_file, args.scope)
        return 0
    path = getattr(args, "editor_workspace", None)
    if not path:
        raise ProtocolError("workspace_required", "Pass --editor-workspace PATH before editor " + command + ".")
    workspace = Workspace.load(Path(path), code_root=code_root)
    if command == "info":
        _print_workspace(workspace)
        return 0
    from .service import serve
    return serve(workspace)


def _print_workspace(workspace):
    statuses = workspace.media_status()
    print(json.dumps({"workspace": str(workspace.root), "data": str(workspace.data_root),
                      "code": str(workspace.code_root), "assets": len(statuses),
                      "offline": {key: value for key, value in statuses.items() if value.get("status") != "online"}},
                     default=str, indent=2))
