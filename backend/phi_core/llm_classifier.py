"""LLM classifier: reads only column headers for datasets, full text for narratives.

Uses Emergent Universal Key via emergentintegrations. Returns strict JSON.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any

from emergentintegrations.llm.chat import LlmChat, UserMessage


MODEL_PROVIDER = "anthropic"
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


async def classify_narrative(text: str, filename: str) -> dict[str, Any]:
    excerpt = text[:4000]
    key = os.environ["EMERGENT_LLM_KEY"]
    chat = LlmChat(
        api_key=key,
        session_id=f"classify_narr_{filename}",
        system_message=SYSTEM,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)
    user = UserMessage(text=f"Filename: {filename}\nFile kind: narrative\nExcerpt (first 4000 chars):\n---\n{excerpt}\n---\nRespond with JSON only.")
    reply = await chat.send_message(user)
    return _strip_json(str(reply))


async def classify_dataset_headers(columns: list[str], filename: str, row_count: int) -> dict[str, Any]:
    key = os.environ["EMERGENT_LLM_KEY"]
    chat = LlmChat(
        api_key=key,
        session_id=f"classify_ds_{filename}",
        system_message=SYSTEM,
    ).with_model(MODEL_PROVIDER, MODEL_NAME)
    header_list = ", ".join(columns[:200])
    user = UserMessage(text=(
        f"Filename: {filename}\nFile kind: structured dataset (row values withheld for PHI safety)\n"
        f"Row count: {row_count}\nColumn headers only: {header_list}\n"
        "Respond with JSON only. Base your assessment strictly on the column names."
    ))
    reply = await chat.send_message(user)
    return _strip_json(str(reply))
