"""Query-time PHI gate for the RePORT AI Portal agent boundary.

Uses the shared :mod:`phi_engine.security.phi_patterns` catalog and the
:mod:`phi_engine.security.phi_allowlist` clinical-phrase allowlist. The
allowlist suppresses obvious-false-positive warnings on clinical
verbatim like "Treatment Completed" that would otherwise match the
generic name-like heuristic.

Presidio NER is intentionally not wired in — comparative benchmarks
showed precision around 22.7 % on mixed data where the rule catalog +
clinical allowlist reach materially higher precision on the calibrated
Indo-VAP field shapes.

The gate is the **defence-in-depth** layer at the LLM tool boundary:
:func:`phi_engine.security.llm_tool_guard.guard_llm_output` calls
:func:`phi_gate_check` on every payload before it reaches an LLM, so even
if an upstream scrub missed a token the live call cannot surface it.

IRB-grade benchmark anchors:
    * Pillar 2.4 — every tool return passes through a PHI gate
    * Pillar 1.5 — narrative-content leak detection
    * Pillar 5.3 — breach-alert emission on blocked responses
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from phi_engine.security import phi_allowlist
from phi_engine.security.phi_patterns import BLOCKING_PATTERNS, WARN_PATTERNS
from phi_engine.utils.logging_system import get_logger

logger = get_logger(__name__)

__all__ = [
    "PHIEgressBlockedError",
    "PHIGateConfigError",
    "PHIGateResult",
    "phi_gate_check",
]


class PHIGateConfigError(ValueError):
    """Raised when the PHI gate is invoked with malformed input."""


class PHIEgressBlockedError(PermissionError):
    """Raised when an outbound LLM prompt trips the PHI gate before dispatch.

    Defense-in-depth on top of the structural headers-only prompt
    construction (the primary control -- llm_detector.py / phi_alignment.py
    never place a row value in a prompt to begin with). This is the
    egress-direction backstop prior-audit finding C2 identified as missing:
    ``guard_llm_output`` only ever scanned the RETURN value, never the
    OUTBOUND prompt. The message carries the matched PATTERN CATEGORY only
    (``PHIGateResult.findings``, e.g. ``SSN``/``EMAIL``) -- never the
    matched text itself and never a position offset (the underlying
    ``phi_gate_check`` API does not expose one; do not fabricate one).
    Known limitation carried from C3: the blocking regex tier has documented
    false negatives, so this gate is a backstop, not a proof of absence.
    """


@dataclass(frozen=True)
class PHIGateResult:
    """Outcome of a PHI-gate scan.

    ``blocked`` is ``True`` when any blocking pattern matched.
    ``findings`` is a sorted, unique tuple of category tags recorded
    across the scan (both blocking and warn-only). Safe to show the
    operator — the tags are category names like ``SSN`` /
    ``EMAIL``, never raw values.
    """

    blocked: bool
    findings: tuple[str, ...]

    def __bool__(self) -> bool:
        # Truthy = SAFE to proceed. Mirrors the archive semantics so
        # `if phi_gate_check(text): return text` reads intuitively.
        return not self.blocked


def _normalize_texts(texts: str | Sequence[str]) -> list[str]:
    if isinstance(texts, str):
        return [texts]
    if not isinstance(texts, Sequence):
        raise PHIGateConfigError("texts must be a string or sequence of strings")
    out: list[str] = []
    for idx, item in enumerate(texts):
        if not isinstance(item, str):
            raise PHIGateConfigError(f"texts[{idx}] must be a string, got {type(item)}")
        out.append(item)
    return out


def _scan_regex(
    text: str,
    blocking: list[tuple[str, re.Pattern[str]]],
    warn: list[tuple[str, re.Pattern[str]]],
) -> tuple[list[str], list[str]]:
    """Return ``(blocking_hits, warn_hits)`` labels for this *text*.

    Warn-tier per-match tuning: the generic two-capital-word name
    heuristic (``PERSON_NAME_GENERIC``) fires on benign bigrams like
    "Treatment Completed", "Cohort A", "Violin Plot" that appear
    throughout clinical narratives. We keep the warn only when *at
    least one* individual match both (a) fails the clinical-phrase
    allowlist and (b) looks like a real name under the seeded first/
    last-name lexicon. This is still advisory-only — blocking tier is
    unaffected.
    """
    blocking_hits: list[str] = []
    warn_hits: list[str] = []
    for label, pat in blocking:
        if pat.search(text):
            blocking_hits.append(label)
    for label, pat in warn:
        if label != "PERSON_NAME_GENERIC":
            if pat.search(text):
                warn_hits.append(label)
            continue
        for match in pat.finditer(text):
            span = match.group(0)
            if phi_allowlist.is_clinical_phrase(span):
                continue
            if phi_allowlist.looks_like_real_name(span):
                warn_hits.append(label)
                break
    return blocking_hits, warn_hits


def _is_clinical_allowlist_hit(text: str) -> bool:
    """Return True when *text* is fully covered by the clinical allowlist.

    Short-circuits the warn tier: clinical phrases like "Bacteriologic
    relapse" or "patient expired" are not PHI. Blocking tier still fires
    — the allowlist does NOT override SSN / email matches.
    """
    return phi_allowlist.is_clinical_phrase(text) or phi_allowlist.is_clinical_free_text(text)


def phi_gate_check(
    texts: str | Sequence[str],
) -> PHIGateResult:
    """Scan *texts* for PHI. Returns ``blocked=True`` only on high-confidence PHI.

    Low-confidence heuristics (bare NUMERIC_ID, DATE_MDY, generic
    PERSON_NAME) are recorded in ``findings`` for audit but do not
    trigger blocking — they over-fire on legitimate clinical phrases
    and would block benign agent responses.

    Clinical-phrase allowlist (:mod:`phi_allowlist`) is consulted on
    the warn tier only. Blocking tier always wins.
    """
    texts_list = _normalize_texts(texts)

    all_blocking: list[str] = []
    all_findings: list[str] = []
    for t in texts_list:
        blocking, warnings_hit = _scan_regex(t, BLOCKING_PATTERNS, WARN_PATTERNS)
        all_blocking.extend(blocking)
        all_findings.extend(blocking)
        if warnings_hit and not _is_clinical_allowlist_hit(t):
            all_findings.extend(warnings_hit)

    unique = tuple(sorted(set(all_findings)))
    is_blocked = bool(set(all_blocking))

    if unique:
        # Best-effort telemetry — redaction filter should already scrub any
        # raw values that ride along via args.
        logger.warning(
            "phi_gate: %s — findings=%s", "BLOCK" if is_blocked else "WARN", list(unique)
        )

    return PHIGateResult(blocked=is_blocked, findings=unique)
