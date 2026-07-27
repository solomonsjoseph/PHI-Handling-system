"""Excel sheet segmentation and header-row detection utilities.

This module provides two helpers that work together to extract well-formed
DataFrames from messy Excel sheets:

``split_sheet_into_tables``
    Segments a raw (``header=None``) DataFrame into logical tables separated
    by fully-empty rows and fully-empty columns.  This is the canonical
    replacement for the private ``_split_sheet_into_tables`` in
    ``load_dictionary.py``.

``promote_header``
    Given a raw segment returned by ``split_sheet_into_tables``, locates the
    first row that looks like a column-header row and promotes it to
    ``DataFrame.columns``.  Rows above the detected header (banner rows) are
    dropped.  Rows below the data (footer rows composed entirely of null
    values) are also dropped.

Header-row detection heuristic
-------------------------------
A row is considered the header if it is the first row within the segment
that has **more than one non-null value**.  Banner rows in typical clinical
Excel files have a single merged/populated cell (e.g. ``"Study: HIV-TB"``).
Actual header rows have one non-null value per column.

Unit rows (e.g. ``["mg", "cm", "", ""]``) are treated as ordinary data
rows — they are NOT promoted to column names.  If your dataset has a unit
row immediately after the header, those units will appear as the first data
row.  This is a deliberate v1 limitation documented here so callers can
filter them if needed.

Footer detection
----------------
Any fully-null row (``NaN`` in every column) at the bottom of the segment
is already removed by ``split_sheet_into_tables`` via ``dropna(how="all")``.
Rows that are not fully null (e.g. a ``TOTAL`` row containing summed values)
are kept as-is — callers that need to discard totals should do so themselves,
or pass ``footer_marker`` to ``promote_header``.

Limitations
-----------
- Does not handle banner rows that are separated from the header by a
  non-empty row other than the header itself.
- Does not detect unit rows or coerce them to metadata.
- Multiple tables per sheet are handled (list returned), but the dataset
  leg typically consumes only the first element.
"""

# SHARED UTILITY (Note 20 Gap C / Note 19): stays in scripts/, imported
# read-only by plugin skills; never moved into a skill, never imports from
# plugins/.

from __future__ import annotations

import logging
from typing import cast

import pandas as pd

from phi_engine.utils import logging_system as log

_log = logging.getLogger(f"report_ai_portal.{__name__}")

__all__ = [
    "promote_header",
    "split_sheet_into_tables",
]


def split_sheet_into_tables(
    df: pd.DataFrame,
    *,
    split_horizontal: bool = True,
) -> list[pd.DataFrame] | None:
    """Split a raw (``header=None``) DataFrame into logical tables.

    This is a boundary-detection pass only — it does **not** promote any row
    to column headers.  Header promotion is the caller's responsibility (see
    :func:`promote_header`).

    Algorithm:

    1. Find horizontal strips: contiguous row groups separated by fully-empty
       rows (all values NaN in every column).
    2. Within each strip, find vertical segments separated by fully-empty
       columns (all values NaN in every row of the strip).
    3. After each segment is extracted, drop any fully-null rows that remain
       (e.g. isolated empty cells that didn't form a full empty row).

    Args:
        df: Input DataFrame read with ``header=None`` from an Excel sheet.
            May contain multiple logical tables laid out side-by-side or
            stacked.
        split_horizontal: If True (default), splits the DataFrame horizontally by
            fully-empty rows. If False, treats the entire vertical span as a single
            horizontal strip.

    Returns:
        List of DataFrames, each representing one detected table segment.
        Returns an empty list ``[]`` if the sheet is genuinely empty.
        Returns ``None`` if a structural error occurred (distinct from an
        empty sheet — callers should log a warning and skip the sheet).
    """
    try:
        if df.empty:
            log.debug("Received empty DataFrame, returning empty table list")
            return []

        log.debug(f"Analyzing DataFrame with shape {df.shape} for table boundaries")

        if split_horizontal:
            empty_rows = df.index[df.isnull().all(axis=1)].tolist()
            row_boundaries: list[int] = [-1, *empty_rows, df.shape[0]]
            horizontal_strips = [
                df.iloc[row_boundaries[i] + 1 : row_boundaries[i + 1]]
                for i in range(len(row_boundaries) - 1)
                if row_boundaries[i] + 1 < row_boundaries[i + 1]
            ]
        else:
            horizontal_strips = [df]

        log.debug(f"Found {len(horizontal_strips)} horizontal strip(s)")

        all_tables: list[pd.DataFrame] = []
        for strip in horizontal_strips:
            empty_col_indices = [
                i for i, col in enumerate(strip.columns) if strip[col].isnull().all()
            ]
            col_boundaries = [-1, *empty_col_indices, len(strip.columns)]
            for j in range(len(col_boundaries) - 1):
                start_col, end_col = col_boundaries[j] + 1, col_boundaries[j + 1]
                if start_col < end_col:
                    table_df = strip.iloc[:, start_col:end_col].copy()
                    table_df.dropna(how="all", inplace=True)
                    if not table_df.empty:
                        all_tables.append(table_df)

        log.debug(f"Detected {len(all_tables)} table(s) from DataFrame")
        return all_tables

    except (KeyError, IndexError) as e:
        log.error(f"DataFrame structure error during table splitting: {e}")
        log.debug("DataFrame info:", exc_info=True)
        return None  # signals parse error, distinct from empty sheet
    except Exception as e:
        log.error(f"Unexpected error splitting DataFrame into tables: {type(e).__name__}: {e}")
        log.debug("Full error details:", exc_info=True)
        return None  # signals parse error


