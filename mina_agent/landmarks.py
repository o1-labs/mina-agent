"""landmarks (LexiFi's OCaml profiler), vendored into harness/state, and its
profile format.

Why vendored: landmarks 1.4 is x86-only (rdtsc); 1.5 supports arm64 but its
opam package requires dune >= 3.16 while the repo pins 3.3.1, so installing
it would upgrade dune. Its only real 3.16 dependency is the `(lang dune 3.16)`
line: lowered to 3.3 it builds with the pinned dune. setup fetches the
pinned sources with `opam source` (checksum-verified by opam, nothing
installed into the switch), patches that line, and places them under
harness/state/landmarks. harness/state/dune marks the directory vendored, so
no alias or default build ever touches it; dune builds it only when
`--instrument-with landmarks` makes a library require it.

A library is instrumented by an `(instrumentation (backend landmarks --auto))`
stanza (the shape of the repo's 341 bisect_ppx stanzas); inactive stanzas
are ignored even when the library is absent. --auto instruments every
top-level function, including those of nested modules; finer windows are
`expr [@landmark "name"]` and `let[@landmark] f = ...`.

Profiles: OCAML_LANDMARKS="format=json,output=<file>,allocation,time". Nodes
are call-tree instances (one per landmark per parent) with time in counter
ticks, calls, allocated_bytes (inclusive), sys_time, children. The threshold
option applies to the textual format only; JSON is complete. Ticks are
converted to seconds by calibrating against the root's sys_time.
"""
import json
import os
import shutil
import subprocess
import tempfile

from . import paths
from .model import CallerEdge, FunctionStats, Profile, to_json

VERSION = "1.5"
STATE_DUNE = "(dirs landmarks)\n(vendored_dirs landmarks)\n"
STANZA = " (instrumentation\n  (backend landmarks --auto))"


def vendor_dir(repo):
    return paths.state_dir(repo) / "landmarks"


def present(repo):
    d = vendor_dir(repo)
    return (d / "dune-project").exists() and (d / "src" / "dune").exists() and (d / "ppx" / "dune").exists()


def fetch(env):
    """Place patched landmarks sources under harness/state/landmarks."""
    dst = vendor_dir(env.repo)
    if present(env.repo):
        return dst, "present"
    opam = shutil.which("opam")
    if not opam:
        raise RuntimeError("opam not on PATH; cannot fetch landmarks sources")
    with tempfile.TemporaryDirectory(prefix="harness-landmarks-") as tmp:
        for pkg in ("landmarks", "landmarks-ppx"):
            r = subprocess.run([opam, "source", f"{pkg}.{VERSION}", f"--dir={tmp}/{pkg}"],
                               capture_output=True, text=True, env=env.activate())
            if r.returncode != 0:
                raise RuntimeError(f"opam source {pkg}.{VERSION} failed: {(r.stderr or r.stdout)[-800:]}")
        if dst.exists():
            shutil.rmtree(dst)
        dst.mkdir(parents=True)
        lm = f"{tmp}/landmarks"
        for item in ("landmarks.opam", "landmarks-ppx.opam", "src", "ppx"):
            src = os.path.join(lm, item)
            if not os.path.exists(src):
                src = os.path.join(tmp, "landmarks-ppx", item)
            (shutil.copytree if os.path.isdir(src) else shutil.copyfile)(src, dst / item)
        shutil.rmtree(dst / "src" / "threads", ignore_errors=True)   # needs threads.posix; unused here
        with open(os.path.join(lm, "dune-project")) as fh:
            proj = fh.read()
        lines = proj.splitlines()
        lines[0] = "(lang dune 3.3)" if lines[0].startswith("(lang dune") else lines[0]
        (dst / "dune-project").write_text("\n".join(lines) + "\n")
    (dst.parent / "dune").write_text(STATE_DUNE)
    return dst, f"fetched {VERSION} and patched (lang dune 3.3)"


def status(repo):
    if present(repo):
        return True, f"{vendor_dir(repo)} (landmarks {VERSION}, patched for dune 3.3; built only under --instrument-with)"
    return False, "not vendored; run mina-agent setup (fetches with opam source, installs nothing)"


# --------------------------------------------------------------------------
# profile parsing
# --------------------------------------------------------------------------

RANK_KEYS = ("self_ms", "total_ms", "calls", "self_alloc_mb", "alloc_mb")


