"""Pure resolver for config-backed capability profiles.

Capability profiles are an additive layer over existing delegation categories and
agent recipes.  They resolve operator-owned profile data into a concrete child
runtime spec before any child spawn happens.  The resolver is deliberately pure:
no I/O, no provider clients, no child spawning.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from typing import Any, Dict, List, Mapping, Optional, Sequence

from toolsets import TOOLSETS
from tools.agent_recipes import list_builtin_recipes
from tools.delegation_categories import (
    DelegationCategoryConfigError,
    resolve_delegation_category,
)


SAFE_APPROVAL_GATES = ("push", "merge", "publish", "send_message")
DEFAULT_APPROVAL_GATES = list(SAFE_APPROVAL_GATES)
DEFAULT_BUDGET = {
    "reasoning_effort": "",
    "max_iterations": None,
    "child_timeout_seconds": None,
}
DEFAULT_WORKSPACE_POLICY = {"kind": "scratch", "mutate": False}
DEFAULT_VERIFICATION_POLICY = {"require_evidence": True, "on_unverifiable": "report"}
DEFAULT_HANDOFF_SCHEMA = {
    "changed_files": "list",
    "commands_run": "list",
    "findings": "list",
    "blockers": "list",
}

_SECRET_FIELD_KEYS = {
    "api_key",
    "apikey",
    "api-key",
    "api_key_env",
    "key_env",
    "env",
    "environment",
    "extra_env",
    "secret",
    "secrets",
    "token",
    "tokens",
    "password",
    "passwd",
    "credential",
    "credentials",
    "auth",
    "authorization",
    "headers",
    "base_url",
    "url",
}
_SECRET_FIELD_PARTS = {
    "auth",
    "authorization",
    "env",
    "environment",
    "header",
    "headers",
    "secret",
    "secrets",
    "token",
    "tokens",
    "password",
    "passwd",
    "credential",
    "credentials",
}
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_ENV_INTERPOLATION = re.compile(
    r"(\$\{[A-Za-z_][A-Za-z0-9_]*[^}]*\}|\$[A-Za-z_][A-Za-z0-9_]*|%[A-Za-z_][A-Za-z0-9_]*%|\$\([^)]*\)|`[^`]*`)"
)
_URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_INVALID_IDENTIFIER_CHARS = re.compile(r"(^//|\s|[\x00-\x1f\x7f])")
_FALLBACK_ALLOWED_KEYS = {"provider", "model", "profile", "allowed_toolsets"}
_PROFILE_ALLOWED_KEYS = {
    "extends",
    "enabled",
    "responsibility",
    "category",
    "prompt_sections",
    "allowed_toolsets",
    "provider",
    "model",
    "budget",
    "workspace_policy",
    "verification_policy",
    "handoff_schema",
    "approval_gates",
    "fallbacks",
}
_MAX_PROFILE_CHAIN_DEPTH = 32
_WORKSPACE_KINDS = {"scratch", "worktree", "dir"}
_VERIFICATION_ON_UNVERIFIABLE = {"report", "fail"}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9._-]{4,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{4,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{6,}"),
    re.compile(
        r"(?i)\b(api[_-]?key|token|password|secret)\b\s*[:=]\s*[^\s,;]+"
    ),
)
_EXTERNAL_PROMPT_FIELD_KEYS = {
    "copy_from",
    "external_prompt",
    "external_prompt_url",
    "import_from",
    "prompt_copy",
    "prompt_import",
    "prompt_source",
    "prompt_url",
    "source_prompt",
    "source_prompt_url",
}
_EXTERNAL_PROMPT_TEXT = re.compile(
    r"(?i)(copy|fetch|import|load)\s+(?:an?\s+)?(?:external\s+)?prompt\s+"
    r"(?:from|via)|prompt\s+(?:copy|import)|oh-my-(?:openagent|opencode|hermes)"
)


def _builtins() -> Dict[str, Dict[str, Any]]:
    return {
        "implementation": {
            "responsibility": (
                "Implement scoped code changes for the delegated goal; preserve "
                "unrelated behavior and verify the changed path."
            ),
            "category": "deep",
            "prompt_sections": {"recipe": "deep-worker"},
            "allowed_toolsets": ["terminal", "file", "search"],
            "budget": {
                "reasoning_effort": "high",
                "max_iterations": 70,
                "child_timeout_seconds": 1200,
            },
            "workspace_policy": {"kind": "worktree", "mutate": True},
            "verification_policy": {
                "require_evidence": True,
                "on_unverifiable": "fail",
                "commands": ["targeted tests for touched code"],
            },
            "handoff_schema": {
                "changed_files": "list",
                "commands_run": "list",
                "tests": "list",
                "risks": "list",
                "blockers": "list",
            },
        },
        "review": {
            "responsibility": "Review changed files for correctness, security, regressions, and missing tests; report findings only.",
            "category": "review",
            "prompt_sections": {"recipe": "critic-reviewer"},
            "allowed_toolsets": ["file", "search"],
            "budget": {
                "reasoning_effort": "high",
                "max_iterations": 40,
                "child_timeout_seconds": 600,
            },
            "workspace_policy": {"kind": "scratch", "mutate": False},
            "handoff_schema": {
                "findings": "list",
                "evidence": "list",
                "commands_run": "list",
                "blockers": "list",
            },
        },
        "testing": {
            "responsibility": (
                "Create or run focused tests for the delegated behavior; isolate "
                "failures from unrelated suite noise."
            ),
            "category": "deep",
            "prompt_sections": {"recipe": "focused-executor"},
            "allowed_toolsets": ["terminal", "file", "search"],
            "budget": {
                "reasoning_effort": "medium",
                "max_iterations": 50,
                "child_timeout_seconds": 900,
            },
            "workspace_policy": {"kind": "worktree", "mutate": True},
            "verification_policy": {
                "require_evidence": True,
                "on_unverifiable": "fail",
                "commands": ["focused test command", "regression check when available"],
            },
            "handoff_schema": {
                "tests_added": "list",
                "commands_run": "list",
                "failures": "list",
                "coverage_gaps": "list",
                "blockers": "list",
            },
        },
        "research": {
            "responsibility": "Gather evidence from supplied context and allowed sources, then synthesize cited findings without mutating project files.",
            "category": "writing",
            "prompt_sections": {"recipe": "researcher"},
            "allowed_toolsets": ["file", "search", "web"],
            "budget": {
                "reasoning_effort": "medium",
                "max_iterations": 50,
                "child_timeout_seconds": 900,
            },
            "workspace_policy": {"kind": "scratch", "mutate": False},
            "verification_policy": {"require_evidence": True, "on_unverifiable": "report"},
            "handoff_schema": {
                "sources": "list",
                "findings": "list",
                "recommendation": "string",
                "confidence": "string",
                "blockers": "list",
            },
        },
        "orchestration": {
            "responsibility": "Decompose the delegated goal, route leaf work, track dependencies, and synthesize handoffs without doing leaf implementation.",
            "category": "deep",
            "prompt_sections": {"recipe": "team-orchestrator"},
            "allowed_toolsets": ["delegation", "file", "search"],
            "budget": {
                "reasoning_effort": "high",
                "max_iterations": 80,
                "child_timeout_seconds": 1200,
            },
            "workspace_policy": {"kind": "scratch", "mutate": False},
            "verification_policy": {"require_evidence": True, "on_unverifiable": "report"},
            "handoff_schema": {
                "plan": "list",
                "created_tasks": "list",
                "dependencies": "list",
                "handoffs": "list",
                "blockers": "list",
            },
        },
        "documentation": {
            "responsibility": "Write or update documentation from verified project facts; avoid runtime code changes unless explicitly requested.",
            "category": "writing",
            "prompt_sections": {"recipe": "focused-executor"},
            "allowed_toolsets": ["file", "search", "web"],
            "budget": {
                "reasoning_effort": "medium",
                "max_iterations": 45,
                "child_timeout_seconds": 900,
            },
            "workspace_policy": {"kind": "worktree", "mutate": True},
            "verification_policy": {
                "require_evidence": True,
                "on_unverifiable": "report",
                "commands": ["docs lint or targeted docs test when available"],
            },
            "handoff_schema": {
                "docs_changed": "list",
                "source_material": "list",
                "commands_run": "list",
                "gaps": "list",
                "blockers": "list",
            },
        },
        "webui-ux": {
            "responsibility": "Inspect WebUI behavior and UX evidence, then propose or make scoped UI fixes with screenshots or test output.",
            "category": "visual",
            "prompt_sections": {"recipe": "deep-worker"},
            "allowed_toolsets": ["browser", "vision", "file", "search", "terminal"],
            "budget": {
                "reasoning_effort": "high",
                "max_iterations": 70,
                "child_timeout_seconds": 1200,
            },
            "workspace_policy": {"kind": "worktree", "mutate": True},
            "verification_policy": {
                "require_evidence": True,
                "on_unverifiable": "report",
                "commands": ["targeted UI/unit test", "browser smoke when available"],
            },
            "handoff_schema": {
                "user_flows": "list",
                "screenshots": "list",
                "changed_files": "list",
                "commands_run": "list",
                "findings": "list",
                "blockers": "list",
            },
        },
        # Legacy category-mirror profiles kept for compatibility with earlier
        # Phase 13 canaries.  The named profile pack above is preferred because
        # its labels describe responsibilities instead of routing categories.
        "quick": {
            "responsibility": "Complete small scoped implementation, lookup, or mechanical fix tasks.",
            "category": "quick",
            "prompt_sections": {"recipe": "focused-executor"},
            "allowed_toolsets": ["file", "search"],
            "budget": {
                "reasoning_effort": "low",
                "max_iterations": 20,
                "child_timeout_seconds": 300,
            },
            "workspace_policy": {"kind": "scratch", "mutate": True},
        },
        "deep": {
            "responsibility": "Handle complex multi-step implementation, debugging, or research tasks with full verification.",
            "category": "deep",
            "prompt_sections": {"recipe": "deep-worker"},
            "allowed_toolsets": ["file", "search", "terminal", "web"],
            "budget": {
                "reasoning_effort": "high",
                "max_iterations": 80,
                "child_timeout_seconds": 1200,
            },
            "workspace_policy": {"kind": "worktree", "mutate": True},
        },
        "visual": {
            "responsibility": "Inspect screenshots, browser state, diagrams, or images and return evidence-backed findings.",
            "category": "visual",
            "prompt_sections": {"recipe": "explorer"},
            "allowed_toolsets": ["vision", "browser", "file"],
            "budget": {
                "reasoning_effort": "medium",
                "max_iterations": 30,
                "child_timeout_seconds": 600,
            },
            "workspace_policy": {"kind": "scratch", "mutate": False},
        },
        "writing": {
            "responsibility": "Draft concise prose or documentation from supplied context and gathered sources.",
            "category": "writing",
            "prompt_sections": {"recipe": "researcher"},
            "allowed_toolsets": ["file", "search", "web"],
            "budget": {
                "reasoning_effort": "medium",
                "max_iterations": 40,
                "child_timeout_seconds": 600,
            },
            "workspace_policy": {"kind": "scratch", "mutate": False},
        },
    }


class CapabilityProfileConfigError(ValueError):
    """Raised when a capability profile cannot be resolved safely."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        profile: Optional[str] = None,
        valid_profiles: Optional[Sequence[str]] = None,
        field: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.profile = profile
        self.valid_profiles = list(valid_profiles or [])
        self.field = field

    def __str__(self) -> str:  # pragma: no cover - trivial formatting
        base = super().__str__()
        if self.valid_profiles:
            return f"{base} (valid profiles: {', '.join(self.valid_profiles)})"
        return base


