"""A malformed phase file is skipped with a warning; the others still register."""
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
    names = [p["name"] for p in phases.all_phases()]
    assert names == ["good"]
    err = capsys.readouterr().err
    assert "skipping phase broken.md" in err and "skipping phase badint.md" in err


def test_register_skips_phase_that_cannot_become_a_command(tmp_path, monkeypatch, capsys):
    _phases_dir(tmp_path, monkeypatch, good=GOOD, badarg=BAD_ARG_NAME)
    app = typer.Typer()
    run_cmd.register(app)
    assert [c.name for c in app.registered_commands] == ["good"]
    assert "skipping phase badarg" in capsys.readouterr().err
