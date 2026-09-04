#!/usr/bin/env python3
"""Tool implementations for the mina-harness MCP server and hooks.

server.py wraps each public function listed in TOOLS as an MCP tool; the
hook commands and discuss call them directly. See server.py for the rules
(all shell-outs via env.py, one dune lock, graph re-derived on change).
"""

import json
import os
import re
import shutil
import sys
import threading
import time
import time
import tomllib

from . import env as envmod
from . import graph as derivemod
from . import paths

MANIFEST = str(paths.MANIFEST)
RAW_TAIL_BYTES = 4096
DUNE_LOCK = threading.Lock()


# --------------------------------------------------------------------------
# environment + graph
# --------------------------------------------------------------------------

class Graph:
    """derived.json, kept current against the dune metadata mtimes."""

    STALE_CHECK_INTERVAL = 2.0   # seconds; bound the whole-tree walk under call storms

    def __init__(self, env):
        self.env = env
        self.data = None
        self.stamp = None
        self.error = None
        self._checked_at = 0.0
        self.refresh(force=True)

    def _stamp(self):
        st = []
        for root, dirs, files in os.walk(os.path.join(self.env.repo, "src")):
            dirs[:] = [d for d in dirs if d not in derivemod.SKIP_DIRS]
            for f in files:
                if f == "dune" or f == "dune-project" or f.endswith((".inc", ".opam")):
                    p = os.path.join(root, f)
                    try:
                        st.append((p, os.stat(p).st_mtime_ns))
                    except OSError:
                        pass
        return hash(tuple(sorted(st)))

    def refresh(self, force=False):
        s = self._stamp()
        if not force and s == self.stamp:
            return False
        try:
            self.data = derivemod.derive_and_write(self.env)
            self.error = None
        except BaseException as ex:  # SystemExit from derive.py included
            self.error = f"derive failed: {ex}"
        self.stamp = s
        return True

    def get(self):
        # Re-stamp (a full source-tree walk) at most once per interval, so a
        # burst of graph queries in one command does not re-walk the tree per
        # call. A mid-session edit is still picked up within the interval.
        now = time.time()
        if now - self._checked_at >= self.STALE_CHECK_INTERVAL:
            self._checked_at = now
            self.refresh()
        if self.error:
            raise RuntimeError(self.error)
        return self.data


ENV = envmod.detect()
GRAPH = Graph(ENV)
with open(MANIFEST, "rb") as _fh:
    MANIFEST_DATA = tomllib.load(_fh)


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def rel(path):
    """Normalize to a repo-relative path; refuse anything outside the repo."""
    p = path if os.path.isabs(path) else os.path.join(ENV.repo, path)
    p = os.path.normpath(p)
    root = ENV.repo.rstrip(os.sep) + os.sep
    if not (p + os.sep).startswith(root):
        raise ValueError(f"{path} is outside the repo")
    return os.path.relpath(p, ENV.repo)


def enclosing_dune_dir(relpath):
    """Nearest ancestor directory (inclusive) containing a dune file."""
    d = relpath if os.path.isdir(os.path.join(ENV.repo, relpath)) else os.path.dirname(relpath)
    while True:
        if os.path.exists(os.path.join(ENV.repo, d, "dune")):
            return d or "."
        if d in ("", "."):
            return None
        d = os.path.dirname(d)


def unit_of(relpath):
    """(kind, key, record) of the unit whose dir encloses relpath, or None."""
    g = GRAPH.get()
    d = relpath if os.path.isdir(os.path.join(ENV.repo, relpath)) else os.path.dirname(relpath)
    while True:
        units = g["by_dir"].get(d)
        if units:
            # a dir with several (tests (names ...)): pick the one named after the file
            base = os.path.splitext(os.path.basename(relpath))[0]
            for u in units:
                if u["kind"] in ("test", "exe") and u["name"] == base:
                    table = {"test": "tests", "exe": "executables"}[u["kind"]]
                    return u["kind"], u["key"], g[table][u["key"]]
            # otherwise prefer a library; a dir with lib + tests is described by its lib
            for kind in ("lib", "test", "exe"):
                for u in units:
                    if u["kind"] == kind:
                        table = {"lib": "libraries", "test": "tests", "exe": "executables"}[kind]
                        return kind, u["key"], g[table][u["key"]]
        if d in ("", "."):
            return None
        d = os.path.dirname(d)


# --------------------------------------------------------------------------
# dune output parsing
# --------------------------------------------------------------------------

