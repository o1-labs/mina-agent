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
import datetime as dt
import json
import os
import re
import subprocess

from . import landmarks, paths

DUNE_SUBCOMMANDS = ("build", "exec", "runtest", "test")


def state_dir(repo):
    p = paths.state_dir(repo) / "profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def session_file(repo):
    return state_dir(repo) / "session.json"


def load(repo):
    p = session_file(repo)
    if not p.exists():
        return None
    with open(p) as fh:
        return json.load(fh)


def save(repo, s):
    with open(session_file(repo), "w") as fh:
        json.dump(s, fh, indent=1)


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


def start(repo, g, focus, scope, libs):
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
    s = {"started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
         "focus": focus, "scope": scope, "libraries": libs,
         "dirs": [g["libraries"][l]["dir"] for l in libs],
         "injected": {dune: base64.b64encode(text.encode()).decode() for dune, (text, _) in plan.items()},
         "skipped": skipped, "profiles": []}
    save(repo, s)
    return s


def restore(repo):
    """Put every injected dune file back and end the session. Returns a
    report; never touches .ml/.mli files."""
    s = load(repo)
    if s is None:
        return {"restored": [], "still_dirty": [], "source_edits": [], "note": "no active session"}
    restored, still_dirty = [], []
    for dune, b64 in s["injected"].items():
        with open(os.path.join(repo, dune), "w", encoding="utf-8") as fh:
            fh.write(base64.b64decode(b64).decode())
        restored.append(dune)
        if _git_dirty(repo, dune):
            still_dirty.append(dune)
    edits = []
    for d in s["dirs"]:
        r = subprocess.run(["git", "-C", os.path.join(repo, d), "status", "--porcelain", "--", "."],
                           capture_output=True, text=True)
        edits += [l[3:] for l in r.stdout.splitlines() if l[3:].endswith((".ml", ".mli"))]
    session_file(repo).unlink()
    return {"restored": restored, "still_dirty": still_dirty, "source_edits": sorted(set(edits)),
            "profiles": s["profiles"]}


# --------------------------------------------------------------------------
# workloads
# --------------------------------------------------------------------------

def resolve_workload(g, manifest_tests, spec):
    """A workload spec -> [(build_target, exe_relpath, argv_tail, cwd_rel)].

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
        return [(exe, exe, ["inline-test-runner", lib], d)]
    if spec.startswith("test:"):
        key = spec[5:]
        rec = g["tests"].get(key)
        if not rec:
            raise ValueError(f"unknown test unit {key!r}")
        exe = f"{rec['dir']}/{rec['name']}.exe"
        return [(exe, exe, [], rec["dir"])]
    if spec.startswith("exe:"):
        exe = spec[4:]
        return [(exe, exe, [], os.path.dirname(exe))]
    t = manifest_tests.get(spec)
    if not t:
        raise ValueError(f"unknown workload {spec!r}: use inline:<lib>, test:<dir>/<name>, exe:<path.exe>, "
                         "or a manifest test name")
    cmd = t["command"]
    if cmd[:2] == ["dune", "exec"] and cmd[2].endswith(".exe"):
        return [(cmd[2], cmd[2], [], os.path.dirname(cmd[2]))]
    alias = next((a for a in cmd if a.startswith("@") and a.endswith("/runtest")), None)
    if alias:
        d = alias[1:-len("/runtest")]
        units = [(k, r) for k, r in g["tests"].items() if r["dir"] == d]
        if units:
            return [(f"{d}/{r['name']}.exe", f"{d}/{r['name']}.exe", [], d) for _, r in units]
        libs = [k for k, r in g["libraries"].items() if r["dir"] == d and r["has_inline_tests"]]
        if libs:
            return resolve_workload(g, manifest_tests, f"inline:{libs[0]}")
    raise ValueError(f"manifest test {spec!r} runs {' '.join(cmd)}, which has no executable the "
                     "profiler can run directly; use test:<dir>/<name> or exe:<path.exe>")


def next_profile_path(repo, spec):
    s = load(repo)
    n = len(s["profiles"]) + 1 if s else 1
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", spec)
    return state_dir(repo) / f"{n:03d}-{safe}.json"


def record_profile(repo, entry):
    s = load(repo)
    if s is not None:
        s["profiles"].append(entry)
        save(repo, s)
