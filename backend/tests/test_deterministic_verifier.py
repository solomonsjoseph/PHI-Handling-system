"""Tests for DeterministicVerifier (Phase 10, docs #54), migrated from the
retired ``agents/operator.py::Operator``'s own test suite
(``tests/test_operator.py``, Task 27/28). Every behavioral check Operator
used to guarantee -- shape checks, source-value comparison, reverse
completeness, omit_by_file handling, corrupt-file isolation, fail-closed
scrub_text -- is preserved unchanged; only the call surface differs since
``DeterministicVerifier`` is a plain class, not an ``Agent`` (no
``AgentContext``/``make_ctx`` needed to construct or call it).

The one Operator test NOT migrated here (`test_agent_log_row_emitted_per_
batch`) checked Operator's own `Agent`-based `self._log` calls via
`op.ctx.trace.legacy_messages` -- infrastructure `DeterministicVerifier`
does not have, since it is not an `Agent` and does not use
`agents.batching.run_batched` for its (now sandboxable) verification
loop. See docs/PHASE_STATUS.md's `### DELETED_TESTS` section for the
recorded deletion.
"""
from __future__ import annotations

import asyncio
import csv
from pathlib import Path

import pytest
from phi_core.control.deterministic_verifier import DeterministicVerifier
from phi_core.control.transform_primitives import PseudonymRegistry, _scrub_text_cell, apply_column_actions_to_dataset


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def _dataset_file(file_id: str, stored_path: str | None = None) -> dict:
    d = {"file_id": file_id, "kind": "dataset", "subtype": "csv"}
    if stored_path is not None:
        d["stored_path"] = stored_path
    return d


def _run(files, decisions, exports, omit_by_file=None):
    return asyncio.run(DeterministicVerifier().run(files, decisions, exports, omit_by_file))


def test_all_action_types_pass_against_a_real_executor_export(tmp_path):
    """One passing case per action type, checked against an export written
    by the real Executor transform (not hand-built), so the shape checks
    are proven against Executor's actual output encoding."""
    header = ["id", "ssn", "age", "dob", "zip", "mrn", "name", "notes"]
    rows = [
        ["1", "123-45-6789", "45", "1980-05-01", "90210",
         "abc123", "Jane Doe", "Contact patient at john.smith@example.com for follow up."],
        ["2", "987-65-4321", "96", "1975-02-14", "10001-1234",
         "def456", "Jane Doe", "Patient reports feeling better this week."],
    ]
    src = tmp_path / "dataset.csv"
    _write_csv(src, header, rows)
    dst = tmp_path / "dataset_out.csv"

    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
        {"file_id": "f1", "column": "age", "action": "cap_age_90", "phi_category": "C",
         "citation": "45 CFR 164.514(b)(2)(i)(C)"},
        {"file_id": "f1", "column": "dob", "action": "year_only", "phi_category": "C",
         "citation": "45 CFR 164.514(b)(2)(i)(C)"},
        {"file_id": "f1", "column": "zip", "action": "zip3_truncate", "phi_category": "B",
         "citation": "45 CFR 164.514(b)(2)(i)(B)"},
        {"file_id": "f1", "column": "mrn", "action": "hash", "phi_category": "H",
         "citation": "45 CFR 164.514(b)(2)(i)(H)"},
        {"file_id": "f1", "column": "name", "action": "pseudonymize", "phi_category": "A",
         "citation": "45 CFR 164.514(b)(2)(i)(A)"},
        {"file_id": "f1", "column": "notes", "action": "scrub_text", "phi_category": "A",
         "citation": "45 CFR 164.514(b)(2)(i)(A)"},
    ]
    apply_column_actions_to_dataset(src, dst, "csv", decisions, PseudonymRegistry(salt="test-salt"))

    files = [_dataset_file("f1", str(src))]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    assert result["failed_file_ids"] == []
    by_col = {v["column"]: v for v in result["verdicts"]}
    for col in header:
        assert by_col[col]["verdict"] == "pass", (col, by_col[col])
    assert result["status"] == "clean"
    assert result["checksums"]["f1"]
    assert result["file_counts"] == {"datasets_expected": 1, "datasets_readable": 1}
    assert result["schema_valid"] == {"f1": True}


