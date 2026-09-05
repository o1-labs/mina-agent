"""Profiling session mechanics: stanza injection, start/restore, numbering."""
import subprocess
from pathlib import Path

from mina_agent import landmarks, paths, profile as P

DUNE = '(library\n (name x)\n (libraries core))\n\n(rule (targets y) (action (echo "; not a comment")))\n'


def _git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path, monkeypatch) -> tuple[str, dict]:
    repo = tmp_path / "r"
    (repo / "src" / "x").mkdir(parents=True)
    (repo / "src" / "x" / "dune").write_text(DUNE)
    (repo / "src" / "x" / "x.ml").write_text("let f x = x\n")
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "root")
    monkeypatch.setattr(landmarks, "present", lambda repo: True)
    monkeypatch.setattr(paths, "HARNESS", tmp_path / "harness")     # generated state lives under the harness checkout
    return str(repo), {"libraries": {"x": {"dir": "src/x", "deps": [], "has_inline_tests": True}}}


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


def test_restore_recognises_a_file_already_put_back(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    P.start(repo, g, "x", "lib", ["x"])
    Path(repo, "src/x/dune").write_text(DUNE)                     # restored by hand / checkout
    rep = P.restore(repo)
    assert rep.already_restored == ("src/x/dune",) and rep.edited == () and rep.restored == ()


def test_restore_reports_stanza_only_when_present(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    P.start(repo, g, "x", "lib", ["x"])
    Path(repo, "src/x/dune").write_text(DUNE + "; a comment\n")   # edited, no stanza
    rep = P.restore(repo)
    assert rep.edited == ("src/x/dune",) and rep.stanza_left == ()


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


def test_extend_adds_a_library_and_restore_covers_it(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    (Path(repo) / "src" / "y").mkdir()
    (Path(repo) / "src" / "y" / "dune").write_text("(library\n (name y))\n")
    _git(repo, "add", "."); _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "y")
    g["libraries"]["y"] = {"dir": "src/y", "deps": []}
    P.start(repo, g, "x", "lib", ["x"])
    s = P.extend(repo, g, ["y"])
    assert s.libraries == ("x", "y") and s.dirs == ("src/x", "src/y") and s.scope == "custom"
    assert set(s.injected) == {"src/x/dune", "src/y/dune"} and "landmarks" in Path(repo, "src/y/dune").read_text()
    assert P.extend(repo, g, ["y"]).libraries == ("x", "y")          # idempotent
    rep = P.restore(repo)
    assert set(rep.restored) == {"src/x/dune", "src/y/dune"} and "landmarks" not in Path(repo, "src/y/dune").read_text()


def test_restore_archives_and_resume_recreates_with_profiles(tmp_path, monkeypatch):
    from mina_agent.model import ProfileEntry
    repo, g = _repo(tmp_path, monkeypatch)
    P.start(repo, g, "x", "lib", ["x"])
    prof = P.state_dir() / "001-inline_x.json"; prof.write_text("{}")
    entry = ProfileEntry(profile="001-inline_x", path=str(prof), workload="inline:x", only_test=None, exe="e",
                         exit_code=0, run_s=1.0, build_s=1.0, total_ms=1.0, units="ms", functions=1,
                         focus_functions_hit=1, focus_self_share_pct=1.0)
    P.record_profile(repo, entry)
    P.restore(repo)
    assert not P.active(repo) and P.last_session_file(repo).exists()
    s = P.resume(repo, g)
    assert P.active(repo) and s.libraries == ("x",) and s.profiles == (entry,)
    assert "landmarks" in Path(repo, "src/x/dune").read_text()
    P.restore(repo)


INLINE_BARE = "(library\n (name x)\n (inline_tests)\n (libraries core))\n"
INLINE_FLAGS = "(library\n (name x)\n (inline_tests\n  (flags -verbose))\n (libraries core))\n"
INLINE_LIBS = "(library\n (name x)\n (inline_tests\n  (libraries a.b))\n (libraries core))\n"
TESTS = "(tests\n (names t_one t_two)\n (libraries core x))\n"
EXE = "(executable\n (name main))\n"


def _link(text, kind, name, lib) -> str:
    out = P.link_library(text, kind, name, lib)
    assert out is not None
    return out


def test_link_library_inline_shapes():
    assert "(inline_tests\n  (libraries disk_cache.lmdb))" in _link(INLINE_BARE, "lib", "x", "disk_cache.lmdb")
    assert "(flags -verbose)\n  (libraries disk_cache.lmdb))" in _link(INLINE_FLAGS, "lib", "x", "disk_cache.lmdb")
    out = _link(INLINE_LIBS, "lib", "x", "disk_cache.lmdb")
    assert "(libraries a.b\n   disk_cache.lmdb)" in out
    assert P.link_library(out, "lib", "x", "disk_cache.lmdb") == out             # idempotent
    assert P.link_library("(library\n (name x))\n", "lib", "x", "z") is None      # no inline tests
    assert P.link_library(INLINE_BARE, "lib", "nope", "z") is None


def test_link_library_tests_and_exe():
    assert "(libraries core x\n  disk_cache.lmdb)" in _link(TESTS, "test", "t_two", "disk_cache.lmdb")
    assert P.link_library(EXE, "exe", "main", "d.l") == "(executable\n (name main)\n (libraries d.l))\n"


def test_link_impl_tracks_and_restores_with_instrumentation(tmp_path, monkeypatch):
    repo, g = _repo(tmp_path, monkeypatch)
    x_dune = Path(repo, "src/x/dune"); x_dune.write_text(INLINE_FLAGS)
    (Path(repo) / "src" / "v").mkdir(); (Path(repo) / "src" / "v" / "dune").write_text("(library (name v))\n")
    (Path(repo) / "src" / "vi").mkdir(); (Path(repo) / "src" / "vi" / "dune").write_text("(library (name v_impl))\n")
    _git(repo, "add", "."); _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "v")
    g["libraries"].update({"v": {"dir": "src/v", "deps": [], "implements": None},
                           "v_impl": {"dir": "src/vi", "deps": ["v"], "implements": "v", "public_name": "v.impl"},
                           "v_other": {"dir": "src/vo", "deps": ["v"], "implements": "v"}})
    g["libraries"]["x"].update({"has_inline_tests": True, "deps": ["v"]})
    g.update({"public_names": {"v.impl": "v_impl"}, "tests": {}, "executables": {}})
    P.start(repo, g, "x", "lib", ["x"])                       # instruments x's dune (same file we will link into)
    s, info = P.link_impl(repo, g, "v_impl", "inline:x")
    assert info["unit"] == "lib x" and not info["already_linked"] and s.linked[0].impl == "v_impl"
    text = x_dune.read_text()
    assert "backend landmarks" in text and "(libraries v.impl)" in text
    s2, info2 = P.link_impl(repo, g, "v_impl", "inline:x")
    assert info2["already_linked"] and len(s2.linked) == 1
    rep = P.restore(repo)
    assert rep.restored == ("src/x/dune",) and x_dune.read_text() == INLINE_FLAGS


def test_link_impl_refuses_second_implementation(tmp_path, monkeypatch):
    import pytest
    repo, g = _repo(tmp_path, monkeypatch)
    g["libraries"].update({"v": {"dir": "src/v", "deps": []}, "v_a": {"dir": "src/va", "deps": ["v"], "implements": "v"},
                           "v_b": {"dir": "src/vb", "deps": ["v"], "implements": "v"}})
    g["libraries"]["x"].update({"deps": ["v_a"], "has_inline_tests": True})
    g.update({"public_names": {}, "tests": {}, "executables": {}})
    P.start(repo, g, "x", "lib", ["x"])
    with pytest.raises(RuntimeError, match="already links v_a"):
        P.link_impl(repo, g, "v_b", "inline:x")
    with pytest.raises(ValueError, match="does not implement"):
        P.link_impl(repo, g, "x", "inline:x")
    P.restore(repo)
