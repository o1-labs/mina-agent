"""Run CI's lint jobs locally."""
import typer
from rich.console import Console
from rich.table import Table

from .. import lint as L
from ..model import Status

STATUS_COLOR = {Status.OK: "green", Status.FAIL: "red", Status.SKIP: "yellow", Status.NOTE: "cyan"}


def colored(status: Status) -> str:
    return f"[{STATUS_COLOR[status]}]{status}[/{STATUS_COLOR[status]}]"


def render(files, results, scope):
    t = Table(show_header=True, header_style="bold", title=f"lint ({scope}: {len(files)} file(s))")
    t.add_column("check"); t.add_column("CI job"); t.add_column("status"); t.add_column("detail")
    for r in results:
        detail = r.detail
        if r.files and r.status is Status.FAIL:
            detail += "\n" + "\n".join("  " + f for f in r.files[:20])
        if r.fix:
            detail += f"\nfix: {r.fix}"
        t.add_row(r.name, r.job, colored(r.status), detail)
    Console().print(t)


def lint(all_: bool = typer.Option(False, "--all", help="Whole tree instead of the staged files."),
         fix: bool = typer.Option(False, "--fix", help="Reformat failing OCaml files in place."),
         history: int = typer.Option(0, "--history", help="Show the last N lint runs from the log instead of running.")):
    """Mirror CI's Lint jobs on the staged files, so CI never rejects a commit for formatting or lint.

    ocamlformat on the index blobs (what `make check-format` runs, scoped),
    require-ppxs, codeowners, rfcs, snarky submodule, shellcheck, hadolint,
    dhall, cargo check, archive upgrade, changelog. Checks that need a tool
    this machine lacks say so instead of passing silently.
    """
    from .. import env as envmod
    e = envmod.require()
    if history:
        for rec in L.history(e.repo, history):
            fails = [r["name"] for r in rec["results"] if r["status"] == "fail"]
            fixed = any(r["status"] == "note" and r["name"] == "ocamlformat" for r in rec["results"])
            files = rec["files"] if isinstance(rec["files"], list) else f"{rec['files']} files"
            verdict = "BLOCKED " + ",".join(fails) if rec["blocked"] else ("fixed" if fixed else "passed")
            print(f"{rec['ts']}  {rec['caller']:10s} {rec['scope']:6s} head={rec['head']}  {verdict}  {files}")
        return
    scope = "all" if all_ else "staged"
    files, results = L.run(e, scope=scope, fix=fix)
    render(files, results, scope)
    if any(r.status is Status.FAIL for r in results):
        raise typer.Exit(1)
