"""Parent-side orchestration for the bounded legacy ``.xls`` (BIFF)
isolation boundary.

This module -- together with the private, minimal-import
``phi_engine.pipeline._xls_worker`` -- is the ONLY place in this
repository permitted to touch ``xlrd`` or intake-controlled BIFF bytes.
No other production module may import ``xlrd``, call
``pandas.ExcelFile(..., engine="xlrd")``, or parse ``.xls`` content;
``tests/test_xls_dependency_contract.py`` enforces that boundary
mechanically.

Every actual byte of an ``.xls`` source is parsed inside a spawned,
resource-limited child process running ``_xls_worker.run`` -- never in
this process. This module's job is entirely: verify the pre-hashed
input, spawn the child via the ``multiprocessing`` ``spawn`` start
method (never ``fork``, so the child never inherits this process's
already-large heap or any of its open descriptors beyond the two
explicitly wired), stream the input bytes across an anonymous pipe
while concurrently draining bounded, non-executable JSON reply frames
over a *separate* ``multiprocessing.Pipe`` connection, validate every
frame against an exact schema, and -- for the two normalization modes
-- write validated rows to a staged bundle that is published (or
quarantined) via the shared no-replace rename primitive in
``phi_engine.utils.atomic_fs``.

**fd-carrier lifecycle.** The parent creates one anonymous OS pipe via
``os.pipe()``. The read end is wrapped in ``_xls_worker.FdCarrier``,
whose ``__reduce__`` invokes ``multiprocessing.reduction.DupFd`` only
while ``Process.start()`` is actively serializing it under the
``spawn`` start method -- the active-popen duplicate-for-child path,
never the unrelated ``resource_sharer`` background-thread path. The
parent's own copy of the raw read fd, and its own copy of the
child-write-only reply ``Connection`` endpoint, are closed immediately
after ``Process.start()`` returns -- on both the success and the
failure branch -- so this process is never left holding a duplicate
descriptor the child alone is meant to own. The write end of the input
pipe, and the parent's read end of the reply ``Connection``, are the
only descriptors this process keeps open across the full call, and
both are unconditionally closed in every exit path (deadline, crash,
success) before returning.

**Locking.** Publication under ``organized_root`` assumes the caller
already holds ``phi_engine.utils.pipeline_lock.pipeline_lock(study)``
for the duration of the call -- exactly the same single per-study lock
``phi_engine.pipeline.organize`` already acquires for the whole
organize run. This module does not acquire a second, XLS-specific
lock: doing so would either be redundant under that existing
single-writer-per-study invariant, or would need to nest inside a lock
scope this module cannot see (the caller's), which is out of scope for
this module's own contract. Bundle publication is therefore safe under
the existing convention, not under a novel one.
"""

from __future__ import annotations

import hashlib
import json
import math
import multiprocessing
import os
import re
import secrets
import selectors
import shutil
import stat
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from phi_engine.pipeline import _xls_worker
from phi_engine.pipeline.dependencies import (
    DependencyKind,
    OrganizedHeader,
    ParsedSupportArtifact,
    SupportFailureCode,
    SupportParseStatus,
)
from phi_engine.utils.atomic_fs import AtomicRenameUnavailable, renameat2_noreplace

__all__ = [
    "WorkerCode",
    "IsolationCode",
    "XlsWorkerError",
    "XlsIsolationError",
    "XlsRetainedNode",
    "XlsRetainedBundle",
    "XlsPackageBudget",
    "XlsPublishedFile",
    "XlsSupportPublication",
    "XlsSupportResult",
    "NormalizedXlsDataset",
    "XlsDatasetPublication",
    "INSPECT_LIMITS",
    "NAMING_LIMITS",
    "DEFAULT_LIMITS",
    "inspect_xls",
    "extract_xls_naming",
    "normalize_xls_support",
    "normalize_xls_datasets",
]


# ---------------------------------------------------------------------------
# Typed codes -- mirrors the worker's own fixed code sets exactly, plus the
# parent-only IsolationCode family from Approach 1.7.
# ---------------------------------------------------------------------------

WorkerCode = Literal[
    "reader-unavailable",
    "parse-error",
    "sheet-limit",
    "table-limit",
    "row-limit",
    "column-limit",
    "cell-limit",
    "expanded-limit",
    "resource-limit",
]
ChildTerminalCode = Literal[
    "source-mismatch",
    "reader-unavailable",
    "parse-error",
    "sheet-limit",
    "table-limit",
    "row-limit",
    "column-limit",
    "cell-limit",
    "expanded-limit",
    "resource-limit",
]
IsolationCode = Literal[
    "source-mismatch",
    "isolation-unavailable",
    "resource-limit",
    "package-resource-limit",
    "retention-limit",
    "protocol-invalid",
    "output-collision",
    "output-directory-changed",
]

