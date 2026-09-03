"""PR review session: a computed navigation pack, then an interactive,
read-only Claude session pre-oriented on it."""
import shutil
import subprocess
from typing import Optional

import typer

from .. import agent

READ_ONLY = ["Edit", "Write", "NotebookEdit", "Bash"]


def review(pr: Optional[int] = typer.Option(None, "--pr", help="Pull request number in this repository."),
           checkout: bool = typer.Option(False, "--checkout",
                                         help="Check the PR out in place (clean tree required) so LSP, the harness "
                                              "tools, and the pack's links see the PR's code. Undo with --done."),
           done: bool = typer.Option(False, "--done", help="Return to the branch you were on before --checkout."),
           open_: bool = typer.Option(False, "--open", help="Also open pack.md in VS Code (needs the `code` command)."),
           dry_run: bool = typer.Option(False, "--dry-run", help="Build the pack and print the session command, run nothing.")):
    """Review a PR in a dedicated, read-only Claude session.

    Builds harness/state/reviews/pr-<N>/ (pack.md: changed files by dune
    unit, interface changes, blast radius, reading order, change map, test
    candidates; diff.md: structural diff via difftastic; base/ and head/
    copies), then opens a Claude session with the pack and diff as its first
    message and Edit/Write/Bash removed. The session guides you through the
    change; it cannot modify anything.
    """
    from .. import env as envmod, review as R
    e = envmod.detect()
    if e.mode == "none":
        typer.echo("no usable toolchain: " + "; ".join(e.reasons), err=True)
        raise typer.Exit(3)
    if done:
        try:
            a = R.done(e.repo)
        except RuntimeError as ex:
            typer.echo(f"review: {ex}", err=True)
            raise typer.Exit(1)
        print(f"back on {a['previous']} (PR #{a['number']} no longer checked out)")
        return
    if pr is None:
        typer.echo("review: --pr is required (or --done)", err=True)
        raise typer.Exit(2)
    if checkout:
        try:
            previous = R.checkout(e.repo, pr)
        except RuntimeError as ex:
            typer.echo(f"review: {ex}", err=True)
            raise typer.Exit(1)
        typer.echo(f"checked out PR #{pr} (was on {previous}); `mina-agent review --done` to return", err=True)
    live = bool(R.active_checkout(e.repo)) and R.active_checkout(e.repo)["number"] == pr
    try:
        pack, pack_md, diff_md = R.build(e.repo, pr, checked_out=live)
    except RuntimeError as ex:
        typer.echo(f"review: {ex}", err=True)
        raise typer.Exit(1)
    libs = sorted({c.unit["key"] for c in pack.files if c.unit and c.unit["kind"] == "lib"})
    typer.echo(f"PR #{pr}: {pack.meta['title']}\n  {len(pack.files)} file(s) in "
               f"{len(libs)} librar{'y' if len(libs) == 1 else 'ies'}: {', '.join(libs) or '-'}\n"
               f"  pack {pack_md}\n  diff {diff_md}\n  PR code {'checked out' if live else 'not checked out (--checkout)'}", err=True)
    if open_:
        if shutil.which("code"):
            subprocess.run(["code", str(pack_md)])
        else:
            typer.echo("--open: `code` is not on PATH (VS Code: Shell Command: Install 'code' command in PATH)", err=True)
    msg = R.first_message(pack, live)
    if dry_run:
        print(f"[dry-run] first message: {len(msg)} characters, begins:\n" + msg[:1200] + "\n...")
        print("\n[dry-run] command:")
        for a in agent.interactive_argv(msg, e.repo, READ_ONLY)[2:]:
            print("   ", a[:140].replace("\n", " ") + ("..." if len(a) > 140 else ""))
        return
    rc = agent.run_interactive(msg, e, READ_ONLY)
    typer.echo(f"review session ended (exit {rc}); pack stays at {pack_md}"
               + ("; run `mina-agent review --done` to leave the PR branch" if live else ""), err=True)
