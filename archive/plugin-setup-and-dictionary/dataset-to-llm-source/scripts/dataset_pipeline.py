#!/usr/bin/env python3
"""Canonical dataset pipeline for RePORT AI Portal — staged extraction.

This is the single dataset pipeline module for the active single-study,
local-first pipeline. It discovers tabular study files under
``data/raw/{STUDY_NAME}/datasets/``, normalises their rows, and writes
the resulting JSONL into the study's **staging workspace**
(``tmp/{STUDY_NAME}/datasets/`` by default). A subsequent publish step
atomically promotes the staging bundle into
``output/{STUDY}/llm_source/dataset_schema/files/``.

Datasets may contain PHI at extraction time. They remain in AMBER staging
until ``scripts.security.phi_scrub`` runs at Step 1.6; only scrubbed staging
artifacts are later published to the llm_source dataset schema.

What this module does:
    1. Discover supported dataset files for the active study
    2. Read ``.xlsx`` and ``.csv`` files
    3. Normalize rows into JSONL-safe records
    4. Write extraction output into the staging datasets directory
    5. Surface per-column drop events from duplicate-column cleanup so a
       later audit pass can record them.

Supported formats:
    - ``.xlsx`` via ``openpyxl``
    - ``.csv`` via ``pandas.read_csv`` (single-file load; preserves one output file per input)

Discovery rules:
    - Only files directly under ``data/raw/{STUDY_NAME}/datasets/`` are considered.
    - Hidden files, OS junk, and Excel lock files are ignored.

Notes:
    - Row iteration uses ``itertuples()`` instead of ``iterrows()`` to avoid dtype coercion and reduce overhead.
    - JSONL writes are committed atomically via temporary files and ``Path.replace()``.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Generator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, TypedDict, cast

import numpy as np
import openpyxl as _openpyxl
import pandas as pd
from tqdm import tqdm

import config
from __version__ import __version__ as _pipeline_version
from scripts.extraction.dedup import clean_duplicate_columns
from scripts.extraction.forms_manifest import (
    ManifestCheckResult,
    ManifestMismatchError,
    check_forms_manifest,
)
from scripts.extraction.io import (
    FILE_ENCODING,
    JSONL_EXT,
    atomic_write_jsonl,
    discover_files,
    promote_header,
    split_sheet_into_tables,
)
from scripts.extraction.io.file_discovery import (
    DEFAULT_JUNK_FILENAMES,
    SUPPORTED_TABULAR_EXTENSIONS,
)
from scripts.utils import logging_system as log
from scripts.utils.errors import format_for_log, wrap
from scripts.utils.integrity import hash_file as _hash_file
from scripts.utils.logging_system import get_logger

_PIPELINE_VERSION: str = _pipeline_version
"""Captured at import time so per-row provenance records a stable string."""

vlog = log.get_verbose_logger()

# Standard library logger for the gate — used by check_forms_manifest so
# that pytest's caplog fixture can capture messages during tests.
_gate_log = get_logger(__name__)

# ============================================================================
# Module Constants
# ============================================================================

# File Configuration
JSONL_FILE_EXTENSION: str = JSONL_EXT
"""Output file extension for JSONL format.

Alias of :data:`scripts.extraction.io.JSONL_EXT` kept for backward
compatibility with test imports.
"""

# Metadata Keys
METADATA_KEY: str = "_metadata"
"""Key name for metadata objects in JSONL records."""

SOURCE_FILE_KEY: str = "source_file"
"""Key name for source filename tracking in JSONL records."""

METADATA_TYPE_KEY: str = "type"
"""Key name for metadata type identifier."""

METADATA_COLUMNS_KEY: str = "columns"
"""Key name for column list in metadata."""

METADATA_NOTE_KEY: str = "note"
"""Key name for explanatory notes in metadata."""

# Metadata Values
METADATA_TYPE_COLUMN_STRUCTURE: str = "column_structure"
"""Metadata type value for files with column headers but no data rows."""

METADATA_NOTE_EMPTY_FILE: str = "File contains column headers but no data rows"
"""Standard message for empty files that contain only column structure."""

# Return Dictionary Keys
RESULT_FILES_FOUND: str = "files_found"
"""Key for total Excel files found in extraction results."""

RESULT_FILES_CREATED: str = "files_created"
"""Key for number of JSONL files successfully created."""

RESULT_TOTAL_RECORDS: str = "total_records"
"""Key for total number of records processed across all files."""

RESULT_ERRORS: str = "errors"
"""Key for list of error messages encountered during processing."""

RESULT_PROCESSING_TIME: str = "processing_time"
"""Key for total processing time in seconds."""

# Logging Configuration
DEFAULT_LOG_LEVEL: int = 20
"""Default logging level (INFO) when LOG_LEVEL not configured."""

MODULE_LOGGER_NAME: str = "scripts.extraction.dataset_pipeline"
"""Logger name for this module when run as standalone script."""

# Secure extraction constants
SUPPORTED_EXTENSIONS: tuple[str, ...] = SUPPORTED_TABULAR_EXTENSIONS
"""File extensions recognised as tabular datasets.