_WORKER_CODES: frozenset[str] = frozenset(
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
_CHILD_TERMINAL_CODES: frozenset[str] = _WORKER_CODES | {"source-mismatch"}
_ISOLATION_CODES: frozenset[str] = frozenset(
    {
        "source-mismatch",
        "isolation-unavailable",
        "resource-limit",
        "package-resource-limit",
        "retention-limit",
        "protocol-invalid",
        "output-collision",
        "output-directory-changed",
    }
)


class XlsWorkerError(Exception):
    """Value-free beyond its fixed ``code``: a genuine ``WorkerCode``
    reported by the isolated child, or coerced to ``"resource-limit"``
    if an internal caller ever passed something else. Never carries
    exception text, a path, or a traceback."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code: str = code if code in _WORKER_CODES else "resource-limit"


class XlsIsolationError(Exception):
    """Value-free beyond its fixed ``code``: a parent-level
    ``IsolationCode``. Never carries exception text, a path, or a
    traceback."""

    def __init__(self, code: str) -> None:
        super().__init__()
        self.code: str = code if code in _ISOLATION_CODES else "resource-limit"


# ---------------------------------------------------------------------------
# Frozen public records (Approach 1.7)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class XlsRetainedNode:
    relative_path: str
    node_type: Literal["directory", "file"]
    mode: int
    device: int
    inode: int
    size: int
    sha256: str | None


@dataclass(frozen=True)
class XlsRetainedBundle:
    nodes: tuple[XlsRetainedNode, ...]


@dataclass
class XlsPackageBudget:
    remaining_canonical_bytes: int
    remaining_wire_bytes: int
    deadline: float
    retained_bundles: list[XlsRetainedBundle] = field(default_factory=list)


@dataclass(frozen=True)
class XlsPublishedFile:
    path: Path
    device: int
    inode: int
    sha256: str
    row_count: int


@dataclass(frozen=True)
class XlsSupportPublication:
    bundle_path: Path
    bundle_device: int
    bundle_inode: int
    output: XlsPublishedFile


@dataclass(frozen=True)
class XlsSupportResult:
    artifact: ParsedSupportArtifact
    publication: XlsSupportPublication | None


@dataclass(frozen=True)
class NormalizedXlsDataset:
    path: Path
    device: int
    inode: int
    sha256: str
    row_count: int
    headers: tuple[OrganizedHeader, ...]


@dataclass(frozen=True)
class XlsDatasetPublication:
    bundle_path: Path
    bundle_device: int
    bundle_inode: int
    output: NormalizedXlsDataset


# ---------------------------------------------------------------------------
# Resource profiles (Approach 1.3)
# ---------------------------------------------------------------------------

INSPECT_LIMITS: dict[str, int] = {
    "max_source_bytes": 128 * 1024 * 1024,
    "max_address_bytes": 1 * 1024**3,
    "max_cpu_seconds": 10,
    "wall_deadline_seconds": 15,
    "max_control_bytes": 64 * 1024,
}

NAMING_LIMITS: dict[str, int] = {
    "max_source_bytes": 16 * 1024 * 1024,
    "max_address_bytes": 256 * 1024 * 1024,
    "max_cpu_seconds": 5,
    "wall_deadline_seconds": 10,
    "max_control_bytes": 64 * 1024,
    "max_cell_codepoints": 32768,
}

# Downstream (dataset + support) normalization shares one exact profile.
# These numeric ceilings mirror phi_engine.pipeline.support_files.
# DEFAULT_LIMITS's existing max_rows/max_columns/max_tables/
# max_cell_codepoints values verbatim (1_000_000 / 4096 / 256 / 32768) --
# duplicated here, not imported, so this module never depends on
# support_files (which will import THIS module once Approach 4 wires
# organize.py's XLS routing, and a two-way import would be circular).
DEFAULT_LIMITS: dict[str, int] = {
    "max_source_bytes": 128 * 1024 * 1024,
    "max_address_bytes": 4 * 1024**3,
    "max_cpu_seconds": 60,
    "wall_deadline_seconds": 90,
    "max_wire_frame_bytes": 1 * 1024 * 1024,
    "max_normalized_line_bytes": 1 * 1024 * 1024,
    "max_normalized_output_bytes": 512 * 1024 * 1024,
    "max_wire_output_bytes": 1 * 1024**3,
    "max_rows": 1_000_000,
    "max_columns": 4096,
    "max_tables": 256,
    "max_cell_codepoints": 32768,
}
# 1_000_000 + 2*256 + 2 = 1,000,514 -- the exact per-workbook frame ceiling.
_MAX_NORMALIZE_FRAMES = DEFAULT_LIMITS["max_rows"] + 2 * DEFAULT_LIMITS["max_tables"] + 2

# Mirrors _xls_worker.py's own private naming bounds (deliberately
# duplicated rather than imported, for the same "never import the
# minimal-import worker module for anything but run()/FdCarrier" reason
# documented in that module's own docstring).
_NAMING_MAX_SHEETS = 4
_NAMING_MAX_ROWS = 20
_NAMING_MAX_COLS = 20

_MAX_RETAINED_BUNDLES = 256


def _worker_limits(profile: dict[str, int], *, normalize: bool = False) -> dict[str, int]:
    limits: dict[str, int] = {
        "max_address_bytes": profile["max_address_bytes"],
        "max_cpu_seconds": profile["max_cpu_seconds"],
    }
    if "max_cell_codepoints" in profile:
        limits["max_cell_codepoints"] = profile["max_cell_codepoints"]
    if normalize:
        limits["max_rows"] = profile["max_rows"]
        limits["max_columns"] = profile["max_columns"]
        limits["max_tables"] = profile["max_tables"]
        limits["max_wire_frame_bytes"] = profile["max_wire_frame_bytes"]
    return limits


def _verify_source_hash(data: bytes, expected_sha256: str) -> None:
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        raise XlsIsolationError("source-mismatch")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """``json.loads`` ``object_pairs_hook``: Approach 1.5 requires the
    parent to reject a frame with a duplicate JSON object key rather
    than silently keeping the last value the way plain ``dict``
    construction would."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key in frame")
        result[key] = value
    return result


# ---------------------------------------------------------------------------
# Parent-side child lifecycle: spawn, stream input, drain reply frames --
# one selector loop under one monotonic deadline (Approach 1.2).
# ---------------------------------------------------------------------------

_WORKER_CONTEXT = multiprocessing.get_context("spawn")
_REAP_GRACE_SECONDS = 2.0
_WRITE_CHUNK_BYTES = 64 * 1024


