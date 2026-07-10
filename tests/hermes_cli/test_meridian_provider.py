"""Focused tests for Meridian first-class provider wiring."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from agent.transports.chat_completions import ChatCompletionsTransport
from providers import get_provider_profile
from providers.base import ProviderProfile
from run_agent import AIAgent
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
    cached_provider_model_ids,
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
    assert profile.fallback_models == ()
    assert profile.prefer_streaming is True
    assert profile.live_models_authoritative is True
    assert profile.default_headers == {"x-meridian-agent": "hermes"}


@patch("run_agent.OpenAI")
def test_meridian_primary_client_sends_agent_identity_header(mock_openai):
    mock_openai.return_value = MagicMock()

    agent = AIAgent(
        api_key="meridian-local",
        base_url="http://127.0.0.1:3456/v1",
        model="claude-haiku-4-5",
        provider="meridian",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    headers = getattr(agent, "_client_kwargs")["default_headers"]
    assert headers["x-meridian-agent"] == "hermes"


@patch("run_agent.OpenAI")
def test_meridian_primary_client_uses_stable_conversation_session_header(mock_openai):
    mock_openai.return_value = MagicMock()

    # Given: two Meridian conversations and one non-Meridian conversation.
    first_agent = AIAgent(
        api_key="meridian-local",
        base_url="http://127.0.0.1:3456/v1",
        model="claude-haiku-4-5",
        provider="meridian",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    second_agent = AIAgent(
        api_key="meridian-local",
        base_url="http://127.0.0.1:3456/v1",
        model="claude-haiku-4-5",
        provider="meridian",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    other_agent = AIAgent(
        api_key="other-local",
        base_url="http://127.0.0.1:4567/v1",
        model="other-model",
        provider="custom",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )

    # When: the first Meridian client's headers are rebuilt for another turn.
    first_headers = getattr(first_agent, "_client_kwargs")["default_headers"]
    first_session = first_headers["x-hermes-session"]
    first_agent._apply_client_headers_for_base_url(first_agent.base_url)
    rebuilt_headers = getattr(first_agent, "_client_kwargs")["default_headers"]
    second_headers = getattr(second_agent, "_client_kwargs")["default_headers"]
    other_headers = getattr(other_agent, "_client_kwargs").get("default_headers", {})

    # Then: Meridian gets a non-empty stable per-instance session, and others do not.
    assert first_session
    assert rebuilt_headers["x-hermes-session"] == first_session
    assert second_headers["x-hermes-session"]
    assert second_headers["x-hermes-session"] != first_session
    assert "x-hermes-session" not in other_headers


def test_meridian_auxiliary_client_sends_agent_identity_header():
    with patch("agent.auxiliary_client.OpenAI") as mock_openai:
        mock_openai.return_value = MagicMock()
        from agent.auxiliary_client import resolve_provider_client

        client, model = resolve_provider_client("meridian", "claude-haiku-4-5")

    assert client is not None
    assert model == "claude-haiku-4-5"
    headers = mock_openai.call_args.kwargs.get("default_headers", {}) or {}
    assert headers["x-meridian-agent"] == "hermes"


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
    monkeypatch.setattr(
        profile,
        "fetch_models",
        lambda api_key=None, base_url=None, timeout=8.0: ["live-a", "live-b"],
    )

    assert "meridian" in [p.slug for p in CANONICAL_PROVIDERS]
    assert _PROVIDER_LABELS["meridian"] == "Meridian"
    assert _PROVIDER_MODELS["meridian"] == []
    assert get_default_model_for_provider("meridian") == "live-a"
    assert provider_model_ids("meridian") == ["live-a", "live-b"]


def test_meridian_no_static_or_stale_fallback_when_live_unavailable(monkeypatch):
    profile = get_provider_profile("meridian")
    assert profile is not None

    monkeypatch.setattr(
        profile,
        "fetch_models",
        lambda api_key=None, base_url=None, timeout=8.0: ["live-a"],
    )
    assert cached_provider_model_ids("meridian", force_refresh=True) == ["live-a"]

    monkeypatch.setattr(
        profile,
        "fetch_models",
        lambda api_key=None, base_url=None, timeout=8.0: None,
    )
    assert provider_model_ids("meridian") == []
    assert cached_provider_model_ids("meridian") == []
    assert get_default_model_for_provider("meridian") == ""


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


# ── Reasoning controls on real ChatCompletionsTransport requests (C4.2) ──
#
# Meridian speaks the OpenAI-compatible chat-completions wire, so the native
# Anthropic thinking mapping never runs. For release Claude models the Meridian
# profile must inject the adaptive-summarized reasoning shape at the transport
# boundary: a top-level ``reasoning_effort`` plus
# ``extra_body.thinking = {"type": "adaptive", "display": "summarized"}``.


def _reasoning_messages() -> list[dict[str, str]]:
    return [{"role": "user", "content": "ping"}]


def test_meridian_release_model_emits_adaptive_reasoning_controls():
    # Given: the real chat-completions transport, the Meridian profile, a
    # release Claude model, and reasoning enabled at high effort.
    transport = ChatCompletionsTransport()

    # When: the transport assembles the real request kwargs.
    kwargs = transport.build_kwargs(
        model="claude-opus-4-8",
        messages=_reasoning_messages(),
        tools=None,
        provider_profile=get_provider_profile("meridian"),
        reasoning_config={"enabled": True, "effort": "high"},
    )

    # Then: a top-level reasoning_effort paired with adaptive-summarized thinking.
    assert kwargs.get("reasoning_effort") == "high"
    assert kwargs["extra_body"]["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }


@pytest.mark.parametrize(
    "model",
    ["claude-sonnet-5", "claude-opus-4-8", "claude-opus-4.8"],
)
def test_meridian_release_models_and_dot_alias_emit_reasoning(model):
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model=model,
        messages=_reasoning_messages(),
        tools=None,
        provider_profile=get_provider_profile("meridian"),
        reasoning_config={"enabled": True, "effort": "high"},
    )

    assert kwargs.get("reasoning_effort") == "high"
    assert kwargs["extra_body"]["thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }


def test_meridian_reasoning_disabled_emits_no_controls():
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="claude-opus-4-8",
        messages=_reasoning_messages(),
        tools=None,
        provider_profile=get_provider_profile("meridian"),
        reasoning_config={"enabled": False, "effort": "high"},
    )

    assert "reasoning_effort" not in kwargs
    assert "thinking" not in kwargs.get("extra_body", {})


def test_meridian_absent_reasoning_config_emits_no_controls():
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="claude-opus-4-8",
        messages=_reasoning_messages(),
        tools=None,
        provider_profile=get_provider_profile("meridian"),
    )

    assert "reasoning_effort" not in kwargs
    assert "thinking" not in kwargs.get("extra_body", {})


def test_meridian_non_release_model_emits_no_controls():
    # The Haiku aux model routed through Meridian must not get reasoning controls.
    transport = ChatCompletionsTransport()

    kwargs = transport.build_kwargs(
        model="claude-haiku-4-5",
        messages=_reasoning_messages(),
        tools=None,
        provider_profile=get_provider_profile("meridian"),
        reasoning_config={"enabled": True, "effort": "high"},
    )

    assert "reasoning_effort" not in kwargs
    assert "thinking" not in kwargs.get("extra_body", {})


def test_base_profile_contract_unchanged_for_other_providers():
    # Other providers use the base contract, which never emits reasoning
    # controls — proving the Meridian change is scoped to its own profile.
    base = ProviderProfile(name="plain")

    extra_body, top_level = base.build_api_kwargs_extras(
        reasoning_config={"enabled": True, "effort": "high"},
        model="claude-opus-4-8",
    )

    assert extra_body == {}
    assert top_level == {}