def load(path) -> Profile:
    """Read a landmarks JSON profile into per-function aggregates, keyed
    "name @ file:line". Self time/allocation is the node's inclusive figure
    minus its children's, summed over all instances of the function; callers
    are the parent functions with the inclusive time spent under each."""
    with open(path) as fh:
        g = json.load(fh)
    nodes = g["nodes"]
    byid = {n["id"]: n for n in nodes}
    root = byid[g["root"]]
    hz = root["time"] / root["sys_time"] if root.get("sys_time") else None
    ms = (lambda t: t / hz * 1e3) if hz else (lambda t: t)
    parent = {c: n["id"] for n in nodes for c in n["children"]}
    key = lambda n: f"{n['name']} @ {n['location']}"

    acc: dict[str, dict] = {}       # per-function running sums
    for n in nodes:
        if n["kind"] == "root":
            continue
        kids = [byid[c] for c in n["children"]]
        a = acc.setdefault(key(n), {"name": n["name"], "location": n["location"], "kind": n["kind"],
                                    "self_ms": 0.0, "total_ms": 0.0, "calls": 0,
                                    "self_alloc_mb": 0.0, "alloc_mb": 0.0, "callers": {}})
        a["self_ms"] += ms(n["time"] - sum(k["time"] for k in kids))
        a["total_ms"] += ms(n["time"])
        a["calls"] += n["calls"]
        a["self_alloc_mb"] += (n["allocated_bytes"] - sum(k["allocated_bytes"] for k in kids)) / 1e6
        a["alloc_mb"] += n["allocated_bytes"] / 1e6
        p = parent.get(n["id"])
        if p is not None and byid[p]["kind"] != "root":
            c = a["callers"].setdefault(key(byid[p]), [0, 0.0])
            c[0] += n["calls"]
            c[1] += ms(n["time"])
    total = ms(root["time"])
    fns = {
        k: FunctionStats(
            name=a["name"], location=a["location"], kind=a["kind"],
            self_ms=round(a["self_ms"], 2), total_ms=round(a["total_ms"], 2), calls=a["calls"],
            self_alloc_mb=round(a["self_alloc_mb"], 2), alloc_mb=round(a["alloc_mb"], 2),
            self_pct=round(100 * a["self_ms"] / total, 1) if total else 0.0,
            callers=tuple(sorted((CallerEdge(c, n, round(t, 2)) for c, (n, t) in a["callers"].items()),
                                 key=lambda e: -e.total_ms)))
        for k, a in acc.items()}
    return Profile(label=g.get("label"), hz=hz, units="ms" if hz else "ticks",
                   total_ms=round(total, 2), nodes=len(nodes), functions=fns)


def top(prof: Profile, by="self_ms", k=15, library_dirs=None) -> list[dict]:
    """Ranked functions as JSON rows; library_dirs restricts to locations under those dirs."""
    fns = [f for f in prof.functions.values() if not library_dirs or f.under(library_dirs)]
    rows = sorted(fns, key=lambda f: -getattr(f, by))[:k]
    return [{**{kk: v for kk, v in to_json(f).items() if kk != "callers"}, "top_callers": to_json(f.callers[:3])}
            for f in rows]


def diff(a: Profile, b: Profile, k=20) -> dict:
    """Per-function change from profile a to b (same workload), largest first."""
    def row(key):
        fa, fb = a.functions.get(key), b.functions.get(key)
        sa, sb = (fa.self_ms if fa else 0.0), (fb.self_ms if fb else 0.0)
        aa, ab = (fa.self_alloc_mb if fa else 0.0), (fb.self_alloc_mb if fb else 0.0)
        return {"function": key, "self_ms_before": sa, "self_ms_after": sb, "self_ms_delta": round(sb - sa, 2),
                "self_alloc_mb_before": aa, "self_alloc_mb_after": ab, "self_alloc_mb_delta": round(ab - aa, 2),
                "status": "added" if fa is None else "removed" if fb is None else "changed"}
    rows = sorted((row(key) for key in set(a.functions) | set(b.functions)), key=lambda r: -abs(r["self_ms_delta"]))
    return {"total_ms_before": a.total_ms, "total_ms_after": b.total_ms,
            "total_ms_delta": round(b.total_ms - a.total_ms, 2), "functions": rows[:k]}
