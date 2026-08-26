"""Real-process tests for the bounded legacy ``.xls`` isolation boundary
(``phi_engine.pipeline.xls_isolation`` + ``phi_engine.pipeline._xls_worker``).

Every test that exercises a mode (``inspect``/``naming``/
``normalize_dataset``/``normalize_support``) does so through a genuine
``multiprocessing`` ``spawn`` child running the real worker module --
nothing here mocks ``multiprocessing``, ``Process``, or the worker.
Fixture ``.xls`` bytes are authored with real ``xlwt`` and read back
through the real ``xlrd``-backed worker.
"""

from __future__ import annotations

import datetime
import hashlib
import io
import json
import os
import time
from pathlib import Path

import pytest
import xlwt

from phi_engine.pipeline import xls_isolation as iso
from phi_engine.pipeline.dependencies import DependencyKind, SupportFailureCode, SupportParseStatus
from phi_engine.utils.atomic_fs import AtomicRenameUnavailable


# ---------------------------------------------------------------------------
# .xls fixture construction (genuine xlwt bytes)
# ---------------------------------------------------------------------------


def _xls_bytes(sheets: list[tuple[str, list[list[object]]]]) -> bytes:
    """Build a real BIFF (.xls) file. ``sheets`` is a list of
    ``(name, rows)``; ``None`` cells are left unwritten (genuinely
    blank in the underlying BIFF stream)."""
    wb = xlwt.Workbook()
    for name, rows in sheets:
        ws = wb.add_sheet(name)
        for r, row in enumerate(rows):
            for c, value in enumerate(row):
                if value is None:
                    continue
                ws.write(r, c, value)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xls_bytes_with_error_cell(header: list[str], rows: list[list[object]], error_at: tuple[int, int]) -> bytes:
    """One-sheet workbook where cell ``error_at`` (0-based, relative to
    the header row) is a genuine BIFF error-type cell -- xlrd reports
    ``XL_CELL_ERROR`` for it, which the worker converts to ``float('nan')``."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("S1")
    for c, value in enumerate(header):
        ws.write(0, c, value)
    err_r, err_c = error_at
    for r, row in enumerate(rows, start=1):
        for c, value in enumerate(row):
            if value is None or (r, c) == (err_r, err_c):
                continue
            ws.write(r, c, value)
    ws.row(err_r).set_cell_error(err_c, "#DIV/0!")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xls_bytes_with_date_cell() -> bytes:
    """One-sheet workbook whose second column is a genuine BIFF
    date-formatted cell -- xlrd reports ``XL_CELL_DATE`` for it, which
    the worker converts to a real ``datetime.datetime`` (see
    ``_xlrd_cell_value``). Constructing the DataFrame from a column of
    Python ``datetime`` objects makes pandas box each value as a
    ``pandas.Timestamp`` once read back via ``to_numpy().tolist()`` --
    a type ``json.JSONEncoder`` cannot serialize without ``default=str``."""
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Sheet1")
    date_style = xlwt.XFStyle()
    date_style.num_format_str = "YYYY-MM-DD"
    ws.write(0, 0, "SUBJID")
    ws.write(0, 1, "VISITDATE")
    ws.write(1, 0, 1)
    ws.write(1, 1, datetime.date(2024, 1, 15), date_style)
    ws.write(2, 0, 2)
    ws.write(2, 1, datetime.date(2024, 2, 20), date_style)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _artifact_id(seed: str) -> str:
    return "a_" + hashlib.sha256(seed.encode()).hexdigest()[:32]


def _budget(*, canonical: int = 512 * 1024 * 1024, wire: int = 1024 * 1024 * 1024, deadline: float | None = None) -> iso.XlsPackageBudget:
    import time

    return iso.XlsPackageBudget(
        remaining_canonical_bytes=canonical,
        remaining_wire_bytes=wire,
        deadline=time.monotonic() + 300 if deadline is None else deadline,
    )


_DATASET_XLS = _xls_bytes([("Sheet1", [["SUBJID", "AGE"], [1, 40], [2, 55]])])
_DATE_XLS = _xls_bytes_with_date_cell()
_EMPTY_XLS = _xls_bytes([("Sheet1", [])])
_TWO_SHEET_XLS = _xls_bytes([("Sheet1", [["A", "B"], [1, 2]]), ("Sheet2", [["C"], [3]])])
_TWO_TABLE_XLS = _xls_bytes(
    [("Sheet1", [["A", "B"], [1, 2], [None, None], ["C", "D"], [3, 4]])]
)
_SUPPORT_XLS = _xls_bytes(
    [
        ("Dict", [["variable", "label"], ["AGE", "Age in years"], ["SEX", "Sex"]]),
        ("Codes", [["code", "meaning"], ["M", "Male"], [None, None], ["header2"], ["F", "Female"]]),
    ]
)
_SUPPORT_HEADER_ONLY_XLS = _xls_bytes([("Dict", [["variable", "label"]])])
_NOT_AN_XLS = b"this is not a valid BIFF/xls file at all, just bytes" * 4


# ---------------------------------------------------------------------------
# inspect_xls
# ---------------------------------------------------------------------------


def test_inspect_xls_reports_sheet_count_via_real_child() -> None:
    count = iso.inspect_xls(_DATASET_XLS, _sha(_DATASET_XLS), max_sheets=64)
    assert count == 1


def test_inspect_xls_reports_multi_sheet_count() -> None:
    count = iso.inspect_xls(_TWO_SHEET_XLS, _sha(_TWO_SHEET_XLS), max_sheets=64)
    assert count == 2


def test_inspect_xls_rejects_source_mismatch_before_spawn() -> None:
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.inspect_xls(_DATASET_XLS, "0" * 64, max_sheets=64)
    assert excinfo.value.code == "source-mismatch"


def test_inspect_xls_malformed_workbook_is_worker_parse_error() -> None:
    with pytest.raises(iso.XlsWorkerError) as excinfo:
        iso.inspect_xls(_NOT_AN_XLS, _sha(_NOT_AN_XLS), max_sheets=64)
    assert excinfo.value.code == "parse-error"


def test_inspect_xls_sheet_count_exceeding_max_sheets_is_protocol_invalid() -> None:
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.inspect_xls(_TWO_SHEET_XLS, _sha(_TWO_SHEET_XLS), max_sheets=1)
    assert excinfo.value.code == "protocol-invalid"


def test_inspect_xls_no_stderr_traceback_on_worker_failure(capfd: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(iso.XlsWorkerError):
        iso.inspect_xls(_NOT_AN_XLS, _sha(_NOT_AN_XLS), max_sheets=64)
    captured = capfd.readouterr()
    assert "Traceback" not in captured.out
    assert "Traceback" not in captured.err


# ---------------------------------------------------------------------------
# extract_xls_naming
# ---------------------------------------------------------------------------


def test_extract_xls_naming_reads_real_rows() -> None:
    sheets = iso.extract_xls_naming(_DATASET_XLS, _sha(_DATASET_XLS))
    assert sheets == [(1, [["SUBJID", "AGE"], ["1", "40"], ["2", "55"]])]


def test_extract_xls_naming_bounds_sheets_rows_and_columns() -> None:
    many_rows = [["h0", "h1"]] + [[str(i), str(i + 1)] for i in range(30)]
    wide = [["c" + str(i) for i in range(30)]] + [[str(i) for i in range(30)]]
    data = _xls_bytes(
        [
            ("A", many_rows),
            ("B", wide),
            ("C", [["x"]]),
            ("D", [["y"]]),
            ("E", [["z"]]),  # 5th sheet -- naming caps at 4
        ]
    )
    sheets = iso.extract_xls_naming(data, _sha(data))
    assert len(sheets) == 4
    assert [index for index, _rows in sheets] == [1, 2, 3, 4]
    _index, rows_a = sheets[0]
    assert len(rows_a) == 20  # capped at 20 rows
    _index, rows_b = sheets[1]
    assert all(len(row) == 20 for row in rows_b)  # capped at 20 columns


def test_extract_xls_naming_source_mismatch() -> None:
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.extract_xls_naming(_DATASET_XLS, "f" * 64)
    assert excinfo.value.code == "source-mismatch"


def test_extract_xls_naming_malformed_workbook_is_worker_parse_error() -> None:
    with pytest.raises(iso.XlsWorkerError) as excinfo:
        iso.extract_xls_naming(_NOT_AN_XLS, _sha(_NOT_AN_XLS))
    assert excinfo.value.code == "parse-error"


def test_extract_xls_naming_empty_sheet_reads_zero_rows() -> None:
    sheets = iso.extract_xls_naming(_EMPTY_XLS, _sha(_EMPTY_XLS))
    assert sheets == [(1, [])]


# ---------------------------------------------------------------------------
# normalize_xls_datasets
# ---------------------------------------------------------------------------


def test_normalize_xls_datasets_success_publishes_bundle(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    output_dir = organized_root / "datasets"
    budget = _budget()
    artifact_id = _artifact_id("dataset-1")

    publication = iso.normalize_xls_datasets(
        data=_DATASET_XLS,
        expected_sha256=_sha(_DATASET_XLS),
        artifact_id=artifact_id,
        organized_root=organized_root,
        output_dir=output_dir,
        output_stem="labs",
        limits=dict(iso.DEFAULT_LIMITS),
        package_budget=budget,
    )

    assert publication is not None
    assert publication.output.row_count == 2
    assert publication.output.headers[0].raw_name == "SUBJID"
    assert publication.output.headers[1].raw_name == "AGE"
    assert publication.bundle_path == output_dir / artifact_id
    assert publication.bundle_path.is_dir()
    assert oct(publication.bundle_path.stat().st_mode & 0o777) == "0o700"

    out_file = publication.output.path
    assert out_file.is_file()
    assert oct(out_file.stat().st_mode & 0o777) == "0o600"
    lines = out_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2

    subjid_id = publication.output.headers[0].header_id
    age_id = publication.output.headers[1].header_id
    row0 = json.loads(lines[0])
    row1 = json.loads(lines[1])
    assert {row0[subjid_id], row1[subjid_id]} == {1, 2}
    assert {row0[age_id], row1[age_id]} == {40, 55}

    digest = hashlib.sha256()
    for line in lines:
        digest.update((line + "\n").encode("utf-8"))
    assert digest.hexdigest() == publication.output.sha256

    # No stage directory was left behind.
    assert not any(p.name.startswith(".xls-stage-") for p in output_dir.iterdir())

    # Budget was debited (never refunded on success either -- it is spent).
    assert budget.remaining_canonical_bytes < iso.DEFAULT_LIMITS["max_normalized_output_bytes"] + 1
    assert budget.remaining_wire_bytes < 1024 * 1024 * 1024


def test_normalize_xls_datasets_source_mismatch_before_spawn(tmp_path: Path) -> None:
    budget = _budget()
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256="a" * 64,
            artifact_id=_artifact_id("mismatch"),
            organized_root=tmp_path / "organized",
            output_dir=tmp_path / "organized" / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "source-mismatch"
    assert not (tmp_path / "organized").exists()


def test_normalize_xls_datasets_multi_sheet_is_worker_sheet_limit_defense_in_depth(tmp_path: Path) -> None:
    """Direct defense-in-depth coverage: even without preflight's
    inspect-based single-sheet gate, the worker itself must refuse a
    multi-sheet dataset workbook rather than silently normalizing only
    the first sheet."""
    organized_root = tmp_path / "organized"
    budget = _budget()
    with pytest.raises(iso.XlsWorkerError) as excinfo:
        iso.normalize_xls_datasets(
            data=_TWO_SHEET_XLS,
            expected_sha256=_sha(_TWO_SHEET_XLS),
            artifact_id=_artifact_id("two-sheet"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "sheet-limit"
    # Failure must be quarantined, never left as a bare stage directory,
    # and never published as a bundle.
    assert not (organized_root / "datasets" / _artifact_id("two-sheet")).exists()
    quarantine_root = organized_root / ".xls-quarantine"
    assert quarantine_root.is_dir()
    entries = list(quarantine_root.iterdir())
    assert len(entries) == 1
    assert entries[0].name.startswith("dataset.")
    assert len(budget.retained_bundles) == 1


def test_normalize_xls_datasets_two_tables_is_table_limit(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget()
    with pytest.raises(iso.XlsWorkerError) as excinfo:
        iso.normalize_xls_datasets(
            data=_TWO_TABLE_XLS,
            expected_sha256=_sha(_TWO_TABLE_XLS),
            artifact_id=_artifact_id("two-table"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "table-limit"


def test_normalize_xls_datasets_blank_sheet_returns_none(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget()
    publication = iso.normalize_xls_datasets(
        data=_EMPTY_XLS,
        expected_sha256=_sha(_EMPTY_XLS),
        artifact_id=_artifact_id("empty"),
        organized_root=organized_root,
        output_dir=organized_root / "datasets",
        output_stem="labs",
        limits=dict(iso.DEFAULT_LIMITS),
        package_budget=budget,
    )
    assert publication is None
    datasets_dir = organized_root / "datasets"
    assert not datasets_dir.exists() or list(datasets_dir.iterdir()) == []


def test_normalize_xls_datasets_non_finite_cell_is_worker_parse_error(tmp_path: Path) -> None:
    data = _xls_bytes_with_error_cell(["SUBJID", "AGE"], [[1, 40], [2, 55]], error_at=(1, 1))
    organized_root = tmp_path / "organized"
    budget = _budget()
    with pytest.raises(iso.XlsWorkerError) as excinfo:
        iso.normalize_xls_datasets(
            data=data,
            expected_sha256=_sha(data),
            artifact_id=_artifact_id("nonfinite"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "parse-error"


def test_normalize_xls_datasets_date_cell_normalizes_successfully(tmp_path: Path) -> None:
    """Finding 1 (CRITICAL): a genuine xlrd date cell converts to a
    ``pandas.Timestamp`` when the worker builds its DataFrame. Before
    ``_encode_bounded`` gained ``default=str`` (matching the canonical-
    hash encoder's existing convention), ``json.JSONEncoder.iterencode``
    raised ``TypeError`` on that ``Timestamp``, ``_encode_bounded``
    returned ``None``, and the row frame silently failed to send --
    surfacing as a misleading ``expanded-limit`` for any date column,
    never a real size overflow."""
    organized_root = tmp_path / "organized"
    budget = _budget()
    publication = iso.normalize_xls_datasets(
        data=_DATE_XLS,
        expected_sha256=_sha(_DATE_XLS),
        artifact_id=_artifact_id("date-cell"),
        organized_root=organized_root,
        output_dir=organized_root / "datasets",
        output_stem="visits",
        limits=dict(iso.DEFAULT_LIMITS),
        package_budget=budget,
    )

    assert publication is not None
    assert publication.output.row_count == 2
    subjid_id = publication.output.headers[0].header_id
    date_id = publication.output.headers[1].header_id

    lines = publication.output.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    row0 = json.loads(lines[0])
    row1 = json.loads(lines[1])
    assert {row0[subjid_id], row1[subjid_id]} == {1, 2}
    assert {row0[date_id], row1[date_id]} == {
        str(datetime.datetime(2024, 1, 15)),
        str(datetime.datetime(2024, 2, 20)),
    }

    # Worker-computed canonical hash (sent in the "end" frame and used to
    # publish) must agree with the parent's own re-derived digest over the
    # exact bytes actually written -- proving worker/parent canonical
    # SHA-256 agreement survived the fix.
    digest = hashlib.sha256()
    for line in lines:
        digest.update((line + "\n").encode("utf-8"))
    assert digest.hexdigest() == publication.output.sha256


def test_normalize_xls_datasets_package_deadline_is_package_resource_limit(tmp_path: Path) -> None:
    import time

    organized_root = tmp_path / "organized"
    budget = _budget(deadline=time.monotonic() - 1.0)  # already expired
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256=_sha(_DATASET_XLS),
            artifact_id=_artifact_id("deadline"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "package-resource-limit"


def test_normalize_xls_datasets_wall_deadline_is_resource_limit(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget()
    limits = dict(iso.DEFAULT_LIMITS)
    limits["wall_deadline_seconds"] = 0.001  # too small for any real spawn to finish
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256=_sha(_DATASET_XLS),
            artifact_id=_artifact_id("wall-deadline"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=limits,
            package_budget=budget,
        )
    assert excinfo.value.code == "resource-limit"


def test_normalize_xls_datasets_retention_limit_before_spawn(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget()
    budget.retained_bundles.extend(iso.XlsRetainedBundle(nodes=()) for _ in range(iso._MAX_RETAINED_BUNDLES))
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256=_sha(_DATASET_XLS),
            artifact_id=_artifact_id("retention"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "retention-limit"
    # Nothing was ever staged for this call.
    assert not (organized_root / "datasets").exists()

def test_normalize_xls_datasets_wire_output_bytes_ceiling_is_resource_limit(tmp_path: Path) -> None:
    """Important finding: ``max_wire_output_bytes`` (Approach 1.3/4.5's
    per-workbook cumulative wire-bytes ceiling, DEFAULT_LIMITS) must
    actually be enforced -- previously dead configuration, so a single
    workbook could otherwise consume the entire package-level
    remaining_wire_bytes aggregate (up to 8 GiB) rather than the
    documented per-workbook ceiling."""
    organized_root = tmp_path / "organized"
    budget = _budget()  # generous package-level wire budget -- not the limit under test
    limits = dict(iso.DEFAULT_LIMITS)
    limits["max_wire_output_bytes"] = 4  # far too small for even the begin frame's raw bytes
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256=_sha(_DATASET_XLS),
            artifact_id=_artifact_id("wire-ceiling"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=limits,
            package_budget=budget,
        )
    assert excinfo.value.code == "resource-limit"
    # Only the tiny amount that actually crossed the wire before the
    # per-workbook ceiling tripped was debited -- the failure is driven
    # by the per-workbook ceiling, not by draining the package aggregate.
    assert budget.remaining_wire_bytes > 1024 * 1024 * 1024 - 4096



def test_normalize_xls_datasets_output_collision_is_isolation_error(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    output_dir = organized_root / "datasets"
    artifact_id = _artifact_id("collision")
    output_dir.mkdir(parents=True)
    (output_dir / artifact_id).mkdir(mode=0o700)  # occupy the final name in advance

    budget = _budget()
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256=_sha(_DATASET_XLS),
            artifact_id=artifact_id,
            organized_root=organized_root,
            output_dir=output_dir,
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "output-collision"


def test_normalize_xls_datasets_canonical_budget_exhaustion_is_package_resource_limit(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget(canonical=1)  # far too small for even one row's canonical bytes
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256=_sha(_DATASET_XLS),
            artifact_id=_artifact_id("canon-budget"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "package-resource-limit"
    assert budget.remaining_canonical_bytes == 0  # debited to zero, never refunded


# ---------------------------------------------------------------------------
# Frame validators (finding: no direct extra-key/missing-key/wrong-type
# coverage existed for _valid_* against hand-built malformed dicts -- only
# well-behaved real workers were ever exercised).
# ---------------------------------------------------------------------------


_VALID_HEADER = {"header_id": "h_abc", "column_index": 0, "raw_name": "AGE", "normalized_name": "age"}
_VALID_BEGIN_DATASET = {
    "type": "begin",
    "output_index": 0,
    "sheet_index": 1,
    "table_index": 0,
    "headers": [_VALID_HEADER],
}
_VALID_ROW_DATASET = {"type": "row", "output_index": 0, "row_index": 0, "row": {"h_abc": 40}}
_VALID_SUPPORT_ROW = {
    "support_artifact_id": "a_x",
    "source_sha256": "s",
    "sheet_index": 0,
    "table_index": 0,
    "row_index": 0,
    "cells": [{"column_index": 0, "value": "v"}],
}
_VALID_END = {"type": "end", "output_index": 0, "row_count": 1, "sha256": "0" * 64}
_VALID_DONE = {"type": "done", "output_count": 1}
_VALID_ERROR = {"type": "error", "code": "parse-error"}


def test_valid_header_rejects_extra_missing_and_wrong_type_fields() -> None:
    assert iso._valid_header(_VALID_HEADER) is True
    assert iso._valid_header({**_VALID_HEADER, "extra": 1}) is False  # extra key
    assert iso._valid_header({k: v for k, v in _VALID_HEADER.items() if k != "raw_name"}) is False  # missing key
    assert iso._valid_header({**_VALID_HEADER, "column_index": "0"}) is False  # wrong type
    assert iso._valid_header({**_VALID_HEADER, "column_index": -1}) is False  # out of range
    assert iso._valid_header("not-a-dict") is False


def test_valid_begin_rejects_extra_missing_wrong_type_and_aggregate_mismatch() -> None:
    assert iso._valid_begin(_VALID_BEGIN_DATASET, aggregate=False) is True
    assert iso._valid_begin({**_VALID_BEGIN_DATASET, "extra": 1}, aggregate=False) is False  # extra key
    assert iso._valid_begin(
        {k: v for k, v in _VALID_BEGIN_DATASET.items() if k != "headers"}, aggregate=False
    ) is False  # missing key
    assert iso._valid_begin({**_VALID_BEGIN_DATASET, "headers": "not-a-list"}, aggregate=False) is False
    assert iso._valid_begin({**_VALID_BEGIN_DATASET, "sheet_index": 0}, aggregate=False) is False  # 1-indexed
    # Dataset shape (non-None sheet/table indices) rejected under aggregate=True.
    assert iso._valid_begin(_VALID_BEGIN_DATASET, aggregate=True) is False
    aggregate_begin = {"type": "begin", "output_index": 0, "sheet_index": None, "table_index": None, "headers": []}
    assert iso._valid_begin(aggregate_begin, aggregate=True) is True
    # Aggregate shape must never carry headers.
    assert iso._valid_begin({**aggregate_begin, "headers": [_VALID_HEADER]}, aggregate=True) is False
    assert iso._valid_begin({**_VALID_BEGIN_DATASET, "type": "row"}, aggregate=False) is False


def test_valid_dataset_row_rejects_extra_missing_wrong_type_and_header_mismatch() -> None:
    header_ids = {"h_abc"}
    assert iso._valid_dataset_row(_VALID_ROW_DATASET, header_ids) is True
    assert iso._valid_dataset_row({**_VALID_ROW_DATASET, "extra": 1}, header_ids) is False  # extra key
    assert iso._valid_dataset_row(
        {k: v for k, v in _VALID_ROW_DATASET.items() if k != "row_index"}, header_ids
    ) is False  # missing key
    assert iso._valid_dataset_row({**_VALID_ROW_DATASET, "row_index": "0"}, header_ids) is False  # wrong type
    assert iso._valid_dataset_row({**_VALID_ROW_DATASET, "row": {"h_other": 1}}, header_ids) is False  # unknown col
    assert iso._valid_dataset_row({**_VALID_ROW_DATASET, "row": {}}, header_ids) is False  # missing col
    assert iso._valid_dataset_row({**_VALID_ROW_DATASET, "row": []}, header_ids) is False  # wrong row type


def test_valid_support_row_rejects_extra_missing_wrong_type_and_identity_mismatch() -> None:
    frame = {"type": "row", "output_index": 0, "row_index": 0, "row": _VALID_SUPPORT_ROW}
    assert iso._valid_support_row(frame, "a_x", "s") is True
    assert iso._valid_support_row({**frame, "extra": 1}, "a_x", "s") is False  # extra top-level key
    bad_row = {**_VALID_SUPPORT_ROW, "extra": 1}
    assert iso._valid_support_row({**frame, "row": bad_row}, "a_x", "s") is False  # extra nested key
    missing_row = {k: v for k, v in _VALID_SUPPORT_ROW.items() if k != "cells"}
    assert iso._valid_support_row({**frame, "row": missing_row}, "a_x", "s") is False  # missing nested key
    assert iso._valid_support_row(frame, "a_wrong", "s") is False  # artifact identity mismatch
    assert iso._valid_support_row(frame, "a_x", "wrong") is False  # source hash mismatch
    bad_cell_order = {**_VALID_SUPPORT_ROW, "cells": [{"column_index": 1, "value": "v"}]}
    assert iso._valid_support_row({**frame, "row": bad_cell_order}, "a_x", "s") is False  # cell out of order
    bad_cell_type = {**_VALID_SUPPORT_ROW, "cells": [{"column_index": 0, "value": 1}]}
    assert iso._valid_support_row({**frame, "row": bad_cell_type}, "a_x", "s") is False  # wrong cell value type


def test_valid_end_rejects_extra_missing_wrong_type_and_bad_hash_shape() -> None:
    assert iso._valid_end(_VALID_END) is True
    assert iso._valid_end({**_VALID_END, "extra": 1}) is False  # extra key
    assert iso._valid_end({k: v for k, v in _VALID_END.items() if k != "sha256"}) is False  # missing key
    assert iso._valid_end({**_VALID_END, "row_count": "1"}) is False  # wrong type
    assert iso._valid_end({**_VALID_END, "row_count": -1}) is False  # out of range
    assert iso._valid_end({**_VALID_END, "sha256": "not-hex"}) is False  # malformed hash shape


def test_valid_done_rejects_extra_missing_wrong_type_and_expected_count_mismatch() -> None:
    assert iso._valid_done(_VALID_DONE) is True
    assert iso._valid_done({**_VALID_DONE, "extra": 1}) is False  # extra key
    assert iso._valid_done({}) is False  # missing key
    assert iso._valid_done({**_VALID_DONE, "output_count": "1"}) is False  # wrong type
    assert iso._valid_done({**_VALID_DONE, "output_count": -1}) is False  # out of range
    assert iso._valid_done(_VALID_DONE, expected_output_count=0) is False  # expected-count mismatch
    assert iso._valid_done(_VALID_DONE, expected_output_count=1) is True


def test_valid_error_rejects_extra_missing_wrong_type_and_unknown_code() -> None:
    assert iso._valid_error(_VALID_ERROR) is True
    assert iso._valid_error({**_VALID_ERROR, "extra": 1}) is False  # extra key
    assert iso._valid_error({"type": "error"}) is False  # missing key
    assert iso._valid_error({**_VALID_ERROR, "code": 1}) is False  # wrong type
    assert iso._valid_error({**_VALID_ERROR, "code": "not-a-known-code"}) is False  # unknown code


def test_valid_dataset_row_value_rejects_non_finite_floats() -> None:
    """Finding 2 (CRITICAL) unit coverage: ``json.loads`` parses the
    bare literals ``NaN``/``Infinity``/``-Infinity`` by default, so the
    row-value validator must explicitly reject them via
    ``math.isfinite`` rather than accepting any ``float``."""
    assert iso._valid_dataset_row_value(float("nan")) is False
    assert iso._valid_dataset_row_value(float("inf")) is False
    assert iso._valid_dataset_row_value(float("-inf")) is False
    assert iso._valid_dataset_row_value(1.5) is True
    assert iso._valid_dataset_row_value(0.0) is True
    assert iso._valid_dataset_row_value(None) is True
    assert iso._valid_dataset_row_value("x") is True
    assert iso._valid_dataset_row_value(True) is True
    assert iso._valid_dataset_row_value(3) is True


def test_normalize_collector_rejects_non_finite_dataset_row_value(tmp_path: Path) -> None:
    """Finding 2 (CRITICAL): a NaN/Infinity dataset row value must be
    rejected as a typed protocol error by ``_NormalizeCollector.handle``
    -- never let ``json.dumps(..., allow_nan=False)`` raise an uncaught
    ``ValueError`` while computing the canonical hash."""
    collector = iso._NormalizeCollector(
        aggregate=False,
        artifact_id=_artifact_id("nan-row"),
        source_sha256=_sha(b"x"),
        line_limit=1024,
        package_budget=_budget(),
        max_normalized_output_bytes=1024,
        max_wire_output_bytes=1024,
        staging_path=tmp_path / "nan-row.jsonl",
    )
    begin_frame = {
        "type": "begin",
        "output_index": 0,
        "sheet_index": 1,
        "table_index": 0,
        "headers": [
            {"header_id": "h_abc", "column_index": 0, "raw_name": "AGE", "normalized_name": "age"}
        ],
    }
    assert collector.handle(begin_frame) is True

    row_frame = {"type": "row", "output_index": 0, "row_index": 0, "row": {"h_abc": float("nan")}}
    # Must not raise -- must be rejected as a typed protocol error.
    result = collector.handle(row_frame)
    assert result is False
    assert isinstance(collector.error, iso.XlsIsolationError)
    assert collector.error.code == "protocol-invalid"


# ---------------------------------------------------------------------------
# Adversarial stub children (findings 3/4/5): module-level (picklable under
# spawn) so they can stand in for _xls_worker.run via _run_child's test-only
# `_target` seam, driving the REAL parent-side selector/decode loop over a
# genuinely separate spawned process against deliberately malformed frame
# bytes -- nothing here mocks _run_child, multiprocessing, or Connection.
# ---------------------------------------------------------------------------


def _stub_recursion_bomb(mode, input_read_fd, input_size, expected_sha256, artifact_id, limits, conn):
    try:
        input_read_fd.detach()
    except Exception:
        pass
    raw = ("[" * 10000).encode("ascii")
    try:
        conn.send_bytes(raw)
    except Exception:
        pass
    conn.close()


def _stub_duplicate_key(mode, input_read_fd, input_size, expected_sha256, artifact_id, limits, conn):
    try:
        input_read_fd.detach()
    except Exception:
        pass
    raw = b'{"type":"inspection","sheet_count":1,"sheet_count":2}'
    try:
        conn.send_bytes(raw)
    except Exception:
        pass
    conn.close()


def _stub_post_terminal_frame(mode, input_read_fd, input_size, expected_sha256, artifact_id, limits, conn):
    try:
        input_read_fd.detach()
    except Exception:
        pass
    try:
        conn.send_bytes(b'{"type":"inspection","sheet_count":1}')
        conn.send_bytes(b'{"type":"inspection","sheet_count":2}')
    except Exception:
        pass
    conn.close()


def _stub_clean_terminal_frame(mode, input_read_fd, input_size, expected_sha256, artifact_id, limits, conn):
    try:
        input_read_fd.detach()
    except Exception:
        pass
    try:
        conn.send_bytes(b'{"type":"inspection","sheet_count":1}')
    except Exception:
        pass
    conn.close()


def _run_inspect_child(target) -> "iso._ChildOutcome":
    return iso._run_child(
        mode="inspect",
        data=_DATASET_XLS,
        expected_sha256=_sha(_DATASET_XLS),
        artifact_id=None,
        worker_limits=iso._worker_limits(iso.INSPECT_LIMITS),
        deadline=time.monotonic() + 5.0,
        recv_maxlength=iso.INSPECT_LIMITS["max_control_bytes"] + 1,
        terminal_types=frozenset({"inspection", "error"}),
        _target=target,
    )


def test_run_child_rejects_deeply_nested_json_as_protocol_error() -> None:
    """Finding 3 (CRITICAL): deeply nested JSON must never raise an
    uncaught RecursionError out of the parent's frame decode -- it must
    fail closed as a typed protocol error instead of crashing the
    caller with a raw stdlib exception."""
    outcome = _run_inspect_child(_stub_recursion_bomb)
    assert outcome.started is True
    assert outcome.protocol_error is True


def test_run_child_rejects_duplicate_json_keys_as_protocol_error() -> None:
    """Finding 4 (IMPORTANT): a frame with a duplicate JSON object key
    must be rejected -- never silently resolved to the last value the
    way plain ``dict`` construction would."""
    outcome = _run_inspect_child(_stub_duplicate_key)
    assert outcome.started is True
    assert outcome.protocol_error is True


def test_run_child_rejects_frame_sent_after_terminal_frame() -> None:
    """Finding 5 (IMPORTANT): any frame the child queues after its own
    terminal (inspection/error/done) frame is protocol-invalid -- the
    parent must not silently discard the extra frame and report a
    clean success."""
    outcome = _run_inspect_child(_stub_post_terminal_frame)
    assert outcome.started is True
    assert outcome.frames[0] == {"type": "inspection", "sheet_count": 1}
    assert outcome.protocol_error is True


def test_run_child_clean_terminal_frame_then_close_is_not_protocol_error() -> None:
    """Regression guard for the finding-5 fix itself: ``Connection.poll()``
    also reports readiness on ordinary EOF (the child closing its end
    right after its one terminal frame -- the normal successful-exit
    case), not only when a real extra frame is queued. The post-terminal
    check must attempt an actual bounded recv and treat ``EOFError`` as
    clean, or every ordinary successful child would be flagged
    protocol-invalid."""
    outcome = _run_inspect_child(_stub_clean_terminal_frame)
    assert outcome.started is True
    assert outcome.frames == [{"type": "inspection", "sheet_count": 1}]
    assert outcome.protocol_error is False


# ---------------------------------------------------------------------------
# normalize_xls_support
# ---------------------------------------------------------------------------


def test_normalize_xls_support_success_multi_sheet_multi_table(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    output_dir = organized_root / "support" / "dictionary_mapping"
    budget = _budget()
    artifact_id = _artifact_id("support-1")

    result = iso.normalize_xls_support(
        data=_SUPPORT_XLS,
        expected_sha256=_sha(_SUPPORT_XLS),
        artifact_id=artifact_id,
        organized_root=organized_root,
        output_dir=output_dir,
        limits=dict(iso.DEFAULT_LIMITS),
        package_budget=budget,
    )

    assert result.artifact.parse_status is SupportParseStatus.PARSED
    assert result.artifact.kind is DependencyKind.DICTIONARY_MAPPING
    assert result.artifact.artifact_id == artifact_id
    assert result.publication is not None
    # Dict sheet contributes 2 data rows (header dropped); Codes sheet has
    # two tables separated by a blank row, each with its header dropped:
    # ["M","Male"] (1 row) and ["F","Female"] (1 row) -- "header2" is its
    # own single-cell single-row table with zero data rows.
    assert result.artifact.normalized_rows_path is not None
    lines = result.artifact.normalized_rows_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == result.publication.output.row_count
    assert result.publication.output.row_count == 4
    for line in lines:
        assert '"support_artifact_id"' in line
        assert artifact_id in line
    digest = hashlib.sha256()
    for line in lines:
        digest.update((line + "\n").encode("utf-8"))
    assert digest.hexdigest() == result.artifact.normalized_rows_sha256


def test_normalize_xls_support_zero_data_rows_still_parses_with_empty_sha(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget()
    artifact_id = _artifact_id("support-empty")

    result = iso.normalize_xls_support(
        data=_SUPPORT_HEADER_ONLY_XLS,
        expected_sha256=_sha(_SUPPORT_HEADER_ONLY_XLS),
        artifact_id=artifact_id,
        organized_root=organized_root,
        output_dir=organized_root / "support" / "dictionary_mapping",
        limits=dict(iso.DEFAULT_LIMITS),
        package_budget=budget,
    )
    assert result.artifact.parse_status is SupportParseStatus.PARSED
    assert result.publication.output.row_count == 0
    assert result.artifact.normalized_rows_sha256 == hashlib.sha256(b"").hexdigest()
    assert result.artifact.normalized_rows_path.read_bytes() == b""


def test_normalize_xls_support_worker_error_produces_failed_artifact_and_quarantine(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget()
    artifact_id = _artifact_id("support-fail")
    limits = dict(iso.DEFAULT_LIMITS)
    limits["max_rows"] = 1  # Dict sheet has 3 rows -- forces row-limit

    result = iso.normalize_xls_support(
        data=_SUPPORT_XLS,
        expected_sha256=_sha(_SUPPORT_XLS),
        artifact_id=artifact_id,
        organized_root=organized_root,
        output_dir=organized_root / "support" / "dictionary_mapping",
        limits=limits,
        package_budget=budget,
    )

    assert result.artifact.parse_status is SupportParseStatus.FAILED
    assert result.artifact.failure_code is SupportFailureCode.ROW_LIMIT
    assert result.artifact.normalized_rows_path is None
    assert result.artifact.normalized_rows_sha256 is None
    assert result.publication is None
    # No output bundle was published for a failed artifact.
    assert not (organized_root / "support" / "dictionary_mapping" / artifact_id).exists()
    # But the failed stage was quarantined and ledgered.
    quarantine_root = organized_root / ".xls-quarantine"
    entries = list(quarantine_root.iterdir())
    assert len(entries) == 1
    assert entries[0].name.startswith("support.")
    assert len(budget.retained_bundles) == 1


def test_normalize_xls_support_source_mismatch_raises_before_spawn(tmp_path: Path) -> None:
    budget = _budget()
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_support(
            data=_SUPPORT_XLS,
            expected_sha256="c" * 64,
            artifact_id=_artifact_id("support-mismatch"),
            organized_root=tmp_path / "organized",
            output_dir=tmp_path / "organized" / "support" / "dictionary_mapping",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "source-mismatch"


def test_normalize_xls_support_malformed_workbook_is_worker_parse_error(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget()
    artifact_id = _artifact_id("support-malformed")
    result = iso.normalize_xls_support(
        data=_NOT_AN_XLS,
        expected_sha256=_sha(_NOT_AN_XLS),
        artifact_id=artifact_id,
        organized_root=organized_root,
        output_dir=organized_root / "support" / "dictionary_mapping",
        limits=dict(iso.DEFAULT_LIMITS),
        package_budget=budget,
    )
    assert result.artifact.parse_status is SupportParseStatus.FAILED
    assert result.artifact.failure_code is SupportFailureCode.PARSE_ERROR


def test_normalize_xls_support_table_limit_produces_failed_artifact(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    budget = _budget()
    artifact_id = _artifact_id("support-table-limit")
    limits = dict(iso.DEFAULT_LIMITS)
    limits["max_tables"] = 1  # Codes sheet alone has two tables

    result = iso.normalize_xls_support(
        data=_SUPPORT_XLS,
        expected_sha256=_sha(_SUPPORT_XLS),
        artifact_id=artifact_id,
        organized_root=organized_root,
        output_dir=organized_root / "support" / "dictionary_mapping",
        limits=limits,
        package_budget=budget,
    )
    assert result.artifact.parse_status is SupportParseStatus.FAILED
    assert result.artifact.failure_code is SupportFailureCode.TABLE_LIMIT


# ---------------------------------------------------------------------------
# Publication bundle grammar / directory permissions
# ---------------------------------------------------------------------------


def test_support_bundle_directory_and_file_permissions(tmp_path: Path) -> None:
    organized_root = tmp_path / "organized"
    output_dir = organized_root / "support" / "dictionary_mapping"
    budget = _budget()
    artifact_id = _artifact_id("support-perms")

    result = iso.normalize_xls_support(
        data=_SUPPORT_XLS,
        expected_sha256=_sha(_SUPPORT_XLS),
        artifact_id=artifact_id,
        organized_root=organized_root,
        output_dir=output_dir,
        limits=dict(iso.DEFAULT_LIMITS),
        package_budget=budget,
    )
    bundle = result.publication.bundle_path
    assert bundle.name == artifact_id
    assert oct(bundle.stat().st_mode & 0o777) == "0o700"
    children = list(bundle.iterdir())
    assert len(children) == 1
    assert children[0].name == "rows.jsonl"
    assert oct(children[0].stat().st_mode & 0o777) == "0o600"


# ---------------------------------------------------------------------------
# AtomicRenameUnavailable maps to isolation-unavailable ONLY for
# normalization publication -- never inspect/naming (which never publish).
# ---------------------------------------------------------------------------


def test_atomic_rename_unavailable_maps_to_isolation_unavailable_for_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _always_unavailable(*_args: object, **_kwargs: object) -> None:
        raise AtomicRenameUnavailable()

    monkeypatch.setattr(iso, "renameat2_noreplace", _always_unavailable)
    organized_root = tmp_path / "organized"
    budget = _budget()
    with pytest.raises(iso.XlsIsolationError) as excinfo:
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256=_sha(_DATASET_XLS),
            artifact_id=_artifact_id("no-rename"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )
    assert excinfo.value.code == "isolation-unavailable"


def test_inspect_and_naming_never_publish_so_atomic_rename_is_irrelevant(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("inspect/naming must never call renameat2_noreplace")

    monkeypatch.setattr(iso, "renameat2_noreplace", _boom)
    assert iso.inspect_xls(_DATASET_XLS, _sha(_DATASET_XLS), max_sheets=64) == 1
    assert iso.extract_xls_naming(_DATASET_XLS, _sha(_DATASET_XLS))


# ---------------------------------------------------------------------------
# fd / process lifecycle hygiene
# ---------------------------------------------------------------------------


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_repeated_inspect_calls_leave_stable_fd_count() -> None:
    iso.inspect_xls(_DATASET_XLS, _sha(_DATASET_XLS), max_sheets=64)  # warm up
    baseline = _open_fd_count()
    for _ in range(5):
        iso.inspect_xls(_DATASET_XLS, _sha(_DATASET_XLS), max_sheets=64)
        assert _open_fd_count() == baseline


def test_repeated_failed_calls_also_leave_stable_fd_count() -> None:
    def _once() -> None:
        with pytest.raises(iso.XlsWorkerError):
            iso.inspect_xls(_NOT_AN_XLS, _sha(_NOT_AN_XLS), max_sheets=64)

    _once()  # warm up
    baseline = _open_fd_count()
    for _ in range(5):
        _once()
        assert _open_fd_count() == baseline


def test_repeated_normalize_calls_leave_stable_fd_count(tmp_path: Path) -> None:
    def _once(i: int) -> None:
        organized_root = tmp_path / f"organized-{i}"
        budget = _budget()
        iso.normalize_xls_datasets(
            data=_DATASET_XLS,
            expected_sha256=_sha(_DATASET_XLS),
            artifact_id=_artifact_id(f"fd-check-{i}"),
            organized_root=organized_root,
            output_dir=organized_root / "datasets",
            output_stem="labs",
            limits=dict(iso.DEFAULT_LIMITS),
            package_budget=budget,
        )

    _once(0)  # warm up
    baseline = _open_fd_count()
    for i in range(1, 6):
        _once(i)
        assert _open_fd_count() == baseline
