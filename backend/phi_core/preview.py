"""Row-level review preview (Phase D).

The reviewer's spot-check moment. For each dataset file we sample up to
``max_samples`` non-empty cells and show the reviewer:

* ``column`` — column name
* ``action`` — the decision the pipeline will apply
* ``original_masked`` — a partially-masked view of the RAW cell so the
  preview itself does NOT re-emit PHI (mask keeps first + last 2 chars)
* ``redacted`` — the exact string the export will contain after the
  chosen action is applied

The masking rule is identical to the Publish Guard finding masker so the
preview UI cannot itself become a PHI leak surface.

Reviewer must tick "I have reviewed the sample" before Submit enables
(enforced in the UI; server-side the actual-knowledge attestation
continues to gate the submit endpoint).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .file_readers import iter_dataset_rows
from .agents.reasoning import _apply_action, PseudonymRegistry
from .crypto import pseudonym_salt


MAX_SAMPLES_PER_FILE = 5
MAX_SAMPLES_PER_COLUMN = 2


def _mask_original(value: str) -> str:
    """Partial-mask a raw cell value for the preview UI."""
    if value is None:
        return ""
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    return s[:2] + "*" * (len(s) - 4) + s[-2:]


def build_preview(
    session: dict[str, Any],
    max_samples_per_file: int = MAX_SAMPLES_PER_FILE,
) -> dict[str, Any]:
    """Return spot-check samples per dataset file.

    Only dataset files are sampled (narrative and metadata files are read
    fully by the scrub-text pipeline and don't have per-column decisions).
    """
    files = session.get("files") or []
    decisions = session.get("agent_decisions") or []
    # index decisions by (file_id, column) -> action
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for d in decisions:
        by_key[(d.get("file_id", ""), d.get("column", ""))] = d

    registry = PseudonymRegistry(salt=pseudonym_salt(session.get("id", "")))
    out_files: list[dict[str, Any]] = []
    for f in files:
        if f.get("kind") != "dataset":
            continue
        src = f.get("stored_path")
        if not src or not Path(src).exists():
            continue
        subtype = f.get("subtype", "csv")
        samples: list[dict[str, Any]] = []
        per_column_count: dict[str, int] = {}
        try:
            for row_idx, row in iter_dataset_rows(Path(src), subtype):
                if len(samples) >= max_samples_per_file:
                    break
                for col, val in row.items():
                    if val is None or val == "":
                        continue
                    if per_column_count.get(col, 0) >= MAX_SAMPLES_PER_COLUMN:
                        continue
                    if len(samples) >= max_samples_per_file:
                        break
                    d = by_key.get((f.get("file_id", ""), col))
                    if not d:
                        continue
                    action = d.get("action", "keep")
                    if action == "keep":
                        redacted = _mask_original(str(val))
                        masked = True
                    else:
                        redacted = _apply_action(str(val), action, col, registry=registry)
                        masked = False
                    samples.append({
                        "column": col,
                        "action": action,
                        "row_index": row_idx,
                        "original_masked": _mask_original(str(val)),
                        "redacted": redacted,
                        "masked": masked,
                    })
                    per_column_count[col] = per_column_count.get(col, 0) + 1
        except Exception as e:
            samples.append({"error": f"{type(e).__name__}: {e}"})

        out_files.append({
            "file_id": f.get("file_id"),
            "file_name": f.get("original_name"),
            "kind": f.get("kind"),
            "samples": samples,
        })
    return {
        "session_id": session.get("id"),
        "files": out_files,
        "max_samples_per_file": max_samples_per_file,
        "note": (
            "Original cell values are partial-masked for reviewer safety; "
            "redacted column shows the exact export string that will be written, "
            "except for kept columns, whose redacted value is also masked so a "
            "reviewer confirming a kept column is clinical only sees the shape."
        ),
    }
