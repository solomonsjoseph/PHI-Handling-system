from __future__ import annotations

import contextlib
import csv
import hashlib
import json
import lzma
import struct
import zipfile
import zlib
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Iterator, Mapping

import pandas as pd

from phi_engine.pipeline.dependencies import (
    DependencyKind,
    ParsedSupportArtifact,
    SupportFailureCode,
    SupportParseStatus,
)
from phi_engine.utils._extraction_io.sheet_split import promote_header, split_sheet_into_tables

DEFAULT_LIMITS: dict[str, int] = {
    "max_source_bytes": 128 * 1024 * 1024,
    "max_expanded_workbook_bytes": 512 * 1024 * 1024,
    "max_decompression_ratio": 100,
    "max_zip_member_bytes": 8 * 1024 * 1024,
    "max_zip_members": 2048,
    "max_zip_directory_bytes": 4 * 1024 * 1024,
    "max_sheets": 64,
    "max_tables": 256,
    "max_rows": 1_000_000,
    "max_columns": 4096,
    "max_cell_codepoints": 32768,
    "max_json_depth": 32,
}

_SUPPORTED_SUFFIXES = {".pdf", ".xlsx", ".xls", ".csv", ".json", ".jsonl"}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            encoded = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str)
            digest.update(encoded.encode("utf-8"))
            digest.update(b"\n")
            fh.write(encoded + "\n")
    path.chmod(0o600)
    return digest.hexdigest()


def _fail(artifact_id: str, source_sha256: str, kind: DependencyKind, fmt: str, code: SupportFailureCode) -> ParsedSupportArtifact:
    return ParsedSupportArtifact(
        artifact_id=artifact_id,
        source_sha256=source_sha256,
        kind=kind,
        format=fmt,
        parse_status=SupportParseStatus.FAILED,
        normalized_rows_path=None,
        normalized_rows_sha256=None,
        failure_code=code,
    )


def _depth(value: Any, current: int = 0) -> int:
    if isinstance(value, dict):
        return max([current] + [_depth(v, current + 1) for v in value.values()])
    if isinstance(value, list):
        return max([current] + [_depth(v, current + 1) for v in value])
    return current


def _cell(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) > limit:
        raise ValueError("cell-limit")
    return text


def _row(artifact_id: str, source_sha256: str, sheet_index: int, table_index: int, row_index: int, values: list[Any], cell_limit: int) -> dict[str, Any]:
    return {
        "support_artifact_id": artifact_id,
        "source_sha256": source_sha256,
        "sheet_index": sheet_index,
        "table_index": table_index,
        "row_index": row_index,
        "cells": [
            {"column_index": idx, "value": _cell(value, cell_limit)}
            for idx, value in enumerate(values)
        ],
    }


def _check_count(count: int, limit: int, exc: Exception) -> None:
    if count > limit:
        raise exc


def _zip_expanded_size(path: Path) -> tuple[int, int]:
    expanded = 0
    compressed = path.stat().st_size
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            expanded += int(info.file_size)
    return expanded, compressed


def _expanded_size_and_ratio(path: Path, suffix: str) -> tuple[int, float]:
    size = path.stat().st_size
    if suffix == ".xlsx":
        expanded, compressed = _zip_expanded_size(path)
        ratio = (expanded / compressed) if compressed else float("inf")
        return expanded, ratio
    return size, 1.0


def _enforce_expanded_limits(path: Path, limits: dict[str, int], suffix: str) -> None:
    expanded, ratio = _expanded_size_and_ratio(path, suffix)
    if expanded > limits["max_expanded_workbook_bytes"]:
        raise ValueError("expanded-limit")
    if ratio > limits["max_decompression_ratio"]:
        raise ValueError("ratio-limit")


def _parse_csv(path: Path, artifact_id: str, source_sha256: str, limits: dict[str, int], suffix: str) -> list[dict[str, Any]]:
    _enforce_expanded_limits(path, limits, suffix)
    rows: list[dict[str, Any]] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        for row_index, row_values in enumerate(reader):
            if row_index == 0:
                continue
            _check_count(row_index, limits["max_rows"], ValueError("row-limit"))
            _check_count(len(row_values), limits["max_columns"], ValueError("column-limit"))
            rows.append(_row(artifact_id, source_sha256, 0, 0, row_index - 1, row_values, limits["max_cell_codepoints"]))
    return rows


