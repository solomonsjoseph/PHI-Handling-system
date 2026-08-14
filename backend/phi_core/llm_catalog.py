"""Curated multi-provider LLM model catalog.

The Settings UI presents operators with a picker of REAL model IDs
grouped by provider (Open Router, ChatGPT, Claude, Gemini). Provider
selection filters the model list; temperature and max tokens are the
only other controls.

Each entry declares:
  * ``id``   — model ID accepted by the underlying provider SDK
  * ``label``— human-readable display name
  * ``provider_family`` — openrouter|openai|anthropic|gemini
  * ``tier``  — flagship|balanced|fast|reasoning
  * ``supports_web_search`` — native web-search tool available
  * ``via_emergent_key`` — legacy flag kept for catalog shape / tests

Statute + Praxis web search works today for Anthropic
(``web_search_20250305``) and Gemini (``googleSearch``).
"""
from __future__ import annotations

from typing import Any


# UI order and labels for Settings / Wizard. Backend ids stay stable.
UI_PROVIDERS: list[tuple[str, str]] = [
    ("openrouter", "Open Router"),
    ("openai", "ChatGPT"),
    ("anthropic", "Claude"),
    ("gemini", "Gemini"),
]


CATALOG: list[dict[str, Any]] = [
    # ---- Claude (Anthropic) --------------------------------------------
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
        "id": "claude-sonnet-4-5-20250929",
        "label": "Claude Sonnet 4.5",
        "provider_family": "anthropic",
        "tier": "balanced",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Default. Best regulation-fidelity trade-off; native web search.",
    },
    {
        "id": "claude-sonnet-4-20250514",
        "label": "Claude Sonnet 4",
        "provider_family": "anthropic",
        "tier": "balanced",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Prior Sonnet generation; still strong for classification.",
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
    {
        "id": "claude-3-5-haiku-20241022",
        "label": "Claude 3.5 Haiku",
        "provider_family": "anthropic",
        "tier": "fast",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Lower-cost Haiku for light workloads.",
    },

    # ---- ChatGPT (OpenAI API) ------------------------------------------
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
        "id": "gpt-5.2",
        "label": "GPT-5.2",
        "provider_family": "openai",
        "tier": "balanced",
        "supports_web_search": False,
        "via_emergent_key": True,
        "notes": "Latest general-purpose GPT.",
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
    {
        "id": "gpt-4.1",
        "label": "GPT-4.1",
        "provider_family": "openai",
        "tier": "balanced",
        "supports_web_search": False,
        "via_emergent_key": True,
        "notes": "Strong prior-generation general-purpose model.",
    },
    {
        "id": "gpt-4.1-mini",
        "label": "GPT-4.1 Mini",
        "provider_family": "openai",
        "tier": "fast",
        "supports_web_search": False,
        "via_emergent_key": True,
        "notes": "Lower-cost 4.1 variant.",
    },
    {
        "id": "gpt-4o",
        "label": "GPT-4o",
        "provider_family": "openai",
        "tier": "balanced",
        "supports_web_search": False,
        "via_emergent_key": True,
        "notes": "Widely available multimodal GPT.",
    },
    {
        "id": "gpt-4o-mini",
        "label": "GPT-4o Mini",
        "provider_family": "openai",
        "tier": "fast",
        "supports_web_search": False,
        "via_emergent_key": True,
        "notes": "Cheapest OpenAI option for volume runs.",
    },

    # ---- Gemini --------------------------------------------------------
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
    {
        "id": "gemini-2.5-pro",
        "label": "Gemini 2.5 Pro",
        "provider_family": "gemini",
        "tier": "flagship",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Prior Pro generation with Search grounding.",
    },
    {
        "id": "gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "provider_family": "gemini",
        "tier": "balanced",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Balanced speed / quality for Gemini 2.5.",
    },
    {
        "id": "gemini-2.0-flash",
        "label": "Gemini 2.0 Flash",
        "provider_family": "gemini",
        "tier": "fast",
        "supports_web_search": True,
        "via_emergent_key": True,
        "notes": "Lower-cost Flash for high-throughput.",
    },

    # ---- Open Router ---------------------------------------------------
    {
        "id": "openrouter/anthropic/claude-opus-4",
        "label": "Claude Opus 4",
        "provider_family": "openrouter",
        "tier": "flagship",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "OpenRouter route to Claude Opus 4.",
    },
    {
        "id": "openrouter/anthropic/claude-sonnet-4.5",
        "label": "Claude Sonnet 4.5",
        "provider_family": "openrouter",
        "tier": "balanced",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "OpenRouter route to Claude Sonnet 4.5.",
    },
    {
        "id": "openrouter/anthropic/claude-haiku-4.5",
        "label": "Claude Haiku 4.5",
        "provider_family": "openrouter",
        "tier": "fast",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "OpenRouter route to Claude Haiku 4.5.",
    },
    {
        "id": "openrouter/openai/gpt-5.4",
        "label": "GPT-5.4",
        "provider_family": "openrouter",
        "tier": "flagship",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "OpenRouter route to GPT-5.4.",
    },
    {
        "id": "openrouter/openai/gpt-5.2",
        "label": "GPT-5.2",
        "provider_family": "openrouter",
        "tier": "balanced",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "OpenRouter route to GPT-5.2.",
    },
    {
        "id": "openrouter/openai/gpt-4o-mini",
        "label": "GPT-4o Mini",
        "provider_family": "openrouter",
        "tier": "fast",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "OpenRouter lower-cost GPT route.",
    },
    {
        "id": "openrouter/google/gemini-3-pro",
        "label": "Gemini 3 Pro",
        "provider_family": "openrouter",
        "tier": "flagship",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "OpenRouter route to Gemini 3 Pro.",
    },
    {
        "id": "openrouter/google/gemini-2.5-flash",
        "label": "Gemini 2.5 Flash",
        "provider_family": "openrouter",
        "tier": "fast",
        "supports_web_search": False,
        "via_emergent_key": False,
        "notes": "OpenRouter lower-cost Gemini route.",
    },
]


