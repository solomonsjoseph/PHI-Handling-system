"""Anonymizer: apply review decisions and produce PHI-scrubbed output.

Strategies per span:
  - accept + no replacement -> [REDACTED:<hipaa>]
  - accept + replacement    -> use replacement
  - reject                  -> keep original
  - reclassify              -> use new_category label, still redact with tag
Deterministic: no randomness during application.
"""
from __future__ import annotations

from .models import DetectedSpan


def _tag(span: DetectedSpan) -> str:
    if span.replacement:
        return span.replacement
    hipaa = span.hipaa_category or "X"
    return f"[REDACTED:{hipaa}:{span.entity_type}]"


def apply_to_text(text: str, spans: list[DetectedSpan]) -> str:
    """Apply accepted spans to text. Reject-kept spans are ignored."""
    active = [s for s in spans if s.review_status in ("accepted", "reclassified")]
    active.sort(key=lambda s: s.start, reverse=True)
    out = text
    for s in active:
        out = out[:s.start] + _tag(s) + out[s.end:]
    return out


def scrub_for_prompt(text: str, detectors: tuple[str, ...] = ("presidio", "rule")) -> tuple[str, int]:
    """Deterministically redact every detected identifier before text enters
    an LLM prompt. Returns (scrubbed_text, span_count).

    ``detectors`` defaults to presidio+rule (free prose: agent notes, dictionary
    descriptions). Callers scrubbing static form/template text -- Title-Case
    labels with no sentence context -- should pass ``("rule",)`` only: presidio's
    NER model false-positives heavily on capitalized label words ("Last Name",
    "Diagnosis", "Registration") with no surrounding prose to disambiguate,
    while rule-based regex (SSN/phone/date patterns) still catches genuine
    identifiers if real values were typed onto the form."""
    from .detectors import detect_text
    spans = detect_text(text, detectors=detectors)
    for sp in spans:
        sp.review_status = "accepted"
    return apply_to_text(text, spans), len(spans)

