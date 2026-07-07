from __future__ import annotations

from pathlib import Path

from .common import ValidationIssue, ValidationResult, corpus_files, iter_jsonl

NAME = "offset_validator"


def validate(corpus_dir: Path, manifest_path: Path | None = None) -> ValidationResult:
    issues: list[ValidationIssue] = []
    for path in corpus_files(corpus_dir):
        rel_path = str(path)
        for record in iter_jsonl(path):
            record_id = str(record.get("record_id", ""))
            text = record.get("text", "")
            if not isinstance(text, str):
                text = ""
            spans = record.get("gold_spans", [])
            if not isinstance(spans, list):
                continue
            for index, span in enumerate(spans):
                if not isinstance(span, dict):
                    issues.append(
                        ValidationIssue(
                            code="OFFSET_MISMATCH",
                            path=rel_path,
                            record_id=record_id,
                            message=f"record {record_id} span {index} is not an object",
                        )
                    )
                    continue
                start = span.get("start")
                end = span.get("end")
                value = span.get("value")
                if not isinstance(start, int) or not isinstance(end, int) or not isinstance(value, str):
                    issues.append(
                        ValidationIssue(
                            code="OFFSET_MISMATCH",
                            path=rel_path,
                            record_id=record_id,
                            message=f"record {record_id} span {index} has invalid offset fields",
                        )
                    )
                    continue
                if start < 0 or end < start or end > len(text) or text[start:end] != value:
                    issues.append(
                        ValidationIssue(
                            code="OFFSET_MISMATCH",
                            path=rel_path,
                            record_id=record_id,
                            message=f"record {record_id} span {index} offset mismatch",
                        )
                    )
    return ValidationResult(NAME, ok=not issues, issues=tuple(issues))
