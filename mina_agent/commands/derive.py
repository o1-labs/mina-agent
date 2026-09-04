"""Derive the library graph (hidden)."""
import typer


def derive(check: bool = typer.Option(False, "--check", help="Exit 1 if derived.json is stale."),
           build: bool = typer.Option(False, "--build", help="(Re)compile describe-dune only.")):
    """Write harness/state/derived.json from the dune files via describe-dune."""
    from .. import env as envmod, graph, paths
    e = envmod.require()
    if build:
        print(graph.build_tool(e))
        return
    if check:
        ok = graph.check(e)
        print("derived.json is current" if ok else "derived.json is stale; rerun mina-agent derive")
        raise typer.Exit(0 if ok else 1)
    d = graph.derive_and_write(e)
    print(f"wrote {paths.derived_json(e.repo)}: {graph.summary(d)}")
    for n in d["notes"]:
        print("  note:", n)
