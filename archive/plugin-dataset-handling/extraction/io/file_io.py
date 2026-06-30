"""Canonical atomic file-write helpers for the RePORT AI Portal pipeline.

Every module that persists JSONL, JSON, or plain-text artifacts should use
these helpers instead of rolling its own write-to-temp-then-rename dance.
The strategy is:

1. Write to a ``NamedTemporaryFile`` in the **same directory** as the final
   output (guaranteeing same-filesystem for the rename).
2. On success, ``Path.replace()`` atomically swaps the temp file into place.
3. On failure, the temp file is cleaned up in a ``finally`` block.

This eliminates the risk of half-written files after crashes and avoids the
race condition inherent in using a predictable ``.tmp`` suffix.

Exported helpers
~~~~~~~~~~~~~~~~
- ``atomic_write_jsonl``  — write ``list[dict]`` as JSONL lines.
- ``atomic_write_json``   — write a single ``dict`` as pretty-printed JSON.
- ``atomic_write_dataframe_jsonl`` — write a ``pandas.DataFrame`` via
  ``DataFrame.to_json(orient="records", lines=True)``.
"""

# SHARED UTILITY (Note 20 Gap C / Note 19): stays in scripts/, imported
# read-only by plugin skills; never moved into a skill, never imports from
# plugins/.

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pandas as pd

__all__ = [
    "ATOMIC_WRITE_SUFFIX",
    "FILE_ENCODING",
    "JSONL_EXT",
    "NAMED_TEMP_PREFIX",
    "atomic_write_dataframe_jsonl",
    "atomic_write_json",
    "atomic_write_jsonl",
]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

ATOMIC_WRITE_SUFFIX: str = ".tmp"
"""Temporary suffix used during atomic writes before final replace."""

FILE_ENCODING: str = "utf-8"
"""Default text encoding for all file operations."""

JSONL_EXT: str = ".jsonl"
"""Canonical JSONL file extension."""

NAMED_TEMP_PREFIX: str = "report_ai_portal_"
"""Default prefix for NamedTemporaryFile instances."""


# ---------------------------------------------------------------------------
# Atomic write: JSONL records
# ---------------------------------------------------------------------------


def atomic_write_jsonl(
    output_path: Path | str,
    records: Iterable[dict[str, Any]],
    *,
    ensure_ascii: bool = False,
    sort_keys: bool = False,
    default: Any = None,
    prefix: str = NAMED_TEMP_PREFIX,
) -> None:
    """Write an iterable of dicts as JSONL atomically.

    Args:
        output_path: Final destination path.
        records: Iterable of JSON-serializable dicts, one per line.
        ensure_ascii: Passed to ``json.dumps``.
        sort_keys: Passed to ``json.dumps``.
        default: Fallback serializer passed to ``json.dumps``.
        prefix: Prefix for the temporary file name.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=FILE_ENCODING,
            dir=out.parent,
            prefix=prefix,
            suffix=ATOMIC_WRITE_SUFFIX,
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            for record in records:
                fh.write(
                    json.dumps(
                        record,
                        ensure_ascii=ensure_ascii,
                        sort_keys=sort_keys,
                        default=default,
                    )
                    + "\n"
                )
            fh.flush()
            os.fsync(fh.fileno())  # durability: flush kernel buffers before rename
        tmp_path.replace(out)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Atomic write: single JSON document
# ---------------------------------------------------------------------------


def atomic_write_json(
    output_path: Path | str,
    payload: Any,
    *,
    ensure_ascii: bool = False,
    indent: int = 2,
    prefix: str = NAMED_TEMP_PREFIX,
) -> None:
    """Write a single JSON-serializable value atomically.

    Args:
        output_path: Final destination path.
        payload: JSON-serializable value (dict, list, or scalar).
        ensure_ascii: Passed to ``json.dump``.
        indent: Indentation level for pretty-printing.
        prefix: Prefix for the temporary file name.
    """
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=FILE_ENCODING,
            dir=out.parent,
            prefix=prefix,
            suffix=ATOMIC_WRITE_SUFFIX,
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            json.dump(payload, fh, ensure_ascii=ensure_ascii, indent=indent)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())  # durability: flush kernel buffers before rename
        tmp_path.replace(out)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Atomic write: pandas DataFrame → JSONL
# ---------------------------------------------------------------------------


def atomic_write_dataframe_jsonl(
    output_path: Path | str,
    df: pd.DataFrame,
    *,
    prefix: str = NAMED_TEMP_PREFIX,
) -> None:
    """Write a ``pandas.DataFrame`` to JSONL atomically.

    Uses ``DataFrame.to_json(orient="records", lines=True)`` for serialization.
    Import of ``pandas`` is deferred so modules that don't use DataFrames
    avoid the import cost.

    Args:
        output_path: Final destination path.
        df: DataFrame to serialize.
        prefix: Prefix for the temporary file name.
    """

    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=FILE_ENCODING,
            dir=out.parent,
            prefix=prefix,
            suffix=ATOMIC_WRITE_SUFFIX,
            delete=False,
        ) as fh:
            tmp_path = Path(fh.name)
            df.to_json(fh, orient="records", lines=True, force_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())  # durability: flush kernel buffers before rename
        tmp_path.replace(out)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
