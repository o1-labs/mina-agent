"""Local mirror of CI's Lint jobs, scoped to what is being committed.

Each check corresponds to one buildkite/src/Jobs/Lint/*.dhall job and runs
the same thing CI runs (the repo's own scripts where they exist), but only on
the staged files that would trigger that job. A check that needs a tool this
machine lacks reports `skip` loudly rather than passing silently. Nothing
here runs dune.

    mina-agent lint            staged files (what `git commit` would take)
    mina-agent lint --all      the whole tree, e.g. before opening a PR
    mina-agent lint --fix      reformat the failing OCaml files in place
    mina-agent hook pre-commit lint, exit 1 on any failure (installed by init)
"""
import concurrent.futures as cf
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

# src/app/reformat/reformat.ml dirs_trustlist, what `make check-format` skips
FORMAT_SKIP = (".git/", "_build/", "_opam/", "frontend/", "external/", "node_modules/",
               "opam_switches/", "src/lib/snarky/", "src/lib/crypto/kimchi_bindings/stubs/kimchi-stubs-vendors/",
               "src/lib/crypto/proof-systems/", ".direnv/", "harness/")
HADOLINT_IGNORE = ["DL3008", "DL3002", "DL3013", "DL3007", "DL3006", "DL3028"]  # Makefile check-docker


@dataclass
class Result:
    name: str
    job: str                 # the CI job this mirrors
    status: str              # ok | fail | skip | note
    detail: str = ""
    files: list = field(default_factory=list)
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


def _skip_format(path):
    return any(path.startswith(s) or ("/" + s) in ("/" + path) for s in FORMAT_SKIP)


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------

def check_ocamlformat(repo, env, files, staged, fix=False):
    """Lint/OCaml: `make check-format` == `ocamlformat --check` per .ml/.mli
    outside the reformat tool's skip list. Staged mode checks the *index*
    blob, so a file formatted on disk but staged unformatted still fails."""
    targets = [f for f in files if f.endswith((".ml", ".mli")) and not _skip_format(f)]
    if not targets:
        return Result("ocamlformat", "Lint/OCaml", "ok", "no OCaml files in scope")
    aenv = env.activate()
    if not shutil.which("ocamlformat", path=aenv.get("PATH")):
        return Result("ocamlformat", "Lint/OCaml", "skip", "ocamlformat not in the switch", targets)

    def one(path):
        kind = "--intf" if path.endswith(".mli") else "--impl"
        if staged:
            blob = subprocess.run(["git", "show", f":{path}"], cwd=repo, capture_output=True).stdout
            r = subprocess.run(["ocamlformat", "--check", f"--name={path}", kind, "-"],
                               cwd=repo, env=aenv, input=blob, capture_output=True)
        else:
            r = subprocess.run(["ocamlformat", "--check", path], cwd=repo, env=aenv, capture_output=True)
        return path, r.returncode == 0

    with cf.ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as ex:
        bad = sorted(p for p, ok in ex.map(one, targets) if not ok)
    if bad and fix:
        subprocess.run(["ocamlformat", "-i", *bad], cwd=repo, env=aenv)
        return Result("ocamlformat", "Lint/OCaml", "note", f"reformatted {len(bad)} file(s) in the working tree; "
                      "git add them", bad, "git add " + " ".join(bad))
    if bad:
        return Result("ocamlformat", "Lint/OCaml", "fail", f"{len(bad)} of {len(targets)} file(s) need formatting",
                      bad, "mina-agent lint --fix && git add " + " ".join(bad))
    return Result("ocamlformat", "Lint/OCaml", "ok", f"{len(targets)} file(s) formatted")


