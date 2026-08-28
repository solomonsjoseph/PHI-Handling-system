"""D66: sanitize a ``TraceEvent.payload`` before it is hashed and persisted
(v3 #66 "trace privacy").

Reuses this codebase's existing PHI/PII scrubber
(``phi_core.security.scrub_nested`` / ``phi_core.security.scrub_persisted_text``)
and secret scanner (``control.secrets_scan.contains_secret``) rather than
rebuilding either -- this module is the missing wiring step ("sanitize
before persistence"), not a new detector.

Production default is the fail-closed posture v3 #66 specifies: raw
prompt/completion/tool-result content is never persisted unless a caller
explicitly opts in via the three ``TRACE_RAW_*`` env flags, and anything the
scrubber cannot confidently clear is replaced with ``CONTENT_REDACTED``
rather than persisted raw first and redacted later.

D3/D4 (Wave R-b): ``TraceEvent.status_text`` and ``TraceEvent.retry_category``
are free-text fields real callers interpolate raw study content into (a
dictionary entry name, a column name, a raw exception ``str(exc)`` forwarded
from a sandboxed child process). ``sanitize_status_text`` runs the same
``scrub_persisted_text`` PHI/PII scrubber this codebase already uses for
every other persisted free-text field (agent decision reasons, ledger
notes) over both, so ``TraceEventStore.append`` can scrub them before they
are chained into the hash and before they reach any reader (SSE stream,
``/agent-trace`` API) that projects a persisted ``TraceEvent`` back out.
"""
from __future__ import annotations

import os
from typing import Any

from ..security import scrub_nested, scrub_persisted_text
from .secrets_scan import contains_secret

CONTENT_REDACTED = "CONTENT_REDACTED"

# Payload keys treated as raw LLM I/O and gated by the flags below. Any of
# these three env vars set truthy re-enables persisting that field's raw
# (but still PHI/secret-scrubbed) text; unset/false means the key is
# replaced with CONTENT_REDACTED outright, never scrubbed-and-kept.
_RAW_CONTENT_KEYS: dict[str, str] = {
    "prompt_text": "TRACE_RAW_PROMPT_CONTENT",
    "prompt": "TRACE_RAW_PROMPT_CONTENT",
    "completion_text": "TRACE_RAW_COMPLETION_CONTENT",
    "reply_text": "TRACE_RAW_COMPLETION_CONTENT",
    "completion": "TRACE_RAW_COMPLETION_CONTENT",
    "tool_result": "TRACE_RAW_TOOL_RESULT_CONTENT",
    "tool_result_text": "TRACE_RAW_TOOL_RESULT_CONTENT",
}


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def raw_content_flags() -> dict[str, bool]:
    """Current value of the three production-default-false flags."""
    return {
        "TRACE_RAW_PROMPT_CONTENT": _flag("TRACE_RAW_PROMPT_CONTENT"),
        "TRACE_RAW_COMPLETION_CONTENT": _flag("TRACE_RAW_COMPLETION_CONTENT"),
        "TRACE_RAW_TOOL_RESULT_CONTENT": _flag("TRACE_RAW_TOOL_RESULT_CONTENT"),
    }


def sanitize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a sanitized copy of a ``TraceEvent.payload`` dict.

    Pipeline (v3 #66): dataset-value / PHI / PII check, then secret check,
    then the raw-content policy gate. Each top-level key gated by
    ``_RAW_CONTENT_KEYS`` is replaced with ``CONTENT_REDACTED`` unless its
    flag is on, in which case it still passes through the PHI/secret
    scrubbers like every other field -- the flag relaxes "redact this whole
    field" to "scrub and keep it", never past the scrubbers entirely.
    """
    if not payload:
        return {}
    flags = raw_content_flags()
    out: dict[str, Any] = {}
    for key, value in payload.items():
        gate_flag = _RAW_CONTENT_KEYS.get(key)
        if gate_flag is not None and not flags[gate_flag]:
            out[key] = CONTENT_REDACTED
            continue
        scrubbed = scrub_nested(value, _key=key)
        if contains_secret(scrubbed):
            out[key] = CONTENT_REDACTED
            continue
        out[key] = scrubbed
    return out


def sanitize_status_text(text: str) -> str:
    """D3/D4: scrub PHI/PII from a ``TraceEvent.status_text`` or
    ``retry_category`` value before it is hashed and persisted.

    Unlike ``sanitize_payload``'s dataset-value scrubbing (``scrub_nested``,
    which is aware of `_key`-based allow-lists meant for nested LLM I/O
    dicts), these two fields are plain top-level strings that real callers
    already interpolate raw content into -- a dictionary entry name, a
    column name, or ``str(exc)`` from a raw exception forwarded out of a
    sandboxed child process (D3). Reuses ``scrub_persisted_text`` directly,
    the same PHI/PII scrubber this codebase already runs over every other
    persisted free-text field (agent decision reasons, ledger notes,
    reviewer comments) rather than reimplementing detection here.
    """
    if not text:
        return text or ""
    return scrub_persisted_text(text)
