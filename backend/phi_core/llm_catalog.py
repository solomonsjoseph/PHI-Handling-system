"""Curated multi-provider LLM model catalog.

The Wizard/Settings UI should present operators with a picker of REAL,
current model IDs (grouped by provider) rather than a free-form text box
where a typo means every agent silently switches to a stale default.

Each entry declares:
  * ``id``   — model ID accepted by the underlying provider SDK
  * ``label``— human-readable display name
  * ``provider`` — one of emergent|anthropic|openai|gemini|openrouter
  * ``tier``  — flagship|balanced|fast|reasoning (rough tier for the UI)
  * ``supports_web_search`` — whether the provider exposes a native
    web-search tool that the agent layer can invoke. True today for
    Anthropic (``web_search_20250305``) and Gemini (``googleSearch``).
    OpenAI's web_search is only available via the Responses API path
    which the Emergent proxy does not currently plumb through, so it is
    marked False and the agent falls back to LLM-only knowledge.
  * ``via_emergent_key`` — usable through the Emergent Universal Key
    (zero-setup). Otherwise a BYOK is required.

Kept small and hand-curated: showing 60 models overwhelms the operator,
and stale entries erode trust. Add rows here when new flagship models ship.
"""
from __future__ import annotations

from typing import Any


CATALOG: list[dict[str, Any]] = [
    # ---- Anthropic (Claude) --------------------------------------------
    {
        "id": "claude-sonnet-4-5-20250929",
        "label": "Claude Sonnet 4.5",
        "provider_family": "anthropic",
        "tier": "balanced",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Default. Best regulation-fidelity trade-off; native web search.",
    },
    {
        "id": "claude-opus-4-20250929",
        "label": "Claude Opus 4",
        "provider_family": "anthropic",
        "tier": "flagship",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Highest reasoning quality; recommended for adversarial PHI.",
    },
    {
        "id": "claude-haiku-4-5-20250929",
        "label": "Claude Haiku 4.5",
        "provider_family": "anthropic",
        "tier": "fast",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Fastest / cheapest Claude. Good for high-throughput classification.",
    },

    # ---- OpenAI (GPT) --------------------------------------------------
    {
        "id": "gpt-5.2",
        "label": "GPT-5.2",
        "provider_family": "openai",
        "tier": "balanced",
        "supports_web_search": False,
        "via_emergent_key": True,
        "notes": "Latest general-purpose GPT via Emergent proxy.",
    },
    {
        "id": "gpt-5.4",
        "label": "GPT-5.4",
        "provider_family": "openai",
        "tier": "flagship",
        "supports_web_search": False,
        "via_emergent_key": True,
        "notes": "Highest-quality GPT; slower / more expensive.",
    },
    {
        "id": "gpt-5.4-mini",
        "label": "GPT-5.4 Mini",
        "provider_family": "openai",
        "tier": "fast",
        "supports_web_search": False,
        "via_emergent_key": True,
        "notes": "Fast, cheap GPT for high-throughput classification.",
    },

    # ---- Google (Gemini) -----------------------------------------------
    {
        "id": "gemini-3-pro",
        "label": "Gemini 3 Pro",
        "provider_family": "gemini",
        "tier": "flagship",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Google Search grounding available. Strong reasoning.",
    },
    {
        "id": "gemini-3-flash",
        "label": "Gemini 3 Flash",
        "provider_family": "gemini",
        "tier": "fast",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Fast, cheap Gemini with native Google Search grounding.",
    },

    # ---- OpenRouter (BYOK, any model) ----------------------------------
    {
        "id": "openrouter/anthropic/claude-sonnet-4.5",
        "label": "OpenRouter → Claude Sonnet 4.5",
        "provider_family": "openrouter",
        "tier": "balanced",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "BYOK required. No web-search tool through OpenRouter proxy.",
    },
    {
        "id": "openrouter/openai/gpt-5.2",
        "label": "OpenRouter → GPT-5.2",
        "provider_family": "openrouter",
        "tier": "balanced",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "BYOK required.",
    },
]


PROVIDER_FAMILIES: dict[str, dict[str, Any]] = {
    "anthropic": {
        "label": "Anthropic — Claude",
        "web_search_tool": {"type": "web_search_20250305",
                            "name": "web_search"},
    },
    "openai": {
        "label": "OpenAI — GPT",
        "web_search_tool": None,  # not exposed through Emergent proxy today
    },
    "gemini": {
        "label": "Google — Gemini",
        "web_search_tool": {"googleSearch": {}},
    },
    "openrouter": {
        "label": "OpenRouter — any model, one key",
        "web_search_tool": None,
    },
    "openai_compatible": {
        "label": "Custom OpenAI-compatible endpoint",
        "web_search_tool": None,
    },
}


def catalog_for_ui() -> dict[str, Any]:
    """Payload for ``GET /api/settings/llm/catalog``.

    Returns:
        {
          "providers": [
             {"id": "anthropic", "label": "Anthropic — Claude",
              "via_emergent_key": True, "supports_web_search": True},
             ...
          ],
          "models": [ ... CATALOG ... ],
          "default_model_id": "claude-sonnet-4-5-20250929",
        }
    """
    providers = [
        {
            "id": fam,
            "label": PROVIDER_FAMILIES[fam]["label"],
            "web_search_available": PROVIDER_FAMILIES[fam]["web_search_tool"] is not None,
            "via_emergent_key": any(
                m["provider_family"] == fam and m["via_emergent_key"] for m in CATALOG
            ),
        }
        for fam in PROVIDER_FAMILIES
    ]
    return {
        "providers": providers,
        "models": CATALOG,
        "default_model_id": "claude-sonnet-4-5-20250929",
    }


def web_search_tool_for(provider_family: str) -> dict[str, Any] | None:
    """Return the provider-hosted web_search tool descriptor for the family,
    or ``None`` if the provider does not expose a native web_search tool."""
    entry = PROVIDER_FAMILIES.get(provider_family)
    return entry["web_search_tool"] if entry else None


def resolve_family(provider: str, model_id: str) -> str:
    """Given the top-level provider (emergent/anthropic/openai/...) and the
    model id, resolve the provider family used for tool routing.

    The ``emergent`` provider is a proxy that routes to anthropic / openai
    / gemini depending on model id; look up the model in the catalog to
    determine the underlying family. Falls back to `provider` for BYOK
    providers that map 1:1 to a family.
    """
    if provider == "emergent":
        for m in CATALOG:
            if m["id"] == model_id and m["via_emergent_key"]:
                return m["provider_family"]
        return "anthropic"  # emergent default
    return provider
