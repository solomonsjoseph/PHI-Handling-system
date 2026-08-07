"""Multi-provider LLM client for agents.

Providers supported through LiteLLM (portable, run anywhere):
  - anthropic (claude-*) with native web_search_20250305 tool
  - openai (gpt-*)
  - gemini (gemini-*)
  - openrouter (openrouter/*)
  - openai_compatible (custom base_url; ALLOWED_LLM_BASE_URL_HOSTS gates it)

Also supports the Emergent Universal Key by routing to Anthropic /
OpenAI / Gemini via ``emergentintegrations`` when the library is
installed and ``EMERGENT_LLM_KEY`` is present. The library is imported
lazily so the app runs cleanly on any deployment without it.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import litellm

# Silence litellm's noisy prints
litellm.suppress_debug_info = True
litellm.drop_params = True


def _default_provider() -> str:
    """Pick a sensible default provider based on the current environment.

    Sir Q "Ensure it is not locked to emergent only, and it must be free
    to be used anywhere." We honour whichever key the operator's
    environment actually exposes, so a deploy with an Anthropic key gets
    an Anthropic default without editing config.
    """
    if os.environ.get("EMERGENT_LLM_KEY"):
        return "emergent"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    # No key at all: still return emergent so the UI renders; the first
    # LLM call will surface a helpful error rather than crashing silently.
    return "emergent"


@dataclass
class LlmConfig:
    provider: str = ""           # "" -> resolved from env at from_dict time
    model: str = "claude-sonnet-4-5-20250929"
    api_key: str = ""
    base_url: str = ""           # for openai_compatible
    temperature: float = 0.1
    max_tokens: int = 2000

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LlmConfig":
        d = d or {}
        provider = d.get("provider") or _default_provider()
        return cls(
            provider=provider,
            model=d.get("model", "claude-sonnet-4-5-20250929"),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
            temperature=float(d.get("temperature", 0.1)),
            max_tokens=int(d.get("max_tokens", 2000)),
        )


def _resolve_emergent_family(model_id: str) -> str:
    """Route to the underlying provider family for a given model id when
    using the Emergent Universal Key.

    Delegates to the catalog when possible so a single source of truth
    decides Anthropic vs OpenAI vs Gemini routing.
    """
    try:
        from ..llm_catalog import resolve_family
        return resolve_family("emergent", model_id)
    except Exception:
        # Fallback heuristics on model id if the catalog import fails.
        m = model_id.lower()
        if m.startswith("gpt") or m.startswith("openai/"):
            return "openai"
        if m.startswith("gemini") or "google" in m:
            return "gemini"
        return "anthropic"


def _emergent_call(system: str, user: str, cfg: LlmConfig) -> str:
    """Call the Emergent Universal Key. Routes to anthropic / openai /
    gemini based on the model id.

    Requires ``emergentintegrations`` (Emergent-specific package). If the
    library is not installed we fall through to a LiteLLM call using
    whichever provider-family API key is present in the environment, so
    self-hosted deployments still work.
    """
    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except ImportError:
        # emergentintegrations not installed: fall back to a direct call
        # with the underlying family SDK. Requires ANTHROPIC_API_KEY /
        # OPENAI_API_KEY / GEMINI_API_KEY to be set for the resolved
        # family.
        family = _resolve_emergent_family(cfg.model)
        fallback = LlmConfig(
            provider=family, model=cfg.model,
            temperature=cfg.temperature, max_tokens=cfg.max_tokens,
        )
        return _litellm_call(system, user, fallback)

    key = os.environ["EMERGENT_LLM_KEY"]
    family = _resolve_emergent_family(cfg.model)
    chat = (
        LlmChat(api_key=key, session_id="agent", system_message=system)
        .with_model(family, cfg.model)
    )
    reply = chat.send_message_sync(UserMessage(text=user)) if hasattr(chat, "send_message_sync") else None
    if reply is None:
        # emergentintegrations exposes async send_message; block via asyncio
        import asyncio
        loop = asyncio.new_event_loop()
        try:
            reply = loop.run_until_complete(chat.send_message(UserMessage(text=user)))
        finally:
            loop.close()
    return str(reply)


def _emergent_call_with_web_search(system: str, user: str, cfg: LlmConfig,
                                   max_uses: int = 3) -> tuple[str, list[dict[str, Any]]]:
    """Call the Emergent Universal Key with the provider's native
    web-search tool enabled when the family exposes one.

    Provider-specific tool schemas:
      * ``anthropic`` — ``{"type": "web_search_20250305", "name": "web_search"}``
      * ``gemini``    — ``{"googleSearch": {}}``
      * ``openai``    — Not exposed through the Emergent proxy today; falls
                        back to a plain LLM call without web search.

    If ``emergentintegrations`` is not installed we route the anthropic
    family through LiteLLM's native web_search_20250305 support, so
    non-Emergent deploys still get citations.

    Returns ``(content, citations)``; citations may be empty when the
    provider chose not to search or is running without tool support.
    """
    from ..llm_catalog import web_search_tool_for
    import asyncio

    family = _resolve_emergent_family(cfg.model)
    tool = web_search_tool_for(family)

    try:
        from emergentintegrations.llm.chat import LlmChat, UserMessage
    except ImportError:
        # No emergentintegrations. Route Anthropic through LiteLLM native
        # web_search_20250305 tool if possible; other families fall back
        # to a plain LLM call.
        fallback = LlmConfig(
            provider=family, model=cfg.model,
            temperature=cfg.temperature, max_tokens=cfg.max_tokens,
        )
        if family == "anthropic":
            return _litellm_call_with_web_search(system, user, fallback, max_uses=max_uses)
        return _litellm_call(system, user, fallback), []

    key = os.environ["EMERGENT_LLM_KEY"]

    if tool is None:
        # No native web-search on this family. Return a plain call so the
        # agent still gets an answer, just without live citations.
        content = _emergent_call(system, user, cfg)
        return content, []

    # Anthropic supports ``max_uses``; Gemini doesn't accept it. Only add
    # for anthropic so we don't send an unrecognised field.
    tool = dict(tool)
    if family == "anthropic":
        tool["max_uses"] = max_uses

    async def _do() -> tuple[str, list[dict[str, Any]]]:
        chat = (
            LlmChat(api_key=key, session_id="agent-web", system_message=system)
            .with_model(family, cfg.model)
            .with_tools(tools=[tool])
        )
        resp = await chat.send_message_with_tools(UserMessage(text=user))
        content = getattr(resp, "content", None) or ""
        urls = _URL_RE.findall(content)
        seen: set[str] = set()
        cites: list[dict[str, Any]] = []
        for u in urls:
            if u in seen:
                continue
            seen.add(u)
            cites.append({"url": u, "title": ""})
            if len(cites) >= 20:
                break
        return content, cites

    return asyncio.run(_do())


def _litellm_call_with_web_search(system: str, user: str, cfg: LlmConfig,
                                  max_uses: int = 3) -> tuple[str, list[dict[str, Any]]]:
    """LiteLLM path for Anthropic native web_search_20250305 tool.

    Lets the shipped Statute + Praxis agents work end-to-end with a plain
    ``ANTHROPIC_API_KEY`` in the environment (no emergentintegrations
    required). Returns (content, citations) in the same shape as the
    Emergent path.
    """
    tool = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_uses,
    }
    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "temperature": cfg.temperature,
        "max_tokens": cfg.max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "tools": [tool],
    }
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    resp = litellm.completion(**kwargs)
    content = resp.choices[0].message.content or ""
    if isinstance(content, list):
        content = "".join(str(c.get("text", "")) if isinstance(c, dict) else str(c)
                          for c in content)
    urls = _URL_RE.findall(content)
    seen: set[str] = set()
    cites: list[dict[str, Any]] = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        cites.append({"url": u, "title": ""})
        if len(cites) >= 20:
            break
    return content, cites


_URL_RE = re.compile(r"https?://[^\s\)\]\"']+")


def call_llm_with_web_search(system: str, user: str,
                             cfg: LlmConfig | None = None,
                             max_uses: int = 3) -> tuple[str, list[dict[str, Any]]]:
    """Public helper: LLM call with Claude native web_search.

    Wired for both providers that can invoke Anthropic's provider-hosted
    ``web_search_20250305`` tool:

      * ``emergent`` -> Emergent Universal Key (via emergentintegrations
        when installed, LiteLLM fallback otherwise).
      * ``anthropic`` -> plain Anthropic key via LiteLLM's native
        tool-use path.

    All other providers fall back to a plain LLM call (no citations) so
    agents remain functional without provider-hosted search.
    """
    cfg = cfg or LlmConfig.from_dict(None)
    if cfg.provider == "emergent":
        return _emergent_call_with_web_search(system, user, cfg, max_uses=max_uses)
    if cfg.provider == "anthropic":
        return _litellm_call_with_web_search(system, user, cfg, max_uses=max_uses)
    return _litellm_call(system, user, cfg), []


def _litellm_call(system: str, user: str, cfg: LlmConfig) -> str:
    """Call via LiteLLM. Model naming follows LiteLLM conventions."""
    model = cfg.model
    if cfg.provider == "openrouter" and not model.startswith("openrouter/"):
        model = f"openrouter/{model}"
    kwargs: dict[str, Any] = {"model": model, "temperature": cfg.temperature, "max_tokens": cfg.max_tokens,
                              "messages": [{"role": "system", "content": system},
                                           {"role": "user",   "content": user}]}
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.base_url:
        kwargs["api_base"] = cfg.base_url
    resp = litellm.completion(**kwargs)
    return resp.choices[0].message.content or ""


def call_llm(system: str, user: str, cfg: LlmConfig | None = None) -> str:
    cfg = cfg or LlmConfig.from_dict(None)
    if cfg.provider == "emergent":
        return _emergent_call(system, user, cfg)
    return _litellm_call(system, user, cfg)


_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)


def parse_json(text: str, default: Any = None) -> Any:
    m = _JSON_RE.search(text)
    if not m:
        return default if default is not None else {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return default if default is not None else {}
