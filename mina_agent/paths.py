"""Where things live.

Package data (read-only, shipped with the tool):  mina_agent/data/
Generated state (gitignored):                     <harness>/state/

<harness> is the checkout this package runs from: its own git repository,
cloned into the Mina checkout (conventionally as `harness/`, any name works)
so dune can build the sources it vendors. The Mina repo is found from the
working directory, falling back to the directory that contains <harness>.
"""
import os
from pathlib import Path

PKG = Path(__file__).resolve().parent
HARNESS = PKG.parent               # the harness checkout
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
    fallback = HARNESS.parent
    if (fallback / "dune-project").exists():
        return str(fallback)
    raise RuntimeError("not inside a Mina checkout (no dune-project above cwd)")


def harness_relpath(repo) -> str | None:
    """The harness checkout's path relative to the Mina repo, or None when it
    is not inside it."""
    try:
        return str(HARNESS.resolve().relative_to(Path(repo).resolve()))
    except ValueError:
        return None


def dotenv() -> Path:
    return HARNESS / ".envrc"


def state_dir():
    p = HARNESS / "state"
    p.mkdir(parents=True, exist_ok=True)
    return p


def derived_json():
    return state_dir() / "derived.json"


def logs_dir():
    p = state_dir() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def notes_file():
    return state_dir() / "NOTES.md"


def generated_plugin():
    return state_dir() / "plugin"


def describe_dune_bin():
    p = state_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p / "describe-dune"


def usages_bin():
    p = state_dir() / "bin"
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
