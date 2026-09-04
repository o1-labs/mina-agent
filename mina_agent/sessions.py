"""Which Claude sessions the harness started, so `--continue` resumes the
right one. The SessionStart hook appends a line per session with the mode
the launcher put in the environment (MINA_AGENT_SESSION_MODE); Claude's own
--continue would pick the latest conversation in the directory whatever
started it."""
import datetime as dt
import json
import os

from . import paths

MODE_VAR = "MINA_AGENT_SESSION_MODE"


def log_path():
    return paths.state_dir() / "sessions.jsonl"


def record(session_id: str | None, source: str | None) -> None:
    """Called from the SessionStart hook; a no-op outside harness sessions."""
    mode = os.environ.get(MODE_VAR)
    if not mode or not session_id:
        return
    rec = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "mode": mode,
           "session_id": session_id, "source": source}
    with open(log_path(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def last(mode: str) -> str | None:
    """The most recently recorded session id for a mode."""
    p = log_path()
    if not p.exists():
        return None
    for line in reversed(p.read_text().splitlines()):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("mode") == mode:
            return rec.get("session_id")
    return None
