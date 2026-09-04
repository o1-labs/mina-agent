"""Run a command in the activated switch (hidden)."""
import os
import sys

import typer


def exec_(ctx: typer.Context):
    """Run a command inside the detected toolchain: mina-agent exec -- dune --version"""
    from .. import env as envmod
    args = list(ctx.args)
    if args[:1] == ["--"]:
        args = args[1:]
    if not args:
        typer.echo("usage: mina-agent exec -- <cmd...>", err=True)
        raise typer.Exit(2)
    e = envmod.require()
    raise typer.Exit(e.run(args, lock=os.path.basename(args[0]) == "dune").returncode)
