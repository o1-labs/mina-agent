#!/usr/bin/env python3
"""Merge harness/settings.template.json into .claude/settings.local.json.

settings.local.json is gitignored (repo .gitignore: /.claude/), so the hooks
and deny rules the harness needs are kept in the template and merged here.
Existing keys are preserved: deny lists are unioned, hook events are replaced
by the template's, everything else is left alone. Idempotent.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TEMPLATE = os.path.join(HERE, "settings.template.json")
TARGET = os.path.join(REPO, ".claude", "settings.local.json")


def main():
    with open(TEMPLATE) as fh:
        tpl = json.load(fh)
    tpl.pop("_comment", None)
    cur = {}
    if os.path.exists(TARGET):
        with open(TARGET) as fh:
            cur = json.load(fh)
    perms = cur.setdefault("permissions", {})
    perms["deny"] = sorted(set(perms.get("deny", [])) | set(tpl["permissions"]["deny"]))
    hooks = cur.setdefault("hooks", {})
    hooks.update(tpl["hooks"])
    os.makedirs(os.path.dirname(TARGET), exist_ok=True)
    with open(TARGET, "w") as fh:
        json.dump(cur, fh, indent=2)
        fh.write("\n")
    print(f"merged {os.path.relpath(TEMPLATE, REPO)} into {os.path.relpath(TARGET, REPO)}: "
          f"{len(perms['deny'])} deny rules, hooks for {', '.join(sorted(hooks))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