PROVIDER_FAMILIES: dict[str, dict[str, Any]] = {
    "openrouter": {
        "label": "Open Router",
        "web_search_tool": None,
    },
    "openai": {
        "label": "ChatGPT",
        "web_search_tool": None,
    },
    "anthropic": {
        "label": "Claude",
        "web_search_tool": {"type": "web_search_20250305",
                            "name": "web_search"},
    },
    "gemini": {
        "label": "Gemini",
        "web_search_tool": {"googleSearch": {}},
    },
    # Kept for backend allow-list / SSRF / legacy settings docs only.
    # Not advertised in Settings UI_PROVIDERS.
    "openai_compatible": {
        "label": "Custom OpenAI-compatible endpoint",
        "web_search_tool": None,
    },
    "chatgpt": {
        "label": "ChatGPT subscription",
        "web_search_tool": None,
    },
}


DEFAULT_MODEL_ID = "claude-sonnet-4-5-20250929"


def models_for_provider(provider_family: str) -> list[dict[str, Any]]:
    """Catalog rows for a single provider family, flagship → fast."""
    tier_rank = {"flagship": 0, "reasoning": 1, "balanced": 2, "fast": 3}
    rows = [m for m in CATALOG if m["provider_family"] == provider_family]
    return sorted(rows, key=lambda m: (tier_rank.get(m.get("tier", ""), 9), m["label"]))


def default_model_for(provider: str) -> str:
    """Pick a sensible default model id for a provider family."""
    fam = provider
    if fam == "emergent":
        fam = "anthropic"
    if fam == "chatgpt":
        # OAuth ChatGPT path is separate; Settings "ChatGPT" maps to openai.
        fam = "openai"
    rows = models_for_provider(fam)
    if not rows:
        return DEFAULT_MODEL_ID
    # Prefer balanced, then first sorted row.
    for m in rows:
        if m.get("tier") == "balanced":
            return m["id"]
    return rows[0]["id"]


def catalog_for_ui() -> dict[str, Any]:
    """Payload for ``GET /api/settings/llm/catalog``."""
    providers = [
        {
            "id": fam,
            "label": label,
            "web_search_available": PROVIDER_FAMILIES[fam]["web_search_tool"] is not None,
            "via_emergent_key": any(
                m["provider_family"] == fam and m["via_emergent_key"] for m in CATALOG
            ),
        }
        for fam, label in UI_PROVIDERS
        if fam in PROVIDER_FAMILIES
    ]
    return {
        "providers": providers,
        "models": CATALOG,
        "default_model_id": DEFAULT_MODEL_ID,
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
