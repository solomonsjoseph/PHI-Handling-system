"""Phase 15b category 3: upload hardening (docs section 98).

Drives every scenario through ``build_manifest`` -- the single real
intake entry point (unpack_zip -> scan_intake -> manifest status/exit
code resolution) a live upload endpoint actually calls -- with a
genuine, freshly-crafted ZIP file, rather than re-testing
``unpack_zip``/``scan_intake`` in isolation the way
``test_intake_hardening.py`` already does. Positive-detection: each
malicious artifact is real, on-disk, and would have reached the backend
exactly as constructed here.
"""
from __future__ import annotations

import io
import zipfile

from phi_core.intake import build_manifest


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _write_zip(path, entries: dict[str, bytes]) -> None:
    path.write_bytes(_zip_bytes(entries))


# ---------------------------------------------------------------------------
# 1. ZIP Slip / path traversal -- an entry path that escapes the intake
#    root via ../ segments must never be written outside dest_root, and
#    build_manifest must fail closed rather than silently drop the entry.
# ---------------------------------------------------------------------------


def test_build_manifest_rejects_a_zip_slip_entry_end_to_end(tmp_path):
    zip_path = tmp_path / "study.zip"
    escape_marker = b"ZZZZIPSLIPPAYLOAD-should-never-land-outside-workspace"
    _write_zip(zip_path, {
        "datasets/study.csv": b"id,name\n1,Jane\n",
        "dictionary/codebook.csv": b"column,meaning\nid,identifier\n",
        "../../../tmp/evil_escape.txt": escape_marker,
    })
    workspace_root = tmp_path / "intake_workspace"

    manifest = build_manifest("advuploads-zipslip", zip_path, workspace_root)

    assert manifest.status == "failed"
    assert manifest.exit_code == 2
    assert "unsafe path" in (manifest.error or "")
    # Never written anywhere on disk, including outside the intended tree.
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert escape_marker not in path.read_bytes()


def test_build_manifest_rejects_an_absolute_path_entry_end_to_end(tmp_path):
    zip_path = tmp_path / "study2.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        info = zipfile.ZipInfo("/etc/absolute_escape.txt")
        zf.writestr(info, b"absolute path payload")
        zf.writestr("datasets/study.csv", b"id\n1\n")
    zip_path.write_bytes(buf.getvalue())
    workspace_root = tmp_path / "intake_workspace2"

    manifest = build_manifest("advuploads-abspath", zip_path, workspace_root)

    assert manifest.status == "failed"
    assert manifest.exit_code == 2


# ---------------------------------------------------------------------------
# 2. Archive bomb -- an entry whose decompressed size wildly exceeds its
#    compressed size (a compression-ratio bomb) must be refused before
#    the full decompressed payload is ever streamed to disk.
# ---------------------------------------------------------------------------


def test_build_manifest_rejects_a_compression_ratio_bomb_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("INTAKE_MAX_RATIO", "50")
    zip_path = tmp_path / "bomb.zip"
    # Highly repetitive content compresses far beyond a 50x ratio.
    bomb_content = b"A" * (5 * 1024 * 1024)  # 5 MiB of a single repeated byte
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        zf.writestr("datasets/study.csv", bomb_content)
    zip_path.write_bytes(buf.getvalue())
    workspace_root = tmp_path / "intake_workspace_bomb"

    manifest = build_manifest("advuploads-bomb", zip_path, workspace_root)

    assert manifest.status == "failed"
    assert manifest.exit_code == 2
    assert "compression ratio" in (manifest.error or "")
    # No 5 MiB file was ever fully materialized under the workspace root.
    written_sizes = [p.stat().st_size for p in workspace_root.rglob("*") if p.is_file()]
    assert all(size < len(bomb_content) for size in written_sizes)


