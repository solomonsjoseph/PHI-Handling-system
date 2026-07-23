from __future__ import annotations

import hashlib
import io
import inspect
import os
import shutil
import struct
import tempfile
import warnings
import zipfile
from pathlib import Path

import openpyxl
import pytest

import phi_engine.pipeline.intake_preflight as intake_preflight_module
from phi_engine.pipeline.intake_preflight import (
    count_xlsx_sheets,
    inspect_intake_source,
)
from phi_engine.pipeline.support_files import DEFAULT_LIMITS


def _make_canonical(root: Path) -> None:
    (root / "datasets").mkdir(parents=True)
    (root / "forms").mkdir(parents=True)
    (root / "data_dictionary").mkdir(parents=True)
    (root / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (root / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    _write_workbook(root / "data_dictionary" / "dict.xlsx", sheet_names=["Sheet1"])


def _write_workbook(path: Path, *, sheet_names: list[str]) -> None:
    wb = openpyxl.Workbook()
    wb.active.title = sheet_names[0]
    for name in sheet_names[1:]:
        wb.create_sheet(name)
    wb.save(path)


def _raw_workbook_zip_bytes(sheets_inner_xml: str, *, namespace: str | None = None) -> bytes:
    ns = namespace or "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{ns}" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets_inner_xml}</sheets></workbook>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", workbook_xml)
    return buf.getvalue()


def _reasons_for_path(result, path: str) -> set[str]:
    return {item["reason"] for item in result.review_items if item["path"] == path} | {
        item["reason"] for item in result.errors if item["path"] == path
    }


def _by_path(candidates) -> dict[str, object]:
    return {c.relative_path: c for c in candidates}


def _reasons(items) -> set[str]:
    return {item["reason"] for item in items}


def _review_map(result) -> dict[str, str]:
    return {item["path"]: item["reason"] for item in result.review_items}


def _error_map(result) -> dict[str, str]:
    return {item["path"]: item["reason"] for item in result.errors}


# --- public contract -------------------------------------------------------------------


def test_public_names_match_the_approved_contract() -> None:
    assert set(intake_preflight_module.__all__) == {
        "Component",
        "IntakeCandidate",
        "IntakePreflight",
        "inspect_intake_source",
        "count_xlsx_sheets",
    }
    sig = inspect.signature(count_xlsx_sheets)
    assert list(sig.parameters) == ["fileobj"]
    sig2 = inspect.signature(inspect_intake_source)
    assert list(sig2.parameters) == ["source"]


# --- candidates for a canonical package -------------------------------------------------


def test_canonical_package_produces_ready_candidates_no_review_no_errors(tmp_path: Path) -> None:
    _make_canonical(tmp_path)

    result = inspect_intake_source(tmp_path)

    assert result.review_items == ()
    assert result.errors == ()
    by_path = _by_path(result.candidates)
    assert by_path["datasets/labs.csv"].component == "datasets"
    assert by_path["forms/consent.pdf"].component == "forms"
    assert by_path["data_dictionary/dict.xlsx"].component == "data_dictionary"
    assert by_path["data_dictionary/dict.xlsx"].sheet_count == 1
    assert by_path["datasets/labs.csv"].sheet_count is None
    for candidate in result.candidates:
        assert candidate.sha256 and len(candidate.sha256) == 64
        assert candidate.identity.size >= 0


def test_mappings_alone_satisfies_the_dictionary_or_mapping_requirement(tmp_path: Path) -> None:
    (tmp_path / "datasets").mkdir(parents=True)
    (tmp_path / "forms").mkdir(parents=True)
    (tmp_path / "mappings").mkdir(parents=True)
    (tmp_path / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (tmp_path / "mappings" / "map.csv").write_text("code,label\n1,a\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    assert result.review_items == ()
    assert result.errors == ()
    assert _by_path(result.candidates)["mappings/map.csv"].component == "mappings"


def test_nested_directories_and_duplicate_bytes_remain_distinct_candidates(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    nested = tmp_path / "datasets" / "site_a" / "visit_1"
    nested.mkdir(parents=True)
    (nested / "dup.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")  # same bytes as labs.csv

    result = inspect_intake_source(tmp_path)

    by_path = _by_path(result.candidates)
    assert "datasets/site_a/visit_1/dup.csv" in by_path
    assert by_path["datasets/site_a/visit_1/dup.csv"].sha256 == by_path["datasets/labs.csv"].sha256
    assert by_path["datasets/site_a/visit_1/dup.csv"].relative_path != by_path["datasets/labs.csv"].relative_path


def test_candidates_and_review_items_are_deterministically_ordered(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    (tmp_path / "datasets" / "z.txt").write_text("x", encoding="utf-8")
    (tmp_path / "datasets" / "a.txt").write_text("x", encoding="utf-8")

    first = inspect_intake_source(tmp_path)
    second = inspect_intake_source(tmp_path)

    assert [c.relative_path for c in first.candidates] == sorted(c.relative_path for c in first.candidates)
    assert first.candidates == second.candidates
    assert first.review_items == second.review_items
    assert first.errors == second.errors


# --- source root itself ------------------------------------------------------------------


def test_symlinked_source_root_is_rejected_root_never_followed(tmp_path: Path) -> None:
    real = tmp_path / "real_package"
    _make_canonical(real)
    link = tmp_path / "source_link"
    link.symlink_to(real)

    result = inspect_intake_source(link)

    assert result.candidates == ()
    assert result.review_items == ()
    assert result.errors == ({"path": "", "reason": "source-symlink-not-allowed"},)


def test_symlink_loop_source_root_fails_closed_without_raw_exception(tmp_path: Path) -> None:
    loop = tmp_path / "loop"
    loop.symlink_to(loop)

    result = inspect_intake_source(loop)

    assert result.errors == ({"path": "", "reason": "source-symlink-not-allowed"},)


def test_unreadable_source_root_yields_single_error_no_candidates(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"

    result = inspect_intake_source(missing)

    assert result.candidates == ()
    assert result.review_items == ()
    assert result.errors == ({"path": "", "reason": "source-unreadable"},)


def test_source_root_that_is_a_regular_file_is_rejected(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "file.txt"
    not_a_dir.write_text("x", encoding="utf-8")

    result = inspect_intake_source(not_a_dir)

    assert result.errors == ({"path": "", "reason": "source-unreadable"},)


def test_symlinked_ancestor_of_source_root_is_rejected_not_only_the_final_component(tmp_path: Path) -> None:
    # The root pin walks EVERY ancestor path segment, not only the final
    # "source" component -- a symlink anywhere in the supplied ancestry must
    # be rejected, even though the final segment itself is a real directory.
    real = tmp_path / "real_package"
    _make_canonical(real)
    linked_parent = tmp_path / "linked_parent"
    linked_parent.symlink_to(tmp_path)
    source_through_symlinked_ancestor = linked_parent / "real_package"

    result = inspect_intake_source(source_through_symlinked_ancestor)

    assert result.candidates == ()
    assert result.errors == ({"path": "", "reason": "source-symlink-not-allowed"},)


def test_root_path_renamed_mid_traversal_never_produces_a_mixed_package_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Synchronized swap: after the first candidate has been fully processed,
    # atomically rename the exact filesystem-namespace path preflight was
    # given away, then rename a replacement package into that same path --
    # simulating a concurrent directory replacement of the whole root.
    # Because the root descriptor is pinned by inode (not by path) and every
    # subsequent listing/open is descriptor-relative to it, this rename can
    # never be observed by the in-flight traversal at all: the result must
    # reflect only the originally-pinned tree, never the replacement and
    # never a mixture of the two.
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    _make_canonical(original)
    _make_canonical(replacement)
    (replacement / "datasets" / "labs.csv").write_text("SUBJID,AGE\n9,99\n", encoding="utf-8")  # distinct content
    source = tmp_path / "source"
    shutil.copytree(original, source)

    real_hash = intake_preflight_module._hash_from_fd
    call_count = {"n": 0}

    def swapping_hash(fd: int) -> str:
        call_count["n"] += 1
        result = real_hash(fd)
        if call_count["n"] == 1:
            # Atomic (each step is its own atomic rename) whole-root swap,
            # performed only after the first candidate's own hash finished.
            aside = tmp_path / "source_old"
            os.rename(source, aside)
            os.rename(replacement, source)
        return result

    monkeypatch.setattr(intake_preflight_module, "_hash_from_fd", swapping_hash)

    result = inspect_intake_source(source)

    assert call_count["n"] >= 1, "the swap hook never fired -- test setup is broken"
    original_sha = hashlib.sha256(b"SUBJID,AGE\n1,40\n").hexdigest()
    replacement_sha = hashlib.sha256(b"SUBJID,AGE\n9,99\n").hexdigest()
    by_path = _by_path(result.candidates)
    assert "datasets/labs.csv" in by_path
    assert by_path["datasets/labs.csv"].sha256 == original_sha
    assert by_path["datasets/labs.csv"].sha256 != replacement_sha
    assert result.review_items == ()
    assert result.errors == ()


def test_final_file_replaced_between_discovery_and_open_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Synchronized swap at the narrowest possible window: mutate the file's
    # content in place (same directory entry, so the parent directory's own
    # mtime is untouched) exactly between the moment its identity is
    # captured from discovery and the moment it is actually opened for
    # verified reading.
    _make_canonical(tmp_path)
    target = tmp_path / "datasets" / "labs.csv"

    real_dirent_identity = intake_preflight_module._dirent_identity

    def swapping_dirent_identity(entry):
        result = real_dirent_identity(entry)
        if entry.name == "labs.csv":
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("SUBJID,AGE\n9,99\nEXTRA,ROW\n")
        return result

    monkeypatch.setattr(intake_preflight_module, "_dirent_identity", swapping_dirent_identity)

    result = inspect_intake_source(tmp_path)

    assert "datasets/labs.csv" not in _by_path(result.candidates)
    assert _error_map(result).get("datasets/labs.csv") == "source-unreadable"


def test_nested_directory_replaced_during_child_processing_never_mixes_trees(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Synchronized swap: replace a NESTED directory (not the root) with a
    # different physical tree while its own children are still being
    # processed. Data integrity comes from descriptor pinning: the "nested"
    # frame's own held fd is inode-pinned and unaffected by the external
    # rename, so its still-unread children are guaranteed to come from the
    # ORIGINAL tree regardless of the swap. The post-children identity
    # recheck (on "nested" itself, or its parent "datasets" whose entries
    # the rename also touched) is an ADDITIONAL best-effort layer that can
    # occasionally miss a rename happening within the same filesystem mtime
    # tick -- so the guarantee actually under test is "never a mixture",
    # not "detection always fires": either the whole result is invalidated
    # (detection fired), or every candidate is provably from the original
    # tree only (detection missed but pinning still held).
    _make_canonical(tmp_path)
    nested = tmp_path / "datasets" / "nested"
    nested.mkdir()
    (nested / "a.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (nested / "b.csv").write_text("a,b\n3,4\n", encoding="utf-8")

    replacement_base = Path(tempfile.mkdtemp())
    replacement_nested = replacement_base / "nested_replacement_source"
    replacement_nested.mkdir()
    (replacement_nested / "a.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (replacement_nested / "b.csv").write_text("a,b\nZZZ,ZZZ\n", encoding="utf-8")

    real_hash = intake_preflight_module._hash_from_fd
    a_csv_size = (nested / "a.csv").stat().st_size
    swapped = {"done": False}

    def swapping_hash(fd: int) -> str:
        result = real_hash(fd)
        if not swapped["done"] and os.fstat(fd).st_size == a_csv_size:
            swapped["done"] = True
            aside = replacement_base / "nested_old"
            os.rename(nested, aside)
            os.rename(replacement_nested, nested)
        return result

    monkeypatch.setattr(intake_preflight_module, "_hash_from_fd", swapping_hash)

    result = inspect_intake_source(tmp_path)

    assert swapped["done"], "the swap hook never fired -- test setup is broken"
    if result.candidates:
        # Best-effort mtime-based detection missed this particular rename
        # (clock-tick granularity); the data itself must still be provably
        # from the original tree only -- never the replacement, never mixed.
        by_path = _by_path(result.candidates)
        assert by_path["datasets/nested/a.csv"].sha256 == hashlib.sha256(b"a,b\n1,2\n").hexdigest()
        assert by_path["datasets/nested/b.csv"].sha256 == hashlib.sha256(b"a,b\n3,4\n").hexdigest()
        replacement_b_sha = hashlib.sha256(b"a,b\nZZZ,ZZZ\n").hexdigest()
        assert by_path["datasets/nested/b.csv"].sha256 != replacement_b_sha
    else:
        assert result.errors == ({"path": "", "reason": "source-unreadable"},)
        assert result.review_items == ()


# --- blocking structural layouts ---------------------------------------------------------


def test_flat_source_layout_uses_fixed_root_path_never_none(tmp_path: Path) -> None:
    (tmp_path / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result) == {"": "flat-source-layout"}
    # Retained, but never AI-eligible.
    assert _by_path(result.candidates)["labs.csv"].component == "_unclassified"


def test_stray_root_file_alongside_known_directories_is_flat_source_layout_not_unknown_directory(
    tmp_path: Path,
) -> None:
    _make_canonical(tmp_path)
    (tmp_path / "mystery.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    review = _review_map(result)
    assert review[""] == "flat-source-layout"
    assert "mystery.csv" not in review
    assert "unknown-top-level-directory" not in review.values()
    assert _by_path(result.candidates)["mystery.csv"].component == "_unclassified"


def test_unknown_only_root_reports_both_flat_layout_and_unknown_directory(tmp_path: Path) -> None:
    (tmp_path / "extra").mkdir()
    (tmp_path / "extra" / "note.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    review = _review_map(result)
    assert review[""] == "flat-source-layout"
    assert review["extra"] == "unknown-top-level-directory"
    assert _by_path(result.candidates)["extra/note.csv"].component == "_unclassified"


def test_missing_required_directory_is_blocking_review(tmp_path: Path) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (tmp_path / "data_dictionary").mkdir()
    (tmp_path / "data_dictionary" / "dict.csv").write_text("code,label\n1,a\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["forms"] == "missing-component-directory"


def test_component_directory_present_but_empty_of_accepted_files_is_blocking_review(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    (tmp_path / "forms" / "readme.txt").write_text("no pdf here", encoding="utf-8")
    (tmp_path / "forms" / "consent.pdf").unlink()

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["forms"] == "missing-component-content"
    assert _by_path(result.candidates)["forms/readme.txt"].component == "_unclassified"


def test_malformed_xlsx_only_dataset_content_still_reports_missing_component_content(tmp_path: Path) -> None:
    # A directory that superficially "has a file" but whose only file fails
    # workbook validation must not count as satisfying the requirement.
    (tmp_path / "datasets").mkdir()
    (tmp_path / "forms").mkdir()
    (tmp_path / "mappings").mkdir()
    (tmp_path / "datasets" / "bad.xlsx").write_text("not a zip", encoding="utf-8")
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "mappings" / "map.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    review = _review_map(result)
    assert review["datasets"] == "missing-component-content"
    assert review["datasets/bad.xlsx"] == "xlsx-workbook-invalid"


def test_symlink_only_dataset_content_still_reports_missing_component_content(tmp_path: Path) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "forms").mkdir()
    (tmp_path / "mappings").mkdir()
    other = tmp_path / "elsewhere.csv"
    other.write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "datasets" / "linked.csv").symlink_to(other)
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "mappings" / "map.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    review = _review_map(result)
    assert review["datasets"] == "missing-component-content"
    assert review["datasets/linked.csv"] == "source-symlink-not-allowed"


def test_hardlink_only_support_content_still_reports_missing_component_content(tmp_path: Path) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "forms").mkdir()
    (tmp_path / "mappings").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4")
    os.link(tmp_path / "datasets" / "labs.csv", tmp_path / "mappings" / "aliased.csv")

    result = inspect_intake_source(tmp_path)

    review = _review_map(result)
    assert review["data_dictionary"] == "missing-component-directory"
    assert review["mappings"] == "missing-component-content"
    assert "cross-component-hardlink" in _reasons_for_path(result, "mappings/aliased.csv")


def test_neither_dictionary_nor_mappings_present_blocks_both(tmp_path: Path) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "forms").mkdir()
    (tmp_path / "datasets" / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")

    result = inspect_intake_source(tmp_path)

    review = _review_map(result)
    assert review["data_dictionary"] == "missing-component-directory"
    assert review["mappings"] == "missing-component-directory"


def test_dictionary_present_with_content_means_mappings_absence_is_not_flagged(tmp_path: Path) -> None:
    _make_canonical(tmp_path)

    result = inspect_intake_source(tmp_path)

    assert "mappings" not in _review_map(result)


def test_unknown_top_level_directory_is_blocking_review_and_files_retained_unclassified(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    (tmp_path / "extra_stuff").mkdir()
    (tmp_path / "extra_stuff" / "notes.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["extra_stuff"] == "unknown-top-level-directory"
    candidate = _by_path(result.candidates)["extra_stuff/notes.csv"]
    assert candidate.component == "_unclassified"
    assert candidate.source_component == "extra_stuff"


# --- closed format matrix -----------------------------------------------------------------


@pytest.mark.parametrize(
    "component,filename,content",
    [
        ("datasets", "notes.txt", b"plain text"),
        ("datasets", "consent.pdf", b"%PDF-1.4"),
        ("forms", "consent.docx", b"not a pdf"),
        ("forms", "data.csv", b"a,b\n1,2\n"),
        ("data_dictionary", "dict.txt", b"plain text"),
        ("data_dictionary", "legacy.xls", b"\xd0\xcf\x11\xe0"),
        ("mappings", "map.json", b"{}"),
        ("mappings", "legacy.xls", b"\xd0\xcf\x11\xe0"),
    ],
)
def test_unsupported_format_per_component_is_blocking_review(
    tmp_path: Path, component: str, filename: str, content: bytes
) -> None:
    _make_canonical(tmp_path)
    (tmp_path / component).mkdir(exist_ok=True)
    (tmp_path / component / filename).write_bytes(content)

    result = inspect_intake_source(tmp_path)

    rel = f"{component}/{filename}"
    assert _review_map(result)[rel] == "unsupported-format"
    assert _by_path(result.candidates)[rel].component == "_unclassified"


def test_xls_dataset_file_accepted_without_sheet_count_rule(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    xls_bytes = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 64  # not a real workbook; format-only acceptance
    (tmp_path / "datasets" / "legacy.xls").write_bytes(xls_bytes)

    result = inspect_intake_source(tmp_path)

    candidate = _by_path(result.candidates)["datasets/legacy.xls"]
    assert candidate.component == "datasets"
    assert candidate.sheet_count is None
    assert result.review_items == ()


# --- xlsx sheet-count policy ---------------------------------------------------------------


def test_dataset_xlsx_with_exactly_one_sheet_is_accepted(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    _write_workbook(tmp_path / "datasets" / "single.xlsx", sheet_names=["Only"])

    result = inspect_intake_source(tmp_path)

    candidate = _by_path(result.candidates)["datasets/single.xlsx"]
    assert candidate.component == "datasets"
    assert candidate.sheet_count == 1
    assert result.review_items == ()


def test_dataset_xlsx_with_multiple_sheets_becomes_unclassified_review(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    _write_workbook(tmp_path / "datasets" / "multi.xlsx", sheet_names=["S1", "S2", "S3"])

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/multi.xlsx"] == "dataset-xlsx-multiple-sheets"
    candidate = _by_path(result.candidates)["datasets/multi.xlsx"]
    assert candidate.component == "_unclassified"
    assert candidate.sheet_count == 3


def test_support_xlsx_within_max_sheets_is_accepted(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    (tmp_path / "mappings").mkdir(exist_ok=True)
    _write_workbook(tmp_path / "mappings" / "map.xlsx", sheet_names=[f"S{i}" for i in range(5)])

    result = inspect_intake_source(tmp_path)

    candidate = _by_path(result.candidates)["mappings/map.xlsx"]
    assert candidate.component == "mappings"
    assert candidate.sheet_count == 5


def test_support_xlsx_exceeding_max_sheets_fails_closed(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    max_sheets = DEFAULT_LIMITS["max_sheets"]
    names = [f"S{i}" for i in range(max_sheets + 2)]
    _write_workbook(tmp_path / "data_dictionary" / "huge.xlsx", sheet_names=names)

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["data_dictionary/huge.xlsx"] == "support-xlsx-sheet-limit"
    assert _by_path(result.candidates)["data_dictionary/huge.xlsx"].component == "_unclassified"


def test_malformed_xlsx_is_workbook_invalid_and_unclassified(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    (tmp_path / "datasets" / "corrupt.xlsx").write_bytes(b"not actually a zip file at all")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/corrupt.xlsx"] == "xlsx-workbook-invalid"
    assert _by_path(result.candidates)["datasets/corrupt.xlsx"].component == "_unclassified"


def test_xlsx_missing_workbook_member_is_workbook_invalid(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    bogus = tmp_path / "datasets" / "no_workbook.xlsx"
    with zipfile.ZipFile(bogus, "w") as zf:
        zf.writestr("hello.txt", "not a workbook")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/no_workbook.xlsx"] == "xlsx-workbook-invalid"


def test_xlsx_duplicate_workbook_member_is_workbook_invalid(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    bogus = tmp_path / "datasets" / "dup_member.xlsx"
    buf = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("xl/workbook.xml", "content1")
            zf.writestr("xl/workbook.xml", "content2")
    bogus.write_bytes(buf.getvalue())

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/dup_member.xlsx"] == "xlsx-workbook-invalid"


def test_xlsx_wrong_namespace_sheet_tag_never_counted_as_a_real_sheet(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    bogus_bytes = _raw_workbook_zip_bytes('<sheet name="S1" sheetId="1"/>', namespace="urn:evil")
    (tmp_path / "datasets" / "wrongns.xlsx").write_bytes(bogus_bytes)

    result = inspect_intake_source(tmp_path)

    # Zero real (correctly-namespaced) sheets found -> invalid, not silently
    # accepted as a 1-sheet dataset via a foreign-namespace tag match.
    assert _review_map(result)["datasets/wrongns.xlsx"] == "xlsx-workbook-invalid"
    assert _by_path(result.candidates)["datasets/wrongns.xlsx"].component == "_unclassified"


def test_xlsx_encrypted_member_normalizes_to_workbook_invalid_no_raw_text(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
    data = bytearray(buf.getvalue())
    local_sig = struct.pack("<I", 0x04034B50)
    central_sig = struct.pack("<I", 0x02014B50)
    idx = data.find(local_sig)
    flags = struct.unpack_from("<H", data, idx + 6)[0]
    struct.pack_into("<H", data, idx + 6, flags | 0x1)
    idx2 = data.find(central_sig)
    flags2 = struct.unpack_from("<H", data, idx2 + 8)[0]
    struct.pack_into("<H", data, idx2 + 8, flags2 | 0x1)
    (tmp_path / "datasets" / "encrypted.xlsx").write_bytes(bytes(data))

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/encrypted.xlsx"] == "xlsx-workbook-invalid"
    serialized = repr(result.review_items)
    assert "password" not in serialized.lower()
    assert "RuntimeError" not in serialized


def test_xlsx_zip_with_excessive_member_count_fails_closed(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="S1" sheetId="1"/></sheets></workbook>',
        )
        for i in range(DEFAULT_LIMITS["max_zip_members"] + 5):
            zf.writestr(f"junk/{i}.txt", "")
    (tmp_path / "datasets" / "manymembers.xlsx").write_bytes(buf.getvalue())

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/manymembers.xlsx"] == "xlsx-workbook-invalid"


def test_xlsx_non_workbook_member_over_per_member_cap_fails_closed(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="S1" sheetId="1"/></sheets></workbook>',
        )
        zf.writestr("xl/huge_other_member.xml", "x" * (DEFAULT_LIMITS["max_zip_member_bytes"] + 1024))
    (tmp_path / "datasets" / "hugemember.xlsx").write_bytes(buf.getvalue())

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/hugemember.xlsx"] == "xlsx-workbook-invalid"


# --- source symlink / hardlink / identity attacks ------------------------------------------


def test_source_file_symlink_in_component_dir_is_blocking_review_never_a_candidate_content(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    link = tmp_path / "datasets" / "linked.csv"
    link.symlink_to(tmp_path / "datasets" / "labs.csv")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/linked.csv"] == "source-symlink-not-allowed"
    assert "datasets/linked.csv" not in _by_path(result.candidates)


def test_nested_file_symlink_deep_inside_component_dir_is_blocking_review(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    nested = tmp_path / "datasets" / "site_a" / "visit_1"
    nested.mkdir(parents=True)
    (nested / "linked.csv").symlink_to(tmp_path / "datasets" / "labs.csv")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/site_a/visit_1/linked.csv"] == "source-symlink-not-allowed"
    assert "datasets/site_a/visit_1/linked.csv" not in _by_path(result.candidates)


def test_nested_directory_symlink_inside_component_dir_is_blocking_review_and_not_walked(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    real_nested = tmp_path / "backing_nested"
    real_nested.mkdir()
    (real_nested / "hidden.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "datasets" / "linked_dir").symlink_to(real_nested)

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/linked_dir"] == "source-symlink-not-allowed"
    assert not any(c.relative_path.startswith("datasets/linked_dir/") for c in result.candidates)


def test_hidden_named_directory_symlink_is_still_reported_not_silently_dropped(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    real_nested = tmp_path / "backing_hidden"
    real_nested.mkdir()
    (real_nested / "x.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "datasets" / ".hidden_link").symlink_to(real_nested)

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/.hidden_link"] == "source-symlink-not-allowed"


def test_hidden_named_file_symlink_is_still_reported_not_silently_dropped(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    (tmp_path / "datasets" / ".hidden_link.csv").symlink_to(tmp_path / "datasets" / "labs.csv")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets/.hidden_link.csv"] == "source-symlink-not-allowed"


def test_source_directory_symlink_at_top_level_is_blocking_review_and_not_walked(tmp_path: Path) -> None:
    real = tmp_path / "backing_store"
    real.mkdir()
    (real / "labs.csv").write_text("SUBJID,AGE\n1,40\n", encoding="utf-8")
    (tmp_path / "forms").mkdir()
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4\n%%EOF")
    (tmp_path / "mappings").mkdir()
    (tmp_path / "mappings" / "map.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    (tmp_path / "datasets").symlink_to(real)

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["datasets"] == "source-symlink-not-allowed"
    assert not any(c.relative_path.startswith("datasets/") for c in result.candidates)
    assert not any(c.source_component == "datasets" for c in result.candidates)
    by_path = _by_path(result.candidates)
    assert by_path["backing_store/labs.csv"].component == "_unclassified"


def test_hardlink_from_forms_to_dataset_file_is_quarantined(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    os.link(tmp_path / "datasets" / "labs.csv", tmp_path / "forms" / "aliased.pdf")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["forms/aliased.pdf"] == "cross-component-hardlink"
    candidate = _by_path(result.candidates)["forms/aliased.pdf"]
    assert candidate.component == "_unclassified"
    assert _by_path(result.candidates)["datasets/labs.csv"].component == "datasets"


def test_hardlink_from_data_dictionary_to_dataset_file_is_quarantined(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    os.link(tmp_path / "datasets" / "labs.csv", tmp_path / "data_dictionary" / "aliased.csv")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["data_dictionary/aliased.csv"] == "cross-component-hardlink"
    assert _by_path(result.candidates)["data_dictionary/aliased.csv"].component == "_unclassified"


def test_hardlink_from_mappings_to_dataset_file_is_quarantined(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    (tmp_path / "mappings").mkdir()
    (tmp_path / "mappings" / "map.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    os.link(tmp_path / "datasets" / "labs.csv", tmp_path / "mappings" / "aliased.csv")

    result = inspect_intake_source(tmp_path)

    assert _review_map(result)["mappings/aliased.csv"] == "cross-component-hardlink"
    assert _by_path(result.candidates)["mappings/aliased.csv"].component == "_unclassified"


def test_hardlink_quarantine_applies_even_when_the_support_file_would_otherwise_be_unclassified(tmp_path: Path) -> None:
    # An unsupported-suffix support file that is ALSO a dataset hardlink must
    # still be reported as the hardlink, in addition to its unsupported-format
    # finding -- the aliasing is a distinct, independently-reported concern.
    _make_canonical(tmp_path)
    real = tmp_path / "datasets" / "real_data"
    real.write_bytes(b"binary-ish content")
    os.link(real, tmp_path / "forms" / "aliased.bin")

    result = inspect_intake_source(tmp_path)

    assert "cross-component-hardlink" in _reasons_for_path(result, "forms/aliased.bin")
    assert _by_path(result.candidates)["forms/aliased.bin"].component == "_unclassified"


def test_hardlink_within_datasets_is_not_quarantined(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    os.link(tmp_path / "datasets" / "labs.csv", tmp_path / "datasets" / "labs_alias.csv")

    result = inspect_intake_source(tmp_path)

    assert "cross-component-hardlink" not in _reasons(result.review_items)
    assert _by_path(result.candidates)["datasets/labs_alias.csv"].component == "datasets"


def test_source_unreadable_file_is_error_not_candidate_or_review(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    unreadable = tmp_path / "datasets" / "noperm.csv"
    unreadable.write_text("SUBJID,AGE\n2,50\n", encoding="utf-8")
    os.chmod(unreadable, 0o000)
    try:
        result = inspect_intake_source(tmp_path)
    finally:
        os.chmod(unreadable, 0o600)

    assert _error_map(result).get("datasets/noperm.csv") == "source-unreadable"
    assert "datasets/noperm.csv" not in _by_path(result.candidates)


def test_unreadable_nested_subtree_blocks_rather_than_silently_omits(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    blocked = tmp_path / "datasets" / "blocked"
    blocked.mkdir()
    (blocked / "unseen.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    os.chmod(blocked, 0o000)
    try:
        result = inspect_intake_source(tmp_path)
    finally:
        os.chmod(blocked, 0o700)

    assert _error_map(result).get("datasets/blocked") == "source-unreadable"
    assert not any("unseen.csv" in c.relative_path for c in result.candidates)


def test_deeply_nested_source_fails_closed_without_recursion_error(tmp_path: Path) -> None:
    (tmp_path / "datasets").mkdir()
    (tmp_path / "forms").mkdir()
    (tmp_path / "forms" / "consent.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "mappings").mkdir()
    (tmp_path / "mappings" / "map.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    deep = tmp_path / "datasets"
    for i in range(200):
        deep = deep / f"s{i}"
        deep.mkdir()
    (deep / "unreachable.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    result = inspect_intake_source(tmp_path)  # must not raise RecursionError

    assert not any("unreachable.csv" in c.relative_path for c in result.candidates)
    assert "source-unreadable" in _reasons(result.errors)


# --- real TOCTOU during preflight's own read ------------------------------------------------


def test_mutation_during_hash_is_rejected_not_a_stale_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _make_canonical(tmp_path)
    target = tmp_path / "datasets" / "labs.csv"
    original_size = target.stat().st_size
    real_hash = intake_preflight_module._hash_from_fd
    mutated = {"done": False}

    def mutating_hash(fd: int) -> str:
        result = real_hash(fd)
        # _hash_from_fd is called once per candidate file; only mutate the
        # specific target's own hash call (identified by its distinct
        # original size, since all three canonical files differ in size),
        # and only once -- otherwise a naive "mutate on every call" would
        # rewrite the target's already-mutated content again during a LATER
        # file's unrelated hash call, converging to identical bytes/mtime
        # and making the mutation invisible to any stat-based check.
        if not mutated["done"] and os.fstat(fd).st_size == original_size:
            mutated["done"] = True
            target.write_text("SUBJID,AGE\n1,40\nMUTATED,ROW\n", encoding="utf-8")
        return result

    monkeypatch.setattr(intake_preflight_module, "_hash_from_fd", mutating_hash)

    result = inspect_intake_source(tmp_path)

    assert mutated["done"], "mutation hook never fired -- test setup is broken"
    assert "datasets/labs.csv" not in _by_path(result.candidates)
    assert _error_map(result).get("datasets/labs.csv") == "source-unreadable"
    # No stale record of the pre-mutation identity/hash survives anywhere.
    for candidate in result.candidates:
        assert candidate.relative_path != "datasets/labs.csv"


def test_mutation_during_xlsx_sheet_count_is_rejected_not_a_stale_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_canonical(tmp_path)
    target = tmp_path / "data_dictionary" / "dict.xlsx"
    real_count = intake_preflight_module.count_xlsx_sheets

    def mutating_count(stream):
        result = real_count(stream)
        target.write_bytes(target.read_bytes() + b"\x00")
        return result

    monkeypatch.setattr(intake_preflight_module, "count_xlsx_sheets", mutating_count)

    result = inspect_intake_source(tmp_path)

    assert "data_dictionary/dict.xlsx" not in _by_path(result.candidates)
    assert _error_map(result).get("data_dictionary/dict.xlsx") == "source-unreadable"


# --- reason records stay value-free ---------------------------------------------------------


def test_review_and_error_records_never_include_content_or_raw_exception_text(tmp_path: Path) -> None:
    _make_canonical(tmp_path)
    secret_marker = "TOTALLY_SECRET_PHI_VALUE"
    (tmp_path / "datasets" / "bad.xlsx").write_text(f"corrupt-{secret_marker}", encoding="utf-8")
    link = tmp_path / "datasets" / "linked.csv"
    link.symlink_to(tmp_path / "datasets" / "labs.csv")

    result = inspect_intake_source(tmp_path)

    serialized = repr(result.review_items) + repr(result.errors)
    assert secret_marker not in serialized
    fixed_codes = {
        "flat-source-layout",
        "unknown-top-level-directory",
        "missing-component-directory",
        "missing-component-content",
        "unsupported-format",
        "dataset-xlsx-multiple-sheets",
        "support-xlsx-sheet-limit",
        "source-unreadable",
        "source-target-outside-root",
        "source-symlink-not-allowed",
        "cross-component-hardlink",
        "xlsx-workbook-invalid",
    }
    for item in list(result.review_items) + list(result.errors):
        assert item["reason"] in fixed_codes
        assert isinstance(item["path"], str)  # never None, always a fixed string


def test_no_llm_dispatch_boundary_is_ever_called_during_preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Behavioral dispatch spy: hook the actual configured model-client
    # boundary this repository uses (phi_engine.config.config.get_llm_client
    # / LLMClient.complete) so any call raises immediately, then run a full
    # preflight (including the xlsx path) and prove zero calls occurred.
    import phi_engine.config.config as config_module

    def _forbidden_get_llm_client(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("preflight must never call get_llm_client()")

    def _forbidden_complete(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("preflight must never call LLMClient.complete()")

    monkeypatch.setattr(config_module, "get_llm_client", _forbidden_get_llm_client)
    monkeypatch.setattr(config_module.LLMClient, "complete", _forbidden_complete)

    _make_canonical(tmp_path)
    result = inspect_intake_source(tmp_path)
    assert result.review_items == ()
    assert result.errors == ()


# --- count_xlsx_sheets direct contract ------------------------------------------------------


def test_count_xlsx_sheets_counts_sheet_elements(tmp_path: Path) -> None:
    path = tmp_path / "book.xlsx"
    _write_workbook(path, sheet_names=["A", "B", "C"])
    with path.open("rb") as fh:
        assert count_xlsx_sheets(fh) == 3


def test_count_xlsx_sheets_rejects_bad_zip() -> None:
    stream = io.BytesIO(b"definitely not a zip archive")
    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(stream)
    assert excinfo.value.reason == "xlsx-workbook-invalid"


def test_count_xlsx_sheets_normalizes_a_failing_reset_seek_no_raw_text() -> None:
    # Regression: the fileobj.seek(0, os.SEEK_SET) reset that precedes the
    # bounded ZipFile construction must be inside the hostile-input
    # normalization boundary, not before it -- a filesystem-level failure
    # on that specific call must never escape as a raw OSError.
    class _FailingSeekStream:
        # Let _stream_size's own bounded seek/tell probing succeed
        # (tell, seek(0, SEEK_END), tell, seek(current, SEEK_SET) -- four
        # calls), then fail on the NEXT seek: the count_xlsx_sheets reset
        # at `fileobj.seek(0, os.SEEK_SET)` this regression targets.
        def __init__(self) -> None:
            self._seek_calls = 0

        def tell(self) -> int:
            return 0

        def seek(self, offset: int, whence: int = 0) -> int:
            self._seek_calls += 1
            if self._seek_calls > 2:
                raise OSError(5, "SENTINEL_RAW_SEEK_EIO")
            return 0

        def read(self, size: int = -1) -> bytes:
            return b""

    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(_FailingSeekStream())
    assert excinfo.value.reason == "xlsx-workbook-invalid"
    assert "SENTINEL_RAW_SEEK_EIO" not in str(excinfo.value)


def test_count_xlsx_sheets_rejects_oversized_workbook_member(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook>" + ("x" * 200) + "</workbook>")
    buf.seek(0)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_zip_member_bytes", 16)
    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(buf)
    assert excinfo.value.reason == "xlsx-workbook-invalid"


def test_count_xlsx_sheets_rejects_over_decompression_ratio_zip_bomb(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    payload = b"0" * (5 * 1024 * 1024)
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", payload)
    buf.seek(0)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_decompression_ratio", 2)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_zip_member_bytes", 64 * 1024 * 1024)
    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(buf)
    assert excinfo.value.reason == "xlsx-workbook-invalid"


def test_count_xlsx_sheets_rejects_source_over_max_source_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="S1" sheetId="1"/></sheets></workbook>',
        )
    buf.seek(0)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_source_bytes", 4)
    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(buf)
    assert excinfo.value.reason == "xlsx-workbook-invalid"


def test_count_xlsx_sheets_rejects_expanded_total_over_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="S1" sheetId="1"/></sheets></workbook>',
        )
        zf.writestr("xl/other.xml", "y" * 5000)
    buf.seek(0)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_zip_member_bytes", 1024 * 1024)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_expanded_workbook_bytes", 1000)
    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(buf)
    assert excinfo.value.reason == "xlsx-workbook-invalid"


def test_count_xlsx_sheets_rejects_central_directory_over_max_zip_directory_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Public-API proof that the archive's central directory itself is
    # bounded: an archive far smaller than max_source_bytes, with far fewer
    # entries than max_zip_members, is still rejected once its central
    # directory metadata alone would require more than max_zip_directory_bytes
    # to read -- the cap fires while zipfile is still parsing, not only
    # after full central-directory materialization succeeds.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="S1" sheetId="1"/></sheets></workbook>',
        )
        for i in range(50):
            zf.writestr(f"padding/{'x' * 200}_{i}.txt", "")
    buf.seek(0)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_zip_directory_bytes", 512)
    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(buf)
    assert excinfo.value.reason == "xlsx-workbook-invalid"


def test_count_xlsx_sheets_accepts_a_valid_member_larger_than_the_directory_cap() -> None:
    # Positive boundary proof for the rearm fix: the construction-only
    # max_zip_directory_bytes budget bounds ZipFile's own central-directory
    # parse, but must NOT still be in effect once the workbook member is
    # actually streamed -- a workbook whose central directory is tiny (one
    # member) but whose compressed member content exceeds the directory cap
    # must be accepted when every approved source/member/expanded/ratio
    # limit independently passes.
    import base64
    import random

    # Deterministic, low-compressibility filler: a fixed-seed PRNG gives
    # the same bytes on every run (unlike os.urandom) while remaining
    # high-entropy enough that DEFLATE cannot shrink it below the member
    # cap this test depends on.
    padding = base64.b64encode(random.Random(20240115).randbytes(4_500_000)).decode("ascii")
    xml = (
        '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<!--{padding}--><sheets><sheet name=\"S1\" sheetId=\"1\"/></sheets></workbook>"
    )
    encoded = xml.encode("utf-8")
    assert len(encoded) > DEFAULT_LIMITS["max_zip_directory_bytes"], "test payload must exceed the directory cap"
    assert len(encoded) <= DEFAULT_LIMITS["max_zip_member_bytes"]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/workbook.xml", xml)
    buf.seek(0)

    assert count_xlsx_sheets(buf) == 1


def test_count_xlsx_sheets_stops_early_past_max_sheets(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    sheets_xml = "".join(f'<sheet name="S{i}" sheetId="{i}" r:id="rId{i}"/>' for i in range(200))
    buf.write(_raw_workbook_zip_bytes(sheets_xml))
    buf.seek(0)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_sheets", 10)
    count = count_xlsx_sheets(buf)
    assert count > 10


def test_count_xlsx_sheets_rejects_excessive_member_count(monkeypatch: pytest.MonkeyPatch) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="S1" sheetId="1"/></sheets></workbook>',
        )
        for i in range(50):
            zf.writestr(f"junk/{i}.txt", "")
    buf.seek(0)
    monkeypatch.setitem(DEFAULT_LIMITS, "max_zip_members", 10)
    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(buf)
    assert excinfo.value.reason == "xlsx-workbook-invalid"


def test_count_xlsx_sheets_normalizes_corrupt_deflate_stream(tmp_path: Path) -> None:
    # Flip a byte inside the compressed data region of a validly-structured
    # DEFLATE member so decompression itself fails with zlib.error, proving
    # that failure is normalized rather than escaping raw.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="S1" sheetId="1"/></sheets></workbook>' + ("padding" * 200),
        )
    data = bytearray(buf.getvalue())
    # Corrupt a byte well past the local file header, inside the compressed
    # payload, without touching any ZIP structural fields.
    data[80] ^= 0xFF
    corrupt = io.BytesIO(bytes(data))
    with pytest.raises(Exception) as excinfo:
        count_xlsx_sheets(corrupt)
    assert excinfo.value.reason == "xlsx-workbook-invalid"
    assert "zlib" not in str(excinfo.value).lower()


# --- moved walker convention (single source, no duplicate walker) ---------------------------


def test_iter_source_files_is_deterministic_and_filters_junk(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.csv").write_text("x", encoding="utf-8")
    (tmp_path / "a.csv").write_text("x", encoding="utf-8")
    (tmp_path / ".hidden.csv").write_text("x", encoding="utf-8")
    (tmp_path / "Thumbs.db").write_text("x", encoding="utf-8")

    files = intake_preflight_module._iter_source_files(tmp_path)

    relative = [f.relative_to(tmp_path).as_posix() for f in files]
    assert relative == ["a.csv", "sub/b.csv"]


def test_iter_source_files_includes_nonhidden_symlinked_files_preserving_legacy_intake_behavior(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real.csv"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "linked.csv"
    link.symlink_to(real)

    files = intake_preflight_module._iter_source_files(tmp_path)

    relative = {f.relative_to(tmp_path).as_posix() for f in files}
    assert relative == {"real.csv", "linked.csv"}


def test_iter_source_files_filters_the_complete_junk_catalog_including_nested_directories(tmp_path: Path) -> None:
    from phi_engine.utils._extraction_io.file_discovery import DEFAULT_JUNK_FILENAMES

    (tmp_path / "keep.csv").write_text("x", encoding="utf-8")
    for junk_name in sorted(DEFAULT_JUNK_FILENAMES):
        (tmp_path / junk_name).write_text("x", encoding="utf-8")
    junk_dir_name = sorted(DEFAULT_JUNK_FILENAMES)[0]
    nested_junk_dir = tmp_path / "sub" / junk_dir_name
    nested_junk_dir.mkdir(parents=True)
    (nested_junk_dir / "unreachable.csv").write_text("x", encoding="utf-8")
    (tmp_path / "sub" / "keep_nested.csv").write_text("x", encoding="utf-8")

    files = intake_preflight_module._iter_source_files(tmp_path)

    relative = {f.relative_to(tmp_path).as_posix() for f in files}
    assert relative == {"keep.csv", "sub/keep_nested.csv"}


def test_inspect_intake_source_filters_the_complete_junk_catalog_including_nested_directories(tmp_path: Path) -> None:
    from phi_engine.utils._extraction_io.file_discovery import DEFAULT_JUNK_FILENAMES

    _make_canonical(tmp_path)
    for junk_name in sorted(DEFAULT_JUNK_FILENAMES):
        (tmp_path / "datasets" / junk_name).write_text("x", encoding="utf-8")
    junk_dir_name = sorted(DEFAULT_JUNK_FILENAMES)[0]
    nested_junk_dir = tmp_path / "datasets" / "sub" / junk_dir_name
    nested_junk_dir.mkdir(parents=True)
    (nested_junk_dir / "unreachable.csv").write_text("x", encoding="utf-8")

    result = inspect_intake_source(tmp_path)

    assert result.review_items == ()
    assert result.errors == ()
    relative = {c.relative_path for c in result.candidates}
    assert relative == {"datasets/labs.csv", "forms/consent.pdf", "data_dictionary/dict.xlsx"}
