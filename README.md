# mina-agent

A structural harness for working on the Mina monorepo with Claude Code. It
makes correct behaviour a property of the tools rather than of prose: builds,
type-checks, and tests go through one MCP server that runs dune in the right
switch; the dependency graph is derived from the dune files, never written by
hand; build configuration and the Rust proof-systems boundary cannot be
edited; raw dune, opam, nix, cargo, and make cannot be run. Headless phases
and interactive sessions both start from that same set of walls and facts,
and only when invoked through `mina-agent`: nothing is written into the
repo's Claude settings, so plain `claude` sessions are unaffected.

    uv tool install ./harness            # --editable while developing the harness itself
    mina-agent setup && mina-agent init && mina-agent doctor
    mina-agent --help

The tool lives in uv's tool directory, not in the checkout, so it survives
checking out branches that predate `harness/` (which `review --checkout`
does). Reinstall after changing the harness source.

Every command answers `--help`. Listings of tools, tests, and phases are
derived, not documented: `mina-agent list tools|tests|phases`.

The one thing `--help` cannot tell you: the edit deny rules cover Claude's
file tools and the shell commands Claude Code recognises, not a script the
model writes that opens a file itself. Closing that needs the sandbox, which
this harness does not configure.

Known limitation: the harness serializes the dune calls it makes, but not
a `dune build -w` you run yourself; on the pinned dune (3.3.1) two dune
processes on one `_build` can corrupt each other's temporaries, and the
native fix (`dune rpc build` into the watcher) is experimental there. Stop
the watcher while the agent builds, until the dune pin moves.