def check_require_ppxs(repo, env, files, staged):
    """Lint/OCaml second half: scripts/require-ppxs.py (every dune stanza under
    src preprocesses with ppx_version). Whole-tree by nature, cheap; runs
    when a dune file is in scope."""
    if not any(os.path.basename(f) == "dune" and f.startswith("src/") for f in files):
        return Result("require-ppxs", "Lint/OCaml", "ok", "no dune files in scope")
    import sys
    r = subprocess.run([sys.executable, "scripts/require-ppxs.py"], cwd=repo, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("require-ppxs", "Lint/OCaml", "ok", "all dune stanzas preprocess with ppx_version")
    return Result("require-ppxs", "Lint/OCaml", "fail", (r.stdout + r.stderr).strip()[-600:], [],
                  "add ppx_version to the stanza's (preprocess (pps ...))")


def _script_check(name, job, repo, files, trigger, script, extra_args=()):
    if not any(trigger(f) for f in files):
        return Result(name, job, "ok", "not in scope")
    r = subprocess.run(["bash", script, *extra_args], cwd=repo, capture_output=True, text=True)
    if r.returncode == 0:
        return Result(name, job, "ok", (r.stdout.strip().splitlines() or ["ok"])[-1][:120])
    return Result(name, job, "fail", (r.stdout + r.stderr).strip()[-600:])


def check_codeowners(repo, env, files, staged):
    return _script_check("codeowners", "Lint/Fast", repo, files, lambda f: f == "CODEOWNERS",
                         "scripts/lint_codeowners.sh")


def check_rfcs(repo, env, files, staged):
    return _script_check("rfcs", "Lint/Fast", repo, files, lambda f: f.startswith("rfcs/"),
                         "scripts/lint_rfcs.sh")


def check_snarky_submodule(repo, env, files, staged):
    """Lint/Fast: the snarky pointer must be an ancestor of snarky's master/develop.
    Does a `git fetch` inside the submodule; only runs when the pointer moved."""
    return _script_check("snarky-submodule", "Lint/Fast", repo, files, lambda f: f == "src/lib/snarky",
                         "scripts/check-snarky-submodule.sh")


def check_shellcheck(repo, env, files, staged):
    """Lint/Bash: `shellcheck -S warning` on scripts/**/*.sh and buildkite/scripts/**/*.sh."""
    targets = [f for f in files if f.endswith(".sh") and (f.startswith("scripts/") or f.startswith("buildkite/scripts/"))]
    if not targets:
        return Result("shellcheck", "Lint/Bash", "ok", "no shell scripts in scope")
    if not shutil.which("shellcheck"):
        return Result("shellcheck", "Lint/Bash", "skip", "shellcheck not installed (brew install shellcheck); CI will run it", targets)
    r = subprocess.run(["shellcheck", "-S", "warning", *targets], cwd=repo, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("shellcheck", "Lint/Bash", "ok", f"{len(targets)} script(s) clean")
    return Result("shellcheck", "Lint/Bash", "fail", (r.stdout + r.stderr).strip()[-800:], targets)


def check_hadolint(repo, env, files, staged):
    """Lint/Docker: hadolint with the Makefile's ignore list on dockerfiles/."""
    targets = [f for f in files if f.startswith("dockerfiles/")]
    if not targets:
        return Result("hadolint", "Lint/Docker", "ok", "no dockerfiles in scope")
    if not shutil.which("hadolint"):
        return Result("hadolint", "Lint/Docker", "skip", "hadolint not installed (brew install hadolint); CI will run it", targets)
    args = [a for code in HADOLINT_IGNORE for a in ("--ignore", code)]
    r = subprocess.run(["hadolint", *args, *targets], cwd=repo, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("hadolint", "Lint/Docker", "ok", f"{len(targets)} file(s) clean")
    return Result("hadolint", "Lint/Docker", "fail", (r.stdout + r.stderr).strip()[-800:], targets)


def check_dhall(repo, env, files, staged):
    """Lint/Dhall: `make -C buildkite check_syntax check_lint check_format` (needs dhall)."""
    if not any(f.startswith("buildkite/src/") and f.endswith(".dhall") for f in files):
        return Result("dhall", "Lint/Dhall", "ok", "no dhall files in scope")
    from . import dhall
    ok, why = dhall.status(repo)
    if not ok:
        return Result("dhall", "Lint/Dhall", "skip", why)
    lint_env = {**os.environ, "PATH": f"{dhall.binary(repo).parent}:{os.environ.get('PATH', '')}"}
    r = subprocess.run(["make", "-C", "buildkite", "check_syntax", "check_lint", "check_format"],
                       cwd=repo, env=lint_env, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("dhall", "Lint/Dhall", "ok", "syntax, lint, format clean")
    return Result("dhall", "Lint/Dhall", "fail", (r.stdout + r.stderr).strip()[-800:], [], "cd buildkite && make format")


def check_rust(repo, env, files, staged):
    """Lint/Rust: `cargo check` in src/app/trace-tool and src/app/minimina."""
    crates = [c for c in ("src/app/trace-tool", "src/app/minimina") if any(f.startswith(c + "/") for f in files)]
    if not crates:
        return Result("cargo-check", "Lint/Rust", "ok", "no rust crates in scope")
    if not shutil.which("cargo"):
        return Result("cargo-check", "Lint/Rust", "skip", "cargo not installed; CI will run cargo check", crates)
    bad = []
    for c in crates:
        r = subprocess.run(["cargo", "check", "--quiet"], cwd=os.path.join(repo, c), capture_output=True, text=True)
        if r.returncode != 0:
            bad.append(f"{c}: {r.stderr.strip()[-300:]}")
    if bad:
        return Result("cargo-check", "Lint/Rust", "fail", "\n".join(bad), crates)
    return Result("cargo-check", "Lint/Rust", "ok", f"{len(crates)} crate(s) check clean")


def check_archive_upgrade(repo, env, files, staged):
    """Lint/ArchiveUpgrade: schema changes need an upgrade script (compares to develop)."""
    if not any(f.startswith("src/app/archive/") and f.endswith(".sql") for f in files):
        return Result("archive-upgrade", "Lint/ArchiveUpgrade", "ok", "no archive schema changes in scope")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    ci_env = {**os.environ, "BUILDKITE_BRANCH": branch, "BUILDKITE_BUILD_NUM": "local",
              "BUILDKITE_BUILD_URL": "local", "MINA_DEB_CODENAME": os.environ.get("MINA_DEB_CODENAME", "bullseye")}
    r = subprocess.run(["bash", "buildkite/scripts/archive/upgrade-script-check.sh", "--mode", "verbose",
                        "--comparison-branch", "develop"], cwd=repo, env=ci_env, capture_output=True, text=True)
    if r.returncode == 0:
        return Result("archive-upgrade", "Lint/ArchiveUpgrade", "ok", "upgrade script present")
    return Result("archive-upgrade", "Lint/ArchiveUpgrade", "fail", (r.stdout + r.stderr).strip()[-600:])


def check_changelog(repo, env, files, staged):
    """Lint/Changelog: PR-scoped (changes/<PR number>.md), so per commit this is
    a note: src/ changes on this branch with no changes/*.md yet."""
    if not any(f.startswith("src/") for f in files):
        return Result("changelog", "Lint/Changelog", "ok", "no src changes in scope")
    branch_changes = _git(repo, "diff", "--name-only", "develop...HEAD", "--", "changes/").split()
    staged_changes = [f for f in files if f.startswith("changes/")]
    if branch_changes or staged_changes:
        return Result("changelog", "Lint/Changelog", "ok", "changes/ entry present on this branch")
    return Result("changelog", "Lint/Changelog", "note",
                  "src/ changed but no changes/<PR>.md on this branch; CI's changelog lint checks the PR, "
                  "add changes/<PR number>.md before opening it")


def check_merges(repo, env, files, staged):
    """Lint/Merge: branch-level (merges cleanly into compatible/develop/master); not a commit check."""
    return Result("merges-cleanly", "Lint/Merge", "note", "branch-level check, run by CI on the PR")


CHECKS = [check_ocamlformat, check_require_ppxs, check_codeowners, check_rfcs, check_snarky_submodule,
          check_shellcheck, check_hadolint, check_dhall, check_rust, check_archive_upgrade,
          check_changelog, check_merges]


def run(env, *, scope="staged", fix=False):
    repo = env.repo
    files = staged_files(repo) if scope == "staged" else all_files(repo)
    results = []
    for chk in CHECKS:
        if chk is check_ocamlformat:
            results.append(chk(repo, env, files, scope == "staged", fix=fix))
        else:
            results.append(chk(repo, env, files, scope == "staged"))
    return files, results
