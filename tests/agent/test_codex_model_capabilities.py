from __future__ import annotations

import pytest

from agent.codex_model_capabilities import resolve_codex_reasoning_effort


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        pytest.param("gpt-5.6-sol", "low", "low", id="sol-low"),
        pytest.param("gpt-5.6-sol", "ultra", "max", id="sol-ultra-maps-to-max"),
        pytest.param("gpt-5.6-terra", "ultra", "max", id="terra-ultra-maps-to-max"),
        pytest.param("gpt-5.6-terra", "xhigh", "xhigh", id="terra-xhigh"),
        pytest.param("gpt-5.6-luna", "max", "max", id="luna-max"),
    ],
)
def test_resolve_codex_reasoning_effort_accepts_matrix_edges(
    model: str,
    effort: str,
    expected: str,
) -> None:
    # Given: GPT-5.6 Codex model and supported effort edge.
    # When: resolver normalizes effort for model.
    resolved = resolve_codex_reasoning_effort(model, effort)
    # Then: edge maps to the expected Codex wire effort.
    assert resolved == expected


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        pytest.param("gpt-5.6-sol", "minimal", "low", id="sol-minimal"),
        pytest.param("openai/gpt-5.6-terra", "minimal", "low", id="terra-openai-prefix-minimal"),
    ],
)
def test_resolve_codex_reasoning_effort_maps_minimal_to_low(
    model: str,
    effort: str,
    expected: str,
) -> None:
    # Given: GPT-5.6 Codex model and legacy minimal effort.
    # When: resolver normalizes effort for model.
    resolved = resolve_codex_reasoning_effort(model, effort)
    # Then: minimal collapses to low.
    assert resolved == expected


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("gpt-5.6-luna", id="bare-luna"),
        pytest.param("openai/gpt-5.6-luna", id="vendor-prefixed-luna"),
        pytest.param("openai-codex/gpt-5.6-luna", id="provider-prefixed-luna"),
    ],
)
def test_resolve_codex_reasoning_effort_rejects_ultra_for_luna(model: str) -> None:
    # Given: Luna model and unsupported ultra effort.
    # When / Then: resolver raises local ValueError.
    with pytest.raises(
        ValueError,
        match=r"gpt-5\.6-luna.*ultra.*low, medium, high, xhigh, max",
    ):
        resolve_codex_reasoning_effort(model, "ultra")


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        pytest.param("openai/gpt-5.6-sol", "high", "high", id="openai-prefix-sol"),
        pytest.param("openai-codex/gpt-5.6-terra", "max", "max", id="provider-prefix-terra"),
    ],
)
def test_resolve_codex_reasoning_effort_handles_existing_codex_prefix_forms(
    model: str,
    effort: str,
    expected: str,
) -> None:
    # Given: model string with existing Codex provider/vendor prefix form.
    # When: resolver normalizes model identifier.
    resolved = resolve_codex_reasoning_effort(model, effort)
    # Then: prefix form resolves exactly like bare Codex model id.
    assert resolved == expected


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        pytest.param("gpt-5.5", "minimal", "low", id="legacy-minimal"),
        pytest.param("openai-codex/gpt-5.5", "ultra", "ultra", id="legacy-provider-prefix"),
    ],
)
def test_resolve_codex_reasoning_effort_keeps_non_gpt56_behavior(
    model: str,
    effort: str,
    expected: str,
) -> None:
    # Given: non-GPT-5.6 Codex-compatible model.
    # When: resolver handles effort.
    resolved = resolve_codex_reasoning_effort(model, effort)
    # Then: existing behavior stays intact outside GPT-5.6 matrix.
    assert resolved == expected