def _parse_jsonl(path: Path, artifact_id: str, source_sha256: str, limits: dict[str, int], suffix: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    _enforce_expanded_limits(path, limits, suffix)
    with path.open(encoding="utf-8") as fh:
        for row_index, line in enumerate(fh):
            if not line.strip():
                continue
            value = json.loads(line)
            if _depth(value) > limits["max_json_depth"]:
                raise ValueError("json-depth")
            if isinstance(value, dict):
                cells = [f"{k}={v}" for k, v in value.items()]
            elif isinstance(value, list):
                cells = value
            else:
                cells = [value]
            _check_count(row_index + 1, limits["max_rows"], ValueError("row-limit"))
            _check_count(len(cells), limits["max_columns"], ValueError("column-limit"))
            rows.append(_row(artifact_id, source_sha256, 0, 0, row_index, cells, limits["max_cell_codepoints"]))
    return rows


def _parse_json(path: Path, artifact_id: str, source_sha256: str, limits: dict[str, int], suffix: str) -> list[dict[str, Any]]:
    _enforce_expanded_limits(path, limits, suffix)
    value = json.loads(path.read_text(encoding="utf-8"))
    if _depth(value) > limits["max_json_depth"]:
        raise ValueError("json-depth")
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    rows = []
    for row_index, item in enumerate(items):
        cells = [f"{k}={v}" for k, v in item.items()] if isinstance(item, dict) else [item]
        _check_count(row_index + 1, limits["max_rows"], ValueError("row-limit"))
        _check_count(len(cells), limits["max_columns"], ValueError("column-limit"))
        rows.append(_row(artifact_id, source_sha256, 0, 0, row_index, cells, limits["max_cell_codepoints"]))
    return rows


def _parse_excel(path: Path, artifact_id: str, source_sha256: str, limits: dict[str, int], *, engine: str, suffix: str) -> list[dict[str, Any]]:
    _enforce_expanded_limits(path, limits, suffix)
    book = pd.ExcelFile(path, engine=engine)
    _check_count(len(book.sheet_names), limits["max_sheets"], ValueError("sheet-limit"))
    rows: list[dict[str, Any]] = []
    table_total = 0
    for sheet_index, sheet_name in enumerate(book.sheet_names):
        raw = book.parse(sheet_name=sheet_name, header=None)
        tables = split_sheet_into_tables(raw) or []
        table_total += len(tables)
        _check_count(table_total, limits["max_tables"], ValueError("table-limit"))
        for table_index, table in enumerate(tables):
            promoted = promote_header(table).astype(object).where(pd.notnull(table), "")
            for local_row_index, record in enumerate(promoted.to_numpy().tolist()):
                _check_count(len(rows) + 1, limits["max_rows"], ValueError("row-limit"))
                _check_count(len(record), limits["max_columns"], ValueError("column-limit"))
                rows.append(_row(artifact_id, source_sha256, sheet_index, table_index, local_row_index, record, limits["max_cell_codepoints"]))
    return rows


def _parse_pdf(path: Path, artifact_id: str, source_sha256: str, limits: dict[str, int], suffix: str) -> list[dict[str, Any]]:
    _enforce_expanded_limits(path, limits, suffix)
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ValueError("parse-error") from exc
    reader = PdfReader(str(path))
    rows: list[dict[str, Any]] = []
    _check_count(len(reader.pages), limits["max_sheets"], ValueError("sheet-limit"))
    for page_index, page in enumerate(reader.pages):
        text = page.extract_text() or ""
        for line in text.splitlines():
            if not line.strip():
                continue
            _check_count(len(rows) + 1, limits["max_rows"], ValueError("row-limit"))
            rows.append(_row(artifact_id, source_sha256, page_index, 0, len(rows), [line], limits["max_cell_codepoints"]))
    if not rows:
        raise ValueError("parse-error")
    return rows


def parse_support_artifact(
    *,
    artifact_id: str,
    source_sha256: str,
    kind: DependencyKind,
    source_path: Path,
    output_dir: Path,
    logical_format: str | None = None,
    normalized_source_stem: str | None = None,
    limits: dict[str, int] | None = None,
) -> ParsedSupportArtifact:
    active_limits = dict(DEFAULT_LIMITS)
    if limits:
        for key, value in limits.items():
            if key in active_limits:
                active_limits[key] = min(active_limits[key], int(value))
    source_path = Path(source_path)
    suffix = source_path.suffix.lower()
    if logical_format is not None:
        fmt = logical_format.lower().lstrip(".")
        suffix = "." + fmt
    else:
        fmt = suffix.lstrip(".")
    if suffix not in _SUPPORTED_SUFFIXES:
        return _fail(artifact_id, source_sha256, kind, fmt, SupportFailureCode.UNSUPPORTED_FORMAT)
    if source_path.stat().st_size > active_limits["max_source_bytes"]:
        return _fail(artifact_id, source_sha256, kind, fmt, SupportFailureCode.SOURCE_SIZE_LIMIT)
    try:
        if suffix == ".csv":
            rows = _parse_csv(source_path, artifact_id, source_sha256, active_limits, suffix)
        elif suffix == ".jsonl":
            rows = _parse_jsonl(source_path, artifact_id, source_sha256, active_limits, suffix)
        elif suffix == ".json":
            rows = _parse_json(source_path, artifact_id, source_sha256, active_limits, suffix)
        elif suffix == ".xlsx":
            rows = _parse_excel(source_path, artifact_id, source_sha256, active_limits, engine="openpyxl", suffix=suffix)
        elif suffix == ".xls":
            rows = _parse_excel(source_path, artifact_id, source_sha256, active_limits, engine="xlrd", suffix=suffix)
        elif suffix == ".pdf":
            rows = _parse_pdf(source_path, artifact_id, source_sha256, active_limits, suffix)
        else:
            return _fail(artifact_id, source_sha256, kind, fmt, SupportFailureCode.UNSUPPORTED_FORMAT)
    except ValueError as exc:
        code = {
            "row-limit": SupportFailureCode.ROW_LIMIT,
            "column-limit": SupportFailureCode.COLUMN_LIMIT,
            "cell-limit": SupportFailureCode.CELL_SIZE_LIMIT,
            "json-depth": SupportFailureCode.JSON_DEPTH_LIMIT,
            "sheet-limit": SupportFailureCode.SHEET_LIMIT,
            "table-limit": SupportFailureCode.TABLE_LIMIT,
            "expanded-limit": SupportFailureCode.EXPANDED_SIZE_LIMIT,
            "ratio-limit": SupportFailureCode.DECOMPRESSION_RATIO_LIMIT,
            "parse-error": SupportFailureCode.PARSE_ERROR,
        }.get(str(exc), SupportFailureCode.PARSE_ERROR)
        return _fail(artifact_id, source_sha256, kind, fmt, code)
    except Exception:
        return _fail(artifact_id, source_sha256, kind, fmt, SupportFailureCode.PARSE_ERROR)
    stem = normalized_source_stem or source_path.stem or artifact_id
    out_path = output_dir / f"{stem}__{artifact_id}.jsonl"
    normalized_sha = _write_jsonl(out_path, rows)
    return ParsedSupportArtifact(
        artifact_id=artifact_id,
        source_sha256=source_sha256,
        kind=kind,
        format=fmt,
        parse_status=SupportParseStatus.PARSED,
        normalized_rows_path=out_path,
        normalized_rows_sha256=normalized_sha,
        failure_code=None,
    )


# --- shared bounded-ZipFile primitive -----------------------------------------------------
#
# One directory-bounded ``zipfile.ZipFile`` construction path, reused by both
# intake_preflight.py's workbook-metadata sheet count and intake_naming.py's
# xlsx evidence extraction, so a ZIP-backed document's central-directory
# parse -- and, from the caller's own member/aggregate/ratio checks against
# ``DEFAULT_LIMITS``, its member reads -- are governed by exactly one
# fail-closed reader rather than two divergent copies. ``zipfile`` remains
# the sole, authoritative ZIP parser throughout; this never reimplements
# EOCD/central-directory/ZIP64 structure of its own.

# Hostile-input exceptions normalized to the fixed reason code below.
_HOSTILE_ZIP_EXCEPTIONS = (
    zipfile.BadZipFile,
    OSError,
    EOFError,
    RuntimeError,
    NotImplementedError,
    ValueError,
    struct.error,
    zlib.error,
    lzma.LZMAError,
)


class BoundedZipMemberError(Exception):
    """Fixed, value-free signal: a ZIP-backed document could not be safely
    bounded under ``DEFAULT_LIMITS`` (central directory, member count,
    per-member size, aggregate expansion, or decompression ratio), or the
    archive itself is malformed. Carries no raw exception text or path."""


class _BoundedReader:
    """Enforces an actual-bytes-read cap, independent of any declared/attacker-
    controlled ZIP central-directory size metadata. Every delegated read is
    itself clamped to at most the remaining budget (including negative
    "read everything" requests), so no single call can pull an unbounded
    amount from the underlying stream/decompressor before the cap is
    checked. Also usable to wrap the raw archive stream itself (with
    ``seek``/``tell`` passthrough) so ``zipfile``'s own central-directory
    parse cannot allocate an unbounded amount before we ever see it;
    ``rearm`` lets the SAME wrapper instance -- and therefore the same
    ``ZipFile`` object built on it -- switch to a different budget for a
    later phase (e.g. once central-directory parsing has finished) without
    ever reconstructing or reparsing the archive.
    """

    def __init__(self, stream: BinaryIO, limit: int) -> None:
        self._stream = stream
        self._limit = limit
        self._read = 0

    def rearm(self, limit: int) -> None:
        self._limit = limit
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self._limit - self._read + 1  # +1 so overage is observable
        if remaining <= 0:
            raise BoundedZipMemberError()
        request = remaining if (size is None or size < 0 or size > remaining) else size
        chunk = self._stream.read(request)
        self._read += len(chunk)
        if self._read > self._limit:
            raise BoundedZipMemberError()
        return chunk

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def seekable(self) -> bool:
        seek_check = getattr(self._stream, "seekable", None)
        return True if seek_check is None else bool(seek_check())


@contextlib.contextmanager
def open_bounded_zipfile(fileobj: BinaryIO, limits: Mapping[str, int]) -> Iterator[tuple[zipfile.ZipFile, _BoundedReader]]:
    """Construct exactly one ``zipfile.ZipFile`` on a directory-bounded
    reader, then validate member count, per-member size, aggregate expanded
    size, and decompression ratio from that single central-directory parse
    against ``limits`` (the caller's own ``DEFAULT_LIMITS``-derived table).
    Once validated, the SAME wrapper is rearmed to ``limits["max_source_bytes"]``
    before being yielded alongside the open ``ZipFile``, so any subsequent
    member read the caller (or a library it hands the wrapper to) performs
    is governed by the already-approved bounds, not the tiny directory cap.
    Raises :class:`BoundedZipMemberError` with no raw exception text on any
    malformed, encrypted, oversized, or pathological input.
    """

    guarded = _BoundedReader(fileobj, limits["max_zip_directory_bytes"])
    try:
        fileobj.seek(0, 0)
        with zipfile.ZipFile(guarded) as zf:
            infolist = zf.infolist()
            if len(infolist) > limits["max_zip_members"]:
                raise BoundedZipMemberError()
            expanded_total = 0
            compressed_total = 0
            for info in infolist:
                if int(info.file_size) > limits["max_zip_member_bytes"]:
                    raise BoundedZipMemberError()
                expanded_total += int(info.file_size)
                compressed_total += int(info.compress_size)
            if expanded_total > limits["max_expanded_workbook_bytes"]:
                raise BoundedZipMemberError()
            compressed_total = compressed_total or 1
            if (expanded_total / compressed_total) > limits["max_decompression_ratio"]:
                raise BoundedZipMemberError()
            guarded.rearm(limits["max_source_bytes"])
            yield zf, guarded
    except BoundedZipMemberError:
        raise
    except _HOSTILE_ZIP_EXCEPTIONS:
        raise BoundedZipMemberError() from None
