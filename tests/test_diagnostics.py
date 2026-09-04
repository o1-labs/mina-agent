"""parse_dune_errors over the location-header shapes ocamlc 4.14 emits."""
from mina_agent.tools import parse_dune_errors

SINGLE = '''File "a.ml", line 1, characters 14-26:
1 | let x : int = "not an int"
                  ^^^^^^^^^^^^
Error: This expression has type string but an expression was expected of type
         int
'''
MULTI = '''File "a.ml", lines 2-3, characters 2-7:
2 | ..let y : int =
3 |   "a" ^ "b"
Error: This expression has type string but an expression was expected of type
         int
'''
NO_COLS = '''File "src/lib/foo/dune", line 4:
Error: Library "nope" not found.
'''
WARNING = '''File "b.ml", line 7, characters 2-5:
Warning 26 [unused-var]: unused variable baz.
'''


def test_single_line_header():
    (e,) = parse_dune_errors(SINGLE)
    assert (e["file"], e["line"], e["line_end"], e["col_start"], e["col_end"]) == ("a.ml", 1, None, 14, 26)
    assert e["severity"] == "error" and "expected of type int" in e["message"]


def test_multi_line_header():
    (e,) = parse_dune_errors(MULTI)
    assert (e["line"], e["line_end"], e["col_start"], e["col_end"]) == (2, 3, 2, 7)
    assert e["severity"] == "error"


def test_header_without_columns():
    (e,) = parse_dune_errors(NO_COLS)
    assert (e["file"], e["line"], e["col_start"]) == ("src/lib/foo/dune", 4, None)


def test_back_to_back_diagnostics_keep_their_own_locations():
    errs = parse_dune_errors(SINGLE + MULTI + WARNING)
    assert [(e["line"], e["severity"]) for e in errs] == [(1, "error"), (2, "error"), (7, "warning")]
    assert "unused variable" in errs[2]["message"]
