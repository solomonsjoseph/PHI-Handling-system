"""Shim: minimal I/O primitives extracted from phi_engine.utils._extraction_io for phi_engine use.
These are general-purpose atomic-write utilities; they contain no study-specific logic.
"""
from __future__ import annotations

from phi_engine.utils._extraction_io.clinical_dates import (
    ParsedDate, is_dmy_variable, parse_date, value_looks_like_date,
)
from phi_engine.utils._extraction_io.file_discovery import (
    DEFAULT_JUNK_FILENAMES, SUPPORTED_TABULAR_EXTENSIONS, discover_files,
)
from phi_engine.utils._extraction_io.file_io import (
    ATOMIC_WRITE_SUFFIX, FILE_ENCODING, JSONL_EXT,
    atomic_write_dataframe_jsonl, atomic_write_json, atomic_write_jsonl,
)
from phi_engine.utils._extraction_io.jsonl_reader import (
    JSONLParseError, load_json_object_line,
)
from phi_engine.utils._extraction_io.sheet_split import (
    promote_header, split_sheet_into_tables,
)

__all__ = [
    "ATOMIC_WRITE_SUFFIX", "DEFAULT_JUNK_FILENAMES", "FILE_ENCODING",
    "JSONL_EXT", "SUPPORTED_TABULAR_EXTENSIONS", "JSONLParseError",
    "ParsedDate", "atomic_write_dataframe_jsonl", "atomic_write_json",
    "atomic_write_jsonl", "discover_files", "is_dmy_variable",
    "load_json_object_line", "parse_date", "promote_header",
    "split_sheet_into_tables", "value_looks_like_date",
]
