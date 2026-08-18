"""Tests for the Reviewer agent (Task 29): confirms Operator's coverage of
every decision Judge/Sentinel produced, not Operator's per-cell
correctness (that is Operator's own job, covered by test_operator.py).
"""
from __future__ import annotations

import asyncio
import csv
from pathlib import Path

from phi_core.agents.reviewer import Reviewer
from phi_corpus.benchmark import _reviewer_coverage, build_report
from phi_corpus.planters import plant


class FakeAgentLog:
    def __init__(self):
        self.inserted: list[dict] = []

    async def insert_one(self, doc, *_args, **_kwargs):
        self.inserted.append(doc)


class FakeDb:
    def __init__(self):
        self.agent_log = FakeAgentLog()


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        for row in rows:
            writer.writerow(row)


def _decision(file_id: str, column: str, action: str = "keep") -> dict:
    return {
        "file_id": file_id, "column": column, "action": action,
        "phi_category": "NONE", "citation": "",
    }


def _verdict(file_id: str, column: str, method: str = "keep", verdict: str = "pass") -> dict:
    return {
        "file_id": file_id, "column": column,
        "violation": {"phi_category": "NONE", "citation": ""},
        "method": method, "checks": [], "verdict": verdict, "problem": "", "performed": "",
    }


def test_missing_operator_verdict_is_a_finding_and_blocks_the_file(tmp_path):
    """A decision Judge/Sentinel produced has no corresponding Operator
    verdict at all -- Reviewer must catch this coverage gap even though
    Operator itself reported nothing wrong for the file."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "name"], [["1", "Jane"], ["2", "John"]])
    exports = {"f1": str(src)}
    decisions = [_decision("f1", "id"), _decision("f1", "name")]
    operator_result = {
        "verdicts": [_verdict("f1", "id")],  # the "name" verdict silently missing
        "failed_file_ids": [],
        "status": "clean",
    }

    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result, exports))

    findings = [f for f in result["findings"] if f["kind"] == "missing_operator_verdict"]
    assert len(findings) == 1
    assert findings[0]["file_id"] == "f1"
    assert findings[0]["column"] == "name"
    assert result["status"] == "issues"
    assert "f1" not in result["exports"]
    assert result["coverage"]["missing"] == 1
    assert result["coverage"]["decisions"] == 2
    assert result["coverage"]["verdicts"] == 1


def test_omit_by_file_column_still_present_is_a_finding(tmp_path):
    """A column marked for omission in the human-review deferral tail must
    genuinely be absent from the written file. Reviewer opens the real
    export and checks itself, independent of what Operator or Executor
    claims happened."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "ssn"], [["1", "123-45-6789"]])
    exports = {"f1": str(src)}
    decisions = [_decision("f1", "id")]
    operator_result = {
        "verdicts": [_verdict("f1", "id")],
        "failed_file_ids": [],
        "status": "clean",
    }
    omit_by_file = {"f1": {"ssn"}}

    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result, exports, omit_by_file))

    findings = [f for f in result["findings"] if f["kind"] == "omit_column_leaked"]
    assert len(findings) == 1
    assert findings[0]["file_id"] == "f1"
    assert findings[0]["column"] == "ssn"
    assert result["status"] == "issues"
    assert "f1" not in result["exports"]


def test_coverage_mismatch_when_zero_fail_verdicts_but_column_count_differs(tmp_path):
    """Operator reported zero failures for this file, but the recounted
    decision count doesn't match the real written column count. Reviewer
    turns that diagnostic into a blocking finding rather than trusting
    Operator's clean read of its own work."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "name", "extra"], [["1", "Jane", "x"]])
    exports = {"f1": str(src)}
    # Only two decisions were made, but the written file has three columns.
    decisions = [_decision("f1", "id"), _decision("f1", "name")]
    operator_result = {
        "verdicts": [_verdict("f1", "id"), _verdict("f1", "name")],
        "failed_file_ids": [],
        "status": "clean",
    }

    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result, exports))

    findings = [f for f in result["findings"] if f["kind"] == "coverage_mismatch"]
    assert len(findings) == 1
    assert findings[0]["file_id"] == "f1"
    assert "column" not in findings[0]
    assert result["status"] == "issues"
    assert "f1" not in result["exports"]


def test_coverage_mismatch_not_raised_when_operator_already_has_a_fail(tmp_path):
    """A file with a genuine Operator fail verdict is Operator's own
    problem to report; Reviewer's coverage_mismatch check only fires when
    Operator's own verdicts show zero failures for the file."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "name", "extra"], [["1", "Jane", "x"]])
    exports = {"f1": str(src)}
    decisions = [_decision("f1", "id"), _decision("f1", "name")]
    operator_result = {
        "verdicts": [_verdict("f1", "id", verdict="fail"), _verdict("f1", "name")],
        "failed_file_ids": [],
        "status": "issues",
    }

    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result, exports))

    assert not any(f["kind"] == "coverage_mismatch" for f in result["findings"])


