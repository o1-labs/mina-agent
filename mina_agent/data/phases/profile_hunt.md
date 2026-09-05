---
name: profile_hunt
session: profile
allowed_tools: Read, Grep, Glob, Edit, LSP, mcp__mina-harness__*
disallowed_tools: mcp__mina-harness__perf_compare, mcp__mina-harness__perf_measure, mcp__mina-harness__bug_report_file, mcp__mina-harness__bug_report_bundle, Bash, Write, NotebookEdit, WebFetch, WebSearch, Agent, Task
permission_mode: acceptEdits
max_turns: 40
max_budget_usd: 10
args: focus
---
Find where library `{{focus}}` spends its time and allocation, zoom in until
the hot code is small enough to read, and either make a measured
optimization or report exactly what one would be.

The library is compiled with landmarks instrumentation: every top-level
function is a probe. Tools: profile_run (build a workload instrumented, run
it under the profiler; reports the focus's share of the time and its top
functions), profile_top (rank by self_ms, total_ms, calls, self_alloc_mb,
alloc_mb; self excludes callees), profile_callers (who calls a function and
what it calls, with time under each edge), profile_diff (per-function change
between two profiles of the same workload), profile_add_library (put another
library under instrumentation when the hot path crosses into it; windows
outside the instrumented set are inert), profile_link_impl (link a chosen
implementation of a virtual library into the workload: test runners get
the default one, e.g. disk_cache's identity, not what the daemon uses),
plus the usual build, check,
errors, usages, find_module, type_at, definition, and file reads.

You have about 40 tool calls in total. Spend them like this:

1. One profile_run on the first workload candidate listed under Session
   below. If the focus's share of time is near zero, the workload does not
   reach the focus: run the next candidate once, then proceed with whatever
   you have.
2. profile_top by self_ms and by self_alloc_mb. Time and allocation are
   different questions. Use profile_callers once on the top function to see
   what makes it hot.
3. If the hottest focus function's cost is a call into another library,
   profile_add_library that library and profile_run the same workload
   again before anything else. If the hottest function is longer than
   about 60 lines, add finer windows with Edit: wrap the sub-expressions you suspect as
   `expr [@landmark "name"]` (or `let[@landmark] f = ...` for local
   bindings), keeping the code otherwise identical, then profile_run the
   same workload again and profile_top. One round of windows; the harness
   type-checks each edit and returns diagnostics, read them.
4. Read the code of the regions that are both substantial (about 5% of
   total or more) and small enough to read in full. Explain what costs
   what, with the numbers.
5. If a change is clearly correct and local (an allocation avoided, a
   repeated computation hoisted, a linear scan replaced), make it with Edit,
   profile_run the same workload, and profile_diff against the earlier
   profile. If the workload's tests fail after the change, Read the `log`
   file profile_run returns (the run's complete output) and its parsed
   `failures` before deciding anything. Otherwise do not edit for speculation.

Reading the numbers: times are milliseconds calibrated to CPU time;
instrumentation adds a per-call cost, so a tiny function called hundreds of
thousands of times looks worse than it is (judge by calls x cost and by
allocation as well); `load(<module>)` is module initialization; ROOT is the
whole run. Do not report the test harness itself (test runners, quickcheck
generators) as a hotspot unless nothing else remains.

Before finishing, remove every `[@landmark ...]` window you added, leaving
only a real optimization if you made one. Do not run test or test_one.

Finish with a report: the workload and its focus share; the ranked regions
with self time, allocation, calls, and file:line; the cause of each; the
optimization made with its measured profile_diff, or the concrete change
you would make and what it would save; and which files you changed.
