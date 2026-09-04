#!/usr/bin/env python3
"""Derive the library graph into harness/state/derived.json.

Does exactly what nix/ocaml.nix does to build its `dune-description`:
copy only dune / dune-project / *.inc / *.opam files into a temp tree, run
o1-labs' describe-dune there, and read its JSON. Then reshape that JSON into
lookup tables for the MCP tools (deps_of, dependents_of, tests_for).

CLI: mina-agent derive [--check|--build] (hidden); setup and init call it.

The tool is compiled on first use from mina_agent/data/vendor/describe-dune
into harness/state/bin/describe-dune with the one-line build from its
upstream Makefile. Never hand-edit derived.json.
"""
import hashlib
import json
import os
import shutil
import contextlib
import subprocess
import sys
import tempfile

from . import paths

VENDOR = str(paths.VENDOR_DESCRIBE_DUNE)

# Same file set as nix/ocaml.nix: sourceFilesBySuffices ../src [...]
KEEP_SUFFIXES = ("dune", "dune-project", ".inc", ".opam")
SKIP_DIRS = {"_build", "node_modules", "_opam", "opam_switches", ".git"}
# describe-dune flattens libraries, ppx_runtime_libraries and pps into one
# deps list. External ppx drivers and instrumentation backends are noise for
# "what does this code use" and are set aside; anything local (including the
# repo's own ppx rewriters and ppx_version.runtime) is a real edge.
PPX_PREFIXES = ("ppx_", "bisect_ppx")


def tool_path(env):
    return str(paths.describe_dune_bin())


def build_tool(env):
    TOOL = tool_path(env)
    src = os.path.join(VENDOR, "describe_dune.ml")
    argv = ["ocamlfind", "ocamlopt"]
    for p in ("cmdliner", "parsexp", "yojson", "stdio", "base"):
        argv += ["-package", p]
    argv += ["-linkpkg", src, "-o", TOOL]
    r = env.run(argv, capture=True, cwd=os.path.dirname(TOOL))
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        raise SystemExit("derive.py: failed to build describe-dune")
    # ocamlfind leaves .cm*/.o beside the source; keep vendor/ clean
    for ext in (".cmi", ".cmx", ".cmo", ".o"):
        p = os.path.join(VENDOR, "describe_dune" + ext)
        if os.path.exists(p):
            os.remove(p)
    return TOOL


def filtered_tree(repo, dst):
    """Copy the dune metadata files of src/ plus the two root files."""
    n = 0
    for root, dirs, files in os.walk(os.path.join(repo, "src")):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS
                   and not os.path.islink(os.path.join(root, d))]
        for f in files:
            if f == "dune" or f == "dune-project" or f.endswith((".inc", ".opam")):
                rel = os.path.relpath(os.path.join(root, f), repo)
                os.makedirs(os.path.dirname(os.path.join(dst, rel)), exist_ok=True)
                shutil.copyfile(os.path.join(root, f), os.path.join(dst, rel))
                n += 1
    for f in ("dune", "dune-project"):
        shutil.copyfile(os.path.join(repo, f), os.path.join(dst, f))
        n += 1
    return n


def run_describe(env):
    TOOL = tool_path(env)
    if not os.path.exists(TOOL):
        build_tool(env)
    with tempfile.TemporaryDirectory(prefix="harness-describe-") as tmp:
        nfiles = filtered_tree(env.repo, tmp)
        r = subprocess.run([TOOL], cwd=tmp, capture_output=True, text=True)
        # describe-dune exits 10 when any single dune file failed to parse but
        # still prints everything else. Report, don't fail.
        if r.returncode not in (0, 10):
            sys.stderr.write(r.stderr)
            raise SystemExit(f"derive.py: describe-dune exit {r.returncode}")
        notes = [l for l in r.stderr.splitlines() if l.strip()]
        return json.loads(r.stdout), nfiles, notes


def stamp(repo):
    """Cache key for derived.json: a digest over (path, mtime_ns) of every dune
    metadata file under src/ plus the vendored describe-dune commit. The graph
    is a pure function of exactly those inputs."""
    h = hashlib.sha1()
    with open(os.path.join(VENDOR, "COMMIT")) as fh:
        h.update(fh.read().strip().encode())
    src = os.path.join(repo, "src")
    entries = []
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f == "dune" or f == "dune-project" or f.endswith((".inc", ".opam")):
                p = os.path.join(root, f)
                try:
                    entries.append((os.path.relpath(p, repo), os.stat(p).st_mtime_ns))
                except OSError:
                    pass
    for rel, mt in sorted(entries):
        h.update(f"{rel}\0{mt}\n".encode())
    return h.hexdigest()


