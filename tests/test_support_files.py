from __future__ import annotations

import json
import stat
import zipfile
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from phi_engine.pipeline.dependencies import DependencyKind, SupportFailureCode, SupportParseStatus
from phi_engine.pipeline.support_files import DEFAULT_LIMITS, parse_support_artifact


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _parse(path: Path, out: Path, **limits: int):
    return parse_support_artifact(
        artifact_id="a_" + "1" * 32,
        source_sha256="2" * 64,
        kind=DependencyKind.DICTIONARY,
        source_path=path,
        output_dir=out,
        limits=limits or None,
    )


def _assert_normalized_rows(path: Path) -> None:
    rows = _read_jsonl(path)
    assert rows
    for row in rows:
        assert set(row) == {"support_artifact_id", "source_sha256", "sheet_index", "table_index", "row_index", "cells"}
        assert isinstance(row["cells"], list)
        for cell in row["cells"]:
            assert set(cell) == {"column_index", "value"}


def test_support_parser_audits_all_formats_and_writes_0600(tmp_path: Path) -> None:
    csv_path = tmp_path / "dict.csv"
    csv_path.write_text("variable,label\nSUBJID,Subject ID\n", encoding="utf-8")
    json_path = tmp_path / "dict.json"
    json_path.write_text(json.dumps([{"variable": "AGE", "label": "Age"}]), encoding="utf-8")
    jsonl_path = tmp_path / "dict.jsonl"
    jsonl_path.write_text(json.dumps({"variable": "SEX", "label": "Sex"}) + "\n", encoding="utf-8")
    xlsx_path = tmp_path / "dict.xlsx"
    pytest.importorskip("openpyxl")
    import pandas as pd

    pd.DataFrame({"variable": ["VISIT"], "label": ["Visit"]}).to_excel(xlsx_path, index=False)
    pdf_path = tmp_path / "dict.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.drawString(100, 750, "SUBJID Subject identifier")
    c.drawString(100, 735, "AGE Age in years")
    c.save()

    for source in (csv_path, json_path, jsonl_path, xlsx_path, pdf_path):
        parsed = _parse(source, tmp_path / "out" / source.suffix.lstrip("."))
        assert parsed.parse_status is SupportParseStatus.PARSED, source
        assert parsed.normalized_rows_path is not None
        assert stat.S_IMODE(parsed.normalized_rows_path.stat().st_mode) == 0o600
        _assert_normalized_rows(parsed.normalized_rows_path)

    import xlwt

    real_xls_path = tmp_path / "real_dict.xls"
    book = xlwt.Workbook()
    sheet = book.add_sheet("Dictionary")
    sheet.write(0, 0, "variable")
    sheet.write(0, 1, "label")
    sheet.write(1, 0, "SUBJID")
    sheet.write(1, 1, "Subject ID")
    book.save(str(real_xls_path))
    parsed = _parse(real_xls_path, tmp_path / "out" / "real_xls")
    assert parsed.parse_status is SupportParseStatus.PARSED
    assert parsed.normalized_rows_path is not None
    _assert_normalized_rows(parsed.normalized_rows_path)

    xls_path = tmp_path / "dict.xls"
    xls_path.write_bytes(b"not a real xls")
    parsed = _parse(xls_path, tmp_path / "out" / "xls", max_expanded_workbook_bytes=0)
    assert parsed.parse_status is SupportParseStatus.FAILED
    assert parsed.failure_code is SupportFailureCode.EXPANDED_SIZE_LIMIT
    assert parsed.normalized_rows_path is None


def test_support_parser_enforces_exact_limits_without_partial_evidence(tmp_path: Path) -> None:
    csv_path = tmp_path / "dict.csv"
    csv_path.write_text("variable,label\nSUBJID,Subject ID\n", encoding="utf-8")
    cases = [
        ({"max_source_bytes": 1}, SupportFailureCode.SOURCE_SIZE_LIMIT),
        ({"max_rows": 0}, SupportFailureCode.ROW_LIMIT),
        ({"max_columns": 1}, SupportFailureCode.COLUMN_LIMIT),
        ({"max_cell_codepoints": 1}, SupportFailureCode.CELL_SIZE_LIMIT),
    ]
    for limits, code in cases:
        parsed = _parse(csv_path, tmp_path / f"out_{code.value}", **limits)
        assert parsed.parse_status is SupportParseStatus.FAILED
        assert parsed.failure_code is code
        assert parsed.normalized_rows_path is None
        assert not (tmp_path / f"out_{code.value}" / parsed.artifact_id).with_suffix(".jsonl").exists()

    json_path = tmp_path / "deep.json"
    json_path.write_text(json.dumps({"a": {"b": {"c": 1}}}), encoding="utf-8")
    parsed = _parse(json_path, tmp_path / "out_depth", max_json_depth=1)
    assert parsed.parse_status is SupportParseStatus.FAILED
    assert parsed.failure_code is SupportFailureCode.JSON_DEPTH_LIMIT
    assert parsed.normalized_rows_path is None


def test_support_parser_rejects_lower_configured_limits_and_zip_ratio(tmp_path: Path) -> None:
    csv_path = tmp_path / "dict.csv"
    csv_path.write_text("variable,label\nA,a\nB,b\n", encoding="utf-8")
    parsed = _parse(csv_path, tmp_path / "out_lower", max_rows=1)
    assert parsed.parse_status is SupportParseStatus.FAILED
    assert parsed.failure_code is SupportFailureCode.ROW_LIMIT

    zip_path = tmp_path / "bomb.xlsx"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", "A" * 10000)
    parsed = _parse(zip_path, tmp_path / "out_ratio", max_decompression_ratio=1)
    assert parsed.parse_status is SupportParseStatus.FAILED
    assert parsed.failure_code is SupportFailureCode.DECOMPRESSION_RATIO_LIMIT


def test_support_parser_rejects_unsupported_format(tmp_path: Path) -> None:
    source = tmp_path / "dict.txt"
    source.write_text("variable label", encoding="utf-8")
    parsed = _parse(source, tmp_path / "out")
    assert parsed.parse_status is SupportParseStatus.FAILED
    assert parsed.failure_code is SupportFailureCode.UNSUPPORTED_FORMAT
    assert parsed.normalized_rows_path is None
