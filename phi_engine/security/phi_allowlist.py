"""Clinical-phrase allowlist for PHI false-positive suppression.

Three pure functions:

* :func:`is_clinical_phrase` — True for whitelisted clinical / research
  terms like "Treatment Completed" or "Bacteriologic relapse".
* :func:`is_clinical_free_text` — True for whole-value clinical notations
  like "patient expired" or "died on 3/1/2014" that should not be
  flagged by the generic name-like heuristic.
* :func:`looks_like_real_name` — True when a two-to-four-word capitalized string
  has at least one token in the common-name lexicon.

The bundled datasets are **small seed lists** that prevent the most common
false positives in Indo-VAP free-text (TB status, treatment outcome,
specimen quality). Extend by adding entries directly to the frozen sets below.
"""

from __future__ import annotations

import re

__all__ = [
    "CLINICAL_PHRASES",
    "CLINICAL_SINGLE_WORDS",
    "COMMON_FIRST_NAMES",
    "COMMON_LAST_NAMES",
    "is_clinical_free_text",
    "is_clinical_phrase",
    "looks_like_real_name",
]


# Seed lists — add entries here to extend coverage.
CLINICAL_PHRASES: frozenset[str] = frozenset(
    {
        # TB outcomes
        "bacteriologic relapse",
        "bact. relapse",
        "clinical relapse",
        "bacteriologic failure",
        "bact. failure",
        "clinical failure",
        "treatment completed",
        "treatment success",
        "treatment failure",
        "loss to follow up",
        "lost to follow up",
        "cured",
        "cure declared",
        # Pregnancy / obstetric
        "normal delivery",
        "preterm delivery",
        "spontaneous abortion",
        # Study outcome
        "definite case",
        "probable case",
        "possible case",
        "not tb",
        "not a case",
        "not applicable",
        # Status
        "yes, current smoker",
        "yes, former smoker",
        "no, never",
        "never smoker",
        "don't know",
        # Specimen
        "sample not collected",
        "specimen rejected",
        "insufficient volume",
        # Generic
        "not available",
        "not done",
        "not reported",
        "not known",
    }
)
"""Lower-cased whole-phrase allowlist."""


CLINICAL_SINGLE_WORDS: frozenset[str] = frozenset(
    {
        # TB vocabulary
        "tb",
        "tuberculosis",
        "mdr",
        "xdr",
        "ziehl",
        "neelsen",
        "smear",
        "culture",
        "liquid",
        "solid",
        "lowenstein",
        "jensen",
        "isoniazid",
        "rifampicin",
        "pyrazinamide",
        "ethambutol",
        "streptomycin",
        # Clinical descriptors
        "positive",
        "negative",
        "pending",
        "reactive",
        "nonreactive",
        "normal",
        "abnormal",
        "definite",
        "probable",
        "possible",
        "treatment",
        "completed",
        "cured",
        "failure",
        "relapse",
        "cavitary",
        "cavity",
        "lesion",
        "bilateral",
        "unilateral",
        "apical",
        "basal",
        "minimal",
        "moderate",
        "advanced",
        # Lab indicators
        "contamination",
        "contaminated",
        "invalid",
        "indeterminate",
        "rejected",
        "insufficient",
    }
)
"""Lower-cased single-word clinical vocabulary (used for two-token phrase check)."""


COMMON_FIRST_NAMES: frozenset[str] = frozenset(
    {
        # English (small seed)
        "john",
        "james",
        "mary",
        "patricia",
        "robert",
        "michael",
        "linda",
        "barbara",
        # Indian (small seed)
        "rajesh",
        "suresh",
        "ramesh",
        "mahesh",
        "sanjay",
        "anil",
        "sunil",
        "vijay",
        "priya",
        "pooja",
        "ananya",
        "aishwarya",
        "lakshmi",
        "saraswati",
        "gita",
        "geetha",
        "babu",
        "kumar",
        "raju",
    }
)
"""Small seed — extend by adding entries to this frozenset."""


COMMON_LAST_NAMES: frozenset[str] = frozenset(
    {
        # Indian (small seed)
        "sharma",
        "verma",
        "gupta",
        "kumar",
        "singh",
        "patel",
        "reddy",
        "naidu",
        "rao",
        "iyer",
        "nair",
        "menon",
        "pillai",
        # English (small seed)
        "smith",
        "johnson",
        "williams",
        "brown",
        "jones",
    }
)
"""Small seed — extend by adding entries to this frozenset."""


# ---------------------------------------------------------------------------
# Context-aware clinical free-text patterns
# ---------------------------------------------------------------------------

_PATIENT_VARIANTS = r"(?:pat[ie]{0,2}[aie]?nt|paitent|ptient|pt|participant|subject)"
_EXPIRED_VARIANTS = r"exp(?:i(?:re?|er)|ri)(?:d|ed)?"

_CLINICAL_FREE_TEXT_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        rf"^(?:.*\s)?(?:{_PATIENT_VARIANTS}\s+)?{_EXPIRED_VARIANTS}\s*$",
        re.I,
    ),
    re.compile(
        rf"^(?:.*\s)?(?:{_PATIENT_VARIANTS}\s+)?{_EXPIRED_VARIANTS}\s+on\b",
        re.I,
    ),
    re.compile(r"^(?:.*\s)?(?:died|death)\s*(?:on|at|in|due)?\b", re.I),
    re.compile(rf"(?:dots|card|handover|transfer)\b.*\b{_EXPIRED_VARIANTS}", re.I),
    re.compile(rf"^\s*{_EXPIRED_VARIANTS}\s*$", re.I),
]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def is_clinical_phrase(text: str) -> bool:
    """Return True if *text* is a known clinical / research phrase.

    Checks both the exact phrase (lowered) against :data:`CLINICAL_PHRASES`
    and whether *every* whitespace-separated token is in
    :data:`CLINICAL_SINGLE_WORDS`.
    """
    lowered = text.strip().lower()
    if not lowered:
        return False
    if lowered in CLINICAL_PHRASES:
        return True
    tokens = lowered.split()
    return len(tokens) >= 2 and all(t in CLINICAL_SINGLE_WORDS for t in tokens)


def is_clinical_free_text(text: str) -> bool:
    """Return True if the entire value is a recognisable clinical notation.

    Catches phrasings the generic name-like heuristic would otherwise
    flag, e.g. "patient expired" or "died on 3/1/2014".
    """
    stripped = text.strip()
    if not stripped:
        return False
    return any(pat.search(stripped) for pat in _CLINICAL_FREE_TEXT_PATTERNS)


def looks_like_real_name(text: str) -> bool:
    """Return True if *text* looks like a real person name.

    A two-to-four-word capitalized string is considered likely a name when at
    least one token is in :data:`COMMON_FIRST_NAMES` or
    :data:`COMMON_LAST_NAMES`, AND the string is not a clinical phrase.
    """
    if is_clinical_phrase(text):
        return False
    tokens = [t.lower() for t in text.strip().split()]
    if len(tokens) < 2 or len(tokens) > 4:
        return False
    has_first = any(t in COMMON_FIRST_NAMES for t in tokens)
    has_last = any(t in COMMON_LAST_NAMES for t in tokens)
    return has_first or has_last
