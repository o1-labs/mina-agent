"""Local mirror of CI's Lint jobs, scoped to what is being committed.

Each check corresponds to one buildkite/src/Jobs/Lint/*.dhall job and runs
the same thing CI runs (the repo's own scripts where they exist), but only on
the staged files that would trigger that job. In staged mode the index is
materialised into a temporary tree (`git checkout-index`, deleted afterwards)
and content checks run there, so they judge what the commit will contain,
not what is on disk. Checks that are about git state (submodule pointer,
branch comparisons) and cargo (its target cache) use the real checkout. A
check that needs a tool this machine lacks reports `skip` loudly rather than
passing silently. Nothing here runs dune.

    mina-agent lint            staged files (what `git commit` would take)
    mina-agent lint --all      the whole tree, e.g. before opening a PR
    mina-agent lint --fix      reformat the failing OCaml files in place
    mina-agent hook pre-commit lint, exit 1 on any failure (installed by init)
"""
import concurrent.futures as cf
import contextlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import paths
from .model import Status

# src/app/reformat/reformat.ml dirs_trustlist, what `make check-format` skips
FORMAT_SKIP = (".git/", "_build/", "_opam/", "frontend/", "external/", "node_modules/",
               "opam_switches/", "src/lib/snarky/", "src/lib/crypto/kimchi_bindings/stubs/kimchi-stubs-vendors/",
               "src/lib/crypto/proof-systems/", ".direnv/", "harness/")
HADOLINT_IGNORE = ["DL3008", "DL3002", "DL3013", "DL3007", "DL3006", "DL3028"]  # Makefile check-docker


@dataclass(frozen=True)
class Result:
    name: str
    job: str                 # the CI job this mirrors
    status: Status
    detail: str = ""
    files: list[str] = field(default_factory=list)
    fix: str = ""


def _git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True).stdout


def staged_files(repo):
    """Added/copied/modified/renamed paths in the index (new names for renames)."""
    out = _git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.split("\0") if p]


def all_files(repo):
    out = _git(repo, "ls-files", "-z")
    return [p for p in out.split("\0") if p]


@contextlib.contextmanager
def staged_tree(repo):
    """A temporary directory holding the index contents (staged blobs, no
    untracked or unstaged edits). Removed on exit."""
    with tempfile.TemporaryDirectory(prefix="mina-lint-") as tmp:
        subprocess.run(["git", "checkout-index", "-a", f"--prefix={tmp}/"], cwd=repo, check=True)
        yield tmp


def _skip_format(path, repo=None):
    rel = paths.harness_relpath(repo) if repo else None
    skips = FORMAT_SKIP + ((rel + "/",) if rel and rel != "." else ())
    return any(path.startswith(s) or ("/" + s) in ("/" + path) for s in skips)


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_ocamlformat(repo, tree, env, files, staged, fix=False):
    """Lint/OCaml: `make check-format` == `ocamlformat --check` per .ml/.mli
    outside the reformat tool's skip list. Runs in `tree`, so in staged mode
    a file formatted on disk but staged unformatted still fails; --fix
    rewrites the working tree."""
    targets = [f for f in files if f.endswith((".ml", ".mli")) and not _skip_format(f, repo)]
    if not targets:
        return Result("ocamlformat", "Lint/OCaml", Status.OK, "no OCaml files in scope")
    aenv = env.activate()
    if not shutil.which("ocamlformat", path=aenv.get("PATH")):
        return Result("ocamlformat", "Lint/OCaml", Status.SKIP, "ocamlformat not in the switch", targets)

    def one(path):
        r = subprocess.run(["ocamlformat", "--check", path], cwd=tree, env=aenv, capture_output=True)
        return path, r.returncode == 0

    with cf.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        bad = sorted(p for p, ok in ex.map(one, targets) if not ok)
    if bad and fix:
        subprocess.run(["ocamlformat", "-i", *bad], cwd=repo, env=aenv)
        return Result("ocamlformat", "Lint/OCaml", Status.NOTE, f"reformatted {len(bad)} file(s) in the working tree; "
                      "git add them", bad, "git add " + " ".join(bad))
    if bad:
        return Result("ocamlformat", "Lint/OCaml", Status.FAIL, f"{len(bad)} of {len(targets)} file(s) need formatting",
                      bad, "mina-agent lint --fix && git add " + " ".join(bad))
    return Result("ocamlformat", "Lint/OCaml", Status.OK, f"{len(targets)} file(s) formatted")


