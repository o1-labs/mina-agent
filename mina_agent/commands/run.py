"""Headless phase commands, one per data/phases/*.md, generated from the
front matter: each declared arg becomes a required --<arg> option."""
import datetime as dt
import inspect
import json
import os
import sys
from typing import Optional

import typer

from .. import agent, paths, phases
from ..model import Phase


def _run_phase(phase, args, *, trace, dry_run, max_turns, max_budget_usd, model):
    from .. import env as envmod, graph, profile as P
    e = envmod.require()
    g = graph.load_or_derive(e)
    prompt = phases.render(phase, args)
    session = None
    if phase.session == "profile":
        # instrument the focus for the whole run; the model gets the same
        # Session block an interactive profile session starts with
        from .. import tools
        from .profile import _resolve_focus, _workloads
        if P.active(e.repo):
            typer.echo("a profiling session is already active; run mina-agent profile --restore first", err=True)
            raise typer.Exit(2)
        lib = _resolve_focus(tools, g, args["focus"])
        plan = {"focus": lib, "dirs": [g["libraries"][lib]["dir"]]}
        block = [f"\n## Session\n\nFocus: library {lib} in {g['libraries'][lib]['dir']}, instrumented.",
                 "Workload candidates (cheapest and most direct first):"]
        block += [f"  profile_run(\"{spec}\")  [{cost}]  {why}" for spec, why, cost in _workloads(tools, g, plan)]
        prompt += "\n".join(block) + "\n"
        session = (lib, [lib])
    options = agent.build_options(phase, e, max_turns=max_turns, max_budget_usd=max_budget_usd,
                                  model=model)
    if dry_run:
        print("[dry-run] options:\n" + json.dumps(agent.options_summary(options), indent=1, default=str))
        print("\n[dry-run] prompt:\n" + prompt)
        print("\n[dry-run] system prompt addition:\n" + agent.system_addition())
        return
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = paths.logs_dir(e.repo) / f"{stamp}-{phase.name}.jsonl"
    print(f"phase {phase.name}  args {args}  log {os.path.relpath(log_path, e.repo)}\n")
    if session:
        s = P.start(e.repo, g, session[0], "lib", session[1])
        print(f"profiling session: instrumented {len(s['injected'])} dune file(s) for {session[0]}\n")
    try:
        traj = agent.run_headless(prompt, options, log_path,
                                  on_call=lambda t, c: print(t.progress_line(c), flush=True))
    finally:
        if session:
            rep = P.restore(e.repo)
            print(f"\nprofiling session ended: restored {len(rep['restored'])} dune file(s), "
                  f"{len(rep['profiles'])} profile(s) kept")
            for f in rep["source_edits"]:
                print(f"  source edit left in place: {f}")
    if agent.STDERR:
        sys.stderr.write("\n".join(agent.STDERR[-20:]) + "\n")
    finish(traj, phase.name, str(log_path), trace)


def finish(traj, phase_name, log_path, trace):
    r = traj.result or {}
    print("\n=== result ===")
    print((r.get("result") or "(no final assistant text)").strip())
    if not traj.result or traj.tools_available is None:
        print("\nWARNING: init or result message missing from the stream; the run did not "
              "complete normally or the SDK message shapes changed.")
    print("\n" + traj.stats_line())
    if trace:
        md = traj.summary_md(phase_name, log_path)
        out = os.path.splitext(log_path)[0] + ".summary.md"
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(md)
        print("\n=== trace ===\n" + md)
        print(f"summary written to {out}")
    ok = r.get("subtype") == "success" and not r.get("is_error")
    if not ok:
        raise typer.Exit(1)


def make_command(phase: Phase):
    arg_names = list(phase.args)

    def command(**kw):
        args = {a: kw.pop(a) for a in arg_names}
        _run_phase(phase, args, **kw)

    params = [inspect.Parameter(a, inspect.Parameter.KEYWORD_ONLY, annotation=str,
                                default=typer.Option(..., f"--{a.replace('_', '-')}",
                                                     help=f"phase argument {{{{{a}}}}}"))
              for a in arg_names]
    params += [
        inspect.Parameter("trace", inspect.Parameter.KEYWORD_ONLY, annotation=bool,
                          default=typer.Option(False, "--trace", help="Print and save the trajectory evidence.")),
        inspect.Parameter("dry_run", inspect.Parameter.KEYWORD_ONLY, annotation=bool,
                          default=typer.Option(False, "--dry-run", help="Print options and prompts, run nothing.")),
        inspect.Parameter("max_turns", inspect.Parameter.KEYWORD_ONLY, annotation=Optional[int],
                          default=typer.Option(None, "--max-turns", help=f"Override the phase's {phase.max_turns}.")),
        inspect.Parameter("max_budget_usd", inspect.Parameter.KEYWORD_ONLY, annotation=Optional[float],
                          default=typer.Option(None, "--max-budget-usd", help=f"Override the phase's {phase.max_budget_usd}.")),
        inspect.Parameter("model", inspect.Parameter.KEYWORD_ONLY, annotation=Optional[str],
                          default=typer.Option(None, "--model", help="Model alias or id.")),
    ]
    setattr(command, "__signature__", inspect.Signature(params))
    command.__annotations__ = {p.name: p.annotation for p in params}
    command.__name__ = phase.name
    command.__doc__ = (f"{phase.summary}\n\n"
                       f"tools: {', '.join(phase.allowed_tools)}\n"
                       f"removed: {', '.join(phase.disallowed_tools)}\n"
                       f"limits: {phase.max_turns} turns, ${phase.max_budget_usd}  "
                       f"permission mode: {phase.permission_mode}")
    return command


def register(app):
    """One command per valid phase. A phase whose front-matter cannot become
    a command (e.g. an arg name that is not an identifier) is reported and
    skipped rather than breaking every other command."""
    for phase in phases.all_phases():
        try:
            app.command(phase.command_name)(make_command(phase))
        except (ValueError, TypeError) as ex:
            sys.stderr.write(f"mina-agent: skipping phase {phase.name}: {ex}\n")
