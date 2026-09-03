"""PR review session: a computed navigation pack, then an interactive,
read-only Claude session pre-oriented on it."""
import shutil
import subprocess
import sys
from typing import Optional

import typer

from .. import agent

READ_ONLY = ["Edit", "Write", "NotebookEdit", "Bash"]


def review(pr: Optional[int] = typer.Option(None, "--pr", help="Pull request number in this repository."),
           checkout: bool = typer.Option(False, "--checkout",
                                         help="Check the PR out in place for the session (clean tree required) so "
                                              "LSP, the harness tools, and the pack's links see the PR's code. Your "
                                              "branch is restored when the session ends, however it ends."),
           done: bool = typer.Option(False, "--done",
                                     help="Recovery only: restore the branch if a session died without doing so."),
           open_: bool = typer.Option(False, "--open", help="Also open pack.md in VS Code (needs the `code` command)."),
           dry_run: bool = typer.Option(False, "--dry-run", help="Build the pack and print the session command, run nothing.")):
    """Review a PR in a dedicated, read-only Claude session.

    Builds harness/state/reviews/pr-<N>/ (pack.md: changed files by dune
    unit, interface changes, blast radius, reading order, change map, test
    candidates; diff.md: structural diff via difftastic; base/ and head/
    copies), then opens a Claude session with the pack and diff as its first
    message and Edit/Write/Bash removed. The session guides you through the
    change; it cannot modify anything. With --checkout the PR is checked out
    for the duration of the session only.
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

    stale = R.active_checkout(e.repo)
    if stale:
        typer.echo(f"review: PR #{stale['number']} is still checked out from an earlier session "
                   f"(was on {stale['previous']}); run `mina-agent review --done` first", err=True)
        raise typer.Exit(1)

    live = False
    if checkout and not dry_run:
        try:
            previous = R.checkout(e.repo, pr)
        except RuntimeError as ex:
            typer.echo(f"review: {ex}", err=True)
            raise typer.Exit(1)
        live = True
        typer.echo(f"checked out PR #{pr} for this session (was on {previous})", err=True)

    try:
        try:
            pack, pack_md, review_html = R.build(e.repo, pr, checked_out=live)
        except RuntimeError as ex:
            typer.echo(f"review: {ex}", err=True)
            raise typer.Exit(1)
        libs = sorted({c.unit["key"] for c in pack.files if c.unit and c.unit["kind"] == "lib"})
        typer.echo(f"PR #{pr}: {pack.meta['title']}\n  {len(pack.files)} file(s) in "
                   f"{len(libs)} librar{'y' if len(libs) == 1 else 'ies'}: {', '.join(libs) or '-'}\n"
                   f"  pack {pack_md}\n  review page {review_html}\n"
                   f"  PR code {'checked out' if live else 'not checked out (--checkout)'}", err=True)
        if open_:
            opener = "open" if sys.platform == "darwin" else ("xdg-open" if shutil.which("xdg-open") else None)
            if opener:
                subprocess.run([opener, str(review_html)])   # semantic diffs + clickable map in the browser
            else:
                typer.echo(f"--open: open {review_html} in a browser", err=True)
        msg = R.first_message(pack, live)
        if dry_run:
            print(f"[dry-run] first message: {len(msg)} characters, begins:\n" + msg[:1200] + "\n...")
            print("\n[dry-run] command:")
            for a in agent.interactive_argv(msg, e.repo, READ_ONLY)[2:]:
                print("   ", a[:140].replace("\n", " ") + ("..." if len(a) > 140 else ""))
            return
        try:
            rc = agent.run_interactive(msg, e, READ_ONLY)
        except KeyboardInterrupt:
            rc = 130
        typer.echo(f"review session ended (exit {rc}); pack stays at {pack_md}", err=True)
    finally:
        if live:
            try:
                a = R.done(e.repo)
                typer.echo(f"back on {a['previous']}", err=True)
            except RuntimeError as ex:
                typer.echo(f"review: could not restore your branch: {ex}\n"
                           "        fix the tree, then run `mina-agent review --done`", err=True)
