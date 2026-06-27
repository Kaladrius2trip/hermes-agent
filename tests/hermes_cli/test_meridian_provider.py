"""Focused tests for Meridian first-class provider wiring."""

from __future__ import annotations

from providers import get_provider_profile
from hermes_cli.auth import (
    MERIDIAN_NOAUTH_PLACEHOLDER,
    PROVIDER_REGISTRY,
    get_auth_status,
    resolve_api_key_provider_credentials,
)
from hermes_cli.models import (
    CANONICAL_PROVIDERS,
    _PROVIDER_LABELS,
    _PROVIDER_MODELS,
    get_default_model_for_provider,
    normalize_provider,
    parse_model_input,
    provider_model_ids,
)
from hermes_cli.providers import (
    HERMES_OVERLAYS,
    determine_api_mode,
    normalize_provider as normalize_provider_in_providers,
    resolve_provider_full,
)


def test_meridian_provider_profile_registered():
    profile = get_provider_profile("meridian")

    assert profile is not None
    assert profile.name == "meridian"
    assert profile.api_mode == "chat_completions"
    assert profile.base_url == "http://127.0.0.1:3456/v1"
    assert profile.models_url == "http://127.0.0.1:3456/v1/models"
    assert profile.env_vars == ("MERIDIAN_API_KEY", "MERIDIAN_BASE_URL")
    assert profile.default_aux_model == "claude-haiku-4-5"
    assert profile.fallback_models[0] == "claude-opus-4-8"


def test_meridian_aliases_resolve_in_profile_and_cli_layers():
    profile = get_provider_profile("meridian-claude")
    assert profile is not None
    assert profile.name == "meridian"
    assert normalize_provider("meridian-proxy") == "meridian"
    assert normalize_provider_in_providers("claude-meridian") == "meridian"


def test_meridian_auth_registry_uses_local_no_auth_placeholder(monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.delenv("MERIDIAN_BASE_URL", raising=False)

    pconfig = PROVIDER_REGISTRY["meridian"]
    assert pconfig.auth_type == "api_key"
    assert pconfig.inference_base_url == "http://127.0.0.1:3456/v1"
    assert pconfig.api_key_env_vars == ("MERIDIAN_API_KEY",)
    assert pconfig.base_url_env_var == "MERIDIAN_BASE_URL"

    creds = resolve_api_key_provider_credentials("meridian")
    assert creds["api_key"] == MERIDIAN_NOAUTH_PLACEHOLDER
    assert creds["base_url"] == "http://127.0.0.1:3456/v1"
    assert creds["source"] == "default"

    status = get_auth_status("meridian")
    assert status["configured"] is True
    assert status["logged_in"] is True
    assert status["base_url"] == "http://127.0.0.1:3456/v1"


def test_meridian_base_url_env_override(monkeypatch):
    monkeypatch.delenv("MERIDIAN_API_KEY", raising=False)
    monkeypatch.setenv("MERIDIAN_BASE_URL", "http://127.0.0.1:4567/v1/")

    creds = resolve_api_key_provider_credentials("meridian")
    assert creds["api_key"] == MERIDIAN_NOAUTH_PLACEHOLDER
    assert creds["base_url"] == "http://127.0.0.1:4567/v1"


def test_meridian_model_catalog_and_default(monkeypatch):
    profile = get_provider_profile("meridian")
    assert profile is not None
    monkeypatch.setattr(profile, "fetch_models", lambda api_key=None, timeout=8.0: None)

    assert "meridian" in [p.slug for p in CANONICAL_PROVIDERS]
    assert _PROVIDER_LABELS["meridian"] == "Meridian"
    assert _PROVIDER_MODELS["meridian"][0] == "claude-opus-4-8"
    assert get_default_model_for_provider("meridian") == "claude-opus-4-8"
    assert provider_model_ids("meridian") == list(_PROVIDER_MODELS["meridian"])


def test_meridian_model_input_provider_prefix():
    assert parse_model_input("meridian:claude-opus-4-8", "openrouter") == (
        "meridian",
        "claude-opus-4-8",
    )


def test_meridian_providers_overlay_and_resolution():
    overlay = HERMES_OVERLAYS["meridian"]
    assert overlay.transport == "openai_chat"
    assert overlay.extra_env_vars == ("MERIDIAN_API_KEY",)
    assert overlay.base_url_override == "http://127.0.0.1:3456/v1"
    assert overlay.base_url_env_var == "MERIDIAN_BASE_URL"

    pdef = resolve_provider_full("meridian")
    assert pdef is not None
    assert pdef.id == "meridian"
    assert pdef.name == "Meridian"
    assert pdef.transport == "openai_chat"
    assert pdef.base_url == "http://127.0.0.1:3456/v1"
    assert pdef.source == "hermes"
    assert determine_api_mode("meridian", pdef.base_url) == "chat_completions"