def test_full_coverage_clean_run(tmp_path):
    """Every decision has a matching Operator verdict, no omitted column
    leaks into the output, and the decision count matches the written
    columns. Reviewer reports clean and returns exports unchanged."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "name"], [["1", "Jane"], ["2", "John"]])
    exports = {"f1": str(src)}
    decisions = [_decision("f1", "id"), _decision("f1", "name")]
    operator_result = {
        "verdicts": [_verdict("f1", "id"), _verdict("f1", "name")],
        "failed_file_ids": [],
        "status": "clean",
    }

    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result, exports))

    assert result["findings"] == []
    assert result["status"] == "clean"
    assert result["exports"] == exports
    assert result["exports"] is not exports
    assert result["coverage"]["decisions"] == result["coverage"]["verdicts"]
    assert result["coverage"]["missing"] == 0


def test_input_exports_dict_is_never_mutated(tmp_path):
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "name"], [["1", "Jane"]])
    src2 = tmp_path / "out2.csv"
    _write_csv(src2, ["id"], [["1"]])
    exports = {"f1": str(src), "f2": str(src2)}
    original = dict(exports)
    decisions = [_decision("f1", "id"), _decision("f1", "name"), _decision("f2", "id")]
    operator_result = {
        # f2's decision has no verdict at all -- should be dropped from the
        # returned exports, but the original dict must be untouched.
        "verdicts": [_verdict("f1", "id"), _verdict("f1", "name")],
        "failed_file_ids": [],
        "status": "clean",
    }

    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result, exports))

    assert exports == original
    assert "f2" not in result["exports"]
    assert "f1" in result["exports"]


def test_file_already_in_failed_file_ids_stays_excluded_and_is_issues(tmp_path):
    """A file Operator already flagged as failed (never reached a
    readable export) needs no further per-column comparison from
    Reviewer, and never lands back in the returned exports."""
    decisions = [_decision("f1", "id")]
    operator_result = {
        "verdicts": [_verdict("f1", "id", verdict="fail")],
        "failed_file_ids": ["f1"],
        "status": "issues",
    }
    exports: dict[str, str] = {}

    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result, exports))

    assert result["status"] == "issues"
    assert "f1" not in result["exports"]


def test_agent_log_row_emitted_per_file_with_coverage_check_phase(tmp_path):
    """Task 29's logging contract: one row per file, not one aggregate row
    per batch, with the exact phase and payload shape."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id"], [["1"]])
    exports = {"f1": str(src)}
    decisions = [_decision("f1", "id")]
    operator_result = {
        "verdicts": [_verdict("f1", "id")],
        "failed_file_ids": [],
        "status": "clean",
    }

    db = FakeDb()
    reviewer = Reviewer(session_id="s", llm=None, db=db)
    asyncio.run(reviewer.run(decisions, operator_result, exports))

    rows = [d for d in db.agent_log.inserted if d["phase"] == "review.coverage_check"]
    assert len(rows) == 1
    payload = rows[0]["payload"]
    assert payload["file_id"] == "f1"
    assert payload["columns"] == ["id"]
    assert payload["decisions_checked"] == 1
    assert payload["operator_verdicts_found"] == 1
    assert payload["missing"] == 0
    assert payload["verdict"] == "clean"
    assert rows[0]["agent"] == "Reviewer"
    assert rows[0]["status_text"]


def test_corrupt_export_for_a_non_failed_file_does_not_raise_and_skips_header_checks(tmp_path):
    """Reviewer must never crash on an export it cannot read when Operator
    did not already flag the file as failed -- it degrades gracefully,
    skipping the omit-leak and coverage_mismatch checks (both need a real
    header) while still catching whatever missing_operator_verdict
    findings the file's decisions warrant."""
    bad = tmp_path / "bad.weird"
    bad.write_text("garbage")
    exports = {"f1": str(bad)}
    decisions = [_decision("f1", "id"), _decision("f1", "name")]
    operator_result = {
        "verdicts": [_verdict("f1", "id")],  # "name" missing -> still caught
        "failed_file_ids": [],  # NOT flagged failed by Operator
        "status": "clean",
    }
    omit_by_file = {"f1": {"ssn"}}  # would trigger omit-leak if the header were readable

    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result, exports, omit_by_file))

    kinds = {f["kind"] for f in result["findings"]}
    assert kinds == {"missing_operator_verdict"}
    assert result["status"] == "issues"
    assert "f1" not in result["exports"]


