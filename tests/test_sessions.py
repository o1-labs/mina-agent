"""Session recording for --continue."""
from mina_agent import agent, paths, sessions


def test_record_and_last_by_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "HARNESS", tmp_path)
    monkeypatch.delenv(sessions.MODE_VAR, raising=False)
    sessions.record("ignored-no-mode", "startup")
    monkeypatch.setenv(sessions.MODE_VAR, "develop")
    sessions.record("dev-1", "startup")
    monkeypatch.setenv(sessions.MODE_VAR, "discuss")
    sessions.record("disc-1", "startup")
    monkeypatch.setenv(sessions.MODE_VAR, "develop")
    sessions.record("dev-2", "resume")
    assert sessions.last("develop") == "dev-2" and sessions.last("discuss") == "disc-1"
    assert sessions.last("profile") is None


def test_resume_argv_has_no_first_message():
    fresh = agent.interactive_argv("hello", "/tmp", develop=True)
    resumed = agent.interactive_argv("hello", "/tmp", develop=True, resume="abc-123")
    assert fresh[1] == "hello" and "--resume" not in fresh
    assert resumed[1:3] == ["--resume", "abc-123"] and "hello" not in resumed
    assert resumed[resumed.index("--permission-mode") + 1] == "acceptEdits"
