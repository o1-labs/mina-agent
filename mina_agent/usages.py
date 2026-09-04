"""Project-wide usages of one binding, read from the typed trees in _build.

data/tools/usages.ml is our own compiler-libs program (not vendored: the
library it needs ships with the compiler in the switch). setup compiles it
into harness/state/bin/usages the way graph.py compiles describe-dune;
tools.usages computes the search scope from the derived graph, runs it, and
annotates the result with libraries.

Why the typed trees: merlin's project-wide occurrences need occurrence data
that only OCaml >= 5.2 writes, so on 4.14 `occurrences -scope project` is
silently buffer-local. The .cmt files still hold every resolved reference.
"""
import json
import os
import subprocess
import sys

from . import paths

SRC = paths.TOOLS / "usages.ml"
PACKAGES = "compiler-libs.common,unix,yojson"


def build_tool(env):
    out = str(paths.usages_bin(env.repo))
    argv = ["ocamlfind", "ocamlopt", "-package", PACKAGES, "-linkpkg", str(SRC), "-o", out]
    r = env.run(argv, capture=True, cwd=os.path.dirname(out))
    # ocamlopt leaves .cm*/.o beside the source; keep data/tools clean
    for ext in (".cmi", ".cmx", ".cmo", ".o"):
        p = SRC.with_suffix(ext)
        if p.exists():
            p.unlink()
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit("usages.py: failed to build usages")
    return out


def cone(g, kind, key):
    """Units whose code can reference a binding of (kind, key): the unit
    itself and, for a library, its transitive dependents, executables and
    tests included. Returns [(kind, key)] with the owner first."""
    units = [(kind, key)]
    if kind != "lib":
        return units
    seen, stack = {key}, [key]
    while stack:
        for d in g["dependents"].get(stack.pop(), []):
            if ":" in d:
                u = tuple(d.split(":", 1))
                if u not in units:
                    units.append(u)
            elif d not in seen:
                seen.add(d)
                stack.append(d)
                units.append(("lib", d))
    return units


def unit_record(g, kind, key):
    return g[{"lib": "libraries", "exe": "executables", "test": "tests"}[kind]][key]


def check_aliases(g, units):
    """One @<dir>/check per directory of the units: building them (re)writes
    every .cmt the units own. dune is incremental, so a fresh cone costs one
    dune invocation."""
    return [f"@{d}/check" for d in sorted({unit_record(g, k, key)["dir"] for k, key in units})]


def objs_dir(repo, g, kind, key):
    """Where dune puts a unit's .cmt files: .<lib>.objs for libraries,
    .<name>.eobjs for executables and tests."""
    rec = unit_record(g, kind, key)
    name = key if kind == "lib" else rec["name"]
    sub = f".{name}.objs" if kind == "lib" else f".{name}.eobjs"
    return os.path.join(repo, "_build", "default", rec["dir"], sub, "byte")


def cmt_files(repo, g, units):
    """(paths, unbuilt): every .cmt/.cmti of the units, and the units that
    have none (never compiled, so their references cannot be seen)."""
    files, unbuilt = [], []
    for kind, key in units:
        d = objs_dir(repo, g, kind, key)
        try:
            names = os.listdir(d)
        except OSError:
            names = []
        fs = [os.path.join(d, n) for n in names if n.endswith((".cmt", ".cmti"))]
        if fs:
            files += fs
        else:
            unbuilt.append(key if kind == "lib" else f"{kind}:{key}")
    return files, unbuilt


def run(env, def_file, line, col, cmts, timeout_s):
    tool = paths.usages_bin(env.repo)
    if not tool.exists():
        build_tool(env)
    r = subprocess.run([str(tool), def_file, str(line), str(col)], input="\n".join(cmts) + "\n",
                       capture_output=True, text=True, timeout=timeout_s, cwd=env.repo)
    if r.returncode not in (0, 2):
        raise RuntimeError(f"usages exited {r.returncode}: {r.stderr[-2000:]}")
    return json.loads(r.stdout)