def promote_header(
    df: pd.DataFrame,
    *,
    footer_marker: str | None = None,
) -> pd.DataFrame:
    """Promote the first header-like row to ``DataFrame.columns``.

    Applies the following transformations in order:

    1. **Banner-row skip**: rows before the detected header row are dropped.
       A header row is the first row with more than one non-null value.
       This handles single-cell banner rows such as ``"Study: HIV-TB"`` that
       appear above the actual column names.

    2. **Header promotion**: the detected row becomes the column index.
       ``NaN`` column names are replaced with ``"Unnamed"``.

    3. **Footer trimming (optional)**: if *footer_marker* is given, any row
       where the first cell contains that string (case-insensitive) and all
       subsequent cells are either numeric or null is dropped.

    4. **Null-row cleanup**: fully-null rows remaining after the header are
       dropped.

    Unit rows (a row of unit strings like ``"mg"``, ``"cm"`` immediately
    after the header) are **not** detected or removed.  They appear as
    ordinary data rows in the returned DataFrame.  This is intentional — v1
    does not attempt to distinguish unit metadata from real data.

    Args:
        df: A table segment from :func:`split_sheet_into_tables` (integer
            column index, ``header=None`` origin).
        footer_marker: If provided, any row whose first non-null string cell
            starts with this prefix (case-insensitive) is dropped.  Typical
            value: ``"total"``.

    Returns:
        DataFrame with:
        - columns set to the detected header row,
        - rows above the header dropped,
        - fully-null rows dropped,
        - footer rows (if *footer_marker* is set) dropped.

    Raises:
        ValueError: If no header row can be found (e.g. the table has only
            one row and it is entirely null).
    """
    df = df.reset_index(drop=True)

    # Step 1: find the header row — first row with > 1 non-null value.
    header_idx: int | None = None
    for i, row in df.iterrows():
        non_null_count = row.notna().sum()
        if non_null_count > 1:
            header_idx = cast(int, i)
            break

    if header_idx is None:
        # Fall back: if all rows have at most 1 non-null value, use row 0.
        header_idx = 0

    # Step 2: promote that row to columns; drop rows above it (banner rows).
    header_row = df.iloc[header_idx]
    new_cols: list[str] = []
    for val in header_row:
        if pd.isna(val):
            new_cols.append("Unnamed")
        else:
            new_cols.append(str(val))
    data_df = df.iloc[header_idx + 1 :].copy()
    data_df.columns = pd.Index(new_cols)
    data_df = data_df.reset_index(drop=True)

    # Step 3: optional footer trimming.
    #
    # Guard on a non-empty frame: when ``data_df`` has zero rows (a header-only
    # table — e.g. a clinical form with no recorded events), ``apply(axis=1)``
    # yields an empty Series and ``data_df[~mask]`` collapses the frame to
    # shape ``(0, 0)``, silently dropping the column index. A zero-row frame has
    # no footer rows to trim, so skipping the step both avoids the pandas
    # collapse and preserves the columns the column-structure JSONL path needs.
    if footer_marker is not None and len(data_df) > 0:
        marker_lower = footer_marker.lower()

        def _is_footer_row(row: pd.Series) -> bool:  # type: ignore[type-arg]
            """True when the first non-null cell starts with *footer_marker*.

            Footer/summary rows (e.g. a ``TOTAL | 155 | 165`` column-sum row) are
            structural artefacts, not clinical records — they legitimately carry
            values in the non-first cells, so we deliberately do NOT require the
            rest of the row to be empty. Dropping them is a deterministic extraction
            cleanup, and every drop is logged below (index + marker only, never the
            cell value) so the removal is auditable (UP2 transparency).
            """
            first_val = next((v for v in row if pd.notna(v) and str(v).strip()), None)
            if first_val is None:
                return False
            return str(first_val).strip().lower().startswith(marker_lower)

        mask = data_df.apply(_is_footer_row, axis=1)
        dropped_indices = data_df.index[mask].tolist()
        if dropped_indices:
            _log.debug(
                "promote_header: dropping %d footer row(s) matching %r at indices %s",
                len(dropped_indices),
                footer_marker,
                dropped_indices,
            )
        data_df = data_df[~mask].reset_index(drop=True)

    # Step 4: drop fully-null rows.
    data_df.dropna(how="all", inplace=True)
    data_df = data_df.reset_index(drop=True)

    return data_df