def test_cap_age_90_wrong_value_passes_shape_but_fails_source_comparison(tmp_path):
    """D13 plan step 6: a shape check alone cannot catch a well-formed but
    wrong age cap. Source age 96 must become '90+'; a corrupted export that
    writes a different, still-valid-shaped age ('89') passes the old shape
    check and must now be caught by the source comparison."""
    src = tmp_path / "in.csv"
    _write_csv(src, ["age"], [["96"]])
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["age"], [["89"]])  # valid shape (0-89), wrong: source caps to '90+'
    files = [_dataset_file("f1", str(src))]
    decisions = [{"file_id": "f1", "column": "age", "action": "cap_age_90",
                  "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert "96" not in v["problem"]
    assert "89" not in v["problem"]
    assert any(c["name"] == "shape" and c["pass"] for c in v["checks"])
    assert any(c["name"] == "source_value_match" and not c["pass"] for c in v["checks"])
    assert result["status"] == "issues"


def test_year_only_wrong_value_fails_source_comparison(tmp_path):
    src = tmp_path / "in.csv"
    _write_csv(src, ["dob"], [["1980-05-01"]])
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["dob"], [["1981"]])  # valid shape (4 digits), wrong year
    files = [_dataset_file("f1", str(src))]
    decisions = [{"file_id": "f1", "column": "dob", "action": "year_only",
                  "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert any(c["name"] == "source_value_match" and not c["pass"] for c in v["checks"])


def test_zip3_truncate_wrong_value_fails_source_comparison(tmp_path):
    src = tmp_path / "in.csv"
    _write_csv(src, ["zip"], [["90210"]])
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["zip"], [["903"]])  # valid shape (3 digits), wrong: source truncates to '902'
    files = [_dataset_file("f1", str(src))]
    decisions = [{"file_id": "f1", "column": "zip", "action": "zip3_truncate",
                  "phi_category": "B", "citation": "45 CFR 164.514(b)(2)(i)(B)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert any(c["name"] == "source_value_match" and not c["pass"] for c in v["checks"])


def test_pseudonymize_inconsistent_mapping_fails_source_comparison(tmp_path):
    """D13 plan step 6: equal source values must map to equal pseudonyms,
    and distinct source values must never collide. Both violations are
    checked without the verifier ever knowing the registry's salt or
    algorithm -- only the mapping's shape."""
    src = tmp_path / "in.csv"
    _write_csv(src, ["name"], [["Jane Doe"], ["Jane Doe"]])
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["name"], [["Paaaaaaaa"], ["Pbbbbbbbb"]])  # same source, different pseudonyms
    files = [_dataset_file("f1", str(src))]
    decisions = [{"file_id": "f1", "column": "name", "action": "pseudonymize",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert "Jane Doe" not in v["problem"]
    assert any(c["name"] == "source_value_match" and not c["pass"] for c in v["checks"])


def test_cap_age_90_without_source_still_passes_on_shape_alone(tmp_path):
    """Plan step 6 says 'keep every existing check' -- when no stored_path
    is available the value comparison cannot run at all, and the verifier
    falls back to the pre-existing shape-only check rather than failing
    closed (unlike scrub_text, which has always required source)."""
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["age"], [["90+"]])
    files = [_dataset_file("f1")]  # no stored_path
    decisions = [{"file_id": "f1", "column": "age", "action": "cap_age_90",
                  "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "pass"
    assert [c["name"] for c in v["checks"]] == ["column_presence", "shape"]
    assert result["status"] == "clean"


def test_cap_age_90_shape_violation_is_caught(tmp_path):
    """A corrupted export (age never capped) is caught, and the raw
    offending value never appears in the reported problem text."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["age"], [["96"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "age", "action": "cap_age_90",
                  "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)"}]
    exports = {"f1": str(src)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert "96" not in v["problem"]
    assert result["status"] == "issues"


def test_decision_for_nonexistent_column_is_flagged(tmp_path):
    """Finding 12: a stale/misspelled column name from Judge/Sentinel is
    surfaced rather than silently ignored. The written file's only real
    column ('id') has no decision either, so reverse completeness also
    flags it as undecided."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id"], [["1"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "ssn", "action": "drop",
                  "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)"}]
    exports = {"f1": str(src)}

    result = _run(files, decisions, exports)

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["ssn"]["verdict"] == "fail"
    assert "no corresponding column" in by_col["ssn"]["problem"]
    assert by_col["id"]["verdict"] == "fail"
    assert by_col["id"]["method"] == "undecided"
    assert result["status"] == "issues"


def test_drop_column_left_populated_fails(tmp_path):
    src = tmp_path / "out.csv"
    _write_csv(src, ["ssn"], [["123-45-6789"], [""]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "ssn", "action": "drop",
                  "phi_category": "G", "citation": "45 CFR 164.514(b)(2)(i)(G)"}]
    exports = {"f1": str(src)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert v["problem"] == "drop column left populated"


def test_scrub_text_cell_preserves_adjacent_markup():
    """Regression test for reasoning.py finding 4 (already fixed): a
    detected PHI span must not eat into adjacent markup. This proves the
    existing `_scrub_text_cell` behavior the verifier relies on rather
    than re-checking, not new verifier logic."""
    text = "<b>John Smith</b> <a href='mailto:x@y.com'>"
    out = _scrub_text_cell(text)
    assert "</b>" in out
    assert "John Smith" not in out


def test_omit_by_file_column_expected_absent_passes(tmp_path):
    src = tmp_path / "out.csv"
    _write_csv(src, ["id"], [["1"]])  # 'notes' entirely absent, as omit_by_file specifies
    files = [_dataset_file("f1")]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "notes", "action": "scrub_text", "phi_category": "A",
         "citation": "45 CFR 164.514(b)(2)(i)(A)"},
    ]
    exports = {"f1": str(src)}
    omit_by_file = {"f1": {"notes"}}

    result = _run(files, decisions, exports, omit_by_file)

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["notes"]["verdict"] == "pass"
    assert by_col["notes"]["performed"] == "column omitted as expected"
    assert result["status"] == "clean"


def test_omit_by_file_column_present_when_expected_absent_fails(tmp_path):
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "notes"], [["1", "leaked text"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(src)}
    omit_by_file = {"f1": {"notes"}}

    result = _run(files, decisions, exports, omit_by_file)

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["notes"]["verdict"] == "fail"
    assert by_col["notes"]["problem"] == "column was supposed to be omitted but is present in output"
    # 'id' has no decision either and is not omitted -- reverse completeness.
    assert by_col["id"]["verdict"] == "fail"
    assert by_col["id"]["method"] == "undecided"


def test_missing_export_file_fails_every_decision(tmp_path):
    files = [_dataset_file("f1")]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
    ]
    exports: dict[str, str] = {}  # Executor never wrote f1

    result = _run(files, decisions, exports)

    assert result["failed_file_ids"] == ["f1"]
    assert len(result["verdicts"]) == 2
    assert all(v["verdict"] == "fail" for v in result["verdicts"])
    assert all(v["checks"] == [] for v in result["verdicts"])
    assert all("missing from exports or could not be read" in v["problem"] for v in result["verdicts"])
    assert result["status"] == "issues"
    assert result["file_counts"] == {"datasets_expected": 1, "datasets_readable": 0}
    assert result["schema_valid"] == {"f1": False}


def test_non_dataset_file_decisions_are_out_of_scope(tmp_path):
    """Metadata/narrative files never carry per-column decisions in this
    pipeline; the verifier must not invent a verdict for one."""
    files = [{"file_id": "f1", "kind": "metadata", "subtype": "csv"}]
    decisions = [{"file_id": "f1", "column": "code", "action": "keep",
                  "phi_category": "NONE", "citation": ""}]
    exports = {"f1": str(tmp_path / "does_not_matter.csv")}

    result = _run(files, decisions, exports)

    assert result["verdicts"] == []
    assert result["failed_file_ids"] == []
    assert result["status"] == "clean"


def test_undecided_written_column_is_flagged(tmp_path):
    """Reverse completeness: a column present in the written output with
    no matching decision and no omit_by_file entry is invisible to
    Judge/Sentinel and must be surfaced, not silently accepted."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "extra"], [["1", "leftover value"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "id", "action": "keep",
                  "phi_category": "NONE", "citation": ""}]
    exports = {"f1": str(src)}

    result = _run(files, decisions, exports)

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["id"]["verdict"] == "pass"
    assert by_col["extra"]["verdict"] == "fail"
    assert by_col["extra"]["method"] == "undecided"
    assert by_col["extra"]["violation"] == {}
    assert result["status"] == "issues"


@pytest.mark.parametrize("action,bad_value", [
    ("year_only", "198"),
    ("zip3_truncate", "9021"),
    ("hash", "not-a-hash"),
    ("pseudonymize", "JaneDoe"),
])
def test_transform_shape_violations_are_caught(tmp_path, action, bad_value):
    """A shape violation is caught for every transform action, not just
    cap_age_90, and the raw offending value never leaks into the problem
    text."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["col"], [[bad_value]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "col", "action": action,
                  "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)"}]
    exports = {"f1": str(src)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert bad_value not in v["problem"]
    assert action in v["problem"]


def test_scrub_text_no_change_from_source_fails(tmp_path):
    """The verifier itself catches a scrub_text column that never actually
    changed, not merely `_scrub_text_cell` in isolation."""
    src = tmp_path / "in.csv"
    _write_csv(src, ["notes"], [["Patient contacted at john@example.com"]])
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["notes"], [["Patient contacted at john@example.com"]])  # scrub never ran
    files = [_dataset_file("f1", str(src))]
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["notes"]["verdict"] == "fail"
    assert by_col["notes"]["problem"] == "scrub_text produced no observable change"


def test_scrub_text_missing_stored_path_fails_closed(tmp_path):
    """No stored_path at all -- the verifier cannot compare against a
    source it was never given, so it fails closed rather than passing
    vacuously."""
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["notes"], [["anything, doesn't matter"]])
    files = [_dataset_file("f1")]  # no stored_path
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert v["problem"] == "cannot verify scrub_text ran"


