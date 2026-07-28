"""SEC-004 fail-closed export tests + SEC-005 zip-bomb caps."""
import io
import os
import zipfile
from pathlib import Path

import pytest

from phi_core.agents.reasoning import (
    _redact_metadata_file,
    apply_column_actions_to_dataset,
)
from phi_core.intake import unpack_zip


# ---------- SEC-004 -------------------------------------------------------

def test_metadata_csv_is_redacted_not_copied(tmp_path: Path):
    """A dictionary CSV must NOT be copied verbatim - PHI-looking cells scrubbed."""
    src = tmp_path / "codebook.csv"
    src.write_text(
        "column_name,description\n"
        "patient_name,Full name of patient e.g. John Doe\n"
        "phone_number,Callback phone 415-555-1234 or james@example.edu\n",
        encoding="utf-8",
    )
    dst = tmp_path / "codebook.out.csv"
    _redact_metadata_file(src, dst)
    text = dst.read_text(encoding="utf-8")
    assert "John Doe" not in text
    assert "415-555-1234" not in text
    assert "james@example.edu" not in text
    # The safe column names must survive.
    assert "patient_name" in text
    assert "phone_number" in text


def test_unmapped_column_defaults_to_drop(tmp_path: Path):
    """An orphan column with no decision must be blanked (fail-closed)."""
    src = tmp_path / "data.csv"
    src.write_text(
        "known_col,orphan_col\n"
        "keep_me_value,should_be_dropped\n",
        encoding="utf-8",
    )
    dst = tmp_path / "data.out.csv"
    decisions = [{"file_id": "f", "column": "known_col", "action": "keep"}]
    apply_column_actions_to_dataset(src, dst, "csv", decisions)
    out = dst.read_text(encoding="utf-8").splitlines()
    assert out[0] == "known_col,orphan_col"
    assert out[1] == "keep_me_value,"


def test_unmapped_column_scrub_text_when_env_override(tmp_path: Path, monkeypatch):
    """`PHI_UNMAPPED_COLUMN_ACTION=scrub_text` scrubs instead of dropping."""
    monkeypatch.setenv("PHI_UNMAPPED_COLUMN_ACTION", "scrub_text")
    src = tmp_path / "data.csv"
    src.write_text(
        "known_col,orphan_col\n"
        "ok,call James at 415-555-1234\n",
        encoding="utf-8",
    )
    dst = tmp_path / "data.out.csv"
    decisions = [{"file_id": "f", "column": "known_col", "action": "keep"}]
    apply_column_actions_to_dataset(src, dst, "csv", decisions)
    out = dst.read_text(encoding="utf-8")
    assert "415-555-1234" not in out
    assert "call" in out  # non-PHI content preserved


# ---------- SEC-005 -------------------------------------------------------

def _zip_with_many_entries(zip_path: Path, n: int) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(n):
            zf.writestr(f"datasets/f{i}.csv", b"a,b\n1,2\n")


def _zip_with_bomb(zip_path: Path) -> None:
    # High-ratio compression: 10 MB of zeros -> ~10 KB compressed.
    payload = b"\x00" * (10 * 1024 * 1024)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("datasets/big.csv", payload)


def test_intake_rejects_too_many_entries(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("INTAKE_MAX_ENTRIES", "5")
    z = tmp_path / "many.zip"
    _zip_with_many_entries(z, 10)
    _, err = unpack_zip(z, tmp_path / "out")
    assert err and "cap is 5" in err


def test_intake_rejects_compression_bomb(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("INTAKE_MAX_RATIO", "10")
    z = tmp_path / "bomb.zip"
    _zip_with_bomb(z)
    _, err = unpack_zip(z, tmp_path / "out")
    assert err and "compression ratio" in err


def test_intake_rejects_total_size(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("INTAKE_MAX_TOTAL_BYTES", "1024")
    monkeypatch.setenv("INTAKE_MAX_ENTRIES", "50")
    monkeypatch.setenv("INTAKE_MAX_RATIO", "10000")
    z = tmp_path / "big.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("datasets/a.csv", b"x" * 2048)
    _, err = unpack_zip(z, tmp_path / "out")
    assert err and "aggregate streamed size" in err
