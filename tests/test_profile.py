"""Profiling session mechanics: stanza injection, start/restore, numbering."""
import subprocess
from pathlib import Path

from mina_agent import landmarks, profile as P

DUNE = '(library\n (name x)\n (libraries core))\n\n(rule (targets y) (action (echo "; not a comment")))\n'


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path, monkeypatch):
    repo = tmp_path / "r"
    (repo / "src" / "x").mkdir(parents=True)
    (repo / "src" / "x" / "dune").write_text(DUNE)
    (repo / "src" / "x" / "x.ml").write_text("let f x = x\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "root")
    monkeypatch.setattr(landmarks, "present", lambda repo: True)
    return str(repo), {"libraries": {"x": {"dir": "src/x", "deps": []}}}


def test_inject_stanza_targets_the_named_library_only():
    out = P.inject_stanza(DUNE, "x")
    assert out is not None and "backend landmarks --auto" in out
    assert out.index("landmarks") < out.index("(rule")          # inside the library form
    assert P.inject_stanza(out, "x") == out                      # idempotent
    assert P.inject_stanza(DUNE, "nope") is None


def test_start_and_restore_round_trip(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    s = P.start(repo, g, "x", "lib", ["x"])
    assert list(s["injected"]) == ["src/x/dune"] and P.active(repo)
    assert "landmarks" in Path(repo, "src/x/dune").read_text()
    rep = P.restore(repo)
    assert rep["restored"] == ["src/x/dune"] and rep["edited"] == [] and rep["still_dirty"] == []
    assert Path(repo, "src/x/dune").read_text() == DUNE and not P.active(repo)


def test_restore_keeps_a_dune_file_edited_during_the_session(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    P.start(repo, g, "x", "lib", ["x"])
    dune = Path(repo, "src/x/dune")
    dune.write_text(dune.read_text().replace("core", "core base"))
    rep = P.restore(repo)
    assert rep["edited"] == ["src/x/dune"] and rep["restored"] == []
    assert "base" in dune.read_text() and "landmarks" in dune.read_text()


def test_restore_reports_leftover_windows(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    P.start(repo, g, "x", "lib", ["x"])
    Path(repo, "src/x/x.ml").write_text('let f x = (x [@landmark "w"])\n')
    rep = P.restore(repo)
    assert rep["source_edits"] == ["src/x/x.ml"] and rep["windows_left"] == ["src/x/x.ml"]


def test_start_refuses_dirty_dune_file(tmp_path, monkeypatch):
    import pytest
    repo, g = _repo(tmp_path, monkeypatch)
    Path(repo, "src/x/dune").write_text(DUNE + "; local edit\n")
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        P.start(repo, g, "x", "lib", ["x"])
    assert not P.active(repo)


def test_next_profile_path_numbers_after_files_on_disk(tmp_path, monkeypatch):
    repo, _ = _repo(tmp_path, monkeypatch)
    d = P.state_dir(repo)
    (d / "002-inline_x.json").write_text("{}")
    assert P.next_profile_path(repo, "inline:x").name == "003-inline_x.json"
    assert P.next_profile_path(repo, "exe:a/b.exe").name == "003-exe_a_b.exe.json"
