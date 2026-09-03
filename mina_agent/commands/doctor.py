"""Verify the full harness setup and report."""
import json
import os
import shutil
import subprocess

import typer
from rich.console import Console
from rich.table import Table

from .. import agent, paths


def doctor(skip_mcp: bool = typer.Option(False, "--skip-mcp",
                                          help="Skip the `claude mcp get` connectivity check.")):
    """Check toolchain, graph tool, derived graph, settings, hooks, and MCP registration."""
    from .. import env as envmod, graph
    rows = []

    def row(name, ok, detail):
        rows.append((name, ok, detail))

    e = envmod.detect()
    row("toolchain", e.mode != "none",
        f"mode={e.mode} activated={e.activated} dune={e.dune_version} ocaml={e.ocaml}")
    b = e.build_dir or {}
    row("_build provenance", not (b.get("built_by") and b["built_by"] != e.mode),
        f"built_by={b.get('built_by')} exists={b.get('exists')}")
    for w in e.warnings:
        row("warning", None, w)
    if e.mode != "none":
        aenv = e.activate()
        for name in ("ocamlmerlin", "claude"):
            p = shutil.which(name, path=aenv.get("PATH"))
            row(name, bool(p), p or "not on PATH")
        from .. import lsp
        p, source = lsp.resolve(e)
        row("ocamllsp", True if p else None, f"{p} ({source})" if p else source)
        gen = lsp.plugin_dir(e.repo)
        if p:
            row("lsp plugin", bool(gen) and lsp.linked(e.repo),
                f"{gen} linked from {paths.SKILLS_DIR_LINK}" if gen and lsp.linked(e.repo)
                else "not generated or not linked; run mina-agent init")
        drift = subprocess.run([os.path.join(e.repo, "_opam", "bin", "check_opam_switch"), "opam.export"],
                               cwd=e.repo, capture_output=True, text=True)
        if drift.returncode == 0 and "not a superset" not in drift.stdout + drift.stderr:
            row("opam.export", True, "project switch matches the export")
        else:
            miss = [l.strip() for l in (drift.stdout + drift.stderr).splitlines() if "Could not find" in l]
            row("opam.export", None, "; ".join(miss) or "check_opam_switch unavailable"
                + " (make build's check_opam_switch will fail; set DISABLE_CHECK_OPAM_SWITCH=true)")
    binp = agent.mina_agent_bin()
    row("mina-agent", bool(shutil.which("mina-agent")), binp)
    tool = paths.describe_dune_bin(e.repo)
    row("describe-dune", tool.exists(), str(tool) if tool.exists() else "run mina-agent setup")
    dj = paths.derived_json(e.repo)
    if dj.exists() and tool.exists() and e.mode != "none":
        fresh = graph.check(e)
        row("derived graph", fresh, str(dj) + ("" if fresh else " is stale; rerun mina-agent init"))
    else:
        row("derived graph", False, "missing; run mina-agent setup && mina-agent init")
    st = paths.settings_local(e.repo)
    if st.exists():
        s = json.load(open(st))
        hooks = s.get("hooks", {})
        deny = s.get("permissions", {}).get("deny", [])
        wanted = agent.deny_rules()
        missing = [d for d in wanted if d not in deny]
        row("deny rules", not missing, f"{len(deny)} present" + (f"; missing {missing}" if missing else ""))
        pointing = all(binp in h.get("command", "") for ms in hooks.values() for m in ms for h in m["hooks"])
        row("hooks", bool(hooks) and pointing,
            f"{', '.join(sorted(hooks)) or 'none'}" + ("" if pointing else "; not pointing at this binary"))
    else:
        row("settings", False, f"{st} missing; run mina-agent init")
    if not skip_mcp:
        r = subprocess.run(["claude", "mcp", "get", paths.MCP_SERVER_NAME], capture_output=True, text=True)
        out = r.stdout + r.stderr
        row("mcp server", "Connected" in out, out.strip().splitlines()[2].strip() if r.returncode == 0
            and len(out.splitlines()) > 2 else "not registered; run mina-agent init")
    hook = paths.git_hook(e.repo)
    ours = hook.exists() and "mina-agent" in hook.read_text() and binp in hook.read_text()
    row("git pre-commit", ours, str(hook) if ours else f"{hook} missing or foreign; run mina-agent init")
    for tool_name, job in (("shellcheck", "Lint/Bash"), ("hadolint", "Lint/Docker")):
        found = shutil.which(tool_name)
        row(tool_name, True if found else None, found or f"not installed; {job} will be skipped locally and run by CI")
    from .. import dhall
    row("dhall", *dhall.status(e.repo))
    notes = paths.notes_file(e.repo)
    row("notes", None, str(notes) if notes.exists() else "none yet (created by discuss)")

    t = Table(show_header=True, header_style="bold")
    t.add_column("check"); t.add_column("status"); t.add_column("detail")
    bad = 0
    for name, ok, detail in rows:
        mark = "[green]ok[/green]" if ok else ("[yellow]note[/yellow]" if ok is None else "[red]FAIL[/red]")
        bad += ok is False
        t.add_row(name, mark, detail)
    Console().print(t)
    if bad:
        raise typer.Exit(1)
