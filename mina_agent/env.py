#!/usr/bin/env python3
"""Environment adapter for the Mina agent harness.

This is the only file in the harness that knows how the OCaml toolchain is
reached. Everything else either imports `detect()` or shells out to
`mina-agent exec -- <cmd>`.

CLI (via mina-agent):
    mina-agent status --json                 # JSON description of the env
    mina-agent exec -- <cmd...>              # run cmd inside the activated env

Env vars:
    HARNESS_MODE=opam|nix   override detection
    HARNESS_VERBOSE=1       print argv and the activation diff to stderr
    HARNESS_DRY_RUN=1       print argv as JSON and exit 0 without running

Exit codes for `run`:
    child's exit code on success
    2  bad usage or bad HARNESS_MODE
    3  no usable toolchain (mode "none")

Design notes:
  * Activation happens once, at the launcher boundary. `activate()` returns a
    full environment dict; children inherit it. If the current process is
    already inside an activated shell, activate() is a no-op copy.
  * Nothing here mutates the switch, the store, or the filesystem.
  * Nix is a reserved mode. Detection of an *already entered* nix shell is
    two lines; entering one is a stub (see _nix_activate).
"""
import dataclasses
import json
import os
import shutil
import subprocess
import sys
import time

MODES = ("opam", "nix", "none")

# Environment variables that change dune's behaviour. Reported, never set.
REPORTED_VARS = ("DUNE_PROFILE", "KIMCHI_STUBS", "OPAM_SWITCH_PREFIX",
                 "IN_NIX_SHELL", "MINA_LIBP2P_HELPER_PATH")

# From Makefile:162: (ulimit -s 65532 || true) && (ulimit -n 10240 || true)
RLIMIT_STACK_KB = 65532
RLIMIT_NOFILE = 10240


def _repo_root():
    from . import paths
    return paths.repo_root()


def _real(p):
    return os.path.realpath(p) if p else None


def _under(path, prefix):
    if not path or not prefix:
        return False
    path = _real(path)
    prefix = _real(prefix).rstrip(os.sep) + os.sep
    return path.startswith(prefix)


