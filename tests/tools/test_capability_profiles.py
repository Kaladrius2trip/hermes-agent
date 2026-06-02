"""Tests for capability profile config resolution."""

import pytest

from tools.capability_profiles import (
    CapabilityProfileConfigError,
    resolve_capability_profile,
)


def _delegation_config():
    return {
        "categories": {
            "review": {
                "recipe": "critic-reviewer",
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.5",
                "reasoning_effort": "high",
                "max_iterations": 40,
                "child_timeout_seconds": 600,
                "toolsets_mode": "intersect",
                "toolsets": ["file", "search"],
                "fallback_chain": [
                    {"provider": "openrouter", "model": "google/gemini-3-flash"},
                ],
            },
        },
    }


def test_builtin_review_profile_resolves_safe_readonly_defaults_and_strict_toolset_intersection():
    resolved = resolve_capability_profile(
        {"profiles": {}},
        profile="review",
        delegation_config=_delegation_config(),
        parent_toolsets=["terminal", "file", "search", "web"],
        requested_toolsets=["terminal", "search", "file"],
    )

    assert resolved["profile"] == "review"
    assert resolved["category"] == "review"
    assert resolved["prompt_sections"] == {"recipe": "critic-reviewer"}
    assert resolved["provider"] == "openrouter"
    assert resolved["model"] == "anthropic/claude-sonnet-4.5"
    assert resolved["budget"] == {
        "reasoning_effort": "high",
        "max_iterations": 40,
        "child_timeout_seconds": 600,
    }
    assert resolved["toolsets"] == ["file", "search"]
    assert resolved["workspace_policy"] == {"kind": "scratch", "mutate": False}
    assert resolved["verification_policy"] == {
        "require_evidence": True,
        "on_unverifiable": "report",
    }
    assert resolved["approval_gates"] == ["push", "merge", "publish", "send_message"]
    assert resolved["fallbacks"] == [
        {"provider": "openrouter", "model": "google/gemini-3-flash"},
    ]
    assert resolved["fallback_metadata"] == {
        "enabled": True,
        "count": 1,
        "providers": ["openrouter"],
        "models": ["google/gemini-3-flash"],
        "profiles": [],
    }


def test_configured_profile_overrides_builtin_without_widening_tools():
    capabilities = {
        "profiles": {
            "review": {
                "responsibility": "Review only security-sensitive Python diffs.",
                "allowed_toolsets": ["terminal", "file", "search"],
                "budget": {"max_iterations": 12},
                "approval_gates": ["push", "merge"],
            },
        },
    }

    resolved = resolve_capability_profile(
        capabilities,
        profile="review",
        delegation_config=_delegation_config(),
        parent_toolsets=["file", "search"],
        requested_toolsets=["terminal", "file"],
    )

    assert resolved["responsibility"] == "Review only security-sensitive Python diffs."
    assert resolved["toolsets"] == ["file"]
    assert resolved["budget"]["max_iterations"] == 12
    assert resolved["budget"]["reasoning_effort"] == "high"
    assert resolved["approval_gates"] == ["push", "merge"]


def test_profile_can_reference_legacy_category_without_changing_category_resolver_behavior():
    capabilities = {
        "profiles": {
            "safe-review": {
                "responsibility": "Review changed files without mutation.",
                "category": "review",
                "allowed_toolsets": ["file", "search", "terminal"],
                "workspace_policy": {"mutate": False},
            },
        },
    }

    resolved = resolve_capability_profile(
        capabilities,
        profile="safe-review",
        delegation_config=_delegation_config(),
        parent_toolsets=["file", "search", "terminal"],
        requested_toolsets=None,
    )

    assert resolved["profile"] == "safe-review"
    assert resolved["category"] == "review"
    assert resolved["prompt_sections"] == {"recipe": "critic-reviewer"}
    assert resolved["provider"] == "openrouter"
    assert resolved["model"] == "anthropic/claude-sonnet-4.5"
    assert resolved["toolsets"] == ["file", "search"]
    assert resolved["fallbacks"] == [
        {"provider": "openrouter", "model": "google/gemini-3-flash"},
    ]


