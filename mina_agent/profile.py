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
from .model import LinkedImpl, ProfileEntry, RestoreReport, Session, Workload, WorkloadCandidate, to_json

DUNE_SUBCOMMANDS = ("build", "exec", "runtest", "test")


def state_dir():
    p = paths.state_dir() / "profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def session_file(repo):
    return state_dir() / "session.json"


def last_session_file(repo):
    """Where restore() archives the session it ended, so `profile --continue`
    can re-create it: same libraries, same recorded profiles."""
    return state_dir() / "last-session.json"


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
    return unit_span(text, "lib", name)


STANZA_HEADS = {"lib": ("library",), "test": ("test", "tests"), "exe": ("executable", "executables")}


def unit_span(text, kind, name):
    """Span of the stanza of `kind` declaring `name`: (name X) or a member of
    (names ...). None when absent."""
    single = re.compile(r"\(\s*name\s+" + re.escape(name) + r"\s*\)")
    multi = re.compile(r"\(\s*names\b([^)]*)\)")
    for s, e in _forms(text):
        form = text[s:e]
        head = re.match(r"\(\s*([a-z_]+)\b", form)
        if not head or head.group(1) not in STANZA_HEADS[kind]:
            continue
        m = multi.search(form)
        if single.search(form) or (m and name in m.group(1).split()):
            return s, e
    return None


def _fields(text, span):
    """(field name, (start, end)) of every sub-form directly inside a form."""
    s, e = span
    inner_start = s + 1
    out = []
    for fs, fe in _forms(text[inner_start:e - 1]):
        a, b = inner_start + fs, inner_start + fe
        head = re.match(r"\(\s*([a-z_]+)\b", text[a:b])
        out.append((head.group(1) if head else "", (a, b)))
    return out


def _field(text, span, name):
    return next((sp for n, sp in _fields(text, span) if n == name), None)


def _indent_of(text, pos):
    """Whitespace between the start of pos's line and pos."""
    nl = text.rfind("\n", 0, pos)
    return text[nl + 1:pos] if text[nl + 1:pos].strip() == "" else ""


def _append_to_libraries(text, span, lib):
    """text with `lib` added to the (libraries ...) field of the form at span,
    creating the field when absent; unchanged when already listed. New lines
    follow the indentation of the form's existing fields."""
    libs = _field(text, span, "libraries")
    if libs:
        a, b = libs
        if lib in re.findall(r"[^\s()]+", text[a:b])[1:]:
            return text
        indent = _indent_of(text, a) + " "
        return text[:b - 1].rstrip() + f"\n{indent}{lib}" + text[b - 1:]
    s, e = span
    fields = _fields(text, span)
    indent = _indent_of(text, fields[0][1][0]) if fields else _indent_of(text, s) + " "
    return text[:e - 1].rstrip() + f"\n{indent}(libraries {lib})" + text[e - 1:]


def link_library(text, kind, name, lib):
    """text with `lib` linked into the unit `name` of `kind`: for a library,
    into its (inline_tests (libraries ...)) so the inline-test runner links
    it; for a test or executable, into its (libraries ...). None when the
    stanza (or, for a library, its inline_tests field) is not found."""
    span = unit_span(text, kind, name)
    if span is None:
        return None
    if kind == "lib":
        it = _field(text, span, "inline_tests")
        if it is None:
            return None
        return _append_to_libraries(text, it, lib)
    return _append_to_libraries(text, span, lib)


def _modify_dune(repo, s: Session, dune: str, transform) -> Session:
    """Apply `transform(text) -> text | None` to a dune file the session may
    already have edited, keeping the record restore() needs: the original
    bytes are kept from the first edit, injected_sha follows the latest.
    A file edited outside the session (sha mismatch) or dirty before its
    first edit is refused."""
    full = os.path.join(repo, dune)
    with open(full, encoding="utf-8") as fh:
        current = fh.read()
    if dune in s.injected:
        if _sha(current) != s.injected_sha.get(dune):
            raise RuntimeError(f"{dune} was edited outside the session since it was instrumented; not touching it")
    elif _git_dirty(repo, dune):
        raise RuntimeError(f"{dune} has uncommitted changes; commit or stash them first")
    new = transform(current)
    if new is None:
        raise RuntimeError(f"could not find the stanza to edit in {dune}")
    if new == current:
        return s
    with open(full, "w", encoding="utf-8") as fh:
        fh.write(new)
    injected = s.injected if dune in s.injected else {**s.injected, dune: base64.b64encode(current.encode()).decode()}
    return dataclasses.replace(s, injected=injected, injected_sha={**s.injected_sha, dune: _sha(new)})


