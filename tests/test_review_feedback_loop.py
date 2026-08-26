from __future__ import annotations

import csv
import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tests._workspace_harness import hermetic_phi_workspace, write_pdf_table

STUDY_PREFIX = "PytestReviewFeedback"


@dataclass(frozen=True)
class ReviewStudy:
    study: str
    workspace: Path
    source: Path


@pytest.fixture
def review_study(tmp_path: Path):
    study = f"{STUDY_PREFIX}{uuid.uuid4().hex[:8]}"
    source = tmp_path / "source"
    with hermetic_phi_workspace(tmp_path, study) as workspace:
        yield ReviewStudy(study=study, workspace=workspace, source=source)


def _write_dataset_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _ensure_support_content(source: Path) -> None:
    """The mandatory forms/ + dictionary-or-mapping content every v3
    intake_add package requires -- written once and reused across every
    intake_add call against the same source (idempotent, identical
    bytes)."""
    write_pdf_table(source / "forms" / "consent.pdf", ["FIELD", "VALUE"], [["consent", "signed"]])
    _write_dataset_csv(
        source / "dictionary_mapping" / "dict.csv",
        [{"reference_code": "REF-01", "reference_label": "General study reference material"}],
    )


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _published_dataset(study: ReviewStudy, form_name: str) -> Path:
    return study.workspace / "output" / study.study / "llm_source" / "datasets" / form_name


def _latest_approval_payload(study: ReviewStudy) -> dict[str, Any]:
    runs_dir = study.workspace / "output" / study.study / "runs"
    run_ids = sorted(p.name for p in runs_dir.iterdir() if p.is_dir())
    assert run_ids, "pipeline did not write a run directory"
    approval_path = runs_dir / run_ids[-1] / "phi_handling_approval.json"
    assert approval_path.is_file()
    return json.loads(approval_path.read_text(encoding="utf-8"))


def _approval_form(study: ReviewStudy, form_name: str) -> dict[str, Any]:
    payload = _latest_approval_payload(study)
    for form in payload["forms"]:
        if form["form_name"] == form_name:
            return form
    raise AssertionError(f"approval payload did not contain form {form_name!r}")


def _approval_action(study: ReviewStudy, form_name: str, header: str) -> str:
    form = _approval_form(study, form_name)
    for item in form["classifications"]:
        if item["header"] == header:
            return item["action"]
    raise AssertionError(f"approval payload did not classify header {header!r}")


def _intake_organize_and_run(study: ReviewStudy, form_name: str, rows: list[dict[str, Any]]):
    """*form_name* is the published output name (``<stem>.jsonl``); the
    dataset is written as ``<stem>.csv`` under ``datasets/`` so the
    organizer's stem-preserving CSV route reproduces the same published
    name."""
    dataset_stem = Path(form_name).stem
    _write_dataset_csv(study.source / "datasets" / f"{dataset_stem}.csv", rows)
    _ensure_support_content(study.source)

    from phi_engine.pipeline.intake import intake_add
    from phi_engine.pipeline.organize import organize
    from phi_engine.pipeline.run import run_pipeline

    intake_manifest = intake_add(study.source, study.study)
    assert intake_manifest["status"] == "ready", intake_manifest["review_items"]
    organize(study.study)
    result = run_pipeline(study.study, "us")
    assert result.scrub_raised is None
    assert result.guard_ok is True
    assert _published_dataset(study, form_name).is_file()
    return result


def test_decision_store_round_trip_overwrites_yaml_but_appends_trail(review_study: ReviewStudy) -> None:
    from phi_engine.pipeline.review import decide, load_review_decisions

    store_path = decide(review_study.study, header="NOTES", decision="drop")
    decisions = load_review_decisions(review_study.study)

    assert decisions["NOTES"]["decision"] == "drop"
    assert store_path == review_study.workspace / "config" / review_study.study / "review_decisions.yaml"

    decide(review_study.study, header="NOTES", decision="keep")
    decisions = load_review_decisions(review_study.study)

    assert list(decisions) == ["NOTES"]
    assert decisions["NOTES"]["decision"] == "keep"

    trail_path = store_path.parent / "decisions.jsonl"
    trail = [json.loads(line) for line in trail_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["decision"] for entry in trail] == ["drop", "keep"]
    assert [entry["header"] for entry in trail] == ["NOTES", "NOTES"]


def test_keep_decision_unholds_risky_named_keep_header_end_to_end(review_study: ReviewStudy) -> None:
    form_name = "risk_header_form.jsonl"
    rows = [{"SUBJID": "SUBJ001", "LANDMARK": "North Wing", "SITE_CODE": "S01"}]

    first = _intake_organize_and_run(review_study, form_name, rows)
    first_rows = _read_jsonl(_published_dataset(review_study, form_name))
    first_form = _approval_form(review_study, form_name)

    assert first.exit_code == 0
    assert _approval_action(review_study, form_name, "LANDMARK") == "keep"
    assert "LANDMARK" in first_form["force_drop_headers"]
    assert "LANDMARK" not in first_rows[0]

    from phi_engine.pipeline.review import decide

    decide(review_study.study, header="LANDMARK", decision="keep")
    second = _intake_organize_and_run(review_study, form_name, rows)
    second_rows = _read_jsonl(_published_dataset(review_study, form_name))
    second_form = _approval_form(review_study, form_name)

    assert second.exit_code == 0
    assert "LANDMARK" not in second_form["force_drop_headers"]
    assert second_rows[0]["LANDMARK"] == "North Wing"


