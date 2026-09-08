"""What a phase needs, and whether this machine can actually supply it.

`needs:` and `optional:` in a phase's front matter name *capabilities*, not
executables. Most are the same thing — `gh` on PATH is `gh` working — but
some tools are installed and still cannot do their job: samply is on PATH
and cannot sample a process unless the kernel permits it. Deciding that in
one place is what keeps `doctor`, the phase preflight, and the warnings in a
tool result from disagreeing about whether a machine has samply.

`needs:` is a hard requirement: the phase cannot start without it.
`optional:` degrades: the phase still runs, the person is asked first, and
the model is told in its prompt so it does not read a missing measurement as
a measurement of nothing.
"""
import shutil


def _samply():
    from . import perf
    return perf.samply_status()


# name -> () -> (path|None, detail). Anything not listed is "on PATH".
SPECIAL = {"samply": _samply}


def check(name: str, path: str | None = None) -> tuple[bool, str]:
    """(usable, detail) for one capability."""
    if fn := SPECIAL.get(name):
        found, detail = fn()
        return bool(found), detail
    p = shutil.which(name, path=path)
    return bool(p), p or "not on PATH"


def unmet(names, path: str | None = None) -> list[tuple[str, str]]:
    """[(name, why)] for the capabilities of `names` this machine lacks."""
    return [(n, why) for n in names for ok, why in (check(n, path),) if not ok]
