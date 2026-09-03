#!/usr/bin/env python3
"""PostToolUse hook for Edit|Write: type-check the edited OCaml file.

Reads the hook JSON from stdin (docs: "Hook stdin JSON input" / PostToolUse),
ignores anything that is not .ml/.mli, otherwise runs tools.check() and
returns the structured result as additionalContext (docs: "JSON stdout
output format" / PostToolUse). Never blocks: exit 0 always.
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
        return 0
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith((".ml", ".mli")):
        return 0
    import tools  # derives the graph; ~1 s
    try:
        r = tools.check(path)
    except Exception as ex:
        out = {"hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": f"harness check skipped for {path}: {ex}"}}
        print(json.dumps(out))
        return 0
    n_err, n_warn = len(r["errors"]), len(r["warnings"])
    status = "ok" if r["ok"] else f"{n_err} error(s)"
    ctx = {"harness_check": {"path": r["path"], "alias": r["alias"], "ok": r["ok"],
                             "elapsed_s": r["elapsed_s"], "errors": r["errors"],
                             "warnings": r["warnings"][:10]}}
    if not r["ok"] and not r["errors"]:
        ctx["harness_check"]["raw_tail"] = r["raw_tail"]
    first = f" ({r['errors'][0]['file']}:{r['errors'][0]['line']})" if r["errors"] else ""
    out = {"systemMessage": f"harness check {r['alias']}: {status}{first} in {r['elapsed_s']}s",
           "hookSpecificOutput": {"hookEventName": "PostToolUse",
                                  "additionalContext": json.dumps(ctx)}}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
