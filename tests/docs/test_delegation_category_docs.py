from pathlib import Path

import yaml

from tools.delegation_categories import resolve_delegation_category

ROOT = Path(__file__).resolve().parents[2]
PRESET = ROOT / "docs" / "config" / "delegation-category-presets.yaml"
DELEGATION_DOC = ROOT / "website" / "docs" / "user-guide" / "features" / "delegation.md"
CONFIG_DOC = ROOT / "website" / "docs" / "user-guide" / "configuration.md"
CLI_EXAMPLE = ROOT / "cli-config.yaml.example"

REQUIRED_CATEGORIES = {"quick", "deep", "review", "visual", "writing"}


def test_delegation_category_preset_is_parseable_and_safe():
    config = yaml.safe_load(PRESET.read_text(encoding="utf-8"))

    assert sorted(config) == ["delegation"]
    delegation = config["delegation"]
    assert delegation["default_category"] == "quick"
    assert REQUIRED_CATEGORIES <= set(delegation["categories"])

    serialized = PRESET.read_text(encoding="utf-8")
    assert "[REDACTED]" in serialized
    assert "api_key:" not in serialized
    assert "password:" not in serialized
    assert "telemetry" not in serialized.lower()
    assert "posthog" not in serialized.lower()

    parent_toolsets = ["file", "terminal", "web", "browser", "vision", "search"]
    for name, spec in delegation["categories"].items():
        assert spec.get("toolsets_mode") == "intersect"
        resolved = resolve_delegation_category(delegation, name, parent_toolsets=parent_toolsets)
        assert set(resolved["toolsets"]) <= set(parent_toolsets)
        assert resolved["category"] == name

    review = resolve_delegation_category(delegation, "review", parent_toolsets=parent_toolsets)
    assert "terminal" not in review["toolsets"]
    assert review["recipe"] == "critic-reviewer"


def test_delegation_category_docs_cover_migration_and_troubleshooting():
    doc = DELEGATION_DOC.read_text(encoding="utf-8")
    for phrase in [
        "Capability categories",
        "Safe category presets",
        "Migration from ad-hoc delegation prompts",
        "Clean-room notice",
        "default_category",
        "toolsets_mode: intersect",
        "fallback_chain",
        "recipe",
        "quick",
        "deep",
        "review",
        "visual",
        "writing",
        "Troubleshooting category routing",
    ]:
        assert phrase in doc


def test_configuration_reference_and_example_show_category_keys():
    config_doc = CONFIG_DOC.read_text(encoding="utf-8")
    cli_example = CLI_EXAMPLE.read_text(encoding="utf-8")

    for text in (config_doc, cli_example):
        assert "default_category" in text
        assert "categories:" in text
        assert "toolsets_mode: intersect" in text
        assert "recipe:" in text
        assert "fallback_chain" in text
