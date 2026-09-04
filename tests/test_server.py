"""Anticipated tool failures reach the model as ToolError text."""
import inspect

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from mina_agent.server import anticipated


def tool(path: str, timeout_s: int = 60) -> dict:
    """doc"""
    raise ValueError(f"unknown library {path!r}")


def test_value_error_becomes_tool_error_with_message():
    with pytest.raises(ToolError, match="unknown library 'x'"):
        anticipated(tool)("x")


def test_signature_and_doc_survive_for_mcp_introspection():
    w = anticipated(tool)
    assert inspect.signature(w) == inspect.signature(tool)
    assert w.__doc__ == "doc" and w.__name__ == "tool"


def test_other_exceptions_pass_through():
    def crash():
        raise KeyError("k")
    with pytest.raises(KeyError):
        anticipated(crash)()
