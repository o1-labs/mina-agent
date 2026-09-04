"""Trajectory.feed over the .jsonl record shapes, including the string-content
user message the CLI emits at auto-compaction."""
from mina_agent import trajectory
from mina_agent.trajectory import Trajectory

TOOL_USE = {"kind": "AssistantMessage", "content": [
    {"block": "ToolUseBlock", "id": "t1", "name": "mcp__mina-harness__check", "input": {"path": "a.ml"}}]}
TOOL_RESULT = {"kind": "UserMessage", "content": [
    {"block": "ToolResultBlock", "tool_use_id": "t1", "content": '{"ok": true}', "is_error": False}]}
COMPACTION = {"kind": "UserMessage",
              "content": "This session is being continued from a previous conversation..."}


def test_string_content_user_message_is_ignored():
    t = Trajectory()
    for rec in (TOOL_USE, COMPACTION, TOOL_RESULT):
        t.feed(rec)
    assert t.calls[0]["result"] == '{"ok": true}'


def test_blocks_of_non_list_content_is_empty():
    assert trajectory.blocks(COMPACTION) == []
    assert trajectory.blocks({"kind": "UserMessage"}) == []
    assert trajectory.blocks(TOOL_RESULT) == TOOL_RESULT["content"]
