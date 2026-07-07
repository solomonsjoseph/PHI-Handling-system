from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .common import ValidationIssue, ValidationResult

NAME = "hash_validator"


def _candidate_paths(corpus_dir: Path, manifest_path: Path, manifest_key: str, raw_path: str) -> list[Path]:
    path = Path(raw_path)
    candidates = [path]
    if not path.is_absolute():
        candidates.append(manifest_path.parent / path)
        candidates.append(corpus_dir / path)
        candidates.append(corpus_dir / f"{manifest_key}.jsonl")
    seen: set[Path] = set()
    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _resolve_existing(corpus_dir: Path, manifest_path: Path, manifest_key: str, raw_path: str) -> Path | None:
    for candidate in _candidate_paths(corpus_dir, manifest_path, manifest_key, raw_path):
        if candidate.is_file():
            return candidate
    return None


def validate(corpus_dir: Path, manifest_path: Path | None = None) -> ValidationResult:
    issues: list[ValidationIssue] = []
    if manifest_path is None or not manifest_path.is_file():
        issues.append(
            ValidationIssue(
                code="MISSING_MANIFEST_FILE",
                path=str(manifest_path or ""),
                message="manifest file is required",
            )
        )
        return ValidationResult(NAME, ok=False, issues=tuple(issues))

    manifest: dict[str, Any] = json.loads(manifest_path.read_text())
    files = manifest.get("files", {})
    if not isinstance(files, dict):
        issues.append(
            ValidationIssue(
                code="MISSING_MANIFEST_FILE",
                path=str(manifest_path),
                message="manifest files map is missing",
            )
        )
        return ValidationResult(NAME, ok=False, issues=tuple(issues))

    for manifest_key, info in files.items():
        if not isinstance(info, dict):
            issues.append(
                ValidationIssue(
                    code="MISSING_MANIFEST_FILE",
                    path=str(manifest_path),
                    message=f"manifest entry {manifest_key} is invalid",
                )
            )
            continue
        raw_path = str(info.get("path", ""))
        resolved = _resolve_existing(corpus_dir, manifest_path, str(manifest_key), raw_path)
        if resolved is None:
            issues.append(
                ValidationIssue(
                    code="MISSING_MANIFEST_FILE",
                    path=raw_path,
                    message=f"manifest entry {manifest_key} file is missing",
                )
            )
            continue
        expected = str(info.get("sha256", ""))
        actual = hashlib.sha256(resolved.read_bytes()).hexdigest()
        if actual != expected:
            issues.append(
                ValidationIssue(
                    code="HASH_MISMATCH",
                    path=str(resolved),
                    message=f"manifest entry {manifest_key} sha256 mismatch",
                )
            )

    # Reverse check: every corpus file must be accounted for in the manifest.
    # Without this, a file added to corpus_dir but never registered in the
    # manifest silently escapes hash verification entirely.
    manifest_resolved: set[Path] = set()
    for manifest_key, info in files.items():
        if not isinstance(info, dict):
            continue
        raw_path = str(info.get("path", ""))
        resolved = _resolve_existing(corpus_dir, manifest_path, str(manifest_key), raw_path)
        if resolved is not None:
            manifest_resolved.add(resolved.resolve())

    for actual_file in corpus_dir.rglob("*.jsonl"):
        if actual_file.resolve() not in manifest_resolved:
            issues.append(
                ValidationIssue(
                    code="UNMANIFESTED_FILE",
                    path=str(actual_file),
                    message=f"file {actual_file} exists in corpus but is not listed in the manifest",
                )
            )

    return ValidationResult(NAME, ok=not issues, issues=tuple(issues))
