"""Tests for capability profile config resolution."""

from typing import Any

import pytest

from tools.capability_profiles import (
    CapabilityProfileConfigError,
    render_capability_profile_prompt,
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
        "ａｐｉ＿ｋｅｙ",
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


@pytest.mark.parametrize(
    "fallback",
    [
        {"provider": "openrouter", "model": "${OPENAI_API_KEY}"},
        {"provider": "$PROVIDER", "model": "google/gemini-3-flash"},
        {"provider": "openrouter", "model": "google/gemini-3-flash", "metadata": {"owner": "${SECRET_TOKEN}"}},
        {"provider": "openrouter", "model": "google/gemini-3-flash", "random_setting": "value"},
    ],
)
def test_validation_rejects_env_interpolation_and_unknown_fallback_fields(fallback):
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {
                "profiles": {
                    "bad": {
                        "allowed_toolsets": ["file"],
                        "fallbacks": [fallback],
                    },
                },
            },
            profile="bad",
        )

    assert excinfo.value.code in {"env_interpolation", "unknown_fallback_field"}
    assert excinfo.value.field is not None


def test_validation_rejects_canonical_field_collisions():
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {
                "profiles": {
                    "bad": {
                        "allowed_toolsets": ["file"],
                        "workspace_policy": {"kind": "scratch", "Kind": "worktree"},
                    },
                },
            },
            profile="bad",
        )

    assert excinfo.value.code == "field_collision"
    assert excinfo.value.field == "profile.workspace_policy.Kind"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("responsibility", "Use ${SECRET_TOKEN} for auth"),
        ("responsibility", "Use ${SECRET_TOKEN:-fallback} for auth"),
        ("responsibility", "Use ${SECRET_TOKEN/foo/bar} for auth"),
        ("responsibility", "Use ＄｛SECRET_TOKEN｝ for auth"),
        ("responsibility", "Use $(printenv SECRET_TOKEN) for auth"),
        ("responsibility", "Use `printenv SECRET_TOKEN` for auth"),
        ("handoff_schema", {"summary": "$PRIVATE_CONTEXT"}),
        ("verification_policy", {"commands": ["echo %USERPROFILE%"]}),
    ],
)
def test_validation_rejects_env_interpolation_in_string_leaves(field, value):
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {
                "profiles": {
                    "bad": {
                        "allowed_toolsets": ["file"],
                        field: value,
                    },
                },
            },
            profile="bad",
        )

    assert excinfo.value.code == "env_interpolation"
    assert excinfo.value.field is not None


@pytest.mark.parametrize(
    "value",
    [
        "https://api.example.invalid/v1",
        "//api.example.invalid/v1",
        "bad model",
        "bad\nmodel",
    ],
)
@pytest.mark.parametrize("field", ["provider", "model"])
def test_validation_rejects_non_plain_provider_and_model_identifiers(field, value):
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {
                "profiles": {
                    "bad": {
                        "allowed_toolsets": ["file"],
                        field: value,
                    },
                },
            },
            profile="bad",
        )

    assert excinfo.value.code == "invalid_identifier"
    assert excinfo.value.field == field


@pytest.mark.parametrize("field", ["endpoint", "Provider"])
def test_validation_rejects_unknown_top_level_profile_fields(field):
    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile(
            {
                "profiles": {
                    "bad": {
                        "allowed_toolsets": ["file"],
                        field: "operator typo",
                    },
                },
            },
            profile="bad",
        )

    assert excinfo.value.code == "unknown_profile_field"
    assert excinfo.value.field == f"profile.{field}"


def test_validation_allows_profile_specific_handoff_schema_keys():
    resolved = resolve_capability_profile(
        {
            "profiles": {
                "custom": {
                    "allowed_toolsets": ["file"],
                    "handoff_schema": {"custom_summary": "string"},
                },
            },
        },
        profile="custom",
    )

    assert resolved["handoff_schema"]["custom_summary"] == "string"