HEADER = re.compile(r'^File "([^"]+)", line (\d+), characters (\d+)-(\d+):')
HEADER_NOCOL = re.compile(r'^File "([^"]+)", line (\d+)')
SEVERITY = re.compile(r"^(Error(?: \([^)]*\))?|Warning(?: \d+)?(?: \[[^\]]*\])?):\s*(.*)")


def split_diags(text):
    """(errors, warnings) from dune output."""
    d = parse_dune_errors(text)
    return ([e for e in d if e["severity"] == "error"],
            [e for e in d if e["severity"] == "warning"])


def parse_dune_errors(text):
    """Turn dune/ocaml diagnostics into [{file,line,col_start,col_end,severity,message}]."""
    out = []
    cur = None
    for line in text.splitlines():
        m = HEADER.match(line) or HEADER_NOCOL.match(line)
        if m:
            if cur:
                out.append(cur)
            g = m.groups()
            cur = {"file": g[0], "line": int(g[1]),
                   "col_start": int(g[2]) if len(g) > 2 else None,
                   "col_end": int(g[3]) if len(g) > 3 else None,
                   "severity": None, "message": ""}
            continue
        if cur is None:
            continue
        s = SEVERITY.match(line)
        if s:
            cur["severity"] = "error" if s.group(1).startswith("Error") else "warning"
            cur["message"] = s.group(2).strip()
        elif cur["severity"] and line.strip():
            cur["message"] += " " + line.strip()
        elif cur["severity"] and not line.strip():
            out.append(cur)
            cur = None
    if cur:
        out.append(cur)
    return [e for e in out if e["severity"]]


INLINE_FAIL = re.compile(r'^File "([^"]+)", line (\d+), characters [\d-]+: (.*) (?:threw|is false)')
INLINE_SUMMARY = re.compile(r"^\d+ tests? ran, \d+ test_modules? ran")
ALCOTEST_FAIL = re.compile(r"^\s*\[FAIL\]\s+(.*)")
ALCOTEST_SUMMARY = re.compile(r"^\d+ failures?!|^Test Successful in|^\d+ tests? run")


def parse_test_output(text):
    failures, summary = [], None
    for line in text.splitlines():
        m = INLINE_FAIL.match(line)
        if m:
            failures.append({"file": m.group(1), "line": int(m.group(2)), "name": m.group(3)})
            continue
        m = ALCOTEST_FAIL.match(line)
        if m:
            failures.append({"file": None, "line": None, "name": m.group(1).strip()})
            continue
        if INLINE_SUMMARY.match(line) or ALCOTEST_SUMMARY.match(line):
            summary = line.strip()
    return failures, summary


def tail(text):
    return text[-RAW_TAIL_BYTES:]


# --------------------------------------------------------------------------
# running dune
# --------------------------------------------------------------------------

def run_dune(argv, timeout_s):
    """Run argv through the env adapter under the dune lock."""
    if ENV.mode == "none":
        raise RuntimeError("no usable toolchain: " + "; ".join(ENV.reasons))
    t0 = time.time()
    with DUNE_LOCK:
        try:
            r = ENV.run(argv, capture=True, timeout=timeout_s)
            code, out = r.returncode, (r.stdout or "") + (r.stderr or "")
            timed_out = False
        except Exception as ex:  # subprocess.TimeoutExpired, without importing it
            if type(ex).__name__ != "TimeoutExpired":
                raise
            code, timed_out = -1, True
            out = ((ex.stdout or b"") if isinstance(ex.stdout, bytes) else (ex.stdout or ""))
            if isinstance(out, bytes):
                out = out.decode(errors="replace")
    return code, out, round(time.time() - t0, 1), timed_out


# --------------------------------------------------------------------------
# manifest + tests
# --------------------------------------------------------------------------

def manifest_tests():
    return {t["name"]: t for t in MANIFEST_DATA["tests"]}


def inline_test_entry(lib):
    g = GRAPH.get()
    rec = g["libraries"].get(lib)
    if not rec or not rec["has_inline_tests"]:
        return None
    tpl = MANIFEST_DATA["inline_tests"]
    return {"name": tpl["name_prefix"] + lib,
            "command": [a.replace("{dir}", rec["dir"]) for a in tpl["command_template"]],
            "cost": tpl["cost"], "modes": tpl["modes"], "libraries": [lib]}


def resolve_test(name):
    t = manifest_tests().get(name)
    if t:
        return t
    prefix = MANIFEST_DATA["inline_tests"]["name_prefix"]
    if name.startswith(prefix):
        t = inline_test_entry(name[len(prefix):])
        if t:
            return t
        raise ValueError(f"{name[len(prefix):]} is not a library with (inline_tests)")
    raise ValueError(f"unknown test {name!r}; see manifest.toml [[tests]] or use inline:<library>")


