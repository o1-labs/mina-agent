"""Register the MCP server and install the walls into this checkout."""
import json
import subprocess

import typer

from .. import agent, paths


def merge_settings(repo):
    """Merge data/settings.template.json into .claude/settings.local.json:
    deny lists are unioned, hook events are replaced by the template's with
    the hook commands pointing at this mina-agent binary, everything else is
    preserved. Idempotent."""
    with open(paths.SETTINGS_TEMPLATE) as fh:
        tpl = json.load(fh)
    tpl.pop("_comment", None)
    target = paths.settings_local(repo)
    cur = {}
    if target.exists():
        with open(target) as fh:
            cur = json.load(fh)
    perms = cur.setdefault("permissions", {})
    perms["deny"] = sorted(set(perms.get("deny", [])) | set(tpl["permissions"]["deny"]))
    binpath = agent.mina_agent_bin()
    hooks = {}
    for event, matchers in tpl["hooks"].items():
        hooks[event] = []
        for m in matchers:
            m = json.loads(json.dumps(m))
            for h in m["hooks"]:
                h["command"] = h["command"].replace("mina-agent", f'"{binpath}"', 1)
            hooks[event].append(m)
    cur.setdefault("hooks", {}).update(hooks)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as fh:
        json.dump(cur, fh, indent=2)
        fh.write("\n")
    return target, len(perms["deny"]), sorted(hooks)


def register_mcp():
    """`claude mcp add --scope local` so the entry lives in ~/.claude.json keyed
    to this checkout, not in the repo (settings files cannot hold mcpServers)."""
    name = paths.MCP_SERVER_NAME
    subprocess.run(["claude", "mcp", "remove", name, "-s", "local"],
                   capture_output=True, text=True)
    r = subprocess.run(["claude", "mcp", "add", "--scope", "local", "--transport", "stdio", name,
                        "--", agent.mina_agent_bin(), "serve"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return name


def init():
    """Register the MCP server and install the walls into this checkout.

    Adds mina-harness to Claude Code at local scope (~/.claude.json, keyed to
    this checkout), merges the hooks and deny rules into the gitignored
    .claude/settings.local.json, derives the graph, creates harness/state.
    Safe to rerun.
    """
    from .. import env as envmod, graph
    e = envmod.detect()
    if e.mode == "none":
        typer.echo("no usable toolchain: " + "; ".join(e.reasons), err=True)
        raise typer.Exit(3)
    paths.state_dir(e.repo)
    d = graph.derive_and_write(e)
    print(f"graph: {graph.summary(d)}")
    target, ndeny, events = merge_settings(e.repo)
    print(f"settings: {target} ({ndeny} deny rules; hooks for {', '.join(events)})")
    try:
        name = register_mcp()
        print(f"mcp: registered {name} at local scope -> {agent.mina_agent_bin()} serve")
    except (RuntimeError, FileNotFoundError) as ex:
        typer.echo(f"mcp: registration failed: {ex}", err=True)
        raise typer.Exit(1)
    print("next: mina-agent doctor")
