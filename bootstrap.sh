#!/bin/sh
# Create the harness's private Python venv and install the pinned MCP SDK.
# This is the only thing the harness installs. It lives entirely in
# harness/.venv (gitignored) and does not touch the opam switch, nix, cargo,
# or the system Python. Delete harness/.venv to undo.
set -eu
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q -r requirements.txt
.venv/bin/python -c 'import mcp.server.mcpserver' && echo "harness venv ready: $(pwd)/.venv"
