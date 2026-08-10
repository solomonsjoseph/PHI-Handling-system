"""Anonymizer: apply review decisions and produce PHI-scrubbed output.

Strategies per span:
  - accept + no replacement -> [REDACTED:<hipaa>]
  - accept + replacement    -> use replacement
  - reject                  -> keep original
  - reclassify              -> use new_category label, still redact with tag
Deterministic: no randomness during application.
"""
from __future__ import annotations

from pathlib import Path

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

