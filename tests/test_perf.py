"""perf parsers: GC counters, /usr/bin/time, samply symbol share."""
import gzip
import json

from mina_agent import perf

GC = "allocated_words: 16353748\nminor_words: 16346635\npromoted_words: 120315\nmajor_words: 127428\nheap_words: 188416\ntop_heap_words: 188416\n"
MAC = "        0.42 real         0.40 user         0.01 sys\n            12763136  maximum resident set size\n"
LINUX = "\tElapsed (wall clock) time (h:mm:ss or m:ss): 1:02.50\n\tMaximum resident set size (kbytes): 12464\n"


def test_gc_stats():
    g = perf.parse_gc_stats(GC)
    assert g and g.allocated_words == 16353748 and g.allocated_bytes == 16353748 * 8 and g.top_heap_words == 188416
    assert perf.parse_gc_stats("no stats here") is None


def test_time_formats():
    assert perf.parse_time(MAC) == (0.42, 12763136)
    assert perf.parse_time(LINUX) == (62.5, 12464 * 1024)


def test_symbol_share(tmp_path):
    # two frames in one lib: f at 0x100 (size 0x50), g at 0x200; stacks: [f], [f>g], [g]; samples 2,1,1
    prof = {"libs": [{"name": "x.exe"}], "threads": [{
        "resourceTable": {"lib": [0]},
        "funcTable": {"length": 2, "resource": [0, 0]},
        "frameTable": {"length": 2, "func": [0, 1], "address": [0x110, 0x205]},
        "stackTable": {"length": 3, "prefix": [None, 0, None], "frame": [0, 1, 1]},
        "samples": {"stack": [0, 0, 1, 2, None]}}]}
    syms = {"string_table": ["camlX__f_1", "camlX__g_2"], "data": [{"debug_name": "x.exe", "symbol_table": [
        {"rva": 0x100, "size": 0x50, "symbol": 0}, {"rva": 0x200, "size": 0x50, "symbol": 1}]}]}
    p = tmp_path / "p.json.gz"; s = tmp_path / "p.json.syms.json"
    with gzip.open(p, "wt") as fh:
        json.dump(prof, fh)
    s.write_text(json.dumps(syms))
    assert perf.symbol_share(str(p), str(s), "X__f") == (3, 4)     # f on stacks 0 and 1
    assert perf.symbol_share(str(p), str(s), "X__g") == (2, 4)
    assert perf.symbol_share(str(p), str(s), "nope") == (0, 4)
    sh = perf.sample_shares(str(p), str(s), "X__g")
    assert (sh.inclusive, sh.leaf, sh.root, sh.total) == (2, 2, 0, 4)   # no caml_start_program: 0% complete
    assert sh.ocaml_threads == 1 and sh.ocaml_cpu_share_pct == 100.0
    sh_f = perf.sample_shares(str(p), str(s), "X__f")
    assert (sh_f.inclusive, sh_f.leaf) == (3, 2)                        # f is the leaf on stack 0 only
    inclusive, warnings = perf.assess(sh_f)
    assert inclusive is None and "stacks incomplete" in warnings[0] and "leaf" in warnings[0]


def test_assess_trusts_complete_stacks():
    from mina_agent.model import SampleShares
    full = SampleShares(total=1000.0, inclusive=140.0, leaf=30.0, root=950.0)
    assert perf.assess(full) == (14.0, ())
    assert full.completeness_pct == 95.0 and full.leaf_pct == 3.0


def test_parse_env():
    assert perf.parse_env("A=1 B='two words' C=") == {"A": "1", "B": "two words", "C": ""}
    assert perf.parse_env("") == {}
    import pytest
    with pytest.raises(ValueError):
        perf.parse_env("not-a-pair")


