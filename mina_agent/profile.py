"""A profiling session: which libraries are instrumented, how, and how a
workload is run under the profiler.

The session is a file, harness/state/profile/session.json, so every process
of the harness (the MCP server, the post-edit hook, `mina-agent profile
--restore`) sees the same state: while it exists, every dune command the
harness runs carries `--instrument-with landmarks`, so _build stays
instrumented for the in-scope libraries instead of flipping on each check.

Instrumentation is a temporary edit: the harness inserts the landmarks
stanza into each in-scope library's dune stanza, keeps the original bytes in
the session file, and restores them when the session ends or on --restore.
It refuses to touch a dune file that already has uncommitted changes, and it
never reverts .ml/.mli files: a source edit made during the session may be
the optimization itself, and windows the model added are its to remove.
"""
import base64
import dataclasses
import datetime as dt
import hashlib
import json
import os
import re
import subprocess

from . import landmarks, paths
from .graph import write_json_atomic
from .model import ProfileEntry, RestoreReport, Session, Workload, WorkloadCandidate, to_json

DUNE_SUBCOMMANDS = ("build", "exec", "runtest", "test")


def state_dir(repo):
    p = paths.state_dir(repo) / "profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def session_file(repo):
    return state_dir(repo) / "session.json"


def load(repo) -> Session | None:
    p = session_file(repo)
    if not p.exists():
        return None
    with open(p) as fh:
        return Session.from_json(json.load(fh))


def save(repo, s: Session):
    write_json_atomic(session_file(repo), to_json(s))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def active(repo):
    return session_file(repo).exists()


def dune_argv(repo, argv):
    """argv with --instrument-with landmarks inserted while a session is active."""
    if argv and os.path.basename(argv[0]) == "dune" and len(argv) > 1 and argv[1] in DUNE_SUBCOMMANDS \
            and "--instrument-with" not in argv and active(repo):
        return argv[:2] + ["--instrument-with", "landmarks"] + argv[2:]
    return argv


# --------------------------------------------------------------------------
# dune stanza injection
# --------------------------------------------------------------------------

def _forms(text):
    """Top-level s-expression spans of a dune file: [(start, end_exclusive)].
    Respects strings and line comments."""
    out, depth, start, i, n = [], 0, None, 0, len(text)
    while i < n:
        c = text[i]
        if c == ";":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            if depth == 0:
                start = i
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0 and start is not None:
                out.append((start, i + 1))
                start = None
        i += 1
    return out


def library_span(text, name):
    """Span of the (library ...) form declaring (name <name>), or None."""
    pat = re.compile(r"\(\s*name\s+" + re.escape(name) + r"\s*\)")
    for s, e in _forms(text):
        form = text[s:e]
        if re.match(r"\(\s*library\b", form) and pat.search(form):
            return s, e
    return None


def inject_stanza(text, name):
    """text with the landmarks stanza added to library <name>, or None."""
    span = library_span(text, name)
    if span is None:
        return None
    s, e = span
    form = text[s:e]
    if "backend landmarks" in form:
        return text
    return text[:e - 1].rstrip() + "\n" + landmarks.STANZA + text[e - 1:]


def _git_dirty(repo, relpath):
    d = os.path.dirname(os.path.join(repo, relpath))
    r = subprocess.run(["git", "-C", d, "status", "--porcelain", "--", os.path.basename(relpath)],
                       capture_output=True, text=True)
    return bool(r.stdout.strip())


def resolve_focus(g, focus) -> str:
    """A library key from a dune name, a public name, or a source path inside it."""
    if focus in g["libraries"]:
        return focus
    if focus in g["public_names"]:
        return g["public_names"][focus]
    from . import tools
    u = tools.library_of(focus)
    if u["kind"] != "lib":
        raise ValueError(f"{focus} is inside {u['kind']} unit {u['key']}, not a library; focus on a library")
    return u["key"]


