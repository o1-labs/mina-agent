#!/usr/bin/env python3
"""Bash command guard: block environment mutation and raw toolchain calls.

offending(command) splits compound commands on the
separators the permissions docs list (&&, ||, ;, |, |&, &, newline), strips
the documented wrappers (timeout, time, nice, nohup, stdbuf, env assignments,
command, builtin), and checks each subcommand's first word. Used by `mina-agent hook pre-bash`
(exit 2 blocks) and by the in-process PreToolUse callback in agent.py.
"""
import json
import os
import re
import shlex
import sys

MSG_TOOLS = "use the mina-harness MCP tools (build/check/test) instead"
MSG_ENV = "environment mutation is off limits in this harness (see harness/README.md)"

# first word -> reason. Basenames, so ./_opam/bin/dune matches too.
BLOCKED = {
    "dune": MSG_TOOLS, "ocamlfind": MSG_TOOLS, "ocamlopt": MSG_TOOLS, "ocamlc": MSG_TOOLS,
    "opam": MSG_ENV, "nix": MSG_ENV, "nix-env": MSG_ENV, "nix-shell": MSG_ENV,
    "nix-build": MSG_ENV, "cargo": MSG_ENV, "rustup": MSG_ENV,
    "make": MSG_ENV + "; make targets run scripts/update-opam-switch.sh",
    "update-opam-switch.sh": MSG_ENV, "pin-external-packages.sh": MSG_ENV,
    "cabal": MSG_ENV, "pip": MSG_ENV, "pip3": MSG_ENV,
}
WRAPPERS = {"timeout", "time", "nice", "nohup", "stdbuf", "command", "builtin",
            "noglob", "env", "sudo", "exec"}
REDIRECT_FD = re.compile(r"\d*>&\d+|&>")
SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;"}
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")


def strip_heredocs(command):
    """Drop heredoc bodies so their text is not mistaken for commands."""
    out, skip_until = [], None
    for line in command.split("\n"):
        if skip_until is not None:
            if line.strip() == skip_until:
                skip_until = None
            continue
        m = HEREDOC.search(line)
        if m:
            skip_until = m.group(1)
        out.append(line)
    return "\n".join(out)


def _split_unquoted_newlines(text):
    """Split on newlines that are not inside single or double quotes, so a
    multi-line quoted argument (a commit message, a heredoc-free string) is
    one token rather than several commands."""
    out, cur, q, esc = [], [], None, False
    for ch in text:
        if esc:
            cur.append(ch); esc = False; continue
        if ch == "\\" and q != "'":
            cur.append(ch); esc = True; continue
        if q:
            if ch == q:
                q = None
            cur.append(ch)
        elif ch in ("'", '"'):
            q = ch; cur.append(ch)
        elif ch == "\n":
            out.append("".join(cur)); cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def subcommands(command):
    """Split on shell separators, respecting quotes (docs list &&,||,;,|,|&,&,newline)."""
    subs = []
    for line in _split_unquoted_newlines(strip_heredocs(REDIRECT_FD.sub(" ", command))):
        lex = shlex.shlex(line, posix=True, punctuation_chars=";&|")
        lex.whitespace_split = True
        cur = []
        try:
            for tok in lex:
                if tok in SEPARATORS or set(tok) <= set(";&|"):
                    if cur:
                        subs.append(cur)
                    cur = []
                else:
                    cur.append(tok)
        except ValueError:  # unbalanced quotes: fall back to whitespace split
            cur = line.split()
        if cur:
            subs.append(cur)
    return subs


def first_word(toks):
    i = 0
    while i < len(toks):
        t = toks[i]
        if (t in WRAPPERS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or t.startswith("-")
                or re.match(r"^\d+[smhd]?$", t)):  # wrapper args like `timeout 30`, `nice -n 5`
            i += 1
            continue
        return os.path.basename(t)
    return None


def offending(command):
    for toks in subcommands(command):
        w = first_word(toks)
        if w and w in BLOCKED:
            return " ".join(toks), w, BLOCKED[w]
    return None
