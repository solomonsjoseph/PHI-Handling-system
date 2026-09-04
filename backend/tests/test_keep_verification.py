"""Value-level verification tests for Sentinel keep decisions."""
from types import SimpleNamespace

from phi_core.agents.reasoning import verify_keep_decisions


def _decision(column: str, action: str = "keep", file_id: str = "dataset.csv") -> dict:
    return {
        "file_id": file_id,
        "column": column,
        "action": action,
        "reason": "Judge decision",
    }


def test_detector_hit_demotes_keep_without_recording_cell_contents(tmp_path, monkeypatch):
    source = tmp_path / "dataset.csv"
    source.write_text("barcode\nsafe-token\n", encoding="utf-8")
    monkeypatch.setattr(
        "phi_core.agents.reasoning.detect_text",
        lambda *_args, **_kwargs: [SimpleNamespace(hipaa_category="A")],
    )

    decisions, demotions = verify_keep_decisions([_decision("barcode")], {"dataset.csv": source})

    assert decisions[0]["action"] == "human_review"
    assert demotions == [{
        "file_id": "dataset.csv",
        "column": "barcode",
        "from": "keep",
        "to": "human_review",
        "detector": "A",
        "citation": "45 CFR 164.514(b)(2)(i)",
    }]
    assert "safe-token" not in str(decisions)
    assert "safe-token" not in str(demotions)


def test_jurisdiction_pattern_hit_demotes_keep(tmp_path, monkeypatch):
    source = tmp_path / "dataset.csv"
    source.write_text("age\nage 95\n", encoding="utf-8")
    monkeypatch.setattr("phi_core.agents.reasoning.detect_text", lambda *_args, **_kwargs: [])

    decisions, demotions = verify_keep_decisions([_decision("age")], {"dataset.csv": source})

    assert decisions[0]["action"] == "human_review"
    assert demotions[0]["detector"] == "AGE_OVER_89"


def test_unreadable_source_demotes_every_keep_for_that_file(tmp_path):
    missing = tmp_path / "unreadable.csv"
    decisions = [_decision("first"), _decision("second")]

    verified, demotions = verify_keep_decisions(decisions, {"dataset.csv": missing})

    assert [d["action"] for d in verified] == ["human_review", "human_review"]
    assert [d["detector"] for d in demotions] == ["unreadable", "unreadable"]


def test_non_keep_and_missing_source_decisions_are_unchanged(tmp_path):
    source = tmp_path / "dataset.csv"
    source.write_text("value\nsafe-token\n", encoding="utf-8")
    decisions = [
        _decision("value", action="drop"),
        _decision("value", file_id="not-in-datasets.csv"),
    ]

    verified, demotions = verify_keep_decisions(decisions, {"dataset.csv": source})

    assert verified == decisions
    assert demotions == []


def _csv(tmp_path, header: str, values: list[str]):
    source = tmp_path / "dataset.csv"
    rows = "\n".join(f'"{value}"' for value in values)
    source.write_text(f"{header}\n{rows}\n", encoding="utf-8")
    return source


def test_state_and_country_values_do_not_demote_a_keep(tmp_path):
    """45 CFR 164.514(b)(2)(i)(B) reaches only subdivisions smaller than a
    State, so presidio's flat LOCATION label on 'CA' or 'Mexico' is not a
    category (B) identifier and must not send the column to a human."""
    states = _csv(tmp_path, "state", ["CA", "TX", "GA", "NJ", "NY"])
    countries = _csv(tmp_path, "country_of_birth",
                     ["Mexico", "Vietnam", "Guatemala", "India", "Philippines"])

    for path in (states, countries):
        decisions, demotions = verify_keep_decisions(
            [_decision(path.stem if path.stem != "dataset" else "state")],
            {"dataset.csv": path},
        )
        assert demotions == [], (path, demotions)
        assert decisions[0]["action"] == "keep"


def test_sub_state_geography_still_demotes_a_keep(tmp_path):
    source = _csv(tmp_path, "city", ["Fresno", "Houston", "Atlanta", "Edison", "Detroit"])

    decisions, demotions = verify_keep_decisions([_decision("city")], {"dataset.csv": source})

    assert decisions[0]["action"] == "human_review"
    assert demotions[0]["detector"] == "B"


