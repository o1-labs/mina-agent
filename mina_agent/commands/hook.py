"""Claude Code hook entry points (hidden). Each reads the hook JSON on stdin
and prints the documented JSON on stdout; exit 2 blocks (PreToolUse)."""
import json
import sys

import typer

app = typer.Typer(help="Hook entry points for .claude/settings.local.json (read stdin, write stdout).")


def _payload():
    try:
        return json.load(sys.stdin)
    except ValueError:
        return {}


@app.command("post-edit")
def post_edit():
    """PostToolUse Edit|Write: type-check the edited .ml/.mli and return diagnostics."""
    from .. import agent
    out = agent.post_edit_check_output(_payload().get("tool_input"))
    if out:
        print(json.dumps(out))


@app.command("pre-bash")
def pre_bash():
    """PreToolUse Bash: block raw dune/opam/nix/cargo/make and environment mutation."""
    from .. import guard
    hit = guard.offending((_payload().get("tool_input") or {}).get("command") or "")
    if hit:
        sub, word, why = hit
        sys.stderr.write(f"blocked `{sub}`: `{word}` is not allowed here; {why}\n")
        raise typer.Exit(2)


@app.command("session-start")
def session_start():
    """SessionStart: re-derive the graph and hand the session its facts."""
    from .. import env as envmod, graph, tools, banner
    _payload()
    e = envmod.detect()
    if e.mode != "none":
        graph.derive_and_write(e)
    out = {"systemMessage": banner.render(e.to_dict()),
           "hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": "\n".join(tools.facts())}}
    print(json.dumps(out))