@dataclass
class _ChildOutcome:
    started: bool = False
    exitcode: int | None = None
    frames: list[dict[str, Any]] = field(default_factory=list)
    protocol_error: bool = False
    frame_limit_exceeded: bool = False
    timed_out: bool = False


def _reap_child(process: Any, deadline: float) -> None:
    """Positively reap a started child on every path. Mirrors
    ``intake_naming._reap_pdf_worker``'s escalation exactly: an ordinary
    cooperating child gets to exit on its own within what remains of the
    deadline; only a child still alive after that is escalated through
    terminate -> join -> kill -> join. Each step is independently
    exception-guarded so a raised exception from one step never skips the
    escalation that follows it."""
    try:
        remaining = max(0.0, deadline - time.monotonic())
        process.join(remaining)
    except Exception:
        pass
    try:
        if process.is_alive():
            process.terminate()
    except Exception:
        pass
    try:
        if process.is_alive():
            process.join(_REAP_GRACE_SECONDS)
    except Exception:
        pass
    try:
        if process.is_alive():
            process.kill()
    except Exception:
        pass
    try:
        if process.is_alive():
            process.join(_REAP_GRACE_SECONDS)
    except Exception:
        pass


def _run_child(
    *,
    mode: str,
    data: bytes,
    expected_sha256: str,
    artifact_id: str | None,
    worker_limits: dict[str, int],
    deadline: float,
    recv_maxlength: int | Callable[[], int],
    terminal_types: frozenset[str],
    on_frame: Callable[[dict[str, Any], int], bool] | None = None,
    max_frames: int | None = None,
    worker_entrypoint: Callable[..., None] = _xls_worker.run,
    _target: Callable[..., None] = _xls_worker.run,
) -> _ChildOutcome:
    """``_target`` is a test-only seam (defaults to the real worker
    entry point): it lets tests drive this exact real-``multiprocessing``
    parent-side selector/decode loop against a genuine, separately
    spawned child process that sends deliberately adversarial frame
    bytes -- proving the parent's protocol validation fails closed --
    without mocking ``_run_child`` itself or any IPC primitive."""
    outcome = _ChildOutcome()
    read_fd, write_fd = os.pipe()
    os.set_blocking(write_fd, False)
    carrier = _xls_worker.FdCarrier.for_fd(read_fd)
    parent_conn, child_conn = _WORKER_CONTEXT.Pipe(duplex=False)
    process = _WORKER_CONTEXT.Process(
        target=_target,
        args=(mode, carrier, len(data), expected_sha256, artifact_id, worker_limits, child_conn),
        daemon=True,
    )
    try:
        process.start()
        outcome.started = True
    except Exception:
        outcome.started = False
    finally:
        # The parent never needs its own copy of the raw read fd (handed
        # away via DupFd during start()) or the child-write-only
        # connection endpoint -- on both the success and failure branch.
        try:
            os.close(read_fd)
        except OSError:
            pass
        try:
            child_conn.close()
        except OSError:
            pass

    if not outcome.started:
        try:
            os.close(write_fd)
        except OSError:
            pass
        try:
            parent_conn.close()
        except OSError:
            pass
        try:
            process.close()
        except Exception:
            pass
        return outcome

    write_offset = 0
    write_open = True
    terminal = False
    sel = selectors.DefaultSelector()

    def _stop_write() -> None:
        nonlocal write_open
        write_open = False
        try:
            sel.unregister(write_fd)
        except (KeyError, ValueError):
            pass
        try:
            os.close(write_fd)
        except OSError:
            pass

    def _current_maxlength() -> int:
        return recv_maxlength() if callable(recv_maxlength) else recv_maxlength

    def _drain() -> None:
        nonlocal terminal
        while parent_conn.poll(0):
            try:
                raw = parent_conn.recv_bytes(maxlength=_current_maxlength())
            except EOFError:
                terminal = True
                return
            except OSError:
                outcome.protocol_error = True
                terminal = True
                return
            try:
                payload = json.loads(raw.decode("ascii"), object_pairs_hook=_reject_duplicate_keys)
            except (UnicodeDecodeError, ValueError, RecursionError):
                outcome.protocol_error = True
                terminal = True
                return
            if not isinstance(payload, dict) or "type" not in payload:
                outcome.protocol_error = True
                terminal = True
                return
            outcome.frames.append(payload)
            if max_frames is not None and len(outcome.frames) > max_frames:
                outcome.frame_limit_exceeded = True
                terminal = True
                return
            keep_going = True
            if on_frame is not None:
                keep_going = on_frame(payload, len(raw))
            if payload.get("type") in terminal_types or not keep_going:
                terminal = True
                # Approach 1.5: any frame the child queues after its own
                # terminal frame (error/done) is protocol-invalid. A bare
                # poll(0) is not enough to detect this -- poll() also
                # reports readiness when the child has simply closed the
                # connection (the normal, successful-completion case), so
                # an actual bounded recv is required to tell "EOF" (fine)
                # apart from "a real extra frame queued" (protocol error).
                if parent_conn.poll(0):
                    try:
                        parent_conn.recv_bytes(maxlength=_current_maxlength())
                    except EOFError:
                        pass
                    except OSError:
                        outcome.protocol_error = True
                    else:
                        outcome.protocol_error = True
                return

    try:
        if len(data) > 0:
            sel.register(write_fd, selectors.EVENT_WRITE)
        else:
            _stop_write()
        sel.register(parent_conn.fileno(), selectors.EVENT_READ)
        sel.register(process.sentinel, selectors.EVENT_READ)

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                outcome.timed_out = True
                break
            events = sel.select(remaining)
            sentinel_fired = False
            for key, _mask in events:
                fd = key.fd
                if fd == write_fd and write_open:
                    try:
                        chunk = data[write_offset : write_offset + _WRITE_CHUNK_BYTES]
                        n = os.write(write_fd, chunk)
                    except (BlockingIOError, InterruptedError):
                        continue
                    except OSError:
                        _stop_write()
                        continue
                    if n == 0:
                        _stop_write()
                        continue
                    write_offset += n
                    if write_offset >= len(data):
                        _stop_write()
                elif fd == parent_conn.fileno():
                    _drain()
                elif fd == process.sentinel:
                    sentinel_fired = True
            if terminal:
                break
            if sentinel_fired:
                # The child has exited -- drain any frame it managed to
                # send before dying, once more, before giving up on it.
                _drain()
                break
    finally:
        try:
            sel.close()
        except Exception:
            pass
        _reap_child(process, deadline)
        try:
            outcome.exitcode = process.exitcode
        except Exception:
            outcome.exitcode = None
        if write_open:
            try:
                os.close(write_fd)
            except OSError:
                pass
        try:
            parent_conn.close()
        except OSError:
            pass
        try:
            process.close()
        except Exception:
            pass

    return outcome


