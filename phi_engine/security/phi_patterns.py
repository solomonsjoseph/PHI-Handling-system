"""Shared PHI regex catalog used by phi_gate and log_hygiene.

Single source of truth for "what does a PHI-like substring look like" so
the query-time gate, the log redactor, and the narrative scrub all agree.

Three tiers:

* **Blocking patterns** — high-confidence PHI (Aadhaar, PAN, SSN, email,
  phone, Indian PIN). A blocking hit in any tool return blocks the
  response.
* **Warn patterns** — lower-confidence heuristics (bare NUMERIC_ID,
  DATE_MDY, PERSON_NAME). Logged but do not block. Over-aggressive in
  mixed clinical text; surfaced for audit, not enforcement.
* **Subject-ID patterns** — Indo-VAP-specific subject-ID shapes
  (``SC\\d{4,}``, ``SUBJ-\\d+``, ``SUBJID_N``). Used to key per-subject
  HMAC redaction in the log wrapper.

Regulatory anchors: HIPAA §164.514(b)(2)(i)(A-P), DPDPA §2(t), Aadhaar
Act §29, SPDI Rule 3, ICMR 2017 §11.4.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

__all__ = [
    "BLOCKING_PATTERNS",
    "SUBJECT_ID_PATTERNS",
    "WARN_PATTERNS",
    "IndianPhonePattern",
    "VerhoeffPattern",
]


# Standard Verhoeff Tables for Aadhaar validation
_VERHOEFF_D = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 2, 3, 4, 0, 6, 7, 8, 9, 5],
    [2, 3, 4, 0, 1, 7, 8, 9, 5, 6],
    [3, 4, 0, 1, 2, 8, 9, 5, 6, 7],
    [4, 0, 1, 2, 3, 9, 5, 6, 7, 8],
    [5, 9, 8, 7, 6, 0, 4, 3, 2, 1],
    [6, 5, 9, 8, 7, 1, 0, 4, 3, 2],
    [7, 6, 5, 9, 8, 2, 1, 0, 4, 3],
    [8, 7, 6, 5, 9, 3, 2, 1, 0, 4],
    [9, 8, 7, 6, 5, 4, 3, 2, 1, 0],
]

_VERHOEFF_P = [
    [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
    [1, 5, 7, 6, 2, 8, 3, 0, 9, 4],
    [5, 8, 0, 3, 7, 9, 6, 1, 4, 2],
    [8, 9, 1, 6, 0, 4, 3, 5, 2, 7],
    [9, 4, 5, 3, 1, 2, 6, 8, 7, 0],
    [4, 2, 8, 6, 5, 7, 3, 9, 0, 1],
    [2, 7, 9, 3, 8, 0, 6, 4, 1, 5],
    [7, 0, 4, 6, 9, 1, 3, 2, 5, 8],
]


def _verhoeff_validate(number: str) -> bool:
    if not number.isdigit():
        return False
    check = 0
    for i, digit in enumerate(reversed(number)):
        check = _VERHOEFF_D[check][_VERHOEFF_P[i % 8][int(digit)]]
    return check == 0


class VerhoeffPattern:
    """A wrapper for re.Pattern that validates matches using Verhoeff checksum.

    This is used to reduce false positives for identifier patterns (like Aadhaar)
    that have a mathematically defined checksum.
    """

    def __init__(self, name: str, base_pattern: re.Pattern[str]) -> None:
        self.name = name
        self._pattern = base_pattern

    @property
    def pattern(self) -> str:
        return self._pattern.pattern

    @property
    def flags(self) -> int:
        return self._pattern.flags

    @property
    def groups(self) -> int:
        return self._pattern.groups

    @property
    def groupindex(self) -> dict[str, int]:
        return dict(self._pattern.groupindex)

    def validate(self, matched_text: str) -> bool:
        """Return True iff *matched_text* is a Verhoeff-valid 12-digit Aadhaar.

        Shared by the Presidio recognizer (``presidio_gate``) so the regex
        framework and this wrapper apply identical validation — no drift.
        """
        candidate = "".join(c for c in matched_text if c.isdigit())
        return len(candidate) == 12 and _verhoeff_validate(candidate)

    def search(self, string: str, pos: int = 0, endpos: int = 2**31 - 1) -> re.Match[str] | None:
        for match in self._pattern.finditer(string, pos, endpos):
            candidate = "".join(c for c in match.group(0) if c.isdigit())
            if len(candidate) == 12 and _verhoeff_validate(candidate):
                return match
        return None

    def finditer(self, string: str, pos: int = 0, endpos: int = 2**31 - 1):
        for match in self._pattern.finditer(string, pos, endpos):
            candidate = "".join(c for c in match.group(0) if c.isdigit())
            if len(candidate) == 12 and _verhoeff_validate(candidate):
                yield match

    def sub(self, repl: str | Callable[[re.Match[str]], str], string: str, count: int = 0) -> str:
        def replacement_fn(match: re.Match[str]) -> str:
            candidate = "".join(c for c in match.group(0) if c.isdigit())
            if len(candidate) == 12 and _verhoeff_validate(candidate):
                if callable(repl):
                    return repl(match)
                return repl
            return match.group(0)

        return self._pattern.sub(replacement_fn, string, count)


def _is_valid_indian_phone(number: str) -> bool:
    if len(number) != 10:
        return False
    counts: dict[str, int] = {}
    for char in number:
        counts[char] = counts.get(char, 0) + 1
    if any(count >= 8 for count in counts.values()):
        return False
    return len(counts) > 1


class IndianPhonePattern:
    """A wrapper for re.Pattern that validates Indian phone matches.

    Filters out repeating-digit placeholders common in clinical datasets.
    """

    def __init__(self, name: str, base_pattern: re.Pattern[str]) -> None:
        self.name = name
        self._pattern = base_pattern

    @property
    def pattern(self) -> str:
        return self._pattern.pattern

    @property
    def flags(self) -> int:
        return self._pattern.flags

    @property
    def groups(self) -> int:
        return self._pattern.groups

    @property
    def groupindex(self) -> dict[str, int]:
        return dict(self._pattern.groupindex)

    def validate(self, matched_text: str) -> bool:
        """Return True iff *matched_text* is a valid (non-placeholder) Indian phone.

        Shared by the Presidio recognizer (``presidio_gate``) so the regex
        framework and this wrapper apply identical validation — no drift.
        """
        candidate = "".join(c for c in matched_text if c.isdigit())
        if candidate.startswith("91") and len(candidate) == 12:
            candidate = candidate[2:]
        return len(candidate) == 10 and _is_valid_indian_phone(candidate)

    def search(self, string: str, pos: int = 0, endpos: int = 2**31 - 1) -> re.Match[str] | None:
        for match in self._pattern.finditer(string, pos, endpos):
            matched_text = match.group(0)
            candidate = "".join(c for c in matched_text if c.isdigit())
            if candidate.startswith("91") and len(candidate) == 12:
                candidate = candidate[2:]
            if len(candidate) == 10 and _is_valid_indian_phone(candidate):
                return match
        return None

    def finditer(self, string: str, pos: int = 0, endpos: int = 2**31 - 1):
        for match in self._pattern.finditer(string, pos, endpos):
            matched_text = match.group(0)
            candidate = "".join(c for c in matched_text if c.isdigit())
            if candidate.startswith("91") and len(candidate) == 12:
                candidate = candidate[2:]
            if len(candidate) == 10 and _is_valid_indian_phone(candidate):
                yield match

    def sub(self, repl: str | Callable[[re.Match[str]], str], string: str, count: int = 0) -> str:
        def replacement_fn(match: re.Match[str]) -> str:
            matched_text = match.group(0)
            candidate = "".join(c for c in matched_text if c.isdigit())
            if candidate.startswith("91") and len(candidate) == 12:
                candidate = candidate[2:]
            if len(candidate) == 10 and _is_valid_indian_phone(candidate):
                if callable(repl):
                    return repl(match)
                return repl
            return match.group(0)

        return self._pattern.sub(replacement_fn, string, count)


BLOCKING_PATTERNS: list[tuple[str, Any]] = [
    # ── Indian government IDs ────────────────────────────────────────────
    # Allow space, dash, OR dot as separators — real Aadhaar numbers
    # start with a digit 2-9 and are not repeating identical digits.
    (
        "AADHAAR",
        VerhoeffPattern(
            "AADHAAR",
            re.compile(r"\b(?!(\d)(?:[\s\-\.]?\1){11}\b)[2-9]\d{3}[\s\-\.]?\d{4}[\s\-\.]?\d{4}\b"),
        ),
    ),
    # PAN is officially uppercase, but data entry can lowercase it; the 5-alpha +
    # 4-digit + 1-alpha shape is distinctive enough that case-insensitive matching
    # adds negligible false-positive surface while closing a lowercased-PAN leak
    # vector (Note 34: a PAN mislabeled under a benign header evades both the
    # case-sensitive regex and Presidio, which has no PAN recognizer).
    ("PAN", re.compile(r"\b[A-Za-z]{5}\d{4}[A-Za-z]\b")),
    ("INDIAN_VOTER_ID", re.compile(r"\b[A-Z]{3}\d{7}\b")),
    ("INDIAN_DL", re.compile(r"\b[A-Z]{2}\d{2}\s?\d{4}\d{7}\b")),
    ("INDIAN_PASSPORT", re.compile(r"\b[A-Z]\d{7}\b")),
    # ── Contact ──────────────────────────────────────────────────────────
    (
        "INDIAN_PHONE",
        IndianPhonePattern(
            "INDIAN_PHONE",
            re.compile(r"(?<![a-zA-Z0-9])(?:\+91[\s-]?)?[6-9]\d{9}(?![a-zA-Z0-9])"),
        ),
    ),
    ("EMAIL", re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")),
    ("URL", re.compile(r"\bhttps?://[^\s/$.?#].[^\s]*\b", re.I)),
    (
        "INDIAN_PIN",
        re.compile(r"(?i:pin\s*(?:code)?|postal\s*code|zip)\s*[:=\-]?\s*\b(\d{6})\b"),
    ),
    # ── US identifier shapes (cross-site collaboration hedge) ────────────
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
    # Indo-VAP / RePORT India subject ID shapes.
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
