"""Multi-provider LLM client for agents.

Providers supported through LiteLLM:
  - anthropic (claude-*)
  - openai (gpt-*)
  - gemini (gemini-*)
  - openrouter (openrouter/*)
  - custom OpenAI-compatible (base_url override)

Also supports the Emergent Universal Key by routing to Anthropic via
emergentintegrations when EMERGENT_LLM_KEY is present and no external
key was configured.
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


@dataclass
class LlmConfig:
    provider: str = "emergent"   # emergent|anthropic|openai|gemini|openrouter|openai_compatible
    model: str = "claude-sonnet-4-5-20250929"
    api_key: str = ""
    base_url: str = ""           # for openai_compatible
    temperature: float = 0.1
    max_tokens: int = 2000

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LlmConfig":
        d = d or {}
        return cls(
            provider=d.get("provider", "emergent"),
            model=d.get("model", "claude-sonnet-4-5-20250929"),
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
            temperature=float(d.get("temperature", 0.1)),
            max_tokens=int(d.get("max_tokens", 2000)),
        )


def _emergent_call(system: str, user: str, cfg: LlmConfig) -> str:
    """Call Claude via emergentintegrations (Emergent Universal Key)."""
    from emergentintegrations.llm.chat import LlmChat, UserMessage
    key = os.environ["EMERGENT_LLM_KEY"]
    chat = LlmChat(api_key=key, session_id="agent", system_message=system).with_model("anthropic", cfg.model)
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
    """Call Claude with Anthropic's provider-hosted ``web_search_20250305``
    tool enabled. Anthropic executes the search server-side and returns
    the final answer with inline citations; there is no client-side tool
    loop to run.

    Returns ``(content, citations)`` where ``citations`` is the list of
    source URLs Claude used (may be empty if the LLM answered from its
    own knowledge without searching). Because LiteLLM collapses the raw
    Anthropic block structure into a plain string, citation extraction is
    best-effort: URLs appearing in the reply text are captured as
    ``{"url": ..., "title": ""}`` entries.
    """
    from emergentintegrations.llm.chat import LlmChat, UserMessage
    import asyncio

    key = os.environ["EMERGENT_LLM_KEY"]

    async def _do() -> tuple[str, list[dict[str, Any]]]:
        chat = (
            LlmChat(api_key=key, session_id="agent-web", system_message=system)
            .with_model("anthropic", cfg.model)
            .with_tools(tools=[{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": max_uses,
            }])
        )
        resp = await chat.send_message_with_tools(UserMessage(text=user))
        content = getattr(resp, "content", None) or ""
        # LiteLLM stringifies Anthropic's structured web_search response, so
        # citations must be recovered from URLs embedded in the text.
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


_URL_RE = re.compile(r"https?://[^\s\)\]\"']+")


def call_llm_with_web_search(system: str, user: str,
                             cfg: LlmConfig | None = None,
                             max_uses: int = 3) -> tuple[str, list[dict[str, Any]]]:
    """Public helper: LLM call with Claude native web_search.

    Only wired for the ``emergent`` provider today because the Emergent
    Universal Key routes to Anthropic and Anthropic exposes the
    provider-hosted ``web_search_20250305`` tool. Falls back to a plain
    LLM call (no citations) for other providers so agents remain
    functional without the tool.
    """
    cfg = cfg or LlmConfig()
    if cfg.provider == "emergent":
        return _emergent_call_with_web_search(system, user, cfg, max_uses=max_uses)
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
    cfg = cfg or LlmConfig()
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