# ---------------------------------------------------------------------------
# Exact protocol frame validation (Approach 1.5)
# ---------------------------------------------------------------------------

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^a_[0-9a-f]{32}$")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str(value: Any) -> bool:
    return isinstance(value, str)


def _valid_inspection(frame: Any, max_sheets: int) -> bool:
    return (
        isinstance(frame, dict)
        and set(frame) == {"type", "sheet_count"}
        and frame.get("type") == "inspection"
        and _is_int(frame.get("sheet_count"))
        and 0 <= frame["sheet_count"] <= max_sheets
    )


def _valid_naming(frame: Any, max_sheets: int, max_rows: int, max_cols: int, cell_limit: int) -> bool:
    if not (isinstance(frame, dict) and set(frame) == {"type", "sheets"} and frame.get("type") == "naming"):
        return False
    sheets = frame["sheets"]
    if not isinstance(sheets, list) or len(sheets) > max_sheets:
        return False
    for entry in sheets:
        if not (isinstance(entry, dict) and set(entry) == {"index", "rows"}):
            return False
        if not (_is_int(entry["index"]) and entry["index"] >= 1):
            return False
        rows = entry["rows"]
        if not isinstance(rows, list) or len(rows) > max_rows:
            return False
        for row in rows:
            if not isinstance(row, list) or len(row) > max_cols:
                return False
            for cell in row:
                if not _is_str(cell) or len(cell) > cell_limit:
                    return False
    return True


def _valid_header(entry: Any) -> bool:
    return (
        isinstance(entry, dict)
        and set(entry) == {"header_id", "column_index", "raw_name", "normalized_name"}
        and _is_str(entry["header_id"])
        and _is_int(entry["column_index"])
        and entry["column_index"] >= 0
        and _is_str(entry["raw_name"])
        and _is_str(entry["normalized_name"])
    )


def _valid_begin(frame: Any, *, aggregate: bool) -> bool:
    if not (
        isinstance(frame, dict)
        and set(frame) == {"type", "output_index", "sheet_index", "table_index", "headers"}
        and frame.get("type") == "begin"
    ):
        return False
    if not (_is_int(frame["output_index"]) and frame["output_index"] == 0):
        return False
    sheet_index = frame["sheet_index"]
    table_index = frame["table_index"]
    if aggregate:
        if sheet_index is not None or table_index is not None:
            return False
    else:
        if not (_is_int(sheet_index) and sheet_index >= 1):
            return False
        if not (_is_int(table_index) and table_index >= 0):
            return False
    headers = frame["headers"]
    if not isinstance(headers, list):
        return False
    if aggregate and headers:
        return False
    return all(_valid_header(h) for h in headers)


def _valid_dataset_row_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, str)):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    return False


def _valid_dataset_row(frame: Any, header_ids: set[str]) -> bool:
    if not (
        isinstance(frame, dict)
        and set(frame) == {"type", "output_index", "row_index", "row"}
        and frame.get("type") == "row"
    ):
        return False
    if not (_is_int(frame["output_index"]) and frame["output_index"] == 0):
        return False
    if not _is_int(frame["row_index"]):
        return False
    row = frame["row"]
    if not isinstance(row, dict) or set(row) != header_ids:
        return False
    return all(_valid_dataset_row_value(v) for v in row.values())


def _valid_support_row(frame: Any, artifact_id: str, source_sha256: str) -> bool:
    if not (
        isinstance(frame, dict)
        and set(frame) == {"type", "output_index", "row_index", "row"}
        and frame.get("type") == "row"
    ):
        return False
    if not (_is_int(frame["output_index"]) and frame["output_index"] == 0):
        return False
    if not _is_int(frame["row_index"]):
        return False
    row = frame["row"]
    if not (
        isinstance(row, dict)
        and set(row) == {"support_artifact_id", "source_sha256", "sheet_index", "table_index", "row_index", "cells"}
    ):
        return False
    if row["support_artifact_id"] != artifact_id or row["source_sha256"] != source_sha256:
        return False
    if not (_is_int(row["sheet_index"]) and row["sheet_index"] >= 0):
        return False
    if not (_is_int(row["table_index"]) and row["table_index"] >= 0):
        return False
    if not _is_int(row["row_index"]):
        return False
    cells = row["cells"]
    if not isinstance(cells, list):
        return False
    for index, cell in enumerate(cells):
        if not (isinstance(cell, dict) and set(cell) == {"column_index", "value"}):
            return False
        if cell["column_index"] != index:
            return False
        if not _is_str(cell["value"]):
            return False
    return True


