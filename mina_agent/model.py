"""Domain types shared across the harness.

Everything that crosses a module boundary as data is declared here once:
the toolchain mode, check statuses, compiler diagnostics, dune run results,
test failures, phases. Modules build these and read their attributes; the
JSON sinks (MCP results, run logs, --json output) call to_json() at the edge.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, NamedTuple


class Mode(StrEnum):
    """How the OCaml toolchain is reached."""
    OPAM = "opam"
    NIX = "nix"
    NONE = "none"


class Status(StrEnum):
    """Outcome of a lint or doctor check."""
    OK = "ok"
    NOTE = "note"
    SKIP = "skip"
    FAIL = "fail"


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One compiler message with its location. line_end is set for
    `lines N-M` headers; columns are absent for dune-file errors."""
    file: str
    line: int
    severity: Severity
    message: str
    line_end: int | None = None
    col_start: int | None = None
    col_end: int | None = None


class DuneRun(NamedTuple):
    """Outcome of one dune invocation."""
    code: int
    out: str
    elapsed_s: float
    timed_out: bool

    @property
    def ok(self) -> bool:
        return self.code == 0


@dataclass(frozen=True, slots=True)
class TestFailure:
    name: str
    file: str | None = None
    line: int | None = None


@dataclass(frozen=True, slots=True)
class BuildProvenance:
    """What produced _build, read from _build/log."""
    exists: bool
    built_by: str | None = None      # "opam" | "nix" | "unknown"
    ocamlc: str | None = None


@dataclass(frozen=True, slots=True)
class Phase:
    """A headless phase: a prompt template plus its walls and limits."""
    name: str
    path: str
    body: str
    args: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    disallowed_tools: tuple[str, ...] = ()
    permission_mode: str = "default"
    max_turns: int = 30
    max_budget_usd: float = 5.0
    session: str | None = None       # "profile": run inside a profiling session on args["focus"]

    @property
    def summary(self) -> str:
        return self.body.split("\n\n", 1)[0].replace("\n", " ")

    @property
    def command_name(self) -> str:
        return self.name.replace("_", "-")


def to_json(obj: Any) -> Any:
    """Plain JSON-able data: dataclasses and NamedTuples become dicts,
    recursively; StrEnum members are already str."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_json(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, tuple) and hasattr(obj, "_asdict"):
        return {k: to_json(v) for k, v in getattr(obj, "_asdict")().items()}
    if isinstance(obj, dict):
        return {k: to_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [to_json(v) for v in obj]
    return obj