# --------------------------------------------------------------------------



def env_status() -> dict:
    """Detected toolchain mode (opam/nix/none), dune and OCaml versions, warnings."""
    d = ENV.to_dict()
    d["derived_graph"] = {"ok": GRAPH.error is None, "error": GRAPH.error}
    return d


def build(target: str, timeout_s: int = 600) -> dict:
    """Run `dune build <target>`. target is a repo-relative path or alias like
    src/lib/hex or @src/lib/hex/check or src/app/cli/src/mina.exe.
    Returns structured OCaml errors parsed from dune output."""
    t = target if target.startswith("@") else rel(target)
    code, out, elapsed, timed_out = run_dune(["dune", "build", t], timeout_s)
    errs, warns = split_diags(out)
    return {"ok": code == 0, "target": t, "elapsed_s": elapsed, "timed_out": timed_out,
            "errors": errs, "warnings": warns, "raw_tail": tail(out)}


def check(path: str, timeout_s: int = 600) -> dict:
    """Cheapest type-check of the library containing path: `dune build @<dir>/check`
    (compiles interfaces/objects without linking). Returns structured errors."""
    p = rel(path)
    d = enclosing_dune_dir(p)
    if d is None:
        raise ValueError(f"{p} is not inside any dune directory")
    alias = f"@{d}/check"
    code, out, elapsed, timed_out = run_dune(["dune", "build", alias], timeout_s)
    errs, warns = split_diags(out)
    u = unit_of(p)
    return {"ok": code == 0, "path": p, "alias": alias,
            "library": u[1] if u and u[0] == "lib" else None,
            "elapsed_s": elapsed, "timed_out": timed_out,
            "errors": errs, "warnings": warns, "raw_tail": tail(out)}


def test(name: str, timeout_s: int = 900) -> dict:
    """Run a named test from manifest.toml, or inline:<library> for a library's
    ppx_inline_test blocks. Refuses tests whose modes exclude the current mode."""
    t = resolve_test(name)
    if ENV.mode not in t["modes"]:
        return {"ok": False, "name": name, "refused": True,
                "reason": f"test {name} runs in modes {t['modes']}, current mode is {ENV.mode}",
                "command": t["command"]}
    argv = list(t["command"])
    # dune caches passing runtest aliases; --force makes the test actually run
    # (and print its summary) even when nothing changed.
    if argv[:2] == ["dune", "build"] and "--force" not in argv:
        argv.insert(2, "--force")
    code, out, elapsed, timed_out = run_dune(argv, timeout_s)
    errs, warns = split_diags(out)
    failures, summary = parse_test_output(out)
    return {"ok": code == 0, "name": name, "command": argv, "cost": t["cost"],
            "elapsed_s": elapsed, "timed_out": timed_out, "summary_line": summary,
            "failures": failures, "build_errors": errs, "build_warnings": warns, "raw_tail": tail(out)}


def test_one(file: str, test_name: str = "", timeout_s: int = 900) -> dict:
    """Run one ppx_inline_test block via scripts/testone.sh <file> [test_name].
    test_name matches the string after `let%test "..."`; empty runs all blocks in the file."""
    f = rel(file)
    argv = ["bash", "scripts/testone.sh", f] + ([test_name] if test_name else [])
    code, out, elapsed, timed_out = run_dune(argv, timeout_s)
    errs, warns = split_diags(out)
    failures, summary = parse_test_output(out)
    return {"ok": code == 0, "file": f, "test_name": test_name or None, "command": argv,
            "elapsed_s": elapsed, "timed_out": timed_out, "summary_line": summary,
            "failures": failures, "build_errors": errs, "build_warnings": warns, "raw_tail": tail(out)}


