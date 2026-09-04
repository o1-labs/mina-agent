"""mina-agent command line.

Human commands appear in --help. Plumbing that prompts, hooks, and the MCP
registration call (serve, hook, exec, derive, list) is registered hidden: it
runs and answers --help, but stays out of the top-level surface.
"""
import sys

import typer
from typer.core import TyperGroup
from rich.console import Console
from rich.panel import Panel

from . import __version__
from .env import NoToolchain

HIDDEN = {"serve", "hook", "exec", "derive", "list"}

REMARK = """\
Typical workflow:

  1. mina-agent setup             build the graph tool, verify dune/merlin/claude
  2. mina-agent init              register the MCP server and install the walls
  3. mina-agent doctor            confirm everything is wired
  4. mina-agent fix-build-error --target src/lib/<lib> --trace
  5. mina-agent discuss           interactive session with the harness tools (read-only by default)
     mina-agent develop           the same with edits accepted and a developer shell: where code is written
  6. mina-agent trace <log>       trajectory evidence for any past run
  7. mina-agent lint              CI's lint jobs on the staged files (also the git pre-commit hook)
  8. mina-agent dashboard --open  live browser view of runs, hooks, denials, and the lint gate
  9. mina-agent profile --focus <library>   profiling session: instrumented build, ranked hotspots, zoom
 10. mina-agent clean             remove everything generated (harness/state); setup + init rebuild it

Run mina-agent <command> --help for details."""


class _Group(TyperGroup):
    def format_help(self, ctx, formatter):
        super().format_help(ctx, formatter)
        Console(stderr=False).print(Panel(REMARK, title="Remark", title_align="left",
                                          border_style="dim", padding=(1, 2)))


class _App(typer.Typer):
    """typer app that maps NoToolchain (from env.require) to exit 3 with the
    reasons on stderr, so no command carries that prologue itself."""

    def __call__(self, *args, **kwargs):
        try:
            return super().__call__(*args, **kwargs)
        except NoToolchain as ex:
            sys.stderr.write(f"mina-agent: {ex}\n")
            raise SystemExit(3)


app = _App(
    cls=_Group,
    help="Structural agent harness for the Mina monorepo.",
    no_args_is_help=True,
    context_settings={"help_option_names": ["-h", "--help"]},
    rich_markup_mode="rich",
)


def _version(value):
    if value:
        print(f"mina-agent {__version__}")
        raise typer.Exit()


@app.callback()
def _main(ctx: typer.Context,
          version: bool = typer.Option(False, "--version", "-V", callback=_version, is_eager=True,
                                       help="Show version and exit.")):
    """Structural agent harness for the Mina monorepo."""
    if ctx.invoked_subcommand not in HIDDEN and ctx.invoked_subcommand is not None:
        from . import banner
        sys.stderr.write(banner.render())


# ── register commands ─────────────────────────────────────────────────

from .commands import setup, init, doctor, status, discuss, develop, trace, lint, dashboard, clean, profile  # noqa: E402
from .commands import serve, hook, exec_, derive, list_            # noqa: E402
from .commands import run as run_cmd                                # noqa: E402

app.command()(setup.setup)
app.command()(init.init)
app.command()(doctor.doctor)
app.command()(status.status)
app.command()(discuss.discuss)
app.command()(develop.develop)
app.command()(trace.trace)
app.command()(lint.lint)
app.command()(dashboard.dashboard)
app.command(epilog=profile.EXAMPLES)(profile.profile)
app.command()(clean.clean)
run_cmd.register(app)                       # one command per data/phases/*.md
app.command("serve", hidden=True)(serve.serve)
app.add_typer(hook.app, name="hook", hidden=True)
app.command("exec", hidden=True, context_settings={"allow_extra_args": True,
                                                     "ignore_unknown_options": True})(exec_.exec_)
app.command("derive", hidden=True)(derive.derive)
app.add_typer(list_.app, name="list", hidden=True)


if __name__ == "__main__":
    app()
