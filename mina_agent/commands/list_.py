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
def libraries(filter: str = typer.Option("", "--filter", help="Only names or directories containing this."),
              inline_tests: bool = typer.Option(False, "--inline-tests", help="Only libraries with inline tests."),
              limit: int = typer.Option(60, "--limit", help="Rows to show (most depended-on first).")):
    """Libraries from the derived graph: directory, inline tests, dependents.
    A good profiling focus has inline tests (a ready workload) and dependents
    (its cost is paid widely)."""
    from .. import tools as T
    g = T.GRAPH.get()
    rows = []
    for name, rec in g["libraries"].items():
        if filter and filter not in name and filter not in rec["dir"]:
            continue
        if inline_tests and not rec["has_inline_tests"]:
            continue
        deps = g["dependents"].get(name, [])
        rows.append((name, rec["dir"], rec["has_inline_tests"], len([d for d in deps if ":" not in d]),
                     len([d for d in deps if d.startswith("test:")])))
    rows.sort(key=lambda r: (-r[3], r[0]))
    # ratio columns let rich fit the table to the terminal, ellipsizing the
    # two text columns rather than wrapping paths mid-word or overflowing
    t = Table(show_header=True, header_style="bold", expand=True,
              caption="deps: libraries depending on it; tests: test units depending on it; "
                      "inline: has (inline_tests)", caption_justify="left")
    t.add_column("library", ratio=3, no_wrap=True, overflow="ellipsis")
    t.add_column("dir", ratio=4, no_wrap=True, overflow="ellipsis")
    for c in ("inline", "deps", "tests"):
        t.add_column(c, justify="right", no_wrap=True)
    for name, d, it, nl, nt in rows[:limit]:
        t.add_row(name, d, "yes" if it else "", str(nl), str(nt))
    Console().print(t)
    if len(rows) > limit:
        print(f"{len(rows) - limit} more; raise --limit or narrow --filter")


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