def tests_for(path: str) -> dict:
    """Candidate tests for a source path, cheapest and most relevant first:
    the file's own library (inline tests, manifest tests), then tests of
    libraries that directly depend on it. Each has name/command/cost/reason."""
    g = GRAPH.get()
    p = rel(path)
    u = unit_of(p)
    if u is None:
        raise ValueError(f"{p} is not inside any described dune unit")
    kind, key, rec = u
    out, seen = [], set()

    def add(entry, reason):
        if entry and entry["name"] not in seen:
            seen.add(entry["name"])
            out.append({**entry, "reason": reason,
                        "runnable_in_current_mode": ENV.mode in entry["modes"]})

    if kind != "lib":
        add({"name": f"{kind}:{key}", "command": ["dune", "build", f"@{rec['dir']}/runtest"],
             "cost": "unmeasured", "modes": ["opam", "nix"], "libraries": []},
            f"path is inside {kind} unit {key}")
        return {"path": p, "unit": {"kind": kind, "key": key}, "candidates": out}

    lib = key
    mt = manifest_tests()
    core = MANIFEST_DATA["core"].get(lib)
    if core:
        add(mt.get(core["cheap_test"]), f"core library cheap_test for {lib}")
    add(inline_test_entry(lib), "inline tests of the same library")
    for t in sorted((t for t in mt.values() if lib in t["libraries"]),
                    key=lambda t: {"fast": 0, "slow": 1}.get(t["cost"], 2)):
        add(t, f"manifest test covering {lib}")
    for dep in g["dependents"].get(lib, []):
        if dep.startswith("test:"):
            tkey = dep[5:]
            trec = g["tests"][tkey]
            add({"name": dep, "command": ["dune", "build", f"@{trec['dir']}/runtest"],
                 "cost": "unmeasured", "modes": ["opam", "nix"], "libraries": [lib]},
                f"test unit depending directly on {lib}")
        elif not dep.startswith("exe:"):
            add(inline_test_entry(dep), f"inline tests of dependent library {dep}")
            for t in mt.values():
                if dep in t["libraries"]:
                    add(t, f"manifest test covering dependent {dep}")
        if len(out) >= 20:
            break
    return {"path": p, "unit": {"kind": "lib", "key": lib, "dir": rec["dir"]},
            "candidates": out[:20]}


def deps_of(library: str) -> dict:
    """Direct dependencies of a library (private dune name or public name)."""
    g = GRAPH.get()
    name = g["public_names"].get(library, library)
    rec = g["libraries"].get(name)
    if not rec:
        raise ValueError(f"unknown library {library!r}")
    return {"library": name, "public_name": rec["public_name"], "dir": rec["dir"],
            "deps": rec["deps"], "external_deps": rec["external_deps"], "ppx": rec["ppx"],
            "has_inline_tests": rec["has_inline_tests"]}


def dependents_of(library: str) -> dict:
    """Libraries, executables, and test units that directly depend on a library."""
    g = GRAPH.get()
    name = g["public_names"].get(library, library)
    if name not in g["libraries"]:
        raise ValueError(f"unknown library {library!r}")
    deps = g["dependents"].get(name, [])
    return {"library": name,
            "libraries": [d for d in deps if ":" not in d],
            "executables": [d[4:] for d in deps if d.startswith("exe:")],
            "tests": [d[5:] for d in deps if d.startswith("test:")]}


def library_of(path: str) -> dict:
    """Which dune unit (library / executable / test) a source path belongs to."""
    p = rel(path)
    u = unit_of(p)
    if u is None:
        d = enclosing_dune_dir(p)
        raise ValueError(f"{p}: no described unit; nearest dune dir is {d}")
    kind, key, rec = u
    return {"path": p, "kind": kind, "key": key, "dir": rec["dir"],
            "name": rec.get("name", key), "public_name": rec.get("public_name")}




# --------------------------------------------------------------------------
# module index: module name -> file + owning library
# --------------------------------------------------------------------------

def _modname(stem):
    return stem[:1].upper() + stem[1:]


def _lib_files(rec, by_dir):
    """{Module: {ml, mli}} for one library. Dune names a module after its file
    stem; (modules ...) narrows the set and (include_subdirs ...) widens it to
    subdirectories that are not dune units of their own."""
    d = rec["dir"]
    full = os.path.join(ENV.repo, d)
    try:
        with open(os.path.join(full, "dune")) as fh:
            recurse = "include_subdirs" in fh.read()
    except OSError:
        recurse = False
    found = {}
    if recurse:
        for root, dirs, files in os.walk(full):
            rel_root = os.path.relpath(root, ENV.repo)
            dirs[:] = [x for x in dirs if os.path.join(rel_root, x) not in by_dir
                       and x not in derivemod.SKIP_DIRS]
            for f in files:
                found.setdefault(f, os.path.join(rel_root, f))
    else:
        try:
            found = {f: os.path.join(d, f) for f in os.listdir(full)}
        except OSError:
            return {}
    mods = rec.get("modules")
    out = {}
    for f, p in found.items():
        stem, ext = os.path.splitext(f)
        if ext not in (".ml", ".mli") or not stem[:1].isalpha() or "." in stem:
            continue
        if mods and (stem in mods.get("exclude", [])
                     or not (mods.get("with_standard") or stem in mods.get("include", []))):
            continue
        out.setdefault(_modname(stem), {"ml": None, "mli": None})[ext[1:]] = p
    return out


def _module_index(g):
    return {key: {"top": _modname(key), "files": _lib_files(rec, g["by_dir"])}
            for key, rec in g["libraries"].items()}


