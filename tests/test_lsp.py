"""ocamllsp resolution: which binary, and how each mode explains a miss."""
from mina_agent import lsp
from mina_agent.env import Env
from mina_agent.model import BuildProvenance, Mode


def _env(mode):
    return Env(mode=mode, activated=True, reasons=[], warnings=[], repo="/r",
               build_dir=BuildProvenance(False), env={}, _activated_env={"PATH": "/fake"})


def _no_ocamllsp(monkeypatch):
    monkeypatch.delenv("MINA_AGENT_OCAMLLSP", raising=False)
    monkeypatch.setattr(lsp.shutil, "which", lambda name, path=None: None)


def test_nix_shell_ocamllsp_is_labelled_as_such(monkeypatch):
    monkeypatch.delenv("MINA_AGENT_OCAMLLSP", raising=False)
    monkeypatch.setattr(lsp.shutil, "which",
                        lambda name, path=None: "/nix/store/abc/bin/ocamllsp" if name == "ocamllsp" else None)
    assert lsp.resolve(_env(Mode.NIX)) == ("/nix/store/abc/bin/ocamllsp", "PATH (nix shell)")
    assert lsp.resolve(_env(Mode.OPAM)) == ("/nix/store/abc/bin/ocamllsp", "PATH (project switch)")


def test_nix_miss_points_at_the_devshell_not_opam(monkeypatch):
    """The opam advice is wrong in a nix shell: there is no switch to install into."""
    _no_ocamllsp(monkeypatch)
    path, why = lsp.resolve(_env(Mode.NIX))
    assert path is None
    assert "devShell" in why and "nix develop" in why
    assert "opam" not in why


def test_opam_miss_keeps_the_switch_advice(monkeypatch):
    _no_ocamllsp(monkeypatch)
    path, why = lsp.resolve(_env(Mode.OPAM))
    assert path is None and "opam install ocaml-lsp-server" in why


def test_explicit_override_wins_in_either_mode(monkeypatch, tmp_path):
    exe = tmp_path / "ocamllsp"
    exe.write_text("")
    monkeypatch.setenv("MINA_AGENT_OCAMLLSP", str(exe))
    for mode in (Mode.NIX, Mode.OPAM):
        assert lsp.resolve(_env(mode)) == (str(exe), "MINA_AGENT_OCAMLLSP")
