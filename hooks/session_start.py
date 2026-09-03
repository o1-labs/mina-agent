#!/usr/bin/env python3
"""SessionStart hook: re-derive the library graph and hand the session its facts.

Emits JSON (docs: "JSON stdout output format" / SessionStart):
  hookSpecificOutput.additionalContext - plain facts for Claude, written as
      statements, never instructions (docs: prompt-injection note).
  systemMessage - the banner + status line, shown to the human only.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        payload = {}
    import tools   # detect() + derive() run at import
    import banner
    env = tools.ENV
    m = tools.MANIFEST_DATA
    status = env.to_dict()
    facts = []
    facts.append(f"mina-harness environment: mode={env.mode} activated={env.activated} "
                 f"dune={env.dune_version} ocaml={env.ocaml}.")
    for w in env.warnings:
        facts.append(f"warning: {w}")
    if tools.GRAPH.error:
        facts.append(f"library graph unavailable: {tools.GRAPH.error}")
    else:
        g = tools.GRAPH.data
        facts.append(f"library graph derived from dune files: {len(g['libraries'])} libraries, "
                     f"{len(g['tests'])} test units, {len(g['executables'])} executables.")
    b = m["boundary"]
    facts.append("OCaml/Rust boundary (read-only, mutable=false): libraries "
                 + ", ".join(b["libraries"]) + f" in {b['stubs_dir']} wrap crates "
                 + ", ".join(b["crates"]) + ". Protected paths: " + ", ".join(b["rust_paths"]) + ".")
    core = "; ".join(f"{k} ({v['dir']}, cheap test {v['cheap_test']})" for k, v in m["core"].items())
    facts.append("Core libraries: " + core + ".")
    tests = "; ".join(f"{t['name']} [{t['cost']}, modes {','.join(t['modes']) or 'none'}]"
                      for t in m["tests"])
    facts.append("Manifest tests: " + tests + ". Any library with (inline_tests) also has "
                 f"{m['inline_tests']['name_prefix']}<library>.")
    facts.append("MCP server mina-harness provides: " + ", ".join(tools.TOOLS)
                 + ". type_at/definition describe code as last compiled; check decides "
                 "whether an edit compiles. Raw dune/opam/nix/cargo/make commands are blocked "
                 "by a hook; build-config and Rust boundary files are deny-listed for edits.")
    out = {"systemMessage": banner.render(status),
           "hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": "\n".join(facts)}}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