def find_module(name: str) -> dict:
    """Where a module lives: its canonical .ml/.mli and the dune library that
    owns it, for a module name with no position attached (from conversation,
    a build error, or a dune file). Accepts a bare module (Zkapp_account), a
    dotted path (Mina_base.Zkapp_account.Stable), or a dune public name
    (archive.cli). Library hits come first — a library's top module is its
    wrapper, i.e. the file named after it when one exists — then same-named
    submodule files in other libraries, each with its library, so an ambiguous
    name (26 intf.ml) is resolvable. Components beyond the file come back as
    `remaining`; resolve those inside the file with the LSP (documentSymbol).
    The library returned is the key for deps_of / dependents_of / tests_for /
    check_dependents."""
    g = GRAPH.get()
    q = name.strip()
    if not q:
        raise ValueError("empty module name")
    if q in g["public_names"]:  # a dune public name names one library directly
        key = g["public_names"][q]
        idx = {key: {"top": _modname(key), "files": _lib_files(g["libraries"][key], g["by_dir"])}}
        comps = [idx[key]["top"]]
    else:
        idx = _module_index(g)
        comps = q.split(".")
    head, rest = comps[0], comps[1:]
    hits = []
    for key, ent in idx.items():
        rec = g["libraries"][key]
        base = {"library": key, "public_name": rec["public_name"], "dir": rec["dir"]}
        if ent["top"] == head:
            main = ent["files"].get(head)
            h = {"role": "library", "module": head, **base,
                 "ml": main["ml"] if main else None, "mli": main["mli"] if main else None}
            if not main:  # dune generates the wrapper; the files are its submodules
                subs = sorted(ent["files"])
                h.update(auto_wrapper=True, submodules=subs[:40], submodule_count=len(subs))
            hits.append(h)
        elif head in ent["files"]:
            f = ent["files"][head]
            hits.append({"role": "submodule", "module": f"{ent['top']}.{head}", **base,
                         "ml": f["ml"], "mli": f["mli"]})
    hits.sort(key=lambda h: (h["role"] != "library", h["dir"]))
    remaining = rest
    if rest and hits and hits[0]["role"] == "library":  # Top.Sub -> the file sub.ml
        lib, sub = hits[0], rest[0]
        f = idx[lib["library"]]["files"].get(sub)
        if f:
            hits = [{"role": "submodule", "module": f"{head}.{sub}", "library": lib["library"],
                     "public_name": lib["public_name"], "dir": lib["dir"], "ml": f["ml"], "mli": f["mli"]}]
            remaining = rest[1:]
        else:
            lib["note"] = (f"no file {sub.lower()}.ml in {lib['dir']}; {sub} is declared inside "
                           f"{lib['ml'] or 'the library'}: use documentSymbol or workspaceSymbol")
            hits = [lib]
    out = {"query": q, "hit_count": len(hits), "hits": hits[:30], "remaining": remaining}
    if not hits:
        out["note"] = ("no library or file is named after this module; a value, type, or module "
                       "declared inside a file is found by the LSP workspaceSymbol")
    elif remaining:
        out["note"] = f"{'.'.join(remaining)} is inside the returned file; use documentSymbol on it"
    return out


def check_dependents(library: str, timeout_s: int = 900) -> dict:
    """Type-check every library that directly depends on `library`, in one
    dune call (`dune build @a/check @b/check ...`). Use after changing an
    interface, to catch breakage in consumers without linking anything."""
    g = GRAPH.get()
    name = g["public_names"].get(library, library)
    if name not in g["libraries"]:
        raise ValueError(f"unknown library {library!r}")
    deps = [d for d in g["dependents"].get(name, []) if ":" not in d]
    aliases = [f"@{g['libraries'][d]['dir']}/check" for d in deps]
    if not aliases:
        return {"ok": True, "library": name, "checked": [], "elapsed_s": 0,
                "errors": [], "raw_tail": ""}
    code, out, elapsed, timed_out = run_dune(["dune", "build"] + aliases, timeout_s)
    errs, warns = split_diags(out)
    return {"ok": code == 0, "library": name, "checked": deps, "elapsed_s": elapsed,
            "timed_out": timed_out, "errors": errs, "warnings": warns, "raw_tail": tail(out)}


# --------------------------------------------------------------------------
# merlin (read-side queries against the last compiled state)
# --------------------------------------------------------------------------

