from __future__ import annotations

import json
from types import SimpleNamespace

from agent.agent_runtime_helpers import invoke_tool
from model_tools import handle_function_call


def test_handle_function_call_denies_tool_not_in_explicit_allowlist(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text("must not be read", encoding="utf-8")

    result = json.loads(
        handle_function_call(
            "read_file",
            {"path": str(target)},
            task_id="acl-deny-test",
            enabled_tools=[],
            skip_pre_tool_call_hook=True,
        )
    )

    assert result["error"].startswith("Tool 'read_file' denied by ACL")


def test_handle_function_call_denies_tool_missing_from_nonempty_allowlist(tmp_path):
    target = tmp_path / "secret.txt"
    target.write_text("must not be read", encoding="utf-8")

    result = json.loads(
        handle_function_call(
            "read_file",
            {"path": str(target)},
            task_id="acl-deny-test",
            enabled_tools=["terminal"],
            skip_pre_tool_call_hook=True,
        )
    )

    assert "read_file" in result["error"]
    assert "denied by ACL" in result["error"]


def test_agent_loop_tool_invocation_denies_agent_level_tool_not_in_acl_allowlist():
    agent = SimpleNamespace(valid_tool_names=set())

    result = json.loads(
        invoke_tool(
            agent,
            "todo",
            {"todos": []},
            effective_task_id="acl-deny-agent-level",
            pre_tool_block_checked=True,
        )
    )

    assert result["error"].startswith("Tool 'todo' denied by ACL")
