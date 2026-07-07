from __future__ import annotations

import re
from pathlib import Path

from .common import ValidationIssue, ValidationResult, corpus_files, iter_jsonl

NAME = "citation_validator"

# Placeholder / non-citation junk that a bare non-empty check would accept.
# Legitimate citation phrasing is too open-ended to positively enumerate
# ("Income Tax Act 1961 s.139A", "DICOM PS3.15 Annex E", "ABDM HDMP 2020" are
# all real but unpredictable in wording), so this only rejects known-junk
# shapes rather than requiring a specific one.
_PLACEHOLDER_RE = re.compile(
    r"^(?:n/?a|none|null|tbd|todo|fixme|xxx|citation needed|unknown|pending)$",
    re.IGNORECASE,
)


def _is_recognized_citation(value: str) -> bool:
    stripped = value.strip()
    if len(stripped) < 8 or _PLACEHOLDER_RE.match(stripped):
        return False
    # Real citations are multi-word (an act/standard name plus a year, rule,
    # or section marker); a single token is more likely a stray placeholder.
    return " " in stripped


def _has_nonempty_string(values: object) -> bool:
    return isinstance(values, list) and any(isinstance(value, str) and value.strip() for value in values)


def _has_recognized_citation(values: object) -> bool:
    return isinstance(values, list) and any(
        isinstance(value, str) and _is_recognized_citation(value) for value in values
    )


def validate(corpus_dir: Path, manifest_path: Path | None = None) -> ValidationResult:
    issues: list[ValidationIssue] = []
    for path in corpus_files(corpus_dir):
        path_str = str(path)
        for record in iter_jsonl(path):
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("record_id", ""))
            citations = record.get("authority_citations")
            record_has_authority = _has_nonempty_string(citations)
            if not record_has_authority:
                issues.append(
                    ValidationIssue(
                        code="MISSING_AUTHORITY",
                        path=path_str,
                        record_id=record_id,
                        message=f"record {record_id} missing record-level authority citations",
                    )
                )
            elif not _has_recognized_citation(citations):
                issues.append(
                    ValidationIssue(
                        code="UNRECOGNIZED_AUTHORITY",
                        path=path_str,
                        record_id=record_id,
                        message=(
                            f"record {record_id} authority_citations present but too short "
                            f"or single-token to be a real citation"
                        ),
                    )
                )
            spans = record.get("gold_spans", [])
            if not isinstance(spans, list):
                continue
            for span_index, span in enumerate(spans):
                if not isinstance(span, dict):
                    continue
                span_authority = span.get("authority")
                span_has_authority = isinstance(span_authority, str) and bool(span_authority.strip())
                if not record_has_authority and not span_has_authority:
                    issues.append(
                        ValidationIssue(
                            code="MISSING_AUTHORITY",
                            path=path_str,
                            record_id=record_id,
                            message=f"record {record_id} span {span_index} missing authority",
                        )
                    )
                elif span_has_authority and not _is_recognized_citation(span_authority):
                    issues.append(
                        ValidationIssue(
                            code="UNRECOGNIZED_AUTHORITY",
                            path=path_str,
                            record_id=record_id,
                            message=(
                                f"record {record_id} span {span_index} authority "
                                f"{span_authority!r} looks like a placeholder"
                            ),
                        )
                    )
    return ValidationResult(NAME, ok=not issues, issues=tuple(issues))
