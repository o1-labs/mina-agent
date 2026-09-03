# describe-dune (vendored)

Source: https://github.com/o1-labs/describe-dune
Commit: see COMMIT. Unmodified copy of `src/describe_dune.ml` and LICENSE.

This is the same tool the repo's flake runs in `nix/ocaml.nix` to produce the
dune description that `dune-nix` consumes. `harness/derive.py` compiles it on
first use into `harness/bin/describe-dune` with the toolchain from
`harness/env.py`, using the one-line build from the upstream Makefile:

    ocamlfind ocamlopt -package cmdliner -package parsexp -package yojson \
        -package stdio -package base -linkpkg describe_dune.ml -o describe-dune

All five packages are already in the repo's opam switch (opam.export).

Known limitation: it does not honor `(dirs ...)` and recurses without a cycle
guard on `(include ...)`, so it must be run on a filtered tree containing only
dune / dune-project / *.inc / *.opam files, exactly as the flake does.
Running it on a raw checkout walks into opam_switches/ and segfaults on a
dune test fixture (`include-loop.t`).
