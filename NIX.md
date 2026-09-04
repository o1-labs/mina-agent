# Nix support: handoff

The harness detects an entered `nix develop` shell (`IN_NIX_SHELL` set and
`dune` resolving into `/nix/store`) and reports `mode nix` in `status` and
`doctor`, but every command refuses to run in it (`Env.usable` is opam-only)
until the items below are done and verified on a nix machine. Nothing else
in the harness distinguishes the modes: once activated, all dune, merlin,
lint and LSP calls run on the inherited environment, and in a nix shell the
harness already inherits it as is (`Env.activate` copies `os.environ` when
`activated` is true).

## Required

1. **landmarks sources without opam.** `landmarks.fetch` runs `opam source
   landmarks.1.5 landmarks-ppx.1.5`; there is no opam in the flake shell.
   Replace it with a direct download of the release tarball, verified by a
   pinned sha256, the way `dhall.py` fetches its binary. Keep the
   `(lang dune 3.3)` patch and the layout under `state/landmarks`.

2. **LSP resolution text.** `lsp.resolve` finds `ocamllsp` on the shell's
   PATH already (the flake devShell provides `ocaml-lsp-server`), but labels
   it "PATH (project switch)" and, when missing, gives opam advice. Branch
   on `env.mode`: label "PATH (nix shell)"; the not-found hint says the
   flake devShell provides it, re-enter `nix develop`.

3. **Flip the switch.** `Env.usable` in `env.py` returns `mode is Mode.OPAM`;
   make it `mode is not Mode.NONE` and delete `NIX_UNSUPPORTED`. Update the
   test in `tests/test_env.py` that pins the refusal.

## Verify on a nix machine

- `mina-agent status` shows `mode nix (activated)` with the shell's dune and
  ocaml versions.
- `mina-agent admin setup` builds describe-dune and usages with the shell's
  ocamlfind, fetches landmarks, derives the graph.
- `mina-agent doctor` is all ok/note; the `lsp plugin` row resolves to the
  flake's ocamllsp.
- `mina-agent discuss`, then `check` on a file and `usages` on a binding.
- `mina-agent profile --focus currency --headless --max-turns 5`: the
  instrumented build works and a profile is recorded.

## Not needed

- `_nix_activate` (entering a shell from outside) stays a stub: the
  supported case is a shell the user has already entered.
- `uv` is a machine prerequisite in both modes; the flake shell does not
  provide it (`nix profile install nixpkgs#uv`).
