# Adding a phase

A phase is a headless or interactive workflow: a prompt template plus the
walls and limits it runs under. It is one markdown file; nothing else has to
be registered. Drop `mina_agent/data/phases/<name>.md` in place and
`mina-agent run <name>` exists, with one `--<arg>` option per declared
argument, listed by `mina-agent show phases` and `mina-agent run --help`.

## The file

```markdown
---
name: verify_perf                    # command is the name with _ -> -
args: pr                             # required --pr option; {{pr}} in the body
allowed_tools: Read, Grep, Glob, LSP, Bash(git *), Bash(gh *), mcp__mina-harness__*
disallowed_tools: Edit, Write, Bash(git push *), mcp__mina-harness__test
permission_mode: default             # default | acceptEdits
max_turns: 40                        # headless turn budget (--max-turns overrides)
max_budget_usd: 15                   # headless dollar budget
mode: interactive                    # interactive | headless (default headless)
needs: gh                            # executables that must be on PATH, checked before start
env: GH_TOKEN                        # variables that must be set (shell or harness/.envrc)
session: profile                     # optional: start a profiling session around the run
---
The prompt. `{{pr}}` is substituted; an unfilled placeholder is an error.
```

Everything above the second `---` is front matter; `name` defaults to the
file stem; lists are comma-separated. A malformed file is skipped with a
warning at startup and never breaks the other commands.

## What each key buys you

**Walls, not prose.** `allowed_tools` and `disallowed_tools` are enforced by
Claude Code, not by the prompt. A pattern like `Bash(git *)` allows a
command family; a bare `Bash` in `disallowed_tools` removes the shell
entirely (the default for headless phases). The toolchain deny rules from
`settings.template.json` (dune, opam, nix, cargo, make, pip, the compilers)
and the build-config edit rules ride along in every phase; you cannot
re-enable them from a phase. If the prompt says "do not run X", also deny X
here; the prompt is advice, the wall is the rule. Headless runs also get a
base tool set equal to the built-ins you allow, so the MCP tools are never
deferred behind ToolSearch.

**Mode.** Headless runs through the SDK with no prompts, `permission_mode`
deciding what happens without asking (`acceptEdits` for phases that edit),
a run log under `state/logs/` and `--trace` for the trajectory summary.
Interactive opens the TUI with the prompt as the first message and the same
walls, for work that takes minutes and needs a human's judgment
(`verify_perf`). Either default is overridden per invocation with
`--headless` or `--interactive`.

**Budgets.** `max_turns` is the count of assistant turns, roughly one per
tool call plus the final report. Count the steps the prompt demands,
including any cleanup it requires at the end, and leave room: a run cut off
at the budget leaves that cleanup undone (`profile_hunt` moved from 20 to 40
for exactly this reason). `max_budget_usd` is the other cap.

**Preconditions.** `needs` and `env` are checked before anything runs and
fail with a message that says what to install or export. Prefer them over a
first step in the prompt that discovers the same thing after spending
turns.

**Session.** `session: profile` instruments the focus library (the phase
must declare `args: focus`), appends the profiling `## Session` block to the
prompt, and restores the dune files afterwards, in both modes. It is the
only session type today; a new one is a small addition to
`commands/run.py` (`_run_phase`) mirroring how `profile` is handled.

## Writing the prompt

The prompts that have worked share a shape:

1. **Say what is and is not available in one paragraph.** Name the harness
   tools to use for building, checking and testing, and what the shell is
   limited to. The model behaves better told the walls than discovering
   them through denials.
2. **Numbered stages with a stop condition each.** Each stage says what to
   call, what a good result looks like, and when to stop and report instead
   of continuing (`verify_perf` stops when the PR does not say what it
   measured; `fix_bug` stops when the fix lies in a protected path). Guessing
   is the failure mode to design against.
3. **Budget guidance in the prompt itself** when steps are expensive: "call
   perf_compare once", "one round of windows".
4. **A report template** at the end: the fields, in order, that the run's
   final message must contain. This is what a human reads and what
   `--trace` summarises.
5. **Cleanup before the report**, if the phase leaves anything behind, and
   what remains the user's (source edits are never reverted by the
   harness).

Keep the prompt about the task. Facts about the environment (mode, dune,
ocaml, the library graph, the manifest) are appended to the system prompt
automatically from `tools.facts()`; do not restate them.

## When the phase needs a new tool

Add a function to `mina_agent/tools.py` and its name to `TOOLS`; the MCP
server picks it up. Return a JSON-able dict built from typed records
(`model.py`) with `to_json()` at the edge; raise `ValueError` or
`RuntimeError` for anticipated failures, the server turns them into text
the model can read. Then decide which *other* phases must not see it and
add it to their `disallowed_tools` (`perf_compare`, `bug_report_file` and
`profile_run` are denied in the phases that have no business with them). A
tool that changes the checkout, like `perf_compare`, must refuse on a dirty
tree and restore in a `finally`; a tool that runs long must write its full
output to disk and return the path, not a truncated tail.

## Checking it

```
mina-agent show phases                       # the command, args, tools, limits
mina-agent run <name> --<arg> x --dry-run    # rendered prompt, options, tool inventory
uv run pytest tests/test_phases.py           # front-matter parsing, registration, skipping
```

The dry run is the one to read: it shows the exact prompt after
substitution and, for headless phases, the SDK options including the base
tool set and the full disallowed list; for interactive phases, the `claude`
command line. Add a test in `tests/test_phases.py` when the phase relies on
a new front-matter key or a wall that must not regress (the settings tests
are the model: assert the wall, not the prose).

## Checklist

- [ ] file under `mina_agent/data/phases/`, `name` and `args` set
- [ ] walls: every "do not" in the prompt is also a `disallowed_tools` entry
- [ ] `mode` chosen; budgets cover the steps plus cleanup
- [ ] `needs` / `env` for anything the first step would otherwise discover
- [ ] prompt: availability paragraph, stages with stop conditions, report template
- [ ] new tools registered in `TOOLS` and denied where they do not belong
- [ ] `--dry-run` read end to end; `show phases` lists it; tests pass
