"""harness/.envrc, the phase `env:` requirement, and headless settings."""
import os
from pathlib import Path

from mina_agent import agent, env as envmod, phases


def test_dotenv_reads_exports_and_ignores_unchanged(tmp_path, monkeypatch):
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / ".envrc").write_text('export GH_TOKEN="abc 123"\nexport HOME="$HOME"\n# comment\n')
    d = envmod.dotenv(str(tmp_path))
    assert d["GH_TOKEN"] == "abc 123" and "HOME" not in d


def test_dotenv_absent_is_empty(tmp_path):
    assert envmod.dotenv(str(tmp_path)) == {}


def test_phase_env_requirement_parses(tmp_path):
    f = tmp_path / "p.md"
    f.write_text("---\nname: p\nenv: GH_TOKEN, OTHER\n---\nbody\n")
    assert phases.load(f).env == ("GH_TOKEN", "OTHER")
    assert next(p for p in phases.all_phases() if p.name == "fix_bug").env == ("GH_TOKEN",)


def test_headless_settings_have_no_hooks_and_no_coauthor():
    s = agent.headless_settings()
    assert "hooks" not in s and s["includeCoAuthoredBy"] is False
    assert agent.session_settings()["includeCoAuthoredBy"] is False


def test_fix_bug_phase_walls():
    p = next(p for p in phases.all_phases() if p.name == "fix_bug")
    assert "Bash(git *)" in p.allowed_tools and "Bash(git push *)" in p.disallowed_tools
    assert "Bash" not in p.disallowed_tools
