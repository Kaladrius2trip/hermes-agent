"""Regression tests for the 2026-06-11 fork-audit A3 capability-profile fixes.

Covers:
- cap-001: workspace_policy.mutate=false is runtime-enforced in the child
  spawn path (mutation toolsets removed from scope; write_file/patch stripped
  from the constructed child's tool surface and ACL whitelist).
- cap-003: non-mapping profile bodies raise a structured config error.
- cap-004: an unknown category inherited via extends fails closed.
- cap-005: prompt_sections keys other than 'recipe' are rejected instead of
  silently dropped.
- cap-006: budget is validated (unknown keys, wrong types, negatives).
"""

import pytest

from tools.capability_profiles import (
    CapabilityProfileConfigError,
    resolve_capability_profile,
)


def _resolve(profiles, name, **kwargs):
    return resolve_capability_profile({"profiles": profiles}, profile=name, **kwargs)


class TestProfileBodyValidation:
    def test_non_mapping_body_raises_structured_error(self):
        with pytest.raises(CapabilityProfileConfigError) as excinfo:
            _resolve({"p": "oops"}, "p")
        assert excinfo.value.code == "invalid_profile_body"

    def test_list_body_overriding_builtin_raises_structured_error(self):
        # Previously raised a raw ValueError from dict(<list>) deep in the
        # resolver, breaking the structured-error contract.
        with pytest.raises(CapabilityProfileConfigError) as excinfo:
            _resolve({"review": ["nonsense"]}, "review")
        assert excinfo.value.code == "invalid_profile_body"


class TestCategoryFailClosed:
    def test_unknown_category_inherited_via_extends_fails_closed(self):
        profiles = {
            "parent": {
                "responsibility": "Parent.",
                "category": "nope",
                "allowed_toolsets": ["file"],
            },
            "child": {
                "responsibility": "Child.",
                "extends": "parent",
            },
        }
        with pytest.raises(CapabilityProfileConfigError) as excinfo:
            resolve_capability_profile(
                {"profiles": profiles}, profile="child", delegation_config={}
            )
        assert excinfo.value.code == "unknown_category"

    def test_builtin_category_not_in_delegation_config_stays_lenient(self):
        # Builtin profiles carry categories; when the operator has no matching
        # delegation category configured, the builtin still resolves (the
        # fail-closed rule applies to user-contributed categories).
        result = resolve_capability_profile(
            {"profiles": {}}, profile="review", delegation_config={}
        )
        assert result["active"] is True


class TestPromptSectionsStrict:
    def test_inline_sections_rejected_not_silently_dropped(self):
        profiles = {
            "p": {
                "responsibility": "Review.",
                "allowed_toolsets": ["file"],
                "prompt_sections": {
                    "recipe": "critic-reviewer",
                    "sections": [{"title": "Extra Boundary", "body": "Never touch prod."}],
                },
            }
        }
        with pytest.raises(CapabilityProfileConfigError) as excinfo:
            _resolve(profiles, "p")
        assert excinfo.value.code == "invalid_prompt_sections"


class TestBudgetValidation:
    def _profile(self, budget):
        return {
            "p": {
                "responsibility": "Work.",
                "allowed_toolsets": ["file"],
                "budget": budget,
            }
        }

    def test_unknown_budget_key_rejected(self):
        with pytest.raises(CapabilityProfileConfigError) as excinfo:
            _resolve(self._profile({"iterations_cap": 100}), "p")
        assert excinfo.value.code == "invalid_budget"

    def test_negative_max_iterations_rejected(self):
        with pytest.raises(CapabilityProfileConfigError) as excinfo:
            _resolve(self._profile({"max_iterations": -5}), "p")
        assert excinfo.value.code == "invalid_budget"

    def test_wrong_type_timeout_rejected(self):
        with pytest.raises(CapabilityProfileConfigError) as excinfo:
            _resolve(self._profile({"child_timeout_seconds": "soon"}), "p")
        assert excinfo.value.code == "invalid_budget"

    def test_valid_budget_accepted(self):
        result = _resolve(
            self._profile({"reasoning_effort": "high", "max_iterations": 9}), "p"
        )
        assert result["budget"]["max_iterations"] == 9


class TestMutateRuntimeEnforcement:
    def test_mutation_tools_stripped_from_child_surface(self):
        from unittest.mock import MagicMock, patch

        from tools.delegate_tool import _build_child_agent

        parent = MagicMock()
        parent.enabled_toolsets = ["file", "search", "terminal"]
        parent.model = "test-model"
        parent.base_url = ""
        parent.platform = "cli"
        parent._delegate_depth = 0

        mock_child = MagicMock()
        mock_child.tools = [
            {"type": "function", "function": {"name": "read_file"}},
            {"type": "function", "function": {"name": "write_file"}},
            {"type": "function", "function": {"name": "patch"}},
            {"type": "function", "function": {"name": "search_files"}},
        ]
        mock_child.valid_tool_names = {"read_file", "write_file", "patch", "search_files"}
        mock_child.allowed_tool_names = None

        with patch("run_agent.AIAgent", return_value=mock_child) as MockAgent:
            child = _build_child_agent(
                task_index=0,
                goal="Review only",
                context=None,
                toolsets=["file", "search", "terminal"],
                model=None,
                max_iterations=5,
                task_count=1,
                parent_agent=parent,
                capability_profile={
                    "profile": "review",
                    "workspace_policy": {"kind": "scratch", "mutate": False},
                },
            )

        spawn_toolsets = MockAgent.call_args.kwargs["enabled_toolsets"]
        assert "terminal" not in spawn_toolsets  # mutation-capable toolset removed
        surface = {t["function"]["name"] for t in child.tools}
        assert "write_file" not in surface
        assert "patch" not in surface
        assert "read_file" in surface
        assert "write_file" not in set(child.allowed_tool_names or [])
        assert "write_file" not in child.valid_tool_names

    def test_mutate_true_profile_keeps_tools(self):
        from unittest.mock import MagicMock, patch

        from tools.delegate_tool import _build_child_agent

        parent = MagicMock()
        parent.enabled_toolsets = ["file", "terminal"]
        parent.model = "test-model"
        parent.base_url = ""
        parent.platform = "cli"
        parent._delegate_depth = 0

        mock_child = MagicMock()
        mock_child.tools = [{"type": "function", "function": {"name": "write_file"}}]
        mock_child.valid_tool_names = {"write_file"}

        with patch("run_agent.AIAgent", return_value=mock_child) as MockAgent:
            child = _build_child_agent(
                task_index=0,
                goal="Implement",
                context=None,
                toolsets=["file", "terminal"],
                model=None,
                max_iterations=5,
                task_count=1,
                parent_agent=parent,
                capability_profile={
                    "profile": "implementation",
                    "workspace_policy": {"kind": "worktree", "mutate": True},
                },
            )

        assert "terminal" in MockAgent.call_args.kwargs["enabled_toolsets"]
        assert {t["function"]["name"] for t in child.tools} == {"write_file"}
