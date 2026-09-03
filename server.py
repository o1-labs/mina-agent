#!/usr/bin/env python3
"""mina-harness MCP server (stdio). Thin wrapper over tools.py.

Run:   harness/.venv/bin/python harness/server.py
Test:  harness/.venv/bin/python harness/server.py --selftest
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tools  # noqa: E402

from mcp.server.mcpserver import MCPServer  # noqa: E402

server = MCPServer(
    "mina-harness",
    instructions=("Build, type-check, and test tools for the Mina monorepo. "
                  "Use these instead of running dune, opam, nix, or cargo directly. "
                  "type_at/definition answer questions about code as last compiled; "
                  "check decides whether an edit compiles."),
)

for _name in tools.TOOLS:
    server.tool()(getattr(tools, _name))

if __name__ == "__main__":
    if "--selftest" in sys.argv:
        tools.selftest()
        print("tools registered:", [t.name for t in server._tool_manager.list_tools()])
    else:
        server.run(transport="stdio")