def _valid_end(frame: Any) -> bool:
    return (
        isinstance(frame, dict)
        and set(frame) == {"type", "output_index", "row_count", "sha256"}
        and frame.get("type") == "end"
        and _is_int(frame["output_index"])
        and frame["output_index"] == 0
        and _is_int(frame["row_count"])
        and frame["row_count"] >= 0
        and _is_str(frame["sha256"])
        and bool(_SHA256_HEX_RE.match(frame["sha256"]))
    )


def _valid_done(frame: Any, expected_output_count: int | None = None) -> bool:
    if not (isinstance(frame, dict) and set(frame) == {"type", "output_count"} and frame.get("type") == "done"):
        return False
    if not (_is_int(frame["output_count"]) and frame["output_count"] >= 0):
        return False
    if expected_output_count is not None and frame["output_count"] != expected_output_count:
        return False
    return True


def _valid_error(frame: Any) -> bool:
    return (
        isinstance(frame, dict)
        and set(frame) == {"type", "code"}
        and frame.get("type") == "error"
        and _is_str(frame["code"])
        and frame["code"] in _CHILD_TERMINAL_CODES
    )


# ---------------------------------------------------------------------------
# Streaming normalization transcript driver
# ---------------------------------------------------------------------------


class _NormalizeCollector:
    """Drives the exact normalization protocol state machine
    (Approach 1.5) as frames stream in, writing each validated row's
    canonical JSONL bytes to a staged file incrementally rather than
    buffering the whole transcript. ``aggregate=True`` selects the
    support (single aggregate output) shape; ``aggregate=False`` selects
    the dataset (single-sheet, single-table) shape."""

    def __init__(
        self,
        *,
        aggregate: bool,
        artifact_id: str,
        source_sha256: str,
        line_limit: int,
        package_budget: XlsPackageBudget,
        max_normalized_output_bytes: int,
        max_wire_output_bytes: int,
        staging_path: Path,
    ) -> None:
        self.aggregate = aggregate
        self.artifact_id = artifact_id
        self.source_sha256 = source_sha256
        self.line_limit = line_limit
        self.package_budget = package_budget
        self.remaining_output_bytes = max_normalized_output_bytes
        self.remaining_wire_output_bytes = max_wire_output_bytes
        self.staging_path = staging_path
        self.state = "await-begin"
        self.headers: list[dict[str, Any]] = []
        self.header_ids: set[str] = set()
        self.row_count = 0
        self.expected_next_row_index = 0
        self.digest = hashlib.sha256()
        self.error: BaseException | None = None
        self.output_seen = False
        self._fh: Any = None

    def _fail(self, exc: BaseException) -> bool:
        self.error = exc
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        return False

    def debit_wire_bytes(self, raw_len: int) -> bool:
        """Approach 1.3/4.5's per-workbook ``max_wire_output_bytes``
        ceiling on cumulative wire (raw frame) bytes -- distinct from
        the package-level ``package_budget.remaining_wire_bytes``
        aggregate shared across the whole intake session. Called by
        the caller's ``on_frame`` closure alongside the package-level
        debit, before the payload is otherwise handled."""
        if raw_len > self.remaining_wire_output_bytes:
            self.remaining_wire_output_bytes = 0
            return self._fail(XlsIsolationError("resource-limit"))
        self.remaining_wire_output_bytes -= raw_len
        return True

    def handle(self, frame: dict[str, Any]) -> bool:
        ftype = frame.get("type")
        if ftype == "error":
            if not _valid_error(frame):
                return self._fail(XlsIsolationError("protocol-invalid"))
            code = frame["code"]
            if code == "source-mismatch":
                return self._fail(XlsIsolationError("source-mismatch"))
            return self._fail(XlsWorkerError(code))

        if self.state == "await-begin":
            if ftype == "done":
                if not _valid_done(frame, expected_output_count=0):
                    return self._fail(XlsIsolationError("protocol-invalid"))
                self.state = "done"
                return False
            if ftype != "begin" or not _valid_begin(frame, aggregate=self.aggregate):
                return self._fail(XlsIsolationError("protocol-invalid"))
            self.output_seen = True
            self.headers = frame["headers"]
            self.header_ids = {h["header_id"] for h in self.headers}
            try:
                self.staging_path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(self.staging_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self._fh = os.fdopen(fd, "wb")
            except OSError:
                return self._fail(XlsIsolationError("protocol-invalid"))
            self.state = "in-rows"
            return True

        if self.state == "in-rows":
            if ftype == "end":
                if not _valid_end(frame) or frame["row_count"] != self.row_count:
                    return self._fail(XlsIsolationError("protocol-invalid"))
                if frame["sha256"] != self.digest.hexdigest():
                    return self._fail(XlsIsolationError("protocol-invalid"))
                try:
                    self._fh.flush()
                    os.fsync(self._fh.fileno())
                    self._fh.close()
                except OSError:
                    return self._fail(XlsIsolationError("protocol-invalid"))
                self._fh = None
                self.state = "await-done"
                return True
            if ftype != "row":
                return self._fail(XlsIsolationError("protocol-invalid"))
            valid = (
                _valid_support_row(frame, self.artifact_id, self.source_sha256)
                if self.aggregate
                else _valid_dataset_row(frame, self.header_ids)
            )
            if not valid:
                return self._fail(XlsIsolationError("protocol-invalid"))
            if frame["row_index"] != self.expected_next_row_index:
                return self._fail(XlsIsolationError("protocol-invalid"))
            row_obj = frame["row"]
            canonical = (
                json.dumps(row_obj, sort_keys=True, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
                + b"\n"
            )
            if len(canonical) > self.line_limit:
                return self._fail(XlsIsolationError("protocol-invalid"))
            if len(canonical) > self.package_budget.remaining_canonical_bytes:
                self.package_budget.remaining_canonical_bytes = 0
                return self._fail(XlsIsolationError("package-resource-limit"))
            self.package_budget.remaining_canonical_bytes -= len(canonical)
            if len(canonical) > self.remaining_output_bytes:
                return self._fail(XlsIsolationError("retention-limit"))
            self.remaining_output_bytes -= len(canonical)
            try:
                self._fh.write(canonical)
            except OSError:
                return self._fail(XlsIsolationError("protocol-invalid"))
            self.digest.update(canonical)
            self.row_count += 1
            self.expected_next_row_index += 1
            return True

        if self.state == "await-done":
            expected = 1 if self.output_seen else 0
            if ftype != "done" or not _valid_done(frame, expected_output_count=expected):
                return self._fail(XlsIsolationError("protocol-invalid"))
            self.state = "done"
            return False

        return self._fail(XlsIsolationError("protocol-invalid"))


def _finalize_normalize_outcome(
    outcome: _ChildOutcome,
    collector: _NormalizeCollector,
    package_budget: XlsPackageBudget,
    effective_deadline: float,
    profile_deadline: float,
) -> BaseException | None:
    if not outcome.started:
        return XlsIsolationError("isolation-unavailable")
    if collector.error is not None:
        return collector.error
    if outcome.frame_limit_exceeded:
        return XlsIsolationError("resource-limit")
    if outcome.protocol_error:
        if package_budget.remaining_wire_bytes <= 0:
            return XlsIsolationError("package-resource-limit")
        return XlsIsolationError("protocol-invalid")
    if collector.state == "done":
        if outcome.exitcode != 0:
            return XlsIsolationError("resource-limit")
        return None
    # No valid terminal frame was ever reached: a crash, an OOM/CPU kill, or
    # a deadline expiry mid-transcript. Never a worker-level code here --
    # there was no valid worker error frame to report.
    if effective_deadline <= package_budget.deadline and package_budget.deadline < profile_deadline:
        return XlsIsolationError("package-resource-limit")
    return XlsIsolationError("resource-limit")


# ---------------------------------------------------------------------------
# Bundle publication / quarantine / ledger (Approach 1.7 directory grammar)
# ---------------------------------------------------------------------------


def _artifact_hex(artifact_id: str) -> str:
    if not _ARTIFACT_ID_RE.match(artifact_id):
        raise XlsIsolationError("protocol-invalid")
    return artifact_id[2:]


def _mkdir_mode(path: Path, mode: int) -> None:
    os.mkdir(path, mode)
    os.chmod(path, mode)  # explicit chmod: umask may have narrowed mkdir's mode


def _stage_dir(target_parent: Path) -> Path:
    target_parent.mkdir(parents=True, exist_ok=True)
    stage = target_parent / f".xls-stage-{secrets.token_hex(16)}"
    _mkdir_mode(stage, 0o700)
    return stage


def _ledger_bundle(path: Path) -> XlsRetainedBundle:
    nodes: list[XlsRetainedNode] = []
    top = path.lstat()
    nodes.append(
        XlsRetainedNode(
            relative_path="",
            node_type="directory",
            mode=stat.S_IMODE(top.st_mode),
            device=top.st_dev,
            inode=top.st_ino,
            size=top.st_size,
            sha256=None,
        )
    )
    for child in sorted(path.iterdir()):
        cst = child.lstat()
        if stat.S_ISREG(cst.st_mode):
            digest = hashlib.sha256(child.read_bytes()).hexdigest()
            nodes.append(
                XlsRetainedNode(
                    relative_path=child.name,
                    node_type="file",
                    mode=stat.S_IMODE(cst.st_mode),
                    device=cst.st_dev,
                    inode=cst.st_ino,
                    size=cst.st_size,
                    sha256=digest,
                )
            )
    return XlsRetainedBundle(nodes=tuple(nodes))


def _quarantine_stage(stage: Path, organized_root: Path, kind: str, package_budget: XlsPackageBudget) -> None:
    """Best-effort: move a failed stage directory into the fixed
    quarantine grammar and ledger it. A quarantine-move failure (e.g.
    ``AtomicRenameUnavailable`` at this mount) is swallowed here -- the
    caller's own typed error (already determined) is what propagates;
    this is purely an additional forensic side effect, never the
    primary failure signal."""
    if not stage.exists():
        return
    qroot = organized_root / ".xls-quarantine"
    try:
        qroot.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc)
        name = f"{kind}.{ts:%Y%m%d}T{ts:%H%M%S}{ts.microsecond:06d}Z.{secrets.token_hex(16)}"
        parent_fd = os.open(stage.parent, os.O_DIRECTORY)
        try:
            qroot_fd = os.open(qroot, os.O_DIRECTORY)
            try:
                renameat2_noreplace(parent_fd, stage.name, qroot_fd, name)
            finally:
                os.close(qroot_fd)
        finally:
            os.close(parent_fd)
    except (OSError, AtomicRenameUnavailable):
        return
    try:
        package_budget.retained_bundles.append(_ledger_bundle(qroot / name))
    except OSError:
        pass


def _publish_bundle(stage: Path, final_name: str, target_parent: Path) -> tuple[int, int]:
    target_parent.mkdir(parents=True, exist_ok=True)
    parent_fd = os.open(target_parent, os.O_DIRECTORY)
    try:
        try:
            renameat2_noreplace(parent_fd, stage.name, parent_fd, final_name)
        except FileExistsError:
            raise XlsIsolationError("output-collision") from None
        except AtomicRenameUnavailable:
            raise XlsIsolationError("isolation-unavailable") from None
    finally:
        os.close(parent_fd)
    final_path = target_parent / final_name
    st = final_path.stat()
    return st.st_dev, st.st_ino


def _reserve_bundle_slot(package_budget: XlsPackageBudget) -> None:
    if len(package_budget.retained_bundles) >= _MAX_RETAINED_BUNDLES:
        raise XlsIsolationError("retention-limit")


# ---------------------------------------------------------------------------
# Public API (Approach 1.7)
# ---------------------------------------------------------------------------


def inspect_xls(data: bytes, expected_sha256: str, *, max_sheets: int, deadline: float | None = None) -> int:
    _verify_source_hash(data, expected_sha256)
    profile_deadline = time.monotonic() + INSPECT_LIMITS["wall_deadline_seconds"]
    effective_deadline = profile_deadline if deadline is None else min(profile_deadline, deadline)
    package_bound = deadline is not None and deadline <= profile_deadline

    outcome = _run_child(
        mode="inspect",
        data=data,
        expected_sha256=expected_sha256,
        artifact_id=None,
        worker_limits=_worker_limits(INSPECT_LIMITS),
        deadline=effective_deadline,
        recv_maxlength=INSPECT_LIMITS["max_control_bytes"] + 1,
        terminal_types=frozenset({"inspection", "error"}),
    )
    if not outcome.started:
        raise XlsIsolationError("isolation-unavailable")
    if not outcome.frames:
        if outcome.timed_out:
            raise XlsIsolationError("package-resource-limit" if package_bound else "resource-limit")
        raise XlsIsolationError("resource-limit")
    if outcome.protocol_error:
        raise XlsIsolationError("protocol-invalid")

    frame = outcome.frames[-1]
    if frame.get("type") == "error":
        if not _valid_error(frame):
            raise XlsIsolationError("protocol-invalid")
        code = frame["code"]
        if code == "source-mismatch":
            raise XlsIsolationError("source-mismatch")
        raise XlsWorkerError(code)
    if not _valid_inspection(frame, max_sheets):
        raise XlsIsolationError("protocol-invalid")
    if outcome.exitcode != 0:
        raise XlsIsolationError("resource-limit")
    return frame["sheet_count"]


def extract_xls_naming(data: bytes, expected_sha256: str) -> list[tuple[int, list[list[str]]]]:
    _verify_source_hash(data, expected_sha256)
    deadline = time.monotonic() + NAMING_LIMITS["wall_deadline_seconds"]

    outcome = _run_child(
        mode="naming",
        data=data,
        expected_sha256=expected_sha256,
        artifact_id=None,
        worker_limits=_worker_limits(NAMING_LIMITS),
        deadline=deadline,
        recv_maxlength=NAMING_LIMITS["max_control_bytes"] + 1,
        terminal_types=frozenset({"naming", "error"}),
    )
    if not outcome.started:
        raise XlsIsolationError("isolation-unavailable")
    if not outcome.frames:
        raise XlsIsolationError("resource-limit")
    if outcome.protocol_error:
        raise XlsIsolationError("protocol-invalid")

    frame = outcome.frames[-1]
    if frame.get("type") == "error":
        if not _valid_error(frame):
            raise XlsIsolationError("protocol-invalid")
        code = frame["code"]
        if code == "source-mismatch":
            raise XlsIsolationError("source-mismatch")
        raise XlsWorkerError(code)
    if not _valid_naming(frame, _NAMING_MAX_SHEETS, _NAMING_MAX_ROWS, _NAMING_MAX_COLS, NAMING_LIMITS["max_cell_codepoints"]):
        raise XlsIsolationError("protocol-invalid")
    if outcome.exitcode != 0:
        raise XlsIsolationError("resource-limit")
    return [(entry["index"], entry["rows"]) for entry in frame["sheets"]]


def normalize_xls_datasets(
    *,
    data: bytes,
    expected_sha256: str,
    artifact_id: str,
    organized_root: Path,
    output_dir: Path,
    output_stem: str,
    limits: dict[str, int],
    package_budget: XlsPackageBudget,
) -> XlsDatasetPublication | None:
    _verify_source_hash(data, expected_sha256)
    hex_id = _artifact_hex(artifact_id)
    _reserve_bundle_slot(package_budget)

    profile_deadline = time.monotonic() + limits["wall_deadline_seconds"]
    effective_deadline = min(profile_deadline, package_budget.deadline)

    target_parent = Path(output_dir)
    stage = _stage_dir(target_parent)
    staging_path = stage / f"{output_stem}__sheet-001__table-000.jsonl"
    collector = _NormalizeCollector(
        aggregate=False,
        artifact_id=artifact_id,
        source_sha256=expected_sha256,
        line_limit=limits["max_normalized_line_bytes"],
        package_budget=package_budget,
        max_normalized_output_bytes=limits["max_normalized_output_bytes"],
        max_wire_output_bytes=limits["max_wire_output_bytes"],
        staging_path=staging_path,
    )

    def recv_cap() -> int:
        return min(limits["max_wire_frame_bytes"], package_budget.remaining_wire_bytes + 1)

    def on_frame(payload: dict[str, Any], raw_len: int) -> bool:
        package_budget.remaining_wire_bytes = max(0, package_budget.remaining_wire_bytes - raw_len)
        if not collector.debit_wire_bytes(raw_len):
            return False
        return collector.handle(payload)

    outcome = _run_child(
        mode="normalize_dataset",
        data=data,
        expected_sha256=expected_sha256,
        artifact_id=artifact_id,
        worker_limits=_worker_limits(limits, normalize=True),
        deadline=effective_deadline,
        recv_maxlength=recv_cap,
        terminal_types=frozenset({"error", "done"}),
        on_frame=on_frame,
        max_frames=_MAX_NORMALIZE_FRAMES,
    )

    error = _finalize_normalize_outcome(outcome, collector, package_budget, effective_deadline, profile_deadline)
    if error is not None:
        _quarantine_stage(stage, Path(organized_root), "dataset", package_budget)
        raise error

    if collector.state != "done" or not collector.output_seen:
        shutil.rmtree(stage, ignore_errors=True)
        return None

    device, inode = _publish_bundle(stage, f"a_{hex_id}", target_parent)
    sha256 = collector.digest.hexdigest() if collector.row_count else hashlib.sha256(b"").hexdigest()
    headers = tuple(
        OrganizedHeader(
            header_id=h["header_id"],
            column_index=h["column_index"],
            raw_name=h["raw_name"],
            normalized_name=h["normalized_name"],
        )
        for h in collector.headers
    )
    published = NormalizedXlsDataset(
        path=target_parent / f"a_{hex_id}" / staging_path.name,
        device=device,
        inode=inode,
        sha256=sha256,
        row_count=collector.row_count,
        headers=headers,
    )
    return XlsDatasetPublication(
        bundle_path=target_parent / f"a_{hex_id}", bundle_device=device, bundle_inode=inode, output=published
    )


_WORKER_TO_SUPPORT_FAILURE: dict[str, SupportFailureCode] = {
    "reader-unavailable": SupportFailureCode.READER_UNAVAILABLE,
    "parse-error": SupportFailureCode.PARSE_ERROR,
    "sheet-limit": SupportFailureCode.SHEET_LIMIT,
    "table-limit": SupportFailureCode.TABLE_LIMIT,
    "row-limit": SupportFailureCode.ROW_LIMIT,
    "column-limit": SupportFailureCode.COLUMN_LIMIT,
    "cell-limit": SupportFailureCode.CELL_SIZE_LIMIT,
    "expanded-limit": SupportFailureCode.EXPANDED_SIZE_LIMIT,
    "resource-limit": SupportFailureCode.RESOURCE_LIMIT,
}


def normalize_xls_support(
    *,
    data: bytes,
    expected_sha256: str,
    artifact_id: str,
    organized_root: Path,
    output_dir: Path,
    limits: dict[str, int],
    package_budget: XlsPackageBudget,
) -> XlsSupportResult:
    _verify_source_hash(data, expected_sha256)
    hex_id = _artifact_hex(artifact_id)
    _reserve_bundle_slot(package_budget)

    profile_deadline = time.monotonic() + limits["wall_deadline_seconds"]
    effective_deadline = min(profile_deadline, package_budget.deadline)

    target_parent = Path(output_dir)
    stage = _stage_dir(target_parent)
    staging_path = stage / "rows.jsonl"
    collector = _NormalizeCollector(
        aggregate=True,
        artifact_id=artifact_id,
        source_sha256=expected_sha256,
        line_limit=limits["max_normalized_line_bytes"],
        package_budget=package_budget,
        max_normalized_output_bytes=limits["max_normalized_output_bytes"],
        max_wire_output_bytes=limits["max_wire_output_bytes"],
        staging_path=staging_path,
    )

    def recv_cap() -> int:
        return min(limits["max_wire_frame_bytes"], package_budget.remaining_wire_bytes + 1)

    def on_frame(payload: dict[str, Any], raw_len: int) -> bool:
        package_budget.remaining_wire_bytes = max(0, package_budget.remaining_wire_bytes - raw_len)
        if not collector.debit_wire_bytes(raw_len):
            return False
        return collector.handle(payload)

    outcome = _run_child(
        mode="normalize_support",
        data=data,
        expected_sha256=expected_sha256,
        artifact_id=artifact_id,
        worker_limits=_worker_limits(limits, normalize=True),
        deadline=effective_deadline,
        recv_maxlength=recv_cap,
        terminal_types=frozenset({"error", "done"}),
        on_frame=on_frame,
        max_frames=_MAX_NORMALIZE_FRAMES,
    )

    error = _finalize_normalize_outcome(outcome, collector, package_budget, effective_deadline, profile_deadline)
    if isinstance(error, XlsIsolationError):
        _quarantine_stage(stage, Path(organized_root), "support", package_budget)
        raise error
    if isinstance(error, XlsWorkerError):
        _quarantine_stage(stage, Path(organized_root), "support", package_budget)
        failed = ParsedSupportArtifact(
            artifact_id=artifact_id,
            source_sha256=expected_sha256,
            kind=DependencyKind.DICTIONARY_MAPPING,
            format="xls",
            parse_status=SupportParseStatus.FAILED,
            normalized_rows_path=None,
            normalized_rows_sha256=None,
            failure_code=_WORKER_TO_SUPPORT_FAILURE[error.code],
        )
        return XlsSupportResult(artifact=failed, publication=None)

    if collector.state != "done" or not collector.output_seen:
        _quarantine_stage(stage, Path(organized_root), "support", package_budget)
        raise XlsIsolationError("protocol-invalid")

    device, inode = _publish_bundle(stage, f"a_{hex_id}", target_parent)
    sha256 = collector.digest.hexdigest() if collector.row_count else hashlib.sha256(b"").hexdigest()
    published = XlsPublishedFile(
        path=target_parent / f"a_{hex_id}" / staging_path.name,
        device=device,
        inode=inode,
        sha256=sha256,
        row_count=collector.row_count,
    )
    artifact = ParsedSupportArtifact(
        artifact_id=artifact_id,
        source_sha256=expected_sha256,
        kind=DependencyKind.DICTIONARY_MAPPING,
        format="xls",
        parse_status=SupportParseStatus.PARSED,
        normalized_rows_path=published.path,
        normalized_rows_sha256=sha256,
        failure_code=None,
    )
    return XlsSupportResult(
        artifact=artifact,
        publication=XlsSupportPublication(
            bundle_path=target_parent / f"a_{hex_id}", bundle_device=device, bundle_inode=inode, output=published
        ),
    )
