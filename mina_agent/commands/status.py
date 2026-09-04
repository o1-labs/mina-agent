"""Environment status."""
import typer


def status(as_json: bool = typer.Option(False, "--json", help="Print the full JSON only.")):
    """Show the detected toolchain: mode, activation, dune and OCaml versions, warnings."""
    from .. import env as envmod, banner
    e = envmod.detect()
    if as_json:
        print(e.to_json())
        return
    print(e.summary())
    print(f"repo {e.repo}")
    for r in e.reasons:
        print(f"  {r}")
    for w in e.warnings:
        print(f"  ! {w}")
    print(f"_build: exists={e.build_dir.exists} built_by={e.build_dir.built_by}")
