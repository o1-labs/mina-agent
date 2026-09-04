#!/usr/bin/env python3
"""mina-harness MCP server (stdio). Thin wrapper over tools.py.

Run:   mina-agent serve   (what `mina-agent init` registers with claude mcp add)
"""
import functools

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from . import tools


def anticipated(fn):
    """Tools signal anticipated failures (unknown library, no toolchain,
    path outside any unit) with ValueError/RuntimeError. mcp forwards only
    ToolError text to the model and replaces anything else with a generic
    "Error executing tool"; convert so the reason reaches the model."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (ValueError, RuntimeError) as ex:
            raise ToolError(str(ex)) from ex
    return wrapper

server = MCPServer(
    "mina-harness",
    instructions=("Build, type-check, and test tools for the Mina monorepo. "
                  "Use these instead of running dune, opam, nix, or cargo directly. "
                  "type_at/definition answer questions about code as last compiled; "
                  "check decides whether an edit compiles."),
)

for _name in tools.TOOLS:
    server.tool()(anticipated(getattr(tools, _name)))