def reshape(desc):
    """describe-dune JSON -> lookup tables keyed by private library name."""
    libs, exes, tests = {}, {}, {}
    public_to_name = {}
    for entry in desc:
        d = entry["src"]
        for u in entry["units"]:
            name = u.get("name")
            if not name:
                # dune: name defaults to public_name when the latter has no dots
                pn = u.get("public_name")
                if not pn or "." in pn:
                    continue
                name = pn
            rec = {
                "public_name": u.get("public_name"),
                "dir": d,
                "package": u.get("package"),
                "has_inline_tests": bool(u.get("has_inline_tests")),
                "modules": u.get("modules"),
                "implements": u.get("implements"),
                "raw_deps": u.get("deps", []),
            }
            if u["type"] == "lib":
                # dune requires private library names to be workspace-unique
                libs[name] = rec
                public_to_name[name] = name
                if u.get("public_name"):
                    public_to_name[u["public_name"]] = name
            else:
                # executable/test names repeat across dirs (main, gen,
                # test_common ...), so key them by dir/name
                rec["name"] = name
                (exes if u["type"] == "exe" else tests)[f"{d}/{name}"] = rec

    def resolve(raw):
        local, external, ppx = [], [], []
        for dep in raw:
            if dep in public_to_name:
                n = public_to_name[dep]
                if n not in local:
                    local.append(n)
            elif dep.startswith(PPX_PREFIXES) or dep.endswith(".ppx"):
                ppx.append(dep)
            else:
                external.append(dep)
        return local, external, ppx

    for table in (libs, exes, tests):
        for name, rec in table.items():
            local, external, ppx = resolve(rec.pop("raw_deps"))
            # an implementation depends on its virtual library
            virt = public_to_name.get(rec["implements"])
            if virt and virt not in local:
                local.append(virt)
            rec["implements"] = virt
            rec["deps"] = local
            rec["external_deps"] = external
            rec["ppx"] = ppx

    dependents = {name: [] for name in libs}
    for name, rec in libs.items():
        for dep in rec["deps"]:
            dependents.setdefault(dep, []).append(name)
    # tests and exes count as dependents too; tests_for wants them
    for kind, table in (("test", tests), ("exe", exes)):
        for name, rec in table.items():
            for dep in rec["deps"]:
                dependents.setdefault(dep, []).append(f"{kind}:{name}")
    for v in dependents.values():
        v.sort()

    # dir -> unit names, so a source path can be mapped to its library
    by_dir = {}
    for kind, table in (("lib", libs), ("exe", exes), ("test", tests)):
        for key, rec in table.items():
            by_dir.setdefault(rec["dir"], []).append(
                {"kind": kind, "name": rec.get("name", key), "key": key})

    return {
        "libraries": dict(sorted(libs.items())),
        "executables": dict(sorted(exes.items())),
        "tests": dict(sorted(tests.items())),
        "dependents": dict(sorted(dependents.items())),
        "by_dir": dict(sorted(by_dir.items())),
        "public_names": dict(sorted(public_to_name.items())),
    }


def derive(env):
    desc, nfiles, notes = run_describe(env)
    data = reshape(desc)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=env.repo,
                          capture_output=True, text=True).stdout.strip()
    with open(os.path.join(VENDOR, "COMMIT")) as fh:
        tool_commit = fh.read().strip()
    data_out = {
        "generated_by": "mina_agent.graph (do not hand-edit)",
        "stamp": stamp(env.repo),
        "describe_dune_commit": tool_commit,
        "dune_version": env.dune_version,
        "git_head": head,
        "input_files": nfiles,
        "notes": notes,
    }
    data_out.update(data)
    return data_out


def summary(d):
    return (f"libraries={len(d['libraries'])} executables={len(d['executables'])} "
            f"tests={len(d['tests'])} dune_dirs={len(d['by_dir'])} "
            f"input_files={d['input_files']} notes={len(d['notes'])}")


def write_json_atomic(path, data):
    """Write via a sibling temp file + rename, so a concurrent reader sees the
    old file or the new one, never a partial write."""
    path = str(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".derived-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(data, fh, indent=1)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def derive_and_write(env):
    """Derive and write harness/state/derived.json; returns the data."""
    d = derive(env)
    write_json_atomic(paths.derived_json(), d)
    return d


def load(repo):
    """derived.json as written, or None when absent or unreadable."""
    try:
        with open(paths.derived_json()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_or_derive(env):
    """The cached graph when its stamp matches the dune files, else a fresh one."""
    d = load(env.repo)
    if d is not None and d.get("stamp") == stamp(env.repo):
        return d
    return derive_and_write(env)


def check(env):
    """True when the on-disk derived.json matches a fresh derivation."""
    out = paths.derived_json()
    if not out.exists():
        return False
    with open(out) as fh:
        old = json.load(fh)
    fresh = derive(env)
    volatile = ("git_head",)
    return ({k: v for k, v in old.items() if k not in volatile}
            == {k: v for k, v in fresh.items() if k not in volatile})