def test_zero_decision_file_with_real_export_is_coverage_mismatch_unless_already_failed(tmp_path):
    """A file with zero decisions at all but a real multi-column export is
    a coverage_mismatch when Operator did not already flag it failed.
    An already-failed file skips the header-dependent check entirely
    (Operator already excluded it; no readable header is assumed), but
    stays excluded from the returned exports and the run stays "issues"
    either way."""
    src = tmp_path / "out.csv"
    _write_csv(src, ["id", "name"], [["1", "Jane"]])
    exports = {"f1": str(src)}
    decisions: list[dict] = []  # zero decisions for f1

    operator_result_not_failed = {"verdicts": [], "failed_file_ids": [], "status": "clean"}
    reviewer = Reviewer(session_id="s", llm=None, db=FakeDb())
    result = asyncio.run(reviewer.run(decisions, operator_result_not_failed, exports))
    findings = [f for f in result["findings"] if f["kind"] == "coverage_mismatch"]
    assert len(findings) == 1
    assert findings[0]["file_id"] == "f1"
    assert result["status"] == "issues"
    assert "f1" not in result["exports"]

    operator_result_failed = {"verdicts": [], "failed_file_ids": ["f1"], "status": "issues"}
    reviewer2 = Reviewer(session_id="s", llm=None, db=FakeDb())
    result2 = asyncio.run(reviewer2.run(decisions, operator_result_failed, exports))
    assert not any(f["kind"] == "coverage_mismatch" for f in result2["findings"])
    assert result2["status"] == "issues"
    assert "f1" not in result2["exports"]


def test_reviewer_coverage_rolls_up_reviewer_agent_log_rows():
    """A synthetic agent_log with a mix of clean and issues verdicts and
    varying missing counts must roll up into exact summed totals, and
    'clean' must go false the moment any single row has findings."""
    agent_log = [
        {"agent": "Reviewer", "phase": "review.coverage_check",
         "payload": {"file_id": "f1", "columns": 3, "decisions_checked": 3,
                     "operator_verdicts_found": 3, "missing": 0, "verdict": "clean"}},
        {"agent": "Reviewer", "phase": "review.coverage_check",
         "payload": {"file_id": "f2", "columns": 4, "decisions_checked": 4,
                     "operator_verdicts_found": 2, "missing": 2, "verdict": "issues"}},
        {"agent": "Reviewer", "phase": "review.coverage_check",
         "payload": {"file_id": "f3", "columns": 2, "decisions_checked": 2,
                     "operator_verdicts_found": 1, "missing": 1, "verdict": "issues"}},
        # a row from another agent must never be counted
        {"agent": "Judge", "phase": "judge.decide",
         "payload": {"prompt_text": "irrelevant"}},
    ]
    coverage = _reviewer_coverage(agent_log)
    assert coverage["files_checked"] == 3
    assert coverage["files_with_findings"] == 2
    assert coverage["decisions_checked"] == 9
    assert coverage["operator_verdicts_found"] == 6
    assert coverage["missing"] == 3
    assert coverage["clean"] is False


def test_reviewer_coverage_clean_when_every_row_is_clean():
    agent_log = [
        {"agent": "Reviewer", "phase": "review.coverage_check",
         "payload": {"file_id": "f1", "columns": 3, "decisions_checked": 3,
                     "operator_verdicts_found": 3, "missing": 0, "verdict": "clean"}},
        {"agent": "Reviewer", "phase": "review.coverage_check",
         "payload": {"file_id": "f2", "columns": 1, "decisions_checked": 1,
                     "operator_verdicts_found": 1, "missing": 0, "verdict": "clean"}},
    ]
    coverage = _reviewer_coverage(agent_log)
    assert coverage["files_checked"] == 2
    assert coverage["files_with_findings"] == 0
    assert coverage["missing"] == 0
    assert coverage["clean"] is True


def test_build_report_reviewer_coverage_absent_when_no_agent_log():
    """When agent_log is not supplied, build_report must leave
    reviewer_coverage at None and list it in 'unavailable', exactly as it
    already does for context_hygiene."""
    art = plant(scenario_id="oncology_v1", row_count=12, seed=7)
    gt = art.ground_truth

    report = build_report(
        ground_truth=gt, decisions=[], verify_report={}, mode="agentic",
    )

    assert report["reviewer_coverage"] is None
    unavailable_sections = {u["section"] for u in report["unavailable"]}
    assert "reviewer_coverage" in unavailable_sections
    assert "context_hygiene" in unavailable_sections
