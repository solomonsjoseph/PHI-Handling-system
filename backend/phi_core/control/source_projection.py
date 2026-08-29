"""Phase 2E: HeaderSafetyGate, SourceProjectionGateway, UntrustedContentGateway.

Target-architecture reconciliation (local reference doc
docs/MASTER_ARCHITECTURE_V2.md section 7 "headers and variable names" and
section 22 "SourceProjectionGateway", never committed). The Phase 2 audit
found these three named as genuine gaps: header text reached Schema's
LLM-facing output with no sensitivity scan, and study-derived content
(dictionaries, mappings, forms, comments) was scrubbed ad hoc per specialist
with no single staged classify -> safety-check -> normalize -> project path.

This module composes existing deterministic controls rather than
reimplementing them: ``anonymizer.scrub_for_prompt`` (presidio/rule PHI
detection), ``control.secrets_scan.contains_secret`` (credential-shape
detection), and ``control.gateway._contains_restricted_content`` (the same
post-scrub residual check ``ProviderGateway.complete`` already applies at
egress). Sensitive headers are projected as an opaque token via
``control.opaque.OpaqueMap`` (kind ``"header"``), matching the doc's own
``SENSITIVE_HEADER_004`` example.

Deterministic, no-LLM. Wired into ``phi_core/agents/`` by Wave R-c:
``Schema.run`` routes every header through ``classify_header``/opaque
projection, ``Lexicon``/``Instrument`` route extracted dictionary/form
text through ``source_projection`` before any provider call.
"""
from __future__ import annotations

import re
from typing import Sequence

from phi_core.anonymizer import scrub_for_prompt

from .gateway import _contains_restricted_content
from .opaque import OpaqueMap
from .records import HeaderClassification, SourceContentType, SourceProjectionResult
from .secrets_scan import contains_secret

# Header text is a short label, never free prose: use the rule-based
# detector only (same reasoning ``scrub_for_prompt``'s own docstring gives
# for static form/template labels -- presidio's NER model false-positives
# heavily on capitalized label words with no surrounding sentence context).
_HEADER_DETECTORS: tuple[str, ...] = ("rule",)

# Content types that are free prose (dictionary descriptions, human
# comments) get the full presidio+rule sweep; static labels (forms,
# mappings) get rule-only for the same reason headers do.
_LABEL_LIKE_CONTENT: frozenset[SourceContentType] = frozenset({"header", "form", "mapping"})

_WHITESPACE_RE = re.compile(r"[ \t]+")
_BLANK_LINES_RE = re.compile(r"\n{3,}")

# Wave R-c (v3 section 7 "uncertain" outcome, now real): a header
# carrying an embedded digit run of 3-9 characters not already caught by
# a strict rule (SSN/phone/NPI/MRN, all handled above via `scrub_for_
# prompt`) is ambiguous, not confidently safe or confidently sensitive --
# it could be a coincidental site/version/sequence code ("site_02139",
# "id_1234") or a real identifier fragment typed into a header by
# mistake. This heuristic is deliberately noisy (it will also flag
# ordinary numeric-suffixed columns like "visit_1"): that noise is
# exactly why `uncertain` routes to non-blocking review rather than a
# hard block -- see `agents/specialists.py::Schema.run`.
_AMBIGUOUS_DIGIT_RUN_RE = re.compile(r"(?<!\d)\d{3,9}(?!\d)")


def classify_header(header: str) -> tuple[str, list[str]]:
    """Deterministically classify one header string's disposition.

    Returns ``(disposition, reasons)``. ``sensitive`` when the header text
    itself trips PHI detection or a credential-shape pattern (a study team
    can and does type a real value into a column name by mistake).
    ``uncertain`` when neither strict check fires but the header carries
    an ambiguous embedded digit run (see ``_AMBIGUOUS_DIGIT_RUN_RE``
    above). ``safe`` otherwise.
    """
    reasons: list[str] = []
    _, span_count = scrub_for_prompt(header, detectors=_HEADER_DETECTORS)
    if span_count:
        reasons.append(f"header text matched {span_count} PHI detector span(s)")
    if contains_secret(header):
        reasons.append("header text matched a credential/secret pattern")
    if reasons:
        return "sensitive", reasons
    if _AMBIGUOUS_DIGIT_RUN_RE.search(header):
        return "uncertain", ["header text contains an unclassified numeric run that may be a real identifier fragment"]
    return "safe", reasons


