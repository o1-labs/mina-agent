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
