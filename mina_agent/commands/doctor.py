"""Verify the full harness setup and report.

Same shape as lint: each check is a small function returning a Check, and
one renderer prints the table. Add a check by appending to CHECKS.
"""
import json
import os
import shutil
import subprocess
from dataclasses import dataclass

import typer
from rich.console import Console
from rich.table import Table

from .. import agent, paths

OK, NOTE, FAIL = "ok", "note", "FAIL"


@dataclass
class Check:
    name: str
    status: str          # ok | note | FAIL
    detail: str = ""


def _run(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


# ---- checks: each takes the detected env and returns one or more Checks ----

def toolchain(e):
    yield Check("toolchain", OK if e.mode != "none" else FAIL,
                f"mode={e.mode} activated={e.activated} dune={e.dune_version} ocaml={e.ocaml}")
    b = e.build_dir or {}
    drift = bool(b.get("built_by")) and b["built_by"] != e.mode
    yield Check("_build provenance", FAIL if drift else OK, f"built_by={b.get('built_by')} exists={b.get('exists')}")
    for w in e.warnings:
        yield Check("warning", NOTE, w)


def binaries(e):
    if e.mode == "none":
        return
    path = e.activate().get("PATH")
    for name in ("ocamlmerlin", "claude"):
        p = shutil.which(name, path=path)
        yield Check(name, OK if p else FAIL, p or "not on PATH")
    yield Check("mina-agent", OK if shutil.which("mina-agent") else FAIL, agent.mina_agent_bin())


def lsp(e):
    if e.mode == "none":
        return
    from .. import lsp as L
    p, source = L.resolve(e)
    yield Check("ocamllsp", OK if p else NOTE, f"{p} ({source})" if p else source)
    if p:
        gen = L.plugin_dir(e.repo)
        yield Check("lsp plugin", OK if gen else FAIL,
                    f"{gen} (passed to sessions with --plugin-dir)" if gen else "not generated; run mina-agent init")


def opam_export(e):
    if e.mode == "none":
        return
    r = _run([os.path.join(e.repo, "_opam", "bin", "check_opam_switch"), "opam.export"], cwd=e.repo)
    out = r.stdout + r.stderr
    if r.returncode == 0 and "not a superset" not in out:
        yield Check("opam.export", OK, "project switch matches the export")
    else:
        missing = "; ".join(l.strip() for l in out.splitlines() if "Could not find" in l) or "check_opam_switch unavailable"
        yield Check("opam.export", NOTE, missing + " (make build's check_opam_switch will fail; set DISABLE_CHECK_OPAM_SWITCH=true)")


def graph(e):
    from .. import graph as G
    tool = paths.describe_dune_bin(e.repo)
    yield Check("describe-dune", OK if tool.exists() else FAIL, str(tool) if tool.exists() else "run mina-agent setup")
    dj = paths.derived_json(e.repo)
    if dj.exists() and tool.exists() and e.mode != "none":
        fresh = G.check(e)
        yield Check("derived graph", OK if fresh else FAIL, str(dj) + ("" if fresh else " is stale; rerun mina-agent init"))
    else:
        yield Check("derived graph", FAIL, "missing; run mina-agent setup && mina-agent init")


def session_config(e):
    """The walls travel with the invocation; verify the template renders."""
    try:
        st = agent.session_settings()
        n_hooks = sum(len(m["hooks"]) for ms in st["hooks"].values() for m in ms)
        yield Check("session config", OK, f"{len(st['permissions']['deny'])} deny rules, {n_hooks} hook entries, "
                    "applied per session via --settings / SDK options (nothing in .claude/)")
    except Exception as ex:
        yield Check("session config", FAIL, f"settings template unreadable: {ex}")


def no_leakage(e):
    """Nothing of the harness may be installed into default Claude sessions."""
    leaks = []
    local = paths.settings_local(e.repo)
    if local.exists():
        with open(local) as fh:
            s = json.load(fh)
        if any("mina-agent" in h.get("command", "") for ms in s.get("hooks", {}).values()
               for m in ms for h in m.get("hooks", [])):
            leaks.append(f"harness hooks in {local}")
    if paths.SKILLS_DIR_LINK.is_symlink():
        leaks.append(f"symlink {paths.SKILLS_DIR_LINK}")
    if _run(["claude", "mcp", "get", paths.MCP_SERVER_NAME]).returncode == 0:
        leaks.append(f"claude mcp registration {paths.MCP_SERVER_NAME}")
    yield Check("no leakage into default sessions", OK if not leaks else FAIL,
                "clean" if not leaks else "; ".join(leaks) + " (remove by hand; the harness no longer writes these)")


def git_hook(e):
    hook = paths.git_hook(e.repo)
    ours = hook.exists() and "mina-agent" in hook.read_text() and agent.mina_agent_bin() in hook.read_text()
    yield Check("git pre-commit", OK if ours else FAIL, str(hook) if ours else f"{hook} missing or foreign; run mina-agent init")


def linters(e):
    for name, job in (("shellcheck", "Lint/Bash"), ("hadolint", "Lint/Docker")):
        found = shutil.which(name)
        yield Check(name, OK if found else NOTE, found or f"not installed; {job} will be skipped locally and run by CI")
    from .. import dhall
    ok, detail = dhall.status(e.repo)
    yield Check("dhall", OK if ok else NOTE, detail)


def review_tools(e):
    """`mina-agent review` needs gh (logged in, able to see this repo's PRs)
    and, for structural diffs, difftastic. Neither is required elsewhere."""
    gh = shutil.which("gh")
    if not gh:
        yield Check("gh", NOTE, "not installed; `mina-agent review` unavailable (brew install gh)")
    elif _run(["gh", "auth", "status"]).returncode != 0:
        yield Check("gh", NOTE, f"{gh} installed but not logged in (gh auth login)")
    elif _run(["gh", "pr", "list", "--limit", "1"], cwd=e.repo).returncode != 0:
        yield Check("gh", NOTE, f"{gh} logged in but cannot list this repository's PRs")
    else:
        yield Check("gh", OK, f"{gh}, logged in, can read this repository's PRs")
    d = shutil.which("difft")
    yield Check("difftastic", OK if d else NOTE, d or "not installed; review diffs fall back to git diff (brew install difftastic)")


def external_plugins(e):
    from .. import plugins
    for name, ok, detail in plugins.status(e.repo):
        yield Check(f"plugin {name}", OK if ok else FAIL, detail)


def notes(e):
    p = paths.notes_file(e.repo)
    yield Check("notes", NOTE, str(p) if p.exists() else "none yet (created by discuss)")


CHECKS = [toolchain, binaries, lsp, opam_export, graph, session_config, no_leakage, git_hook, linters,
          review_tools, external_plugins, notes]


def render(checks):
    t = Table(show_header=True, header_style="bold")
    t.add_column("check"); t.add_column("status"); t.add_column("detail")
    colors = {OK: "green", NOTE: "yellow", FAIL: "red"}
    for c in checks:
        t.add_row(c.name, f"[{colors[c.status]}]{c.status}[/{colors[c.status]}]", c.detail)
    Console().print(t)


def doctor():
    """Check toolchain, graph, session config, git hook, linters, and that nothing leaked into default Claude sessions."""
    from .. import env as envmod
    e = envmod.detect()
    checks = [c for fn in CHECKS for c in fn(e)]
    render(checks)
    if any(c.status == FAIL for c in checks):
        raise typer.Exit(1)
