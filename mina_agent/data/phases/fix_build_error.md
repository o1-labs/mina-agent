---
name: fix_build_error
allowed_tools: Read, Grep, Glob, Edit, LSP, mcp__mina-harness__*
disallowed_tools: mcp__mina-harness__bug_report_file, mcp__mina-harness__bug_report_bundle, Bash, Write, NotebookEdit, WebFetch, WebSearch, Agent, Task
permission_mode: acceptEdits
max_turns: 40
max_budget_usd: 5
args: target
---
Run the mina-harness `build` tool on `{{target}}`.

If it fails, locate the cause using the structured errors it returns, the LSP
tool (goToDefinition, hover, findReferences) when available, the harness tools
`tests_for`, `deps_of`, `dependents_of`, `library_of`, `definition`, `type_at`,
and file reads. Make the minimal fix with Edit. After
each edit of a `.ml` or `.mli` file the harness type-checks it automatically
and returns the diagnostics; read that result before doing anything else.
Re-run `build` until it passes.

If the interface of a library changed, run `check_dependents` on it.

Once `build` passes, call `tests_for` on the file you changed (or on the
target if nothing changed) and run the first candidate that is runnable in
the current mode with the `test` tool. Do not run tests marked slow unless no
fast candidate exists.

If the cause lies in a protected path (the Rust proof-systems boundary or
build configuration such as dune-project, *.opam, flake.nix, Cargo.toml),
stop and report that instead of working around it.

Finish with a short report: what was wrong, what you changed and why (file
and line), which test you ran and its result. If nothing needed changing,
say so.