Alias of :data:`scripts.extraction.io.file_discovery.SUPPORTED_TABULAR_EXTENSIONS`.
"""

JUNK_FILENAMES: frozenset[str] = DEFAULT_JUNK_FILENAMES
"""Files/directories unconditionally ignored during discovery.

Alias of :data:`scripts.extraction.io.file_discovery.DEFAULT_JUNK_FILENAMES`.
"""

# NA-preservation options for tabular file reads.
#
# Clinical datasets use strings like "NA", "N/A", "None", or "NULL" as
# legitimate coded responses (e.g. "N/A" = Not Applicable for a field that
# does not apply to this visit).  pandas' default NA detection would silently
# coerce these to NaN → null, corrupting the data.  By disabling the default
# list and only treating the empty string as missing, we preserve every
# explicitly entered value while still mapping truly empty cells to null.
_TABULAR_NA_OPTIONS: dict[str, Any] = {
    "keep_default_na": False,
    "na_values": [""],
}
"""Read options passed to every pandas tabular file reader in this module.

Disables pandas' default NA coercion so that clinical strings such as
``"NA"``, ``"N/A"``, ``"None"``, and ``"NULL"`` are **preserved as-is**
instead of being silently converted to null.  Only the empty string (truly
missing cell) is mapped to NaN/null.
"""


# ============================================================================
# Forms-manifest gate (re-exported from the shared module)
# ============================================================================
#
# The forms-manifest gate (``ManifestMismatchError``, ``ManifestCheckResult``,
# ``check_forms_manifest``) moved to ``scripts.extraction.forms_manifest`` so
# the header-extraction skill and the orchestrator can import it without
# importing from the extraction pipeline (Note 20 Gap B). The three symbols are
# re-exported here (imported at module top and listed in ``__all__`` below) so
# existing imports such as
# ``from scripts.extraction.dataset_pipeline import check_forms_manifest``
# keep resolving to the SAME object — no caller change required.


# ============================================================================
# Typed result for extraction
# ============================================================================


class ExtractionResult(TypedDict):
    """Typed extraction result returned by :func:`extract_datasets`."""

    files_found: int
    files_created: int
    total_records: int
    errors: list[dict[str, str]]
    processing_time: float
    output_dir: str
    dropped_events: list[dict[str, Any]]


# ============================================================================
# Exported symbols
# ============================================================================

__all__ = [
    "ExtractionResult",
    "ManifestCheckResult",
    "ManifestMismatchError",
    "check_forms_manifest",
    "clean_record_for_json",
    "discover_dataset_files",
    "extract_datasets",
    "extract_single_dataset",
    "is_dataframe_empty",
    "process_datasets",
]


# ============================================================================
# Type-conversion helpers
# ============================================================================


def clean_record_for_json(record: dict[str, Any]) -> dict[str, Any]:
    """Convert pandas record to JSON-serializable types.

    Transforms a DataFrame row (as dict) into a JSON-safe format by
    converting pandas/numpy types to Python native types.

    Args:
        record: Dictionary from DataFrame row (typically from row.to_dict()).

    Returns:
        Dictionary with all values converted to JSON-serializable Python types:
            - pd.NA, np.nan → None
            - np.inf, -np.inf → None
            - np.integer → int
            - np.floating with no fractional part → int (e.g. 1001.0 → 1001)
            - np.floating with fractional part → float
            - float with no fractional part → int (e.g. 1001.0 → 1001)
            - float with fractional part → float
            - pd.Timestamp, datetime at midnight → date-only str (e.g. "2014-06-23")
            - pd.Timestamp, datetime with time → ISO 8601 str
            - date → ISO 8601 date str
            - str → stripped of leading/trailing whitespace
            - Other types preserved as-is

    Notes:
        Whole-number floats are converted to int because Excel frequently
        stores integer IDs (subject IDs, site codes, visit numbers) as
        floating-point internally, producing values like ``1001.0`` that
        should be emitted as ``1001``.

        String values are stripped to remove leading/trailing whitespace
        introduced by manual data entry, which would otherwise cause silent
        mismatches in downstream queries and joins.
    """
    cleaned: dict[str, Any] = {}
    for key, value in record.items():
        if pd.isna(value):
            cleaned[key] = None
        elif isinstance(value, bool | np.bool_):
            cleaned[key] = bool(value)  # type: ignore[arg-type]  # np.bool_ is bool-compatible
        elif isinstance(value, np.integer):
            cleaned[key] = value.item()
        elif isinstance(value, np.floating):
            num_value = value.item()
            if not np.isfinite(num_value):
                cleaned[key] = None
            elif num_value.is_integer():
                cleaned[key] = int(num_value)
            else:
                cleaned[key] = num_value
        elif isinstance(value, float):
            if not np.isfinite(value):
                cleaned[key] = None
            elif value.is_integer():
                cleaned[key] = int(value)
            else:
                cleaned[key] = value
        elif isinstance(value, int):
            cleaned[key] = value
        elif isinstance(value, pd.Timestamp | np.datetime64 | datetime | date):
            # Emit date-only ISO string when the time component is midnight,
            # e.g. "2014-06-23" instead of "2014-06-23 00:00:00".
            if isinstance(value, pd.Timestamp):
                ts: pd.Timestamp = value
                if ts.hour == 0 and ts.minute == 0 and ts.second == 0 and ts.microsecond == 0:
                    cleaned[key] = ts.strftime("%Y-%m-%d")
                else:
                    cleaned[key] = ts.isoformat()
            elif isinstance(value, datetime):
                if (
                    value.hour == 0
                    and value.minute == 0
                    and value.second == 0
                    and value.microsecond == 0
                ):
                    cleaned[key] = value.strftime("%Y-%m-%d")
                else:
                    cleaned[key] = value.isoformat()
            elif isinstance(value, date):
                cleaned[key] = value.isoformat()  # already date-only
            else:
                # np.datetime64 — convert via pd.Timestamp for uniform handling
                ts2 = pd.Timestamp(value)  # type: ignore[arg-type]  # numpy datetime64 generic
                if ts2.hour == 0 and ts2.minute == 0 and ts2.second == 0 and ts2.microsecond == 0:
                    cleaned[key] = ts2.strftime("%Y-%m-%d")
                else:
                    cleaned[key] = ts2.isoformat()
        elif isinstance(value, str):
            cleaned[key] = value.strip()
        else:
            cleaned[key] = value
    return cleaned


# ============================================================================
# Discovery helpers
# ============================================================================


def discover_dataset_files(datasets_dir: str | Path) -> list[Path]:
    """Return sorted list of supported dataset files in *datasets_dir*.

    Delegates to :func:`scripts.extraction.io.discover_files` with
    dataset-specific extensions and error labelling.

    Parameters
    ----------
    datasets_dir:
        Path to ``data/raw/{STUDY}/datasets/``.

    Returns
    -------
    list[Path]
        Sorted list of discovered file paths.

    Raises
    ------
    FileNotFoundError
        If *datasets_dir* does not exist.
    ValueError
        If no supported files are found.
    """
    files = discover_files(
        datasets_dir,
        extensions=SUPPORTED_EXTENSIONS,
        label="Dataset",
    )
    log.info("Discovered %d dataset files in %s", len(files), datasets_dir)
    return files


def _filter_allowed_forms(files: list[Path]) -> list[Path]:
    """Apply the form-level PHI approval allowlist, when supplied.

    ``extract_to_llm_source`` sets ``REPORTAL_ALLOWED_DATASET_FORMS`` after
    header-only/synthetic PHI review. This filter is intentionally by filename
    only, so held forms are never opened for row-value extraction.
    """
    raw = os.environ.get("REPORTAL_ALLOWED_DATASET_FORMS", "").strip()
    if not raw:
        return files
    allowed = {item.strip() for item in raw.split(",") if item.strip()}
    if not allowed:
        return files
    filtered = [f for f in files if f.name in allowed]
    skipped = sorted(f.name for f in files if f.name not in allowed)
    if skipped:
        _gate_log.info(
            "Form-level PHI review held %d dataset file(s); skipped before value extraction",
            len(skipped),
        )
    return filtered


# ============================================================================
# DataFrame helpers
# ============================================================================


def is_dataframe_empty(df: pd.DataFrame) -> bool:
    """Check if DataFrame is completely empty (no rows AND no columns).

    Differs from pandas' df.empty: returns True only if BOTH rows and
    columns are absent. DataFrames with columns but no rows are NOT empty.
    """
    return len(df.columns) == 0 and len(df) == 0


# ============================================================================
# JSONL conversion
# ============================================================================


# --- Atomic JSONL helpers ---


def _iter_clean_json_rows(df: pd.DataFrame) -> Generator[tuple[int, dict[str, Any]], None, None]:
    """Yield JSON-safe row dicts using itertuples() for stable, lower-overhead iteration."""
    columns = list(df.columns)
    for row_idx, row in enumerate(df.itertuples(index=False, name=None)):
        yield row_idx, clean_record_for_json(dict(zip(columns, row)))


def _atomic_write_jsonl_records(output_path: Path, records: list[dict[str, Any]]) -> None:
    """Write JSONL records atomically via the shared :mod:`scripts.extraction.io` helper.

    Thin wrapper that delegates to :func:`atomic_write_jsonl` using the
    safer ``NamedTemporaryFile`` strategy.
    """
    atomic_write_jsonl(
        output_path,
        records,
        prefix=config.TEMP_PREFIX_DATASET,
    )


# ============================================================================
# Tabular file reading (multi-format)
# ============================================================================


def _read_tabular_file(path: Path) -> list[tuple[str, pd.DataFrame]]:
    """Read a tabular file and return list of ``(sheet_name, DataFrame)``.

    For CSV files the sheet name is always ``"sheet1"``.
    For Excel files each sheet yields a separate entry.

    NA preservation: :data:`_TABULAR_NA_OPTIONS` is applied to every read so
    that clinical coded strings (``"NA"``, ``"N/A"``, ``"None"``, ``"NULL"``)
    are kept as-is and only the empty string is treated as missing.

    Header-row detection (Excel only):
        Sheets are read with ``header=None`` so that banner rows above the
        real column headers do not corrupt the output.
        :func:`~scripts.extraction.io.split_sheet_into_tables` separates the
        raw sheet into logical table segments (split on fully-empty rows and
        columns).  :func:`~scripts.extraction.io.promote_header` then detects
        the actual header row within each segment (first row with more than one
        non-null value) and promotes it to ``DataFrame.columns``.
        Footer rows composed entirely of null values are discarded during the
        split; non-null footer rows (e.g. ``TOTAL`` summary rows) are removed
        when they match the ``"total"`` footer marker.

        If the sheet contains multiple table segments (unusual for dataset
        files), each segment produces a separate ``(sheet_name, DataFrame)``
        entry tagged ``sheet_name__table_N``.

        If table-splitting fails (returns ``None``), the sheet falls back to
        the legacy ``header=0`` behaviour to avoid silent data loss.
    """
    ext = path.suffix.lower()

    if ext == ".csv":
        df = cast(pd.DataFrame, pd.read_csv(path, encoding=FILE_ENCODING, **_TABULAR_NA_OPTIONS))
        return [("sheet1", df)]

    if ext == ".xlsx":
        results: list[tuple[str, pd.DataFrame]] = []
        with pd.ExcelFile(path, engine="openpyxl") as xls:
            names: list[str] = [str(n) for n in xls.sheet_names]
            for sheet_name in names:
                # Read without a fixed header row so banner rows don't corrupt output.
                raw_df = cast(
                    pd.DataFrame,
                    xls.parse(sheet_name, header=None, **_TABULAR_NA_OPTIONS),
                )
                tables = split_sheet_into_tables(raw_df, split_horizontal=False)
                if tables is None:
                    # Structural error — fall back to legacy header=0 parse.
                    log.warning(
                        "Table-split failed for sheet '%s' in '%s'; falling back to header=0.",
                        sheet_name,
                        path.name,
                    )
                    fallback = cast(
                        pd.DataFrame,
                        xls.parse(sheet_name, **_TABULAR_NA_OPTIONS),
                    )
                    results.append((sheet_name, fallback))
                    continue

                if not tables:
                    # Genuinely empty sheet — emit an empty DataFrame so the
                    # caller's is_dataframe_empty check can skip it cleanly.
                    results.append((sheet_name, pd.DataFrame()))
                    continue

                for idx, table_seg in enumerate(tables):
                    promoted = promote_header(table_seg, footer_marker="total")
                    tag = sheet_name if len(tables) == 1 else f"{sheet_name}__table_{idx + 1}"
                    results.append((tag, promoted))

        return results

    raise ValueError(f"Unsupported file extension: {ext}")


# ============================================================================
# Provenance helpers
# ============================================================================


_PROVENANCE_ENGINE = f"pandas={pd.__version__}/openpyxl={_openpyxl.__version__}"
"""Extraction engine identifier carried in every record's provenance.

