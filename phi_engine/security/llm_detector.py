"""LLM-based PHI header classifier.

The LLM reads column headers and jurisdiction, classifies each header as
PHI or non-PHI, recommends a de-identification action, and cites the
governing authority. It never reads data values -- only header names.

Uncertain decisions (confidence < PHI_CONFIDENCE_THRESHOLD) are appended
to a JSONL review queue for human triage via `phi-review`.

Falls back to presidio_gate + phi_gate_check if the LLM call fails.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

# Authority matrix excerpt -- condensed category list fed into every prompt.
_CATEGORY_HINT = """\
HIPAA Safe Harbor categories (45 CFR 164.514(b)(2)(i)):
  A=Names  B=Geographic  C=Dates  D=Phone  E=Fax  F=Email  G=SSN
  H=MRN  I=HealthPlan  J=Account  K=Certificate/License  L=Vehicle
  M=Device  N=URL  O=IP  P=Biometric  Q=Photo  R=OtherUnique

India identifiers: Aadhaar(12-digit), PAN(10-char), ABHA, Voter-ID,
  UAN, ESI, CGHS, Driving-License, GSTIN, Ration-Card

De-identification actions:
  drop | pseudonymize | jitter_date | generalize | cap | keep | flag_review"""


@dataclass
class LLMDetectionResult:
    header: str
    is_phi: bool
    entity_type: str          # e.g. "DATE", "NAME", "SSN", "NON_PHI"
    hipaa_category: str       # e.g. "C", "A", "G", "" for non-HIPAA
    confidence: float         # 0.0-1.0
    recommended_action: str   # drop | pseudonymize | jitter_date | generalize | cap | keep
    authority_citation: str   # e.g. "45 CFR 164.514(b)(2)(i)(C)"
    reasoning: str
    source: str = "llm"       # "llm" | "fallback"


def _build_prompt(headers: list[str], jurisdiction: str) -> str:
    headers_json = json.dumps(headers)
    return f"""\
You are a PHI de-identification expert. Classify each column header below as PHI or non-PHI
under {jurisdiction} regulations. Headers are column NAMES only -- you will not see data values.

{_CATEGORY_HINT}

Column headers to classify:
{headers_json}

Respond with a JSON array, one object per header, in the same order:
[
  {{
    "header": "<exact header name>",
    "is_phi": true|false,
    "entity_type": "<type or NON_PHI>",
    "hipaa_category": "<letter A-R or empty string>",
    "confidence": 0.0-1.0,
    "recommended_action": "<action>",
    "authority_citation": "<citation>",
    "reasoning": "<one sentence>"
  }}
]

Return ONLY the JSON array. No prose, no markdown fences."""


def _parse_llm_response(text: str, headers: list[str]) -> list[dict]:
    """Extract JSON array from LLM response; raise ValueError on failure."""
    # Strip markdown fences if present
    cleaned = re.sub(r"```(?:json)?\s*|\s*```", "", text.strip())
    data = json.loads(cleaned)
    if not isinstance(data, list) or len(data) != len(headers):
        raise ValueError(f"Expected {len(headers)} items, got {len(data) if isinstance(data, list) else type(data)}")
    return data


def _fallback_classify(headers: list[str], jurisdiction: str) -> list[LLMDetectionResult]:
    """Classify headers using presidio + phi_gate when LLM is unavailable."""
    from phi_engine.security.phi_gate import phi_gate_check
    from phi_engine.security.presidio_gate import analyze_text

    results = []
    for h in headers:
        findings = analyze_text(h)
        if findings:
            f = findings[0]
            results.append(LLMDetectionResult(
                header=h,
                is_phi=True,
                entity_type=f.entity_type,
                hipaa_category="",
                confidence=f.score,
                recommended_action="drop",
                authority_citation="",
                reasoning="Presidio pattern match",
                source="fallback",
            ))
        else:
            gate = phi_gate_check([h])
            results.append(LLMDetectionResult(
                header=h,
                is_phi=gate.blocked,
                entity_type=gate.findings[0] if gate.findings else "NON_PHI",
                hipaa_category="",
                confidence=0.9 if gate.blocked else 0.8,
                recommended_action="drop" if gate.blocked else "keep",
                authority_citation="",
                reasoning="phi_gate regex match" if gate.blocked else "No pattern match",
                source="fallback",
            ))
    return results


def _write_review_queue(items: list[LLMDetectionResult], queue_path: Path) -> None:
    queue_path.parent.mkdir(parents=True, exist_ok=True)
    with queue_path.open("a") as fh:
        for item in items:
            fh.write(json.dumps(asdict(item)) + "\n")


def classify_headers(
    headers: list[str],
    jurisdiction: str = "HIPAA",
    *,
    review_queue_path: Optional[Path] = None,
) -> list[LLMDetectionResult]:
    """Classify PHI in column headers using the LLM.

    Args:
        headers: Column header names (no data values).
        jurisdiction: Governing regulation (e.g. "HIPAA", "DPDPA", "GDPR").
        review_queue_path: Path to append uncertain cases for human review.
            Defaults to audit/human_review/llm_uncertain.jsonl relative to cwd.

    Returns:
        One LLMDetectionResult per header, in the same order.
    """
    from phi_engine.config.config import PHI_CONFIDENCE_THRESHOLD, get_llm_client

    if review_queue_path is None:
        review_queue_path = Path("audit") / "human_review" / "llm_uncertain.jsonl"

    # Attempt LLM classification
    results: list[LLMDetectionResult] = []
    try:
        from phi_engine.security.llm_tool_guard import guard_llm_output

        client = get_llm_client()
        prompt = _build_prompt(headers, jurisdiction)
        raw = client.complete(prompt)
        guard_llm_output(raw)
        parsed = _parse_llm_response(raw, headers)
        for item in parsed:
            results.append(LLMDetectionResult(
                header=item["header"],
                is_phi=bool(item["is_phi"]),
                entity_type=str(item.get("entity_type", "UNKNOWN")),
                hipaa_category=str(item.get("hipaa_category", "")),
                confidence=float(item.get("confidence", 0.5)),
                recommended_action=str(item.get("recommended_action", "flag_review")),
                authority_citation=str(item.get("authority_citation", "")),
                reasoning=str(item.get("reasoning", "")),
                source="llm",
            ))
    except Exception as exc:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning("LLM classification failed (%s); using fallback", exc)
        results = _fallback_classify(headers, jurisdiction)

    # Flag uncertain results for human review
    uncertain = [r for r in results if r.confidence < PHI_CONFIDENCE_THRESHOLD]
    if uncertain:
        _write_review_queue(uncertain, review_queue_path)

    return results


# ---------------------------------------------------------------------------
# CLI entry point: python -m phi_engine.security.llm_detector
# ---------------------------------------------------------------------------

def _main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="Classify PHI in column headers via LLM")
    parser.add_argument("--headers", required=True, help="Comma-separated column header names")
    parser.add_argument("--jurisdiction", default="HIPAA", help="Governing regulation (default: HIPAA)")
    parser.add_argument("--review-queue", default=None, help="Path for uncertain-case JSONL output")
    args = parser.parse_args()

    headers = [h.strip() for h in args.headers.split(",") if h.strip()]
    queue = Path(args.review_queue) if args.review_queue else None
    results = classify_headers(headers, args.jurisdiction, review_queue_path=queue)
    print(json.dumps([asdict(r) for r in results], indent=2))


if __name__ == "__main__":
    _main()
