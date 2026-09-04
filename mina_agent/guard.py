"""Bash allowlist for development sessions.

A PreToolUse hook asks decide() about every Bash command. The answer is
deliberately conservative: a command is allowed only when every segment
(split on |, ||, &&, ; and newlines) starts with an allowed head, carries
no command substitution, process substitution or output redirection, and,
for mina-agent, uses an allowed subcommand. Anything the parser is unsure
about is denied; the user can always run it in their own terminal.
"""
import os
import re
import shlex
from dataclasses import dataclass

UNSAFE = ("$(", "`", "<(", ">(")
SEGMENTS = re.compile(r"\|\||&&|;|\||\n")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\\\]|\\\\.)*\"")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str


def decide(command: str, heads, mina_agent_subcommands) -> Verdict:
    heads = set(heads)
    for tok in UNSAFE:
        if tok in command:
            return Verdict(False, f"{tok} runs an arbitrary command; not allowed in a development session")
    for seg in SEGMENTS.split(command):
        seg = seg.strip()
        if not seg:
            continue
        bare = QUOTED.sub("", seg)
        if re.search(r"(?<![<>])>(?!>)|>>|(?<!<)<(?![<])", bare):
            return Verdict(False, "shell redirection is not allowed; use the Write tool for files")
        try:
            argv = shlex.split(seg)
        except ValueError as ex:
            return Verdict(False, f"could not parse the command ({ex})")
        while argv and ASSIGNMENT.match(argv[0]):
            argv = argv[1:]
        if not argv:
            return Verdict(False, "empty command")
        head = os.path.basename(argv[0])
        if head == "mina-agent":
            sub = argv[1] if len(argv) > 1 else ""
            if sub not in mina_agent_subcommands:
                return Verdict(False, f"mina-agent {sub or '<none>'} is not allowed here; "
                                      f"allowed: {', '.join(mina_agent_subcommands)}")
        elif head not in heads:
            return Verdict(False, f"{head} is not in the development session's shell allowlist "
                                  f"(manifest.toml [develop]); build and test through the mina-harness tools")
    return Verdict(True, "allowed")
