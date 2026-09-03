# harness setup

One-time steps on a fresh clone. Full docs come in harness/README.md later.

    ./harness/bootstrap.sh                 # creates harness/.venv, installs mcp (pinned)
    python3 harness/derive.py              # builds bin/describe-dune, writes derived.json
    claude mcp add --scope local --transport stdio mina-harness -- \
        "$PWD/harness/.venv/bin/python" "$PWD/harness/server.py"
    claude mcp list                        # expect: mina-harness ... Connected

The MCP entry lives in ~/.claude.json keyed to this repo path, not in the
repo. Undo with `claude mcp remove mina-harness -s local`. Delete
harness/.venv and harness/bin to undo the rest.
