---
name: fix_bug
allowed_tools: Read, Grep, Glob, Edit, Write, LSP, Bash(git *), Bash(gh *), Bash(mina-agent lint *), mcp__mina-harness__*
disallowed_tools: Bash(git push *), Bash(git push), Bash(gh pr *), Bash(gh repo *), NotebookEdit, WebFetch, WebSearch, Agent, Task
permission_mode: acceptEdits
max_turns: 80
max_budget_usd: 20
args: issue
needs: gh
---
Fix the bug reported in GitHub issue `{{issue}}`, in three stages, leaving a
series of commits a reviewer can read one at a time.

Shell access is limited to `git`, `gh`, and `mina-agent lint`. Building,
type-checking and testing go through the mina-harness tools (build, check,
check_dependents, errors, test, test_one, tests_for) and the code-reading
tools (find_module, usages, definition, type_at, deps_of, dependents_of).
After each edit of a `.ml` or `.mli` file the harness type-checks it and
returns the diagnostics; read them before doing anything else.

Commits are the user's. They are authored by the configured git identity and
carry no Co-Authored-By line, no "Generated with" line, and no trailer of any
kind. Never amend, squash, rebase, reset, push, or open a pull request.

0. Read the issue: `gh issue view {{issue}} --json number,title,body,labels,comments`.
   Note the issue number. Check `git status --short` is clean and
   `git branch --show-current`; if the branch is develop, compatible or
   master, create `fix/<number>-<short-slug>` with `git switch -c` first.
   Otherwise stay on the current branch.

1. Investigate. Locate the code the issue is about (find_module, usages,
   definition, Grep), read it, and build its library with `build`. Form a
   precise statement of the defect: which function, which input, which wrong
   behaviour, at file:line. If the issue turns out not to be a bug in this
   repository, or the fix lies in a protected path (the Rust proof-systems
   boundary or build configuration), stop and report that.

2. Reproduce with tests that fail. Write one or more unit tests that fail
   because of the defect and would pass once it is fixed: `let%test_unit`
   or `let%test` blocks in the library's existing test module, or a new file
   in its test directory, following the conventions already there. Run them
   with `test_one` (or `test`) and confirm they fail for the reason in the
   issue, not for a compile error or a missing dependency. Then commit the
   tests alone: `git add <test files>` and `git commit -m "<subject>"` with a
   body that quotes the observed failure and references #<number>. This
   commit is expected to fail CI on its own; that is the point.

3. Fix in logical steps. Make the change as a series of commits, each one a
   single reviewable idea (a refactor that enables the fix, the fix itself,
   a follow-up the fix requires), each building cleanly (`check` on every
   edited file, `check_dependents` when an interface changed) before it is
   committed. The last commit makes the tests from stage 2 pass: run them
   again with `test_one` or `test`, then run the first fast candidate from
   `tests_for` on each library you changed. Subjects under 72 characters,
   bodies that say why, `Fixes #<number>` in the final commit's body.

If the pre-commit hook blocks a commit, read its output: for formatting run
`mina-agent lint --fix` and `git add` the files it reformatted; for anything
else fix the cause. Do not bypass the hook.

Finish with a report: the issue in one sentence; the root cause with
file:line; the tests added and their failure before the fix; the commits in
order (`git log --oneline` from the branch point) with one line each on what
and why; the tests run at the end and their results; anything left open.