def _merlin_run(extra, path, timeout_s=60):
    """Run `ocamlmerlin single` with extra args on the file's current contents,
    fed on stdin so unsaved edits are honoured. Returns (relpath, value, clock_ms)."""
    p = rel(path)
    full = os.path.join(ENV.repo, p)
    if not os.path.isfile(full):
        raise ValueError(f"{p} is not a file")
    with open(full, encoding="utf-8", errors="replace") as fh:
        src = fh.read()
    argv = ["ocamlmerlin", "single"] + extra + ["-filename", p]
    with DUNE_LOCK:  # merlin asks `dune ocaml-merlin` for config
        r = ENV.run(argv, capture=True, timeout=timeout_s, input=src)
    try:
        j = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError(f"merlin produced no JSON: {tail(r.stdout + r.stderr)}")
    if j.get("class") != "return":
        raise RuntimeError(f"merlin {j.get('class')}: {j.get('value')}")
    return p, j["value"], (j.get("timing") or {}).get("clock")


def _merlin(command, path, line, col, timeout_s=60):
    """A positioned merlin query (line/col 1-based; merlin wants 0-based columns)."""
    return _merlin_run([command, "-position", f"{line}:{col - 1}"], path, timeout_s)


_NOT_BUILT_HINT = ("no result; if the library has not been compiled since clone or "
                   "since its dependencies changed, run check on this path first")


def type_at(file: str, line: int, col: int) -> dict:
    """Type of the expression at file:line:col (1-based, as in build/check errors),
    plus the enclosing expressions' types. Reflects the last compiled state of
    other modules, so run check after edits to interfaces before trusting it."""
    p, val, ms = _merlin("type-enclosing", file, line, col)
    enclosing = [{"start": [v["start"]["line"], v["start"]["col"] + 1],
                  "end": [v["end"]["line"], v["end"]["col"] + 1],
                  "type": v["type"]} for v in val
                 if "repeat to confirm" not in v["type"]][:3]
    return {"file": p, "line": line, "col": col, "elapsed_ms": ms,
            "type": enclosing[0]["type"] if enclosing else None,
            "enclosing": enclosing,
            "note": None if enclosing else _NOT_BUILT_HINT}


def definition(file: str, line: int, col: int) -> dict:
    """Where the identifier at file:line:col is defined (1-based). Returns the
    defining file and position, or a note if merlin cannot resolve it."""
    p, val, ms = _merlin("locate", file, line, col)
    if isinstance(val, str):
        return {"file": p, "line": line, "col": col, "elapsed_ms": ms,
                "definition": None, "note": val + "; " + _NOT_BUILT_HINT}
    dfile = val.get("file")
    if dfile and os.path.isabs(dfile):
        try:
            dfile = rel(dfile)
        except ValueError:
            pass  # outside the repo (opam library source), keep absolute
    return {"file": p, "line": line, "col": col, "elapsed_ms": ms,
            "definition": {"file": dfile, "line": val["pos"]["line"],
                           "col": val["pos"]["col"] + 1}}


def errors(file: str, timeout_s: int = 60) -> dict:
    """All syntax/type errors and warnings in file, from merlin against the last
    compiled state (~sub-second, no rebuild, reads the file as currently edited).
    This is the fast inner-loop check: it catches local mistakes (typos, wrong
    arguments, non-exhaustive matches) without paying dune's per-edit recompile;
    run check / check_dependents afterwards to confirm cross-module effects, which
    merlin cannot see until a rebuild. If the file's library has not been compiled,
    merlin reports phantom Unbound errors; this detects that and returns stale=true
    with a hint to run check first, rather than a flood of false diagnostics."""
    p, val, ms = _merlin_run(["errors"], file, timeout_s)
    diags = []
    for e in val:
        st = e.get("start") or {"line": 0, "col": -1}
        en = e.get("end") or st
        msg = " ".join((e.get("message") or "").split())
        kind = e.get("type", "typer")
        diags.append({"line": st["line"], "col_start": st["col"] + 1, "col_end": en["col"] + 1,
                      "severity": "warning" if kind == "warning" or msg.startswith("Warning") else "error",
                      "message": msg})
    errs = [d for d in diags if d["severity"] == "error"]
    warns = [d for d in diags if d["severity"] == "warning"]
    # A single Unbound is a real mistake (a name that doesn't exist yet); a pile of
    # them means the library's .cmi files aren't built, so every reference dangles.
    unbound = [d for d in errs if d["message"].startswith("Unbound")]
    stale = len(unbound) >= 3 and len(unbound) >= 0.5 * len(errs)
    out = {"file": p, "elapsed_ms": ms, "ok": not errs, "stale": stale,
           "error_count": len(errs), "warning_count": len(warns),
           "errors": errs[:50], "warnings": warns[:50]}
    if stale:
        out["note"] = ("most errors are Unbound, which usually means " + _NOT_BUILT_HINT
                       + "; then errors reflects only real problems")
    return out