def check_require_ppxs(repo, tree, env, files, staged):
    """Lint/OCaml second half: scripts/require-ppxs.py (every dune stanza under
    src preprocesses with ppx_version). Whole-tree by nature, cheap; runs
    when a dune file is in scope."""
    if not any(os.path.basename(f) == "dune" and f.startswith("src/") for f in files):
        return Result("require-ppxs", "Lint/OCaml", Status.OK, "no dune files in scope")
    import sys
    r = subprocess.run([sys.executable, "scripts/require-ppxs.py"], cwd=tree, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("require-ppxs", "Lint/OCaml", Status.OK, "all dune stanzas preprocess with ppx_version")
    return Result("require-ppxs", "Lint/OCaml", Status.FAIL, (r.stdout + r.stderr).strip()[-600:], [],
                  "add ppx_version to the stanza's (preprocess (pps ...))")


def _script_check(name, job, cwd, files, trigger, script, extra_args=()):
    if not any(trigger(f) for f in files):
        return Result(name, job, Status.OK, "not in scope")
    r = subprocess.run(["bash", script, *extra_args], cwd=cwd, capture_output=True, text=True)
    if r.returncode == 0:
        return Result(name, job, Status.OK, (r.stdout.strip().splitlines() or ["ok"])[-1][:120])
    return Result(name, job, Status.FAIL, (r.stdout + r.stderr).strip()[-600:])


def check_codeowners(repo, tree, env, files, staged):
    return _script_check("codeowners", "Lint/Fast", tree, files, lambda f: f == "CODEOWNERS",
                         "scripts/lint_codeowners.sh")


def check_rfcs(repo, tree, env, files, staged):
    return _script_check("rfcs", "Lint/Fast", tree, files, lambda f: f.startswith("rfcs/"),
                         "scripts/lint_rfcs.sh")


def check_snarky_submodule(repo, tree, env, files, staged):
    """Lint/Fast: the snarky pointer must be an ancestor of snarky's master/develop.
    Does a `git fetch` inside the submodule; only runs when the pointer moved."""
    return _script_check("snarky-submodule", "Lint/Fast", repo, files, lambda f: f == "src/lib/snarky",
                         "scripts/check-snarky-submodule.sh")


def check_shellcheck(repo, tree, env, files, staged):
    """Lint/Bash: `shellcheck -S warning` on scripts/**/*.sh and buildkite/scripts/**/*.sh."""
    targets = [f for f in files if f.endswith(".sh") and (f.startswith("scripts/") or f.startswith("buildkite/scripts/"))]
    if not targets:
        return Result("shellcheck", "Lint/Bash", Status.OK, "no shell scripts in scope")
    if not shutil.which("shellcheck"):
        return Result("shellcheck", "Lint/Bash", Status.SKIP, "shellcheck not installed (brew install shellcheck); CI will run it", targets)
    r = subprocess.run(["shellcheck", "-S", "warning", *targets], cwd=tree, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("shellcheck", "Lint/Bash", Status.OK, f"{len(targets)} script(s) clean")
    return Result("shellcheck", "Lint/Bash", Status.FAIL, (r.stdout + r.stderr).strip()[-800:], targets)


def check_hadolint(repo, tree, env, files, staged):
    """Lint/Docker: hadolint with the Makefile's ignore list on dockerfiles/."""
    targets = [f for f in files if f.startswith("dockerfiles/")]
    if not targets:
        return Result("hadolint", "Lint/Docker", Status.OK, "no dockerfiles in scope")
    if not shutil.which("hadolint"):
        return Result("hadolint", "Lint/Docker", Status.SKIP, "hadolint not installed (brew install hadolint); CI will run it", targets)
    args = [a for code in HADOLINT_IGNORE for a in ("--ignore", code)]
    r = subprocess.run(["hadolint", *args, *targets], cwd=tree, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("hadolint", "Lint/Docker", Status.OK, f"{len(targets)} file(s) clean")
    return Result("hadolint", "Lint/Docker", Status.FAIL, (r.stdout + r.stderr).strip()[-800:], targets)


def check_dhall(repo, tree, env, files, staged):
    """Lint/Dhall: `make -C buildkite check_syntax check_lint check_format` (needs dhall).
    The make targets `sed -i` Base.dhall first; running in `tree` keeps that
    out of the checkout in staged mode."""
    if not any(f.startswith("buildkite/src/") and f.endswith(".dhall") for f in files):
        return Result("dhall", "Lint/Dhall", Status.OK, "no dhall files in scope")
    from . import dhall
    ok, why = dhall.status(repo)
    if not ok:
        return Result("dhall", "Lint/Dhall", Status.SKIP, why)
    lint_env = {**os.environ, "PATH": f"{dhall.binary(repo).parent}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(["make", "-C", "buildkite", "check_syntax", "check_lint", "check_format"],
                       cwd=tree, env=lint_env, capture_output=True, text=True)
    if tree == repo:
        # --all mode runs in the checkout; undo the sed as CI's disposable checkout never needs to
        subprocess.run(["make", "-C", "buildkite", "convert_ifs_to_backticks"],
                       cwd=repo, env=lint_env, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("dhall", "Lint/Dhall", Status.OK, "syntax, lint, format clean")
    return Result("dhall", "Lint/Dhall", Status.FAIL, (r.stdout + r.stderr).strip()[-800:], [], "cd buildkite && make format")


def check_rust(repo, tree, env, files, staged):
    """Lint/Rust: `cargo check` in src/app/trace-tool and src/app/minimina.
    Runs in the checkout (not `tree`) to reuse the crates' target cache."""
    crates = [c for c in ("src/app/trace-tool", "src/app/minimina") if any(f.startswith(c + "/") for f in files)]
    if not crates:
        return Result("cargo-check", "Lint/Rust", Status.OK, "no rust crates in scope")
    if not shutil.which("cargo"):
        return Result("cargo-check", "Lint/Rust", Status.SKIP, "cargo not installed; CI will run cargo check", crates)
    bad = []
    for c in crates:
        r = subprocess.run(["cargo", "check", "--quiet"], cwd=os.path.join(repo, c), capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f"{c}: {r.stderr.strip()[-300:]}")
    if bad:
        return Result("cargo-check", "Lint/Rust", Status.FAIL, "\n".join(bad), crates)
    return Result("cargo-check", "Lint/Rust", Status.OK, f"{len(crates)} crate(s) check clean")


def check_archive_upgrade(repo, tree, env, files, staged):
    """Lint/ArchiveUpgrade: schema changes need an upgrade script (compares to develop)."""
    if not any(f.startswith("src/app/archive/") and f.endswith(".sql") for f in files):
        return Result("archive-upgrade", "Lint/ArchiveUpgrade", Status.OK, "no archive schema changes in scope")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    ci_env = {**os.environ, "BUILDKITE_BRANCH": branch, "BUILDKITE_BUILD_NUM": "local",
              "BUILDKITE_BUILD_URL": "local", "MINA_DEB_CODENAME": os.environ.get("MINA_DEB_CODENAME", "bullseye")}
    r = subprocess.run(["bash", "buildkite/scripts/archive/upgrade-script-check.sh", "--mode", "verbose",
                        "--comparison-branch", "develop"], cwd=repo, env=ci_env, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("archive-upgrade", "Lint/ArchiveUpgrade", Status.OK, "upgrade script present")
    return Result("archive-upgrade", "Lint/ArchiveUpgrade", Status.FAIL, (r.stdout + r.stderr).strip()[-600:])


def check_changelog(repo, tree, env, files, staged):
    """Lint/Changelog: PR-scoped (changes/<PR number>.md), so per commit this is
    a note: src/ changes on this branch with no changes/*.md yet."""
    if not any(f.startswith("src/") for f in files):
        return Result("changelog", "Lint/Changelog", Status.OK, "no src changes in scope")
    branch_changes = _git(repo, "diff", "--name-only", "develop...HEAD", "--", "changes/").split()
    staged_changes = [f for f in files if f.startswith("changes/")]
    if branch_changes or staged_changes:
        return Result("changelog", "Lint/Changelog", Status.OK, "changes/ entry present on this branch")
    return Result("changelog", "Lint/Changelog", Status.NOTE,
                  "src/ changed but no changes/<PR>.md on this branch; CI's changelog lint checks the PR, "
                  "add changes/<PR number>.md before opening it")


def check_merges(repo, tree, env, files, staged):
    """Lint/Merge: branch-level (merges cleanly into compatible/develop/master); not a commit check."""
    return Result("merges-cleanly", "Lint/Merge", Status.NOTE, "branch-level check, run by CI on the PR")


CHECKS = [check_ocamlformat, check_require_ppxs, check_codeowners, check_rfcs, check_snarky_submodule,
          check_shellcheck, check_hadolint, check_dhall, check_rust, check_archive_upgrade,
          check_changelog, check_merges]


def run(env, *, scope="staged", fix=False, caller="cli"):
    repo = env.repo
    staged = scope == "staged"
    files = staged_files(repo) if staged else all_files(repo)
    with (staged_tree(repo) if staged else contextlib.nullcontext(repo)) as tree:
        results = [chk(repo, tree, env, files, staged, **({"fix": fix} if chk is check_ocamlformat else {}))
                   for chk in CHECKS]
    record(repo, scope, caller, files, results)
    return files, results


def log_path(repo):
    from . import paths
    return paths.logs_dir() / "lint.jsonl"


def record(repo, scope, caller, files, results):
    """Append one JSON line per lint run to harness/state/logs/lint.jsonl, so
    every commit gate decision (including the hook's) can be audited."""
    import datetime as dt
    import json
    rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "scope": scope, "caller": caller,
           "head": _git(repo, "rev-parse", "--short", "HEAD").strip(),
           "files": files if scope == "staged" else len(files),
           "blocked": any(r.status is Status.FAIL for r in results),
           "results": [{"name": r.name, "status": r.status, "detail": r.detail[:200],
                        "files": r.files[:20]} for r in results]}
    with open(log_path(repo), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def history(repo, n=10):
    import json
    p = log_path(repo)
    if not p.exists():
        return []
    lines = p.read_text().splitlines()[-n:]
    return [json.loads(l) for l in lines if l.strip()]
