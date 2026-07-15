"""Security: a delegated child must not exceed the parent's concrete ACL.

Fork-audit finding (2026-07-15): _parent_toolsets_for_delegation returns
TOOLSET categories as the child's scope upper bound, but a toolset re-widens a
concrete grant (e.g. the ``file`` toolset carries write_file/patch even when the
parent was granted only read_file). Unless the parent's allowed_tool_names is
threaded into the child AIAgent, a gateway user granted ``delegate_task`` but
denied ``terminal``/``write_file`` can spawn a child that regains them
(privilege escalation). These assert the constructor receives the parent ACL.
"""

from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent


def _parent(allowed_tool_names):
    parent = MagicMock()
    parent.enabled_toolsets = ["file", "terminal"]
    parent.allowed_tool_names = allowed_tool_names
    parent.model = "test-model"
    parent.base_url = ""
    parent.platform = "telegram"
    parent._delegate_depth = 0
    return parent


def _mutate_true_profile():
    return {
        "profile": "implementation",
        "workspace_policy": {"kind": "worktree", "mutate": True},
    }


def test_child_leaf_acl_inherits_parent_minus_blocked_tools():
    parent = _parent(["read_file", "web_search", "delegate_task", "execute_code"])

    mock_child = MagicMock()
    mock_child.tools = [{"type": "function", "function": {"name": "read_file"}}]
    mock_child.valid_tool_names = {"read_file"}

    with patch("run_agent.AIAgent", return_value=mock_child) as MockAgent:
        _build_child_agent(
            task_index=0,
            goal="Implement",
            context=None,
            toolsets=["file", "search"],
            model=None,
            max_iterations=5,
            task_count=1,
            parent_agent=parent,
            capability_profile=_mutate_true_profile(),
        )

    acl = MockAgent.call_args.kwargs.get("allowed_tool_names")
    assert "read_file" in acl
    assert "web_search" in acl
    assert "delegate_task" not in acl
    assert "execute_code" not in acl


def test_child_constructor_unrestricted_when_parent_has_no_acl():
    parent = _parent(None)

    mock_child = MagicMock()
    mock_child.tools = [{"type": "function", "function": {"name": "write_file"}}]
    mock_child.valid_tool_names = {"write_file"}

    with patch("run_agent.AIAgent", return_value=mock_child) as MockAgent:
        _build_child_agent(
            task_index=0,
            goal="Implement",
            context=None,
            toolsets=["file", "terminal"],
            model=None,
            max_iterations=5,
            task_count=1,
            parent_agent=parent,
            capability_profile=_mutate_true_profile(),
        )

    assert MockAgent.call_args.kwargs.get("allowed_tool_names") is None