def _deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in dict(override).items():
        if isinstance(result.get(key), dict) and isinstance(value, Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _intersect_preserving_order(base: Sequence[str], allowed: Sequence[str]) -> List[str]:
    allowed_set = set(allowed)
    return [item for item in base if item in allowed_set]


def _merge_legacy_capability_profiles(
    canonical: Dict[str, Any], legacy_profiles: Any
) -> Dict[str, Any]:
    """Merge legacy top-level ``capability_profiles`` into canonical config.

    ``load_config()`` deep-merges ``DEFAULT_CONFIG`` first, so a full config
    always contains ``capabilities.profiles`` even when an older user config
    only defines top-level ``capability_profiles``. Preserve that alias without
    letting it override explicit canonical profile definitions or mask a
    malformed canonical ``profiles`` value.
    """
    if not isinstance(legacy_profiles, Mapping):
        return canonical

    merged = dict(canonical)
    profiles = merged.get("profiles")
    if isinstance(profiles, Mapping):
        profile_map = dict(legacy_profiles)
        profile_map.update(profiles)
        merged["profiles"] = profile_map
    elif "profiles" not in merged or profiles is None:
        merged["profiles"] = dict(legacy_profiles)
    return merged


def _normalise_capabilities_config(capabilities_config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    cfg = dict(capabilities_config or {})
    legacy_profiles = cfg.get("capability_profiles")
    if "capabilities" in cfg and "profiles" not in cfg:
        nested = cfg.get("capabilities")
        canonical = dict(nested or {}) if isinstance(nested, Mapping) else {}
        return _merge_legacy_capability_profiles(canonical, legacy_profiles)
    if "capability_profiles" in cfg:
        return _merge_legacy_capability_profiles(cfg, legacy_profiles)
    return cfg


def _valid_profile_names(config_profiles: Mapping[str, Any]) -> List[str]:
    return sorted(set(_builtins()) | set(config_profiles))


def _canonical_field_key(key: str) -> str:
    key = unicodedata.normalize("NFKC", key)
    split_camel = _CAMEL_BOUNDARY.sub("_", key)
    normalized = split_camel.casefold().replace("-", "_")
    return re.sub(r"[^a-z0-9_]+", "_", normalized).strip("_")


def _contains_env_interpolation(value: str) -> bool:
    return bool(_ENV_INTERPOLATION.search(unicodedata.normalize("NFKC", value)))


def _is_forbidden_field_key(key: str) -> bool:
    normalized = _canonical_field_key(key)
    if normalized in _SECRET_FIELD_KEYS:
        return True
    compact = normalized.replace("_", "")
    if "apikey" in compact:
        return True
    if normalized.startswith("env_") or normalized.endswith("_env") or "_env_" in normalized:
        return True
    parts = set(filter(None, normalized.split("_")))
    return bool(parts & _SECRET_FIELD_PARTS)


def _profile_spec(
    name: str,
    config_profiles: Mapping[str, Any],
    *,
    stack: Optional[Sequence[str]] = None,
    valid_profiles: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    path = list(stack or [])
    profile_names = list(valid_profiles or _valid_profile_names(config_profiles))
    if len(path) >= _MAX_PROFILE_CHAIN_DEPTH:
        origin = path[0] if path else name
        raise CapabilityProfileConfigError(
            f"Capability profile extends chain exceeds {_MAX_PROFILE_CHAIN_DEPTH} profiles",
            code="extends_depth",
            profile=origin,
            valid_profiles=profile_names,
            field="extends",
        )
    if name in path:
        origin = path[0] if path else name
        raise CapabilityProfileConfigError(
            f"Capability profile extends loop detected: {' -> '.join(path + [name])}",
            code="extends_loop",
            profile=origin,
            valid_profiles=profile_names,
            field="extends",
        )

    builtins = _builtins()
    base = builtins.get(name, {})
    override = config_profiles.get(name) or {}
    if not isinstance(override, Mapping):
        override = {}
    spec = _deep_merge(base, override)
    parent_name = spec.get("extends")
    if parent_name:
        parent_key = str(parent_name)
        if parent_key not in set(profile_names):
            raise CapabilityProfileConfigError(
                f"Capability profile {name!r} extends unknown profile {parent_key!r}",
                code="unknown_profile",
                profile=parent_key,
                valid_profiles=profile_names,
                field="extends",
            )
        parent_spec = _profile_spec(
            parent_key,
            config_profiles,
            stack=path + [name],
            valid_profiles=profile_names,
        )
        spec = _deep_merge(parent_spec, {k: v for k, v in spec.items() if k != "extends"})
    return spec


def _validate_profile_allowed_keys(spec: Mapping[str, Any], *, profile: str) -> None:
    for key in spec:
        key_text = str(key)
        if key_text not in _PROFILE_ALLOWED_KEYS:
            raise CapabilityProfileConfigError(
                f"Capability profile {profile!r} has unsupported field {key_text!r}",
                code="unknown_profile_field",
                profile=profile,
                field=f"profile.{key_text}",
            )


def _validate_no_secret_fields(obj: Any, *, profile: str, path: str = "profile") -> None:
    if isinstance(obj, Mapping):
        seen: Dict[str, str] = {}
        for key, value in obj.items():
            key_text = str(key)
            canonical = _canonical_field_key(key_text)
            if canonical:
                previous = seen.get(canonical)
                if previous is not None and previous != key_text:
                    raise CapabilityProfileConfigError(
                        f"Capability profile {profile!r} has colliding field names at {path}.{key_text}",
                        code="field_collision",
                        profile=profile,
                        field=f"{path}.{key_text}",
                    )
                seen[canonical] = key_text
            if _is_forbidden_field_key(key_text):
                raise CapabilityProfileConfigError(
                    f"Capability profile {profile!r} contains forbidden secret/env field {path}.{key_text}",
                    code="secret_field",
                    profile=profile,
                    field=f"{path}.{key_text}",
                )
            _validate_no_secret_fields(value, profile=profile, path=f"{path}.{key_text}")
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            _validate_no_secret_fields(item, profile=profile, path=f"{path}[{index}]")
    elif isinstance(obj, str) and _contains_env_interpolation(obj):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} field {path} must not contain environment interpolation",
            code="env_interpolation",
            profile=profile,
            field=path,
        )


def _validate_toolsets(toolsets: Optional[Sequence[Any]], *, profile: str, field: str) -> List[str]:
    if toolsets is None:
        return []
    if not isinstance(toolsets, (list, tuple)):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} field {field} must be a list of toolset names",
            code="invalid_toolsets",
            profile=profile,
            field=field,
        )
    valid = set(TOOLSETS)
    resolved: List[str] = []
    for item in toolsets:
        name = str(item)
        if name not in valid:
            raise CapabilityProfileConfigError(
                f"Capability profile {profile!r} references unknown toolset {name!r}",
                code="unknown_toolset",
                profile=profile,
                field=field,
            )
        resolved.append(name)
    return resolved