def workload_candidates(g, focus) -> list[WorkloadCandidate]:
    """Candidate workloads for the focus, cheapest and most direct first."""
    from . import tools
    rec = g["libraries"][focus]
    mt = tools.manifest_tests()
    out: list[WorkloadCandidate] = []
    if rec["has_inline_tests"]:
        out.append(WorkloadCandidate(f"inline:{focus}", "the focus library's own inline tests", "unmeasured"))
    for c in tools.tests_for(rec["dir"])["candidates"]:
        name = c["name"]
        spec = name if name in mt else (f"test:{name[5:]}" if name.startswith("test:") else name)
        if any(spec == o.spec for o in out):
            continue
        try:
            resolve_workload(g, mt, spec)
        except ValueError:
            continue
        out.append(WorkloadCandidate(spec, c["reason"], c["cost"]))
    if "src/app/benchmarks" in g["by_dir"]:
        out.append(WorkloadCandidate("exe:src/app/benchmarks/benchmarks.exe", "the repo's core_bench executable", "slow"))
    return out[:12]


def session_block(repo, g, focus, scope, libs) -> str:
    """The `## Session` orientation text both the interactive and the headless
    profiling session hand the model: focus, instrumented scope, workload
    candidates, where profiles land."""
    names = ": " + ", ".join(libs[:15]) + (" ..." if len(libs) > 15 else "") if len(libs) > 1 else ""
    plural = "y" if len(libs) == 1 else "ies"
    lines = ["## Session", "",
             f"Focus: library {focus} in {g['libraries'][focus]['dir']}. Scope {scope}: "
             f"{len(libs)} instrumented librar{plural}{names}.",
             "Workload candidates (cheapest and most direct first):"]
    lines += [f"  profile_run(\"{w.spec}\")  [{w.cost}]  {w.reason}" for w in workload_candidates(g, focus)]
    lines += ["", f"Profiles land in {state_dir(repo)}; profile ids are their file stems."]
    return "\n".join(lines)


def scope_libraries(g, focus, scope):
    """Library keys to instrument for a focus: the focus alone, its direct
    local deps, or its whole local dependency cone."""
    libs = [focus]
    if scope == "deps":
        libs += [d for d in g["libraries"][focus]["deps"] if d in g["libraries"]]
    elif scope == "cone":
        seen, stack = {focus}, [focus]
        while stack:
            for d in g["libraries"][stack.pop()]["deps"]:
                if d in g["libraries"] and d not in seen:
                    seen.add(d)
                    stack.append(d)
                    libs.append(d)
    return libs


def start(repo, g, focus, scope, libs) -> Session:
    """Inject stanzas for libs and record the session. Refuses if one is
    already active or any target dune file is dirty."""
    if active(repo):
        raise RuntimeError("a profiling session is already active; end it with mina-agent profile --restore")
    if not landmarks.present(repo):
        raise RuntimeError("landmarks is not vendored; run mina-agent setup")
    plan, skipped = {}, []
    for lib in libs:
        rec = g["libraries"][lib]
        dune = os.path.join(rec["dir"], "dune")
        full = os.path.join(repo, dune)
        if not os.path.isfile(full):
            skipped.append((lib, "no dune file"))
            continue
        with open(full, encoding="utf-8") as fh:
            text = fh.read()
        new = inject_stanza(text, lib)
        if new is None:
            skipped.append((lib, "library stanza not found in its dune file"))
            continue
        if new == text:
            continue   # already carries the stanza (checked in)
        if _git_dirty(repo, dune):
            raise RuntimeError(f"{dune} has uncommitted changes; commit or stash them before profiling")
        plan[dune] = (text, new)
    for dune, (text, new) in plan.items():
        with open(os.path.join(repo, dune), "w", encoding="utf-8") as fh:
            fh.write(new)
    s = Session(started=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                focus=focus, scope=scope, libraries=tuple(libs),
                dirs=tuple(g["libraries"][l]["dir"] for l in libs),
                injected={dune: base64.b64encode(text.encode()).decode() for dune, (text, _) in plan.items()},
                injected_sha={dune: _sha(new) for dune, (_, new) in plan.items()},
                skipped=tuple(skipped))
    save(repo, s)
    return s


