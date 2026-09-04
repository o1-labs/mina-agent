"""Uninstrumented before/after measurements of one workload across two
commits: wall clock and peak RSS from /usr/bin/time, allocation from the
runtime's own GC counters (OCAMLRUNPARAM=v=0x400), and the share of CPU
samples under one symbol from samply. Nothing is added to the source or the
dune files; the binaries are what the commits build.

compare() checks the base commit out in place, builds and measures, then
the head, and restores the original branch in a finally. It refuses on a
dirty tree or an active profiling session (which would instrument builds).
"""
import gzip
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path

from . import paths, profile as P
from .model import STACKS_COMPLETE_PCT, GcStats, PerfCompare, PerfRun, SampleShares, Workload

GC_KEYS = ("allocated_words", "minor_words", "promoted_words", "major_words", "top_heap_words")


# ---- parsers (pure) --------------------------------------------------------

def parse_gc_stats(text: str) -> GcStats | None:
    found = {k: int(m.group(1)) for k in GC_KEYS if (m := re.search(rf"^{k}:\s+(\d+)", text, re.M))}
    return GcStats(**found) if len(found) == len(GC_KEYS) else None


def parse_time(text: str) -> tuple[float | None, int | None]:
    """(wall seconds, max RSS bytes) from /usr/bin/time -l (macOS) or -v (Linux)."""
    wall = rss = None
    if m := re.search(r"^\s*([\d.]+) real", text, re.M):                       # macOS
        wall = float(m.group(1))
    if m := re.search(r"^\s*(\d+)\s+maximum resident set size", text, re.M):    # macOS, bytes
        rss = int(m.group(1))
    if m := re.search(r"Elapsed \(wall clock\) time.*?:\s*([\d:.]+)", text):    # Linux
        parts = [float(x) for x in m.group(1).split(":")]
        wall = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
    if m := re.search(r"Maximum resident set size \(kbytes\):\s*(\d+)", text):  # Linux, kB
        rss = int(m.group(1)) * 1024
    return wall, rss


ROOT_SYMBOL = "caml_start_program"
OCAML_THREAD_MIN_PCT = 20.0     # a thread is an OCaml thread when this much of its CPU has a caml* frame on the stack


def sample_shares(profile_gz: str, syms_json: str, symbol: str, *, scope: str = "ocaml") -> SampleShares:
    """Score `symbol` in a samply profile, naming frame addresses from the
    presymbolicated sidecar. Weights are CPU time per sample
    (threadCPUDelta; a count when absent), so blocked threads contribute
    nothing. scope="ocaml" restricts to threads on which OCaml code runs
    (the inline-test runner's worker thread, not the main thread, and not
    the Rust rayon pool), which is the denominator a landmarks figure has;
    scope="all" is the whole process. Reports inclusive (anywhere on the
    stack), leaf (innermost frame) and how much CPU reaches the OCaml root."""
    prof = json.load(gzip.open(profile_gz))
    syms = json.load(open(syms_json))
    strings = syms["string_table"]
    tables = {lib["debug_name"]: sorted(((e["rva"], e["size"], strings[e["symbol"]]) for e in lib["symbol_table"]))
              for lib in syms["data"]}
    import bisect
    starts = {name: [e[0] for e in t] for name, t in tables.items()}

    def name_of(lib_name, address):
        t = tables.get(lib_name)
        if not t:
            return None
        i = bisect.bisect_right(starts[lib_name], address) - 1
        if i >= 0 and t[i][0] <= address < t[i][0] + max(t[i][1], 1):
            return t[i][2]
        return None

    per_thread = []     # (cpu, inclusive, leaf, root, caml, samples)
    for th in prof["threads"]:
        libs = [prof["libs"][i]["name"] if i is not None else None for i in th["resourceTable"]["lib"]]
        ft, fu, st = th["frameTable"], th["funcTable"], th["stackTable"]
        names = []
        for f in range(ft["length"]):
            res = fu["resource"][ft["func"][f]]
            names.append(name_of(libs[res], ft["address"][f]) if res is not None and res >= 0 else None)
        frame_hit = [bool(n and symbol in n) for n in names]
        frame_root = [n == ROOT_SYMBOL for n in names]
        frame_caml = [bool(n and n.startswith("caml")) for n in names]
        stack_hit: dict[int, bool] = {}
        stack_root: dict[int, bool] = {}
        stack_caml: dict[int, bool] = {}
        for s in range(st["length"]):
            pre, f = st["prefix"][s], st["frame"][s]
            stack_hit[s] = frame_hit[f] or (pre is not None and stack_hit[pre])
            stack_root[s] = frame_root[f] or (pre is not None and stack_root[pre])
            stack_caml[s] = frame_caml[f] or (pre is not None and stack_caml[pre])
        stacks = th["samples"]["stack"]
        weights = th["samples"].get("threadCPUDelta") or [1] * len(stacks)
        cpu = inc = leaf = root = caml = 0.0
        n = 0
        for s, w in zip(stacks, weights):
            if s is None:
                continue
            w = w or 0
            n += 1
            cpu += w
            inc += w * stack_hit[s]
            leaf += w * frame_hit[st["frame"][s]]
            root += w * stack_root[s]
            caml += w * stack_caml[s]
        per_thread.append((cpu, inc, leaf, root, caml, n))
    all_cpu = sum(t[0] for t in per_thread)
    chosen = [t for t in per_thread if t[0] and 100 * t[4] / t[0] >= OCAML_THREAD_MIN_PCT] if scope == "ocaml" else per_thread
    if not chosen:      # no OCaml-bearing thread (or a non-OCaml binary): score everything
        chosen = per_thread
    total = sum(t[0] for t in chosen)
    return SampleShares(total=total, inclusive=sum(t[1] for t in chosen), leaf=sum(t[2] for t in chosen),
                        root=sum(t[3] for t in chosen), samples=sum(t[5] for t in chosen), ocaml_threads=len(chosen),
                        ocaml_cpu_share_pct=round(100 * total / all_cpu, 1) if all_cpu else 100.0)


