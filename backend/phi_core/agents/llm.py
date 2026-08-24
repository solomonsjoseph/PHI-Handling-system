"""Multi-provider LLM client for agents.

Providers supported through LiteLLM (portable, run anywhere):
  - anthropic (claude-*) with native web_search_20250305 tool
  - openai (gpt-*)
  - gemini (gemini-*)
  - openrouter (openrouter/*)
  - openai_compatible (custom base_url; ALLOWED_LLM_BASE_URL_HOSTS gates it)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

import litellm

# Silence litellm's noisy prints
litellm.suppress_debug_info = True
litellm.drop_params = True

logger = logging.getLogger(__name__)


def _default_provider() -> str:
    """Pick a sensible default provider based on the current environment.

    Runs anywhere: we honour whichever key the operator's environment
    actually exposes, so a deploy with an Anthropic key gets an Anthropic
    default without editing config.
    """
    # Prefer the four Settings UI providers; ChatGPT-OAuth remains
    # callable if configured, but defaults map into the simple menu.
    if os.environ.get("OPENROUTER_API_KEY"):
        return "openrouter"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"):
        return "gemini"
    if _chatgpt_account_connected():
        return "chatgpt"
    # No key at all: ChatGPT/OpenAI is the Settings default; the first
    # LLM call surfaces a clear auth error rather than crashing silently.
    return "openai"


def _chatgpt_account_connected() -> bool:
    """Whether a ChatGPT OAuth auth file is already on disk. Local import
    to sidestep a module-load-order cycle (phi_core.chatgpt_auth imports
    phi_core.paths, not phi_core.agents, but we still keep the import
    function-scoped, deferring optional-provider imports until needed)."""
    try:
        from ..chatgpt_auth import read_auth
        return read_auth() is not None
    except Exception:
        return False


@dataclass
class LlmConfig:
    provider: str = ""           # "" -> resolved from env at from_dict time
    model: str = ""
    api_key: str = ""
    base_url: str = ""           # for openai_compatible
    temperature: float = 0.1
    max_tokens: int = 2000

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "LlmConfig":
        d = d or {}
        model = str(d.get("model") or "").strip()
        if not model:
            raise ValueError("select a model before running the pipeline")
        return cls(
            provider=d.get("provider") or _default_provider(),
            model=model,
            api_key=d.get("api_key", ""),
            base_url=d.get("base_url", ""),
            temperature=float(d.get("temperature", 0.1)),
            max_tokens=int(d.get("max_tokens", 2000)),
        )


def _litellm_call_with_web_search(system: str, user: str, cfg: LlmConfig,
                                  max_uses: int = 3) -> tuple[str, list[dict[str, Any]]]:
    """LiteLLM path for Anthropic native web_search_20250305 tool.

    Lets the shipped Statute + Praxis agents work end-to-end with a plain
    ``ANTHROPIC_API_KEY`` in the environment. Returns (content, citations).
    """
    tool = {
        "type": "web_search_20250305",
        "name": "web_search",
        "max_uses": max_uses,
    }
    kwargs: dict[str, Any] = {
        "model": cfg.model,
        "max_tokens": cfg.max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "tools": [tool],
    }
    if _model_supports_custom_temperature(cfg.model):
        kwargs["temperature"] = cfg.temperature
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

    Wired for ``anthropic`` -> plain Anthropic key via LiteLLM's native
    tool-use path. All other providers fall back to a plain LLM call (no
    citations) so agents remain functional without provider-hosted search.
    """
    cfg = cfg or LlmConfig.from_dict(None)
    if cfg.provider == "chatgpt":
        _require_chatgpt_connected()
        logger.info(
            "web_search unavailable on provider 'chatgpt'; "
            "falling back to deterministic Statute/Praxis"
        )
        return _litellm_call(system, user, cfg), []
    if cfg.provider == "anthropic":
        return _litellm_call_with_web_search(system, user, cfg, max_uses=max_uses)
    return _litellm_call(system, user, cfg), []


def _model_supports_custom_temperature(model: str) -> bool:
    """OpenAI reasoning-tier models (o1/o3/o4/gpt-5*) reject any
    ``temperature`` other than the API default (1). litellm's
    ``drop_params`` only strips params it already knows a model rejects,
    which lags newly released reasoning models -- so we guard explicitly
    rather than let the request 400 at the provider."""
    bare = model.rsplit("/", 1)[-1].lower()
    return not bare.startswith(("o1", "o3", "o4", "gpt-5"))


def _litellm_call(system: str, user: str, cfg: LlmConfig) -> str:
    """Call via LiteLLM. Model naming follows LiteLLM conventions."""
    model = cfg.model
    if cfg.provider == "openrouter" and not model.startswith("openrouter/"):
        model = f"openrouter/{model}"
    kwargs: dict[str, Any] = {"model": model, "max_tokens": cfg.max_tokens,
                              "messages": [{"role": "system", "content": system},
                                           {"role": "user",   "content": user}]}
    if _model_supports_custom_temperature(model):
        kwargs["temperature"] = cfg.temperature
    if cfg.api_key:
        kwargs["api_key"] = cfg.api_key
    if cfg.base_url:
        kwargs["api_base"] = cfg.base_url
    resp = litellm.completion(**kwargs)
    return resp.choices[0].message.content or ""


def _require_chatgpt_connected() -> None:
    """Fail fast instead of letting a call reach litellm's ChatGPTConfig
    with no auth file, or with a dead one. Without this, litellm's
    Authenticator prints a device code to stdout and polls for 15 minutes;
    under Agent.call that await is cancelled at 90s but the worker thread
    it ran in keeps polling, leaking one thread per agent call.

    Checking file presence alone is not enough: an access token expires
    long before the on-disk file is deleted, and litellm's own refresh
    fallback only saves us when the refresh token is STILL valid --
    revoked-elsewhere or past its own lifetime, and litellm falls through
    to the same 15-minute interactive login this guard exists to prevent.
    We cannot know the refresh token's live validity without a network
    call (which would just re-implement litellm's own refresh), so treat
    an expired access token the same as no file: fail fast and force
    reconnect through our own bounded HTTP-poll flow instead.
    """
    from ..chatgpt_auth import read_auth
    auth = read_auth()
    if auth is None:
        raise RuntimeError("ChatGPT account not connected")
    expires_at = auth.get("expires_at")
    if expires_at is not None and time.time() >= expires_at - 60:
        raise RuntimeError("ChatGPT account not connected")


def call_llm(system: str, user: str, cfg: LlmConfig | None = None) -> str:
    cfg = cfg or LlmConfig.from_dict(None)
    if cfg.provider == "chatgpt":
        _require_chatgpt_connected()
        return _litellm_call(system, user, cfg)
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
