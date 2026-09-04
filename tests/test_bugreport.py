"""Bug-report evidence bundle and filing without gh."""
import json
import zipfile
from pathlib import Path

from mina_agent import bugreport as B, paths
from mina_agent.model import BuildProvenance, Mode
from mina_agent.env import Env


def _env(tmp_path):
    return Env(mode=Mode.OPAM, activated=True, reasons=[], warnings=["w1"], repo=str(tmp_path / "mina"),
               build_dir=BuildProvenance(False), env={}, dune_version="3.3.1", ocaml="4.14.2")


def test_bundle_collects_logs_and_zips(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HARNESS", tmp_path / "harness")
    monkeypatch.setattr(B.tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir()
    (tmp_path / "mina").mkdir()
    logs = paths.state_dir() / "logs"
    logs.mkdir(parents=True)
    (logs / "lint.jsonl").write_text('{"ts": 1}\n')
    for i in range(3):
        (logs / f"2026090{i}T000000Z-fix_build_error.jsonl").write_text('{"kind": "ResultMessage"}\n')
    (logs / "20260902T000000Z-fix_build_error.summary.md").write_text("# summary\n")
    b = B.bundle(_env(tmp_path), runs=2, doctor_text="doctor ok\n")
    assert "environment.json" in b.files and "doctor.txt" in b.files and "lint.jsonl" in b.files
    assert [f for f in b.files if f.startswith("runs/")] == [
        "runs/20260901T000000Z-fix_build_error.jsonl", "runs/20260902T000000Z-fix_build_error.jsonl",
        "runs/20260902T000000Z-fix_build_error.summary.md"]
    env = json.loads((Path(b.directory) / "environment.json").read_text())
    assert env["dune"] == "3.3.1" and env["warnings"] == ["w1"]
    with zipfile.ZipFile(b.zip) as zf:
        assert len(zf.namelist()) == len(b.files)


def test_file_issue_without_gh_saves_a_draft(tmp_path, monkeypatch):
    monkeypatch.setattr(B.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(B.shutil, "which", lambda name: None)
    r = B.file_issue("check: wrong alias", "## What happened\nx\n", "/tmp/b.zip")
    assert r["filed"] is False and "not installed" in r["reason"]
    draft = Path(r["draft"]).read_text()
    assert draft.startswith("# check: wrong alias") and "/tmp/b.zip" in draft
    assert r["new_issue_url"].endswith("/o1-labs/mina-agent/issues/new")
