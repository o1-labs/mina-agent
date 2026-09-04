"""Parsers for compiler and test-runner output. Pure: text in, records out."""
import re

from .model import Diagnostic, Severity, TestFailure

RAW_TAIL_BYTES = 4096

# OCaml location header: File "f", line[s] N[-M][, characters A-B]:
HEADER = re.compile(r'^File "(?P<file>[^"]+)", lines? (?P<line>\d+)(?:-(?P<line_end>\d+))?'
                    r'(?:, characters (?P<col_start>\d+)-(?P<col_end>\d+))?')
SEVERITY = re.compile(r"^(Error(?: \([^)]*\))?|Warning(?: \d+)?(?: \[[^\]]*\])?):\s*(.*)")


def split_diags(text: str) -> tuple[list[Diagnostic], list[Diagnostic]]:
    """(errors, warnings) from dune output."""
    d = parse_dune_errors(text)
    return ([e for e in d if e.severity is Severity.ERROR],
            [e for e in d if e.severity is Severity.WARNING])


def parse_dune_errors(text: str) -> list[Diagnostic]:
    """Diagnostics from dune/ocaml output. A header opens a location; the
    next Error:/Warning: line gives severity and message; further non-blank
    lines continue the message; a blank line or the next header ends it.
    A header never followed by a severity line is dropped."""
    out: list[Diagnostic] = []
    loc: dict | None = None      # header fields of the open location
    cur: Diagnostic | None = None

    def close():
        nonlocal cur
        if cur:
            out.append(cur)
        cur = None

    for line in text.splitlines():
        m = HEADER.match(line)
        if m:
            close()
            g = m.groupdict()
            loc = {"file": g["file"], "line": int(g["line"]),
                   **{k: int(g[k]) if g[k] is not None else None for k in ("line_end", "col_start", "col_end")}}
            continue
        if loc is None:
            continue
        s = SEVERITY.match(line)
        if s:
            sev = Severity.ERROR if s.group(1).startswith("Error") else Severity.WARNING
            cur = Diagnostic(severity=sev, message=s.group(2).strip(), **loc)
        elif cur and line.strip():
            cur = Diagnostic(**{**loc, "severity": cur.severity, "message": cur.message + " " + line.strip()})
        elif cur:
            close()
            loc = None
    close()
    return out


INLINE_FAIL = re.compile(r'^File "([^"]+)", line (\d+), characters [\d-]+: (.*) (?:threw|is false)')
INLINE_SUMMARY = re.compile(r"^\d+ tests? ran, \d+ test_modules? ran")
ALCOTEST_FAIL = re.compile(r"^\s*\[FAIL\]\s+(.*)")
ALCOTEST_SUMMARY = re.compile(r"^\d+ failures?!|^Test Successful in|^\d+ tests? run")


def parse_test_output(text: str) -> tuple[list[TestFailure], str | None]:
    """(failures, summary line) from ppx_inline_test or alcotest output."""
    failures: list[TestFailure] = []
    summary = None
    for line in text.splitlines():
        m = INLINE_FAIL.match(line)
        if m:
            failures.append(TestFailure(m.group(3), m.group(1), int(m.group(2))))
            continue
        m = ALCOTEST_FAIL.match(line)
        if m:
            failures.append(TestFailure(m.group(1).strip()))
            continue
        if INLINE_SUMMARY.match(line) or ALCOTEST_SUMMARY.match(line):
            summary = line.strip()
    return failures, summary


def tail(text: str) -> str:
    return text[-RAW_TAIL_BYTES:]


