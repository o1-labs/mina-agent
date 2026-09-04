"""staged_tree materialises the index, not the working tree."""
import subprocess
from pathlib import Path

from mina_agent.lint import staged_tree


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def test_staged_tree_holds_index_content_only(tmp_path):
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "root")
    (repo / "a.sh").write_text("staged\n")
    _git(repo, "add", "a.sh")
    (repo / "a.sh").write_text("unstaged edit\n")
    (repo / "untracked.sh").write_text("x\n")
    with staged_tree(str(repo)) as tree:
        assert (Path(tree) / "a.sh").read_text() == "staged\n"
        assert not (Path(tree) / "untracked.sh").exists()
        kept = tree
    assert not Path(kept).exists()