def _build_provenance(repo):
    """Read _build/log and classify the ocamlc that produced it."""
    log = os.path.join(repo, "_build", "log")
    info = {"exists": os.path.isdir(os.path.join(repo, "_build")),
            "built_by": None, "ocamlc": None}
    if not os.path.exists(log):
        return info
    try:
        with open(log, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if "ocamlc" in line and " -config" in line:
                    tok = [t for t in line.split() if t.endswith("ocamlc.opt")
                           or t.endswith("/ocamlc")]
                    if tok:
                        info["ocamlc"] = tok[0]
                        break
                if line.startswith("$"):
                    break  # first command should have been the -config probe
    except OSError:
        return info
    oc = info["ocamlc"]
    if oc:
        if oc.startswith("/nix/store/"):
            info["built_by"] = "nix"
        elif _under(oc, os.path.join(repo, "_opam")) or "opam" in oc:
            info["built_by"] = "opam"
        else:
            info["built_by"] = "unknown"
    return info


@dataclasses.dataclass
class Env:
    mode: str
    activated: bool
    reasons: list
    warnings: list
    repo: str
    dune: str = None
    dune_version: str = None
    ocaml: str = None
    ocaml_bin: str = None
    build_dir: dict = None
    env: dict = None
    _activated_env: dict = dataclasses.field(default=None, repr=False)

    # -- activation ---------------------------------------------------------

    def activate(self):
        """Return a complete environment dict for this mode. Cached."""
        if self._activated_env is not None:
            return self._activated_env
        if self.mode == "none":
            raise RuntimeError(
                "no usable toolchain: activate the opam switch "
                "(e.g. `direnv allow` or `eval $(opam env --switch . --set-switch)`) "
                "or enter `nix develop` first")
        if self.activated:
            self._activated_env = dict(os.environ)
        elif self.mode == "opam":
            self._activated_env = _opam_activate(self.repo)
        elif self.mode == "nix":
            self._activated_env = _nix_activate(self.repo)
        else:
            raise RuntimeError(f"unknown mode {self.mode}")
        # Skip the JS/wasm bindings the way CI does (buildkite unit-test.sh):
        # building them regenerates committed .node/.d.ts artifacts under
        # kimchi_bindings/js, dirtying a clean tree. The harness only builds
        # and type-checks OCaml, so it never needs them.
        self._activated_env.setdefault("NO_JS_BUILD", "1")
        return self._activated_env

    def argv(self, cmd):
        """The argv that run() will execute. No wrapper exists any more; kept
        as a seam so callers never build argv themselves."""
        return list(cmd)

    def run(self, cmd, capture=False, cwd=None, **kw):
        """Run cmd in the activated env. Returns CompletedProcess.

        dune invocations take a cross-process lock (harness/state/dune.lock):
        dune 3.3.1 does not serialize concurrent instances on one _build, and
        two of them (server + hook, or exec + a phase) corrupt each other's
        temporary artifacts. ocamllsp's own read-only `dune ocaml-merlin`
        calls are outside this and harmless."""
        argv = self.argv(cmd)
        env = self.activate()
        cwd = cwd or self.repo
        if os.environ.get("HARNESS_VERBOSE"):
            _log(f"[harness] cwd={cwd}")
            _log(f"[harness] argv={json.dumps(argv)}")
            for k, (old, new) in _env_diff(os.environ, env).items():
                _log(f"[harness] env {k}: {old!r} -> {new!r}")
        with _dune_lock(self.repo, argv):
            return subprocess.run(_with_limits(argv), env=env, cwd=cwd,
                                  capture_output=capture, text=True, **kw)

    # -- reporting ----------------------------------------------------------

    def to_dict(self):
        d = dataclasses.asdict(self)
        d.pop("_activated_env", None)
        return d

    def to_json(self):
        return json.dumps(self.to_dict(), indent=2)


def _opam_activate(repo):
    """Compute the env `opam env` would produce for the repo-local switch."""
    opam = shutil.which("opam")
    if not opam:
        raise RuntimeError("opam not on PATH")
    out = subprocess.run(
        [opam, "env", f"--switch={repo}", "--set-switch", "--sexp"],
        capture_output=True, text=True, check=True).stdout
    env = dict(os.environ)
    env.update(_parse_opam_sexp(out))
    return env


def _parse_opam_sexp(text):
    """Parse `opam env --sexp`: (("K" "V") ("K" "V") ...)."""
    vals = {}
    i, n = 0, len(text)
    key = None
    while i < n:
        c = text[i]
        if c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            s = "".join(buf)
            if key is None:
                key = s
            else:
                vals[key] = s
                key = None
            i = j + 1
        else:
            i += 1
    return vals


def _nix_activate(repo):
    """Stub. Entering a nix shell from outside is not implemented.

    A future implementation would run something like
        nix develop "git+file://<repo>?submodules=1#default" --command env -0
    or `nix print-dev-env`, and parse the result into a dict. See
    nix/README.md (flake ref, shell names) and buildkite/scripts/test-nix.sh
    (the invocation CI uses). Not built because it cannot be tested on this
    machine: the Mina binary cache holds only x86_64-linux paths.
    """
    raise RuntimeError(
        "nix mode is a stub: enter `nix develop` yourself, then rerun "
        "(IN_NIX_SHELL will be detected)")


# Shell prelude that mirrors Makefile:162. Done in sh rather than with
# resource.setrlimit because macOS refuses to raise RLIMIT_STACK from a
# Python process (EINVAL) while accepting the same value from a shell. The
# Makefile's literal 65532 exceeds the macOS hard limit (65520), so fall back
# to the hard limit, then to leaving it alone, like the Makefile's `|| true`.
LIMITS_PRELUDE = (
    f"ulimit -s {RLIMIT_STACK_KB} 2>/dev/null || ulimit -s $(ulimit -H -s) 2>/dev/null || true; "
    f"ulimit -n {RLIMIT_NOFILE} 2>/dev/null || true; "
    'exec "$@"'
)


def _with_limits(argv):
    """Wrap argv so it runs under the raised ulimits."""
    return ["/bin/sh", "-c", LIMITS_PRELUDE, "harness-env"] + list(argv)


import contextlib


@contextlib.contextmanager
def _dune_lock(repo, argv):
    """fcntl lock held for the duration of a dune invocation; no-op otherwise."""
    first = os.path.basename(argv[0]) if argv else ""
    if first != "dune":
        yield
        return
    import fcntl
    from . import paths
    lock_path = paths.state_dir(repo) / "dune.lock"
    with open(lock_path, "w") as fh:
        t0 = time.time()
        fcntl.flock(fh, fcntl.LOCK_EX)
        waited = time.time() - t0
        if waited > 1 and os.environ.get("HARNESS_VERBOSE"):
            _log(f"[harness] waited {waited:.1f}s for dune.lock")
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _env_diff(old, new):
    diff = {}
    for k in sorted(set(old) | set(new)):
        if old.get(k) != new.get(k):
            diff[k] = (old.get(k), new.get(k))
    return diff


def _log(msg):
    sys.stderr.write(msg + "\n")


# -- detection ---------------------------------------------------------------

def detect():
    repo = _repo_root()
    reasons, warnings = [], []
    override = os.environ.get("HARNESS_MODE")
    dune = shutil.which("dune")
    dune_real = _real(dune)
    opam_dir = os.path.join(repo, "_opam")
    switch_prefix = os.environ.get("OPAM_SWITCH_PREFIX")

    in_nix = bool(os.environ.get("IN_NIX_SHELL")) and bool(dune_real) and \
        dune_real.startswith("/nix/store/")
    in_opam = bool(dune_real) and (_under(dune_real, opam_dir) or
                                   _under(dune_real, switch_prefix))

    if override:
        if override not in ("opam", "nix"):
            raise SystemExit(
                _usage_error(f"HARNESS_MODE={override!r}: expected opam or nix"))
        mode = override
        reasons.append(f"HARNESS_MODE={override} override")
        if mode == "nix":
            warnings.append("nix mode is a stub and unverified on this machine")
            activated = in_nix
        else:
            activated = in_opam
    elif in_nix:
        mode, activated = "nix", True
        reasons.append(f"IN_NIX_SHELL set and dune is {dune_real}")
    elif in_opam:
        mode, activated = "opam", True
        reasons.append(f"dune resolves to {dune_real}, under the repo-local switch")
    elif os.path.exists(opam_dir) and shutil.which("opam"):
        mode, activated = "opam", False
        reasons.append(f"{opam_dir} exists and opam is on PATH, "
                       "but dune on PATH is not from it")
    else:
        mode, activated = "none", False
        reasons.append("no repo-local _opam switch reachable and not in a nix shell")

    build = _build_provenance(repo)
    if build["built_by"] and mode != "none" and build["built_by"] != mode:
        warnings.append(
            f"_build was produced by {build['built_by']} but mode is {mode}; "
            "a full rebuild is likely")
    if not shutil.which("ocamllsp"):
        warnings.append("ocamllsp not on PATH")
    if not shutil.which("gsed") and sys.platform == "darwin":
        warnings.append("gsed not on PATH; src/lib/mina_block/tests needs GNU sed")

    e = Env(mode=mode, activated=activated, reasons=reasons, warnings=warnings,
            repo=repo, build_dir=build,
            env={k: os.environ.get(k) for k in REPORTED_VARS})

    if mode != "none":
        try:
            aenv = e.activate()
            e.dune = shutil.which("dune", path=aenv.get("PATH"))
            ocamlc = shutil.which("ocamlc", path=aenv.get("PATH"))
            e.ocaml_bin = os.path.dirname(ocamlc) if ocamlc else None
            e.dune_version = _version([e.dune, "--version"], aenv)
            e.ocaml = _version([ocamlc, "-version"], aenv)
        except Exception as ex:  # activation failed; report, don't crash
            e.warnings.append(f"activation failed: {ex}")
    return e


def _version(argv, env):
    if not argv[0]:
        return None
    try:
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              timeout=30).stdout.strip() or None
    except Exception:
        return None
