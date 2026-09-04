"""Interactive development session: edits accepted, shell allowlisted."""
from typing import Optional

import typer

from .. import agent, paths
from .discuss import NOTES_TEMPLATE, orientation

RULES = """\
You are in a mina-agent development session inside the Mina monorepo. This
is where code gets written: edits are accepted without asking, and tests
may be run when they are the right check.

Build, type-check and test through the mina-harness tools: after each edit
of a .ml/.mli the harness type-checks it and returns diagnostics (read them
before anything else); build for a target, check_dependents after an
interface change, errors for the fast inner loop, tests_for then test or
test_one. Read code with find_module, usages, definition, type_at, deps_of,
dependents_of, and the LSP tool. Use them instead of guessing.

The shell is an allowlist enforced by a hook: git, gh, mina-agent
{subcommands}, and read-only utilities ({heads}). Anything else is denied,
and the raw toolchain (dune, opam, nix, cargo, make) always is. Some allowed
commands still ask first (git push, rebase, reset, gh pr create); that is
the user's call at the keyboard, so ask in words before you reach for them.

Commits are the user's own, under the configured git identity, with no
Co-Authored-By or Generated-with lines. Before committing: check every
edited file, run the relevant test, and let the pre-commit lint gate run
(if it blocks on formatting, `mina-agent lint --fix` then git add). Never
push, rebase, reset or amend unless the user asks in this session.

What is not possible here: editing build configuration or the Rust
proof-systems boundary. Those are denied structurally.

To measure performance (is this faster, where does the time or allocation
go, does a claim hold), use the benchmarking skill: it chooses between the
uninstrumented tools (perf_measure, perf_compare: samply sample share,
exact allocation, peak RSS, wall clock) and a landmarks profiling session.

If something in this harness itself misbehaves, offer to file a bug report
for o1-labs/mina-agent (the harness-bug-report skill).

When work produces a conclusion worth keeping (a diagnosis, a decision, a
constraint), append one line to {notes} in the form
`- [<UTC timestamp>] <text>` after confirming with the user.
"""


def develop(focus: Optional[str] = typer.Option(None, "--focus", "-f",
                                                help="A source path or library to orient on: its build "
                                                     "status and test candidates are injected up front."),
            dry_run: bool = typer.Option(False, "--dry-run", help="Print the claude command, run nothing.")):
    """Start an interactive development session: edits accepted, developer shell only.

    The opposite of discuss: Edit/Write run without asking (permission mode
    acceptEdits), tests may be run, and the shell is an allowlist of
    developer commands (git, gh, mina-agent lint/list/status/doctor,
    read-only utilities) enforced by a hook; the raw toolchain and build
    configuration stay walled. The list lives in manifest.toml [develop].
    """
    from .. import env as envmod, graph
    e = envmod.require()
    graph.load_or_derive(e)
    notes = paths.notes_file()
    if not notes.exists():
        notes.write_text(NOTES_TEMPLATE)
    cfg = agent.develop_config()
    rules = RULES.format(notes=str(notes), subcommands="/".join(cfg["mina_agent_subcommands"]),
                         heads=", ".join(h for h in cfg["bash_heads"] if h not in ("git", "gh")))
    first_message = "\n".join([rules, *orientation(e, focus, notes)])
    if dry_run:
        print("[dry-run] first message:\n" + first_message)
        print("\n[dry-run] command:")
        for a in agent.interactive_argv(first_message, e.repo, develop=True)[2:]:
            print("   ", a[:160].replace("\n", " ") + ("..." if len(a) > 160 else ""))
        return
    typer.echo(f"development session; notes file: {notes}", err=True)
    rc = agent.run_interactive(first_message, e, develop=True)
    typer.echo(f"session ended (exit {rc})", err=True)
