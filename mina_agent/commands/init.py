"""Register the MCP server and install the walls into this checkout."""
import typer

from .. import agent, paths


HOOK_MARKER = "# installed by mina-agent init"


def install_git_hook(repo):
    """Write .git/hooks/pre-commit (or core.hooksPath/pre-commit) calling
    `mina-agent hook pre-commit`. Leaves a foreign hook alone."""
    hook = paths.git_hook(repo)
    body = f"#!/bin/sh\n{HOOK_MARKER}\nexec \"{agent.mina_agent_bin()}\" hook pre-commit\n"
    if hook.exists() and HOOK_MARKER not in hook.read_text():
        return hook, "exists and is not ours; left alone (chain it manually: mina-agent hook pre-commit)"
    hook.parent.mkdir(parents=True, exist_ok=True)
    hook.write_text(body)
    hook.chmod(0o755)
    return hook, "installed"


def init():
    """Prepare this checkout: graph, LSP plugin, git pre-commit hook.

    Writes nothing into the repo's Claude settings, the MCP registry, or the
    skills directory; the harness applies only when invoked (discuss, phases).
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
    hook, how = install_git_hook(e.repo)
    print(f"git pre-commit: {hook} ({how})")
    from .. import lsp
    path, source = lsp.resolve(e)
    if path:
        out = lsp.write_plugin(e.repo, path)
        print(f"lsp: {path} ({source}) -> plugin {out} (passed to sessions with --plugin-dir)")
    else:
        print(f"lsp: {source}; Claude's LSP tool unavailable, merlin tools still work")
    print("next: mina-agent doctor")
