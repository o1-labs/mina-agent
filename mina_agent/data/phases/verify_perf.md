---
name: verify_perf
allowed_tools: Read, Grep, Glob, LSP, Bash(git *), Bash(gh *), mcp__mina-harness__*
disallowed_tools: Edit, Write, NotebookEdit, WebFetch, WebSearch, Agent, Task, Bash(git switch *), Bash(git checkout *), Bash(git stash*), Bash(git reset*), Bash(git rebase*), Bash(git merge*), Bash(git commit*), Bash(git push*), Bash(gh pr create*), Bash(gh pr merge*), Bash(gh pr checkout*), mcp__mina-harness__profile_run, mcp__mina-harness__test, mcp__mina-harness__test_one, mcp__mina-harness__bug_report_file, mcp__mina-harness__bug_report_bundle
permission_mode: default
max_turns: 40
max_budget_usd: 15
args: pr
needs: gh
---
Verify the performance claims of pull request `{{pr}}` by reproducing them
with uninstrumented measurements of the base and head commits, and say
whether the claimed numbers roughly recover.

You cannot edit files, switch branches, or run tests directly. The one
measuring tool is `perf_compare`: it checks out base and head in place,
builds the workload, measures each with /usr/bin/time (wall clock, peak
RSS), OCAMLRUNPARAM=v=0x400 (bytes allocated) and, given a `symbol`,
samply (share of CPU samples under that function), and restores the branch.
It is slow; call it once with the right workload, not repeatedly.

0. Read the PR: `gh pr view {{pr}} --json number,title,body,url,baseRefName,
   baseRefOid,headRefName,headRefOid` and `gh pr diff {{pr}} --name-only`.
   Make sure both commits are local (`git rev-parse <headRefOid>`; if not,
   `git fetch <remote> <headRefName>`). base for the comparison is
   `git merge-base <baseRefOid> <headRefOid>`, so the PR's own effect is
   isolated from later movement of the base branch.

1. Extract the claims. From the description (and comments if the body
   points there), write down each claim as: metric (time, allocation, peak
   memory, sample share), workload (what was run: which test, benchmark
   executable, or command), before, after, and how it was measured. Then
   decide:
   - If the description does not say what was run, or says only that a
     live node was observed, or names a workload that has no executable in
     this repository: stop now. Report that the claim cannot be located,
     quote what the PR says about measurement, and list exactly what the
     author must add (the command or test, the commit measured, the
     machine class). Do not guess a workload.
   - Otherwise map the workload to a spec: inline:<library> for a
     library's inline tests, test:<dir>/<name> for a test executable,
     exe:<path.exe> for a benchmark or other executable, or a manifest test
     name (`tests_for` and `library_of` help find them; the changed files
     from `gh pr diff` point at the library). If the PR names a function
     whose cost changed, pass its OCaml name as `symbol` (e.g. `hash`
     inside `Staged_ledger` is `camlStaged_ledger__hash`; a substring such
     as `Staged_ledger__hash` is enough).

2. Measure once: `perf_compare(workload, base=<merge-base>,
   head=<headRefOid>, symbol=<if any>, repeats=3)`. Budget for it: two
   incremental builds and five runs of the workload per side. If it fails
   because the tree is dirty or a profiling session is active, report that
   and stop.

3. Judge. For each claim, put the measured base, head and change beside
   the claimed before, after and change. Time claims are compared on
   median wall clock, or on sample share when a symbol was named (the share
   is CPU-weighted over the OCaml threads; if `symbol_share_pct` is None the
   stacks were incomplete: say so and use `symbol_leaf_share_pct` of the
   functions the change touches, or report the time claim unresolvable on
   this machine);
   allocation claims on bytes allocated (the GC's exact count, so a claimed
   "N GB saved" should match closely); memory claims on peak RSS.
   "Recovered" means the direction matches and the magnitude is within a
   factor that the workload's noise explains (state the noise: the spread
   of the three wall-clock runs). Wall clock on a shared machine is noisy;
   allocation and sample share are not.

Finish with a report: the PR and the two commits measured; the workload
used and why it is the one the PR describes; a table of claims versus
measurements; a one-line verdict per claim (recovered, partly, not
recovered, could not measure) with the reason; the measurement noise; and
any claim you had to leave unverified. Do not speculate about why a change
is faster; that is the PR's job.
