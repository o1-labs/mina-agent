"""Profiling session: instrumented build of a focus, ranked hotspots, zoom."""
from typing import Optional

import typer

from .. import agent, paths
from ..model import RestoreReport

RULES = """\
You are in a mina-agent profiling session inside the Mina monorepo. The
focus libraries listed below are compiled with landmarks instrumentation
(every top-level function is a probe), and every build the harness runs in
this session keeps them instrumented. All discussion-session tools apply;
in addition: profile_run, profile_top, profile_callers, profile_diff,
profile_status.

Method: find the code that matters and make it small enough to read.

1. Workload. Start with the cheapest candidate below that exercises the
   focus. profile_run reports how many focus functions ran and their share
   of the time; if that is near zero, the workload does not reach the focus,
   pick another. Say which workload and its cost before running it.
2. Rank. profile_top by self_ms, then by self_alloc_mb: time and allocation
   are different questions with different answers. Self excludes callees.
   Use profile_callers to attribute a hot function to the call sites that
   make it hot.
3. Zoom. A hot function too large to read at once (more than about 60 lines)
   gets finer windows: wrap sub-expressions as `expr [@landmark "name"]`
   or local bindings as `let[@landmark] f = ...`, run the same workload
   again, and rank the windows. Repeat until every region you report is
   both substantial (about 5% of total or more) and small enough to read
   in full. Windows are Edits: show the exact diff and ask first, as for
   any edit. Several sizable windows are fine; do not stop at one.
4. Read. Only then read the code of the regions that survived and explain
   what costs what, with the numbers.
5. Measure a change. After an optimization, profile_run the same workload
   and profile_diff against the earlier profile. A speedup is a measured
   delta, never an expectation.

Before finishing: remove the windows you added (they are yours), keep any
real optimization, and say which source files changed. The harness itself
restores the dune files it instrumented when this session ends.

Reading the numbers: times are milliseconds calibrated to CPU time;
instrumentation adds a per-call cost, so a tiny function called hundreds of
thousands of times looks worse than it is (judge by calls x cost and by
allocation as well); `load(<module>)` is module initialization; the ROOT node
is the whole run.
"""


def _report(rep: RestoreReport, out):
    """The restore report, one line per file that needs a human decision."""
    for f in rep.edited:
        out(f"  dune file edited during the session, left as is (still carries the landmarks stanza): {f}")
    for f in rep.still_dirty:
        out(f"  still differs from git after restore: {f}")
    for f in rep.source_edits:
        out(f"  source edit left in place (yours to keep or revert): {f}")
    for f in rep.windows_left:
        out(f"  [@landmark] windows left behind, remove them: {f}")


def _examples():
    """Epilog for --help: concrete invocations from the manifest's core
    libraries, so the examples are real names, not invented ones."""
    import tomllib
    with open(paths.MANIFEST, "rb") as fh:
        core = tomllib.load(fh).get("core", {})
    ex = [("--focus currency", "a library by its dune name"),
          ("--focus src/lib/staged_ledger/staged_ledger.ml", "a path inside one")]
    ex += [(f"--focus {name}", f"core library, {rec['dir']}") for name, rec in list(core.items())[:3]]
    ex += [("--focus mina_ledger --scope deps", "also instrument its direct dependencies"),
           ("--focus pickles --dry-run", "show the plan and workloads, instrument nothing")]
    lines = ["[bold]Examples[/bold]", ""]
    for args, why in ex:
        lines += [f"  mina-agent profile {args}", f"      {why}"]
    lines += ["", "Find a focus with [bold]mina-agent list libraries[/bold] (--filter <substring>, "
              "--inline-tests): every library with its directory, whether it has inline tests, "
              "and how many libraries depend on it."]
    return "\n".join(lines)


EXAMPLES = _examples()


