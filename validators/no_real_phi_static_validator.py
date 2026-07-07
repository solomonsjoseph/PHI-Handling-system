from __future__ import annotations

from pathlib import Path

from .common import ValidationIssue, ValidationResult, corpus_files, iter_jsonl

NAME = "no_real_phi_static_validator"
BANNED_SENTINELS = (
    ("gmail.com", "public_email_domain"),
    ("yahoo.com", "public_email_domain"),
    ("hotmail.com", "public_email_domain"),
    ("outlook.com", "public_email_domain"),
    ("icloud.com", "public_email_domain"),
    ("protonmail.com", "public_email_domain"),
    ("real patient", "real_patient_phrase"),
    ("actual patient", "actual_patient_phrase"),
    ("078-05-1120", "known_leaked_ssn"),  # widely-circulated "Woolworths" SSN
    ("219-09-9999", "known_leaked_ssn"),
    ("457-55-5462", "known_leaked_ssn"),
)


def _text_with_gold_spans_masked(record: dict) -> str:
    """Blank out gold-span regions before scanning for real-PHI sentinels.

    Synthetic generators deliberately use public email domains (gmail.com
    etc.) and PHI-shaped values *as the gold-span content itself* -- that is
    the corpus's job. A sentinel hit is only meaningful outside a gold span,
    where it indicates unlabeled real PHI the generator didn't intend.
    """
    text = record.get("text", "")
    if not isinstance(text, str):
        return ""
    chars = list(text)
    spans = record.get("gold_spans", [])
    if not isinstance(spans, list):
        return text
    for span in spans:
        if not isinstance(span, dict):
            continue
        start = span.get("start")
        end = span.get("end")
        if isinstance(start, int) and isinstance(end, int):
            bounded_start = max(0, min(start, len(chars)))
            bounded_end = max(bounded_start, min(end, len(chars)))
            for index in range(bounded_start, bounded_end):
                chars[index] = " "
    return "".join(chars)


def validate(corpus_dir: Path, manifest_path: Path | None = None) -> ValidationResult:
    issues: list[ValidationIssue] = []
    for path in corpus_files(corpus_dir):
        path_str = str(path)
        for record in iter_jsonl(path):
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("record_id", ""))
            text = _text_with_gold_spans_masked(record)
            if not text:
                continue
            lowered = text.lower()
            for banned, category in BANNED_SENTINELS:
                if banned in lowered:
                    issues.append(
                        ValidationIssue(
                            code="REAL_PHI_SENTINEL",
                            path=path_str,
                            record_id=record_id,
                            message=f"record {record_id} matched sentinel category {category}",
                        )
                    )
    return ValidationResult(NAME, ok=not issues, issues=tuple(issues))