def test_drop_decision_removes_otherwise_kept_column_end_to_end(review_study: ReviewStudy) -> None:
    form_name = "drop_decision_form.jsonl"
    rows = [
        {"SUBJID": f"SUBJ{idx:03d}", "ANALYSIS_GROUP": f"G{idx:02d}", "GROUP": "B"}
        for idx in range(11)
    ]

    first = _intake_organize_and_run(review_study, form_name, rows)
    first_rows = _read_jsonl(_published_dataset(review_study, form_name))

    assert first.exit_code == 0
    assert _approval_action(review_study, form_name, "ANALYSIS_GROUP") == "keep"
    assert first_rows[0]["ANALYSIS_GROUP"] == "G00"

    from phi_engine.pipeline.review import decide

    decide(review_study.study, header="ANALYSIS_GROUP", decision="drop")
    second = _intake_organize_and_run(review_study, form_name, rows)
    second_rows = _read_jsonl(_published_dataset(review_study, form_name))
    second_form = _approval_form(review_study, form_name)

    assert second.exit_code == 0
    assert "ANALYSIS_GROUP" in second_form["force_drop_headers"]
    assert all("ANALYSIS_GROUP" not in row for row in second_rows)


def test_override_cap_decision_caps_numeric_value_end_to_end(review_study: ReviewStudy) -> None:
    form_name = "override_cap_form.jsonl"
    header = "SCORE_VALUE"
    rows = [{"SUBJID": "SUBJ001", header: "120", "GROUP": "A"}]

    first = _intake_organize_and_run(review_study, form_name, rows)
    first_rows = _read_jsonl(_published_dataset(review_study, form_name))
    default_action = _approval_action(review_study, form_name, header)

    assert first.exit_code == 0
    assert default_action == "keep"
    assert first_rows[0][header] == "120"

    from phi_engine.pipeline.review import decide

    decide(review_study.study, header=header, decision="override", action="cap")
    second = _intake_organize_and_run(review_study, form_name, rows)
    second_rows = _read_jsonl(_published_dataset(review_study, form_name))
    override_form = _approval_form(review_study, form_name)

    assert second.exit_code == 0
    assert _approval_action(review_study, form_name, header) == "cap"
    assert override_form["actions"][header] == "cap"
    assert second_rows[0][header] == "90+"


def test_list_review_items_reports_organizer_bucket_and_decisions(review_study: ReviewStudy) -> None:
    _write_dataset_csv(review_study.source / "datasets" / "valid_form.csv", [{"SUBJID": "SUBJ001", "ANALYSIS_GROUP": "A"}])
    # A malformed .csv: intake-preflight performs zero CSV content
    # validation (suffix-only classification), so this lands as a normal
    # accepted dataset candidate and only fails when organize's own
    # pd.read_csv() actually parses it -- landing in the organizer's own
    # non-blocking review bucket. Unlike .xls/.xlsx, which intake-preflight
    # now opens via the isolated xls_isolation worker to count sheets, so a
    # corrupt workbook is rejected (blocking) before organize() ever runs.
    (review_study.source / "datasets" / "needs_review.csv").parent.mkdir(parents=True, exist_ok=True)
    (review_study.source / "datasets" / "needs_review.csv").write_text('"unterminated quote field\nmore,data,here\n', encoding="utf-8")
    _ensure_support_content(review_study.source)

    from phi_engine.pipeline.intake import intake_add
    from phi_engine.pipeline.organize import organize
    from phi_engine.pipeline.review import decide, list_review_items

    intake_manifest = intake_add(review_study.source, review_study.study)
    assert intake_manifest["status"] == "ready", intake_manifest["review_items"]
    organize(review_study.study)
    decide(review_study.study, header="ANALYSIS_GROUP", decision="drop")

    review_items = list_review_items(review_study.study)

    assert review_items["study"] == review_study.study
    assert any(
        item["file"] == "needs_review.csv" and item["reason"] == "csv-parse-error"
        for item in review_items["organizer_review_bucket"]
    )
    assert review_items["decisions_on_file"]["ANALYSIS_GROUP"]["decision"] == "drop"
    assert review_items["dependency_recommendations"] == []
    # A ready, non-review-blocked intake has zero redacted intake review
    # items; the key itself is a permanent addition to list_review_items.
    assert review_items["intake_review_items"] == []
    assert set(review_items) == {
        "study",
        "organizer_review_bucket",
        "held_forms",
        "llm_uncertain_queue",
        "dependency_recommendations",
        "decisions_on_file",
        "intake_review_items",
    }
