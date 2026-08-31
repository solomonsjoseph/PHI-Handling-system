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

RegulationsExpert + PHIMethodsExpert web search works today for Anthropic
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
    # https://platform.claude.com/docs/en/about-claude/models/overview (fetched 2026-08-14)
    {
        "id": "claude-fable-5",
        "label": "Claude Fable 5",
        "provider_family": "anthropic",
        "tier": "reasoning",
        "supports_web_search": True,
        "notes": "Anthropic's most capable widely released model; next-gen long-running agents.",
    },
    {
        "id": "claude-opus-5",
        "label": "Claude Opus 5",
        "provider_family": "anthropic",
        "tier": "flagship",
        "supports_web_search": True,
        "notes": "Highest reasoning quality for complex agentic coding / enterprise work.",
    },
    {
        "id": "claude-sonnet-5",
        "label": "Claude Sonnet 5",
        "provider_family": "anthropic",
        "tier": "balanced",
        "supports_web_search": True,
        "notes": "Default. Best combination of speed and intelligence; native web search.",
    },
    {
        "id": "claude-haiku-4-5-20251001",
        "label": "Claude Haiku 4.5",
        "provider_family": "anthropic",
        "tier": "fast",
        "supports_web_search": True,
        "notes": "Fastest Claude with near-frontier intelligence.",
    },

    # ---- ChatGPT (OpenAI API) ------------------------------------------
    # https://developers.openai.com/api/docs/models (fetched 2026-08-14)
    {
        "id": "gpt-5.6-sol",
        "label": "GPT-5.6 Sol",
        "provider_family": "openai",
        "tier": "flagship",
        "supports_web_search": False,
        "notes": "Highest-quality GPT; slower / more expensive.",
    },
    {
        "id": "gpt-5.6-terra",
        "label": "GPT-5.6 Terra",
        "provider_family": "openai",
        "tier": "balanced",
        "supports_web_search": False,
        "notes": "Balances intelligence and cost for general-purpose use.",
    },
    {
        "id": "gpt-5.6-luna",
        "label": "GPT-5.6 Luna",
        "provider_family": "openai",
        "tier": "fast",
        "supports_web_search": False,
        "notes": "Cost-sensitive, high-volume GPT for classification workloads.",
    },

    # ---- Gemini --------------------------------------------------------
    # https://ai.google.dev/gemini-api/docs/models (fetched 2026-08-14)
    {
        "id": "gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro",
        "provider_family": "gemini",
        "tier": "flagship",
        "supports_web_search": True,
        "notes": "Advanced intelligence for complex problem-solving; Google Search grounding.",
    },
    {
        "id": "gemini-3.7-flash",
        "label": "Gemini 3.7 Flash",
        "provider_family": "gemini",
        "tier": "balanced",
        "supports_web_search": True,
        "notes": "Latest natively multimodal reasoning Flash model.",
    },
    {
        "id": "gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite",
        "provider_family": "gemini",
        "tier": "fast",
        "supports_web_search": True,
        "notes": "Fastest, most cost-effective Gemini 3 model.",
    },

    # ---- Open Router ---------------------------------------------------
    {
        "id": "openrouter/anthropic/claude-opus-5",
        "label": "Claude Opus 5",
        "provider_family": "openrouter",
        "tier": "flagship",
        "supports_web_search": False,
        "notes": "OpenRouter route to Claude Opus 5.",
    },
    {
        "id": "openrouter/anthropic/claude-sonnet-5",
        "label": "Claude Sonnet 5",
        "provider_family": "openrouter",
        "tier": "balanced",
        "supports_web_search": False,
        "notes": "OpenRouter route to Claude Sonnet 5.",
    },
    {
        "id": "openrouter/anthropic/claude-haiku-4.5",
        "label": "Claude Haiku 4.5",
        "provider_family": "openrouter",
        "tier": "fast",
        "supports_web_search": False,
        "notes": "OpenRouter route to Claude Haiku 4.5.",
    },
    {
        "id": "openrouter/openai/gpt-5.6-sol",
        "label": "GPT-5.6 Sol",
        "provider_family": "openrouter",
        "tier": "flagship",
        "supports_web_search": False,
        "notes": "OpenRouter route to GPT-5.6 Sol.",
    },
    {
        "id": "openrouter/openai/gpt-5.6-luna",
        "label": "GPT-5.6 Luna",
        "provider_family": "openrouter",
        "tier": "fast",
        "supports_web_search": False,
        "notes": "OpenRouter lower-cost GPT route.",
    },
    {
        "id": "openrouter/google/gemini-3.1-pro-preview",
        "label": "Gemini 3.1 Pro",
        "provider_family": "openrouter",
        "tier": "flagship",
        "supports_web_search": False,
        "notes": "OpenRouter route to Gemini 3.1 Pro.",
    },
    {
        "id": "openrouter/google/gemini-3.5-flash-lite",
        "label": "Gemini 3.5 Flash-Lite",
        "provider_family": "openrouter",
        "tier": "fast",
        "supports_web_search": False,
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


DEFAULT_MODEL_ID = "claude-sonnet-5"


def models_for_provider(provider_family: str) -> list[dict[str, Any]]:
    """Catalog rows for a single provider family, flagship → fast."""
    tier_rank = {"flagship": 0, "reasoning": 1, "balanced": 2, "fast": 3}
    rows = [m for m in CATALOG if m["provider_family"] == provider_family]
    return sorted(rows, key=lambda m: (tier_rank.get(m.get("tier", ""), 9), m["label"]))


def default_model_for(provider: str) -> str:
    """Pick a sensible default model id for a provider family."""
    fam = provider
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
        }
        for fam, label in UI_PROVIDERS
        if fam in PROVIDER_FAMILIES
    ]
    return {
        "providers": providers,
        "models": CATALOG,
        "default_model_id": DEFAULT_MODEL_ID,
    }


def resolve_family(provider: str) -> str:
    """Given the top-level provider (anthropic/openai/gemini/...), resolve
    the provider family used for tool routing. BYOK providers map 1:1 to a
    family, so this is currently an identity mapping kept as its own
    function so tool-routing call sites don't depend on that being true."""
    return provider