def test_build_manifest_rejects_a_zip_exceeding_the_aggregate_size_cap_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("INTAKE_MAX_TOTAL_BYTES", str(64 * 1024))
    monkeypatch.setenv("INTAKE_MAX_RATIO", "100000")
    zip_path = tmp_path / "toolarge.zip"
    # Semi-random content (won't trip the ratio cap) that still exceeds
    # the aggregate byte cap once streamed.
    import os as _os
    oversized_content = _os.urandom(96 * 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("datasets/study.csv", oversized_content)
    zip_path.write_bytes(buf.getvalue())
    workspace_root = tmp_path / "intake_workspace_toolarge"

    manifest = build_manifest("advuploads-toolarge", zip_path, workspace_root)

    assert manifest.status == "failed"
    assert manifest.exit_code == 2
    assert "aggregate streamed size" in (manifest.error or "")


# ---------------------------------------------------------------------------
# 3. Malformed archive -- corrupt/non-zip bytes must fail closed with a
#    clean error, never a crash or partial unpack.
# ---------------------------------------------------------------------------


def test_build_manifest_rejects_a_malformed_archive_end_to_end(tmp_path):
    zip_path = tmp_path / "corrupt.zip"
    zip_path.write_bytes(b"PK\x03\x04this is not really a valid zip stream at all" + b"\x00" * 40)
    workspace_root = tmp_path / "intake_workspace_corrupt"

    manifest = build_manifest("advuploads-corrupt", zip_path, workspace_root)

    assert manifest.status == "failed"
    assert manifest.exit_code == 2
    assert manifest.error


# ---------------------------------------------------------------------------
# 4. Malicious filename -- an entry name carrying a NUL byte (a classic
#    path-confusion / truncation attack shape) must never be accepted or
#    silently truncated into a different, unintended file.
# ---------------------------------------------------------------------------


def test_build_manifest_rejects_or_safely_contains_a_null_byte_filename_end_to_end(tmp_path):
    zip_path = tmp_path / "nullbyte.zip"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("datasets/study.csv", b"id\n1\n")
        # A NUL byte in the entry name -- if naively passed to a C-string
        # -based filesystem call this could truncate to "datasets/evil".
        zf.writestr("datasets/evil\x00.exe.csv", b"MZ\x90\x00fake executable payload masquerading as csv")
    zip_path.write_bytes(buf.getvalue())
    workspace_root = tmp_path / "intake_workspace_nullbyte"

    # Either build_manifest fails the whole intake closed, or it succeeds
    # having routed the NUL-byte entry to _unclassified/rejected it --
    # either way, no file whose on-disk name differs unexpectedly from
    # its declared zip entry (a truncated ".../evil" masking the real
    # payload) is ever left servable as a clean dataset file.
    manifest = build_manifest("advuploads-nullbyte", zip_path, workspace_root)

    if manifest.status == "ready":
        clean_dataset_names = {
            e.relpath for e in manifest.entries if e.component == "datasets"
        }
        assert "datasets/evil\x00.exe.csv" not in clean_dataset_names
        assert not any("evil" in name for name in clean_dataset_names)
    else:
        assert manifest.status == "failed"


# ---------------------------------------------------------------------------
# 5. Unexpected executable content -- a PE binary disguised with a .csv
#    extension, driven all the way through build_manifest (not merely
#    scan_intake on a pre-extracted tree), must land in _unclassified,
#    never in the servable datasets component.
# ---------------------------------------------------------------------------


def test_build_manifest_quarantines_a_disguised_pe_binary_end_to_end(tmp_path):
    zip_path = tmp_path / "disguised.zip"
    pe_payload = b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64 + b"this is really a windows binary"
    _write_zip(zip_path, {
        "datasets/study.csv": b"id,name\n1,Jane\n",
        "datasets/study_extra.csv": pe_payload,
        "dictionary/codebook.csv": b"column,meaning\nid,identifier\n",
    })
    workspace_root = tmp_path / "intake_workspace_pe"

    manifest = build_manifest("advuploads-pe", zip_path, workspace_root)

    assert manifest.status == "review_required"
    unclassified = [e for e in manifest.entries if e.component == "_unclassified"]
    assert len(unclassified) == 1
    assert unclassified[0].relpath == "datasets/study_extra.csv"
    assert "executable content detected" in unclassified[0].reason
    clean_datasets = [e for e in manifest.entries if e.component == "datasets"]
    assert len(clean_datasets) == 1
    assert clean_datasets[0].relpath == "datasets/study.csv"


def test_build_manifest_quarantines_a_shebang_script_disguised_as_a_dictionary_end_to_end(tmp_path):
    zip_path = tmp_path / "disguised_script.zip"
    _write_zip(zip_path, {
        "datasets/study.csv": b"id,name\n1,Jane\n",
        "dictionary/codebook.csv": b"#!/bin/sh\nrm -rf / --no-preserve-root\n",
    })
    workspace_root = tmp_path / "intake_workspace_script"

    manifest = build_manifest("advuploads-script", zip_path, workspace_root)

    unclassified = [e for e in manifest.entries if e.component == "_unclassified"]
    assert len(unclassified) == 1
    assert "shebang" in unclassified[0].reason
    assert not any(e.component == "dictionary" for e in manifest.entries)
