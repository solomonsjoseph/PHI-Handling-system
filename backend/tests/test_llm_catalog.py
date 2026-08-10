"""Tests for the multi-provider LLM catalog + family resolution."""
from __future__ import annotations

import pytest


def test_catalog_shape():
    from phi_core.llm_catalog import catalog_for_ui
    payload = catalog_for_ui()
    assert "providers" in payload
    assert "models" in payload
    assert "default_model_id" in payload
    # Every provider family declares whether web_search is available
    for p in payload["providers"]:
        assert "id" in p and "label" in p
        assert "web_search_available" in p
        assert "via_emergent_key" in p
    # Every model row carries the fields the UI depends on
    for m in payload["models"]:
        for key in ("id", "label", "provider_family", "tier",
                    "supports_web_search", "via_emergent_key"):
            assert key in m, f"model {m!r} missing key {key!r}"


def test_catalog_covers_the_three_provider_families_reachable_via_emergent_key():
    """Anthropic + OpenAI + Gemini must all be reachable through the
    Emergent Universal Key so the operator has a real menu to pick from."""
    from phi_core.llm_catalog import CATALOG
    families_via_emergent = {
        m["provider_family"] for m in CATALOG if m["via_emergent_key"]
    }
    assert {"anthropic", "openai", "gemini"} <= families_via_emergent


@pytest.mark.parametrize("model_id,expected_family", [
    ("claude-sonnet-4-5-20250929", "anthropic"),
    ("claude-opus-4-20250929", "anthropic"),
    ("gpt-5.2", "openai"),
    ("gpt-5.4-mini", "openai"),
    ("gemini-3-pro", "gemini"),
    ("gemini-3-flash", "gemini"),
])
def test_family_resolution_from_model_id_via_emergent(model_id, expected_family):
    from phi_core.llm_catalog import resolve_family
    assert resolve_family("emergent", model_id) == expected_family


def test_family_resolution_falls_back_to_anthropic_for_unknown_model():
    """Emergent + unknown model id -> default to anthropic (Claude is the
    canonical Emergent-key target)."""
    from phi_core.llm_catalog import resolve_family
    assert resolve_family("emergent", "some-future-model-id") == "anthropic"


def test_family_resolution_byok_provider_maps_to_itself():
    from phi_core.llm_catalog import resolve_family
    assert resolve_family("anthropic", "claude-opus-4-20250929") == "anthropic"
    assert resolve_family("openai", "gpt-5.2") == "openai"
    assert resolve_family("gemini", "gemini-3-pro") == "gemini"
    assert resolve_family("openrouter", "openrouter/anthropic/claude-sonnet-4.5") == "openrouter"


def test_web_search_tool_selection_per_family():
    from phi_core.llm_catalog import web_search_tool_for
    assert web_search_tool_for("anthropic") == {
        "type": "web_search_20250305", "name": "web_search"
    }
    assert web_search_tool_for("gemini") == {"googleSearch": {}}
    # OpenAI + others do not expose a native web_search tool through the
    # Emergent proxy today; the caller falls back to a plain LLM call.
    assert web_search_tool_for("openai") is None
    assert web_search_tool_for("openrouter") is None
    assert web_search_tool_for("bogus-family") is None