def test_scrub_text_unreadable_source_fails_closed(tmp_path):
    """stored_path points at a file that cannot be read -- same fail-closed
    outcome as a missing path, never a silent pass."""
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["notes"], [["anything, doesn't matter"]])
    files = [_dataset_file("f1", str(tmp_path / "does_not_exist.csv"))]
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    v = result["verdicts"][0]
    assert v["verdict"] == "fail"
    assert v["problem"] == "cannot verify scrub_text ran"


def test_corrupt_written_file_isolated_from_sibling_files(tmp_path):
    """A read failure on one file's written export (unsupported extension
    here, deterministically triggers the read failure) is isolated to that
    file_id -- it lands in failed_file_ids without crashing verification
    of a sibling file that reads cleanly."""
    good_src = tmp_path / "good.csv"
    _write_csv(good_src, ["id"], [["1"]])
    files = [
        _dataset_file("f1"),
        {"file_id": "f2", "kind": "dataset", "subtype": "weird"},
    ]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f2", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
    ]
    exports = {"f1": str(good_src), "f2": str(tmp_path / "bad.weird")}

    result = _run(files, decisions, exports)

    assert result["failed_file_ids"] == ["f2"]
    by_file_col = {(v["file_id"], v["column"]): v for v in result["verdicts"]}
    assert by_file_col[("f1", "id")]["verdict"] == "pass"
    assert by_file_col[("f2", "id")]["verdict"] == "fail"
    assert "missing from exports or could not be read" in by_file_col[("f2", "id")]["problem"]
    assert result["status"] == "issues"