def test_clinical_term_repeated_across_subjects_does_not_demote_a_keep(tmp_path):
    """A span many subjects share cannot be an identifier 'of the individual'
    under 45 CFR 164.514(b)(2), whatever presidio labelled it."""
    source = _csv(tmp_path, "site_of_disease",
                  ["Pulmonary", "Pulmonary", "Pulmonary", "Pulmonary", "Pulmonary"])

    decisions, demotions = verify_keep_decisions(
        [_decision("site_of_disease")], {"dataset.csv": source},
    )

    assert demotions == []
    assert decisions[0]["action"] == "keep"


def test_one_unique_name_among_repeated_clinical_terms_still_demotes(tmp_path):
    """The keeper-header hijack: a single real name planted in an otherwise
    harmless column is confined to one row, so it stays identifying."""
    source = _csv(tmp_path, "site_of_disease",
                  ["Pulmonary", "Pulmonary", "Elena Martinez", "Pulmonary", "Pulmonary"])

    decisions, demotions = verify_keep_decisions(
        [_decision("site_of_disease")], {"dataset.csv": source},
    )

    assert decisions[0]["action"] == "human_review"
    assert demotions[0]["detector"] == "A"


def test_low_confidence_speculative_match_does_not_demote_a_keep(tmp_path):
    """An ICD-10 code read as a license number scores presidio's own floor."""
    source = _csv(tmp_path, "diagnosis_code",
                  ["A15.0", "A15.0", "A18.2", "A15.0", "A18.2"])

    decisions, demotions = verify_keep_decisions(
        [_decision("diagnosis_code")], {"dataset.csv": source},
    )

    assert demotions == []
    assert decisions[0]["action"] == "keep"


def test_one_shared_city_still_demotes_a_keep(tmp_path):
    """Category (B) is enumerated, so a city every subject shares is still a
    city; the shared-value reasoning that rescues clinical vocabulary must
    not reach geography."""
    source = _csv(tmp_path, "city", ["Fresno", "Fresno", "Fresno", "Fresno", "Fresno"])

    decisions, demotions = verify_keep_decisions([_decision("city")], {"dataset.csv": source})

    assert decisions[0]["action"] == "human_review"
    assert demotions[0]["detector"] == "B"


def test_hard_rule_corrects_the_prose_citation_not_only_the_category():
    """Judge has dropped `ssn` correctly while citing (C), the dates
    subcategory, in a sentence about social security numbers. The exported
    bundle carries that sentence as the auditor's evidence, so the letter in
    the prose has to move with the structured category."""
    from phi_core.agents.deterministic_rules import apply_sentinel_hard_rules

    decisions, overrides = apply_sentinel_hard_rules([{
        "column": "ssn", "file_id": "f", "action": "drop",
        "phi_category": "C", "confidence": 0.99,
        "citation": "HIPAA Safe Harbor 45 CFR 164.514(b)(2)(i)(C) social security numbers.",
    }])

    assert decisions[0]["phi_category"] == "G"
    assert decisions[0]["citation"] == "45 CFR 164.514(b)(2)(i)(G)"
    assert overrides[0]["category_corrected"] == "G"


def test_summarise_rejections_names_the_column_and_the_unusable_value():
    from phi_core.agents.orchestrator import _summarise_rejections

    text = _summarise_rejections([
        {"column": "treatment_facility_name", "file_id": "f",
         "field": "action", "proposed": "human_review"},
    ])

    assert "treatment_facility_name" in text
    assert "human_review" in text
    assert _summarise_rejections([]) == ""


def test_a_deterministic_escalation_survives_a_second_validation_pass():
    """`validate_decisions` runs twice over the same list: once in the decide
    loop and again in `run_decision_gates`. A column the pipeline itself sent
    to human review carries `suggested_action`, and the second pass must not
    read that as unusable model output and overwrite the specific reason."""
    from phi_core.agents.reasoning import validate_decisions

    escalated = [{
        "file_id": "f", "column": "treatment_regimen", "action": "human_review",
        "suggested_action": "keep", "reason": "Keep verification: matched B in a row value",
        "confidence": 0.96, "phi_category": "A", "subject": "participant",
    }]

    once, first = validate_decisions(escalated)
    twice, second = validate_decisions(once)

    assert first == [] and second == []
    assert twice[0]["reason"].startswith("Keep verification:")
    assert twice[0]["confidence"] == 0.96


def test_a_raw_model_human_review_is_still_rejected():
    from phi_core.agents.reasoning import validate_decisions

    decisions, rejections = validate_decisions([{
        "file_id": "f", "column": "col", "action": "human_review",
        "confidence": 0.9, "phi_category": "A", "subject": "participant",
    }])

    assert [r["field"] for r in rejections] == ["action"]
    assert decisions[0]["confidence"] == 0.0