def _closure(g, roots):
    """Local libraries reachable from roots through deps (roots included when libraries)."""
    seen, stack = set(), list(roots)
    while stack:
        k = stack.pop()
        if k in seen or k not in g["libraries"]:
            continue
        seen.add(k)
        stack.extend(g["libraries"][k]["deps"])
    return seen


def link_unit(g, workload: str):
    """The stanza a workload links through: (kind, name, dir, root deps)."""
    w = resolve_workload(g, {}, workload)[0] if workload.startswith(("inline:", "test:", "exe:")) else None
    if workload.startswith("inline:"):
        lib = workload[7:]
        return "lib", lib, g["libraries"][lib]["dir"], [lib]
    if workload.startswith("test:"):
        rec = g["tests"][workload[5:]]
        return "test", rec["name"], rec["dir"], rec["deps"]
    if workload.startswith("exe:"):
        assert w is not None
        key = f"{os.path.dirname(w.exe)}/{os.path.basename(w.exe).removesuffix('.exe')}"
        rec = g["executables"].get(key)
        if rec is None:
            raise ValueError(f"{workload} is not an executable the graph describes; use test:<dir>/<name> or inline:<lib>")
        return "exe", rec["name"], rec["dir"], rec["deps"]
    raise ValueError(f"{workload!r}: give inline:<lib>, test:<dir>/<name> or exe:<path.exe> to link into")


def link_impl(repo, g, impl: str, workload: str) -> tuple[Session, dict]:
    """Link implementation `impl` (a library key) of a virtual library into
    the workload's link unit for the rest of the session."""
    s = load(repo)
    if s is None:
        raise RuntimeError("no active profiling session; start one with mina-agent profile --focus <library>")
    rec = g["libraries"].get(impl)
    if rec is None:
        raise ValueError(f"unknown library {impl!r}")
    virtual = rec.get("implements")
    if not virtual:
        raise ValueError(f"{impl} does not implement a virtual library (no `implements` in its stanza)")
    kind, name, d, roots = link_unit(g, workload)
    others = sorted(k for k in _closure(g, roots) if g["libraries"][k].get("implements") == virtual and k != impl)
    if others:
        raise RuntimeError(f"{workload} already links {', '.join(others)} for {virtual}; dune refuses two "
                           f"implementations of one virtual library, so {impl} cannot be added")
    dune = os.path.join(d, "dune")
    public = rec.get("public_name") or impl
    s2 = _modify_dune(repo, s, dune, lambda t: link_library(t, kind, name, public))
    already = s2 is s
    entry = LinkedImpl(impl=impl, virtual=virtual, workload=workload, dune=dune)
    if entry not in s2.linked:
        s2 = dataclasses.replace(s2, linked=(*s2.linked, entry))
    save(repo, s2)
    return s2, {"impl": impl, "public_name": public, "virtual": virtual, "workload": workload, "unit": f"{kind} {name}",
                "dune": dune, "already_linked": already,
                "note": "the next profile_run / test / test_one relinks the workload with it; profiles recorded "
                        "before this call used the previous implementation"}


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
    lines += ["", f"Profiles land in {state_dir()}; profile ids are their file stems."]
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


def _instrument(repo, g, libs):
    """Inject the landmarks stanza into each library's dune file. Plans every
    edit before writing any, so a dirty file refuses the whole batch.
    Returns (injected: dune path -> base64 original, injected_sha: dune path
    -> sha of the written text, skipped: (lib, reason))."""
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
    return ({dune: base64.b64encode(text.encode()).decode() for dune, (text, _) in plan.items()},
            {dune: _sha(new) for dune, (_, new) in plan.items()}, skipped)


