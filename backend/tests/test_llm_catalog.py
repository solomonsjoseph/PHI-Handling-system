"""Tests for the multi-provider LLM catalog + family resolution."""
from __future__ import annotations

import pytest


def test_catalog_shape():
    from phi_core.llm_catalog import catalog_for_ui
    payload = catalog_for_ui()
    assert "providers" in payload
    assert "models" in payload
    assert "default_model_id" in payload
    # Settings advertises exactly the four UI providers, in order.
    assert [p["id"] for p in payload["providers"]] == [
        "openrouter", "openai", "anthropic", "gemini",
    ]
    for p in payload["providers"]:
        assert "id" in p and "label" in p
        assert "web_search_available" in p
    for m in payload["models"]:
        for key in ("id", "label", "provider_family", "tier",
                    "supports_web_search"):
            assert key in m, f"model {m!r} missing key {key!r}"


def test_catalog_covers_ui_provider_families():
    """Every Settings provider must have at least one flagship and one fast model."""
    from phi_core.llm_catalog import CATALOG, UI_PROVIDERS
    for fam, _label in UI_PROVIDERS:
        rows = [m for m in CATALOG if m["provider_family"] == fam]
        assert rows, f"no models for {fam}"
        tiers = {m["tier"] for m in rows}
        assert "flagship" in tiers or "balanced" in tiers
        assert "fast" in tiers


def test_default_model_for_each_ui_provider():
    from phi_core.llm_catalog import UI_PROVIDERS, default_model_for, CATALOG
    ids = {m["id"] for m in CATALOG}
    for fam, _ in UI_PROVIDERS:
        mid = default_model_for(fam)
        assert mid in ids
        assert any(m["id"] == mid and m["provider_family"] == fam for m in CATALOG)


def test_family_resolution_byok_provider_maps_to_itself():
    from phi_core.llm_catalog import resolve_family
    assert resolve_family("anthropic", "claude-opus-5") == "anthropic"
    assert resolve_family("openai", "gpt-5.6-terra") == "openai"
    assert resolve_family("gemini", "gemini-3.1-pro-preview") == "gemini"
    assert resolve_family("openrouter", "openrouter/anthropic/claude-sonnet-5") == "openrouter"


def test_web_search_tool_selection_per_family():
    from phi_core.llm_catalog import web_search_tool_for
    assert web_search_tool_for("anthropic") == {
        "type": "web_search_20250305", "name": "web_search"
    }
    assert web_search_tool_for("gemini") == {"googleSearch": {}}
    assert web_search_tool_for("openai") is None
    assert web_search_tool_for("openrouter") is None
    assert web_search_tool_for("bogus-family") is None


@pytest.mark.parametrize(
    ("environment", "expected"),
    [
        ({"OPENROUTER_API_KEY": "key"}, "openrouter"),
        ({"OPENAI_API_KEY": "key"}, "openai"),
        ({"ANTHROPIC_API_KEY": "key"}, "anthropic"),
        ({"GEMINI_API_KEY": "key"}, "gemini"),
    ],
)
def test_environment_key_selects_provider_without_selecting_model(
    monkeypatch, environment, expected
):
    from phi_core.agents.llm import LlmConfig, _default_provider

    for key in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in environment.items():
        monkeypatch.setenv(key, value)

    assert _default_provider() == expected
    with pytest.raises(ValueError, match="select a model"):
        LlmConfig.from_dict({})


def test_explicit_model_configuration_is_preserved():
    from phi_core.agents.llm import LlmConfig

    cfg = LlmConfig.from_dict({"provider": "openai", "model": "gpt-5.6-terra"})
    assert (cfg.provider, cfg.model) == ("openai", "gpt-5.6-terra")
