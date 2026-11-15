"""Editor commands registered by the existing reels CLI."""
from __future__ import annotations

import json
from pathlib import Path


def register_parser(subparsers):
    parser = subparsers.add_parser("editor", help="native macOS Reels Editor service (works directly on this folder)")
    parser.set_defaults(fn=run)
    commands = parser.add_subparsers(dest="editor_cmd", required=True)
    commands.add_parser("serve", help="versioned NDJSON editing service on stdin/stdout, live on this folder")
    commands.add_parser("info", help="show the live roots, editor state folder and original-media status")
    relay = commands.add_parser("mcp", help="internal official-SDK MCP stdio relay")
    relay.add_argument("--socket", required=True)
    relay.add_argument("--token-file", required=True)
    relay.add_argument("--scope", choices=("editing", "search"), required=True)
    return parser


def run(project, args) -> int:
    command = args.editor_cmd
    if command == "mcp":
        from .mcp import serve_mcp
        serve_mcp(args.socket, args.token_file, args.scope)
        return 0
    from ..config import find_root
    from .workspace import Workspace

    workspace = Workspace.live(find_root())
    if command == "info":
        statuses = workspace.media_status()
        print(json.dumps({"repo": str(workspace.data_root), "state": str(workspace.root),
                          "code": str(workspace.code_root), "originals": str(workspace.project.media_dir),
                          "assets": len(statuses),
                          "offline": {key: value for key, value in statuses.items() if value.get("status") != "online"}},
                         default=str, indent=2))
        return 0
    from .service import serve
    return serve(workspace)