Coarse run-level identifier stamped on every record's provenance regardless
of source format. For ``.xlsx`` files the engine is openpyxl at the version
shown. For ``.csv`` files no Excel engine is involved; pandas reads them
directly.
"""


def hash_raw_file(path: Path, *, chunk_size: int = 1 << 16) -> str:
    """Return lowercase hex SHA-256 of *path* contents.

    Backwards-compatible wrapper over :func:`scripts.utils.integrity.hash_file`.
    Every extracted record's ``_provenance.raw_sha256`` is stamped with this
    value — closing the NIST SP 800-188 §5.2 integrity-chain requirement and
    enabling tamper-detection between raw → staged → published bundles.
    """
    return _hash_file(path, chunk_size=chunk_size)


def _build_provenance(
    *,
    source_file: str,
    sheet_name: str,
    row_index: int,
    study_name: str,
    extraction_ts: str,
    raw_sha256: str | None = None,
    pipeline_version: str = _PIPELINE_VERSION,
    extraction_engine: str = _PROVENANCE_ENGINE,
) -> dict[str, Any]:
    """Return provenance metadata for one extracted record.

    Beyond the legacy four fields, this records:

    * ``raw_sha256`` — SHA-256 of the source file contents. Computed once
      per file by :func:`hash_raw_file` and threaded in by the caller.
    * ``pipeline_version`` — the ``__version__`` of the pipeline at
      extraction time, so re-runs against a newer code base are
      distinguishable in the audit report.
    * ``extraction_engine`` — pandas + openpyxl version string, so a
      regulator can reproduce the exact extraction path.

    All three are regulatory-anchored: FDA 21 CFR Part 11 §11.10(e)
    (audit records of who/what/when), NIST SP 800-188 §5.2 (integrity
    chains), CDISC ODM origin traceability.
    """
    provenance: dict[str, Any] = {
        "source_file": source_file,
        "sheet_name": sheet_name,
        "row_index": row_index,
        "study_name": study_name,
        "pipeline_version": pipeline_version,
        "extraction_engine": extraction_engine,
    }
    if raw_sha256 is not None:
        provenance["raw_sha256"] = raw_sha256
    return provenance


# ============================================================================
# Single-file secure extraction
# ============================================================================


def extract_single_dataset(
    file_path: Path,
    output_dir: Path,
    study_name: str,
    extraction_ts: str,
) -> tuple[bool, int, str | None, list[dict[str, Any]]]:
    """Extract one dataset file to JSONL directly under *output_dir*.

    Provably-duplicate columns are removed (via ``clean_duplicate_columns``)
    before the JSONL is written. Output lands directly in *output_dir*
    (typically the staging directory for datasets — ``tmp/{STUDY}/datasets/``
    by default via :func:`extract_datasets`).

    Parameters
    ----------
    file_path:
        Absolute path to the source dataset file.
    output_dir:
        Directory that will receive the JSONL output.
    study_name:
        Active study identifier for provenance.
    extraction_ts:
        ISO-8601 timestamp string shared across the batch.

    Returns
    -------
    tuple[bool, int, str | None, list[dict[str, Any]]]
        ``(success, record_count, error_message, dropped_events)``.
        ``dropped_events`` is always a list (possibly empty); it aggregates
        the per-column drop events reported by
        :func:`~scripts.extraction.dedup.clean_duplicate_columns` across
        every sheet processed from this file.
    """
    start = time.time()

    try:
        sheets = _read_tabular_file(file_path)
    except Exception as exc:
        return False, 0, f"Failed to read {file_path.name}: {exc}", []

    # Hash once per file (NIST SP 800-188 §5.2 integrity chain); threaded
    # into every row's _provenance dict. Best-effort: hash failure does
    # not abort extraction — we log and continue with raw_sha256=None.
    try:
        raw_sha256: str | None = hash_raw_file(file_path)
    except OSError as exc:
        log.warning("Could not hash %s for provenance: %s", file_path.name, exc)
        raw_sha256 = None

    total_records = 0
    file_drop_events: list[dict[str, Any]] = []

    for sheet_name, df in sheets:
        if is_dataframe_empty(df):
            vlog.detail(f"Skipping empty sheet '{sheet_name}' in {file_path.name}")
            continue

        # Remove provably-duplicate columns before writing JSONL
        df, _drop_events = clean_duplicate_columns(df, source_file=file_path.name, sheet=sheet_name)
        if _drop_events:
            file_drop_events.extend(_drop_events)

        stem = file_path.stem
        if len(sheets) > 1:
            safe_sheet = sheet_name.replace("/", "_").replace("\\", "_").replace(" ", "_")
            out_name = f"{stem}__{safe_sheet}{JSONL_FILE_EXTENSION}"
        else:
            out_name = f"{stem}{JSONL_FILE_EXTENSION}"

        output_path = output_dir / out_name

        # --- write directly to the clean output directory ---
        count_orig = _write_provenance_jsonl(
            df=df,
            output_path=output_path,
            source_file=file_path.name,
            sheet_name=sheet_name,
            study_name=study_name,
            extraction_ts=extraction_ts,
            raw_sha256=raw_sha256,
        )
        total_records += count_orig

        tqdm.write(
            f"  ✓ {file_path.name} [{sheet_name}] → {count_orig} records ({len(df.columns)} cols)",
            file=sys.stderr,
        )

    elapsed = time.time() - start
    vlog.timing(f"Extraction of {file_path.name}", elapsed)
    return True, total_records, None, file_drop_events


def _write_provenance_jsonl(
    *,
    df: pd.DataFrame,
    output_path: Path,
    source_file: str,
    sheet_name: str,
    study_name: str,
    extraction_ts: str,
    raw_sha256: str | None = None,
) -> int:
    """Write DataFrame rows as provenance-annotated JSONL. Returns record count."""
    records: list[dict[str, Any]] = []

    if len(df) == 0 and len(df.columns) > 0:
        record: dict[str, Any] = dict.fromkeys(df.columns)
        record["source_file"] = source_file
        record["_provenance"] = _build_provenance(
            source_file=source_file,
            sheet_name=sheet_name,
            row_index=-1,
            study_name=study_name,
            extraction_ts=extraction_ts,
            raw_sha256=raw_sha256,
        )
        record["_metadata"] = {
            "type": "column_structure",
            "columns": list(df.columns),
            "note": "File contains column headers but no data rows",
        }
        _atomic_write_jsonl_records(output_path, [record])
        return 1

    for row_idx, rec in _iter_clean_json_rows(df):
        try:
            rec["source_file"] = source_file
            rec["_provenance"] = _build_provenance(
                source_file=source_file,
                sheet_name=sheet_name,
                row_index=int(row_idx),
                study_name=study_name,
                extraction_ts=extraction_ts,
                raw_sha256=raw_sha256,
            )
            records.append(rec)
        except (TypeError, ValueError) as exc:
            log.warning("Skipping row %d in %s[%s]: %s", row_idx, source_file, sheet_name, exc)

    _atomic_write_jsonl_records(output_path, records)
    return len(records)


# ============================================================================
# Main orchestrator — direct extraction to the trio bundle
# ============================================================================


def extract_datasets(
    *,
    datasets_dir: str | Path | None = None,
    output_dir: str | Path | None = None,
    study_name: str | None = None,
    runs_dir: str | Path | None = None,
    run_id: str | None = None,
) -> ExtractionResult:
    """Discover and extract all dataset files into AMBER staging.

    Output lands in *output_dir* when supplied, otherwise in
    ``config.STAGING_DATASETS_DIR`` (``tmp/{STUDY}/datasets/``). The bundle
    is later published from staging to ``llm_source/dataset_schema/files/`` by a separate
    publish step after the Step 1.6 PHI scrub and cleanup propagation.

    When *runs_dir* is provided (or when ``config.STUDY_OUTPUT_DIR`` / "runs"
    resolves cleanly), an ``extraction_timing.json`` sidecar is written to
    ``{runs_dir}/{run_id}/`` after extraction completes.  The sidecar captures
    the ``extraction_utc`` timestamp that was previously baked into every
    per-row ``_provenance`` dict.

    Returns
    -------
    dict
        Extraction summary with keys: ``files_found``, ``files_created``,
        ``total_records``, ``errors``, ``processing_time``, ``output_dir``,
        and ``dropped_events`` (flat list of per-column drop events emitted
        by :func:`~scripts.extraction.dedup.clean_duplicate_columns` across
        every processed sheet).
    """
    from scripts.utils.run_context import resolve_run_id, write_extraction_timing_sidecar

    overall_start = time.time()
    extraction_ts = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    _datasets_dir = Path(datasets_dir) if datasets_dir else Path(config.DATASETS_DIR)
    _study = study_name or config.STUDY_NAME

    # H1: Zone guards — lazy import to avoid circular dependency
    from scripts.security.secure_env import assert_not_raw

    # --- resolve output dir ---
    _output_dir = Path(output_dir) if output_dir is not None else Path(config.STAGING_DATASETS_DIR)

    # Caller-provided or default output must not point into the raw zone.
    assert_not_raw(_output_dir)

    # --- validate ---
    if not _datasets_dir.is_dir():
        msg = f"Dataset source directory missing: {_datasets_dir}"
        log.error(msg)
        return _error_result(msg, overall_start)

    # --- manifest gate (must run before any extraction work) ---
    # Refuses with ManifestMismatchError on missing required form or unknown
    # file.  Reject-listed files are auto-skipped (returned in rejected_files
    # and filtered out below).  When the manifest is absent a warning is
    # logged and extraction proceeds with no rejects (backward-compatible).
    _manifest = check_forms_manifest(_datasets_dir)
    _rejected_files = _manifest.rejected_files

    try:
        files = discover_dataset_files(_datasets_dir)
    except (FileNotFoundError, ValueError) as exc:
        return _error_result(str(exc), overall_start)

    # Filter out manifest-declared rejects so the extraction loop never reads
    # them.  The gate has already logged each skip at INFO level.
    if _rejected_files:
        files = [f for f in files if f.name not in _rejected_files]

    files = _filter_allowed_forms(files)

    # --- prepare output ---
    try:
        _output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _error_result(f"Cannot create output dir: {exc}", overall_start)

    # --- process ---
    total_records = 0
    files_created = 0
    errors: list[dict[str, str]] = []
    dropped_events: list[dict[str, Any]] = []

    for f in tqdm(files, desc="Extracting datasets", unit="file", file=sys.stderr):
        ok, count, err, events = extract_single_dataset(f, _output_dir, _study, extraction_ts)
        if events:
            dropped_events.extend(events)
        if ok:
            files_created += 1
            total_records += count
        elif err:
            errors.append({"file": f.name, "error": err})

    elapsed = time.time() - overall_start

    # --- summary ---
    log.info("Dataset extraction complete:")
    log.info("  %d total records extracted", total_records)
    log.info("  %d/%d files processed", files_created, len(files))
    log.info("  Output written to: %s", _output_dir)
    if dropped_events:
        log.info("  %d duplicate column(s) dropped during extraction", len(dropped_events))
    if errors:
        log.warning("  %d errors", len(errors))

    log.info(
        f"[DATASET EXTRACTION] study={_study} files={len(files)} created={files_created} "
        f"records={total_records} dropped={len(dropped_events)} errors={len(errors)} "
        f"elapsed={elapsed:.1f}s"
    )

    # --- write extraction timing sidecar ---
    # extraction_utc was removed from per-row _provenance to keep primary
    # JSONL content-only.  Emit it here so the timestamp is not lost.
    _active_run_id = run_id if run_id is not None else resolve_run_id()
    _runs_dir = Path(runs_dir) if runs_dir is not None else Path(config.STUDY_OUTPUT_DIR) / "runs"
    try:
        write_extraction_timing_sidecar(
            output_dir=_runs_dir.parent,
            run_id=_active_run_id,
            study=_study,
            extraction_utc=extraction_ts,
            pipeline_version=_PIPELINE_VERSION,
        )
    except OSError as exc:
        log.warning("Could not write extraction_timing sidecar: %s", exc)

    return ExtractionResult(
        files_found=len(files),
        files_created=files_created,
        total_records=total_records,
        errors=errors,
        processing_time=elapsed,
        output_dir=str(_output_dir),
        dropped_events=dropped_events,
    )


def _error_result(msg: str, start: float) -> ExtractionResult:
    """Return a failed-extraction result dict."""
    log.error("ERROR: %s", msg)
    return ExtractionResult(
        files_found=0,
        files_created=0,
        total_records=0,
        errors=[{"file": "", "error": msg}],
        processing_time=time.time() - start,
        output_dir="",
        dropped_events=[],
    )


# ============================================================================
# Unified lifecycle entry point
# ============================================================================


def process_datasets(*, debug: bool = False) -> dict[str, Any]:
    """Unified entry point: extract raw datasets into the staging workspace.

    This is the single function main.py should call for the dataset leg of
    extraction. Output lands in ``config.STAGING_DATASETS_DIR`` and is later
    published to ``llm_source/dataset_schema/files/`` by a separate publish step.

    Args:
        debug: No-op, retained for CLI compatibility with earlier versions
               of the pipeline that used it to preserve a temp workspace.

    Returns:
        dict with keys:
            extraction: :class:`ExtractionResult` dict from extraction step
                (includes ``dropped_events`` populated from
                :func:`~scripts.extraction.dedup.clean_duplicate_columns`).
            errors: aggregated list of extraction errors (only present when
                    extraction reported errors).
    """
    del debug  # retained for CLI compatibility; no longer meaningful
    result: dict[str, Any] = {}

    extraction = extract_datasets()
    result["extraction"] = extraction

    ext_errors = extraction.get("errors", [])
    if ext_errors:
        result["errors"] = ext_errors

    # L1: Unified lifecycle log
    ext_time: float = extraction.get("processing_time", 0.0)
    log.info(
        f"[DATASET LIFECYCLE] files_found={extraction['files_found']} "
        f"files_created={extraction['files_created']} "
        f"records={extraction['total_records']} "
        f"errors={len(ext_errors)} extract_time={ext_time:.1f}s"
    )

    return result


# ============================================================================
# CLI entry point
# ============================================================================

if __name__ == "__main__":
    # ── DEBUG-ONLY entry point ──
    # When run directly (python -m scripts.extraction.dataset_pipeline),
    # output persists in tmp/dataset_extractions/ for manual inspection.
    #
    # PRODUCTION path: main.py → process_datasets() → extract_datasets() with no
    # output_dir → writes directly to output/{STUDY}/llm_source/dataset_schema/files/.
    import argparse

    _parser = argparse.ArgumentParser(
        description="DEBUG: extract datasets to tmp/dataset_extractions/",
    )
    _parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose/debug logging (default: simple INFO)",
    )
    _args = _parser.parse_args()

    try:
        log.setup_logging(
            module_name=MODULE_LOGGER_NAME,
            log_level="DEBUG" if _args.verbose else "INFO",
            verbose=_args.verbose,
        )

        _debug_output = Path(config.TMP_DIR) / "dataset_extractions"
        log.info("=" * 70)
        log.info("  DEBUG MODE — dataset extraction")
        log.info("  Study:   %s", config.STUDY_NAME)
        log.info("  Source:  %s", config.DATASETS_DIR)
        log.info("  Output:  %s", _debug_output)
        if _args.verbose:
            log.info("  Log:     VERBOSE (DEBUG level)")
        else:
            log.info("  Log:     SIMPLE  (INFO level, use -v for verbose)")
        log.info("=" * 70)

        result = extract_datasets(output_dir=str(_debug_output))

        # ── Summary ──
        log.info("─" * 70)
        log.info("  EXTRACTION SUMMARY")
        log.info("─" * 70)
        log.info("  Files found:   %s", result["files_found"])
        log.info("  Files created: %s", result["files_created"])
        log.info("  Total records: %s", result["total_records"])
        log.info("  Errors:        %s", len(result["errors"]))
        log.info("  Time:          %.1fs", result["processing_time"])
        log.info("  Output dir:    %s", result["output_dir"])

        if result["errors"]:
            log.warning("  ERRORS:")
            for err in result["errors"]:
                log.warning("    • %s", err)

        # ── File listing ──
        if _debug_output.exists():
            files = sorted(_debug_output.glob(f"*{JSONL_FILE_EXTENSION}"))
            total_lines = 0
            log.info("  %s/ (%d files):", _debug_output.name, len(files))
            log.info("  %-45s %8s  %10s", "File", "Records", "Size")
            log.info("  %s %s  %s", "─" * 45, "─" * 8, "─" * 10)
            for f in files:
                with open(f, encoding=FILE_ENCODING) as fh:
                    lines = sum(1 for _ in fh)
                total_lines += lines
                size_kb = f.stat().st_size / 1024
                log.info("  %-45s %8s  %8.1f KB", f.name, f"{lines:,}", size_kb)
            log.info("  %s %s  %s", "─" * 45, "─" * 8, "─" * 10)
            log.info("  %-45s %8s", "TOTAL", f"{total_lines:,}")

        log.info("─" * 70)

        if result["errors"]:
            sys.exit(1)
        sys.exit(0)
    except KeyboardInterrupt:
        log.warning("Extraction cancelled by user")
        sys.exit(130)
    except Exception as e:
        log.error("Fatal: %s", format_for_log(wrap(e, stage="extract", operation="run")))
        sys.exit(1)
