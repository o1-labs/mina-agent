"""Remove everything the harness generated under harness/state."""
import fcntl
import shutil
from pathlib import Path

import typer

from .. import paths


def _size(p):
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _human(n):
    for unit in ("B", "K", "M", "G"):
        if n < 1024 or unit == "G":
            return f"{n:.0f}{unit}"
        n /= 1024


def clean(yes: bool = typer.Option(False, "--yes", "-y", help="Remove without asking.")):
    """Remove harness/state: the derived graph, compiled tools, logs, notes, the
    generated LSP plugin, and cloned plugins.

    Everything there is generated; `mina-agent setup && mina-agent init`
    recreates it. The installed tool, the git pre-commit hook, and the repo
    itself are untouched. Refuses while a harness dune command holds the lock.
    """
    state = Path(paths.repo_root()) / "harness" / "state"
    if not state.is_dir() or not any(state.iterdir()):
        print(f"{state}: nothing to remove")
        return
    from .. import profile as P
    if P.active(str(state.parent.parent)):
        typer.echo("a profiling session is active (harness/state/profile/session.json holds the original dune "
                   "files); run mina-agent profile --restore first", err=True)
        raise typer.Exit(2)
    lock = state / "dune.lock"
    if lock.exists():
        with open(lock, "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                typer.echo("a harness dune command is running (dune.lock is held); try again when it finishes",
                           err=True)
                raise typer.Exit(2)
            fcntl.flock(fh, fcntl.LOCK_UN)
    total = 0
    for p in sorted(state.iterdir(), key=lambda p: p.name):
        n = _size(p)
        total += n
        print(f"  {_human(n):>6}  {p.name}{'/' if p.is_dir() else ''}")
    print(f"  {_human(total):>6}  total in {state}")
    if not yes and not typer.confirm("remove all of it?", default=False):
        print("kept")
        raise typer.Exit(1)
    shutil.rmtree(state)
    print("removed. To regenerate:\n\n    mina-agent setup && mina-agent init\n")