def start(repo, g, focus, scope, libs) -> Session:
    """Inject stanzas for libs and record the session. Refuses if one is
    already active or any target dune file is dirty."""
    if active(repo):
        raise RuntimeError("a profiling session is already active; end it with mina-agent profile --restore")
    if not landmarks.present(repo):
        raise RuntimeError("landmarks is not vendored; run mina-agent admin setup")
    injected, injected_sha, skipped = _instrument(repo, g, libs)
    s = Session(started=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                focus=focus, scope=scope, libraries=tuple(libs),
                dirs=tuple(g["libraries"][l]["dir"] for l in libs),
                injected=injected, injected_sha=injected_sha, skipped=tuple(skipped))
    save(repo, s)
    return s


def extend(repo, g, libs) -> Session:
    """Add libraries to the active session's instrumented set: inject their
    stanzas, record them so restore() covers them, widen `dirs` so the focus
    scope of profile_run/profile_top includes them. The focus itself is
    unchanged; scope becomes "custom"."""
    s = load(repo)
    if s is None:
        raise RuntimeError("no active profiling session; start one with mina-agent profile --focus <library>")
    new_libs = [l for l in libs if l not in s.libraries]
    if not new_libs:
        return s
    injected, injected_sha, skipped = _instrument(repo, g, new_libs)
    s = dataclasses.replace(
        s, scope="custom", libraries=(*s.libraries, *new_libs),
        dirs=(*s.dirs, *(g["libraries"][l]["dir"] for l in new_libs)),
        injected={**s.injected, **injected}, injected_sha={**s.injected_sha, **injected_sha},
        skipped=(*s.skipped, *skipped))
    save(repo, s)
    return s


def resume(repo, g) -> Session:
    """Re-create the last ended session: instrument the same libraries again
    and carry its recorded profiles forward (their files are still under
    state/profile), so profile_top / profile_diff keep working across the
    break. Refuses if a session is active or nothing was archived."""
    p = last_session_file(repo)
    if not p.exists():
        raise RuntimeError("no previous profiling session to continue")
    prev = Session.from_json(json.loads(p.read_text()))
    libs = [l for l in prev.libraries if l in g["libraries"]]
    s = start(repo, g, prev.focus, prev.scope, libs)
    kept = tuple(e for e in prev.profiles if os.path.exists(e.path))
    s = dataclasses.replace(s, profiles=kept)
    save(repo, s)
    for l in prev.linked:                  # re-link the implementations the session had chosen
        s, _ = link_impl(repo, g, l.impl, l.workload)
    return s


def restore(repo) -> RestoreReport:
    """Put every injected dune file back and end the session. Never touches
    .ml/.mli files, and never overwrites a dune file that was edited after
    injection (reported as `edited` instead)."""
    s = load(repo)
    if s is None:
        return RestoreReport(note="no active session")
    restored, already, edited, stanza_left, still_dirty = [], [], [], [], []
    for dune, b64 in s.injected.items():
        full = os.path.join(repo, dune)
        original = base64.b64decode(b64).decode()
        with open(full, encoding="utf-8") as fh:
            current = fh.read()
        if current == original:
            already.append(dune)    # put back by hand or by a checkout; nothing to do
            continue
        if _sha(current) != s.injected_sha.get(dune, _sha(current)):
            edited.append(dune)     # changed during the session; theirs to resolve
            if "backend landmarks" in current:
                stanza_left.append(dune)
            continue
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(original)
        restored.append(dune)
        if _git_dirty(repo, dune):
            still_dirty.append(dune)
    edits = sorted({l[3:] for d in s.dirs
                    for l in subprocess.run(["git", "-C", os.path.join(repo, d), "status", "--porcelain", "--", "."],
                                            capture_output=True, text=True).stdout.splitlines()
                    if l[3:].endswith((".ml", ".mli"))})
    windows_left = [f for f in edits if "[@landmark" in open(os.path.join(repo, f), encoding="utf-8").read()]
    write_json_atomic(last_session_file(repo), to_json(s))
    session_file(repo).unlink()
    return RestoreReport(restored=tuple(restored), already_restored=tuple(already), edited=tuple(edited),
                         stanza_left=tuple(stanza_left), still_dirty=tuple(still_dirty),
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
    taken = [int(m.group(1)) for p in state_dir().glob("*.json")
             if (m := re.match(r"(\d{3})-", p.name))]
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", spec)
    return state_dir() / f"{max(taken, default=0) + 1:03d}-{safe}.json"


def record_profile(repo, entry: ProfileEntry):
    s = load(repo)
    if s is not None:
        save(repo, dataclasses.replace(s, profiles=(*s.profiles, entry)))
