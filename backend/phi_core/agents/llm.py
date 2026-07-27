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
