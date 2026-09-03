"""Build the graph tool and verify the toolchain this harness relies on."""
import shutil

import typer

from .. import paths


def setup():
    """Build describe-dune with the repo's switch and verify dune, merlin, and claude.

    Installs nothing system-wide and never touches the opam switch, nix, or
    cargo: describe-dune's five OCaml deps are already in opam.export, and the
    binary lands in harness/state/bin.
    """
    from .. import env as envmod, graph
    e = envmod.detect()
    print(f"toolchain: mode={e.mode} activated={e.activated} dune={e.dune_version} ocaml={e.ocaml}")
    for w in e.warnings:
        print(f"  warning: {w}")
    if e.mode == "none":
        typer.echo("no usable toolchain: " + "; ".join(e.reasons), err=True)
        raise typer.Exit(3)
    aenv = e.activate()
    for name, why in (("ocamlmerlin", "type_at / definition tools"),
                      ("claude", "headless phases and discuss")):
        found = shutil.which(name, path=aenv.get("PATH"))
        print(f"{name:12s} {'ok  ' + found if found else 'MISSING  (' + why + ')'}")
    tool = graph.build_tool(e)
    print(f"describe-dune built: {tool}")
    d = graph.derive_and_write(e)
    print(f"graph: {graph.summary(d)}")
    print("next: mina-agent init")