def restore(repo) -> RestoreReport:
    """Put every injected dune file back and end the session. Never touches
    .ml/.mli files, and never overwrites a dune file that was edited after
    injection (reported as `edited` instead)."""
    s = load(repo)
    if s is None:
        return RestoreReport(note="no active session")
    restored, edited, still_dirty = [], [], []
    for dune, b64 in s.injected.items():
        full = os.path.join(repo, dune)
        with open(full, encoding="utf-8") as fh:
            current = fh.read()
        if _sha(current) != s.injected_sha.get(dune, _sha(current)):
            edited.append(dune)     # someone changed it during the session; theirs to resolve
            continue
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(base64.b64decode(b64).decode())
        restored.append(dune)
        if _git_dirty(repo, dune):
            still_dirty.append(dune)
    edits = sorted({l[3:] for d in s.dirs
                    for l in subprocess.run(["git", "-C", os.path.join(repo, d), "status", "--porcelain", "--", "."],
                                            capture_output=True, text=True).stdout.splitlines()
                    if l[3:].endswith((".ml", ".mli"))})
    windows_left = [f for f in edits if "[@landmark" in open(os.path.join(repo, f), encoding="utf-8").read()]
    session_file(repo).unlink()
    return RestoreReport(restored=tuple(restored), edited=tuple(edited), still_dirty=tuple(still_dirty),
                         source_edits=tuple(edits), windows_left=tuple(windows_left), profiles=s.profiles)


# --------------------------------------------------------------------------
# workloads
# --------------------------------------------------------------------------

def resolve_workload(g, manifest_tests, spec) -> list[Workload]:
    """A workload spec -> the executables to build and run.

    inline:<lib>            the library's ppx_inline_test runner
    test:<dir>/<name>       a (test) unit from the graph
    exe:<path/to/x.exe>     any executable target
    <manifest test name>    its test units (a @dir/runtest command) or exe
    """
    if spec.startswith("inline:"):
        lib = spec[7:]
        rec = g["libraries"].get(lib)
        if not rec or not rec["has_inline_tests"]:
            raise ValueError(f"{lib} is not a library with (inline_tests)")
        d = rec["dir"]
        exe = f"{d}/.{lib}.inline-tests/inline_test_runner_{lib}.exe"
        return [Workload(exe, exe, ("inline-test-runner", lib), d)]
    if spec.startswith("test:"):
        key = spec[5:]
        rec = g["tests"].get(key)
        if not rec:
            raise ValueError(f"unknown test unit {key!r}")
        exe = f"{rec['dir']}/{rec['name']}.exe"
        return [Workload(exe, exe, (), rec["dir"])]
    if spec.startswith("exe:"):
        exe = spec[4:]
        return [Workload(exe, exe, (), os.path.dirname(exe))]
    t = manifest_tests.get(spec)
    if not t:
        raise ValueError(f"unknown workload {spec!r}: use inline:<lib>, test:<dir>/<name>, exe:<path.exe>, "
                         "or a manifest test name")
    cmd = t["command"]
    if cmd[:2] == ["dune", "exec"] and cmd[2].endswith(".exe"):
        return [Workload(cmd[2], cmd[2], (), os.path.dirname(cmd[2]))]
    alias = next((a for a in cmd if a.startswith("@") and a.endswith("/runtest")), None)
    if alias:
        d = alias[1:-len("/runtest")]
        units = [(k, r) for k, r in g["tests"].items() if r["dir"] == d]
        if units:
            return [Workload(f"{d}/{r['name']}.exe", f"{d}/{r['name']}.exe", (), d) for _, r in units]
        libs = [k for k, r in g["libraries"].items() if r["dir"] == d and r["has_inline_tests"]]
        if libs:
            return resolve_workload(g, manifest_tests, f"inline:{libs[0]}")
    raise ValueError(f"manifest test {spec!r} runs {' '.join(cmd)}, which has no executable the "
                     "profiler can run directly; use test:<dir>/<name> or exe:<path.exe>")


def next_profile_path(repo, spec):
    """Next free NNN-<spec>.json in the profile dir, numbered after whatever
    is on disk (a run that wrote a profile but was never recorded still
    holds its number)."""
    taken = [int(m.group(1)) for p in state_dir(repo).glob("*.json")
             if (m := re.match(r"(\d{3})-", p.name))]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", spec)
    return state_dir(repo) / f"{max(taken, default=0) + 1:03d}-{safe}.json"


def record_profile(repo, entry: ProfileEntry):
    s = load(repo)
    if s is not None:
        save(repo, dataclasses.replace(s, profiles=(*s.profiles, entry)))
