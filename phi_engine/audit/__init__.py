"""Audit zone helpers: process-role marker + zone guards + ledger writer."""

from __future__ import annotations

import os

PROCESS_ROLE_ENV_VAR = "REPORTAL_PROCESS_ROLE"
PROCESS_ROLE_LLM_AGENT = "llm-agent"


def current_process_role() -> str | None:
    """Return the current process role, or None if unset."""
    val = os.environ.get(PROCESS_ROLE_ENV_VAR)
    return val if val else None


def is_llm_agent() -> bool:
    """Return True iff REPORTAL_PROCESS_ROLE is exactly 'llm-agent'."""
    return current_process_role() == PROCESS_ROLE_LLM_AGENT
