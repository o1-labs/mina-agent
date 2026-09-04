"""Interactive session with the harness tools, pre-oriented and walled."""
from typing import Optional

import typer

from .. import agent, paths

NOTES_TEMPLATE = """# mina-agent notes

Conclusions recorded by `mina-agent discuss` sessions. One entry per line,
newest last:

- [YYYY-MM-DDTHH:MM:SSZ] note text
"""

RULES = """\
You are in a mina-agent discussion session inside the Mina monorepo.

What you may do without asking: read any file; use the mina-harness tools
build, check, check_dependents, errors, type_at, definition, find_module,
usages, deps_of, dependents_of, tests_for, library_of, env_status, and the
LSP tool. Use them instead of guessing at types, definitions, who uses a
binding, or whether something compiles.

What you must ask before doing: any Edit (state the exact file path, show the
exact diff, wait for explicit approval), and any `test` or `test_one` call
(they can take minutes; say which test and its cost first).

What is not possible here: editing build configuration or the Rust
proof-systems boundary; running dune, opam, nix, cargo, make, pip, the OCaml
compilers, or the opam-switch scripts directly.
Those are denied structurally, do not attempt workarounds.

If something in this harness itself misbehaves (a mina-harness tool errors
or answers wrongly, a hook or the session does something unexpected), offer
to file a bug report for o1-labs/mina-agent: the harness-bug-report skill
gathers the evidence and files it with gh once the user agrees on the text.

When the discussion produces a conclusion worth keeping (a diagnosis, a plan,
a constraint), append one line to {notes} in the form
`- [<UTC timestamp>] <text>` after confirming with the user.
"""


def _count_notes(p):
    if not p.exists():
        return 0
    return sum(1 for l in p.read_text().splitlines() if l.startswith("- ["))


def discuss(focus: Optional[str] = typer.Option(None, "--focus", "-f",
                                                help="A source path or library to orient on: its build "
                                                     "status and test candidates are injected up front."),
            dry_run: bool = typer.Option(False, "--dry-run", help="Print the claude command, run nothing.")):
    """Start an interactive Claude session with the harness tools and walls.

    Read-only by default: the model asks before editing or running tests, and
    the build-config / Rust-boundary edit rules and the raw-toolchain deny
    rules apply structurally. Conclusions land in harness/state/NOTES.md.
    """
    from .. import env as envmod, graph, tools
    e = envmod.require()
    graph.load_or_derive(e)
    notes = paths.notes_file()
    if not notes.exists():
        notes.write_text(NOTES_TEMPLATE)

    orient = [RULES.format(notes=str(notes)), "## Current state", ""]
    orient.append(f"Environment: {e.summary()}.")
    if focus:
        try:
            u = tools.library_of(focus)
            orient.append(f"Focus {focus}: {u['kind']} unit {u['key']} in {u['dir']}.")
            target = u["dir"] if u["kind"] == "lib" else focus
            b = tools.build(target)
            orient.append(f"build {target}: ok={b['ok']} errors={len(b['errors'])} "
                          f"warnings={len(b['warnings'])} in {b['elapsed_s']}s.")
            for err in b["errors"][:5]:
                orient.append(f"  {err['file']}:{err['line']}:{err['col_start']} {err['message']}")
            tf = tools.tests_for(focus)
            orient.append("tests_for candidates: " + ", ".join(
                f"{c['name']} [{c['cost']}]" for c in tf["candidates"][:5]))
        except Exception as ex:
            orient.append(f"Focus {focus}: could not orient ({ex}).")
    if notes.exists():
        tail = [l for l in notes.read_text().splitlines() if l.startswith("- [")][-10:]
        if tail:
            orient += ["", "Recent notes:"] + tail
    first_message = "\n".join(orient)

    if dry_run:
        print("[dry-run] first message:\n" + first_message)
        print("\n[dry-run] command:")
        for a in agent.interactive_argv(first_message, e.repo)[2:]:
            print("   ", a[:160].replace("\n", " ") + ("..." if len(a) > 160 else ""))
        return
    before = _count_notes(notes)
    typer.echo(f"notes file: {notes}", err=True)
    rc = agent.run_interactive(first_message, e)
    added = _count_notes(notes) - before
    typer.echo(f"session ended (exit {rc}); {added} note(s) added to {notes.name}", err=True)
