"""Where things live.

Package data (read-only, shipped with the tool):  mina_agent/data/
Project state (generated, gitignored):            <repo>/harness/state/

The project is the Mina checkout that contains the current directory; the
tool is installed editable from <repo>/harness, but nothing here assumes
that beyond the fallback in repo_root().
"""
import os
from pathlib import Path

PKG = Path(__file__).resolve().parent
DATA = PKG / "data"
MANIFEST = DATA / "manifest.toml"
SETTINGS_TEMPLATE = DATA / "settings.template.json"
PHASES = DATA / "phases"
PLUGIN = DATA / "plugin"          # template: .lsp.json for ocamllsp; init writes the resolved copy to state/plugin
SKILLS_DIR_LINK = Path.home() / ".claude" / "skills" / "mina-agent"
VENDOR_DESCRIBE_DUNE = DATA / "vendor" / "describe-dune"
TOOLS = DATA / "tools"            # our own compiler-libs programs (usages.ml); setup compiles them into state/bin
MCP_SERVER_NAME = "mina-harness"


def repo_root(start=None):
    """Walk up from start (default cwd) to the directory holding dune-project.
    Falls back to the checkout this package was installed from."""
    d = Path(start or os.getcwd()).resolve()
    for cand in (d, *d.parents):
        if (cand / "dune-project").exists() and (cand / "src").is_dir():
            return str(cand)
    fallback = PKG.parent.parent
    if (fallback / "dune-project").exists():
        return str(fallback)
    raise RuntimeError("not inside a Mina checkout (no dune-project above cwd)")


def state_dir(repo):
    p = Path(repo) / "harness" / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def derived_json(repo):
    return state_dir(repo) / "derived.json"


def logs_dir(repo):
    p = state_dir(repo) / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def notes_file(repo):
    return state_dir(repo) / "NOTES.md"


def generated_plugin(repo):
    return state_dir(repo) / "plugin"


def describe_dune_bin(repo):
    p = state_dir(repo) / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p / "describe-dune"


def usages_bin(repo):
    p = state_dir(repo) / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p / "usages"


def git_hook(repo, name="pre-commit"):
    """Path of a git hook, as git itself resolves it: honours core.hooksPath
    (including `~`), worktrees and submodules (where .git is a file)."""
    import subprocess
    r = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=repo, capture_output=True, text=True, check=True)
    return (Path(repo) / r.stdout.strip()) / name


def settings_local(repo):
    return Path(repo) / ".claude" / "settings.local.json"
