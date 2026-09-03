#!/usr/bin/env python3
"""Welcome banner for the Mina agent harness.

render(status=None) returns the art, plus one status line when given the
env status dict. HARNESS_ASCII=1 forces the 7-bit fallback.
"""
import json
import os
import sys

UNICODE = r"""
   ███╗   ███╗ ██╗ ███╗   ██╗  █████╗
   ████╗ ████║ ██║ ████╗  ██║ ██╔══██╗      h a r n e s s
   ██╔████╔██║ ██║ ██╔██╗ ██║ ███████║
   ██║╚██╔╝██║ ██║ ██║╚██╗██║ ██╔══██║      ⟨⟨⟨ 22 kB ⟩⟩⟩
   ██║ ╚═╝ ██║ ██║ ██║ ╚████║ ██║  ██║
   ╚═╝     ╚═╝ ╚═╝ ╚═╝  ╚═══╝ ╚═╝  ╚═╝      proofs all the way down
   ────────────────────────────────────────────────────────────────
"""

ASCII = r"""
   ##     ## #### ##    ##    ###
   ###   ###  ##  ###   ##   ## ##          h a r n e s s
   #### ####  ##  ####  ##  ##   ##
   ## ### ##  ##  ## ## ##  #######         <<< 22 kB >>>
   ##     ##  ##  ##  ####  ##   ##
   ##     ## #### ##    ##  ##   ##         proofs all the way down
   ----------------------------------------------------------------
"""


def _want_unicode():
    if os.environ.get("HARNESS_ASCII"):
        return False
    enc = (getattr(sys.stdout, "encoding", None) or "").lower()
    return "utf" in enc


def render(status=None):
    art = UNICODE if _want_unicode() else ASCII
    lines = [art.rstrip("\n")]
    if status:
        mode = status.get("mode", "?")
        act = "activated" if status.get("activated") else "not activated"
        dune = status.get("dune_version") or "?"
        ocaml = status.get("ocaml") or "?"
        lines.append(f"   mode {mode} ({act})   dune {dune}   ocaml {ocaml}")
        for w in status.get("warnings", []):
            lines.append(f"   ! {w}")
    return "\n".join(lines) + "\n"
