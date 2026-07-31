"""Real-world file-shape regression tests.

Sir uploaded three example artefacts on 2026-02 (Word dictionary, CSV
dataset, annotated CRF PDF) with the directive "handling of these types
of files is must, the dictionary is word doc here but if large then it
would be excel workbook. Align it accordingly." These tests lock the
.docx dictionary parser + xls fallback so a future refactor cannot drop
them.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest


DOCX_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Diabetes Care QI Data Dictionary</w:t></w:r></w:p>
    <w:tbl>
      <w:tr>
        <w:tc><w:p><w:r><w:t>Column</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Type</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>What it represents</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>patient_id</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>ID</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Unique patient identifier</w:t></w:r></w:p></w:tc>
      </w:tr>
      <w:tr>
        <w:tc><w:p><w:r><w:t>encounter_date</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Date</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>Visit date, ISO format</w:t></w:r></w:p></w:tc>
      </w:tr>
    </w:tbl>
    <w:p><w:r><w:t>End of dictionary.</w:t></w:r></w:p>
  </w:body>
</w:document>"""


def _make_docx(path: Path, xml: str = DOCX_XML) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", xml)


def test_read_docx_tables_extracts_word_table(tmp_path):
    from phi_core.agents.specialists import _read_docx_tables
    p = tmp_path / "dict.docx"
    _make_docx(p)
    out = _read_docx_tables(p)
    assert "# table 1" in out
    assert "Column,Type,What it represents" in out
    assert "patient_id,ID,Unique patient identifier" in out
    assert "encounter_date,Date," in out
    # Cell value with a comma must be quoted correctly
    assert '"Visit date, ISO format"' in out


def test_read_docx_tables_captures_narrative_context(tmp_path):
    from phi_core.agents.specialists import _read_docx_tables
    p = tmp_path / "dict.docx"
    _make_docx(p)
    out = _read_docx_tables(p)
    assert "# narrative context" in out
    assert "Diabetes Care QI Data Dictionary" in out
    assert "End of dictionary." in out


def test_read_docx_tables_returns_empty_on_bad_zip(tmp_path):
    from phi_core.agents.specialists import _read_docx_tables
    p = tmp_path / "not-a-docx.docx"
    p.write_bytes(b"this is not a docx")
    assert _read_docx_tables(p) == ""


def test_read_table_flat_dispatches_docx(tmp_path):
    from phi_core.agents.specialists import _read_table_flat
    p = tmp_path / "dict.docx"
    _make_docx(p)
    out = _read_table_flat(p)
    assert "patient_id" in out


def test_intake_accepts_docx_dictionary():
    from phi_core.intake import COMPONENT_SUFFIXES
    assert ".docx" in COMPONENT_SUFFIXES["dictionary"]
    # xls stays supported too (Sir: "if large then it would be excel workbook")
    assert ".xlsx" in COMPONENT_SUFFIXES["dictionary"]
    assert ".xls" in COMPONENT_SUFFIXES["dictionary"]


def test_read_xls_tables_handles_xlsx_disguised_as_xls(tmp_path):
    """Some data stewards rename .xlsx to .xls. The reader must NOT
    crash regardless of what actually landed on disk. Without xlrd
    installed and with openpyxl refusing the .xls suffix, the reader
    returns an empty string (Lexicon just receives no table text)."""
    from openpyxl import Workbook
    from phi_core.agents.specialists import _read_xls_tables
    p_xlsx = tmp_path / "dict.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Column", "Type", "What it represents"])
    ws.append(["patient_id", "ID", "Unique patient identifier"])
    wb.save(str(p_xlsx))
    p_xls = tmp_path / "dict.xls"
    p_xls.write_bytes(p_xlsx.read_bytes())
    # Must not raise. xlrd may or may not be installed; either way, the
    # function is contract-bound to return a string, never None.
    out = _read_xls_tables(p_xls)
    assert isinstance(out, str)
