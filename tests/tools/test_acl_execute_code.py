from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from unittest.mock import patch

import pytest

from tools.code_execution_tool import execute_code, _rpc_poll_loop

os.environ["TERMINAL_ENV"] = "local"


@pytest.mark.skipif(sys.platform == "win32", reason="UDS not available on Windows")
def test_execute_code_explicit_empty_enabled_tools_generates_no_nested_tools(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    code = """
try:
    from hermes_tools import terminal
    print('terminal import leaked')
except ImportError as exc:
    print('terminal unavailable')
"""

    with patch("model_tools.handle_function_call", side_effect=AssertionError("nested tool should not dispatch")):
        result = json.loads(execute_code(code=code, task_id="acl-empty", enabled_tools=[]))

    assert result["status"] == "success", result
    assert "terminal unavailable" in result["output"]
    assert result["tool_calls_made"] == 0


@pytest.mark.skipif(sys.platform == "win32", reason="UDS not available on Windows")
def test_execute_code_enabled_tools_forwards_nested_acl_to_dispatch(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    code = """
from hermes_tools import terminal
result = terminal('echo scoped')
print(result['output'])
"""

    def fake_handle(function_name, function_args, task_id=None, user_task=None, enabled_tools=None):
        assert function_name == "terminal"
        assert enabled_tools == ["terminal"]
        return json.dumps({"output": "scoped", "exit_code": 0})

    with patch("model_tools.handle_function_call", side_effect=fake_handle):
        result = json.loads(
            execute_code(
                code=code,
                task_id="acl-scoped",
                enabled_tools=["execute_code", "terminal"],
            )
        )

    assert result["status"] == "success", result
    assert "scoped" in result["output"]
    assert result["tool_calls_made"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="UDS not available on Windows")
def test_execute_code_none_enabled_tools_preserves_legacy_nested_tool_fallback(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    code = """
from hermes_tools import terminal
result = terminal('echo legacy')
print(result['output'])
"""

    def fake_handle(function_name, function_args, task_id=None, user_task=None, enabled_tools=None):
        assert function_name == "terminal"
        assert enabled_tools is not None
        assert "terminal" in enabled_tools
        return json.dumps({"output": "legacy", "exit_code": 0})

    with patch("model_tools.handle_function_call", side_effect=fake_handle):
        result = json.loads(execute_code(code=code, task_id="acl-none", enabled_tools=None))

    assert result["status"] == "success", result
    assert "legacy" in result["output"]
    assert result["tool_calls_made"] == 1


@pytest.mark.skipif(sys.platform == "win32", reason="UDS not available on Windows")
def test_execute_code_partial_enabled_tools_does_not_expose_omitted_nested_tool(monkeypatch):
    monkeypatch.setenv("TERMINAL_ENV", "local")
    code = """
try:
    from hermes_tools import terminal
    print('terminal import leaked')
except ImportError:
    print('terminal unavailable')
"""

    with patch("model_tools.handle_function_call", side_effect=AssertionError("terminal must not dispatch")):
        result = json.loads(
            execute_code(
                code=code,
                task_id="acl-partial",
                enabled_tools=["execute_code", "read_file"],
            )
        )

    assert result["status"] == "success", result
    assert "terminal unavailable" in result["output"]
    assert result["tool_calls_made"] == 0


def test_remote_rpc_poll_loop_forwards_enabled_tools_to_dispatch(monkeypatch, tmp_path):
    rpc_dir = tmp_path / "rpc"
    rpc_dir.mkdir()
    (rpc_dir / "req_000001").write_text(
        json.dumps({"tool": "terminal", "args": {"command": "echo remote"}, "seq": 1}),
        encoding="utf-8",
    )
    stop_event = threading.Event()
    captured = {}

    class LocalShellEnv:
        def execute(self, command, cwd="/", timeout=10):
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                text=True,
                capture_output=True,
                timeout=timeout,
            )
            return {"output": proc.stdout + proc.stderr, "exit_code": proc.returncode}

    def fake_handle(function_name, function_args, task_id=None, user_task=None, enabled_tools=None):
        captured["function_name"] = function_name
        captured["function_args"] = function_args
        captured["task_id"] = task_id
        captured["enabled_tools"] = enabled_tools
        stop_event.set()
        return json.dumps({"output": "remote", "exit_code": 0})

    with patch("model_tools.handle_function_call", side_effect=fake_handle):
        _rpc_poll_loop(
            LocalShellEnv(),
            str(rpc_dir),
            "acl-remote",
            [],
            [0],
            5,
            frozenset({"terminal"}),
            stop_event,
        )

    assert captured == {
        "function_name": "terminal",
        "function_args": {"command": "echo remote"},
        "task_id": "acl-remote",
        "enabled_tools": ["terminal"],
    }