def _validate_approval_gates(gates: Optional[Sequence[Any]], *, profile: str) -> List[str]:
    if gates is None:
        return list(DEFAULT_APPROVAL_GATES)
    if not isinstance(gates, (list, tuple)):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} approval_gates must be a list",
            code="invalid_approval_gates",
            profile=profile,
            field="approval_gates",
        )
    safe = set(SAFE_APPROVAL_GATES)
    resolved: List[str] = []
    for item in gates:
        name = str(item)
        if name not in safe:
            raise CapabilityProfileConfigError(
                f"Capability profile {profile!r} has unsafe approval gate {name!r}",
                code="unsafe_approval_gate",
                profile=profile,
                field="approval_gates",
            )
        resolved.append(name)
    return resolved


def _fallback_metadata(fallbacks: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    providers = [str(entry.get("provider", "")) for entry in fallbacks if entry.get("provider")]
    models = [str(entry.get("model", "")) for entry in fallbacks if entry.get("model")]
    profiles = [str(entry.get("profile", "")) for entry in fallbacks if entry.get("profile")]
    return {
        "enabled": len(fallbacks) > 0,
        "count": len(fallbacks),
        "providers": providers,
        "models": models,
        "profiles": profiles,
    }


def _validate_fallbacks(
    fallbacks: Optional[Sequence[Any]],
    *,
    profile: str,
    config_profiles: Mapping[str, Any],
    valid_profiles: Sequence[str],
    allowed_toolsets: Sequence[str],
) -> List[Dict[str, Any]]:
    if fallbacks is None:
        return []
    if not isinstance(fallbacks, (list, tuple)):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} fallbacks must be a list",
            code="invalid_fallbacks",
            profile=profile,
            field="fallbacks",
        )

    valid_profile_set = set(valid_profiles)
    resolved: List[Dict[str, Any]] = []
    for index, raw in enumerate(fallbacks):
        if not isinstance(raw, Mapping):
            raise CapabilityProfileConfigError(
                f"Capability profile {profile!r} fallback #{index} must be a mapping",
                code="invalid_fallback",
                profile=profile,
                field=f"fallbacks[{index}]",
            )
        entry = copy.deepcopy(dict(raw))
        _validate_no_secret_fields(entry, profile=profile, path=f"fallbacks[{index}]")
        for key in list(entry):
            key_text = str(key)
            if key_text not in _FALLBACK_ALLOWED_KEYS:
                raise CapabilityProfileConfigError(
                    f"Capability profile {profile!r} fallback #{index} has unsupported field {key_text!r}",
                    code="unknown_fallback_field",
                    profile=profile,
                    field=f"fallbacks[{index}].{key_text}",
                )
        for key in ("provider", "model", "profile"):
            if key in entry:
                entry[key] = _plain_identifier(
                    entry.get(key),
                    profile=profile,
                    field=f"fallbacks[{index}].{key}",
                )
        if "allowed_toolsets" in entry:
            entry["allowed_toolsets"] = _validate_toolsets(
                entry.get("allowed_toolsets"),
                profile=profile,
                field=f"fallbacks[{index}].allowed_toolsets",
            )
            allowed_set = set(allowed_toolsets)
            widened = [item for item in entry["allowed_toolsets"] if item not in allowed_set]
            if widened:
                raise CapabilityProfileConfigError(
                    f"Capability profile {profile!r} fallback #{index} widens toolsets: {widened}",
                    code="toolset_widening",
                    profile=profile,
                    field=f"fallbacks[{index}].allowed_toolsets",
                )
        fallback_profile = entry.get("profile")
        if fallback_profile:
            fallback_name = str(fallback_profile)
            if fallback_name not in valid_profile_set:
                raise CapabilityProfileConfigError(
                    f"Capability profile {profile!r} fallback references unknown profile {fallback_name!r}",
                    code="unknown_profile",
                    profile=fallback_name,
                    valid_profiles=valid_profiles,
                    field=f"fallbacks[{index}].profile",
                )
            _detect_fallback_loop(
                fallback_name,
                origin=profile,
                config_profiles=config_profiles,
                valid_profiles=valid_profiles,
                stack=[profile],
            )
        resolved.append(entry)
    return resolved


