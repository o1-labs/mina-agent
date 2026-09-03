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
    status = tools.ENV.to_dict()
    facts = tools.facts()
    out = {"systemMessage": banner.render(status),
           "hookSpecificOutput": {"hookEventName": "SessionStart",
                                  "additionalContext": "\n".join(facts)}}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
