"""mina-agent command line.

Human commands appear in --help. Plumbing that prompts, hooks, and the MCP
registration call (serve, hook, exec, derive, list) is registered hidden: it
runs and answers --help, but stays out of the top-level surface.
"""
import sys

import typer
from rich.console import Console
from rich.panel import Panel

from . import __version__

HIDDEN = {"serve", "hook", "exec", "derive", "list"}

REMARK = """\
Typical workflow:

  1. mina-agent setup             build the graph tool, verify dune/merlin/claude
  2. mina-agent init              register the MCP server and install the walls
  3. mina-agent doctor            confirm everything is wired
  4. mina-agent fix-build-error --target src/lib/<lib> --trace
  5. mina-agent discuss           interactive session with the harness tools
  6. mina-agent trace <log>       trajectory evidence for any past run

Run mina-agent <command> --help for details."""


class _Group(typer.core.TyperGroup):
    def format_help(self, ctx, formatter):
        super().format_help(ctx, formatter)
        Console(stderr=False).print(Panel(REMARK, title="Remark", title_align="left",
                                          border_style="dim", padding=(1, 2)))


app = typer.Typer(
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

from .commands import setup, init, doctor, status, discuss, trace  # noqa: E402
from .commands import serve, hook, exec_, derive, list_            # noqa: E402
from .commands import run as run_cmd                                # noqa: E402

app.command()(setup.setup)
app.command()(init.init)
app.command()(doctor.doctor)
app.command()(status.status)
app.command()(discuss.discuss)
app.command()(trace.trace)
run_cmd.register(app)                       # one command per data/phases/*.md
app.command("serve", hidden=True)(serve.serve)
app.add_typer(hook.app, name="hook", hidden=True)
app.command("exec", hidden=True, context_settings={"allow_extra_args": True,
                                                     "ignore_unknown_options": True})(exec_.exec_)
app.command("derive", hidden=True)(derive.derive)
app.add_typer(list_.app, name="list", hidden=True)


if __name__ == "__main__":
    app()
