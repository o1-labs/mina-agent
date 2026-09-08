"""Bash allowlist for development sessions.

A PreToolUse hook asks decide() about every Bash command. The answer is
deliberately conservative: a command is allowed only when every segment
(split on |, ||, &&, ; and newlines outside quotes) starts with an allowed
head, carries no command substitution, process substitution or output
redirection to a file, and, for mina-agent, uses an allowed subcommand.
Redirects that write nothing (`2>&1`, `>&2`, `>/dev/null`, `</dev/null`)
are fine. Anything the parser is unsure about is denied; the user can
always run it in their own terminal.
"""
import os
import re
import shlex
from dataclasses import dataclass

UNSAFE = ("$(", "`", "<(", ">(")
QUOTED = re.compile(r"'[^']*'|\"(?:[^\"\\\\]|\\\\.)*\"")
# fd duplication (2>&1, >&2, 1>&-) and /dev/null in either direction
HARMLESS_REDIRECT = re.compile(r"&?\d*>&[\d-]+|&?\d*>>?\s*/dev/null|<\s*/dev/null")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True, slots=True)
class Verdict:
    allowed: bool
    reason: str


def segments(command: str):
    """Split on |, ||, &&, ; and newlines, ignoring separators inside single
    or double quotes and after a backslash. A lone & (background, or part of
    `2>&1`) is not a separator."""
    out, buf, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i + 1])
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c == "\\" and i + 1 < len(command):
            buf.append(c)
            buf.append(command[i + 1])
            i += 1
        elif c in "|;\n" or (c == "&" and command[i + 1:i + 2] == "&"):
            out.append("".join(buf))
            buf = []
            if c in "|&" and command[i + 1:i + 2] == c:
                i += 1
        else:
            buf.append(c)
        i += 1
    out.append("".join(buf))
    return out


def decide(command: str, heads, mina_agent_subcommands) -> Verdict:
    heads = set(heads)
    for tok in UNSAFE:
        if tok in command:
            return Verdict(False, f"{tok} runs an arbitrary command; not allowed in a development session")
    for seg in segments(command):
        seg = seg.strip()
        if not seg:
            continue
        bare = HARMLESS_REDIRECT.sub(" ", QUOTED.sub("", seg))
        if re.search(r"(?<![<>])>(?!>)|>>|(?<!<)<(?![<])", bare):
            return Verdict(False, "shell redirection to a file is not allowed; use the Write tool for files")
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
