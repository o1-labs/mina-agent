"""Environment status."""
import typer


def status(as_json: bool = typer.Option(False, "--json", help="Print the full JSON only.")):
    """Show the detected toolchain: mode, activation, dune and OCaml versions, warnings."""
    from .. import env as envmod, banner
    e = envmod.detect()
    if as_json:
        print(e.to_json())
        return
    d = e.to_dict()
    print(f"mode {d['mode']} ({'activated' if d['activated'] else 'not activated'})   "
          f"dune {d['dune_version']}   ocaml {d['ocaml']}")
    print(f"repo {d['repo']}")
    for r in d["reasons"]:
        print(f"  {r}")
    for w in d["warnings"]:
        print(f"  ! {w}")
    b = d["build_dir"] or {}
    print(f"_build: exists={b.get('exists')} built_by={b.get('built_by')}")