def usages(file: str, line: int, col: int, timeout_s: int = 120) -> dict:
    """Every use of the binding at file:line:col (1-based) across the repo:
    its library plus everything that depends on it, executables and tests
    included. Read from the typed trees the compiler wrote (.cmt in _build),
    so each result is the typechecker's own resolution: no text matching,
    ppx-generated references excluded. The position may be the definition or
    any use of it (merlin resolves it to the definition first). Values,
    constructors, record fields, and types; not modules (use dependents_of
    for who depends on a library). Sees only compiled units: `unbuilt` lists
    those in scope it could not read."""
    from . import usages as U
    g = GRAPH.get()

    def attempt(def_file, dl, dc):
        u = unit_of(def_file)
        if u is None:
            raise ValueError(f"{def_file} is not inside any described dune unit")
        kind, key, _ = u
        units = U.cone(g, kind, key)
        cmts, unbuilt = U.cmt_files(ENV.repo, g, units)
        return U.run(ENV, def_file, dl, dc, cmts, timeout_s), key, units, unbuilt

    t0 = time.time()
    # The position is taken as a declaration first (a miss costs one .cmt
    # read). Only when nothing is declared there is it a use of the binding,
    # which merlin resolves to the definition. Asking merlin first is wrong at
    # a declaration: for `type t = M.t = A | B` it jumps to the manifest M.t.
    def_file, dl, dc = rel(file), line, col
    res, key, units, unbuilt = attempt(def_file, dl, dc)
    if res.get("error", "").startswith("no declaration"):
        _, val, _ = _merlin("locate", def_file, dl, dc)
        if isinstance(val, dict):
            f = val.get("file") or def_file
            try:
                def_file = rel(f) if os.path.isabs(f) else f
            except ValueError:
                return {"ok": False, "file": rel(file), "line": line, "col": col,
                        "note": f"defined outside the repo ({f}); bindings of external libraries are not searched"}
            dl, dc = val["pos"]["line"], val["pos"]["col"] + 1
            res, key, units, unbuilt = attempt(def_file, dl, dc)
    elapsed = round(time.time() - t0, 1)
    if "error" in res:
        return {"ok": False, "file": def_file, "line": dl, "col": dc, "elapsed_s": elapsed,
                "unbuilt": unbuilt, "note": res["error"]}
    by_lib = {}
    for h in res["usages"]:
        hu = unit_of(h["file"])
        h["library"] = hu[1] if hu else None
        by_lib[h["library"]] = by_lib.get(h["library"], 0) + 1
    n = len(res["usages"])
    out = {"ok": True,
           "binding": {**res["target"], "file": def_file, "line": dl, "col": dc, "library": key},
           "usage_count": n, "usages": res["usages"][:500], "truncated": n > 500,
           "by_library": dict(sorted(by_lib.items(), key=lambda kv: (-kv[1], str(kv[0])))),
           "scope": {"units": len(units), "files_read": res["files_read"],
                     "unreadable": len(res["unreadable"])},
           "unbuilt": unbuilt, "unresolved_types": res["unresolved_types"],
           "unresolved_files": res.get("unresolved_files", []), "elapsed_s": elapsed}
    notes = []
    if unbuilt:
        notes.append(f"{len(unbuilt)} unit(s) in scope have never been compiled, so their "
                     "references are not visible; build them (check) to complete the answer")
    if res["unresolved_types"]:
        notes.append(f"{res['unresolved_types']} type reference(s) with a local module head could not "
                     "be resolved (functor parameters, signature-local types, or an environment that "
                     "failed to rebuild); a use through a local module alias could be among them, "
                     "see unresolved_files")
    if notes:
        out["note"] = "; ".join(notes)
    return out


TOOLS = ["env_status", "build", "check", "check_dependents", "test", "test_one",
         "tests_for", "deps_of", "dependents_of", "library_of", "find_module", "usages",
         "type_at", "definition", "errors"]



