"""Canonical serialization for provider-bound egress records."""
from __future__ import annotations

import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from phi_core.crypto import egress_digest_key

if TYPE_CHECKING:
    from .gateway import GatewayRequest

EGRESS_SCHEMA_VERSION = 2

# Keys which describe gateway protocol structure rather than untrusted content.
_STRUCTURAL_KEYS = frozenset(
    {
        "role",
        "content",
        "source",
        "tool_request_id",
        "type",
        "name",
        "max_uses",
        "status",
        "denial_reason",
        "url",
        "title",
    }
)


def canonical_payload(
    *,
    request: GatewayRequest,
    decision: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
) -> bytes:
    """Serialize the exact sanitized payload and its authorization decision."""
    payload = {
        "egress_schema": EGRESS_SCHEMA_VERSION,
        "policy_version": request.policy_version,
        "prompt_version": request.prompt_version,
        "identity": {
            "session_id": request.session_id,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "attempt": request.attempt,
            "grant_id": request.grant_id,
        },
        "decision": {
            "status": decision.get("status", ""),
            "denial_reason": decision.get("denial_reason", ""),
        },
        "provider": request.provider,
        "model": request.model,
        "endpoint": request.endpoint,
        "messages": list(messages),
        "tools": list(tools),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")


def egress_digest(payload: bytes) -> str:
    """Return the keyed digest that authenticates a canonical egress payload."""
    return hmac.new(egress_digest_key(), payload, hashlib.sha256).hexdigest()
