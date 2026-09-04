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
