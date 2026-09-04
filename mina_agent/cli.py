"""mina-agent command line.

Grouped by who runs what and when:

  discuss | develop | profile   the interactive sessions (the product)
  run <phase> ...               headless phases, one per data/phases/*.md
  show <what>                   read-only inspection (status, doctor, tools, tests, phases, libraries)
  lint                          the commit gate, also what the git hook calls
  trace <log> | dashboard       evidence from past runs
  admin <op>                    install lifecycle (setup, init, clean, derive)
  serve | hook | exec           plumbing the harness writes into settings and hooks (hidden)

`doctor` and `status` stay as visible top-level aliases: they are what you type
first when something is broken. The former flat names remain as hidden aliases
that print where the command moved.
"""
import sys

import typer
from typer.core import TyperGroup
from rich.console import Console
from rich.panel import Panel

from . import __version__
from .env import NoToolchain

HIDDEN = {"serve", "hook", "exec"}

REMARK = """\
Typical workflow:

  1. mina-agent admin setup       build the graph tools, verify dune/merlin/claude, fetch landmarks
  2. mina-agent admin init        register the MCP server, install the walls and the git hook
  3. mina-agent doctor            confirm everything is wired (alias of show doctor)
  4. mina-agent develop           write code: edits accepted, developer shell, harness tools
     mina-agent discuss           the read-only version: ask before edits and tests
     mina-agent profile --focus <library>   profiling session: instrumented build, ranked hotspots
  5. mina-agent run fix-build-error --target src/lib/<lib> --trace     headless phases (run --help lists them)
     mina-agent run fix-bug --issue <url>     mina-agent run verify-perf --pr <n>
  6. mina-agent lint              CI's lint jobs on the staged files (also the git pre-commit hook)
  7. mina-agent trace <log>       trajectory evidence for any past run;  mina-agent dashboard --open
  8. mina-agent show tools|tests|phases|libraries    what the harness knows, derived not documented
  9. mina-agent admin clean       remove everything generated (state/); admin setup + init rebuild it

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

GROUP_SETTINGS = {"no_args_is_help": True, "context_settings": {"help_option_names": ["-h", "--help"]}}

# sessions
app.command()(discuss.discuss)
app.command()(develop.develop)
app.command(epilog=profile.EXAMPLES)(profile.profile)

# headless phases
run = typer.Typer(help="Headless phases, one command per data/phases/*.md; each declared arg is a --option.",
                  **GROUP_SETTINGS)
run_cmd.register(run)
app.add_typer(run, name="run")

# inspection
show = typer.Typer(help="Read-only inspection of the install and what the harness knows.", **GROUP_SETTINGS)
show.command()(status.status)
show.command()(doctor.doctor)
show.command()(list_.tools)
show.command()(list_.tests)
show.command()(list_.phases)
show.command()(list_.libraries)
app.add_typer(show, name="show")
app.command(help="Alias of `show doctor`.")(doctor.doctor)
app.command(help="Alias of `show status`.")(status.status)

# daily
app.command()(lint.lint)
app.command()(trace.trace)
app.command()(dashboard.dashboard)

# install lifecycle
admin = typer.Typer(help="Install lifecycle: setup, init, clean, derive.", **GROUP_SETTINGS)
admin.command()(setup.setup)
admin.command()(init.init)
admin.command()(clean.clean)
admin.command()(derive.derive)
app.add_typer(admin, name="admin")

# plumbing, referenced only from files the harness writes
app.command("serve", hidden=True)(serve.serve)
app.add_typer(hook.app, name="hook", hidden=True)
app.command("exec", hidden=True, context_settings={"allow_extra_args": True,
                                                     "ignore_unknown_options": True})(exec_.exec_)


# ── former flat names: hidden aliases that say where the command moved ──

def _moved(fn, new):
    import functools

    @functools.wraps(fn)
    def wrapper(*a, **k):
        sys.stderr.write(f"mina-agent: `{fn.__name__.replace('_', '-')}` moved to `mina-agent {new}`\n")
        return fn(*a, **k)
    return wrapper


for _fn, _new in ((setup.setup, "admin setup"), (init.init, "admin init"), (clean.clean, "admin clean"),
                  (derive.derive, "admin derive")):
    app.command(hidden=True)(_moved(_fn, _new))
_list_alias = typer.Typer(hidden=True, **GROUP_SETTINGS)
for _fn in (list_.tools, list_.tests, list_.phases, list_.libraries):
    _list_alias.command()(_moved(_fn, f"show {_fn.__name__}"))
app.add_typer(_list_alias, name="list", hidden=True)
run_cmd.register(app, hidden=True, moved_to="run")


if __name__ == "__main__":
    app()