def facts() -> list:
    """Plain factual statements about the environment and manifest, for the
    SessionStart hook and run.py's --append-system-prompt. Statements, never
    instructions (hooks docs: prompt-injection note)."""
    env, m = ENV, MANIFEST_DATA
    out = [f"mina-harness environment: mode={env.mode} activated={env.activated} "
           f"dune={env.dune_version} ocaml={env.ocaml}."]
    out += [f"warning: {w}" for w in env.warnings]
    if GRAPH.error:
        out.append(f"library graph unavailable: {GRAPH.error}")
    else:
        g = GRAPH.data
        out.append(f"library graph derived from dune files: {len(g['libraries'])} libraries, "
                   f"{len(g['tests'])} test units, {len(g['executables'])} executables.")
    b = m["boundary"]
    out.append("OCaml/Rust boundary (read-only, mutable=false): libraries "
               + ", ".join(b["libraries"]) + f" in {b['stubs_dir']} wrap crates "
               + ", ".join(b["crates"]) + ". Protected paths: " + ", ".join(b["rust_paths"]) + ".")
    out.append("Core libraries: " + "; ".join(
        f"{k} ({v['dir']}, cheap test {v['cheap_test']})" for k, v in m["core"].items()) + ".")
    out.append("Manifest tests: " + "; ".join(
        f"{t['name']} [{t['cost']}, modes {','.join(t['modes']) or 'none'}]" for t in m["tests"])
        + f". Any library with (inline_tests) also has {m['inline_tests']['name_prefix']}<library>.")
    from . import lsp
    if lsp.plugin_dir(ENV.repo):
        out.append("ocamllsp is installed: Claude's built-in LSP tool (goToDefinition, findReferences, "
                   "hover, documentSymbol, workspaceSymbol) works on .ml/.mli files; type_at/definition "
                   "are the merlin fallback. goToDefinition resolves a reference at a position; "
                   "workspaceSymbol finds a declared value, type, or module by name (~2s, whole "
                   "workspace); find_module maps a module name with no position to its canonical "
                   "file and library. A library's top module is its file rather than a declaration, "
                   "so workspaceSymbol does not find it and returns same-named aliases or mocks "
                   "in other files instead.")
    out.append("MCP server mina-harness provides: " + ", ".join(TOOLS)
               + ". errors gives merlin's sub-second diagnostics for one file and is the fast "
               "inner-loop check after an edit; check decides whether an edit compiles and "
               "check_dependents whether its consumers still do (the slower, cross-module "
               "truth); find_module maps a module name to its file and owning library, the key "
               "for deps_of/tests_for; usages lists every use of a value, constructor, field, or "
               "type across the repo from the compiled typed trees (the LSP findReferences and "
               "merlin occurrences are file-local on this toolchain); type_at/definition describe "
               "code as last compiled; after every Edit of "
               "a .ml/.mli file a hook runs check automatically and returns its diagnostics. "
               "Raw dune/opam/nix/cargo/make "
               "commands are denied by permission rules; checked-in scripts run normally; "
               "build-config and Rust boundary files are deny-listed for edits.")
    return out


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

SAMPLE_ERR = '''File "harness/scratch/scratch.ml", line 1, characters 14-26:
1 | let x : int = "not an int"
                  ^^^^^^^^^^^^
Error: This expression has type string but an expression was expected of type
         int
File "src/lib/foo/bar.ml", line 7, characters 2-5:
7 |   baz
      ^^^
Error (warning 32 [unused-value-declaration]): unused value baz.

File "src/lib/foo/dune", line 3, characters 12-20:
Error: Library "nope" not found.
'''
SAMPLE_TEST = '''File "src/lib/currency/currency.ml", line 1235, characters 6-61: fee sub_flagged (0.008 sec)
File "src/lib/currency/currency.ml", line 1240, characters 6-40: broken threw (Failure "x").
14 tests ran, 3 test_modules ran
'''


def selftest():
    errs = parse_dune_errors(SAMPLE_ERR)
    assert len(errs) == 3, errs
    assert errs[0]["line"] == 1 and errs[0]["col_start"] == 14 and errs[0]["severity"] == "error"
    assert "expected of type int" in errs[0]["message"], errs[0]
    assert errs[1]["severity"] == "error" and "unused value baz" in errs[1]["message"]
    assert errs[2]["file"].endswith("dune") and errs[2]["col_start"] == 12
    f, s = parse_test_output(SAMPLE_TEST)
    assert f == [{"file": "src/lib/currency/currency.ml", "line": 1240, "name": "broken"}], f
    assert s == "14 tests ran, 3 test_modules ran"
    assert rel(os.path.join(ENV.repo, "src/lib/hex")) == "src/lib/hex"
    try:
        rel("/etc/passwd"); assert False
    except ValueError:
        pass
    g = GRAPH.get()
    assert "pickles" in g["libraries"]
    assert resolve_test("inline:currency")["command"] == ["dune", "build", "@src/lib/currency/runtest"]
    fm = find_module("Mina_base.Zkapp_account.Stable")
    assert fm["hits"][0]["ml"] == "src/lib/mina_base/zkapp_account.ml" and fm["remaining"] == ["Stable"], fm
    assert find_module("Staged_ledger")["hits"][0]["role"] == "library"
    print(f"selftest ok: mode={ENV.mode} libraries={len(g['libraries'])}")