def _detect_fallback_loop(
    name: str,
    *,
    origin: str,
    config_profiles: Mapping[str, Any],
    valid_profiles: Sequence[str],
    stack: List[str],
) -> None:
    if len(stack) >= _MAX_PROFILE_CHAIN_DEPTH:
        raise CapabilityProfileConfigError(
            f"Capability profile fallback chain exceeds {_MAX_PROFILE_CHAIN_DEPTH} profiles",
            code="fallback_depth",
            profile=origin,
            valid_profiles=valid_profiles,
            field="fallbacks",
        )
    if name in stack:
        raise CapabilityProfileConfigError(
            f"Capability profile fallback loop detected: {' -> '.join(stack + [name])}",
            code="fallback_loop",
            profile=origin,
            valid_profiles=valid_profiles,
            field="fallbacks",
        )
    spec = _profile_spec(name, config_profiles, valid_profiles=valid_profiles)
    fallbacks = spec.get("fallbacks") or []
    if not isinstance(fallbacks, (list, tuple)):
        return
    valid_profile_set = set(valid_profiles)
    for entry in fallbacks:
        if not isinstance(entry, Mapping):
            continue
        fallback_profile = entry.get("profile")
        if not fallback_profile:
            continue
        fallback_name = str(fallback_profile)
        if fallback_name not in valid_profile_set:
            raise CapabilityProfileConfigError(
                f"Capability profile {name!r} fallback references unknown profile {fallback_name!r}",
                code="unknown_profile",
                profile=fallback_name,
                valid_profiles=valid_profiles,
                field="fallbacks.profile",
            )
        _detect_fallback_loop(
            fallback_name,
            origin=origin,
            config_profiles=config_profiles,
            valid_profiles=valid_profiles,
            stack=stack + [name],
        )