def symbol_share(profile_gz: str, syms_json: str, symbol: str) -> tuple[float, float]:
    """(inclusive, total) CPU over the OCaml threads; sample_shares has the rest."""
    s = sample_shares(profile_gz, syms_json, symbol)
    return s.inclusive, s.total


def assess(s: SampleShares) -> tuple[float | None, tuple[str, ...]]:
    """(inclusive share to report, warnings): the inclusive share is withheld
    when too few stacks reach the OCaml root to trust it."""
    if s.completeness_pct < STACKS_COMPLETE_PCT:
        return None, (f"stacks incomplete: {s.completeness_pct}% of OCaml-thread CPU reaches {ROOT_SYMBOL} "
                      f"(need {STACKS_COMPLETE_PCT:.0f}%); the inclusive symbol share is withheld, use the leaf "
                      "(self time) share of the functions the change touches. On macOS arm64 the OCaml 4.14 "
                      "switch has no frame pointers and compact unwind does not describe OCaml frames, so "
                      "samply cannot walk past the first OCaml frame; Linux perf with DWARF unwinding gives "
                      "complete stacks.",)
    return s.inclusive_pct, ()


# ---- measurement ----------------------------------------------------------

def _time_cmd() -> list[str]:
    return ["/usr/bin/time", "-l"] if platform.system() == "Darwin" else ["/usr/bin/time", "-v"]


def tools_available() -> dict[str, str | None]:
    import shutil
    return {"time": "/usr/bin/time" if os.path.exists("/usr/bin/time") else None,
            "samply": shutil.which("samply")}


