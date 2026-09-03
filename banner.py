#!/usr/bin/env python3
"""Welcome banner for the Mina agent harness.

Usage:
    python3 harness/banner.py                # banner only
    python3 harness/banner.py --status JSON  # banner + one status line
    HARNESS_ASCII=1 python3 harness/banner.py  # 7-bit ASCII fallback

Stdlib only. Printing is the only side effect.
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


def main(argv):
    status = None
    if len(argv) >= 2 and argv[0] == "--status":
        status = json.loads(argv[1])
    sys.stdout.write(render(status))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