def _normalise_prompt_sections(value: Any, *, profile: str) -> Dict[str, Any]:
    if value is None or value == "":
        value = {"recipe": "readonly-advisor"}
    if isinstance(value, str):
        value = {"recipe": value}
    if not isinstance(value, Mapping):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} prompt_sections must be a mapping or recipe name",
            code="invalid_prompt_sections",
            profile=profile,
            field="prompt_sections",
        )
    result = copy.deepcopy(dict(value))
    recipe = result.get("recipe")
    if recipe and str(recipe) not in set(list_builtin_recipes()):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} references unknown recipe {recipe!r}",
            code="unknown_recipe",
            profile=profile,
            field="prompt_sections.recipe",
        )
    return result


def _plain_identifier(value: Any, *, profile: str, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} field {field} must be a plain identifier string",
            code="invalid_identifier",
            profile=profile,
            field=field,
        )
    if _contains_env_interpolation(value):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} field {field} must not contain environment interpolation",
            code="env_interpolation",
            profile=profile,
            field=field,
        )
    if _URL_SCHEME.match(value):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} field {field} must be a provider/model identifier, not a URL",
            code="invalid_identifier",
            profile=profile,
            field=field,
        )
    if _INVALID_IDENTIFIER_CHARS.search(value):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} field {field} must be a plain identifier",
            code="invalid_identifier",
            profile=profile,
            field=field,
        )
    return value