def profile(focus: Optional[str] = typer.Option(None, "--focus", "-f",
                                                help="Library to profile: a dune library name (currency, "
                                                     "pickles, mina_ledger), a public name, or a source path "
                                                     "inside it (src/lib/currency/currency.ml). See Examples."),
            scope: str = typer.Option("lib", "--scope", help="Which libraries to instrument: lib (the focus), "
                                                          "deps (plus its direct local dependencies), cone "
                                                          "(plus its whole local dependency cone)."),
            restore: bool = typer.Option(False, "--restore", help="End a session left behind by a crash: "
                                                                  "put the instrumented dune files back."),
            dry_run: bool = typer.Option(False, "--dry-run", help="Show the plan and first message; instrument nothing."),
            headless: bool = typer.Option(False, "--headless", help="Run the profile_hunt phase instead of the TUI: "
                                                                    "same session and walls, driven by the SDK, no prompts."),
            max_turns: Optional[int] = typer.Option(None, "--max-turns", help="Headless only: override the phase's turn budget."),
            trace: bool = typer.Option(False, "--trace", help="Headless only: print and save the trajectory evidence.")):
    """Start a profiling session on a library, interactive or headless.

    The focus libraries get a temporary landmarks stanza in their dune files
    (restored when the session ends), builds in the session are
    instrumented, and the model runs workloads under the profiler, ranks
    hotspots, and zooms in with finer windows until the hot code is small
    enough to read. Nothing is installed: landmarks is vendored into
    harness/state by setup.
    """
    from .. import env as envmod, graph, landmarks, profile as P
    e = envmod.detect()
    if restore:
        rep = P.restore(e.repo)
        print(f"restored {len(rep.restored)} dune file(s)" + (f": {rep.note}" if rep.note else ""))
        _report(rep, print)
        return
    if not e.usable:
        raise envmod.NoToolchain(e)
    if not focus:
        typer.echo("--focus is required (a library name or a path inside one)", err=True)
        raise typer.Exit(2)
    if scope not in ("lib", "deps", "cone"):
        typer.echo("--scope must be lib, deps, or cone", err=True)
        raise typer.Exit(2)
    if P.active(e.repo):
        typer.echo("a profiling session is already active; run mina-agent profile --restore first", err=True)
        raise typer.Exit(2)
    if not landmarks.present(e.repo):
        d, msg = landmarks.fetch(e)
        typer.echo(f"landmarks: {d} ({msg})", err=True)
    if headless:
        from .. import phases
        from .run import _run_phase
        phase = next(p for p in phases.all_phases() if p.name == "profile_hunt")
        _run_phase(phase, {"focus": focus}, trace=trace, dry_run=dry_run, max_turns=max_turns,
                   max_budget_usd=None, model=None, scope=scope)
        return
    g = graph.load_or_derive(e)
    lib = P.resolve_focus(g, focus)
    libs = P.scope_libraries(g, lib, scope)
    from .discuss import RULES as DISCUSS_RULES
    notes = paths.notes_file(e.repo)
    first_message = "\n".join([DISCUSS_RULES.format(notes=str(notes)), RULES,
                               P.session_block(e.repo, g, lib, scope, libs)])
    if dry_run:
        print(f"[dry-run] would instrument {len(libs)} dune file(s):")
        for l in libs[:40]:
            print(f"   {l}  {g['libraries'][l]['dir']}/dune")
        print("\n[dry-run] first message:\n" + first_message)
        print("\n[dry-run] command:")
        for a in agent.interactive_argv(first_message, e.repo)[2:]:
            print("   ", a[:160].replace("\n", " ") + ("..." if len(a) > 160 else ""))
        return
    s = P.start(e.repo, g, lib, scope, libs)
    typer.echo(f"instrumented {len(s.injected)} dune file(s) for {len(libs)} librar{'y' if len(libs) == 1 else 'ies'}; "
               f"session {P.session_file(e.repo)}", err=True)
    for l, why in s.skipped:
        typer.echo(f"  skipped {l}: {why}", err=True)
    try:
        rc = agent.run_interactive(first_message, e)
    finally:
        rep = P.restore(e.repo)
        typer.echo(f"session ended; restored {len(rep.restored)} dune file(s), "
                   f"{len(rep.profiles)} profile(s) kept in {P.state_dir(e.repo)}", err=True)
        _report(rep, lambda m: typer.echo(m, err=True))