def test_unknown_profile_errors_before_child_spawn_with_valid_profile_list():
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile({"profiles": {}}, profile="missing")

    assert excinfo.value.code == "unknown_profile"
    assert excinfo.value.profile == "missing"
    assert "review" in excinfo.value.valid_profiles


def test_validation_rejects_unknown_toolsets_unsafe_gates_and_secret_fields():
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {"profiles": {"bad": {"allowed_toolsets": ["file", "root_shell"]}}},
            profile="bad",
        )
    assert excinfo.value.code == "unknown_toolset"

    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {"profiles": {"bad": {"approval_gates": ["push", "wipe_disk"]}}},
            profile="bad",
        )
    assert excinfo.value.code == "unsafe_approval_gate"

    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {
                "profiles": {
                    "bad": {
                        "provider": "openrouter",
                        "api_key": "${OPENROUTER_API_KEY}",
                    },
                },
            },
            profile="bad",
        )
    assert excinfo.value.code == "secret_field"


def test_validation_rejects_recursive_fallback_profile_loops():
    capabilities = {
        "profiles": {
            "primary": {
                "responsibility": "Primary profile.",
                "fallbacks": [{"profile": "backup"}],
            },
            "backup": {
                "responsibility": "Backup profile.",
                "fallbacks": [{"profile": "primary"}],
            },
        },
    }

    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(capabilities, profile="primary")

    assert excinfo.value.code == "fallback_loop"
    assert excinfo.value.profile == "primary"


def test_validation_rejects_recursive_extends_profile_loops():
    capabilities = {
        "profiles": {
            "primary": {"extends": "backup"},
            "backup": {"extends": "primary"},
        },
    }

    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(capabilities, profile="primary")

    assert excinfo.value.code == "extends_loop"
    assert excinfo.value.profile == "primary"
    assert excinfo.value.field == "extends"


def test_validation_rejects_unknown_extends_profile():
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {"profiles": {"primary": {"extends": "missing"}}},
            profile="primary",
        )

    assert excinfo.value.code == "unknown_profile"
    assert excinfo.value.profile == "missing"
    assert excinfo.value.field == "extends"


@pytest.mark.parametrize(
    "field_name",
    [
        "openai_api_key",
        "access_token",
        "auth_header",
        "authorization_header",
        "extra_headers",
        "baseUrl",
        "extraEnv",
        "envVars",
        "credentialFile",
    ],
)
def test_validation_rejects_secret_like_field_variants_in_fallbacks(field_name):
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {
                "profiles": {
                    "bad": {
                        "allowed_toolsets": ["file"],
                        "fallbacks": [
                            {
                                "provider": "openrouter",
                                "model": "google/gemini-3-flash",
                                field_name: "${OPENAI_API_KEY}",
                            }
                        ],
                    },
                },
            },
            profile="bad",
        )

    assert excinfo.value.code == "secret_field"
    assert excinfo.value.field is not None
    assert excinfo.value.field.endswith(f"fallbacks[0].{field_name}")


def test_fallback_toolsets_cannot_exceed_final_parent_requested_scope():
    capabilities = {
        "profiles": {
            "safe": {
                "allowed_toolsets": ["file", "search"],
                "fallbacks": [
                    {
                        "provider": "openrouter",
                        "model": "google/gemini-3-flash",
                        "allowed_toolsets": ["search"],
                    }
                ],
            },
        },
    }

    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            capabilities,
            profile="safe",
            parent_toolsets=["file"],
            requested_toolsets=["file"],
        )

    assert excinfo.value.code == "toolset_widening"
    assert excinfo.value.field == "fallbacks[0].allowed_toolsets"
