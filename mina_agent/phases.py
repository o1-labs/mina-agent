"""Phase files: markdown prompts with a small front-matter block.

    ---
    name: fix_build_error
    allowed_tools: Read, Grep, Glob, Edit, mcp__mina-harness__*
    disallowed_tools: Bash, Write, ...
    permission_mode: acceptEdits
    max_turns: 40
    max_budget_usd: 5
    args: target
    needs: gh                # executables that must be on PATH
    mode: interactive        # run in the TUI by default (headless is the default)
    env: SOME_TOKEN          # variables that must be set, in the shell or harness/.envrc
    ---
    Run the mina-harness `build` tool on `{{target}}` ...

Each file under mina_agent/data/phases/ becomes a `mina-agent <name>`
subcommand whose --<arg> options come from `args`. Nothing is listed by hand.
"""
import re
import sys
from pathlib import Path

from . import paths
from .model import Phase


def _front_matter(text: str, path) -> tuple[dict[str, str], str]:
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
    if not m:
        raise ValueError(f"{path}: expected a '---' front-matter block")
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, m.group(2).strip()


def _csv(s: str) -> tuple[str, ...]:
    return tuple(x.strip() for x in s.split(",") if x.strip())


def load(path) -> Phase:
    path = Path(path)
    meta, body = _front_matter(path.read_text(encoding="utf-8"), path)
    return Phase(
        name=meta.get("name") or path.stem,
        path=str(path),
        body=body,
        args=_csv(meta.get("args", "")),
        allowed_tools=_csv(meta.get("allowed_tools", "")),
        disallowed_tools=_csv(meta.get("disallowed_tools", "")),
        permission_mode=meta.get("permission_mode", "default"),
        max_turns=int(meta.get("max_turns", 30)),
        max_budget_usd=float(meta.get("max_budget_usd", 5)),
        session=meta.get("session"),
        env=_csv(meta.get("env", "")),
        needs=_csv(meta.get("needs", "")),
        mode=_mode(meta.get("mode", "headless"), path),
    )


def _mode(value: str, path) -> str:
    if value not in ("headless", "interactive"):
        raise ValueError(f"{path}: mode must be headless or interactive, got {value!r}")
    return value


def all_phases() -> list[Phase]:
    """Every valid phase under data/phases. A malformed file is reported on
    stderr and skipped, so one bad phase cannot take down the CLI (serve,
    hooks and the git pre-commit hook all go through the same entry point)."""
    out = []
    for f in sorted(paths.PHASES.glob("*.md")):
        try:
            out.append(load(f))
        except (ValueError, OSError) as ex:
            sys.stderr.write(f"mina-agent: skipping phase {f.name}: {ex}\n")
    return out


def render(phase: Phase, args: dict) -> str:
    missing = [a for a in phase.args if a not in args or args[a] is None]
    unknown = [a for a in args if a not in phase.args]
    if missing or unknown:
        raise ValueError(f"phase {phase.name}: missing args {missing}, unknown args {unknown}; "
                         f"declared: {list(phase.args)}")
    body = phase.body
    for k, v in args.items():
        body = body.replace("{{" + k + "}}", str(v))
    left = re.findall(r"{{\w+}}", body)
    if left:
        raise ValueError(f"unfilled placeholders: {left}")
    return body