def _merge_budget(*parts: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    budget = copy.deepcopy(DEFAULT_BUDGET)
    for part in parts:
        if isinstance(part, Mapping):
            budget.update(copy.deepcopy(dict(part)))
    return budget


def _merge_mapping(defaults: Mapping[str, Any], override: Any) -> Dict[str, Any]:
    if isinstance(override, Mapping):
        return _deep_merge(defaults, override)
    return copy.deepcopy(dict(defaults))


def _inactive_result(parent_toolsets, requested_toolsets) -> Dict[str, Any]:
    toolsets: Optional[List[str]] = None
    if parent_toolsets is not None:
        toolsets = list(parent_toolsets)
    if requested_toolsets is not None:
        base = toolsets if toolsets is not None else list(requested_toolsets)
        toolsets = _intersect_preserving_order(base, list(requested_toolsets))
    return {
        "active": False,
        "profile": "",
        "category": "",
        "responsibility": "",
        "prompt_sections": {"recipe": "readonly-advisor"},
        "provider": "",
        "model": "",
        "budget": copy.deepcopy(DEFAULT_BUDGET),
        "toolsets": toolsets,
        "allowed_toolsets": [],
        "workspace_policy": copy.deepcopy(DEFAULT_WORKSPACE_POLICY),
        "verification_policy": copy.deepcopy(DEFAULT_VERIFICATION_POLICY),
        "handoff_schema": copy.deepcopy(DEFAULT_HANDOFF_SCHEMA),
        "approval_gates": list(DEFAULT_APPROVAL_GATES),
        "fallbacks": [],
        "fallback_metadata": _fallback_metadata([]),
    }


def resolve_capability_profile(
    capabilities_config: Optional[Mapping[str, Any]],
    profile: Optional[str] = None,
    *,
    delegation_config: Optional[Mapping[str, Any]] = None,
    parent_toolsets: Optional[List[str]] = None,
    requested_toolsets: Optional[List[str]] = None,
    profile_name: Optional[str] = None,
    category_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve a capability profile into a child runtime spec.

    Args:
        capabilities_config: ``config["capabilities"]`` mapping, or full config.
        profile: explicit profile name. Empty/None uses ``default_profile``.
        delegation_config: existing ``config["delegation"]`` mapping, used only
            when a profile references a legacy category.
        parent_toolsets: upper-bound toolsets available to the parent.
        requested_toolsets: caller-requested child toolsets; another narrowing.
        profile_name: backwards-compatible alias for ``profile``.
        category_name: per-call category override; still resolved through the
            legacy category resolver, then intersected with profile toolsets.

    Returns:
        A dict containing provider/model/budget, prompt recipe reference,
        effective toolsets, policies, approval gates, and fallback metadata.

    Raises:
        CapabilityProfileConfigError on malformed or unsafe profile config.
    """
    capabilities = _normalise_capabilities_config(capabilities_config)
    raw_profiles = capabilities.get("profiles", {})
    if raw_profiles is None:
        raw_profiles = {}
    if not isinstance(raw_profiles, Mapping):
        raise CapabilityProfileConfigError(
            "Capability profiles config must be a mapping",
            code="profiles_type",
            field="profiles",
        )
    config_profiles = dict(raw_profiles)
    valid_profiles = _valid_profile_names(config_profiles)

    if profile is None and profile_name is not None:
        profile = profile_name

    name = profile or capabilities.get("default_profile") or ""
    if not name:
        return _inactive_result(parent_toolsets, requested_toolsets)
    name = str(name)

    if name not in set(valid_profiles):
        raise CapabilityProfileConfigError(
            f"Unknown capability profile {name!r}",
            code="unknown_profile",
            profile=name,
            valid_profiles=valid_profiles,
        )

    spec = _profile_spec(name, config_profiles, valid_profiles=valid_profiles)
    _validate_no_secret_fields(spec, profile=name)
    _validate_profile_allowed_keys(spec, profile=name)

    if spec.get("enabled", True) is False:
        raise CapabilityProfileConfigError(
            f"Capability profile {name!r} is disabled",
            code="disabled_profile",
            profile=name,
            valid_profiles=valid_profiles,
        )

    category_override_requested = category_name is not None
    category_value = category_name if category_override_requested else spec.get("category")
    category_name = str(category_value or "")
    category_result: Dict[str, Any] = {}
    category_toolsets: Optional[List[str]] = None
    category_fallbacks: List[Dict[str, Any]] = []
    category_exists = category_name in dict((delegation_config or {}).get("categories") or {})
    explicit_user_category = category_name and (
        category_override_requested or "category" in dict(config_profiles.get(name) or {})
    )
    if category_name and category_exists:
        try:
            category_result = resolve_delegation_category(
                dict(delegation_config or {}),
                category=category_name,
                parent_toolsets=None,
                requested_toolsets=None,
            )
        except DelegationCategoryConfigError as exc:
            raise CapabilityProfileConfigError(
                str(exc),
                code=exc.code,
                profile=name,
                valid_profiles=valid_profiles,
                field="category",
            ) from exc
        category_toolsets = list(category_result.get("toolsets") or [])
        category_fallbacks = list(category_result.get("fallback_chain") or [])
    elif category_name and explicit_user_category:
        raise CapabilityProfileConfigError(
            f"Capability profile {name!r} references unknown delegation category {category_name!r}",
            code="unknown_category",
            profile=name,
            field="category",
        )

    if "allowed_toolsets" in spec:
        allowed_toolsets = _validate_toolsets(
            spec.get("allowed_toolsets"), profile=name, field="allowed_toolsets"
        )
    elif category_toolsets is not None:
        allowed_toolsets = _validate_toolsets(category_toolsets, profile=name, field="category.toolsets")
    else:
        allowed_toolsets = []

    if category_toolsets is not None:
        allowed_toolsets = _intersect_preserving_order(allowed_toolsets, category_toolsets)

    effective_toolsets = list(allowed_toolsets)
    if parent_toolsets is not None:
        effective_toolsets = _intersect_preserving_order(effective_toolsets, list(parent_toolsets))
    if requested_toolsets is not None:
        effective_toolsets = _intersect_preserving_order(effective_toolsets, list(requested_toolsets))

    category_budget = {
        "reasoning_effort": category_result.get("reasoning_effort", ""),
        "max_iterations": category_result.get("max_iterations"),
        "child_timeout_seconds": category_result.get("child_timeout_seconds"),
    } if category_result else {}

    prompt_sections = spec.get("prompt_sections")
    if (not prompt_sections) and category_result.get("recipe"):
        prompt_sections = {"recipe": category_result.get("recipe")}

    raw_fallbacks = spec.get("fallbacks") if "fallbacks" in spec else category_fallbacks
    fallbacks = _validate_fallbacks(
        raw_fallbacks,
        profile=name,
        config_profiles=config_profiles,
        valid_profiles=valid_profiles,
        allowed_toolsets=effective_toolsets,
    )

    workspace_policy = _merge_mapping(DEFAULT_WORKSPACE_POLICY, spec.get("workspace_policy"))
    if workspace_policy.get("kind") not in _WORKSPACE_KINDS:
        raise CapabilityProfileConfigError(
            f"Capability profile {name!r} has invalid workspace kind {workspace_policy.get('kind')!r}",
            code="invalid_workspace_policy",
            profile=name,
            field="workspace_policy.kind",
        )

    verification_policy = _merge_mapping(DEFAULT_VERIFICATION_POLICY, spec.get("verification_policy"))
    if verification_policy.get("on_unverifiable") not in _VERIFICATION_ON_UNVERIFIABLE:
        raise CapabilityProfileConfigError(
            f"Capability profile {name!r} has invalid on_unverifiable value",
            code="invalid_verification_policy",
            profile=name,
            field="verification_policy.on_unverifiable",
        )

    provider = _plain_identifier(
        spec.get("provider", category_result.get("provider", "")),
        profile=name,
        field="provider",
    )
    model = _plain_identifier(
        spec.get("model", category_result.get("model", "")),
        profile=name,
        field="model",
    )

    return {
        "active": True,
        "profile": name,
        "category": category_name,
        "responsibility": str(spec.get("responsibility") or ""),
        "prompt_sections": _normalise_prompt_sections(prompt_sections, profile=name),
        "provider": provider,
        "model": model,
        "budget": _merge_budget(category_budget, spec.get("budget")),
        "toolsets": effective_toolsets,
        "allowed_toolsets": allowed_toolsets,
        "workspace_policy": workspace_policy,
        "verification_policy": verification_policy,
        "handoff_schema": _merge_mapping(DEFAULT_HANDOFF_SCHEMA, spec.get("handoff_schema")),
        "approval_gates": _validate_approval_gates(spec.get("approval_gates"), profile=name),
        "fallbacks": fallbacks,
        "fallback_metadata": _fallback_metadata(fallbacks),
    }


def _redact_prompt_text(value: Any) -> str:
    text = str(value)
    for pattern in _SECRET_VALUE_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    return text


def _format_bool(value: Any) -> str:
    return "true" if bool(value) else "false"


def _format_sequence(values: Any) -> str:
    if values is None:
        return ""
    if isinstance(values, (list, tuple)):
        return ", ".join(_redact_prompt_text(item) for item in values)
    return _redact_prompt_text(values)


def _assert_no_external_prompt_imports(
    obj: Any,
    *,
    profile: str,
    path: str = "prompt_sections",
) -> None:
    if isinstance(obj, Mapping):
        for key, value in obj.items():
            key_text = str(key)
            canonical = _canonical_field_key(key_text)
            if (
                canonical in _EXTERNAL_PROMPT_FIELD_KEYS
                or canonical.endswith("_prompt_url")
                or canonical.endswith("_prompt_path")
            ):
                raise CapabilityProfileConfigError(
                    f"Capability profile {profile!r} tries to import or copy an external prompt via {path}.{key_text}",
                    code="external_prompt_import",
                    profile=profile,
                    field=f"{path}.{key_text}",
                )
            _assert_no_external_prompt_imports(
                value,
                profile=profile,
                path=f"{path}.{key_text}",
            )
    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            _assert_no_external_prompt_imports(
                item,
                profile=profile,
                path=f"{path}[{index}]",
            )
    elif isinstance(obj, str) and _EXTERNAL_PROMPT_TEXT.search(obj):
        raise CapabilityProfileConfigError(
            f"Capability profile {profile!r} tries to import or copy an external prompt via {path}",
            code="external_prompt_import",
            profile=profile,
            field=path,
        )


def _render_budget_line(budget: Mapping[str, Any]) -> str:
    parts = []
    for key in sorted(budget):
        value = budget.get(key)
        if value not in (None, "", [], {}):
            parts.append(f"{key}={_redact_prompt_text(value)}")
    return "; ".join(parts)


def _render_handoff_schema_lines(schema: Mapping[str, Any]) -> List[str]:
    lines: List[str] = []
    evidence_blocks = schema.get("evidence_blocks")
    for key in sorted(str(k) for k in schema.keys() if str(k) != "evidence_blocks"):
        lines.append(f"- `{key}`: {_redact_prompt_text(schema.get(key))}")

    if isinstance(evidence_blocks, Mapping):
        for block_name in sorted(str(k) for k in evidence_blocks.keys()):
            raw = evidence_blocks.get(block_name)
            if isinstance(raw, Mapping):
                fields = raw.get("required") or raw.get("fields") or raw.get("sections") or []
            elif isinstance(raw, (list, tuple)):
                fields = raw
            elif raw:
                fields = [raw]
            else:
                fields = []
            rendered = _format_sequence(fields) or "not specified"
            lines.append(f"- Evidence block `{block_name}` requires: {rendered}")
    return lines


def render_capability_profile_prompt(
    resolved_profile: Mapping[str, Any],
    *,
    goal: Optional[str] = None,
    context: Optional[str] = None,
) -> str:
    """Render a resolved capability profile into a child-agent prompt fragment.

    The renderer is deterministic and local-only: it never fetches or imports
    prompt text, redacts secret-shaped values, and emits a capability contract
    rather than a persona/identity instruction so it composes with agent recipes.
    """
    if not resolved_profile or resolved_profile.get("active") is False:
        return ""

    profile = str(resolved_profile.get("profile") or "unnamed")
    prompt_sections = resolved_profile.get("prompt_sections") or {}
    _assert_no_external_prompt_imports(prompt_sections, profile=profile)

    lines: List[str] = [
        f"## Capability Profile: {_redact_prompt_text(profile)}",
        "This is a local capability contract, not a persona or identity.",
    ]

    responsibility = _redact_prompt_text(
        resolved_profile.get("responsibility") or "Use the delegated task goal as the responsibility."
    )
    lines.extend(["", "### Responsibility", responsibility])

    workspace = resolved_profile.get("workspace_policy") or {}
    toolsets_text = _format_sequence(resolved_profile.get("toolsets")) or "inherit parent scope"
    gates_text = _format_sequence(resolved_profile.get("approval_gates")) or "none"
    lines.extend(
        [
            "",
            "### Runtime Boundaries",
            f"- Effective toolsets: {toolsets_text}",
            f"- Workspace: {_redact_prompt_text(workspace.get('kind', 'scratch'))}; mutate: {_format_bool(workspace.get('mutate', False))}",
            f"- Approval gates: {gates_text}",
        ]
    )
    if resolved_profile.get("category"):
        lines.append(f"- Delegation category: {_redact_prompt_text(resolved_profile.get('category'))}")
    budget_text = _render_budget_line(resolved_profile.get("budget") or {})
    if budget_text:
        lines.append(f"- Budget: {budget_text}")

    verification = resolved_profile.get("verification_policy") or {}
    lines.extend(
        [
            "",
            "### Verification",
            f"- Require evidence: {_format_bool(verification.get('require_evidence', True))}",
            f"- On unverifiable result: {_redact_prompt_text(verification.get('on_unverifiable', 'report'))}",
        ]
    )
    commands = verification.get("commands")
    if commands:
        lines.append(f"- Suggested verification commands: {_format_sequence(commands)}")

    handoff = resolved_profile.get("handoff_schema") or {}
    lines.extend(
        [
            "",
            "### Handoff Output",
            "Return a concise final answer that satisfies this schema; if impossible, report blockers.",
        ]
    )
    lines.extend(_render_handoff_schema_lines(handoff))

    recipe = ""
    if isinstance(prompt_sections, Mapping):
        recipe = str(prompt_sections.get("recipe") or "").strip()
    if recipe:
        lines.extend(["", "### Prompt Composition", f"- Local recipe: {_redact_prompt_text(recipe)}"])

    # `goal` / `context` are accepted for future renderers but intentionally not
    # interpolated: the base child prompt already carries them, and duplicating
    # task text is cache-hostile and increases prompt-injection surface.
    _ = goal, context
    return "\n".join(lines).rstrip()



def list_builtin_capability_profiles() -> List[str]:
    """Return built-in capability profile names."""
    return sorted(_builtins())


__all__ = [
    "CapabilityProfileConfigError",
    "DEFAULT_APPROVAL_GATES",
    "SAFE_APPROVAL_GATES",
    "list_builtin_capability_profiles",
    "render_capability_profile_prompt",
    "resolve_capability_profile",
]
