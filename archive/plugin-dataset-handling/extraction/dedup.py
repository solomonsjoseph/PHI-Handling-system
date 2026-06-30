"""Unified deduplication helpers for RePORT AI Portal study preparation.

This module provides a single place for duplicate-detection and duplicate-removal
logic used by the active dataset/dictionary publish path:

- **Dataset / Dictionary (JSONL):** duplicate *columns* inside tabular data
  (e.g. ``SUBJID`` and ``SUBJID2`` that contain identical values).

Most functions in this module are **stateless-filesystem helpers**: they accept
data, return cleaned data (or a report), and never touch the filesystem.  File
I/O remains in the caller so that atomic-write semantics are preserved.

Usage:
    >>> from scripts.extraction.dedup import (
    ...     clean_duplicate_columns,          # for DataFrames (dataset / dict)
    ... )
"""

# SHARED UTILITY (Note 20 Gap C / Note 19): stays in scripts/, imported
# read-only by plugin skills; never moved into a skill, never imports from
# plugins/.

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

from tqdm import tqdm

import config
from scripts.utils import logging_system as log

vlog = log.get_verbose_logger()

__all__ = [
    "clean_duplicate_columns",
]


# ============================================================================
# Dataset / Dictionary — duplicate COLUMN removal (JSONL / DataFrame)
# ============================================================================


def _dtypes_match(base_series: pd.Series, dup_series: pd.Series) -> bool:  # type: ignore[name-defined]
    """Return True iff both series share the exact same pandas dtype.

    Strict equality (``base.dtype == dup.dtype``) is intentional: ``int64``
    and ``Int64`` (nullable integer) are considered different types because
    they have different NA semantics.  This mirrors the "strict" dtype
    comparison used elsewhere in the extraction pipeline.
    """
    return base_series.dtype == dup_series.dtype


def _positionally_adjacent(columns: list[str], base_col: str, dup_col: str) -> bool:
    """Return True iff *dup_col* is immediately next to *base_col* in *columns*.

    "Adjacent" means the absolute difference of their indexes is exactly 1
    (either base→dup or dup→base direction).  This guards against treating a
    legitimately independent column as an Excel-autocomplete artifact just
    because an earlier column happens to share the same prefix.
    """
    try:
        base_idx = columns.index(base_col)
        dup_idx = columns.index(dup_col)
    except ValueError:
        return False
    return abs(dup_idx - base_idx) == 1


