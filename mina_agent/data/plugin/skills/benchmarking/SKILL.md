---
name: benchmarking
description: Use when the user wants to measure performance in the Mina codebase - is a change faster or lighter, where time or allocation goes, whether a claimed speedup holds, peak memory - and needs the right instrument chosen and read correctly. Covers samply sample shares, the runtime's exact allocation counters, /usr/bin/time peak RSS and wall clock, before/after comparisons on the working tree or between commits, and when a landmarks profiling session is the better tool.
version: 0.1.0
---

# Benchmarking in the Mina monorepo

Pick the instrument from the question. Every tool here is uninstrumented
except landmarks, and every number is meaningless without the workload,
the commit (or "working tree"), and the noise stated beside it.

## The instruments

| question | instrument | how | trust |
|---|---|---|---|
| Is function F cheaper after my change? | samply sample share | `perf_measure`/`perf_compare` with `symbol` | high: the sampler sees the real binary; compare shares, not seconds |
| How much less does it allocate? | GC counters (`OCAMLRUNPARAM=v=0x400`) | `perf_measure`/`perf_compare`, field `allocated_gb` | exact and deterministic: a claimed "N GB saved" must match to the decimal |
| Is peak memory different? | `/usr/bin/time -l` max RSS | `max_rss_mb` in the same results | reliable; short-lived garbage does not move it, retention does |
| Is the whole thing faster? | wall clock, median of repeats | `wall_median_s` and the spread in `wall_s` | noisy on a shared machine; believe it only beyond the spread |
| Where does the time or allocation go, per function, and what calls it? | landmarks | a profiling session: `mina-agent profile --focus <lib>` (interactive) or `mina-agent profile --focus <lib> --headless` | attributes cost to functions with `[@landmark]` windows to zoom; adds per-call overhead, so ratios, not absolutes |
| Is memory retained (a leak, a large live set)? | memtrace | not wired into the harness; say so and describe the manual route (`MEMTRACE=file`, a binary linked with memtrace) | — |

samply symbol names are the linker's: `camlStaged_ledger__hash_1234`. A
substring such as `Staged_ledger__hash` matches every instance; check the
share is not diluted by same-named functions elsewhere (`usages` and
`find_module` tell you).

How the share is computed, and what can go wrong with it:
- It is CPU-weighted and scoped to the threads that run OCaml code. A Mina
  test process has a Rust rayon pool doing most of the raw CPU (field
  arithmetic) and threads blocked in the kernel; neither belongs in the
  denominator of an OCaml function's share. `ocaml_cpu_share_pct` says how
  much of the process that scope was. This is the denominator a landmarks
  figure has, so the two are comparable.
- `symbol_share_pct` is inclusive (the symbol anywhere on the stack) and
  needs complete stacks. `stack_completeness_pct` is how much of that CPU
  reaches `caml_start_program`; below 80% the inclusive share is withheld
  (None) with a warning. On macOS arm64 with the OCaml 4.14 switch that is
  the normal case (no frame pointers, no compact unwind for OCaml frames):
  stacks stop at the first OCaml frame above a C or Rust leaf.
- `symbol_leaf_share_pct` is self time (innermost frame) and is correct
  regardless of unwinding. Use it on the functions the change touches
  (the serializer, the hash primitive, GC entry points) rather than on a
  caller that does little work itself; a caller's leaf share is ~0 by
  nature. Complete inclusive stacks need Linux perf with DWARF unwinding.
- Sample share compares *fractions* of the run under a symbol; if the
  workload also changed size, wall clock and shares move for other reasons.

## Choosing the workload

The workload is an executable the harness can build and run:
`inline:<library>` (its ppx_inline_test runner), `test:<dir>/<name>` (a
test executable), `exe:<path.exe>`, or a manifest test name. `tests_for`
lists candidates for a source path with their cost. Prefer the smallest
workload that exercises the code under study for a meaningful fraction of
its run; `perf_measure` with a `symbol` tells you that fraction. A test
that takes a size from an environment variable or a flag is the way to a
"mainnet-sized" run: pass `env="VAR=value"` or `extra_args="--flag value"`,
and say which you used.

## Procedures

**Before/after on the working tree** (while writing code):
1. On the unedited tree: `perf_measure(workload, symbol=..., repeats=3)`.
   Note `record` (saved under state/perf/).
2. Make the change.
3. `perf_measure` again with identical arguments. Compare `symbol_share_pct`,
   `allocated_gb`, `max_rss_mb`, `wall_median_s`, and the wall spread.
   If `dirty_tree` is true in the record, say the result is for uncommitted
   edits.

**Before/after between commits** (a branch, a PR): `perf_compare(workload,
base=<merge-base>, head=<sha>, symbol=..., repeats=3)`. It checks out each
commit, builds, measures, and restores the branch. It refuses on a dirty
tree: commit or stash first. `deltas` in its result is already in the shape
a PR description claims.

**Where is the cost:** a landmarks session. Leave this session for
`mina-agent profile --focus <lib>` (or run it headless), and bring the
ranked functions back here to act on them. Do not try to add `[@landmark]`
windows in a develop session; they only mean something under that session's
instrumented build.

## Reading the numbers

- Repeat at least three times; report the median and the spread. A wall
  clock difference inside the spread is not a result.
- Allocation is exact; peak RSS is exact for the run but depends on GC
  timing; sample shares depend on the sampler seeing enough samples (state
  `samples_total`; under a few hundred, lengthen the workload).
- Never compare numbers taken on different machines or with a profiling
  session active (its builds are instrumented; the tools refuse for that
  reason).
- Say what was measured in one line before the numbers: workload, commit or
  working tree, extra args or env, repeats.

## Reporting

One table per question: instrument, workload, before, after, change, noise.
Then a one-line verdict per question: resolved (with the number), not
resolvable with this workload (and what would resolve it), or unmeasured.
