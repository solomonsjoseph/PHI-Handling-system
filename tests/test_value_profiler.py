"""Deterministic value profiler (LOCAL, in-process, never leaves the process).

Unit coverage for phi_engine.pipeline.profile.profile_column plus the two
rules it drives in phi_engine.pipeline.run.run_pipeline: ESCALATION
(value-profile-conflict) and AUTO-CLEAR (value-profile-closed-categorical).
"""

from phi_engine.pipeline.profile import (
    AUTO_CLEAR_MAX_DISTINCT,
    profile_column,
)


def test_closed_categorical_column_is_auto_clearable():
    values = ["Arm-A", "Arm-B", "Arm-C"] * 5
    profile = profile_column(values)
    assert profile.distinct_count == 3
    assert profile.distinct_count <= AUTO_CLEAR_MAX_DISTINCT
    assert profile.blocking_hit_count == 0
    assert profile.warn_hit_count == 0
    assert profile.date_parse_count == 0
    assert profile.is_closed_categorical is True
    assert profile.is_value_profile_conflict is False


def test_high_cardinality_column_is_never_closed_categorical():
    # An identifier-shaped series: one distinct value per row -- structurally
    # cannot be mistaken for a closed categorical set regardless of content.
    values = [f"ID-{i:04d}" for i in range(50)]
    profile = profile_column(values)
    assert profile.distinct_count == 50
    assert profile.is_closed_categorical is False


def test_ssn_shaped_values_trip_the_escalation_conflict_rule():
    values = ["123-45-6789", "987-65-4321", "111-22-3333", "not-an-ssn"]
    profile = profile_column(values)
    assert profile.blocking_hit_count == 3
    assert profile.blocking_hit_rate == 3 / 4
    assert profile.is_value_profile_conflict is True
    assert "SSN" in profile.blocking_categories


def test_low_blocking_rate_does_not_trip_escalation():
    # One coincidental SSN-shaped false positive among many clean values
    # stays under the 0.5 threshold -- escalation requires a majority hit.
    values = ["clean one", "clean two", "123-45-6789"] + ["clean"] * 7
    profile = profile_column(values)
    assert profile.blocking_hit_rate is not None and profile.blocking_hit_rate < 0.5
    assert profile.is_value_profile_conflict is False


def test_date_shaped_values_count_toward_date_parse_rate_not_categorical():
    values = ["2020-01-01", "2020-02-15", "2020-03-30", "2020-04-10"]
    profile = profile_column(values)
    assert profile.date_parse_count == 4
    # Even though 4 <= AUTO_CLEAR_MAX_DISTINCT, a nonzero date-parse count
    # disqualifies the closed-categorical auto-clear (dates are never safe
    # to blanket-auto-clear).
    assert profile.is_closed_categorical is False


def test_empty_and_blank_values_are_excluded_from_all_counts():
    values = ["", None, "  ", "actual-value"]
    profile = profile_column(values)
    assert profile.non_empty_count == 1
    assert profile.distinct_count == 1


def test_profile_never_records_raw_values_only_categories_and_counts():
    values = ["123-45-6789"]
    profile = profile_column(values)
    payload = profile.to_json()
    serialized = str(payload)
    assert "123-45-6789" not in serialized
    assert "SSN" in payload["blocking_categories"]


def test_escalation_catches_phi_in_an_unexpectedly_named_column_end_to_end(tmp_path):
    """The core review-reduction/accuracy-protection case: SSN-shaped values
    planted under a header name ('PROCESS_TAG') that name-only classification
    has no reason to distrust, AND that matches no packaged id/date/drop/keep
    pattern (unlike e.g. 'SITE_CODE', which the packaged id_fields facility
    pattern already pseudonymizes by name -- not a genuine name-blind-spot).
    Without the profiler this would publish raw; with it, the column is
    force-dropped even though its NAME looked safe."""
    import json
    import uuid

    from tests._workspace_harness import hermetic_phi_workspace, write_csv, write_pdf_table

    study = f"ProfilerEscalationTest{uuid.uuid4().hex[:8]}"
    source = tmp_path / "src"
    rows = [[f"S{i}", f"{100 + i:03d}-{i:02d}-{1000 + i:04d}"] for i in range(10)]
    write_csv(source / "datasets" / "study.csv", ["SUBJID", "PROCESS_TAG"], rows)
    # An extractable table (never lands in the organizer's own non-blocking
    # review bucket) and a dictionary naming no dataset column (never
    # creates a dependency recommendation) -- both would otherwise force
    # exit_code 8 and obscure this test's actual subject (the profiler).
    write_pdf_table(source / "forms" / "consent.pdf", ["FIELD", "VALUE"], [["consent", "signed"]])
    write_csv(source / "dictionary_mapping" / "dict.csv", ["reference_code", "reference_label"], [["REF-01", "General study reference material"]])

    with hermetic_phi_workspace(tmp_path, study) as workspace:
        from phi_engine.pipeline.intake import intake_add
        from phi_engine.pipeline.organize import organize
        from phi_engine.pipeline.run import run_pipeline

        intake_manifest = intake_add(source, study)
        assert intake_manifest["status"] == "ready", intake_manifest["review_items"]
        organize(study)
        result = run_pipeline(study, "us")

        assert result.exit_code == 0
        assert result.profile_escalations == 1

        published = workspace / "output" / study / "llm_source" / "datasets" / "study.jsonl"
        assert published.is_file()
        for line in published.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            assert "PROCESS_TAG" not in row
