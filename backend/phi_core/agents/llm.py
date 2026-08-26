"""LLM configuration and response parsing shared by agents.

Inference is intentionally implemented only by ``phi_core.control.gateway``.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any


def _default_provider() -> str:
    """Pick a configured provider without importing an inference SDK."""
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
    return "openai"


def _chatgpt_account_connected() -> bool:
    """Whether a ChatGPT OAuth authorization record is available locally."""
    try:
        from ..chatgpt_auth import read_auth

        return read_auth() is not None
    except Exception:
        return False


@dataclass
class LlmConfig:
    provider: str = ""
    model: str = ""
    api_key: str = ""
    base_url: str = ""
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


_JSON_RE = re.compile(r"\{.*\}|\[.*\]", re.DOTALL)


def parse_json(text: str, default: Any = None) -> Any:
    m = _JSON_RE.search(text)
    if not m:
        return default if default is not None else {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return default if default is not None else {}
