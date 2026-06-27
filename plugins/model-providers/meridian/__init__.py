"""Meridian local Claude SDK proxy provider profile."""

from providers import register_provider
from providers.base import ProviderProfile


MERIDIAN_MODELS = (
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)


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
    fallback_models=MERIDIAN_MODELS,
)

register_provider(meridian)
