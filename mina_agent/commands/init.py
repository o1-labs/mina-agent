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


def exclude_harness(repo):
    """Add the harness checkout to the Mina repo's .git/info/exclude, so an
    unshipped clone inside the tree never shows as untracked. Idempotent."""
    import subprocess
    rel = paths.harness_relpath(repo)
    if not rel or rel == ".":
        return None, "harness is not inside the repo"
    r = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=repo, capture_output=True, text=True, check=True)
    exclude = paths.Path(repo) / r.stdout.strip()
    line = f"/{rel}/"
    have = exclude.read_text().splitlines() if exclude.exists() else []
    if line in have:
        return exclude, "already excluded"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    exclude.write_text("\n".join([*have, line]) + "\n")
    return exclude, f"added {line}"


def init():
    """Prepare this checkout: graph, LSP plugin, declared plugins, git pre-commit hook.

    Writes nothing into the repo's Claude settings, the MCP registry, or the
    skills directory; the harness applies only when invoked (discuss, phases).
    Safe to rerun.
    """
    from .. import env as envmod, graph
    e = envmod.require()
    paths.state_dir()
    d = graph.derive_and_write(e)
    print(f"graph: {graph.summary(d)}")
    hook, how = install_git_hook(e.repo)
    print(f"git pre-commit: {hook} ({how})")
    exclude, how = exclude_harness(e.repo)
    print(f"git exclude: {exclude or ''} ({how})")
    from .. import lsp
    path, source = lsp.resolve(e)
    out = lsp.write_plugin(e.repo, path)
    print(f"plugin: {out} (skills; passed to sessions with --plugin-dir)")
    if path:
        print(f"lsp: {path} ({source}) -> in the plugin")
    else:
        print(f"lsp: {source}; Claude's LSP tool unavailable, merlin tools still work")
    from .. import plugins
    for name, d, msg in plugins.sync(e.repo):
        print(f"plugin {name}: {d} {msg}")
    print("next: mina-agent doctor")
