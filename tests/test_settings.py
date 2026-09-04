"""The Bash wall: what the session settings tell Claude Code to deny, and
that the explanatory PreToolUse hooks match the deny list exactly. Pure:
no toolchain, no repo state."""
import re

from mina_agent import agent

# Command heads the harness never runs. Each must be denied in both the
# bare and the `head *` form, since a prefix rule does not match the bare word.
HEADS = {
    "dune", "opam", "nix", "nix-build", "nix-env", "nix-shell", "cargo", "rustup", "make",
    "ocamlfind", "ocamlopt", "ocamlc", "pip", "pip3",
    "./scripts/update-opam-switch.sh", "scripts/update-opam-switch.sh",
    "./scripts/pin-external-packages.sh",
}


def _head(rule):
    return re.fullmatch(r"Bash\((.+?)(?: \*)?\)", rule).group(1)


def _bash_if_rules(settings):
    return [h["if"] for m in settings["hooks"]["PreToolUse"] if m["matcher"] == "Bash"
            for h in m["hooks"] if "if" in h]


def test_every_head_denied_in_both_forms():
    rules = set(agent.bash_deny_rules())
    for h in HEADS:
        assert f"Bash({h} *)" in rules, h
        assert f"Bash({h})" in rules, h


def test_no_deny_rule_outside_the_head_list():
    assert {_head(r) for r in agent.bash_deny_rules()} == HEADS


def test_context_hooks_mirror_deny_list():
    settings = agent.session_settings()
    assert _bash_if_rules(settings) == agent.bash_deny_rules()


def test_context_hooks_never_block():
    for m in agent.session_settings()["hooks"]["PreToolUse"]:
        for h in m["hooks"]:
            assert h["command"].endswith("hook pre-bash"), h


def test_hook_commands_are_absolute():
    for matchers in agent.session_settings()["hooks"].values():
        for m in matchers:
            for h in m["hooks"]:
                assert h["command"].startswith('"/'), h["command"]


def test_template_carries_no_hand_written_bash_hooks():
    assert "PreToolUse" not in agent._template()["hooks"]
