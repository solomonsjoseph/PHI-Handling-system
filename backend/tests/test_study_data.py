"""Tests for curated static study_data packages."""
from __future__ import annotations

import io
import zipfile

import pytest


def test_list_packages_includes_tuberculosis_level_1():
    from phi_corpus.study_data import list_packages
    ids = {p["id"] for p in list_packages()}
    assert "level_1_tuberculosis" in ids


def test_tuberculosis_package_has_31_columns_with_low_cardinality_facility():
    import csv
    from phi_corpus.study_data import package_dir
    root = package_dir("level_1_tuberculosis")
    with (root / "dataset.csv").open(newline="") as f:
        rows = list(csv.reader(f))
    header, data_rows = rows[0], rows[1:]
    assert len(header) == 31
    facility_idx = header.index("treatment_facility_name")
    facility_values = {row[facility_idx] for row in data_rows}
    assert len(facility_values) == 4
    with (root / "dictionary.csv").open(newline="") as f:
        dict_rows = list(csv.DictReader(f))
    assert len(dict_rows) == 31
    assert any(r["column_name"] == "treatment_facility_name" for r in dict_rows)


def test_build_intake_zip_has_manifest_v3_layout():
    from phi_corpus.study_data import build_intake_zip
    raw = build_intake_zip("level_1_tuberculosis")
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())
    assert "datasets/dataset.csv" in names
    assert "dictionary/dictionary.csv" in names
    assert not any(n.startswith("forms/") for n in names)


def test_build_intake_zip_rejects_path_traversal():
    from phi_corpus.study_data import build_intake_zip
    with pytest.raises(FileNotFoundError):
        build_intake_zip("../secrets")
