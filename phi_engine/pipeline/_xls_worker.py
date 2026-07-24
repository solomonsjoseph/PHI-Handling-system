"""Private, minimal-import worker for phi_engine.pipeline.xls_isolation's
isolated legacy ``.xls`` (BIFF) extraction.

Deliberately imports NOTHING from ``phi_engine`` itself at module
top-level, and imports ``xlrd``/``pandas`` only inside :func:`run` --
the same discipline ``_pdf_extract_worker.py`` uses for the existing
PDF isolation boundary, for the identical reason: the hard
``RLIMIT_AS``/``RLIMIT_CPU`` bound this module's entry point applies to
itself is only meaningful if the process's own baseline virtual-
address-space footprint (before touching any workbook bytes) stays
small. ``xls_isolation.py``'s own top-level imports (``phi_engine``
dataclasses, config, other pipeline siblings) pull in a much larger
baseline than this module allows before a single byte of
intake-controlled BIFF content is ever parsed -- this module exists so
the spawned child never imports that module, or any other
``phi_engine`` module, at all. Header-name normalization and header-id
derivation intentionally duplicate the small, pure formulas already
used by ``phi_engine.pipeline.organize`` (regex normalization,
``sha256(artifact_id || 0 || source_sha256 || 0 || column_index)``)
rather than importing them, for the same reason.

The reply crossing back into the privileged parent process is bounded,
non-executable ASCII JSON frames -- never ``pickle`` or any other
format that could execute code while being decoded -- sent one frame
per ``conn.send_bytes`` call. Every internal failure collapses to one
of a small fixed set of error codes; no exception text, path, value,
or traceback ever crosses this boundary.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import stat
from typing import Any

# --- fd carrier: transfers the raw input-pipe read fd across a spawned
# process boundary via multiprocessing's active-popen duplicate-for-child
# path (see xls_isolation.py's module docstring for the full rationale).
# Deliberately does NOT auto-detach on unpickling: the child calls
# detach() itself, only after applying its hard resource limits, so the
# raw fd is never touchable before those limits are in place. ---------


class FdCarrier:
    """Picklable wrapper around a read-only pipe file descriptor.

    In the parent process, constructed via :meth:`for_fd` holding the
    raw fd number. Its ``__reduce__`` is invoked only while a
    ``multiprocessing.Process.start()`` call is actively serializing
    it (``multiprocessing.context.get_spawning_popen()`` is non-None at
    that moment), so ``multiprocessing.reduction.DupFd`` always takes
    the active-popen duplicate-for-child path -- never the unrelated
    ``resource_sharer.DupFd`` background-thread/SCM_RIGHTS path, which
    is for passing fds between processes that are not directly related
    by an in-flight ``Process.start()`` call.

    In the child process, ``_rebuild_fd_carrier`` reconstructs an
    instance wrapping the already-unpickled dup wrapper WITHOUT calling
    ``.detach()`` -- that call is deferred to :meth:`detach`, invoked by
    :func:`run` only after ``RLIMIT_AS``/``RLIMIT_CPU`` are already in
    place.
    """

    __slots__ = ("_payload", "_is_dup")

    def __init__(self, fd: int) -> None:
        self._payload: Any = fd
        self._is_dup = False

    @classmethod
    def for_fd(cls, fd: int) -> "FdCarrier":
        return cls(fd)

    def detach(self) -> int:
        if not self._is_dup:
            raise RuntimeError("FdCarrier.detach() called before crossing a process boundary")
        dup, self._payload = self._payload, None
        return dup.detach()

    def __reduce__(self) -> tuple[Any, tuple[Any, ...]]:
        from multiprocessing.reduction import DupFd

        dup = DupFd(self._payload)
        return (_rebuild_fd_carrier, (dup,))


def _rebuild_fd_carrier(dup: Any) -> FdCarrier:
    carrier = FdCarrier.__new__(FdCarrier)
    carrier._payload = dup
    carrier._is_dup = True
    return carrier


# --- typed internal failure: carries only a fixed wire error code, never
# exception text. --------------------------------------------------------


class _WorkerFailure(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_WORKER_CODES = frozenset(
    {
        "reader-unavailable",
        "parse-error",
        "sheet-limit",
        "table-limit",
        "row-limit",
        "column-limit",
        "cell-limit",
        "expanded-limit",
        "resource-limit",
    }
)
_CHILD_TERMINAL_CODES = _WORKER_CODES | {"source-mismatch"}

_NAMING_MAX_SHEETS = 4
_NAMING_MAX_ROWS = 20
_NAMING_MAX_COLS = 20
_INPUT_CHUNK_BYTES = 1 << 20
_CONTROL_FRAME_MAX_BYTES = 65536  # cumulative reply ceiling for inspect/naming (item 3)


# --- bounded, non-monolithic JSON encoding -------------------------------


def _encode_bounded(payload: dict[str, Any], max_bytes: int) -> bytes | None:
    """Incrementally encode ``payload`` as compact ASCII JSON via
    ``JSONEncoder.iterencode()``, aborting BEFORE retaining any
    fragment that would cross ``max_bytes`` -- the full string is never
    materialized first. Returns ``None`` on overflow or any encoding
    failure (non-finite float, unpicklable value, recursion)."""
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"), default=str)
    parts: list[str] = []
    total = 0
    try:
        for fragment in encoder.iterencode(payload):
            total += len(fragment)
            if total > max_bytes:
                return None
            parts.append(fragment)
    except (TypeError, ValueError, RecursionError):
        return None
    return "".join(parts).encode("ascii")


def _send_frame(conn: Any, payload: dict[str, Any], max_bytes: int) -> bool:
    encoded = _encode_bounded(payload, max_bytes)
    if encoded is None:
        return False
    try:
        conn.send_bytes(encoded)
        return True
    except OSError:
        return False


def _send_error(conn: Any, code: str) -> None:
    if code not in _CHILD_TERMINAL_CODES:
        code = "resource-limit"
    try:
        conn.send_bytes(json.dumps({"type": "error", "code": code}, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
    except OSError:
        pass


# --- header-id / header-name derivation (duplicated pure formulas; see
# module docstring) -------------------------------------------------------


def _header_id(artifact_id: str, source_sha256: str, column_index: int) -> str:
    payload = artifact_id.encode() + b"\0" + source_sha256.encode() + b"\0" + str(column_index).encode()
    return "h_" + hashlib.sha256(payload).hexdigest()[:24]


def _normalize_header_name(header: str) -> str:
    normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", header.strip())
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", normalized)
    return normalized.strip("_").lower()


# --- input read: verified FIFO, exact-size, hashed --------------------


def _read_verified_input(fd: int, input_size: int, expected_sha256: str) -> bytes:
    try:
        mode_bits = os.fstat(fd).st_mode
    except OSError:
        raise _WorkerFailure("reader-unavailable") from None
    if not stat.S_ISFIFO(mode_bits):
        raise _WorkerFailure("reader-unavailable")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    remaining = input_size
    with os.fdopen(fd, "rb", closefd=True) as reader:
        while remaining > 0:
            chunk = reader.read(min(remaining, _INPUT_CHUNK_BYTES))
            if not chunk:
                raise _WorkerFailure("source-mismatch")  # short read
            chunks.append(chunk)
            digest.update(chunk)
            remaining -= len(chunk)
        if reader.read(1):
            raise _WorkerFailure("source-mismatch")  # extra bytes past declared size
    if digest.hexdigest() != expected_sha256:
        raise _WorkerFailure("source-mismatch")
    return b"".join(chunks)


# --- xlrd cell conversion (pandas-compatible) ----------------------------


def _xlrd_cell_value(cell: Any, book: Any, xlrd_mod: Any) -> Any:
    ctype = cell.ctype
    value = cell.value
    if ctype == xlrd_mod.XL_CELL_EMPTY or ctype == xlrd_mod.XL_CELL_BLANK:
        return None
    if ctype == xlrd_mod.XL_CELL_ERROR:
        return float("nan")
    if ctype == xlrd_mod.XL_CELL_BOOLEAN:
        return bool(value)
    if ctype == xlrd_mod.XL_CELL_NUMBER:
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        return value
    if ctype == xlrd_mod.XL_CELL_DATE:
        try:
            converted = xlrd_mod.xldate.xldate_as_datetime(value, book.datemode)
        except xlrd_mod.xldate.XLDateError:
            return value
        if value < 1:
            return converted.time()
        return converted
    return value


def _safe_cell_value(sheet: Any, r: int, c: int, book: Any, xlrd_mod: Any) -> Any:
    """``ragged_rows=True`` means a row with fewer written cells than
    ``sheet.ncols`` (the widest row's width) has a genuinely shorter
    underlying cell-type array -- indexing past ``sheet.row_len(r)``
    raises ``IndexError`` rather than returning a blank cell. Every
    column beyond a row's own actual length is a genuine blank."""
    if c >= sheet.row_len(r):
        return None
    return _xlrd_cell_value(sheet.cell(r, c), book, xlrd_mod)


def _sheet_rows(sheet: Any, book: Any, xlrd_mod: Any) -> list[list[Any]]:
    return [[_safe_cell_value(sheet, r, c, book, xlrd_mod) for c in range(sheet.ncols)] for r in range(sheet.nrows)]


def _split_tables(rows: list[list[Any]]) -> list[list[list[Any]]]:
    """Segment ``rows`` into contiguous blocks separated by fully-blank
    rows (every cell ``None``). Blank rows never appear inside a
    returned block."""
    tables: list[list[list[Any]]] = []
    current: list[list[Any]] = []
    for row in rows:
        if all(cell is None for cell in row):
            if current:
                tables.append(current)
                current = []
            continue
        current.append(row)
    if current:
        tables.append(current)
    return tables


def _check_non_finite(rows: list[list[Any]]) -> None:
    """Dataset-only: before any DataFrame construction/padding, reject
    any raw converted cell that is a real number with a non-finite
    value (``NaN``/``Infinity``/``-Infinity``) -- including the ``NaN``
    produced for a formula-error cell. Must run before pandas padding
    introduces its OWN (legitimate) NaN for ragged rows."""
    for row in rows:
        for value in row:
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and not math.isfinite(value):
                raise _WorkerFailure("parse-error")


def _require_cell_length(text: str, limit: int) -> str:
    if len(text) > limit:
        raise _WorkerFailure("cell-limit")
    return text


def _naming_cell(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return _require_cell_length(text, limit)


# --- mode implementations -------------------------------------------------


def _open_book(data: bytes, xlrd_mod: Any) -> Any:
    logfile = io.StringIO()
    try:
        return xlrd_mod.open_workbook(
            file_contents=data,
            on_demand=True,
            formatting_info=False,
            ragged_rows=True,
            ignore_workbook_corruption=False,
            logfile=logfile,
        )
    except Exception:
        raise _WorkerFailure("parse-error") from None


def _run_inspect(data: bytes, conn: Any, xlrd_mod: Any, limits: dict[str, int]) -> None:
    book = _open_book(data, xlrd_mod)
    try:
        sheet_count = book.nsheets
    finally:
        try:
            book.release_resources()
        except Exception:
            pass
    if not _send_frame(conn, {"type": "inspection", "sheet_count": sheet_count}, _CONTROL_FRAME_MAX_BYTES):
        raise _WorkerFailure("expanded-limit")


def _run_naming(data: bytes, conn: Any, xlrd_mod: Any, limits: dict[str, int]) -> None:
    book = _open_book(data, xlrd_mod)
    cell_limit = limits["max_cell_codepoints"]
    sheets_payload: list[dict[str, Any]] = []
    try:
        sheet_count = min(book.nsheets, _NAMING_MAX_SHEETS)
        for index in range(sheet_count):
            try:
                sheet = book.sheet_by_index(index)
                rows: list[list[str]] = []
                row_count = min(sheet.nrows, _NAMING_MAX_ROWS)
                col_count = min(sheet.ncols, _NAMING_MAX_COLS)
                for r in range(row_count):
                    rows.append([_naming_cell(_safe_cell_value(sheet, r, c, book, xlrd_mod), cell_limit) for c in range(col_count)])
                sheets_payload.append({"index": index + 1, "rows": rows})
            finally:
                try:
                    book.unload_sheet(index)
                except Exception:
                    pass
    finally:
        try:
            book.release_resources()
        except Exception:
            pass
    if not _send_frame(conn, {"type": "naming", "sheets": sheets_payload}, _CONTROL_FRAME_MAX_BYTES):
        raise _WorkerFailure("expanded-limit")


def _run_normalize_dataset(data: bytes, conn: Any, xlrd_mod: Any, pd_mod: Any, limits: dict[str, int], artifact_id: str, source_sha256: str) -> None:
    book = _open_book(data, xlrd_mod)
    frame_limit = limits["max_wire_frame_bytes"]
    cell_limit = limits["max_cell_codepoints"]
    try:
        sheet_total = book.nsheets
        if sheet_total > 1:
            # Defense-in-depth: the parent (via inspect_xls) is expected to
            # reject a multi-sheet dataset workbook before ever calling this
            # mode, but this worker must never silently normalize only the
            # first of several sheets -- an ordinary worker-level failure,
            # not a protocol violation, since it is fully attributable to
            # workbook content the parent's own preflight should have caught.
            raise _WorkerFailure("sheet-limit")
        if sheet_total < 1:
            if not _send_frame(conn, {"type": "done", "output_count": 0}, frame_limit):
                raise _WorkerFailure("expanded-limit")
            return
        sheet_index = 0
        try:
            sheet = book.sheet_by_index(sheet_index)
            if sheet.nrows > limits["max_rows"]:
                raise _WorkerFailure("row-limit")
            if sheet.ncols > limits["max_columns"]:
                raise _WorkerFailure("column-limit")
            rows = _sheet_rows(sheet, book, xlrd_mod)
        finally:
            try:
                book.unload_sheet(sheet_index)
            except Exception:
                pass
        _check_non_finite(rows)
        tables = _split_tables(rows)
        if not tables:
            if not _send_frame(conn, {"type": "done", "output_count": 0}, frame_limit):
                raise _WorkerFailure("expanded-limit")
            return
        if len(tables) > 1:
            raise _WorkerFailure("table-limit")
        table_index = 0
        table = tables[0]
        header_row, data_rows = table[0], table[1:]
        headers = []
        for column_index, raw in enumerate(header_row):
            raw_name = "" if raw is None else str(raw)
            raw_name = _require_cell_length(raw_name, cell_limit)
            headers.append(
                {
                    "header_id": _header_id(artifact_id, source_sha256, column_index),
                    "column_index": column_index,
                    "raw_name": raw_name,
                    "normalized_name": _normalize_header_name(raw_name),
                }
            )
        header_ids = [h["header_id"] for h in headers]
        df = pd_mod.DataFrame(data_rows, columns=range(len(header_row))) if data_rows else pd_mod.DataFrame(columns=range(len(header_row)))
        df = df.astype(object).where(pd_mod.notnull(df), None)
        begin_payload = {
            "type": "begin",
            "output_index": 0,
            "sheet_index": sheet_index + 1,
            "table_index": table_index,
            "headers": headers,
        }
        if not _send_frame(conn, begin_payload, frame_limit):
            raise _WorkerFailure("expanded-limit")
        canonical_digest = hashlib.sha256()
        row_count = 0
        for row_index, record in enumerate(df.to_numpy().tolist() if len(df.columns) else []):
            row_obj = {}
            for column_index, value in enumerate(record):
                if isinstance(value, str):
                    value = _require_cell_length(value, cell_limit)
                row_obj[header_ids[column_index]] = value
            row_payload = {"type": "row", "output_index": 0, "row_index": row_index, "row": row_obj}
            if not _send_frame(conn, row_payload, frame_limit):
                raise _WorkerFailure("expanded-limit")
            canonical = json.dumps(row_obj, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8") + b"\n"
            canonical_digest.update(canonical)
            row_count += 1
        end_payload = {"type": "end", "output_index": 0, "row_count": row_count, "sha256": canonical_digest.hexdigest()}
        if not _send_frame(conn, end_payload, frame_limit):
            raise _WorkerFailure("expanded-limit")
        if not _send_frame(conn, {"type": "done", "output_count": 1}, frame_limit):
            raise _WorkerFailure("expanded-limit")
    finally:
        try:
            book.release_resources()
        except Exception:
            pass


def _run_normalize_support(data: bytes, conn: Any, xlrd_mod: Any, limits: dict[str, int], artifact_id: str, source_sha256: str) -> None:
    book = _open_book(data, xlrd_mod)
    frame_limit = limits["max_wire_frame_bytes"]
    cell_limit = limits["max_cell_codepoints"]
    try:
        begin_payload = {"type": "begin", "output_index": 0, "sheet_index": None, "table_index": None, "headers": []}
        if not _send_frame(conn, begin_payload, frame_limit):
            raise _WorkerFailure("expanded-limit")
        canonical_digest = hashlib.sha256()
        output_row_index = 0
        table_total = 0
        for sheet_index in range(book.nsheets):
            try:
                sheet = book.sheet_by_index(sheet_index)
                if sheet.nrows > limits["max_rows"]:
                    raise _WorkerFailure("row-limit")
                if sheet.ncols > limits["max_columns"]:
                    raise _WorkerFailure("column-limit")
                rows = _sheet_rows(sheet, book, xlrd_mod)
            finally:
                try:
                    book.unload_sheet(sheet_index)
                except Exception:
                    pass
            for table_index, table in enumerate(_split_tables(rows)):
                table_total += 1
                if table_total > limits["max_tables"]:
                    raise _WorkerFailure("table-limit")
                data_rows = table[1:]  # first row promoted (dropped) as header, matching support_files.py's xlsx convention
                for local_row_index, record in enumerate(data_rows):
                    cells = [{"column_index": idx, "value": _naming_cell(value, cell_limit)} for idx, value in enumerate(record)]
                    row_obj = {
                        "support_artifact_id": artifact_id,
                        "source_sha256": source_sha256,
                        "sheet_index": sheet_index,
                        "table_index": table_index,
                        "row_index": local_row_index,
                        "cells": cells,
                    }
                    row_payload = {"type": "row", "output_index": 0, "row_index": output_row_index, "row": row_obj}
                    if not _send_frame(conn, row_payload, frame_limit):
                        raise _WorkerFailure("expanded-limit")
                    canonical = json.dumps(row_obj, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8") + b"\n"
                    canonical_digest.update(canonical)
                    output_row_index += 1
        end_payload = {"type": "end", "output_index": 0, "row_count": output_row_index, "sha256": canonical_digest.hexdigest()}
        if not _send_frame(conn, end_payload, frame_limit):
            raise _WorkerFailure("expanded-limit")
        if not _send_frame(conn, {"type": "done", "output_count": 1}, frame_limit):
            raise _WorkerFailure("expanded-limit")
    finally:
        try:
            book.release_resources()
        except Exception:
            pass


# --- entry point -----------------------------------------------------------


def _apply_resource_limits(limits: dict[str, int]) -> None:
    try:
        import resource

        max_address_bytes = limits["max_address_bytes"]
        max_cpu_seconds = limits["max_cpu_seconds"]
        resource.setrlimit(resource.RLIMIT_AS, (max_address_bytes, max_address_bytes))
        resource.setrlimit(resource.RLIMIT_CPU, (max_cpu_seconds, max_cpu_seconds))
    except Exception:
        raise _WorkerFailure("reader-unavailable") from None


def _dispatch(mode: str, input_read_fd: Any, input_size: int, expected_sha256: str, artifact_id: str | None, limits: dict[str, int], conn: Any) -> None:
    # Hard resource limits MUST be in place before the sole BIFF-input
    # detach, and long before any parser import or byte is touched.
    _apply_resource_limits(limits)

    try:
        fd = input_read_fd.detach()
    except Exception:
        raise _WorkerFailure("reader-unavailable") from None

    data = _read_verified_input(fd, input_size, expected_sha256)

    try:
        import xlrd
        import xlrd.xldate  # noqa: F401 -- accessed via xlrd.xldate below
    except ImportError:
        raise _WorkerFailure("reader-unavailable") from None

    if mode in ("normalize_dataset", "normalize_support"):
        try:
            import pandas as pd
        except ImportError:
            raise _WorkerFailure("reader-unavailable") from None
    else:
        pd = None  # type: ignore[assignment]

    if mode == "inspect":
        _run_inspect(data, conn, xlrd, limits)
    elif mode == "naming":
        _run_naming(data, conn, xlrd, limits)
    elif mode == "normalize_dataset":
        _run_normalize_dataset(data, conn, xlrd, pd, limits, artifact_id or "", expected_sha256)
    elif mode == "normalize_support":
        _run_normalize_support(data, conn, xlrd, limits, artifact_id or "", expected_sha256)
    else:
        raise _WorkerFailure("resource-limit")


def run(
    mode: str,
    input_read_fd: Any,
    input_size: int,
    expected_sha256: str,
    artifact_id: str | None,
    limits: dict[str, int],
    conn: Any,
) -> None:
    """Runs ONLY inside the isolated child process spawned by
    ``phi_engine.pipeline.xls_isolation``. See the module docstring for
    the full security rationale. Every internal failure -- a resource-
    limit-application failure, a resource-limit kill, an xlrd/pandas
    exception, a pathological structure, a hash mismatch -- collapses
    to one fixed, value-free error frame rather than letting a raw
    exception (whose message could echo source content) cross the
    process boundary."""
    try:
        _dispatch(mode, input_read_fd, input_size, expected_sha256, artifact_id, limits, conn)
    except _WorkerFailure as failure:
        _send_error(conn, failure.code)
    except BaseException:
        _send_error(conn, "resource-limit")
    finally:
        try:
            conn.close()
        except Exception:
            pass
