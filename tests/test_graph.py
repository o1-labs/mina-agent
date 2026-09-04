"""graph.reshape over a small describe-dune document: local ppx libraries and
virtual-library implementations must produce dependent edges."""
import json
from mina_agent.graph import reshape

DESC = [
    {"src": "src/lib/ppx_version/runtime", "units": [
        {"type": "lib", "name": "ppx_version_runtime", "public_name": "ppx_version.runtime", "deps": []}]},
    {"src": "src/lib/ppx_mina", "units": [
        {"type": "lib", "name": "ppx_mina", "public_name": "ppx_mina", "deps": ["ppxlib"]}]},
    {"src": "src/lib/currency", "units": [
        {"type": "lib", "name": "currency", "public_name": "currency",
         "deps": ["core_kernel", "ppx_version.runtime", "ppx_mina", "ppx_jane", "bisect_ppx"]}]},
    {"src": "src/lib/logger", "units": [
        {"type": "lib", "name": "logger", "public_name": "logger", "deps": []}]},
    {"src": "src/lib/logger/native", "units": [
        {"type": "lib", "name": "logger_native", "public_name": "logger.native",
         "implements": "logger", "deps": ["core"]}]},
    {"src": "src/lib/currency/test", "units": [
        {"type": "test", "name": "test_currency", "deps": ["currency"]}]},
]


def test_local_ppx_libraries_are_dependency_edges():
    g = reshape(DESC)
    assert g["libraries"]["currency"]["deps"] == ["ppx_version_runtime", "ppx_mina"]
    assert g["libraries"]["currency"]["ppx"] == ["ppx_jane", "bisect_ppx"]
    assert g["dependents"]["ppx_version_runtime"] == ["currency"]
    assert g["dependents"]["ppx_mina"] == ["currency"]


def test_implementation_depends_on_its_virtual_library():
    g = reshape(DESC)
    assert g["libraries"]["logger_native"]["implements"] == "logger"
    assert g["libraries"]["logger_native"]["deps"] == ["logger"]
    assert g["dependents"]["logger"] == ["logger_native"]


def test_tests_are_dependents():
    assert reshape(DESC)["dependents"]["currency"] == ["test:src/lib/currency/test/test_currency"]


def test_stamp_is_deterministic_and_tracks_dune_files(tmp_path):
    import os, time
    from mina_agent.graph import stamp
    d = tmp_path / "src" / "lib" / "x"
    d.mkdir(parents=True)
    dune = d / "dune"
    dune.write_text("(library (name x))")
    s1 = stamp(str(tmp_path))
    assert s1 == stamp(str(tmp_path))
    os.utime(dune, ns=(time.time_ns(), time.time_ns() + 10**9))
    assert stamp(str(tmp_path)) != s1
    (d / "dune-project").write_text("(lang dune 3.0)")
    assert stamp(str(tmp_path)) not in (s1,)


def test_write_json_atomic_leaves_no_temp_files(tmp_path):
    from mina_agent.graph import write_json_atomic
    out = tmp_path / "derived.json"
    write_json_atomic(out, {"a": 1})
    write_json_atomic(out, {"a": 2})
    assert json.loads(out.read_text()) == {"a": 2}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["derived.json"]
