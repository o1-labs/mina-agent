"""Listings derived from the registry, manifest, and phase files (hidden)."""
import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="List tools, tests, or phases. Everything here is derived, nothing is hand-maintained.")


@app.command()
def tools():
    """MCP tools with their docstrings, from the registered functions."""
    from .. import tools as T
    t = Table(show_header=True, header_style="bold")
    t.add_column("tool"); t.add_column("description")
    for name in T.TOOLS:
        doc = (getattr(T, name).__doc__ or "").strip().split("\n\n")[0].replace("\n", " ")
        t.add_row(name, doc)
    Console().print(t)


@app.command()
def tests():
    """Manifest tests with cost and modes, plus the inline-test rule."""
    from .. import tools as T
    m = T.MANIFEST_DATA
    t = Table(show_header=True, header_style="bold")
    for c in ("name", "cost", "measured s", "modes", "libraries", "command"):
        t.add_column(c)
    for x in m["tests"]:
        t.add_row(x["name"], x["cost"], str(x.get("measured", 0)), ",".join(x["modes"]) or "none",
                  ",".join(x["libraries"]), " ".join(x["command"]))
    Console().print(t)
    it = m["inline_tests"]
    print(f"inline tests: {it['name_prefix']}<library> for every library with (inline_tests); "
          f"command {' '.join(it['command_template'])}; cost {it['cost']}")


@app.command()
def phases():
    """Phase files and the subcommands they generate."""
    from .. import phases as P
    t = Table(show_header=True, header_style="bold")
    for c in ("command", "args", "tools", "removed", "limits"):
        t.add_column(c)
    for p in P.all_phases():
        t.add_row("mina-agent " + p["name"].replace("_", "-"),
                  " ".join(f"--{a}" for a in p["args"]), ", ".join(p["allowed_tools"]),
                  ", ".join(p["disallowed_tools"]),
                  f"{p['max_turns']} turns, ${p['max_budget_usd']}, {p['permission_mode']}")
    Console().print(t)
