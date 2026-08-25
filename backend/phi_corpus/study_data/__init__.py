"""Curated static study packages (datasets + dictionary only).

Hand-authored fixtures under ``phi_corpus/study_data/<level_id>/`` that
pack into a manifest-v3 intake ZIP:

  datasets/dataset.csv
  dictionary/dictionary.csv

These are not generator output: they are reviewed fixtures the Wizard
or API can load without reintroducing PDF/form generation.
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

STUDY_DATA_ROOT = Path(__file__).resolve().parent


def list_packages() -> list[dict[str, Any]]:
    """Return metadata for every ``level_*`` package that has both CSVs."""
    out: list[dict[str, Any]] = []
    if not STUDY_DATA_ROOT.is_dir():
        return out
    for path in sorted(STUDY_DATA_ROOT.iterdir()):
        if not path.is_dir() or path.name.startswith("_"):
            continue
        if path.name == "__pycache__":
            continue
        dataset = path / "dataset.csv"
        dictionary = path / "dictionary.csv"
        if not (dataset.is_file() and dictionary.is_file()):
            continue
        out.append({
            "id": path.name,
            "label": path.name.replace("_", " "),
            "dataset": dataset.name,
            "dictionary": dictionary.name,
            "dataset_bytes": dataset.stat().st_size,
            "dictionary_bytes": dictionary.stat().st_size,
        })
    return out


def package_dir(package_id: str) -> Path:
    """Resolve a package id to its directory; raise ``FileNotFoundError`` if missing."""
    if "/" in package_id or "\\" in package_id or ".." in package_id:
        raise FileNotFoundError(package_id)
    path = STUDY_DATA_ROOT / package_id
    if not path.is_dir():
        raise FileNotFoundError(package_id)
    if not (path / "dataset.csv").is_file() or not (path / "dictionary.csv").is_file():
        raise FileNotFoundError(package_id)
    return path


def _write_deterministic_zip_entry(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    archive.writestr(info, data)


def build_intake_zip(package_id: str) -> bytes:
    """Pack ``dataset.csv`` + ``dictionary.csv`` into a manifest-v3 ZIP."""
    root = package_dir(package_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        _write_deterministic_zip_entry(zf, "datasets/dataset.csv", (root / "dataset.csv").read_bytes())
        _write_deterministic_zip_entry(zf, "dictionary/dictionary.csv", (root / "dictionary.csv").read_bytes())
    return buf.getvalue()
