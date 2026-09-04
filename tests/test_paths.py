"""git_hook resolves the hooks directory the way git does."""
import subprocess
from pathlib import Path

from mina_agent.paths import git_hook


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _repo(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "root")
    return repo


def test_plain_checkout(tmp_path):
    repo = _repo(tmp_path)
    assert git_hook(str(repo)) == repo / ".git" / "hooks" / "pre-commit"


def test_hooks_path_with_tilde(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    _git(repo, "config", "core.hooksPath", "~/.githooks")
    assert git_hook(str(repo)) == tmp_path / ".githooks" / "pre-commit"


def test_worktree(tmp_path):
    repo = _repo(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt))
    hook = git_hook(str(wt))
    assert hook == repo / ".git" / "hooks" / "pre-commit"
    assert hook.parent.is_dir()
