#!/usr/bin/env python3
"""mina-harness MCP server (stdio). Thin wrapper over tools.py.

Run:   mina-agent serve   (what `mina-agent init` registers with claude mcp add)
"""
from mcp.server.mcpserver import MCPServer

from . import tools

server = MCPServer(
    "mina-harness",
    instructions=("Build, type-check, and test tools for the Mina monorepo. "
                  "Use these instead of running dune, opam, nix, or cargo directly. "
                  "type_at/definition answer questions about code as last compiled; "
                  "check decides whether an edit compiles."),
)

for _name in tools.TOOLS:
    server.tool()(getattr(tools, _name))
