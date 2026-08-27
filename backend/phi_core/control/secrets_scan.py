"""Credential/secret detection for the D69 ProviderGateway "secret scan" /
Phase 2B "secret blocking" control (v3 lines 2715, 3342).

Deliberately narrow: anchored literal prefixes/formats for well-known
credential shapes (cloud access keys, provider API keys, GitHub/Slack
tokens, PEM key blocks, bearer JWTs), never an entropy heuristic. An
entropy scanner flags ordinary hashes, UUIDs, and base64 blobs as false
positives on every run; an anchored-prefix scanner fires only when a
literal credential shape actually appears, so wiring its result into a
deny path (``ProviderGateway.complete``) is fail-closed hardening rather
than a source of spurious denials on benign model output.

This is a PHI/PII-adjacent but distinct control from
``phi_core.anonymizer.scrub_for_prompt``: that module screens for patient
identifiers, this module screens for developer/operator credentials that
must never reach a provider request body either.
"""
from __future__ import annotations

import re
from typing import Mapping, Sequence

_STRUCTURAL_KEYS = frozenset(
    {
        "role", "content", "source", "tool_request_id", "type", "name",
        "max_uses", "status", "denial_reason", "url", "title",
    }
)

# Each pattern anchors on a literal, low-false-positive credential shape.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")),
    ("pem_private_key", re.compile(r"-----BEGIN(?: RSA| EC| OPENSSH)? PRIVATE KEY-----")),
)


def find_secrets(text: str) -> tuple[str, ...]:
    """Return the sorted, de-duplicated kinds of secret found in ``text``."""
    if not text:
        return ()
    found = {kind for kind, pattern in _SECRET_PATTERNS if pattern.search(text)}
    return tuple(sorted(found))


def contains_secret(value: object) -> bool:
    """Recursively check a gateway payload (string, mapping, or sequence)
    for a credential shape, mirroring ``gateway._contains_restricted_content``'s
    traversal so both checks see the same structure before egress."""
    if isinstance(value, str):
        return bool(find_secrets(value))
    if isinstance(value, Mapping):
        return any(
            (key not in _STRUCTURAL_KEYS and contains_secret(str(key))) or contains_secret(child)
            for key, child in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(contains_secret(child) for child in value)
    return False
