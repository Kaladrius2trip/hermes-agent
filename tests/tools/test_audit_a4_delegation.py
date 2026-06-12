"""Regression tests for the 2026-06-11 fork-audit A4 delegation fixes.

Covers:
- delegation-001: parent MCP toolsets are NOT re-added past a category/
  profile-narrowed scope (the intersect-only guarantee holds).
- delegation-002: hyphen-separated secret keys are redacted.
- delegation-004: a zero-toolset parent yields a zero-toolset child, not
  DEFAULT_TOOLSETS.
"""

from unittest.mock import MagicMock, patch

from tools.delegate_tool import _build_child_agent
from tools.delegation_audit import redact_audit_value


def _parent(toolsets):
    parent = MagicMock()
    parent.enabled_toolsets = toolsets
    parent.model = "test-model"
    parent.base_url = ""
    parent.platform = "cli"
    parent._delegate_depth = 0
    return parent


def _spawn(parent, *, toolsets, **kwargs):
    mock_child = MagicMock()
    mock_child.tools = []
    mock_child.valid_tool_names = set()
    with patch("run_agent.AIAgent", return_value=mock_child) as MockAgent:
        _build_child_agent(
            task_index=0,
            goal="g",
            context=None,
            toolsets=toolsets,
            model=None,
            max_iterations=5,
            task_count=1,
            parent_agent=parent,
            **kwargs,
        )
    return MockAgent.call_args.kwargs["enabled_toolsets"]


class TestMcpScopeNotWidened:
    def test_category_scope_does_not_inherit_parent_mcp_toolsets(self):
        parent = _parent(["file", "search", "mcp-MiniMax"])

        spawned = _spawn(
            parent,
            toolsets=["file", "search"],
            delegation_category={"category": "review"},
            capability_profile={
                "active": True,
                "profile": "review",
                "workspace_policy": {"kind": "scratch", "mutate": False},
            },
        )

        assert "mcp-MiniMax" not in spawned

    def test_plain_delegation_still_inherits_mcp_toolsets(self):
        parent = _parent(["file", "search", "mcp-MiniMax"])

        spawned = _spawn(parent, toolsets=["file", "search"])

        assert "mcp-MiniMax" in spawned


class TestZeroToolsetParent:
    def test_zero_toolset_parent_yields_zero_toolset_child(self):
        parent = _parent([])

        spawned = _spawn(parent, toolsets=None)

        assert spawned == []

    def test_unknown_parent_toolsets_fall_back_to_defaults(self):
        parent = _parent(None)
        parent.enabled_toolsets = None

        spawned = _spawn(parent, toolsets=None)

        assert "terminal" in spawned  # DEFAULT_TOOLSETS path preserved


class TestHyphenSecretRedaction:
    def test_http_header_style_keys_redacted(self):
        value = {
            "x-api-key": "sk-12345",
            "X-Api-Key": "sk-67890",
            "proxy-authorization": "Basic abc",
            "set-cookie": "session=abc",
            "task_index": 3,
        }

        redacted = redact_audit_value(value)

        assert redacted["x-api-key"] == "[REDACTED]"
        assert redacted["X-Api-Key"] == "[REDACTED]"
        assert redacted["proxy-authorization"] == "[REDACTED]"
        assert redacted["set-cookie"] == "[REDACTED]"
        assert redacted["task_index"] == 3
