"""Phase files: markdown prompts with a small front-matter block.

    ---
    name: fix_build_error
    allowed_tools: Read, Grep, Glob, Edit, mcp__mina-harness__*
    disallowed_tools: Bash, Write, ...
    permission_mode: acceptEdits
    max_turns: 40
    max_budget_usd: 5
    args: target
    ---
    Run the mina-harness `build` tool on `{{target}}` ...

Each file under mina_agent/data/phases/ becomes a `mina-agent <name>`
subcommand whose --<arg> options come from `args`. Nothing is listed by hand.
"""
import os
import re

from . import paths


def load(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: expected a '---' front-matter block")
    meta = {}
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    lst = lambda k: [x.strip() for x in meta.get(k, "").split(",") if x.strip()]
    body = m.group(2).strip()
    return {
        "name": meta.get("name") or os.path.splitext(os.path.basename(path))[0],
        "path": str(path),
        "allowed_tools": lst("allowed_tools"),
        "disallowed_tools": lst("disallowed_tools"),
        "permission_mode": meta.get("permission_mode", "default"),
        "max_turns": int(meta.get("max_turns", 30)),
        "max_budget_usd": float(meta.get("max_budget_usd", 5)),
        "args": lst("args"),
        "session": meta.get("session"),   # "profile": run inside a profiling session on args["focus"]
        "body": body,
        "summary": body.split("\n\n", 1)[0].replace("\n", " "),
    }


def all_phases():
    out = []
    for f in sorted(paths.PHASES.glob("*.md")):
        out.append(load(f))
    return out


def render(phase, args):
    missing = [a for a in phase["args"] if a not in args or args[a] is None]
    unknown = [a for a in args if a not in phase["args"]]
    if missing or unknown:
        raise ValueError(f"phase {phase['name']}: missing args {missing}, unknown args {unknown}; "
                         f"declared: {phase['args']}")
    body = phase["body"]
    for k, v in args.items():
        body = body.replace("{{" + k + "}}", str(v))
    left = re.findall(r"{{\w+}}", body)
    if left:
        raise ValueError(f"unfilled placeholders: {left}")
    return body
