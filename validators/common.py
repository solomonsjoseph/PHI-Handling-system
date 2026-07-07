from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    path: str
    message: str
    record_id: str = ""


@dataclass(frozen=True)
class ValidationResult:
    name: str
    ok: bool
    issues: tuple[ValidationIssue, ...] = ()


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid jsonl {path}:{line_number}") from exc
        records.append(parsed)
    return records


def corpus_files(corpus_dir: Path) -> list[Path]:
    return sorted(corpus_dir.rglob("*.jsonl"))
