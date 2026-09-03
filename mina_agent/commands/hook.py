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


MESSAGES = {
    "tools": "runs the OCaml toolchain directly; the mina-harness MCP tools (build / check / "
             "check_dependents / test / test_one) run dune in the right switch with structured output",
    "env": "mutates or bypasses the build environment (opam/nix/cargo/make/pip), which this harness never does",
}


@app.command("pre-bash")
def pre_bash(why: str = typer.Option("tools", "--why", help="Which explanation to attach: tools | env.")):
    """PreToolUse Bash: attach an explanation, never block.

    Reached only for commands Claude Code's own Bash matcher flags through the
    hook `if` rules in settings (that matcher over-matches on purpose, e.g. on
    quoted text). The deny rules alone decide whether the command runs; this
    adds context so a denial comes with the reason and the alternative.
    """
    cmd = (_payload().get("tool_input") or {}).get("command") or ""
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                      "additionalContext": f"harness: if `{cmd[:100]}` is denied, it is because it "
                                           f"{MESSAGES.get(why, MESSAGES['tools'])}."}}))


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
