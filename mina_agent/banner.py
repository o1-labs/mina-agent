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


def render(env=None):
    art = UNICODE if _want_unicode() else ASCII
    lines = [art.rstrip("\n")]
    if env:
        lines.append(f"   {env.summary()}")
        lines += [f"   ! {w}" for w in env.warnings]
    return "\n".join(lines) + "\n"