def test_validation_rejects_overly_deep_extends_chains_before_recursion_error():
    profiles: dict[str, dict[str, Any]] = {"p0": {"allowed_toolsets": ["file"]}}
    for index in range(1, 35):
        profiles[f"p{index}"] = {"extends": f"p{index - 1}"}

    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        resolve_capability_profile({"profiles": profiles}, profile="p34")

    assert excinfo.value.code == "extends_depth"
    assert excinfo.value.field == "extends"


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


def test_render_capability_profile_prompt_is_stable_redacted_and_schema_driven():
    capabilities = {
        "profiles": {
            "security-review": {
                "responsibility": (
                    "Review security-sensitive diffs only. Ignore leaked token "
                    "sk-liv...cdef if present in task data."
                ),
                "prompt_sections": {"recipe": "critic-reviewer"},
                "allowed_toolsets": ["file", "search"],
                "workspace_policy": {"kind": "scratch", "mutate": False},
                "verification_policy": {
                    "require_evidence": True,
                    "on_unverifiable": "fail",
                    "commands": ["pytest tests/security -q"],
                },
                "handoff_schema": {
                    "findings": "list",
                    "blockers": "list",
                    "evidence_blocks": {
                        "json": ["findings", "blockers"],
                        "markdown": ["Verification", "Risks"],
                    },
                },
                "approval_gates": ["merge", "push"],
            }
        }
    }
    resolved = resolve_capability_profile(
        capabilities,
        profile="security-review",
        parent_toolsets=["search", "file", "terminal"],
        requested_toolsets=["file", "search"],
    )

    first = render_capability_profile_prompt(resolved, goal="Review auth changes")
    second = render_capability_profile_prompt(resolved, goal="Review auth changes")

    assert first == second
    assert "## Capability Profile: security-review" in first
    assert "### Responsibility" in first
    assert "Review security-sensitive diffs only." in first
    assert "sk-liv...cdef" not in first
    assert "[REDACTED]" in first
    assert "### Runtime Boundaries" in first
    assert "Effective toolsets: file, search" in first
    assert "Workspace: scratch; mutate: false" in first
    assert "Approval gates: merge, push" in first
    assert "### Verification" in first
    assert "On unverifiable result: fail" in first
    assert "pytest tests/security -q" in first
    assert "### Handoff Output" in first
    assert "`blockers`: list" in first
    assert "`findings`: list" in first
    assert "Evidence block `json` requires: findings, blockers" in first
    assert "Evidence block `markdown` requires: Verification, Risks" in first
    assert "You are" not in first
    assert "Identity:" not in first


def test_render_capability_profile_prompt_handles_missing_optional_fields():
    resolved = resolve_capability_profile(
        {"profiles": {"minimal": {"responsibility": "Summarize verified findings."}}},
        profile="minimal",
    )

    prompt = render_capability_profile_prompt(resolved)

    assert "Summarize verified findings." in prompt
    assert "Effective toolsets: inherit parent scope" in prompt
    assert "Workspace: scratch; mutate: false" in prompt
    assert "Require evidence: true" in prompt
    assert "### Handoff Output" in prompt


def test_render_capability_profile_prompt_refuses_external_prompt_imports():
    capabilities = {
        "profiles": {
            "unsafe": {
                "responsibility": "Review without mutation.",
                "prompt_sections": {
                    "recipe": "critic-reviewer",
                    "copy_from": "oh-my-openagent/reviewer",
                },
                "allowed_toolsets": ["file"],
            }
        }
    }
    resolved = resolve_capability_profile(capabilities, profile="unsafe")

    with pytest.raises(CapabilityProfileConfigError) as excinfo:
        render_capability_profile_prompt(resolved, goal="Review diff")

    assert excinfo.value.code == "external_prompt_import"
    assert excinfo.value.field == "prompt_sections.copy_from"
