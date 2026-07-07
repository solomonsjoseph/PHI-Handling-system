from __future__ import annotations

import json
from pathlib import Path

from .common import ValidationIssue, ValidationResult, corpus_files, iter_jsonl

NAME = "format_parse_validator"
JSON_FORMATS = {"dicom_header", "fhir_json"}
OPTIONAL_JSON_FORMATS = {"xlsx"}
PLAIN_TEXT_FORMATS = {"text"}


def _issue(path: Path, record_id: str, category: str) -> ValidationIssue:
    return ValidationIssue(
        code="FORMAT_PARSE_FAIL",
        path=str(path),
        record_id=record_id,
        message=f"record {record_id} failed {category} format parse",
    )

def _looks_like_json_text(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return True
    if not stripped.startswith("["):
        return False
    if len(stripped) == 1:
        return True
    return stripped[1] in "{\"[]0123456789tfn-"



def validate(corpus_dir: Path, manifest_path: Path | None = None) -> ValidationResult:
    issues: list[ValidationIssue] = []
    for path in corpus_files(corpus_dir):
        for record in iter_jsonl(path):
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("record_id", ""))
            text = record.get("text")
            fmt = record.get("format")
            if not isinstance(text, str):
                issues.append(_issue(path, record_id, "text"))
                continue
            if not isinstance(fmt, str):
                issues.append(_issue(path, record_id, "format"))
                continue

            if fmt in JSON_FORMATS:
                try:
                    json.loads(text)
                except json.JSONDecodeError:
                    issues.append(_issue(path, record_id, fmt))
            elif fmt in OPTIONAL_JSON_FORMATS and _looks_like_json_text(text):
                try:
                    json.loads(text)
                except json.JSONDecodeError:
                    issues.append(_issue(path, record_id, fmt))
            elif fmt == "hl7v2":
                if "MSH|" not in text or "PID|" not in text:
                    issues.append(_issue(path, record_id, fmt))
            elif fmt == "eml":
                if "Subject:" not in text:
                    issues.append(_issue(path, record_id, fmt))
            elif fmt in PLAIN_TEXT_FORMATS or fmt in OPTIONAL_JSON_FORMATS:
                continue
            else:
                issues.append(_issue(path, record_id, f"unrecognized format {fmt!r}"))
    return ValidationResult(NAME, ok=not issues, issues=tuple(issues))
