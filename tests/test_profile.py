"""Profiling session mechanics: stanza injection, start/restore, numbering."""
import subprocess
from pathlib import Path

from mina_agent import landmarks, paths, profile as P

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
    monkeypatch.setattr(paths, "HARNESS", tmp_path / "harness")     # generated state lives under the harness checkout
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
    assert list(s.injected) == ["src/x/dune"] and P.active(repo)
    assert "landmarks" in Path(repo, "src/x/dune").read_text()
    rep = P.restore(repo)
    assert rep.restored == ("src/x/dune",) and rep.edited == () and rep.still_dirty == ()
    assert Path(repo, "src/x/dune").read_text() == DUNE and not P.active(repo)


def test_restore_keeps_a_dune_file_edited_during_the_session(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    P.start(repo, g, "x", "lib", ["x"])
    dune = Path(repo, "src/x/dune")
    dune.write_text(dune.read_text().replace("core", "core base"))
    rep = P.restore(repo)
    assert rep.edited == ("src/x/dune",) and rep.restored == ()
    assert "base" in dune.read_text() and "landmarks" in dune.read_text()


def test_restore_reports_leftover_windows(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    P.start(repo, g, "x", "lib", ["x"])
    Path(repo, "src/x/x.ml").write_text('let f x = (x [@landmark "w"])\n')
    rep = P.restore(repo)
    assert rep.source_edits == ("src/x/x.ml",) and rep.windows_left == ("src/x/x.ml",)


def test_start_refuses_dirty_dune_file(tmp_path, monkeypatch):
    import pytest
    repo, g = _repo(tmp_path, monkeypatch)
    Path(repo, "src/x/dune").write_text(DUNE + "; local edit\n")
    with pytest.raises(RuntimeError, match="uncommitted changes"):
        P.start(repo, g, "x", "lib", ["x"])
    assert not P.active(repo)


def test_next_profile_path_numbers_after_files_on_disk(tmp_path, monkeypatch):
    repo, _ = _repo(tmp_path, monkeypatch)
    d = P.state_dir()
    assert d == tmp_path / "harness" / "state" / "profile"
    (d / "002-inline_x.json").write_text("{}")
    assert P.next_profile_path(repo, "inline:x").name == "003-inline_x.json"
    assert P.next_profile_path(repo, "exe:a/b.exe").name == "003-exe_a_b.exe.json"


def test_session_round_trips_through_json(tmp_path, monkeypatch):
    from mina_agent.model import ProfileEntry
    repo, g = _repo(tmp_path, monkeypatch)
    P.start(repo, g, "x", "lib", ["x"])
    entry = ProfileEntry(profile="001-inline_x", path="/p", workload="inline:x", only_test=None, exe="e",
                         exit_code=0, run_s=1.0, build_s=2.0, total_ms=3.0, units="ms", functions=4,
                         focus_functions_hit=2, focus_self_share_pct=50.0)
    P.record_profile(repo, entry)
    s = P.load(repo)
    assert s is not None and s.profiles == (entry,) and s.libraries == ("x",)
    P.restore(repo)


def test_landmarks_load_aggregates_instances(tmp_path):
    import json
    prof = tmp_path / "p.json"
    prof.write_text(json.dumps({"label": "t", "root": 0, "nodes": [
        {"id": 0, "name": "ROOT", "location": "", "kind": "root", "time": 1000, "sys_time": 1.0,
         "calls": 1, "allocated_bytes": 0, "children": [1, 2]},
        {"id": 1, "name": "f", "location": "src/x/x.ml:1", "kind": "normal", "time": 600, "calls": 2,
         "allocated_bytes": 2_000_000, "children": [3]},
        {"id": 2, "name": "g", "location": "src/y/y.ml:1", "kind": "normal", "time": 300, "calls": 1,
         "allocated_bytes": 0, "children": []},
        {"id": 3, "name": "g", "location": "src/y/y.ml:1", "kind": "normal", "time": 100, "calls": 5,
         "allocated_bytes": 1_000_000, "children": []}]}))
    p = landmarks.load(prof)
    assert p.units == "ms" and p.total_ms == 1000.0
    f, g = p.functions["f @ src/x/x.ml:1"], p.functions["g @ src/y/y.ml:1"]
    assert (f.self_ms, f.total_ms, f.calls, f.self_alloc_mb) == (500.0, 600.0, 2, 1.0)
    assert (g.self_ms, g.total_ms, g.calls) == (400.0, 400.0, 6)        # two instances summed
    assert g.callers[0].caller == "f @ src/x/x.ml:1" and g.callers[0].calls == 5
    assert f.under(["src/x"]) and not f.under(["src/y"])
    assert [r["name"] for r in landmarks.top(p, "self_ms", 1)] == ["f"]
    d = landmarks.diff(p, p)
    assert d["total_ms_delta"] == 0.0 and all(r["status"] == "changed" for r in d["functions"])