def clean_duplicate_columns(
    df: pd.DataFrame,
    *,
    source_file: str,
    sheet: str | None,
) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
    """Remove duplicate columns ending with numeric suffixes from a DataFrame.

    Implements intelligent duplicate detection for Excel-autocomplete artifacts.
    A column is removed only when **all four** conditions hold:

    1. Its name matches the pattern ``base_name + optional '_' + digits``
       (e.g. ``SUBJID2``, ``NAME_3``).
    2. Its pandas dtype is identical to the base column's dtype (strict
       equality — ``int64`` and ``Int64`` are treated as distinct).
    3. It is positionally adjacent to the base column (consecutive in the
       source column order, i.e. ``abs(index(dup) - index(base)) == 1``).
    4. Its values are 100% identical to the base column (element-wise, with
       NaN-equality).

    Entirely-null columns matching rule 1 are still removed unconditionally
    (the null-path bypasses the dtype and adjacency checks because a null
    column carries no clinical information regardless of position).

    Args:
        df: pandas DataFrame to clean.
        source_file: Name of the source file (e.g. ``"01_Demographics.jsonl"``).
            Recorded verbatim on each drop event.
        sheet: Sheet name for multi-sheet inputs, or ``None`` for single-sheet
            / non-Excel sources.  Recorded verbatim on each drop event.

    Returns:
        Tuple of ``(cleaned_df, drop_events)`` where:

        - ``cleaned_df`` is a copy of *df* with duplicate columns removed.
        - ``drop_events`` is a list of dicts — one per removed column — with the
          keys ``scope`` (always ``"dataset-column"``), ``name`` (the dropped
          column), ``file`` (``source_file``), ``sheet`` (``sheet``),
          ``reason`` (``"100% identical to '<base>'"`` or ``"entirely null"``),
          and ``kept`` (the base column name, or ``None`` for pure-null drops).
    """
    pattern = config.DUPLICATE_COLUMN_PATTERN
    col_list: list[str] = list(df.columns)

    columns_to_keep: list[str] = []
    columns_to_remove: list[str] = []
    removal_reasons: dict[str, str] = {}
    drop_events: list[dict[str, Any]] = []

    for col in df.columns:
        match = re.match(pattern, str(col))
        if match:
            base_name = match.group(1)
            if base_name in df.columns:
                try:
                    if df[col].isna().all():
                        columns_to_remove.append(col)
                        reason = "entirely null"
                        removal_reasons[col] = reason
                        drop_events.append(
                            {
                                "scope": "dataset-column",
                                "name": col,
                                "file": source_file,
                                "sheet": sheet,
                                "reason": reason,
                                "kept": None,
                            }
                        )
                        log.debug("Marking '%s' for removal (entirely null)", col)
                        vlog.detail(f"Marking '{col}' for removal (entirely null)")
                    else:
                        base_col = df[base_name]
                        dup_col = df[col]
                        both_na = base_col.isna() & dup_col.isna()
                        both_equal = base_col == dup_col
                        all_match = (both_na | both_equal).all()

                        if all_match:
                            # Extra guards: dtype must match AND columns must be adjacent.
                            if not _dtypes_match(base_col, dup_col):
                                columns_to_keep.append(col)
                                log.debug(
                                    "Keeping '%s' (candidate kept: dtype mismatch with '%s': "
                                    "%s vs %s)",
                                    col,
                                    base_name,
                                    dup_col.dtype,
                                    base_col.dtype,
                                )
                                vlog.detail(
                                    f"Keeping '{col}' (candidate kept: dtype mismatch with "
                                    f"'{base_name}': {dup_col.dtype} vs {base_col.dtype})"
                                )
                            elif not _positionally_adjacent(col_list, base_name, col):
                                columns_to_keep.append(col)
                                log.debug(
                                    "Keeping '%s' (candidate kept: not positionally adjacent "
                                    "to '%s')",
                                    col,
                                    base_name,
                                )
                                vlog.detail(
                                    f"Keeping '{col}' (candidate kept: not positionally "
                                    f"adjacent to '{base_name}')"
                                )
                            else:
                                columns_to_remove.append(col)
                                reason = f"100% identical to '{base_name}'"
                                removal_reasons[col] = reason
                                drop_events.append(
                                    {
                                        "scope": "dataset-column",
                                        "name": col,
                                        "file": source_file,
                                        "sheet": sheet,
                                        "reason": reason,
                                        "kept": base_name,
                                    }
                                )
                                log.debug(
                                    "Marking '%s' for removal (100%% identical to '%s')",
                                    col,
                                    base_name,
                                )
                                vlog.detail(
                                    f"Marking '{col}' for removal (100% identical to '{base_name}')"
                                )
                        else:
                            columns_to_keep.append(col)
                            match_count = (both_na | both_equal).sum()
                            match_pct = (match_count / len(df) * 100) if len(df) > 0 else 0
                            log.debug(
                                "Keeping '%s' (%.1f%% similar to '%s', not 100%%)",
                                col,
                                match_pct,
                                base_name,
                            )
                            vlog.detail(
                                f"Keeping '{col}' ({match_pct:.1f}% similar to '{base_name}')"
                            )
                except Exception as e:
                    columns_to_keep.append(col)
                    log.warning(
                        "Could not compare '%s' with '%s': %s. Keeping column for safety.",
                        col,
                        base_name,
                        e,
                    )
                    vlog.detail(f"Keeping '{col}' (comparison failed: {e})")
            else:
                columns_to_keep.append(col)
                log.debug("Keeping '%s' (base column '%s' not found)", col, base_name)
        else:
            columns_to_keep.append(col)

    if columns_to_remove:
        removal_summary = [f"{col} ({removal_reasons[col]})" for col in columns_to_remove]
        tqdm.write(
            f"    → Removing {len(columns_to_remove)} duplicate column(s): "
            f"{', '.join(columns_to_remove)}"
        )
        log.info(
            "Removed %d duplicate columns: %s", len(columns_to_remove), ", ".join(removal_summary)
        )
        vlog.detail(f"Duplicate columns removed: {', '.join(removal_summary)}")
    else:
        log.debug("No duplicate columns found to remove")
        vlog.detail("No duplicate columns found")

    return df[columns_to_keep].copy(), drop_events
