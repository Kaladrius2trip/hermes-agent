"""Tests for delegation category config resolution."""

import pytest

from tools.delegation_categories import (
    DelegationCategoryConfigError,
    resolve_delegation_category,
)


def _delegation_config():
    return {
        "default_category": "quick",
        "categories": {
            "quick": {
                "provider": "local-lmstudio",
                "model": "qwen/qwen3.6-35b-a3b",
                "reasoning_effort": "low",
                "toolsets": ["file", "terminal"],
                "toolsets_mode": "intersect",
                "max_iterations": 20,
                "child_timeout_seconds": 300,
                "fallback_chain": [
                    {"provider": "openrouter", "model": "google/gemini-3-flash"},
                ],
            },
            "disabled": {
                "enabled": False,
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.5",
            },
        },
    }


def test_default_category_resolves_primary_runtime_budget_and_fallback_metadata():
    resolved = resolve_delegation_category(
        _delegation_config(),
        category=None,
        parent_toolsets=["file", "terminal", "web"],
        requested_toolsets=None,
    )

    assert resolved["category"] == "quick"
    assert resolved["provider"] == "local-lmstudio"
    assert resolved["model"] == "qwen/qwen3.6-35b-a3b"
    assert resolved["reasoning_effort"] == "low"
    assert resolved["toolsets"] == ["file", "terminal"]
    assert resolved["max_iterations"] == 20
    assert resolved["child_timeout_seconds"] == 300
    assert resolved["fallback_chain"] == [
        {"provider": "openrouter", "model": "google/gemini-3-flash"},
    ]
    assert resolved["fallback_metadata"] == {
        "enabled": True,
        "count": 1,
        "providers": ["openrouter"],
        "models": ["google/gemini-3-flash"],
    }


def test_unknown_category_errors_before_child_spawn_with_valid_category_list():
    with pytest.raises(DelegationCategoryConfigError) as excinfo:
        resolve_delegation_category(
            _delegation_config(),
            category="missing",
            parent_toolsets=["file"],
            requested_toolsets=None,
        )

    assert excinfo.value.code == "unknown_category"
    assert excinfo.value.category == "missing"
    assert excinfo.value.valid_categories == ["disabled", "quick"]


def test_disabled_category_errors_before_child_spawn():
    with pytest.raises(DelegationCategoryConfigError) as excinfo:
        resolve_delegation_category(
            _delegation_config(),
            category="disabled",
            parent_toolsets=["file"],
            requested_toolsets=None,
        )

    assert excinfo.value.code == "disabled_category"
    assert excinfo.value.category == "disabled"


def test_requested_toolsets_only_narrow_category_and_parent_scope():
    resolved = resolve_delegation_category(
        _delegation_config(),
        category="quick",
        parent_toolsets=["file", "terminal", "web", "browser"],
        requested_toolsets=["web", "file", "browser"],
    )

    assert resolved["toolsets"] == ["file"]
    assert "web" not in resolved["toolsets"]
    assert "browser" not in resolved["toolsets"]


def test_parent_toolsets_also_narrow_category_scope():
    resolved = resolve_delegation_category(
        _delegation_config(),
        category="quick",
        parent_toolsets=["file"],
        requested_toolsets=None,
    )

    assert resolved["toolsets"] == ["file"]
