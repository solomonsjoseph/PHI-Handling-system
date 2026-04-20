"""
Corpus benchmark harness for phi-handler / RePORTaLiN PHI stack.

Purpose:
    Score a corpus JSONL against an answer key JSONL by running each record
    through `phi_gate.phi_gate_check` and classifying the outcome as TP, FN,
    FP, TN, TP_category_wrong, TP_span_wrong, or FP_on_hard_negative.

Why this lives outside the phi-handler skill:
    The skill is a production policy and enforcement layer with ten preconditions
    (signed manifest, IRB protocol, RBAC, rule-pack signatures, etc.) designed
    to refuse ambiguity. A benchmark harness must report ambiguity, not refuse
    it. The two have different trust models; they should not share a codebase.

    Per PHASE_0_RESEARCH_COMPLETE.md, Phase 5 of the corpus is the validation
    harness. This file is that harness.

Dependencies:
    - `phi_gate` must be importable in the current Python environment
      (RePORTaLiN PHI stack working-directory).
    - `phi_rules` is imported transitively via phi_gate.

Usage:
    cd <RePORTaLiN-RAG repo root>
    python harness/run_corpus_benchmark.py \
        --corpus rounds/round_1/corpus_round_1.jsonl \
        --answer-key rounds/round_1/answer_key_round_1.jsonl \
        --output rounds/round_1/verdict_round_1.jsonl \
        --summary rounds/round_1/summary_round_1.md

Inputs:
    --corpus        JSONL. Each line: {"record_id", "query", "jurisdiction"}
    --answer-key    JSONL. Each line: {"record_id", "phi_present", "phi_elements": [...]}

Outputs:
    --output        JSONL. Per-record verdict with raw gate output preserved.
    --summary       Markdown. Aggregate metrics and observed-behavior section.

Notes on robustness:
    The signature of `phi_gate.phi_gate_check` is documented in phi-handler
    SKILL.md as `phi_gate.phi_gate_check(query, presidio=True, jurisdiction=...)`.
    The return type is not documented in the skill file. This harness handles
    the three most likely return shapes:
        1. bool: True if PHI detected, False otherwise
        2. dict: {"phi_detected": bool, "entities": [...], "categories": [...]}
        3. object with attributes: result.phi_detected, result.entities, etc.
    If `phi_gate` returns a different shape, update the `normalize_gate_result`
    function and re-run.
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class CorpusRecord:
    record_id: str
    query: str
    jurisdiction: str


@dataclass
class PhiElement:
    category: str
    subcategory: str
    value: str
    span_start: int
    span_end: int
    hipaa_safe_harbor_id: str


@dataclass
class AnswerKeyRecord:
    record_id: str
    phi_present: bool
    phi_elements: list[PhiElement]
    notes: str


@dataclass
class NormalizedGateResult:
    """Unified shape after normalizing phi_gate.phi_gate_check output."""

    phi_detected: bool
    detected_entities: list[dict]  # list of {category, subcategory, value, span_start?, span_end?}
    raw_output: Any  # preserved for the verdict file; whatever phi_gate actually returned
    error: str | None = None  # populated on exception


@dataclass
class Verdict:
    record_id: str
    jurisdiction: str
    phi_present_expected: bool
    phi_detected_actual: bool
    verdict: str  # TP, TP_category_wrong, TP_span_wrong, FN, FP, TN, FP_on_hard_negative, ERROR
    expected_elements: list[dict]
    detected_elements: list[dict]
    notes: str
    raw_gate_output: Any


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


def read_jsonl(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                raise ValueError(f"{path}: line {lineno}: invalid JSON: {e}")
    return records


def load_corpus(path: Path) -> list[CorpusRecord]:
    out = []
    for raw in read_jsonl(path):
        out.append(
            CorpusRecord(
                record_id=raw["record_id"],
                query=raw["query"],
                jurisdiction=raw["jurisdiction"],
            )
        )
    return out


def load_answer_key(path: Path) -> dict[str, AnswerKeyRecord]:
    out = {}
    for raw in read_jsonl(path):
        elements = [
            PhiElement(
                category=e["category"],
                subcategory=e["subcategory"],
                value=e["value"],
                span_start=e["span_start"],
                span_end=e["span_end"],
                hipaa_safe_harbor_id=e["hipaa_safe_harbor_id"],
            )
            for e in raw.get("phi_elements", [])
        ]
        out[raw["record_id"]] = AnswerKeyRecord(
            record_id=raw["record_id"],
            phi_present=raw["phi_present"],
            phi_elements=elements,
            notes=raw.get("notes", ""),
        )
    return out


def write_jsonl(path: Path, records: Iterable[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, default=str) + "\n")


# ---------------------------------------------------------------------------
# phi_gate integration
# ---------------------------------------------------------------------------


def import_phi_gate():
    """
    Import phi_gate from the RePORTaLiN PHI stack.

    Raises ImportError with a clear message if not available. Does not attempt
    to install or path-hack; the caller must run this harness from the
    RePORTaLiN repo root where phi_gate is importable.
    """
    try:
        import phi_gate  # type: ignore

        return phi_gate
    except ImportError as e:
        raise ImportError(
            "Cannot import phi_gate. Run this harness from the RePORTaLiN-RAG "
            "repository root where phi_gate.py is on the Python path. "
            f"Original error: {e}"
        )


def normalize_gate_result(raw: Any) -> NormalizedGateResult:
    """
    Normalize phi_gate.phi_gate_check output into a consistent shape.

    Handles three likely return shapes:
        1. bool
        2. dict with 'phi_detected' and optionally 'entities' keys
        3. object with .phi_detected and optionally .entities attributes

    If the shape is something else, returns a NormalizedGateResult with
    phi_detected=False and error set. This becomes an ERROR verdict, which
    is a findable signal in the report.
    """
    # Shape 1: bool
    if isinstance(raw, bool):
        return NormalizedGateResult(phi_detected=raw, detected_entities=[], raw_output=raw)

    # Shape 2: dict
    if isinstance(raw, dict):
        phi_detected = raw.get("phi_detected")
        if phi_detected is None:
            # sometimes named differently
            phi_detected = raw.get("detected") or raw.get("has_phi") or raw.get("is_phi")
        if phi_detected is None:
            # last resort: if 'entities' is non-empty, assume detected
            entities = raw.get("entities") or raw.get("phi_entities") or raw.get("matches") or []
            phi_detected = bool(entities)
        else:
            entities = raw.get("entities") or raw.get("phi_entities") or raw.get("matches") or []

        normalized_entities = []
        for e in entities:
            if isinstance(e, dict):
                normalized_entities.append(
                    {
                        "category": e.get("category") or e.get("type") or e.get("label") or "UNKNOWN",
                        "subcategory": e.get("subcategory") or "",
                        "value": e.get("value") or e.get("text") or e.get("match") or "",
                        "span_start": e.get("span_start") or e.get("start") or e.get("start_pos"),
                        "span_end": e.get("span_end") or e.get("end") or e.get("end_pos"),
                    }
                )
            elif isinstance(e, str):
                normalized_entities.append({"category": "UNKNOWN", "subcategory": "", "value": e})
        return NormalizedGateResult(
            phi_detected=bool(phi_detected), detected_entities=normalized_entities, raw_output=raw
        )

    # Shape 3: object with attributes
    phi_detected = getattr(raw, "phi_detected", None)
    if phi_detected is None:
        phi_detected = getattr(raw, "detected", None)
    if phi_detected is None:
        phi_detected = getattr(raw, "has_phi", None)
    if phi_detected is not None:
        entities = getattr(raw, "entities", None) or getattr(raw, "matches", None) or []
        normalized_entities = []
        for e in entities:
            if isinstance(e, dict):
                normalized_entities.append(
                    {
                        "category": e.get("category") or e.get("type") or "UNKNOWN",
                        "subcategory": e.get("subcategory") or "",
                        "value": e.get("value") or e.get("text") or "",
                        "span_start": e.get("span_start") or e.get("start"),
                        "span_end": e.get("span_end") or e.get("end"),
                    }
                )
            else:
                normalized_entities.append(
                    {
                        "category": getattr(e, "category", None)
                        or getattr(e, "type", None)
                        or "UNKNOWN",
                        "subcategory": getattr(e, "subcategory", ""),
                        "value": getattr(e, "value", "") or getattr(e, "text", ""),
                        "span_start": getattr(e, "span_start", None) or getattr(e, "start", None),
                        "span_end": getattr(e, "span_end", None) or getattr(e, "end", None),
                    }
                )
        return NormalizedGateResult(
            phi_detected=bool(phi_detected),
            detected_entities=normalized_entities,
            raw_output=raw,
        )

    # Unknown shape
    return NormalizedGateResult(
        phi_detected=False,
        detected_entities=[],
        raw_output=raw,
        error=f"Unknown phi_gate return shape: {type(raw).__name__}. Update normalize_gate_result.",
    )


def run_phi_gate(phi_gate, record: CorpusRecord) -> NormalizedGateResult:
    """Call phi_gate.phi_gate_check and normalize the result."""
    try:
        raw = phi_gate.phi_gate_check(
            record.query, presidio=True, jurisdiction=record.jurisdiction
        )
        return normalize_gate_result(raw)
    except Exception as e:
        return NormalizedGateResult(
            phi_detected=False,
            detected_entities=[],
            raw_output=None,
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
        )


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def category_match(expected: PhiElement, detected: dict) -> bool:
    """
    Compare expected category to detected category.

    Accepts multiple naming conventions. For example, expected 'NAME' matches
    detected 'PERSON', 'PATIENT', or 'PERSON_NAME'. This is intentional: the
    harness must not penalize legitimate naming differences between our
    corpus taxonomy and phi_gate's output vocabulary. Mismatches that look
    like category confusion (PHONE vs ID, SSN vs MRN) still flag.
    """
    cat_expected = expected.category.upper()
    cat_detected = (detected.get("category") or "").upper()
    sub_expected = expected.subcategory.upper()

    # exact match
    if cat_expected == cat_detected:
        return True

    # well-known equivalences
    equivalences = {
        "NAME": {"PERSON", "PATIENT", "PERSON_NAME", "PATIENT_NAME"},
        "DATE": {"DATE_TIME", "DATE_OF_BIRTH", "DOB", "DATETIME"},
        "PHONE": {"PHONE_NUMBER", "TELEPHONE", "US_PHONE_NUMBER", "IN_PHONE_NUMBER"},
        "EMAIL": {"EMAIL_ADDRESS", "EMAILADDRESS"},
        "MRN": {"MEDICAL_RECORD_NUMBER", "MEDICAL_RECORD", "RECORD_ID", "MRN_NUMBER"},
        "SSN": {"SOCIAL_SECURITY_NUMBER", "US_SSN", "US_ITIN"},
        "ADDRESS": {"LOCATION", "STREET_ADDRESS", "US_ADDRESS", "PHYSICAL_ADDRESS"},
    }
    aliases = equivalences.get(cat_expected, set())
    if cat_detected in aliases:
        return True

    # subcategory fallback (in case phi_gate returns DOB but expected says DATE/DATE_OF_BIRTH)
    if sub_expected == cat_detected:
        return True

    return False


def span_overlaps(expected: PhiElement, detected: dict, query: str) -> bool:
    """
    Does detected element overlap the expected span, or match the expected value?

    Checks in order:
        1. Exact span match (start and end both equal)
        2. Value string match (detected value equals expected value)
        3. Overlap >= 50% of expected span length
        4. Substring containment (detected value contains expected value, or vice versa)
    """
    det_start = detected.get("span_start")
    det_end = detected.get("span_end")
    det_value = detected.get("value", "")

    # 1. exact span
    if det_start == expected.span_start and det_end == expected.span_end:
        return True

    # 2. value match
    if det_value and det_value == expected.value:
        return True

    # 3. overlap >= 50%
    if (
        det_start is not None
        and det_end is not None
        and isinstance(det_start, int)
        and isinstance(det_end, int)
    ):
        overlap_start = max(det_start, expected.span_start)
        overlap_end = min(det_end, expected.span_end)
        overlap_len = max(0, overlap_end - overlap_start)
        expected_len = expected.span_end - expected.span_start
        if expected_len > 0 and overlap_len / expected_len >= 0.5:
            return True

    # Substring containment was considered but rejected as a span-match signal:
    # it produces false TPs when the system catches a partial string (e.g., "17/2024"
    # inside expected "03/17/2024"). Partial matches must be caught by the >=50%
    # overlap rule using positional data, not substring heuristics.

    return False


def score_record(
    corpus: CorpusRecord, answer: AnswerKeyRecord, gate: NormalizedGateResult
) -> Verdict:
    """Produce a single verdict per record."""
    expected_elements = [asdict(e) for e in answer.phi_elements]
    detected_elements = gate.detected_entities

    if gate.error:
        return Verdict(
            record_id=corpus.record_id,
            jurisdiction=corpus.jurisdiction,
            phi_present_expected=answer.phi_present,
            phi_detected_actual=False,
            verdict="ERROR",
            expected_elements=expected_elements,
            detected_elements=[],
            notes=f"phi_gate raised an exception. Error: {gate.error}",
            raw_gate_output=None,
        )

    # Hard negatives: phi_present is False
    if not answer.phi_present:
        if gate.phi_detected:
            return Verdict(
                record_id=corpus.record_id,
                jurisdiction=corpus.jurisdiction,
                phi_present_expected=False,
                phi_detected_actual=True,
                verdict="FP_on_hard_negative",
                expected_elements=[],
                detected_elements=detected_elements,
                notes=f"Hard negative but system flagged {len(detected_elements)} entities. {answer.notes}",
                raw_gate_output=gate.raw_output,
            )
        return Verdict(
            record_id=corpus.record_id,
            jurisdiction=corpus.jurisdiction,
            phi_present_expected=False,
            phi_detected_actual=False,
            verdict="TN",
            expected_elements=[],
            detected_elements=[],
            notes=answer.notes,
            raw_gate_output=gate.raw_output,
        )

    # Positive records: phi_present is True
    if not gate.phi_detected:
        return Verdict(
            record_id=corpus.record_id,
            jurisdiction=corpus.jurisdiction,
            phi_present_expected=True,
            phi_detected_actual=False,
            verdict="FN",
            expected_elements=expected_elements,
            detected_elements=[],
            notes=f"Expected PHI missed entirely. Expected: {[e['value'] for e in expected_elements]}",
            raw_gate_output=gate.raw_output,
        )

    # System detected SOMETHING. Now check category and span per expected element.
    # For Round 1 we have exactly one expected element per positive record.
    # Later rounds may have multiple; this logic handles both.
    verdicts_per_element = []
    for expected in answer.phi_elements:
        best_verdict = None
        for detected in detected_elements:
            cat_ok = category_match(expected, detected)
            span_ok = span_overlaps(expected, detected, corpus.query)
            if cat_ok and span_ok:
                best_verdict = "TP"
                break
            elif cat_ok and not span_ok:
                best_verdict = best_verdict or "TP_span_wrong"
            elif span_ok and not cat_ok:
                best_verdict = best_verdict or "TP_category_wrong"
        if best_verdict is None:
            # system detected something, but none of the detections cover this expected element
            best_verdict = "FN"
        verdicts_per_element.append(best_verdict)

    # Aggregate the per-element verdicts into one record-level verdict.
    # Priority: any FN > any TP_category_wrong > any TP_span_wrong > all TP
    if "FN" in verdicts_per_element:
        verdict = "FN"
    elif "TP_category_wrong" in verdicts_per_element:
        verdict = "TP_category_wrong"
    elif "TP_span_wrong" in verdicts_per_element:
        verdict = "TP_span_wrong"
    else:
        verdict = "TP"

    # Also note if system detected MORE elements than expected (over-detection on positive records).
    extra_detections = len(detected_elements) - len(answer.phi_elements)
    notes = ""
    if extra_detections > 0:
        notes = f"System detected {extra_detections} more entities than expected. Check detected_elements for FP-on-positive-record."

    return Verdict(
        record_id=corpus.record_id,
        jurisdiction=corpus.jurisdiction,
        phi_present_expected=True,
        phi_detected_actual=True,
        verdict=verdict,
        expected_elements=expected_elements,
        detected_elements=detected_elements,
        notes=notes or answer.notes,
        raw_gate_output=gate.raw_output,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def build_summary(verdicts: list[Verdict], round_label: str) -> str:
    counts = Counter(v.verdict for v in verdicts)
    total = len(verdicts)

    # Category-level breakdown
    per_category = defaultdict(lambda: Counter())
    for v in verdicts:
        for elem in v.expected_elements:
            per_category[elem["category"]][v.verdict] += 1

    # Build markdown
    lines = [
        f"# {round_label} summary",
        "",
        f"**Total records:** {total}",
        "",
        "## Verdict distribution",
        "",
        "| Verdict | Count | Percent |",
        "|---|---|---|",
    ]
    for verdict_label in [
        "TP",
        "TP_category_wrong",
        "TP_span_wrong",
        "FN",
        "FP",
        "FP_on_hard_negative",
        "TN",
        "ERROR",
    ]:
        n = counts.get(verdict_label, 0)
        pct = f"{100 * n / total:.1f}%" if total else "0.0%"
        lines.append(f"| {verdict_label} | {n} | {pct} |")

    lines.extend(["", "## Per-category breakdown", "", "| Category | TP | TP_cat_wrong | TP_span_wrong | FN |", "|---|---|---|---|---|"])
    for category in sorted(per_category):
        c = per_category[category]
        lines.append(
            f"| {category} | {c.get('TP', 0)} | {c.get('TP_category_wrong', 0)} | {c.get('TP_span_wrong', 0)} | {c.get('FN', 0)} |"
        )

    # Aggregate recall, precision on positive records
    positives_expected = sum(1 for v in verdicts if v.phi_present_expected)
    if positives_expected > 0:
        tp_like = sum(
            1
            for v in verdicts
            if v.verdict in ("TP", "TP_category_wrong", "TP_span_wrong")
        )
        recall = tp_like / positives_expected
        fn = counts.get("FN", 0)

        lines.extend(["", "## Aggregate metrics (positive records only)", ""])
        lines.append(f"- Positives expected: {positives_expected}")
        lines.append(f"- Any detection (TP + TP_cat_wrong + TP_span_wrong): {tp_like}")
        lines.append(f"- False negatives: {fn}")
        lines.append(f"- Detection recall (any flag counts): {recall:.3f}")

        strict_tp = counts.get("TP", 0)
        strict_recall = strict_tp / positives_expected if positives_expected else 0.0
        lines.append(f"- Strict TP (correct category AND span): {strict_tp}")
        lines.append(f"- Strict recall: {strict_recall:.3f}")

    # Hard negative specificity
    negatives_expected = sum(1 for v in verdicts if not v.phi_present_expected)
    if negatives_expected > 0:
        tn = counts.get("TN", 0)
        fp_hn = counts.get("FP_on_hard_negative", 0)
        specificity = tn / negatives_expected
        lines.extend(["", "## Hard negative specificity", ""])
        lines.append(f"- Hard negatives: {negatives_expected}")
        lines.append(f"- Correctly ignored (TN): {tn}")
        lines.append(f"- Falsely flagged (FP_on_hard_negative): {fp_hn}")
        lines.append(f"- Specificity: {specificity:.3f}")

    # Errors
    errors = [v for v in verdicts if v.verdict == "ERROR"]
    if errors:
        lines.extend(["", "## Errors (phi_gate raised an exception)", ""])
        for v in errors:
            lines.append(f"- {v.record_id}: {v.notes}")

    # Failure details
    failures = [
        v
        for v in verdicts
        if v.verdict in ("FN", "FP", "FP_on_hard_negative", "TP_category_wrong", "TP_span_wrong")
    ]
    if failures:
        lines.extend(["", "## Failures (for diagnosis)", ""])
        for v in failures:
            lines.append(f"### {v.record_id} | {v.verdict}")
            lines.append(f"- Expected: {v.expected_elements}")
            lines.append(f"- Detected: {v.detected_elements}")
            lines.append(f"- Notes: {v.notes}")
            lines.append("")

    lines.extend(
        [
            "",
            "## What to send back",
            "",
            "1. The JSONL output file (full verdict detail for every record).",
            "2. This summary file.",
            "3. A short prose paragraph: observed behavior that this structured report does not capture. Things like 'the skill consistently called SSN an ID', or 'envelope 2 fired on every query even for hard negatives'.",
            "4. Skill-specific diagnostics (which envelope fired, improvement loop activity, etc.) if the skill exposes them.",
            "",
        ]
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run corpus through phi_gate and score.")
    parser.add_argument("--corpus", type=Path, required=True)
    parser.add_argument("--answer-key", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument(
        "--round-label", type=str, default="Round", help="Label for summary file header"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip phi_gate call; populate verdicts with ERROR so you can validate the harness plumbing without the RePORTaLiN env.",
    )
    args = parser.parse_args(argv)

    corpus = load_corpus(args.corpus)
    answer_key = load_answer_key(args.answer_key)

    if not args.dry_run:
        try:
            phi_gate = import_phi_gate()
        except ImportError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            print(
                "Rerun with --dry-run to validate the harness plumbing without phi_gate.",
                file=sys.stderr,
            )
            return 2
    else:
        phi_gate = None

    verdicts = []
    for record in corpus:
        if record.record_id not in answer_key:
            print(
                f"WARN: record {record.record_id} in corpus but not in answer key; skipping.",
                file=sys.stderr,
            )
            continue
        answer = answer_key[record.record_id]

        if args.dry_run:
            gate = NormalizedGateResult(
                phi_detected=False,
                detected_entities=[],
                raw_output={"dry_run": True},
                error="dry_run mode; phi_gate not called",
            )
        else:
            gate = run_phi_gate(phi_gate, record)

        verdict = score_record(record, answer, gate)
        verdicts.append(verdict)
        print(f"{record.record_id}: {verdict.verdict}")

    # Write verdicts JSONL
    write_jsonl(
        args.output,
        ({**asdict(v), "raw_gate_output": repr(v.raw_gate_output)} for v in verdicts),
    )
    print(f"\nVerdicts written to {args.output}")

    # Write summary markdown
    summary = build_summary(verdicts, args.round_label)
    args.summary.parent.mkdir(parents=True, exist_ok=True)
    args.summary.write_text(summary, encoding="utf-8")
    print(f"Summary written to {args.summary}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