def test_unknown_file_id_decision_is_flagged(tmp_path):
    """A decision naming a file_id the verifier has never heard of (not in
    `files` at all) is the file-level analog of finding 12 -- surfaced as
    a failure rather than silently dropped."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id"], [["1"]])
    files = [_dataset_file("f1")]  # only f1 known
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "ghost", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
    ]
    exports = {"f1": str(src)}

    result = _run(files, decisions, exports)

    assert "ghost" in result["failed_file_ids"]
    by_file_col = {(v["file_id"], v["column"]): v for v in result["verdicts"]}
    assert by_file_col[("ghost", "ssn")]["verdict"] == "fail"
    assert by_file_col[("f1", "id")]["verdict"] == "pass"
    assert result["status"] == "issues"


def test_scrub_text_column_with_nothing_to_scrub_passes(tmp_path):
    """A scrub_text column whose source never contained anything
    detectable is correctly unchanged from source -- must not be reported
    as a failure just because nothing differs (the false-positive the
    naive changed-from-source check produced)."""
    rows = [["The subject continues routine treatment as scheduled."],
            ["Nothing sensitive is mentioned in this note."]]
    src = tmp_path / "in.csv"
    _write_csv(src, ["notes"], rows)
    dst = tmp_path / "out.csv"
    _write_csv(dst, ["notes"], rows)  # identical: correct, since there was nothing to scrub
    files = [_dataset_file("f1", str(src))]
    decisions = [{"file_id": "f1", "column": "notes", "action": "scrub_text",
                  "phi_category": "A", "citation": "45 CFR 164.514(b)(2)(i)(A)"}]
    exports = {"f1": str(dst)}

    result = _run(files, decisions, exports)

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["notes"]["verdict"] == "pass"
    assert result["status"] == "clean"


def test_dataset_file_with_zero_decisions_still_gets_reverse_completeness(tmp_path):
    """A dataset file Executor wrote with zero decisions at all (every
    column fell through Executor's SEC-004 fail-closed default) must still
    run the undecided-column pass rather than being skipped entirely for
    lack of any decision to key off of."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "ssn"], [["1", ""]])
    files = [_dataset_file("f1")]
    decisions: list[dict] = []  # Judge/Sentinel produced nothing for this file
    exports = {"f1": str(src)}

    result = _run(files, decisions, exports)

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["id"]["method"] == "undecided"
    assert by_col["id"]["verdict"] == "fail"
    assert by_col["ssn"]["method"] == "undecided"
    assert by_col["ssn"]["verdict"] == "fail"
    assert result["status"] == "issues"


def test_header_only_export_does_not_block_decisions(tmp_path):
    """Zero data rows is a valid, empty dataset -- iter_dataset_rows yields
    nothing to build a header from, so the real on-disk header must still
    be used rather than treating every column as missing."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "ssn"], [])  # header only, zero data rows
    files = [_dataset_file("f1")]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f1", "column": "ssn", "action": "drop", "phi_category": "G",
         "citation": "45 CFR 164.514(b)(2)(i)(G)"},
    ]
    exports = {"f1": str(src)}

    result = _run(files, decisions, exports)

    by_col = {v["column"]: v for v in result["verdicts"]}
    assert by_col["id"]["verdict"] == "pass"
    assert by_col["ssn"]["verdict"] == "pass"
    assert result["status"] == "clean"


def test_zero_decision_file_missing_from_exports_is_not_silently_invisible(tmp_path):
    """A dataset file with zero decisions at all (e.g. Schema couldn't read
    its headers) that ALSO failed to write (Executor's write raised, so it
    never reached exports) must not vanish with no verdict and no audit
    trail -- it has to land in failed_file_ids so the session status
    reflects the loss rather than reporting a false 'complete'."""
    files = [_dataset_file("f1")]
    decisions: list[dict] = []  # no decision ever named this file
    exports: dict[str, str] = {}  # and Executor never wrote it either

    result = _run(files, decisions, exports)

    assert result["failed_file_ids"] == ["f1"]
    assert result["status"] == "issues"


# ---- Phase 10 additions: section-54 items 9-12 (schema/counts/checksums) --


def test_checksums_present_for_every_readable_export(tmp_path):
    src1 = tmp_path / "f1.csv"
    _write_csv(src1, ["id"], [["1"]])
    src2 = tmp_path / "f2.csv"
    _write_csv(src2, ["id"], [["2"]])
    files = [_dataset_file("f1"), _dataset_file("f2")]
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f2", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
    ]
    exports = {"f1": str(src1), "f2": str(src2)}

    result = _run(files, decisions, exports)

    assert set(result["checksums"]) == {"f1", "f2"}
    assert result["checksums"]["f1"] != result["checksums"]["f2"]
    assert len(result["checksums"]["f1"]) == 64  # sha256 hex digest length


def test_file_and_column_counts_reflect_expected_versus_readable(tmp_path):
    src1 = tmp_path / "f1.csv"
    _write_csv(src1, ["id"], [["1"]])
    files = [_dataset_file("f1"), _dataset_file("f2")]  # f2 never exported
    decisions = [
        {"file_id": "f1", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
        {"file_id": "f2", "column": "id", "action": "keep", "phi_category": "NONE", "citation": ""},
    ]
    exports = {"f1": str(src1)}

    result = _run(files, decisions, exports)

    assert result["file_counts"] == {"datasets_expected": 2, "datasets_readable": 1}
    assert result["column_counts"]["f1"] == {"decisions": 1, "verdicts": 1}
    assert result["column_counts"]["f2"] == {"decisions": 1, "verdicts": 1}
    assert result["schema_valid"] == {"f1": True, "f2": False}


# ---- Full-pipeline proof (Task 28), now exercising DeterministicVerifier --


def test_raw_read_happens_only_inside_sandbox_when_one_is_attached(tmp_path, monkeypatch):
    """When a `SandboxRecord` is passed, the export's *rows* (the raw PHI-
    bearing data `_read_columns`/`iter_dataset_rows` parses) are only ever
    read inside the isolated `run_isolated` child process -- never
    directly in this (parent) test process. A `reasoning.iter_dataset_
    rows` spy in the parent can only observe parent-process calls, since
    a `multiprocessing.spawn` child re-imports everything fresh; checksum
    computation legitimately opens the same file in-process afterward
    (safe metadata, sha256 of the bytes, never a parsed row value), so
    this spies on row parsing specifically rather than on `open` itself."""
    import os

    from phi_core.agents import reasoning
    from phi_core.control.sandbox import create_sandbox, destroy_sandbox

    os.environ["PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY"] = "1"

    src = tmp_path / "out.csv"
    _write_csv(src, ["age"], [["45"]])
    files = [_dataset_file("f1")]
    decisions = [{"file_id": "f1", "column": "age", "action": "cap_age_90",
                  "phi_category": "C", "citation": "45 CFR 164.514(b)(2)(i)(C)"}]
    exports = {"f1": str(src)}

    sandbox = create_sandbox("d" * 32)
    calls: list[str] = []
    real_iter = reasoning.iter_dataset_rows

    def _spy(*args, **kwargs):
        calls.append("called")
        return real_iter(*args, **kwargs)

    monkeypatch.setattr(reasoning, "iter_dataset_rows", _spy)

    try:
        result = asyncio.run(
            DeterministicVerifier().run(files, decisions, exports, sandbox=sandbox)
        )
    finally:
        destroy_sandbox(sandbox)

    assert calls == [], (
        "row-level dataset parsing must never happen directly in the parent "
        "process when a sandbox is attached -- it must run inside the isolated child"
    )
    v = result["verdicts"][0]
    assert v["verdict"] == "pass"
    assert result["status"] == "clean"
    # Checksums are still computed in-process (safe metadata, no raw
    # values) even though the row-level verification ran sandboxed.
    assert result["checksums"]["f1"]
