#!/usr/bin/env python3
"""Derive the library graph into harness/derived.json.

Does exactly what nix/ocaml.nix does to build its `dune-description`:
copy only dune / dune-project / *.inc / *.opam files into a temp tree, run
o1-labs' describe-dune there, and read its JSON. Then reshape that JSON into
lookup tables for the MCP tools (deps_of, dependents_of, tests_for).

Usage:
    python3 harness/derive.py            # write derived.json, print summary
    python3 harness/derive.py --check    # exit 1 if derived.json is stale
    python3 harness/derive.py --build    # (re)compile the tool only

The tool is compiled on first use from harness/vendor/describe-dune into
harness/bin/describe-dune via env.py, with the one-line build from its
upstream Makefile. Never hand-edit derived.json.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import env as envmod  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
VENDOR = os.path.join(HERE, "vendor", "describe-dune")
TOOL = os.path.join(HERE, "bin", "describe-dune")
OUT = os.path.join(HERE, "derived.json")

# Same file set as nix/ocaml.nix: sourceFilesBySuffices ../src [...]
KEEP_SUFFIXES = ("dune", "dune-project", ".inc", ".opam")
SKIP_DIRS = {"_build", "node_modules", "_opam", "opam_switches", ".git"}
# describe-dune's deps listing includes ppx drivers and instrumentation
# backends. They are real dune deps but noise for "what does this code use".
PPX_PREFIXES = ("ppx_", "bisect_ppx")


def build_tool(env):
    os.makedirs(os.path.dirname(TOOL), exist_ok=True)
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
            if dep.startswith(PPX_PREFIXES) or dep.endswith(".ppx"):
                ppx.append(dep)
            elif dep in public_to_name:
                n = public_to_name[dep]
                if n not in local:
                    local.append(n)
            else:
                external.append(dep)
        return local, external, ppx

    for table in (libs, exes, tests):
        for name, rec in table.items():
            local, external, ppx = resolve(rec.pop("raw_deps"))
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
        "generated_by": "harness/derive.py (do not hand-edit)",
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


def main(argv):
    e = envmod.detect()
    if e.mode == "none":
        sys.stderr.write("derive.py: " + "; ".join(e.reasons) + "\n")
        return 3
    if "--build" in argv:
        build_tool(e)
        print(TOOL)
        return 0
    fresh = derive(e)
    if "--check" in argv:
        if not os.path.exists(OUT):
            print("derived.json missing")
            return 1
        with open(OUT) as fh:
            old = json.load(fh)
        volatile = ("git_head",)
        a = {k: v for k, v in old.items() if k not in volatile}
        b = {k: v for k, v in fresh.items() if k not in volatile}
        if a != b:
            print("derived.json is stale; rerun harness/derive.py")
            return 1
        print("derived.json is current")
        return 0
    with open(OUT, "w") as fh:
        json.dump(fresh, fh, indent=1, sort_keys=False)
        fh.write("\n")
    print(f"wrote {os.path.relpath(OUT, e.repo)}: {summary(fresh)}")
    for n in fresh["notes"]:
        print("  note:", n)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
