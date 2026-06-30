"""Dataset cleanup audit envelope for the staging datasets directory.

Runs on the staging tree (``config.STAGING_DATASETS_DIR`` by default) **after**
raw-data extraction and **before** promotion to the trio bundle.

Since Note 4/18, duplicate-FILE detection and junk-FILE filtering happen at the
RAW file level BEFORE extraction — the ``dataset-deduplication`` skill at
orchestrator phase 2 plus the ``_forms_manifest.yaml`` ``reject:`` gate. By the
time JSONL staging files exist they are already deduplicated and junk-free, so
the old JSONL-level dedup/junk passes (``SUSPECTED_DUPLICATE_PAIRS``,
``JUNK_PATTERNS``, the ``clean_trio_datasets`` row-reading merges) are redundant
and have been removed. **No row VALUES are read here any more.**

The one surviving, load-bearing responsibility is the **audit envelope**: this
module serializes the unified dataset audit report to
``config.AUDIT_DATASET_REPORT_PATH`` and writes the per-dataset ``as_written``
cleanup ledgers from the upstream extraction column-drop events. Cleanup
propagation (Step 1.8) and the cleanup verifier both depend on those artifacts.
Audit lives under ``output/{STUDY}/audit/`` and survives the run — it is
authoritative.

No raw-data access occurs — this module only touches the output zone
(``output/{STUDY}/audit/``) for its audit envelope and reads staging file NAMES
(never values).

Usage:
    >>> from scripts.extraction.dataset_cleanup import emit_dataset_cleanup_audit_envelope
    >>> report = emit_dataset_cleanup_audit_envelope(
    ...     datasets_dir,
    ...     extracted_drop_events=[...],
    ...     study_name="Indo-VAP",
    ... )
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import config
from scripts.audit.ledger import (
    CLEANUP_LEDGER_FILENAME,
    LedgerWriter,
    dataset_cleanup_ledger_path,
    ensure_no_llm_sentinel,
    remove_dataset_no_llm_sentinels,
)
from scripts.extraction.io import (
    atomic_write_json,
)
from scripts.security.secure_env import assert_output_zone, assert_write_zone
from scripts.utils.logging_system import get_logger

logger = get_logger(__name__)

__all__ = [
    "UnscrubbedDatasetError",
    "clean_trio_datasets",
    "emit_dataset_cleanup_audit_envelope",
]


# ── Data model ──────────────────────────────────────────────────────────────


@dataclass
class CleanupReport:
    """Summary of dataset cleanup actions.

    ``junk_removed`` / ``duplicates_merged`` are populated from the manifest
    ``reject:`` list (classified by :func:`_classify_rejected_files`) so every
    excluded raw file is audited; ``duplicates_skipped`` stays for schema
    stability. No row VALUES are read — classification is filename-only.
    """

    junk_removed: list[str] = field(default_factory=list)
    duplicates_merged: list[dict[str, str]] = field(default_factory=list)
    duplicates_skipped: list[dict[str, str]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def total_actions(self) -> int:
        return len(self.junk_removed) + len(self.duplicates_merged)


def _norm_stem(name: str) -> str:
    """Normalized stem for duplicate matching: lowercase alnum, ``_<n>`` stripped."""
    stem = re.sub(r"_\d+$", "", Path(name).stem.lower())
    return re.sub(r"[^a-z0-9]", "", stem)


def _classify_rejected_files(
    rejected: list[str], surviving_stems: list[str]
) -> tuple[list[str], list[dict[str, str]]]:
    """Split manifest-rejected filenames into junk vs duplicate (filename-only).

    A reject whose normalized stem matches (or prefix-overlaps) a surviving
    published stem is recorded as a duplicate of it; otherwise it is standalone
    junk. Deterministic and value-free — reads only filenames, never row values.
    Mirrors the operator's curated ``reject:`` intent for the audit trail.
    """
    surv = {_norm_stem(s): s for s in surviving_stems}
    junk: list[str] = []
    dups: list[dict[str, str]] = []
    for fn in sorted(rejected):
        rn = _norm_stem(fn)
        twin = surv.get(rn) or next(
            (orig for sn, orig in surv.items() if sn and (sn.startswith(rn) or rn.startswith(sn))),
            None,
        )
        if twin:
            dups.append(
                {
                    "removed": fn,
                    "kept": twin,
                    "reason": f"manifest reject-list: duplicate of surviving {twin}",
                }
            )
        else:
            junk.append(fn)
    return junk, dups


class UnscrubbedDatasetError(Exception):
    """Retained for backwards-compatible imports only — no longer raised.

    The dataset-cleanup leg no longer reads row values (the JSONL-level dedup
    that needed the ``_phi_scrubbed`` marker check was removed in Note 18), so
    there is nothing left to fail closed on here. Kept so external callers that
    import the name do not break.
    """


# ── Core ────────────────────────────────────────────────────────────────────


def _serialize_audit(
    report: CleanupReport,
    extraction_drops: list[dict[str, Any]],
    study_name: str,
    out_path: Path,
) -> None:
    """Write the unified audit report for this cleanup leg.

    Flat schema (per cleanup-propagation plan):
        {
          "study": str,
          "generated_utc": "YYYY-MM-DDTHH:MM:SSZ",
          "leg": "dataset",
          "removed": [
            {scope, name, file, sheet, reason, kept}, ...
          ]
        }

    ``extraction_drops`` (upstream column-level drops from the extraction
    leg) pass through verbatim. The junk-file / duplicate-file loops below are
    retained for schema stability but iterate empty lists (file-level cleanup
    moved to raw-file dedup before extraction, Note 18).
    """
    removed: list[dict[str, Any]] = list(extraction_drops)  # pass-through first

    # Junk files → dataset-junk-file events
    for filename in report.junk_removed:
        stem = Path(filename).stem
        removed.append(
            {
                "scope": "dataset-junk-file",
                "name": stem,
                "file": filename,
                "sheet": None,
                "reason": "known junk artifact",
                "kept": None,
            }
        )

    # Duplicate-pair merges → dataset-duplicate-file events
    for dup in report.duplicates_merged:
        removed_name = dup.get("removed", "")
        kept_name = dup.get("kept", "")
        removed.append(
            {
                "scope": "dataset-duplicate-file",
                "name": Path(removed_name).stem if removed_name else "",
                "file": removed_name,
                "sheet": None,
                "reason": dup.get("reason", ""),
                "kept": kept_name or None,
            }
        )

    payload: dict[str, Any] = {
        "study": study_name,
        "generated_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "leg": "dataset",
        "removed": removed,
        "skipped": report.duplicates_skipped,
        "errors": report.errors,
    }

    assert_output_zone(out_path.parent)
    atomic_write_json(out_path, payload)


def _emit_as_written_ledger(
    *,
    extracted_drop_events: list[dict[str, Any]],
    report: CleanupReport,
    audit_path: Path,
    study_name: str | None,
    dataset_files: list[str],
) -> None:
    """Write one cleanup as-written ledger under each dataset audit folder.

    Dual-write only — does not modify the legacy audit report.
    """
    audit_dir = audit_path.parent
    ensure_no_llm_sentinel(audit_dir)
    remove_dataset_no_llm_sentinels(audit_dir)
    (audit_dir / CLEANUP_LEDGER_FILENAME).unlink(missing_ok=True)

    display_names = {Path(name).stem: name for name in dataset_files}
    grouped_events: dict[str, list[dict[str, Any]]] = {}

    def _append_event(dataset_file: str, event: dict[str, Any]) -> None:
        stem = Path(dataset_file).stem
        display_names.setdefault(stem, dataset_file)
        grouped_events.setdefault(stem, []).append(event)

    # Column-drop events (scope == "dataset-column" only)
    for event in extracted_drop_events:
        if event.get("scope") != "dataset-column":
            continue
        _append_event(
            event["file"],
            {
                "variable_id": event["name"],
                "action": "dataset_column_drop",
                "rationale": event.get("reason", ""),
                "dataset_file": event["file"],
            },
        )

    # Junk file removals
    for filename in report.junk_removed:
        stem = Path(filename).stem
        _append_event(
            filename,
            {
                "variable_id": stem,
                "action": "dataset_junk_file",
                "rationale": "known junk artifact",
                "dataset_file": filename,
            },
        )

    # Duplicate-pair merges
    for dup in report.duplicates_merged:
        removed_file = dup.get("removed", "")
        stem = Path(removed_file).stem
        _append_event(
            removed_file,
            {
                "variable_id": stem,
                "action": "dataset_duplicate_file",
                "rationale": dup.get("reason", ""),
                "dataset_file": removed_file,
            },
        )

    for stem in sorted(display_names):
        writer = LedgerWriter(
            output_path=dataset_cleanup_ledger_path(audit_dir, display_names[stem]),
            study=study_name,
            leg="dataset",
            sentinel_dir=audit_dir,
        )
        for event in grouped_events.get(stem, []):
            writer.add_cleanup_event(
                form=stem,
                variable_id=event["variable_id"],
                action=event["action"],
                rule_project_category="cleanup",
                rationale=event["rationale"],
                dataset_file=event["dataset_file"],
                count=None,
            )
        writer.flush()


def emit_dataset_cleanup_audit_envelope(
    datasets_dir: Path | None = None,
    *,
    extracted_drop_events: list[dict[str, Any]] | None = None,
    study_name: str | None = None,
    audit_path: Path | None = None,
    raw_datasets_dir: Path | None = None,
) -> CleanupReport:
    """Emit the unified dataset-cleanup audit envelope (audit-only).

    File-level junk/duplicate handling moved to raw-file dedup before extraction
    (Note 4/18); this function no longer reads row values or removes/merges
    staging files. It serializes the upstream extraction column-drop events into
    the unified audit report plus the per-dataset ``as_written`` cleanup ledgers
    that cleanup propagation (Step 1.8) and the cleanup verifier depend on.

    The audit file is always written — even when ``datasets_dir`` is missing or
    empty — to guarantee a stable envelope downstream.

    Args:
        datasets_dir: staging datasets directory (defaults to
            ``config.STAGING_DATASETS_DIR``); used only to enumerate file NAMES.
        extracted_drop_events: upstream column-drop events, passed through
            verbatim into the audit. Defaults to ``[]``.
        study_name: study identifier for the audit envelope. Defaults to
            ``config.STUDY_NAME``.
        audit_path: destination for the unified audit JSON. Defaults to
            ``config.AUDIT_DATASET_REPORT_PATH``.

    Returns:
        CleanupReport (always empty action lists; retained for schema stability).
    """
    if datasets_dir is None:
        datasets_dir = config.STAGING_DATASETS_DIR
    if extracted_drop_events is None:
        extracted_drop_events = []
    if study_name is None:
        study_name = config.STUDY_NAME
    if audit_path is None:
        audit_path = config.AUDIT_DATASET_REPORT_PATH

    assert_write_zone(datasets_dir)

    report = CleanupReport()
    dataset_files: list[str] = []

    if datasets_dir.is_dir():
        dataset_files = sorted(f.name for f in datasets_dir.glob("*.jsonl"))
        logger.info(
            "Dataset cleanup (audit-only): %d JSONL files in %s",
            len(dataset_files),
            datasets_dir,
        )
    else:
        logger.info(
            "Datasets directory does not exist — emitting empty audit envelope: %s",
            datasets_dir,
        )

    # Phase 2b: record manifest-rejected raw files (junk / duplicate) so the
    # audit trail explains every excluded file, not just dropped columns.
    # Fail-soft: a manifest hiccup must never break the audit envelope.
    if raw_datasets_dir is None:
        raw_datasets_dir = config.DATASETS_DIR
    try:
        from scripts.extraction.forms_manifest import check_forms_manifest

        rejected = sorted(check_forms_manifest(raw_datasets_dir).rejected_files)
        if rejected:
            surviving = [Path(name).stem for name in dataset_files]
            report.junk_removed, report.duplicates_merged = _classify_rejected_files(
                rejected, surviving
            )
            logger.info(
                "Recorded %d manifest-rejected file(s): %d junk, %d duplicate",
                len(rejected),
                len(report.junk_removed),
                len(report.duplicates_merged),
            )
    except Exception as exc:  # audit must stay best-effort
        logger.warning("Could not record manifest rejects (non-fatal): %s", exc)

    # Phase 3: Always emit unified audit (even on empty/missing input)
    _serialize_audit(report, extracted_drop_events, study_name, audit_path)

    # Phase 4: Per-dataset as_written cleanup ledgers (extraction drops only)
    _emit_as_written_ledger(
        extracted_drop_events=extracted_drop_events,
        report=report,
        audit_path=audit_path,
        study_name=study_name,
        dataset_files=dataset_files,
    )

    return report


def clean_trio_datasets(
    datasets_dir: Path | None = None,
    *,
    extracted_drop_events: list[dict[str, Any]] | None = None,
    study_name: str | None = None,
    audit_path: Path | None = None,
) -> CleanupReport:
    """Compatibility alias for the retired JSONL cleanup entry point.

    The old row-level junk/duplicate merge behavior is gone. This alias exists
    only for older tests/imports and delegates to the audit-envelope emitter.
    Production code should call :func:`emit_dataset_cleanup_audit_envelope`.
    """
    return emit_dataset_cleanup_audit_envelope(
        datasets_dir,
        extracted_drop_events=extracted_drop_events,
        study_name=study_name,
        audit_path=audit_path,
    )
