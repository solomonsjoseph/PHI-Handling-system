"""Tests for curated static study_data packages."""
from __future__ import annotations

import io
import zipfile

import pytest


def test_list_packages_includes_tuberculosis_level_1():
    from phi_corpus.study_data import list_packages
    ids = {p["id"] for p in list_packages()}
    assert "level_1_tuberculosis" in ids


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
