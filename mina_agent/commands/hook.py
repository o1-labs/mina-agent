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


DENIAL_CONTEXT = ("harness: if this command is denied, it is because it either runs the OCaml "
                  "toolchain directly (use the mina-harness MCP tools build / check / check_dependents / "
                  "test / test_one, which run dune in the right switch with structured output) or mutates "
                  "the build environment (opam/nix/cargo/make/pip), which this harness never does.")


def _first_for_call(tool_use_id):
    """True for the first hook process handling this tool call. Several `if`
    entries can match one command (Claude Code's matcher over-matches on
    purpose, e.g. any command containing `$`), and each spawns its own
    process; a marker file keyed by tool_use_id makes the context appear once."""
    if not tool_use_id:
        return True
    import os
    import tempfile
    marker = os.path.join(tempfile.gettempdir(), f"mina-agent-prebash-{tool_use_id}")
    try:
        os.close(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


@app.command("pre-bash")
def pre_bash():
    """PreToolUse Bash: attach an explanation, never block.

    Reached only for commands Claude Code's own Bash matcher flags through the
    hook `if` rules in the session settings. The deny rules alone decide
    whether the command runs; this adds one line of context per call so a
    denial arrives with its reason and the alternative.
    """
    payload = _payload()
    if _first_for_call(payload.get("tool_use_id")):
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                 "additionalContext": DENIAL_CONTEXT}}))


@app.command("pre-commit")
def pre_commit():
    """git pre-commit: run CI's lint jobs on the staged files; non-zero blocks the commit."""
    from .. import env as envmod, lint as L
    from .lint import render
    e = envmod.detect()
    if e.mode == "none":
        sys.stderr.write("mina-agent pre-commit: no usable toolchain, skipping lint\n")
        return
    files, results = L.run(e, scope="staged", caller="pre-commit")
    bad = [r for r in results if r.status == "fail"]
    skipped = [r for r in results if r.status == "skip"]
    if bad or skipped:
        render(files, results, "staged")
    if bad:
        sys.stderr.write("mina-agent pre-commit: commit blocked; fix the failures above or bypass once "
                         "with `git commit --no-verify`\n")
        raise typer.Exit(1)


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
