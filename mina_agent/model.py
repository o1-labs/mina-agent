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
    __test__ = False        # keep pytest from collecting it
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
    env: tuple[str, ...] = ()        # environment variables the phase needs (from the shell or harness/.envrc)

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


# --------------------------------------------------------------------------
# profiling
# --------------------------------------------------------------------------

class Workload(NamedTuple):
    """One executable to build and run under the profiler."""
    target: str                 # dune build target
    exe: str                    # path under _build/default
    args: tuple[str, ...]       # argv tail
    cwd: str                    # run directory under _build/default


class WorkloadCandidate(NamedTuple):
    spec: str
    reason: str
    cost: str


@dataclass(frozen=True, slots=True)
class ProfileEntry:
    """One recorded profile of a session."""
    profile: str                # file stem, the id tools take
    path: str
    workload: str
    only_test: str | None
    exe: str
    exit_code: int
    run_s: float
    build_s: float
    total_ms: float
    units: str
    functions: int
    focus_functions_hit: int
    focus_self_share_pct: float
    log: str | None = None      # full stdout+stderr of the run, next to the profile


@dataclass(frozen=True, slots=True)
class Session:
    """An active profiling session, persisted as state/profile/session.json."""
    started: str
    focus: str
    scope: str
    libraries: tuple[str, ...]
    dirs: tuple[str, ...]
    injected: dict[str, str]            # dune path -> base64 of the original bytes
    injected_sha: dict[str, str]        # dune path -> sha256 of the injected text
    skipped: tuple[tuple[str, str], ...] = ()
    profiles: tuple[ProfileEntry, ...] = ()

    @classmethod
    def from_json(cls, d: dict) -> "Session":
        return cls(started=d["started"], focus=d["focus"], scope=d["scope"],
                   libraries=tuple(d["libraries"]), dirs=tuple(d["dirs"]),
                   injected=dict(d["injected"]), injected_sha=dict(d.get("injected_sha", {})),
                   skipped=tuple((a, b) for a, b in d.get("skipped", [])),
                   profiles=tuple(ProfileEntry(**p) for p in d.get("profiles", [])))


@dataclass(frozen=True, slots=True)
class RestoreReport:
    """What restore() did and what it left for a human."""
    restored: tuple[str, ...] = ()
    edited: tuple[str, ...] = ()        # dune files changed during the session, left as is
    still_dirty: tuple[str, ...] = ()
    source_edits: tuple[str, ...] = ()  # .ml/.mli changed under the instrumented dirs
    windows_left: tuple[str, ...] = ()  # of those, ones still carrying [@landmark]
    profiles: tuple[ProfileEntry, ...] = ()
    note: str | None = None


@dataclass(frozen=True, slots=True)
class CallerEdge:
    caller: str
    calls: int
    total_ms: float


@dataclass(frozen=True, slots=True)
class FunctionStats:
    """Per-function aggregate over every call-tree instance in a profile."""
    name: str
    location: str
    kind: str
    self_ms: float
    total_ms: float
    calls: int
    self_alloc_mb: float
    alloc_mb: float
    self_pct: float
    callers: tuple[CallerEdge, ...]     # by time under the edge, largest first

    def under(self, dirs) -> bool:
        return any(self.location.startswith(d + "/") for d in dirs)


@dataclass(frozen=True, slots=True)
class Profile:
    label: str | None
    hz: float | None
    units: str                          # "ms" when calibrated, else "ticks"
    total_ms: float
    nodes: int
    functions: dict[str, FunctionStats]  # key: "name @ file:line"
