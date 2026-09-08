# Nix support

The harness runs against either toolchain. In nix mode it does not build or
enter anything: you enter the flake's devShell yourself and the harness
inherits that environment as is.

    cd <mina>
    nix develop .#with-lsp          # or `direnv allow`, where .envrc says
                                    # `use flake mina#with-lsp`
    mina-agent status               # mode nix (activated)

`with-lsp` is the shell to use: it is the dev shell plus
`ocaml-lsp-server`. The plain `default` shell builds and type-checks
everything, but `doctor` fails on the missing ocamllsp and sessions run
without Claude Code's LSP tool, falling back to merlin for `type_at` and
`definition`.

`uv` is a machine prerequisite in both modes and the flake does not provide
it: `nix profile install nixpkgs#uv`.

## How the mode is decided

`env.detect()` calls it nix when `IN_NIX_SHELL` is set *and* `dune` resolves
into `/nix/store`; opam when `dune` resolves under the repo-local `_opam`.
`HARNESS_MODE=opam|nix` overrides the detection. Once the mode is nix,
`Env.activate()` copies `os.environ` and every dune, merlin, lint and LSP
call runs on it — there is no second environment to compute, and nothing
mutates the store.

Entering a shell from outside is *not* implemented: `_nix_activate` is a
stub, and `Env.usable` is false for a nix mode that is not activated (so
`HARNESS_MODE=nix` outside a shell is refused with a reason rather than
failing later). The supported case is a shell you have already entered.

## What differs from opam mode

Three places, and nothing else:

- **landmarks sources.** `landmarks.fetch` downloads the pinned upstream
  archive over HTTPS and verifies its sha256, rather than shelling out to
  `opam source`. It is the same URL and the same bytes opam-repository pins
  for `landmarks.1.5` and `landmarks-ppx.1.5` (one archive carries both
  packages), so opam mode gets exactly what it got before; nix mode no
  longer needs an opam that is not there. The `(lang dune 3.3)` patch and
  the layout under `state/landmarks` are unchanged.

- **LSP resolution.** The devShell already puts `ocamllsp` on PATH, so
  `lsp.resolve` finds it there and labels it `PATH (nix shell)` rather than
  `PATH (project switch)`. That label is now the only thing about ocamllsp
  that differs between the modes: resolution is the override or PATH in
  both, and a miss fails loudly in both, since the harness neither installs
  ocamllsp nor assumes how you would.

- **`doctor`'s opam.export check.** There is no project switch to compare
  against, and the flake builds its own package set from `opam.export`, so
  the check reports a note instead of running `check_opam_switch` out of a
  `_opam` that does not exist.

## Verified

On x86_64 Linux, in `nix develop .#with-lsp` (dune 3.3.1, ocaml 4.14.2):

- `mina-agent status` — `mode nix (activated)`, with the shell's versions.
- `mina-agent admin setup` — describe-dune and usages built with the shell's
  ocamlfind, dhall 1.30.0 fetched, landmarks 1.5 fetched and patched, graph
  derived (378 libraries, 55 executables, 100 tests).
- `mina-agent admin init`, `mina-agent doctor` — all ok/note, exit 0; the
  `lsp plugin` row resolves to the flake's ocamllsp.
- `check` on `src/lib/hex/hex.ml`, `usages` on a binding in
  `src/lib/crypto/bowe_gabizon_hash` — both resolve.
- An instrumented profiling run on `currency` (`inline:currency`): the
  landmarks build works, the workload runs, and a profile is recorded
  (25 functions, 99.9% of self time in the focus library).

`_build` is shared between modes and the compilers differ, so switching
modes forces a full rebuild. `status` and `doctor` read `_build/log` and say
so (`_build provenance`) rather than letting you discover it halfway
through.
