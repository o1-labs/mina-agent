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


# ---- optional capabilities: the degraded preflight ------------------------

OPTIONAL = "---\nname: opt\nargs: pr\nneeds: gh\noptional: samply\n---\nMeasure {{pr}}.\n"


@pytest.fixture
def opt_phase(tmp_path, monkeypatch):
    _phases_dir(tmp_path, monkeypatch, opt=OPTIONAL)
    return phases.load(tmp_path / "opt.md")


def test_optional_is_parsed_and_needs_still_hard(opt_phase):
    assert opt_phase.optional == ("samply",) and opt_phase.needs == ("gh",)


def _degraded(monkeypatch, missing=("samply",)):
    from mina_agent import capabilities as C
    monkeypatch.setattr(C, "unmet",
                        lambda names, path=None: [(n, "unusable here") for n in names if n in missing])


def test_unattended_refuses_rather_than_measuring_less(opt_phase, monkeypatch, capsys):
    """No one to ask: a degraded run must be asked for explicitly."""
    _degraded(monkeypatch)
    monkeypatch.setattr(run_cmd.sys.stdin, "isatty", lambda: False)
    with pytest.raises(typer.Exit) as ex:
        run_cmd._preflight_optional(opt_phase, {"PATH": ""}, yes=False)
    assert ex.value.exit_code == 2
    err = capsys.readouterr().err
    assert "samply unavailable" in err and "--yes" in err


def test_declining_the_prompt_stops_the_run(opt_phase, monkeypatch, capsys):
    _degraded(monkeypatch)
    monkeypatch.setattr(run_cmd.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(run_cmd.typer, "confirm", lambda *a, **k: False)
    with pytest.raises(typer.Exit) as ex:
        run_cmd._preflight_optional(opt_phase, {"PATH": ""}, yes=False)
    assert ex.value.exit_code == 2
    assert "nothing was run" in capsys.readouterr().err


def test_accepting_tells_the_model_what_is_missing(opt_phase, monkeypatch):
    """The confirmation is not enough on its own: the prompt has to say so,
    or the model reads an absent measurement as a measurement."""
    _degraded(monkeypatch)
    monkeypatch.setattr(run_cmd.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(run_cmd.typer, "confirm", lambda *a, **k: True)
    note = run_cmd._preflight_optional(opt_phase, {"PATH": ""}, yes=False)
    assert "samply" in note and "unusable here" in note
    assert "absent, not as zero and not as a result" in note
    assert note.startswith("Before anything else:") and note.endswith("\n\n")


def test_yes_skips_the_prompt_but_keeps_the_note(opt_phase, monkeypatch):
    _degraded(monkeypatch)
    monkeypatch.setattr(run_cmd.typer, "confirm", lambda *a, **k: pytest.fail("must not ask with --yes"))
    assert "samply" in run_cmd._preflight_optional(opt_phase, {"PATH": ""}, yes=True)


def test_nothing_is_said_when_everything_is_available(opt_phase, monkeypatch, capsys):
    _degraded(monkeypatch, missing=())
    assert run_cmd._preflight_optional(opt_phase, {"PATH": ""}, yes=False) == ""
    assert capsys.readouterr().err == ""


def test_the_yes_flag_exists_only_for_phases_with_optionals(tmp_path, monkeypatch):
    _phases_dir(tmp_path, monkeypatch, opt=OPTIONAL, good=GOOD)
    by_name = {p.name: p for p in phases.all_phases()}
    import inspect
    has = lambda p: "yes" in inspect.signature(run_cmd.make_command(p)).parameters
    assert has(by_name["opt"]) and not has(by_name["good"])
