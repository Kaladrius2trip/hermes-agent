"""Meridian local Claude SDK proxy provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


meridian = ProviderProfile(
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
    # Meridian's OpenAI-compatible SSE path can return HTTP 200 with an empty
    # stream when Claude Code rejects the upstream request (for example Claude
    # third-party-app extra-usage errors). Non-streaming preserves the actual
    # error JSON, so prefer it for Hermes agent calls.
    prefer_streaming=False,
    # Meridian exposes the current subscription-routable catalog at /v1/models.
    # Use that live list directly. No static fallback: a fake list can contain
    # unroutable model IDs and break selection.
    live_models_authoritative=True,
)

register_provider(meridian)
