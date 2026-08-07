"""LLM classifier: reads only column headers for datasets, full text for narratives.

Portable: uses the shared multi-provider client (`agents/llm.py`), which
auto-detects the default provider from environment keys. Works with
Emergent Universal Key, plain Anthropic, OpenAI, Gemini, OpenRouter, or
any OpenAI-compatible endpoint.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

# ``phi_core.agents.llm`` is imported lazily inside the call helpers to
# avoid a circular import (``phi_core.agents.__init__`` pulls the
# orchestrator, which imports ``phi_core.pipeline``, which imports this
# module).

MODEL_NAME = "claude-sonnet-4-5-20250929"

SYSTEM = (
    "You are a compliance analyst. Given file metadata, output a strict JSON "
    "object with keys: content_type (one of clinical_narrative, structured_dataset, "
    "email, form_document, image_metadata, other), likely_phi_domains (list of "
    "strings from: identity, contact, geographic, medical_record, financial, "
    "biometric, device, provider, insurance, research_id, quasi_identifier), "
    "recommended_detectors (list from: presidio, rule, header_hint), "
    "risk_tier (low, moderate, high, critical), notes (short, one sentence). "
    "Cite 45 CFR 164.514 in notes when applicable. No preamble."
)


def _strip_json(s: str) -> dict[str, Any]:
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if not m:
        return {"content_type": "other", "notes": "no JSON in response"}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"content_type": "other", "notes": "malformed JSON"}


def _cfg():
    # Lazy import to avoid the circular dep noted at top of module.
    from phi_core.agents.llm import LlmConfig
    return LlmConfig.from_dict({"model": MODEL_NAME})


async def classify_narrative(text: str, filename: str) -> dict[str, Any]:
    from phi_core.agents.llm import call_llm
    excerpt = text[:4000]
    user = (
        f"Filename: {filename}\nFile kind: narrative\n"
        f"Excerpt (first 4000 chars):\n---\n{excerpt}\n---\n"
        "Respond with JSON only."
    )
    reply = await asyncio.to_thread(call_llm, SYSTEM, user, _cfg())
    return _strip_json(str(reply))


async def classify_dataset_headers(columns: list[str], filename: str,
                                   row_count: int) -> dict[str, Any]:
    from phi_core.agents.llm import call_llm
    header_list = ", ".join(columns[:200])
    user = (
        f"Filename: {filename}\nFile kind: structured_dataset\n"
        f"Row count: {row_count}\n"
        f"Column headers (headers only, never row values): {header_list}\n"
        "Respond with JSON only."
    )
    reply = await asyncio.to_thread(call_llm, SYSTEM, user, _cfg())
    return _strip_json(str(reply))