def header_safety_gate(
    headers: Sequence[str], *, run_id: str, opaque_map: OpaqueMap
) -> tuple[list[str], list[HeaderClassification]]:
    """The v3 section 7 pipeline: DETERMINISTIC HEADER EXTRACTION (the
    caller's job -- headers arrive already extracted) -> HEADER SAFETY GATE
    -> SAFE HEADER PROJECTION. Sensitive headers never route to SCHEMA under
    their literal text; a caller wiring this into the human-review flow
    treats a non-``safe`` disposition as the doc's own AUTHORIZED HUMAN
    REVIEW branch.

    Returns ``(projected_headers, classifications)`` in input order.
    """
    projected: list[str] = []
    classifications: list[HeaderClassification] = []
    for header in headers:
        disposition, reasons = classify_header(header)
        if disposition == "safe":
            projected.append(header)
            classifications.append(HeaderClassification(header=header, disposition=disposition, reasons=reasons))
            continue
        token = opaque_map.to_opaque("header", header)
        projected.append(token)
        classifications.append(
            HeaderClassification(header=header, disposition=disposition, reasons=reasons, opaque_token=token)
        )
    return projected, classifications


def _normalize_untrusted_text(text: str) -> str:
    """UNTRUSTED CONTENT NORMALIZATION (v3 section 22): collapse whitespace
    runs and excess blank lines. Deliberately minimal -- this is hygiene,
    not a security boundary; the security boundary is the PHI/secret check
    that runs before and after it."""
    collapsed = _WHITESPACE_RE.sub(" ", text)
    collapsed = _BLANK_LINES_RE.sub("\n\n", collapsed)
    return collapsed.strip()


def untrusted_content_blocked(text: str) -> bool:
    """UntrustedContentGateway's egress check (v3 section 23): the same
    post-scrub residual-content test ``ProviderGateway.complete`` already
    applies to every outbound payload, exposed here so a caller can apply it
    to study-derived content *before* that content is even assembled into a
    provider request. Prompt-injection instructions inside ``text`` are not
    scanned for -- per the doc's own section 23, detection is not itself a
    security boundary; capability gating (``ToolGateway.search``'s
    ``allowed_tools`` count) is what stops untrusted text from acquiring
    tool authority, and that path is unchanged by this function."""
    return _contains_restricted_content(text)


def source_projection(
    *, content_type: SourceContentType, raw_text: str, run_id: str
) -> SourceProjectionResult:
    """SourceProjectionGateway (v3 section 22): CONTENT CLASSIFICATION ->
    PHI/PII/SECRET SAFETY CHECK -> UNTRUSTED CONTENT NORMALIZATION ->
    PURPOSE-SPECIFIC PROJECTION. LOCAL/SANDBOX EXTRACTION is the caller's
    job (file_readers.py / docx_safe.py already do this); ``raw_text`` is
    already-extracted plain text.

    Every study-derived content type this doc names (headers, dictionary
    files, mapping files, forms, CRFs, PDFs, DOCX, human comments) routes
    through this one function rather than each specialist scrubbing ad hoc.
    """
    detectors = _HEADER_DETECTORS if content_type in _LABEL_LIKE_CONTENT else ("presidio", "rule")
    scrubbed, span_count = scrub_for_prompt(raw_text, detectors=detectors)
    reasons: list[str] = []
    if span_count:
        reasons.append(f"{span_count} PHI detector span(s) redacted")

    if contains_secret(raw_text) or contains_secret(scrubbed):
        return SourceProjectionResult(
            content_type=content_type,
            run_id=run_id,
            disposition="sensitive",
            reasons=reasons + ["credential/secret pattern matched; content blocked, not projected"],
            projected_text="",
            blocked=True,
        )

    normalized = _normalize_untrusted_text(scrubbed)
    if untrusted_content_blocked(normalized):
        return SourceProjectionResult(
            content_type=content_type,
            run_id=run_id,
            disposition="uncertain",
            reasons=reasons + ["residual PHI/PII detected after scrub; content blocked, not projected"],
            projected_text="",
            blocked=True,
        )

    return SourceProjectionResult(
        content_type=content_type,
        run_id=run_id,
        disposition="sensitive" if span_count else "safe",
        reasons=reasons,
        projected_text=normalized,
        blocked=False,
    )
