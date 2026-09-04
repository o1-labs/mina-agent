---
name: harness-bug-report
description: Use when the user flags something in the mina-agent harness as wrong or broken (a mina-harness tool errored or returned a wrong answer, a hook or session behaved unexpectedly, a confusing message), or asks to report a harness bug. Drafts a bug report for github.com/o1-labs/mina-agent, gathers evidence, and files it with gh when available.
version: 0.1.0
---

# Reporting a harness bug

The harness is the mina-agent tool this session runs under (the
`mina-harness` MCP tools, its hooks, `mina-agent` commands, the dashboard,
its phases). Bugs in the Mina codebase itself are not harness bugs; this
skill is for the tool, not the project.

## When to offer

Whenever the user says a harness tool errored, gave a result they know is
wrong, a hook or the session did something unexpected, or a message made no
sense: offer, in one line, to draft a bug report for o1-labs/mina-agent.
Do not offer for problems in Mina's own code. Do not file anything without
the user's explicit agreement on the final text.

## Steps

1. **Gather evidence first.** Call `bug_report_bundle`. It writes a
   directory and a zip of it under the system temp dir with: environment
   (mode, dune, ocaml, harness and Mina commits), `doctor` output, the last
   headless run logs and their summaries (that is what `--trace` produced),
   the lint gate log, and the active profiling session if any. As soon as
   it returns, tell the user where the evidence is, with the full paths:
   the bundle directory (to browse) and the zip (to attach), and which
   files it holds. Say plainly if it holds no run logs (interactive
   sessions write none; only headless runs do). Then read the files in the
   bundle directory that bear on the report and quote the exact error text
   from them.

2. **Draft the report** with this shape, filled from what happened in this
   session and the bundle:

   ```
   Title: <tool or command>: <one-line symptom>

   ## What happened
   <the exact call or command, the exact output or error, quoted>

   ## What I expected
   <one or two sentences>

   ## Steps to reproduce
   1. ...

   ## Environment
   <the environment block from the bundle: harness commit, Mina commit, mode, dune, ocaml, OS>

   ## Evidence
   <relevant excerpts>
   Bundle: <full zip path> (<the files it holds>)
   ```

3. **Agree on it.** Show the full draft and ask for corrections. Iterate
   until the user says it is right. Never add anything the user did not see.

4. **File it.** Call `bug_report_file` with the agreed title and body and
   the bundle path. Then:
   - If it filed: give the issue URL. GitHub has no upload API, so the zip
     could not be attached; repeat the zip's full path and say that
     dragging it onto the issue page in the browser attaches it.
   - If `gh` is missing or not authenticated: the tool saved the draft as a
     markdown file. Give that path, the zip path, and the new-issue URL
     `https://github.com/o1-labs/mina-agent/issues/new`, and ask the user
     to paste and attach themselves.

Keep the report factual. Quote outputs; do not paraphrase errors.
