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
from .model import GcStats, PerfCompare, PerfRun, Workload

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


def symbol_share(profile_gz: str, syms_json: str, symbol: str) -> tuple[int, int]:
    """(samples whose stack contains a function whose name contains `symbol`,
    total samples) over every thread of a samply profile, using the
    presymbolicated sidecar to name frame addresses."""
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

    hit = total = 0
    for th in prof["threads"]:
        libs = [prof["libs"][i]["name"] if i is not None else None for i in th["resourceTable"]["lib"]]
        ft, fu, st = th["frameTable"], th["funcTable"], th["stackTable"]
        frame_hit = [False] * ft["length"]
        for f in range(ft["length"]):
            res = fu["resource"][ft["func"][f]]
            n = name_of(libs[res], ft["address"][f]) if res is not None and res >= 0 else None
            frame_hit[f] = bool(n and symbol in n)
        stack_hit: dict[int, bool] = {}
        for s in range(st["length"]):
            pre = st["prefix"][s]
            stack_hit[s] = frame_hit[st["frame"][s]] or (pre is not None and stack_hit[pre])
        for s in th["samples"]["stack"]:
            if s is None:
                continue
            total += 1
            hit += stack_hit[s]
    return hit, total


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
    samples = (None, None, None, None)
    if symbol and tools_available()["samply"]:
        prof = out_dir / f"{_safe(ref)}.json.gz"
        env.run(["samply", "record", "--save-only", "--unstable-presymbolicate", "-o", str(prof), "--", *with_env, *argv],
                capture=True, cwd=cwd, timeout=timeout_s)
        syms = prof.with_name(prof.name.removesuffix(".gz") + ".syms.json")   # samply's sidecar name
        if prof.exists() and syms.exists():
            hit, total = symbol_share(str(prof), str(syms), symbol)
            samples = (total, hit, round(100 * hit / total, 1) if total else 0.0, str(prof))
    return PerfRun(ref=ref, sha=_git(env.repo, "rev-parse", "HEAD"), build_s=built.elapsed_s,
                   wall_s=tuple(walls), max_rss_bytes=rss_max, gc=gc,
                   samples_total=samples[0], samples_symbol=samples[1], symbol_share_pct=samples[2],
                   profile=samples[3], exit_codes=tuple(codes))


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)


def measure_current(env, g, manifest_tests, workload: str, *, symbol: str | None = None, repeats: int = 3,
                    run_dune, timeout_s: int = 1800, extra_args=(), extra_env=None) -> tuple[PerfRun, str]:
    """Measure the working tree as it is (uncommitted edits included) and
    record the result under state/perf/. Returns (run, record path)."""
    if P.active(env.repo):
        raise RuntimeError("a profiling session is active (builds would be instrumented); run mina-agent profile --restore first")
    runs = P.resolve_workload(g, manifest_tests, workload)
    if len(runs) != 1:
        raise ValueError(f"{workload} resolves to {len(runs)} executables; give one (test:<dir>/<name> or exe:<path>)")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = paths.state_dir() / "perf" / stamp
    out_dir.mkdir(parents=True, exist_ok=True)
    dirty = bool(_git(repo := env.repo, "status", "--porcelain", "--untracked-files=no"))
    r = measure(env, runs[0], "worktree" if dirty else "HEAD", symbol=symbol, repeats=repeats, out_dir=out_dir,
                run_dune=run_dune, timeout_s=timeout_s, extra_args=extra_args, extra_env=extra_env)
    from .model import to_json
    rec = out_dir / "measure.json"
    rec.write_text(json.dumps({"workload": workload, "symbol": symbol, "dirty_tree": dirty, "extra_args": list(extra_args),
                               "extra_env": dict(extra_env or {}), "run": to_json(r)}, indent=1))
    return r, str(rec)


def compare(env, g, manifest_tests, workload: str, base: str, head: str, *, symbol: str | None = None,
            repeats: int = 3, run_dune, timeout_s: int = 1800, extra_args=(), extra_env=None) -> PerfCompare:
    """Measure `workload` at `base` then `head`, restoring the original checkout."""
    repo = env.repo
    if _git(repo, "status", "--porcelain", "--untracked-files=no"):
        raise RuntimeError("the working tree has uncommitted changes; commit or stash them before comparing")
    if P.active(repo):
        raise RuntimeError("a profiling session is active (builds would be instrumented); run mina-agent profile --restore first")
    base_sha, head_sha = _git(repo, "rev-parse", "--verify", base + "^{commit}"), _git(repo, "rev-parse", "--verify", head + "^{commit}")
    original = _git(repo, "branch", "--show-current") or _git(repo, "rev-parse", "HEAD")
    runs = P.resolve_workload(g, manifest_tests, workload)
    if len(runs) != 1:
        raise ValueError(f"{workload} resolves to {len(runs)} executables; give one (test:<dir>/<name> or exe:<path>)")
    w = runs[0]
    out_dir = paths.state_dir() / "perf" / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir.mkdir(parents=True, exist_ok=True)

    def checkout(sha):
        if _git(repo, "rev-parse", "HEAD") != sha:
            _git(repo, "switch", "--detach", "--quiet", sha)
            subprocess.run(["git", "submodule", "update", "--recursive", "--quiet"], cwd=repo, capture_output=True)

    try:
        def one(ref):
            return measure(env, w, ref, symbol=symbol, repeats=repeats, out_dir=out_dir, run_dune=run_dune,
                           timeout_s=timeout_s, extra_args=extra_args, extra_env=extra_env)
        checkout(base_sha)
        b = one(base)
        checkout(head_sha)
        h = one(head)
    finally:
        _git(repo, "switch", "--quiet", *(("--detach", original) if re.fullmatch(r"[0-9a-f]{40}", original) else (original,)))
        subprocess.run(["git", "submodule", "update", "--recursive", "--quiet"], cwd=repo, capture_output=True)
    return PerfCompare(workload=workload, symbol=symbol, base=b, head=h, restored_to=original, tools=tools_available())


def deltas(c: PerfCompare) -> dict:
    """Head relative to base, the numbers a PR description would claim."""
    def pct(a, b):
        return round(100 * (b - a) / a, 1) if a else None
    bw, hw = c.base.wall_median_s, c.head.wall_median_s
    out = {"wall_median_s": {"base": bw, "head": hw, "change_pct": pct(bw, hw) if bw and hw else None}}
    if c.base.max_rss_bytes and c.head.max_rss_bytes:
        out["max_rss_mb"] = {"base": round(c.base.max_rss_bytes / 1e6, 1), "head": round(c.head.max_rss_bytes / 1e6, 1),
                             "change_pct": pct(c.base.max_rss_bytes, c.head.max_rss_bytes)}
    if c.base.gc and c.head.gc:
        a, b = c.base.gc.allocated_bytes, c.head.gc.allocated_bytes
        out["allocated_gb"] = {"base": round(a / 1e9, 3), "head": round(b / 1e9, 3), "change_pct": pct(a, b),
                               "saved_gb": round((a - b) / 1e9, 3)}
    if c.base.symbol_share_pct is not None and c.head.symbol_share_pct is not None:
        out["symbol_share_pct"] = {"symbol": c.symbol, "base": c.base.symbol_share_pct, "head": c.head.symbol_share_pct,
                                   "change_points": round(c.head.symbol_share_pct - c.base.symbol_share_pct, 1)}
    return out
