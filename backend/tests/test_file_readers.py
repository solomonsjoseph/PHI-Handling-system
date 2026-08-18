"""Focused contracts for bounded table readers."""
from __future__ import annotations

from pathlib import Path

import openpyxl
import pytest

from phi_core.file_readers import column_value_stats, read_table_rows


@pytest.mark.parametrize("ext", ["csv", "tsv", "xlsx"])
def test_table_readers_preserve_rows_and_count_bounded_cardinality(
    tmp_path: Path, ext: str
) -> None:
    path = tmp_path / f"table.{ext}"
    expected_rows = [["alpha", "north"], ["beta", "north"], ["alpha", "south"]]
    if ext == "xlsx":
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["subject", "site"])
        for row in expected_rows:
            sheet.append(row)
        workbook.save(path)
        workbook.close()
    else:
        delimiter = "\t" if ext == "tsv" else ","
        path.write_text(
            delimiter.join(["subject", "site"])
            + "\n"
            + "\n".join(delimiter.join(row) for row in expected_rows)
            + "\n",
            encoding="utf-8",
        )

    headers, rows = read_table_rows(path)

    assert headers == ["subject", "site"]
    assert read_table_rows(path, max_rows=2)[1] == expected_rows[:2]
    assert rows == expected_rows
    assert column_value_stats(path, ext, headers) == {
        "subject": {"distinct": 2, "rows": 3},
        "site": {"distinct": 2, "rows": 3},
    }
    assert column_value_stats(path, ext, headers, max_rows=2) == {
        "subject": {"distinct": 2, "rows": 2},
        "site": {"distinct": 1, "rows": 2},
    }


@pytest.mark.parametrize("ext", ["csv", "tsv", "xlsx"])
def test_table_readers_ignore_blank_rows_and_normalize_stat_columns(
    tmp_path: Path, ext: str
) -> None:
    path = tmp_path / f"blank_rows.{ext}"
    expected_rows = [["alpha", ""], ["beta", "north"]]
    if ext == "xlsx":
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append([None, None])
        sheet.append([" Subject ", "Site"])
        for row in expected_rows:
            sheet.append(row)
        sheet.append([None, None])
        workbook.save(path)
        workbook.close()
    else:
        delimiter = "\t" if ext == "tsv" else ","
        path.write_text(
            "\n"
            + delimiter.join([" Subject ", "Site"])
            + "\n"
            + "\n".join(delimiter.join(row) for row in expected_rows)
            + "\n\n",
            encoding="utf-8",
        )

    headers, rows = read_table_rows(path)

    assert [header.strip() for header in headers] == ["Subject", "Site"]
    assert rows == expected_rows
    assert column_value_stats(path, ext, [" subject ", "SITE", "missing"]) == {
        " subject ": {"distinct": 2, "rows": 2},
        "SITE": {"distinct": 2, "rows": 2},
        "missing": {"distinct": 0, "rows": 0},
    }
