"""The development session's shell allowlist."""
from mina_agent import agent, guard

CFG = agent.develop_config()
H, S = CFG["bash_heads"], CFG["mina_agent_subcommands"]


def ok(cmd):
    return guard.decide(cmd, H, S).allowed


def test_allowed_developer_commands():
    for cmd in ("git status --short", "git log --oneline -5 && git diff --stat", "gh pr view 12 --json body",
                "mina-agent lint --fix", "cd src/lib/hex; ls -la", "GIT_PAGER=cat git show HEAD",
                "rg 'let hash' src/lib | head -20", "git commit -m 'fix: handle empty'", 'echo "a > b"',
                "gh api repos/o/r/pulls/1/comments --jq '.[] | {id, body}'", 'rg -n "a|b" src/lib',
                "rg -n foo src 2>&1", "git fetch >/dev/null 2>&1", "cat f 2> /dev/null", "git status </dev/null",
                "echo 'x; y && z' | head -1", "cargo test -p kimchi-stubs", "cargo fmt --check"):
        assert ok(cmd), cmd


def test_segments_respect_quotes():
    assert guard.segments("a '|' b | c") == ["a '|' b ", " c"]
    assert guard.segments('a "x && y" && b; c || d\ne') == ['a "x && y" ', " b", " c ", " d", "e"]
    assert guard.segments("a 2>&1") == ["a 2>&1"]
    assert guard.segments("a \\| b") == ["a \\| b"]


def test_denied_toolchain_and_escapes():
    for cmd in ("dune build", "make build", "opam install x", "bash scripts/testone.sh a.ml", "python3 -c 'print(1)'",
                "git log $(dune --version)", "echo `dune --version`", "git diff > /tmp/x", "cat < /etc/passwd",
                "git diff >> notes.txt", "git log 2> err.txt", "echo 'a' | dune build", "git status || dune build",
                "mina-agent exec -- dune build", "mina-agent clean -y", "ls | xargs rm", "env dune --version",
                "sh -c 'dune build'", "./scripts/probe.sh", "/opt/homebrew/bin/dune build"):
        assert not ok(cmd), cmd


def test_reasons_name_the_cause():
    assert "allowlist" in guard.decide("dune build", H, S).reason
    assert "redirection" in guard.decide("git diff > f", H, S).reason
    assert "mina-agent exec" in guard.decide("mina-agent exec -- dune", H, S).reason


def test_checked_in_scripts_and_interpreters_stay_denied():
    # the session prompt says so; issue #8 was the prompt saying the opposite
    for cmd in ("./buildkite/scripts/profile-dependent-tests.sh devnet",
                "buildkite/scripts/profile-dependent-tests.sh devnet",
                "bash buildkite/scripts/profile-dependent-tests.sh devnet",
                "MINA_PROFILE=devnet dune build --help",
                "DUNE_PROFILE=devnet ./scripts/testone.sh a.ml"):
        assert not ok(cmd), cmd


def test_develop_settings_and_argv():
    s = agent.session_settings(develop=True)
    hooks = [h["command"] for m in s["hooks"]["PreToolUse"] for h in m["hooks"]]
    assert any(h.endswith("hook bash-allowlist") for h in hooks)
    # the session-start facts must describe the allowlist, not the deny rules
    start = [h["command"] for m in s["hooks"]["SessionStart"] for h in m["hooks"]]
    assert all(h.endswith("hook session-start --develop") for h in start), start
    plain = [h["command"] for m in agent.session_settings()["hooks"]["SessionStart"] for h in m["hooks"]]
    assert all(h.endswith("hook session-start") for h in plain), plain
    assert "Bash(git commit *)" in s["permissions"]["allow"] and "WebFetch" in s["permissions"]["deny"]
    assert "mcp__mina-harness" in s["permissions"]["allow"]
    assert "Bash(dune *)" in s["permissions"]["deny"]
    assert "WebFetch" not in agent.session_settings()["permissions"]["deny"]
    argv = agent.interactive_argv("hi", "/tmp", develop=True)
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert "--permission-mode" not in agent.interactive_argv("hi", "/tmp")