def _git(repo, *a) -> str:
    return subprocess.run(["git", *a], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def parse_env(spec: str) -> dict[str, str]:
    """'A=1 B=two' -> {"A": "1", "B": "two"}; shell-style quoting allowed."""
    import shlex
    out = {}
    for tok in shlex.split(spec or ""):
        k, eq, v = tok.partition("=")
        if not eq or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            raise ValueError(f"env must be NAME=value pairs, got {tok!r}")
        out[k] = v
    return out


def measure(env, w: Workload, ref: str, *, symbol: str | None, repeats: int, out_dir: Path,
            run_dune, timeout_s: int, extra_args=(), extra_env=None) -> PerfRun:
    """Build w.target at the current checkout and measure it `repeats` times.
    extra_args go after the workload's own argv (a test's size flags, say);
    extra_env is set for every run (a test's size variable)."""
    built = run_dune(["dune", "build", w.target], timeout_s)
    if not built.ok:
        raise RuntimeError(f"build of {w.target} at {ref} failed:\n{built.out[-2000:]}")
    exe = os.path.join(env.repo, "_build", "default", w.exe)
    cwd = os.path.join(env.repo, "_build", "default", w.cwd)
    argv = [exe, *w.args, *extra_args]
    setenv = [f"{k}={v}" for k, v in (extra_env or {}).items()]
    with_env = (["env", *setenv] if setenv else [])
    walls, rss_max, codes = [], None, []
    for _ in range(repeats):
        r = env.run([*_time_cmd(), *with_env, *argv], capture=True, cwd=cwd, timeout=timeout_s)
        codes.append(r.returncode)
        wall, rss = parse_time(r.stderr or "")
        if wall is not None:
            walls.append(wall)
        if rss is not None:
            rss_max = max(rss_max or 0, rss)
    r = env.run(["env", *setenv, "OCAMLRUNPARAM=v=0x400", *argv], capture=True, cwd=cwd, timeout=timeout_s)
    gc = parse_gc_stats(r.stderr or "")
    samples: dict = {}
    if symbol and tools_available()["samply"]:
        prof = out_dir / f"{_safe(ref)}.json.gz"
        env.run(["samply", "record", "--save-only", "--unstable-presymbolicate", "-o", str(prof), "--", *with_env, *argv],
                capture=True, cwd=cwd, timeout=timeout_s)
        syms = prof.with_name(prof.name.removesuffix(".gz") + ".syms.json")   # samply's sidecar name
        if prof.exists() and syms.exists():
            sh = sample_shares(str(prof), str(syms), symbol)
            inclusive_pct, warnings = assess(sh)
            samples = dict(samples_total=sh.total, samples_symbol=sh.inclusive, symbol_share_pct=inclusive_pct,
                           samples_symbol_leaf=sh.leaf, symbol_leaf_share_pct=sh.leaf_pct,
                           stack_completeness_pct=sh.completeness_pct, warnings=warnings, profile=str(prof))
    return PerfRun(ref=ref, sha=_git(env.repo, "rev-parse", "HEAD"), build_s=built.elapsed_s,
                   wall_s=tuple(walls), max_rss_bytes=rss_max, gc=gc, exit_codes=tuple(codes),
                   **{"samples_total": None, "samples_symbol": None, "symbol_share_pct": None, "profile": None, **samples})


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def measure_current(env, g, manifest_tests, workload: str, *, symbol: str | None = None, repeats: int = 3,
                    run_dune, timeout_s: int = 1800, extra_args=(), extra_env=None,
                    label: str | None = None, out_dir: Path | None = None) -> tuple[PerfRun, str]:
    """Measure the checkout as it is (uncommitted edits included) and record
    the result as JSON under state/perf/. Returns (run, record path).
    `label` names the run (default: HEAD, or worktree when the tree is
    dirty); `out_dir` lets several measurements share one directory."""
    if P.active(env.repo):
        raise RuntimeError("a profiling session is active (builds would be instrumented); run mina-agent profile --restore first")
    runs = P.resolve_workload(g, manifest_tests, workload)
    if len(runs) != 1:
        raise ValueError(f"{workload} resolves to {len(runs)} executables; give one (test:<dir>/<name> or exe:<path>)")
    dirty = bool(_git(env.repo, "status", "--porcelain", "--untracked-files=no"))
    label = label or ("worktree" if dirty else "HEAD")
    out_dir = out_dir or paths.state_dir() / "perf" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir.mkdir(parents=True, exist_ok=True)
    r = measure(env, runs[0], label, symbol=symbol, repeats=repeats, out_dir=out_dir, run_dune=run_dune,
                timeout_s=timeout_s, extra_args=extra_args, extra_env=extra_env)
    from .model import to_json
    rec = out_dir / f"measure-{_safe(label)}.json"
    rec.write_text(json.dumps({"workload": workload, "symbol": symbol, "dirty_tree": dirty, "extra_args": list(extra_args),
                               "extra_env": dict(extra_env or {}), "run": to_json(r)}, indent=1))
    return r, str(rec)


def compare(env, g, manifest_tests, workload: str, base: str, head: str, *, symbol: str | None = None,
            repeats: int = 3, run_dune, timeout_s: int = 1800, extra_args=(), extra_env=None) -> PerfCompare:
    """measure_current at `base`, then at `head`, restoring the original
    checkout afterwards. Only the git choreography lives here."""
    repo = env.repo
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("the working tree has uncommitted changes; commit or stash them before comparing")
    base_sha = _git(repo, "rev-parse", "--verify", base + "^{commit}")
    head_sha = _git(repo, "rev-parse", "--verify", head + "^{commit}")
    original = _git(repo, "branch", "--show-current") or _git(repo, "rev-parse", "HEAD")
    out_dir = paths.state_dir() / "perf" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    def checkout(sha):
        if _git(repo, "rev-parse", "HEAD") != sha:
            _git(repo, "switch", "--detach", "--quiet", sha)
            subprocess.run(["git", "submodule", "update", "--recursive", "--quiet"], cwd=repo, capture_output=True)

    def at(sha, label):
        checkout(sha)
        return measure_current(env, g, manifest_tests, workload, symbol=symbol, repeats=repeats, run_dune=run_dune,
                               timeout_s=timeout_s, extra_args=extra_args, extra_env=extra_env,
                               label=label, out_dir=out_dir)[0]

    try:
        b = at(base_sha, base)
        h = at(head_sha, head)
    finally:
        _git(repo, "switch", "--quiet", *(("--detach", original) if re.fullmatch(r"[0-9a-f]{40}", original) else (original,)))
        subprocess.run(["git", "submodule", "update", "--recursive", "--quiet"], cwd=repo, capture_output=True)
    return PerfCompare(workload=workload, symbol=symbol, base=b, head=h, restored_to=original, tools=tools_available())


def deltas(c: PerfCompare) -> dict:
    """Head relative to base, the numbers a PR description would claim."""
    def pct(a, b):
        return round(100 * (b - a) / a, 1) if a else None
    bw, hw = c.base.wall_median_s, c.head.wall_median_s
    out: dict[str, object] = {"wall_median_s": {"base": bw, "head": hw, "change_pct": pct(bw, hw) if bw and hw else None}}
    if c.base.max_rss_bytes and c.head.max_rss_bytes:
        out["max_rss_mb"] = {"base": round(c.base.max_rss_bytes / 1e6, 1), "head": round(c.head.max_rss_bytes / 1e6, 1),
                             "change_pct": pct(c.base.max_rss_bytes, c.head.max_rss_bytes)}
    if c.base.gc and c.head.gc:
        a, b = c.base.gc.allocated_bytes, c.head.gc.allocated_bytes
        out["allocated_gb"] = {"base": round(a / 1e9, 3), "head": round(b / 1e9, 3), "change_pct": pct(a, b),
                               "saved_gb": round((a - b) / 1e9, 3)}
    if c.base.symbol_leaf_share_pct is not None and c.head.symbol_leaf_share_pct is not None:
        out["symbol_leaf_share_pct"] = {"symbol": c.symbol, "base": c.base.symbol_leaf_share_pct,
                                        "head": c.head.symbol_leaf_share_pct,
                                        "change_points": round(c.head.symbol_leaf_share_pct - c.base.symbol_leaf_share_pct, 1),
                                        "meaning": "self time: samples whose innermost frame is the symbol"}
    if c.base.symbol_share_pct is not None and c.head.symbol_share_pct is not None:
        out["symbol_share_pct"] = {"symbol": c.symbol, "base": c.base.symbol_share_pct, "head": c.head.symbol_share_pct,
                                   "change_points": round(c.head.symbol_share_pct - c.base.symbol_share_pct, 1),
                                   "meaning": "inclusive: samples with the symbol anywhere on the stack"}
    if c.base.stack_completeness_pct is not None:
        out["stack_completeness_pct"] = {"base": c.base.stack_completeness_pct, "head": c.head.stack_completeness_pct}
    if w := tuple(dict.fromkeys(c.base.warnings + c.head.warnings)):
        out["warnings"] = list(w)
    return out
