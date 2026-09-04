"""The command hierarchy: groups, visible aliases, hidden moved names."""
from typer.testing import CliRunner

from mina_agent.cli import app

run = CliRunner()


def _help(*args):
    r = run.invoke(app, [*args, "--help"])
    assert r.exit_code == 0, r.output
    return r.output


def _commands(*args):
    """Command names from the Commands table of `--help` (not the remark text)."""
    out = _help(*args)
    start = out.find("Commands")
    for panel in ("Sessions", "Headless phases", "Inspection", "Every day", "Install"):
        i = out.find(panel)
        if i != -1 and (start == -1 or i < start):
            start = i
    table = out[start:out.find("Remark") if "Remark" in out else len(out)]
    return [line.split()[1] for line in table.splitlines() if line.startswith("│ ") and len(line.split()) > 1]


def test_top_level_surface():
    names = _commands()
    for name in ("discuss", "develop", "profile", "run", "show", "lint", "trace", "dashboard", "admin", "doctor", "status"):
        assert name in names, name
    for moved in ("setup", "init", "clean", "derive", "list", "fix-build-error", "verify-perf"):
        assert moved not in names, moved


def test_groups():
    assert all(n in _help("run") for n in ("fix-bug", "fix-build-error", "profile-hunt", "verify-perf"))
    assert all(n in _help("show") for n in ("status", "doctor", "tools", "tests", "phases", "libraries"))
    assert all(n in _help("admin") for n in ("setup", "init", "clean", "derive"))


def test_moved_names_still_answer():
    assert "--issue" in _help("fix-bug")
    assert "--target" in _help("run", "fix-build-error")
    assert _help("setup") and _help("list", "tools")
