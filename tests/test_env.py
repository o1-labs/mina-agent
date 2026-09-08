"""Env.summary and require()/NoToolchain."""
import pytest

from mina_agent import env as envmod
from mina_agent.model import BuildProvenance, Mode


def _env(mode, **kw):
    return envmod.Env(mode=mode, activated=True, reasons=["r1", "r2"], warnings=[], repo="/r",
                      build_dir=BuildProvenance(False), env={}, **kw)


def test_summary_one_format():
    e = _env(Mode.OPAM, dune_version="3.3.1", ocaml="4.14.2")
    assert e.summary() == "mode opam (activated)   dune 3.3.1   ocaml 4.14.2"
    assert _env(Mode.NONE).summary().endswith("dune ?   ocaml ?")


def test_require_raises_with_reasons(monkeypatch):
    monkeypatch.setattr(envmod, "detect", lambda: _env(Mode.NONE))
    with pytest.raises(envmod.NoToolchain, match="no usable toolchain: r1; r2"):
        envmod.require()


def test_require_returns_usable_env(monkeypatch):
    e = _env(Mode.OPAM)
    monkeypatch.setattr(envmod, "detect", lambda: e)
    assert envmod.require() is e


def _fake_nix_shell(monkeypatch, tmp_path):
    """An entered nix shell: IN_NIX_SHELL set and dune resolving into the store."""
    dune = tmp_path / "nix" / "store" / "abc-dune" / "bin" / "dune"
    dune.parent.mkdir(parents=True)
    dune.write_text("")
    monkeypatch.setenv("IN_NIX_SHELL", "impure")
    monkeypatch.delenv("HARNESS_MODE", raising=False)
    monkeypatch.setattr(envmod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(envmod.shutil, "which", lambda name, path=None: str(dune) if name == "dune" else None)
    monkeypatch.setattr(envmod, "_real", lambda p: "/nix/store/abc-dune/bin/dune" if p else None)


def test_nix_shell_is_detected_and_usable(monkeypatch, tmp_path):
    _fake_nix_shell(monkeypatch, tmp_path)
    e = envmod.detect()
    assert e.mode is Mode.NIX and e.activated and e.usable
    assert any("IN_NIX_SHELL set" in r for r in e.reasons)
    monkeypatch.setattr(envmod, "detect", lambda: e)
    assert envmod.require() is e


def test_nix_shell_activation_inherits_the_environment(monkeypatch, tmp_path):
    """An entered shell is activated in place: activate() copies os.environ
    rather than calling the _nix_activate stub."""
    _fake_nix_shell(monkeypatch, tmp_path)
    monkeypatch.setenv("MINA_AGENT_MARKER", "from-the-shell")
    monkeypatch.setattr(envmod, "_nix_activate", lambda repo: pytest.fail("entered shells must not re-activate"))
    e = envmod.detect()
    assert e.activate()["MINA_AGENT_MARKER"] == "from-the-shell"


def test_nix_mode_outside_a_shell_is_refused(monkeypatch, tmp_path):
    """HARNESS_MODE=nix with no shell entered: entering one is a stub, so
    there is nothing to activate and require() refuses."""
    monkeypatch.delenv("IN_NIX_SHELL", raising=False)
    monkeypatch.setenv("HARNESS_MODE", "nix")
    monkeypatch.setattr(envmod, "_repo_root", lambda: str(tmp_path))
    monkeypatch.setattr(envmod.shutil, "which", lambda name, path=None: None)
    e = envmod.detect()
    assert e.mode is Mode.NIX and not e.activated and not e.usable
    monkeypatch.setattr(envmod, "detect", lambda: e)
    with pytest.raises(envmod.NoToolchain, match="not a nix shell"):
        envmod.require()


# ---- _build provenance: which toolchain, not just which mode --------------

def _with_build(tmp_path, recorded, current):
    """An Env whose _build log named `recorded` and whose shell has `current`."""
    return envmod.Env(mode=Mode.NIX, activated=True, reasons=[], warnings=[], repo=str(tmp_path),
                      build_dir=BuildProvenance(True, "nix", recorded), env={}, ocaml_bin=current)


def test_toolchain_match_ignores_the_compiler_filename(tmp_path):
    """dune's log records ocamlc.opt; PATH gives ocamlc. Same install."""
    d = tmp_path / "store" / "abc" / "bin"
    d.mkdir(parents=True)
    assert _with_build(tmp_path, str(d / "ocamlc.opt"), str(d)).build_toolchain_matches is True


def test_two_nix_shells_are_not_the_same_toolchain(tmp_path):
    """The gap this closes: both are "nix", so built_by alone says nothing."""
    old = tmp_path / "store" / "aaa-ocaml" / "bin"
    new = tmp_path / "store" / "bbb-ocaml" / "bin"
    old.mkdir(parents=True); new.mkdir(parents=True)
    e = _with_build(tmp_path, str(old / "ocamlc.opt"), str(new))
    assert e.build_dir.built_by == str(e.mode)      # indistinguishable by mode
    assert e.build_toolchain_matches is False


def test_nothing_to_compare_is_not_a_complaint(tmp_path):
    assert _with_build(tmp_path, None, "/anywhere").build_toolchain_matches is None
    assert _with_build(tmp_path, "/x/bin/ocamlc", None).build_toolchain_matches is None


def test_detect_warns_when_the_toolchain_moved_under_build(monkeypatch, tmp_path):
    old = tmp_path / "store" / "aaa-ocaml" / "bin"
    new = tmp_path / "store" / "bbb-ocaml" / "bin"
    old.mkdir(parents=True); new.mkdir(parents=True)
    (new / "ocamlc").write_text("")
    (new / "dune").write_text("")
    _fake_nix_shell(monkeypatch, tmp_path)
    monkeypatch.setattr(envmod, "_build_provenance",
                        lambda repo: BuildProvenance(True, "nix", str(old / "ocamlc.opt")))
    monkeypatch.setattr(envmod.shutil, "which",
                        lambda name, path=None: str(new / name) if name in ("dune", "ocamlc") else None)
    monkeypatch.setattr(envmod, "_version", lambda exe, flag, *, env: "x")
    e = envmod.detect()
    assert any("different nix toolchain" in w and "dune will rebuild" in w for w in e.warnings), e.warnings


def test_build_warning_is_not_repeated_by_doctor(monkeypatch, tmp_path):
    """The _build provenance row states it; a second generic `warning` row
    saying the same thing is noise."""
    from mina_agent.commands import doctor
    e = _with_build(tmp_path, None, None)
    e.warnings.extend(["_build was produced by a different nix toolchain (...)", "ocamllsp not on PATH"])
    rows = list(doctor.toolchain(e))
    assert [c.detail for c in rows if c.name == "warning"] == ["ocamllsp not on PATH"]
    assert any(c.name == "_build provenance" for c in rows)
