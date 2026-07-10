from __future__ import annotations

from typing import Final

from hermes_cli.model_normalize import normalize_model_for_provider

_DEFAULT_EFFORT: Final[str] = "medium"
_MINIMAL_ALIAS: Final[str] = "minimal"
_NORMALIZED_MINIMAL: Final[str] = "low"

_GPT56_FULL_EFFORTS: Final[tuple[str, ...]] = (
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)

_GPT56_LUNA_EFFORTS: Final[tuple[str, ...]] = _GPT56_FULL_EFFORTS[:-1]

_GPT56_MODEL_EFFORTS: Final[dict[str, tuple[str, ...]]] = {
    "gpt-5.6-sol": _GPT56_FULL_EFFORTS,
    "gpt-5.6-terra": _GPT56_FULL_EFFORTS,
    "gpt-5.6-luna": _GPT56_LUNA_EFFORTS,
}

# User-facing ``ultra`` selection maps to the Codex backend wire value ``max``.
# Codex exposes no ``ultra`` wire mode; the official client sends Max for an
# Ultra request. Applied only after a model accepts ``ultra`` (Sol/Terra), so
# Luna (which rejects ``ultra`` above) never reaches this map, and non-GPT-5.6
# models (early return) keep their literal effort.
_GPT56_WIRE_EFFORT_OVERRIDES: Final[dict[str, str]] = {"ultra": "max"}


def _normalize_codex_model_id(model: str) -> str:
    return normalize_model_for_provider(model, "openai-codex").strip().lower()


def _normalize_codex_effort(effort: str) -> str:
    normalized = effort.strip().lower()
    if normalized == _MINIMAL_ALIAS:
        return _NORMALIZED_MINIMAL
    if not normalized:
        return _DEFAULT_EFFORT
    return normalized


def resolve_codex_reasoning_effort(model: str, effort: str) -> str:
    """Return the Codex wire reasoning effort for a model.

    GPT-5.6 Sol/Terra accept ``low``, ``medium``, ``high``, ``xhigh``,
    ``max``, and ``ultra`` as user-facing selections. The accepted ``ultra``
    selection is sent on the wire as ``max``: Codex exposes no ``ultra`` wire
    mode, so the official Codex client maps an Ultra request to Max
    (``reasoning_effort_for_request``). Luna accepts everything except
    ``ultra`` and rejects it locally before any network call. Other Codex
    models keep existing behavior, with ``minimal`` normalized to ``low``.
    """
    normalized_model = _normalize_codex_model_id(model)
    normalized_effort = _normalize_codex_effort(effort)

    allowed = _GPT56_MODEL_EFFORTS.get(normalized_model)
    if allowed is None:
        return normalized_effort

    if normalized_effort not in allowed:
        allowed_text = ", ".join(allowed)
        raise ValueError(
            f"{normalized_model} does not support reasoning_effort={normalized_effort}; "
            f"allowed efforts: {allowed_text}"
        )

    return _GPT56_WIRE_EFFORT_OVERRIDES.get(normalized_effort, normalized_effort)
