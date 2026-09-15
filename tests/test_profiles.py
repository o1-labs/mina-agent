"""Mina's build profiles: the only way a session reaches a non-dev profile."""
import pytest

from mina_agent import agent, env as envmod

CFG = agent.profiles_config()


def test_named_profile_sets_every_variable():
    assert envmod.profile_env("devnet", CFG) == {"DUNE_PROFILE": "devnet", "MINA_PROFILE": "devnet"}
    assert envmod.profile_env("mainnet", CFG)["MINA_PROFILE"] == "mainnet"


def test_empty_profile_inherits_the_shell():
    assert envmod.profile_env("", CFG) == {}


def test_unknown_profile_names_the_known_ones():
    with pytest.raises(ValueError, match="devnet"):
        envmod.profile_env("testnet", CFG)


def test_manifest_profiles_match_node_config():
    # src/lib/node_config/profiled/node_config_profiled.ml matches exactly these
    # four and defaults to dev when MINA_PROFILE is unset.
    assert CFG["names"] == ["dev", "devnet", "lightnet", "mainnet"]
    assert CFG["default"] in CFG["names"]
    assert CFG["vars"] == ["DUNE_PROFILE", "MINA_PROFILE"]


def test_profile_dependent_test_is_declared():
    t = next(t for t in agent.manifest()["tests"] if t["name"] == "profile_dependent")
    # the three directories buildkite/scripts/profile-dependent-tests.sh runs
    assert t["command"][:3] == ["dune", "runtest", "-f"]
    assert t["command"][3:] == ["src/lib/blockchain_snark/tests",
                                "src/lib/transaction_snark/test/constraint_count",
                                "src/lib/transaction_snark/test/print_transaction_snark_vk"]


def test_overridden_restores_what_it_replaced():
    e = envmod.Env.__new__(envmod.Env)
    e._activated_env = {"DUNE_PROFILE": "dev", "PATH": "/bin"}
    with e.overridden(envmod.profile_env("devnet", CFG)):
        assert e._activated_env["DUNE_PROFILE"] == "devnet"
        assert e._activated_env["MINA_PROFILE"] == "devnet"
    assert e._activated_env == {"DUNE_PROFILE": "dev", "PATH": "/bin"}
