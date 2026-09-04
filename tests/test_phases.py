"""A malformed phase file is skipped with a warning; the others still register."""
import pytest
import typer

from mina_agent import paths, phases
from mina_agent.commands import run as run_cmd

GOOD = "---\nname: good\nargs: target\n---\nDo the thing.\n"
NO_FRONT_MATTER = "no front matter here\n"
BAD_ARG_NAME = "---\nname: badarg\nargs: target-dir\n---\nbody\n"
BAD_INT = "---\nname: badint\nmax_turns: 40  # comment\n---\nbody\n"


def _phases_dir(tmp_path, monkeypatch, **files):
    for name, text in files.items():
        (tmp_path / f"{name}.md").write_text(text)
    monkeypatch.setattr(paths, "PHASES", tmp_path)


def test_all_phases_skips_unparseable_files(tmp_path, monkeypatch, capsys):
    _phases_dir(tmp_path, monkeypatch, good=GOOD, broken=NO_FRONT_MATTER, badint=BAD_INT)
    names = [p.name for p in phases.all_phases()]
    assert names == ["good"]
    err = capsys.readouterr().err
    assert "skipping phase broken.md" in err and "skipping phase badint.md" in err


def test_register_skips_phase_that_cannot_become_a_command(tmp_path, monkeypatch, capsys):
    _phases_dir(tmp_path, monkeypatch, good=GOOD, badarg=BAD_ARG_NAME)
    app = typer.Typer()
    run_cmd.register(app)
    assert [c.name for c in app.registered_commands] == ["good"]
    assert "skipping phase badarg" in capsys.readouterr().err


def test_load_and_render(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("---\nname: p\nargs: target, focus\nallowed_tools: Read, Edit\nmax_turns: 7\nsession: profile\n---\n"
                 "First paragraph\nstill first.\n\nUse {{target}} and {{focus}}.\n")
    p = phases.load(f)
    assert (p.name, p.args, p.allowed_tools, p.max_turns, p.session) == ("p", ("target", "focus"), ("Read", "Edit"), 7, "profile")
    assert p.summary == "First paragraph still first."
    assert phases.render(p, {"target": "a", "focus": "b"}).endswith("Use a and b.")
    with pytest.raises(ValueError, match="missing args \\['focus'\\]"):
        phases.render(p, {"target": "a"})


def test_phase_mode(tmp_path):
    (tmp_path / "a.md").write_text("---\nname: a\nmode: interactive\n---\nbody\n")
    (tmp_path / "b.md").write_text("---\nname: b\n---\nbody\n")
    (tmp_path / "c.md").write_text("---\nname: c\nmode: sideways\n---\nbody\n")
    assert phases.load(tmp_path / "a.md").interactive and not phases.load(tmp_path / "b.md").interactive
    with pytest.raises(ValueError, match="mode must be"):
        phases.load(tmp_path / "c.md")
    assert next(p for p in phases.all_phases() if p.name == "verify_perf").interactive


def test_interactive_argv_carries_phase_walls():
    from mina_agent import agent
    p = next(p for p in phases.all_phases() if p.name == "verify_perf")
    argv = agent.interactive_argv("go", "/tmp", phase=p)
    assert argv[argv.index("--permission-mode") + 1] == p.permission_mode
    i = argv.index("--allowedTools")
    assert "Bash(gh *)" in argv[i:] and "--disallowedTools" in argv
