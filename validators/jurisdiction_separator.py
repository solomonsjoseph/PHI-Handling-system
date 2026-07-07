from __future__ import annotations

from pathlib import Path

from .common import ValidationIssue, ValidationResult, corpus_files, iter_jsonl

NAME = "jurisdiction_separator"
IMPLEMENTED_JURISDICTIONS = {"us", "in", "eu", "br", "au", "ug"}


def _expected_folder(corpus_dir: Path, path: Path) -> str:
    try:
        rel = path.relative_to(corpus_dir)
    except ValueError:
        return ""
    return rel.parts[0] if len(rel.parts) > 1 else ""


def validate(corpus_dir: Path, manifest_path: Path | None = None) -> ValidationResult:
    issues: list[ValidationIssue] = []
    for path in corpus_files(corpus_dir):
        expected = _expected_folder(corpus_dir, path)
        path_str = str(path)
        for record in iter_jsonl(path):
            if not isinstance(record, dict):
                continue
            record_id = str(record.get("record_id", ""))
            jurisdiction = record.get("jurisdiction")
            if not isinstance(jurisdiction, str):
                issues.append(
                    ValidationIssue(
                        code="JURISDICTION_MISMATCH",
                        path=path_str,
                        record_id=record_id,
                        message=f"record {record_id} jurisdiction is missing for folder {expected or 'root'}",
                    )
                )
                continue
            if expected == "file_formats":
                if jurisdiction not in IMPLEMENTED_JURISDICTIONS:
                    issues.append(
                        ValidationIssue(
                            code="JURISDICTION_MISMATCH",
                            path=path_str,
                            record_id=record_id,
                            message=f"record {record_id} jurisdiction {jurisdiction} is not allowed in file_formats",
                        )
                    )
            elif expected and jurisdiction != expected:
                issues.append(
                    ValidationIssue(
                        code="JURISDICTION_MISMATCH",
                        path=path_str,
                        record_id=record_id,
                        message=f"record {record_id} jurisdiction {jurisdiction} does not match folder {expected}",
                    )
                )
            elif not expected:
                issues.append(
                    ValidationIssue(
                        code="JURISDICTION_MISMATCH",
                        path=path_str,
                        record_id=record_id,
                        message=f"record {record_id} is not under a jurisdiction folder",
                    )
                )
    return ValidationResult(NAME, ok=not issues, issues=tuple(issues))
