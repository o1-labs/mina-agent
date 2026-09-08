"""Capabilities: what a phase needs, and whether the machine supplies it."""
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
