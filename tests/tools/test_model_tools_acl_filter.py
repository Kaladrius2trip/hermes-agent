from __future__ import annotations

import json

from model_tools import get_tool_definitions


def _tool_names(tool_defs):
    return {tool["function"]["name"] for tool in tool_defs}


def _tool_schema(tool_defs, name):
    for tool in tool_defs:
        if tool["function"]["name"] == name:
            return tool["function"]
    raise AssertionError(f"missing tool schema: {name}")


def test_get_tool_definitions_filters_by_concrete_acl_allowlist():
    tool_defs = get_tool_definitions(
        enabled_toolsets=["file"],
        allowed_tool_names={"read_file"},
        quiet_mode=True,
    )

    assert _tool_names(tool_defs) == {"read_file"}


def test_get_tool_definitions_accepts_one_shot_acl_allowlist_iterable():
    allowed = (name for name in ["read_file"])

    tool_defs = get_tool_definitions(
        enabled_toolsets=["file"],
        allowed_tool_names=allowed,
        quiet_mode=True,
    )

    assert _tool_names(tool_defs) == {"read_file"}


def test_execute_code_schema_lists_only_acl_allowed_nested_tools():
    tool_defs = get_tool_definitions(
        enabled_toolsets=["code_execution", "file", "terminal"],
        allowed_tool_names={"execute_code", "read_file"},
        quiet_mode=True,
    )

    assert _tool_names(tool_defs) == {"execute_code", "read_file"}
    execute_schema = json.dumps(_tool_schema(tool_defs, "execute_code"), sort_keys=True)
    assert "read_file" in execute_schema
    assert "write_file" not in execute_schema
    assert "terminal" not in execute_schema
