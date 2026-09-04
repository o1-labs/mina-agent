#!/usr/bin/env bash
# Install the harness into this machine and prepare the Mina checkout it
# lives in. Run from anywhere; idempotent.
#
#   git clone <harness repo> <mina>/harness && <mina>/harness/install.sh
#
# Needs: uv (brew install uv / nix profile install nixpkgs#uv), and the Mina
# toolchain reachable (the repo's opam switch; nix shells: see NIX.md).
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v uv >/dev/null 2>&1 || { echo "install.sh: uv is required (brew install uv, or nix profile install nixpkgs#uv)" >&2; exit 1; }
[ -f "$here/../dune-project" ] || { echo "install.sh: $here is not inside a Mina checkout (expected ../dune-project)" >&2; exit 1; }
uv tool install --force --editable "$here"
cd "$here/.."
mina-agent admin setup
mina-agent admin init
mina-agent doctor
