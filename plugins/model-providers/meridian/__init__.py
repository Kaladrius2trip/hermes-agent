"""Meridian local Claude SDK proxy provider profile."""

from __future__ import annotations

from typing import Any

from providers import register_provider
from providers.base import ProviderProfile


# Release Claude models fronted by the Meridian /v1 proxy that must receive
# adaptive-summarized reasoning controls on real chat-completions requests.
# Membership is checked against the canonical dash form (see
# MeridianProfile.build_api_kwargs_extras), so dotted aliases such as
# "claude-opus-4.8" match too.
_RELEASE_REASONING_MODELS = frozenset(
    {
        "claude-sonnet-5",
        "claude-opus-4-8",
    }
)


class MeridianProfile(ProviderProfile):
    """Meridian profile that emits reasoning controls for release Claude models.

    Meridian speaks the OpenAI-compatible chat-completions wire, so Hermes'
    native Anthropic thinking mapping never runs for it. This override injects
    the equivalent adaptive-summarized reasoning shape at the transport
    boundary: a top-level ``reasoning_effort`` paired with
    ``extra_body.thinking = {"type": "adaptive", "display": "summarized"}``.
    """

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict[str, Any] | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Return ``(extra_body_additions, top_level_kwargs)`` for the transport.

        Emits controls only for a release Claude model with reasoning enabled
        and an explicit effort. Every other model, or reasoning disabled/absent,
        returns ``({}, {})`` so no reasoning controls are sent.
        """
        raw_model = context.get("model")
        model = raw_model.strip().lower() if isinstance(raw_model, str) else ""
        if ":" in model:
            model = model.split(":", 1)[1]
        if "/" in model:
            model = model.rsplit("/", 1)[1]
        model = model.replace(".", "-")
        if model not in _RELEASE_REASONING_MODELS:
            return {}, {}

        effort: str | None = None
        if (
            isinstance(reasoning_config, dict)
            and reasoning_config.get("enabled", True) is not False
        ):
            raw_effort = reasoning_config.get("effort")
            if isinstance(raw_effort, str) and raw_effort.strip():
                effort = raw_effort.strip()
        if effort is None:
            return {}, {}

        extra_body_additions: dict[str, Any] = {
            "thinking": {"type": "adaptive", "display": "summarized"},
        }
        top_level_kwargs: dict[str, Any] = {"reasoning_effort": effort}
        return extra_body_additions, top_level_kwargs


meridian = MeridianProfile(
    name="meridian",
    aliases=(
        "meridian-claude",
        "meridian-proxy",
        "claude-meridian",
    ),
    display_name="Meridian",
    description="Meridian local Claude SDK proxy (OpenAI-compatible /v1)",
    env_vars=("MERIDIAN_API_KEY", "MERIDIAN_BASE_URL"),
    base_url="http://127.0.0.1:3456/v1",
    models_url="http://127.0.0.1:3456/v1/models",
    auth_type="api_key",
    supports_health_check=True,
    default_aux_model="claude-haiku-4-5",
    # Streaming exposes Anthropic thinking_delta events so Hermes can render
    # Sonnet reasoning live instead of only after the turn completes.
    prefer_streaming=True,
    # Meridian exposes the current subscription-routable catalog at /v1/models.
    # Use that live list directly. No static fallback: a fake list can contain
    # unroutable model IDs and break selection.
    live_models_authoritative=True,
    default_headers={"x-meridian-agent": "hermes"},
)

register_provider(meridian)
