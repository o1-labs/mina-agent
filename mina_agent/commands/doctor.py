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
from ..model import Status
from .lint import colored

OK, NOTE, FAIL = Status.OK, Status.NOTE, Status.FAIL


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str = ""


def _run(argv, cwd=None):
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True)


# ---- checks: each takes the detected env and returns one or more Checks ----

def toolchain(e):
    yield Check("toolchain", OK if e.usable else FAIL,
                e.summary() + ("" if e.usable else "; " + "; ".join(e.reasons)))
    b = e.build_dir
    drift = bool(b.built_by) and b.built_by != e.mode
    yield Check("_build provenance", FAIL if drift else OK, f"built_by={b.built_by} exists={b.exists}")
    for w in e.warnings:
        yield Check("warning", NOTE, w)


def binaries(e):
    if not e.usable:
        return
    path = e.activate().get("PATH")
    for name in ("ocamlmerlin", "claude"):
        p = shutil.which(name, path=path)
        yield Check(name, OK if p else FAIL, p or "not on PATH")
    yield Check("mina-agent", OK if shutil.which("mina-agent") else FAIL, agent.mina_agent_bin())


def lsp(e):
    if not e.usable:
        return
    from .. import lsp as L
    p, source = L.resolve(e)
    yield Check("ocamllsp", OK if p else NOTE, f"{p} ({source})" if p else source)
    if p:
        gen = L.plugin_dir(e.repo) if L.has_lsp(e.repo) else None
        yield Check("lsp plugin", OK if gen else FAIL,
                    f"{gen} (passed to sessions with --plugin-dir)" if gen else "not generated; run mina-agent init")


def opam_export(e):
    if not e.usable:
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
    tool = paths.describe_dune_bin()
    yield Check("describe-dune", OK if tool.exists() else FAIL, str(tool) if tool.exists() else "run mina-agent setup")
    dj = paths.derived_json()
    if dj.exists() and tool.exists() and e.usable:
        fresh = G.check(e)
        yield Check("derived graph", OK if fresh else FAIL, str(dj) + ("" if fresh else " is stale; rerun mina-agent init"))
    else:
        yield Check("derived graph", FAIL, "missing; run mina-agent setup && mina-agent init")
    ub = paths.usages_bin()
    yield Check("usages", OK if ub.exists() else FAIL,
                str(ub) if ub.exists() else "not built (reads .cmt typed trees for the usages tool); run mina-agent setup")
    from .. import landmarks, profile as P
    ok, detail = landmarks.status(e.repo)
    yield Check("landmarks", OK if ok else NOTE, detail)
    s = P.load(e.repo)
    if s:
        yield Check("profiling session", NOTE, f"active since {s.started} on {s.focus} "
                    f"({len(s.injected)} dune files instrumented); mina-agent profile --restore ends it")


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


def perf_tools(e):
    from .. import perf
    t = perf.tools_available()
    yield Check("samply", OK if t["samply"] else NOTE,
                t["samply"] or "not installed (cargo install samply); verify-perf measures time and allocation without it, not sample shares")


def github(e):
    """fix-bug reads issues with gh, authenticated however the session env
    provides it: `gh auth login` (keyring) or GH_TOKEN from harness/.envrc."""
    from .. import env as envmod
    senv = e.session_env() if e.usable else {**os.environ, **envmod.dotenv()}
    gh = shutil.which("gh", path=senv.get("PATH"))
    if not gh:
        yield Check("gh", NOTE, "not installed (brew install gh); fix-bug needs it")
        return
    r = subprocess.run([gh, "auth", "status"], capture_output=True, text=True, env=senv)
    how = "GH_TOKEN from harness/.envrc" if "GH_TOKEN" in envmod.dotenv() else "gh auth login"
    yield Check("gh", OK if r.returncode == 0 else FAIL,
                f"authenticated ({how})" if r.returncode == 0
                else "not authenticated: run `gh auth login`, or put GH_TOKEN in harness/.envrc")


def external_plugins(e):
    from .. import plugins
    for name, ok, detail in plugins.status(e.repo):
        yield Check(f"plugin {name}", OK if ok else FAIL, detail)


def notes(e):
    p = paths.notes_file()
    yield Check("notes", NOTE, str(p) if p.exists() else "none yet (created by discuss)")


CHECKS = [toolchain, binaries, lsp, opam_export, graph, session_config, no_leakage, git_hook, linters,
          perf_tools, github, external_plugins, notes]


def render(checks):
    t = Table(show_header=True, header_style="bold")
    t.add_column("check"); t.add_column("status"); t.add_column("detail")
    for c in checks:
        t.add_row(c.name, colored(c.status), c.detail)
    Console().print(t)


def doctor():
    """Check toolchain, graph, session config, git hook, linters, and that nothing leaked into default Claude sessions."""
    from .. import env as envmod
    e = envmod.detect()
    checks = [c for fn in CHECKS for c in fn(e)]
    render(checks)
    if any(c.status is Status.FAIL for c in checks):
        raise typer.Exit(1)
