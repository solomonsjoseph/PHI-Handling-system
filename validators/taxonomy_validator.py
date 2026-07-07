from __future__ import annotations

import re
from pathlib import Path

from .common import ValidationIssue, ValidationResult, corpus_files, iter_jsonl

NAME = "taxonomy_validator"

# entity_type is meant to be a closed-vocabulary taxonomy label (e.g. NAME_A,
# SSN, MRN), not free text. A bare isinstance(str) check accepts "", "asdf",
# or a stray sentence. Require the SCREAMING_SNAKE_CASE shape every generator
# in this repo actually emits, which catches typos/placeholders without
# needing to hardcode an exhaustive whitelist that drifts as generators change.
_ENTITY_TYPE_SHAPE_RE = re.compile(r"^[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)*$")

REQUIRED_RECORD_FIELDS = {
    "record_id",
    "text",
    "gold_spans",
    "jurisdiction",
    "format",
    "authority_citations",
}
REQUIRED_SPAN_FIELDS = {"start", "end", "value", "entity_type", "detection_regime"}
ACCEPTED_DETECTION_REGIMES = {
    "rule_applicable",
    "contextual_ner_required",
    "conflict_case",
}


def _record_id(record: object) -> str:
    if isinstance(record, dict):
        return str(record.get("record_id", ""))
    return ""


def validate(corpus_dir: Path, manifest_path: Path | None = None) -> ValidationResult:
    issues: list[ValidationIssue] = []
    for path in corpus_files(corpus_dir):
        path_str = str(path)
        for record_index, record in enumerate(iter_jsonl(path)):
            record_id = _record_id(record)
            if not isinstance(record, dict):
                issues.append(
                    ValidationIssue(
                        code="BAD_SCHEMA",
                        path=path_str,
                        record_id=record_id,
                        message=f"record index {record_index} is not an object",
                    )
                )
                continue

            missing = sorted(REQUIRED_RECORD_FIELDS - set(record))
            if missing:
                issues.append(
                    ValidationIssue(
                        code="BAD_SCHEMA",
                        path=path_str,
                        record_id=record_id,
                        message=f"record {record_id} missing required fields: {', '.join(missing)}",
                    )
                )

            if not isinstance(record.get("record_id"), str):
                issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record index {record_index} has invalid record_id", record_id))
            if not isinstance(record.get("text"), str):
                issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record {record_id} has invalid text", record_id))
            if not isinstance(record.get("gold_spans"), list):
                issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record {record_id} has invalid gold_spans", record_id))
                continue
            if not isinstance(record.get("jurisdiction"), str):
                issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record {record_id} has invalid jurisdiction", record_id))
            if not isinstance(record.get("format"), str):
                issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record {record_id} has invalid format", record_id))
            if not isinstance(record.get("authority_citations"), list):
                issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record {record_id} has invalid authority_citations", record_id))

            for span_index, span in enumerate(record.get("gold_spans", [])):
                if not isinstance(span, dict):
                    issues.append(
                        ValidationIssue(
                            code="BAD_SCHEMA",
                            path=path_str,
                            record_id=record_id,
                            message=f"record {record_id} span {span_index} is not an object",
                        )
                    )
                    continue
                span_missing = sorted(REQUIRED_SPAN_FIELDS - set(span))
                if span_missing:
                    issues.append(
                        ValidationIssue(
                            code="BAD_SCHEMA",
                            path=path_str,
                            record_id=record_id,
                            message=f"record {record_id} span {span_index} missing required fields: {', '.join(span_missing)}",
                        )
                    )
                if not isinstance(span.get("start"), int) or not isinstance(span.get("end"), int):
                    issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record {record_id} span {span_index} has invalid offsets", record_id))
                if not isinstance(span.get("value"), str):
                    issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record {record_id} span {span_index} has invalid value field", record_id))
                entity_type = span.get("entity_type")
                if not isinstance(entity_type, str):
                    issues.append(ValidationIssue("BAD_SCHEMA", path_str, f"record {record_id} span {span_index} has invalid entity_type", record_id))
                elif not _ENTITY_TYPE_SHAPE_RE.match(entity_type):
                    issues.append(
                        ValidationIssue(
                            code="BAD_ENTITY_TYPE_SHAPE",
                            path=path_str,
                            record_id=record_id,
                            message=(
                                f"record {record_id} span {span_index} entity_type "
                                f"{entity_type!r} is not SCREAMING_SNAKE_CASE"
                            ),
                        )
                    )
                regime = span.get("detection_regime")
                if "detection_regime" in span and regime not in ACCEPTED_DETECTION_REGIMES:
                    issues.append(
                        ValidationIssue(
                            code="BAD_DETECTION_REGIME",
                            path=path_str,
                            record_id=record_id,
                            message=f"record {record_id} span {span_index} has unsupported detection_regime",
                        )
                    )
    return ValidationResult(NAME, ok=not issues, issues=tuple(issues))
