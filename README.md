# mina-agent

A structural harness for working on the Mina monorepo with Claude Code. It
makes correct behaviour a property of the tools rather than of prose: builds,
type-checks, and tests go through one MCP server that runs dune in the right
switch; the dependency graph is derived from the dune files, never written by
hand; build configuration and the Rust proof-systems boundary cannot be
edited; raw dune, opam, nix, cargo, and make cannot be run. Headless phases
and interactive sessions both start from that same set of walls and facts.

    uv tool install --editable ./harness
    mina-agent setup && mina-agent init && mina-agent doctor
    mina-agent --help

Every command answers `--help`. Listings of tools, tests, and phases are
derived, not documented: `mina-agent list tools|tests|phases`.

The one thing `--help` cannot tell you: the edit deny rules cover Claude's
file tools and the shell commands Claude Code recognises, not a script the
model writes that opens a file itself. Closing that needs the sandbox, which
this harness does not configure.
