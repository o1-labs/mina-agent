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


def test_a_miss_says_what_is_lost_and_nothing_about_installing(monkeypatch):
    """Same answer in both modes: the harness cannot guess how you would get
    ocamllsp, so it names the cost and the two places it looks."""
    _no_ocamllsp(monkeypatch)
    for mode in (Mode.NIX, Mode.OPAM):
        path, why = lsp.resolve(_env(mode))
        assert path is None
        assert "no ocamllsp on PATH" in why and "MINA_AGENT_OCAMLLSP" in why
        assert "install" not in why.replace("MINA_AGENT_OCAMLLSP", "")
        assert "opam" not in why and "nix develop" not in why


def test_resolution_never_probes_an_opam_switch(monkeypatch):
    """The mina-lsp sibling switch is gone: PATH or the override, nothing else."""
    calls = []
    monkeypatch.setattr(lsp.shutil, "which", lambda name, path=None: calls.append(name) or None)
    monkeypatch.delenv("MINA_AGENT_OCAMLLSP", raising=False)
    lsp.resolve(_env(Mode.OPAM))
    assert calls == ["ocamllsp"]
    assert not hasattr(lsp, "LSP_SWITCH")


def test_explicit_override_wins_in_either_mode(monkeypatch, tmp_path):
    exe = tmp_path / "ocamllsp"
    exe.write_text("")
    monkeypatch.setenv("MINA_AGENT_OCAMLLSP", str(exe))
    for mode in (Mode.NIX, Mode.OPAM):
        assert lsp.resolve(_env(mode)) == (str(exe), "MINA_AGENT_OCAMLLSP")
