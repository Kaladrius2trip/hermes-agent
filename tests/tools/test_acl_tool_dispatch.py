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


def test_handle_function_call_independent_raw_acl_denies_out_of_acl_tool(tmp_path):
    missing = tmp_path / "nope.txt"

    result = json.loads(
        handle_function_call(
            "read_file",
            {"path": str(missing)},
            task_id="gateb-deny",
            allowed_tool_names=["web_search"],
            enabled_tools=None,
            skip_pre_tool_call_hook=True,
        )
    )

    assert "read_file" in result["error"]
    assert "denied by ACL" in result["error"]


def test_handle_function_call_raw_acl_allows_granted_tool(tmp_path):
    missing = tmp_path / "nope.txt"

    result = json.loads(
        handle_function_call(
            "read_file",
            {"path": str(missing)},
            task_id="gateb-allow",
            allowed_tool_names=["read_file"],
            enabled_tools=None,
            skip_pre_tool_call_hook=True,
        )
    )

    assert "denied by ACL" not in result.get("error", "")


def test_handle_function_call_raw_acl_exempts_tool_search_bridge():
    result = json.loads(
        handle_function_call(
            "tool_search",
            {"query": "x"},
            task_id="gateb-bridge",
            allowed_tool_names=["web_search"],
            enabled_tools=None,
            skip_pre_tool_call_hook=True,
        )
    )

    assert "denied by ACL" not in result.get("error", "")


def test_invoke_tool_raw_acl_denies_inline_tool_on_valid_names_drift():
    agent = SimpleNamespace(
        valid_tool_names={"session_search"},
        allowed_tool_names=[],
        _memory_manager=None,
    )

    result = json.loads(
        invoke_tool(
            agent,
            "session_search",
            {"query": "x"},
            effective_task_id="gateb-inline-drift",
            pre_tool_block_checked=True,
        )
    )

    assert "session_search" in result["error"]
    assert "denied by ACL" in result["error"]


def test_invoke_tool_raw_acl_allows_inline_tool_when_granted():
    agent = SimpleNamespace(
        valid_tool_names={"todo"},
        allowed_tool_names=["todo"],
        _todo_store=None,
        _memory_manager=None,
    )

    result = json.loads(
        invoke_tool(
            agent,
            "todo",
            {"todos": []},
            effective_task_id="gateb-inline-allow",
            pre_tool_block_checked=True,
        )
    )

    assert "denied by ACL" not in result.get("error", "")


def test_execute_code_sandbox_tools_intersected_with_raw_acl(monkeypatch):
    import model_tools

    captured = {}

    def _fake_dispatch(name, args, **kw):
        captured["enabled_tools"] = kw.get("enabled_tools")
        return json.dumps({"ok": True})

    monkeypatch.setattr(model_tools.registry, "dispatch", _fake_dispatch)

    handle_function_call(
        "execute_code",
        {"code": "pass"},
        task_id="gateb-ec-drift",
        allowed_tool_names=["execute_code", "read_file"],
        enabled_tools=["execute_code", "read_file", "terminal"],
        skip_pre_tool_call_hook=True,
    )

    captured_tools = captured.get("enabled_tools") or []
    assert "terminal" not in captured_tools
    assert "read_file" in captured_tools
