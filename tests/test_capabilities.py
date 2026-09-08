"""Capabilities: what a phase needs, and whether the machine supplies it."""
import pathlib
import re

import pytest

from mina_agent import capabilities as C


def test_a_plain_capability_is_just_path(monkeypatch):
    monkeypatch.setattr(C.shutil, "which", lambda name, path=None: "/usr/bin/gh" if name == "gh" else None)
    assert C.check("gh") == (True, "/usr/bin/gh")
    assert C.check("nope") == (False, "not on PATH")


def test_samply_is_not_merely_on_path(monkeypatch):
    """The point of the indirection: samply can be installed and unusable,
    and a PATH lookup would call that fine."""
    monkeypatch.setattr(C.shutil, "which", lambda name, path=None: "/usr/bin/samply")
    from mina_agent import perf
    monkeypatch.setattr(perf, "perf_event_paranoid", lambda: 4)
    ok, why = C.check("samply")
    assert not ok and "perf_event_paranoid" in why


def test_unmet_reports_only_what_is_missing(monkeypatch):
    monkeypatch.setattr(C.shutil, "which", lambda name, path=None: None if name == "absent" else "/usr/bin/" + name)
    assert [n for n, _ in C.unmet(["gh", "absent", "git"])] == ["absent"]
    assert C.unmet([]) == []


# ---- the harness advises nothing ------------------------------------------

# Pre-existing advice, left alone by the change that removed the rest: these
# are about the project's own OCaml toolchain rather than a machine tool, and
# whether they stay is the repository author's call. Delete an entry here to
# tighten the rule over that file.
ADVICE_ALLOWED = {"lsp.py", "dhall.py"}

INSTALL_ADVICE = re.compile(
    r"brew install|nix profile install|nix-env|cargo install|apt(-get)? install"
    r"|pip install|port install|softwareupdate")


def test_the_harness_never_tells_you_how_to_install_anything():
    """It reports what is missing and stops there. Scanned over the whole
    package rather than the handful of messages that once carried advice, so
    a new one cannot quietly appear."""
    pkg = pathlib.Path(C.__file__).parent
    offenders = [f"{f.relative_to(pkg.parent)}:{i + 1}: {line.strip()}"
                 for f in sorted(pkg.rglob("*.py")) if f.name not in ADVICE_ALLOWED
                 for i, line in enumerate(f.read_text().splitlines())
                 if INSTALL_ADVICE.search(line)]
    assert offenders == [], "install advice in harness output:\n" + "\n".join(offenders)


def test_a_missing_tool_still_says_what_it_costs_you():
    """Reporting absence is not the same as saying nothing: the consequence
    is the part the reader cannot work out alone."""
    from mina_agent import perf
    import unittest.mock as m
    with m.patch.object(perf.shutil, "which", lambda name: None):
        found, why = perf.samply_status()
    assert found is None
    assert "not installed" in why and "not sample shares" in why
    assert not INSTALL_ADVICE.search(why)