def test_sample_shares_weights_cpu_and_skips_rust_threads(tmp_path):
    # thread A: OCaml (caml frames), 2 samples of CPU 10 and 30, symbol on the second (leaf)
    # thread B: Rust worker, 1 sample of CPU 1000, no caml frames -> excluded from the ocaml scope
    prof = {"libs": [{"name": "x.exe"}], "threads": [
        {"resourceTable": {"lib": [0]}, "funcTable": {"length": 2, "resource": [0, 0]},
         "frameTable": {"length": 2, "func": [0, 1], "address": [0x100, 0x200]},
         "stackTable": {"length": 2, "prefix": [None, 0], "frame": [0, 1]},
         "samples": {"length": 2, "stack": [0, 1], "threadCPUDelta": [10, 30]}},
        {"resourceTable": {"lib": [0]}, "funcTable": {"length": 1, "resource": [0]},
         "frameTable": {"length": 1, "func": [0], "address": [0x300]},
         "stackTable": {"length": 1, "prefix": [None], "frame": [0]},
         "samples": {"length": 1, "stack": [0], "threadCPUDelta": [1000]}}]}
    syms = {"string_table": ["caml_start_program", "camlX__f_1", "rust_mul"], "data": [{"debug_name": "x.exe", "symbol_table": [
        {"rva": 0x100, "size": 0x10, "symbol": 0}, {"rva": 0x200, "size": 0x10, "symbol": 1}, {"rva": 0x300, "size": 0x10, "symbol": 2}]}]}
    p = tmp_path / "p.json.gz"; s = tmp_path / "p.json.syms.json"
    with gzip.open(p, "wt") as fh:
        json.dump(prof, fh)
    s.write_text(json.dumps(syms))
    sh = perf.sample_shares(str(p), str(s), "X__f")
    assert (sh.total, sh.inclusive, sh.leaf, sh.root) == (40, 30, 30, 40) and sh.ocaml_threads == 1
    assert sh.leaf_pct == 75.0 and sh.completeness_pct == 100.0 and round(sh.ocaml_cpu_share_pct, 1) == 3.8
    everything = perf.sample_shares(str(p), str(s), "X__f", scope="all")
    assert everything.total == 1040 and everything.ocaml_threads == 2


# ---- samply availability ---------------------------------------------------

def test_samply_missing_is_reported_as_missing(monkeypatch):
    monkeypatch.setattr(perf.shutil, "which", lambda name: None)
    found, why = perf.samply_status()
    assert found is None and "cargo install samply" in why


def test_samply_is_unusable_when_the_kernel_forbids_sampling(monkeypatch):
    """Installed but gated: reported as missing, because it would otherwise
    record nothing and the caller would read that as "no samples"."""
    monkeypatch.setattr(perf.shutil, "which", lambda name: "/usr/bin/samply")
    monkeypatch.setattr(perf, "perf_event_paranoid", lambda: 4)
    found, why = perf.samply_status()
    assert found is None
    assert "perf_event_paranoid is 4" in why and "sysctl kernel.perf_event_paranoid=1" in why
    assert perf.tools_available()["samply"] is None


def test_samply_is_usable_at_or_below_the_threshold(monkeypatch):
    monkeypatch.setattr(perf.shutil, "which", lambda name: "/usr/bin/samply")
    for lvl in (perf.PARANOID_MAX, -1, None):     # None: macOS, no such knob
        monkeypatch.setattr(perf, "perf_event_paranoid", lambda lvl=lvl: lvl)
        assert perf.samply_status() == ("/usr/bin/samply", "/usr/bin/samply")


def test_perf_event_paranoid_reads_the_knob(tmp_path):
    knob = tmp_path / "perf_event_paranoid"
    knob.write_text("2\n")
    assert perf.perf_event_paranoid(str(knob)) == 2
    assert perf.perf_event_paranoid(str(tmp_path / "absent")) is None      # macOS
    knob.write_text("not a number\n")
    assert perf.perf_event_paranoid(str(knob)) is None


class _Ran:
    def __init__(self, stderr="", stdout="", returncode=1):
        self.stderr, self.stdout, self.returncode = stderr, stdout, returncode


def test_a_silent_samply_failure_still_says_something():
    assert "exit 1" in perf._samply_failed(_Ran())
    assert "permission denied" in perf._samply_failed(_Ran(stderr="oh no\npermission denied\n"))
