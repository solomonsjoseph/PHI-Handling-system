"""Shared PHI regex catalog used by phi_gate and log_hygiene.

Single source of truth for "what does a PHI-like substring look like" so
the query-time gate, the log redactor, and the narrative scrub all agree.

Three tiers:

* **Blocking patterns** — high-confidence PHI (SSN, US phone, MRN, email,
  address, dates). A blocking hit in any tool return blocks the
  response.
* **Warn patterns** — lower-confidence heuristics (bare NUMERIC_ID,
  DATE_MDY, PERSON_NAME). Logged but do not block. Over-aggressive in
  mixed clinical text; surfaced for audit, not enforcement.
* **Subject-ID patterns** — study subject-ID shapes
  (``SC\\d{4,}``, ``SUBJ-\\d+``, ``SUBJID_N``). Used to key per-subject
  HMAC redaction in the log wrapper.

Regulatory anchors: HIPAA §164.514(b)(2)(i)(A-P).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

__all__ = [
    "BLOCKING_PATTERNS",
    "SUBJECT_ID_PATTERNS",
    "WARN_PATTERNS",
]


BLOCKING_PATTERNS: list[tuple[str, Any]] = [
    # ── Contact ──────────────────────────────────────────────────────────
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("URL", re.compile(r"\bhttps?://[^\s/$.?#].[^\s]*\b", re.I)),
    # ── US identifier shapes ─────────────────────────────────────────────
    ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    # Unhyphenated SSN only fires next to an explicit SSN label -- a bare
    # 9-digit run is too common (account/order numbers) to block on its own.
    ("SSN_UNHYPHENATED", re.compile(r"(?i:\bssn|social\s*security(?:\s*number)?)\s*[:#]?\s*(\d{9})\b")),
    ("MRN", re.compile(r"\bMRN[-:]?\s*\d{6,10}\b", re.I)),
    ("MRN_LABELED", re.compile(r"(?i:medical\s*record\s*(?:number|no\.?|#))\s*[:#]?\s*\d{4,10}\b")),
    ("IP", re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")),
    # US phone: (xxx) xxx-xxxx, xxx-xxx-xxxx, or xxx.xxx.xxxx -- distinct from
    # a bare 10-digit run so it doesn't collide with account/order numbers.
    (
        "US_PHONE",
        re.compile(r"\(\d{3}\)\s?\d{3}[-.\s]\d{4}\b|\b\d{3}[-.]\d{3}[-.]\d{4}\b"),
    ),
    # HIPAA §164.514(b)(2)(i)(C): ages over 89 must be treated as identifying.
    (
        "AGE_OVER_89",
        re.compile(r"\b(?:9\d|1\d{2})\s*(?:years?[\s-]old|y\.?o\.?)\b|(?i:\baged?\s*:?\s*)(9\d|1\d{2})\b"),
    ),
    # Street-address line: number + name + common suffix.
    (
        "ADDRESS",
        re.compile(
            r"\b\d{1,5}\s+[A-Z][a-zA-Z]*(?:\s+[A-Z][a-zA-Z]*)?\s+"
            r"(?:St|Ave|Rd|Ln|Dr|Blvd|Way|Ct|Ter|Pl|"
            r"Street|Avenue|Road|Lane|Drive|Boulevard|Court|Terrace|Place)\b\.?"
        ),
    ),
    # ── Dates (HIPAA §164.514(b)(2)(i)(C)) ───────────────────────────────
    (
        "DATE_ISO",
        re.compile(
            r"\b(?:19|20)\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
            r"(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b"
        ),
    ),
    (
        "DATE_TEXT",
        re.compile(
            r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+"
            r"\d{1,2},?\s+(?:19|20)\d{2}\b"
        ),
    ),
    (
        "PERSON_NAME_PREFIX",
        re.compile(r"\b(?:Mr|Mrs|Ms|Dr|Prof)\.?\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\b"),
    ),
]
"""High-confidence PHI patterns — a hit blocks the response."""


WARN_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # These fire frequently on legitimate clinical text. Use for audit,
    # not enforcement. The archive's is_clinical_phrase / is_clinical_free_text
    # allowlist is meant to suppress these before they reach the gate.
    ("NUMERIC_ID_SHORT", re.compile(r"\b\d{6,7}\b")),
    ("DATE_MDY", re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")),
    ("PERSON_NAME_GENERIC", re.compile(r"\b[A-Z][a-z]{2,15}\s+[A-Z][a-z]{2,15}\b")),
]
"""Low-confidence PHI heuristics — recorded for audit, do NOT block."""


SUBJECT_ID_PATTERNS: list[re.Pattern[str]] = [
    # Study subject-ID shapes used for per-subject HMAC redaction.
    re.compile(r"\bSUBJ[-_]?\d+\b"),
    re.compile(r"\bSC\d{4,}\b"),
    # Require >=4 digits (mirrors the SC subject-ID width): a real Family-ID
    # *value* is a multi-digit identifier (e.g. "FID12345"), whereas the
    # short-suffixed tokens "FID", "FID2"..."FID5" are column/header NAMES
    # (family-member index) that legitimately appear in SoT schema metadata and
    # must not trip the residual leak gate. Data-owner/security: confirm real
    # FID values are >=4 digits.
    re.compile(r"\bFID\d{4,}\b"),
]
"""Literal subject-ID substrings that the log wrapper HMAC-redacts per-subject."""


# ── PHI-safe shape masking ───────────────────────────────────────────────────

_DIGIT_RE = re.compile(r"\d")
_ALPHA_RE = re.compile(r"[A-Za-z]")


def mask_date_shape(value: str) -> str:
    """Return a PHI-safe shape of *value* for logs and error messages.

    Every digit → ``'9'``, every ASCII letter → ``'X'``, separator
    characters are kept.  The shape gives operators enough structural
    information to diagnose parsing issues without revealing the raw value.

    Examples::

        >>> mask_date_shape("28/05/2014")
        '99/99/9999'
        >>> mask_date_shape("UNK")
        'XXX'
        >>> mask_date_shape("07-05-2014 14:30:00")
        '99-99-9999 99:99:99'
    """
    return _ALPHA_RE.sub("X", _DIGIT_RE.sub("9", str(value)))
