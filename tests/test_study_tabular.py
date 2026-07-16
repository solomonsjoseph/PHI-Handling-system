from __future__ import annotations

from pathlib import Path

import pytest

from generators.study_tabular import (
    FORM_DEMOGRAPHICS,
    FORM_LABS,
    FORM_SCREENING,
    USStudyTabularGenerator,
)
from phi_engine.security.phi_scrub import load_scrub_config


GENERATOR_CLASSES = (USStudyTabularGenerator,)




@pytest.fixture
def packaged_scrub_config():
    repo_root = Path(__file__).resolve().parents[1]
    cfg = load_scrub_config(path=repo_root / "phi_engine/config/_defaults/phi_scrub.yaml")
    assert cfg is not None
    return cfg


def _cell_entries(ledger: list[dict], *, form: str, row_index: int, column: str | None = None) -> list[dict]:
    return [
        entry
        for entry in ledger
        if "row_index" in entry
        and entry["form"] == form
        and entry["row_index"] == row_index
        and (column is None or entry["column"] == column)
    ]


@pytest.mark.parametrize("generator_cls", GENERATOR_CLASSES)
@pytest.mark.parametrize("n_subjects", [60, 8])
def test_generate_study_is_deterministic_for_same_seed(generator_cls, n_subjects):
    first = generator_cls(seed=42).generate_study(n_subjects=n_subjects)
    second = generator_cls(seed=42).generate_study(n_subjects=n_subjects)

    assert second == first


@pytest.mark.parametrize("generator_cls", GENERATOR_CLASSES)
def test_generate_study_requires_enough_subjects_for_planted_edges(generator_cls):
    with pytest.raises(ValueError, match="n_subjects must be >= 5"):
        generator_cls(seed=42).generate_study(n_subjects=4)


@pytest.mark.parametrize("generator_cls", GENERATOR_CLASSES)
def test_gold_ledger_cell_rows_match_generated_forms(generator_cls):
    generator = generator_cls(seed=42)
    forms = generator.generate_study(n_subjects=8)
    ledger = generator.gold_ledger()

    for entry in (line for line in ledger if "row_index" in line):
        row = forms[entry["form"]][entry["row_index"]]

        assert entry["form"] in forms
        assert 0 <= entry["row_index"] < len(forms[entry["form"]])
        assert entry["column"] in row
        assert entry["original_value"] == row[entry["column"]]


@pytest.mark.parametrize("generator_cls", GENERATOR_CLASSES)
def test_subject_ids_are_consistent_across_forms_except_planted_labs_orphans(generator_cls):
    forms = generator_cls(seed=42).generate_study(n_subjects=8)

    for row_index in range(8):
        screening_subjid = forms[FORM_SCREENING][row_index]["SUBJID"]
        demographics_subjid = forms[FORM_DEMOGRAPHICS][row_index]["SUBJID"]
        labs_subjid = forms[FORM_LABS][row_index]["SUBJID"]

        if row_index in (3, 4):
            assert screening_subjid
            assert demographics_subjid == screening_subjid
            assert labs_subjid == ""
        else:
            assert screening_subjid
            assert demographics_subjid == screening_subjid
            assert labs_subjid == screening_subjid


@pytest.mark.parametrize("generator_cls", GENERATOR_CLASSES)
def test_planted_edge_cases_and_ledger_overrides_are_at_fixed_indices(generator_cls):
    generator = generator_cls(seed=42)
    forms = generator.generate_study(n_subjects=8)
    ledger = generator.gold_ledger()

    assert forms[FORM_DEMOGRAPHICS][0]["AGE"] == "92"

    for row_index in (1, 2):
        assert forms[FORM_SCREENING][row_index]["VISITDAT"] == "INVALID-DATE"
        entries = _cell_entries(ledger, form=FORM_SCREENING, row_index=row_index, column="VISITDAT")
        assert entries == [
            {
                "form": FORM_SCREENING,
                "row_index": row_index,
                "column": "VISITDAT",
                "original_value": "INVALID-DATE",
                "expected_action": "blank",
            }
        ]

    for row_index in (3, 4):
        screening_subjid = forms[FORM_SCREENING][row_index]["SUBJID"]
        demographics_subjid = forms[FORM_DEMOGRAPHICS][row_index]["SUBJID"]

        assert screening_subjid
        assert demographics_subjid == screening_subjid
        assert forms[FORM_LABS][row_index]["SUBJID"] == ""

        labs_entries = _cell_entries(ledger, form=FORM_LABS, row_index=row_index)
        assert {entry["column"] for entry in labs_entries} == {"COLLDAT", "TBTXDT"}
        assert {entry["expected_action"] for entry in labs_entries} == {"quarantine_row"}
        assert all(entry["original_value"] == forms[FORM_LABS][row_index][entry["column"]] for entry in labs_entries)


def test_default_scrub_config_binds_common_study_columns(packaged_scrub_config):
    cfg = packaged_scrub_config

    assert cfg.id_label_for("SUBJID") == "SUBJ"
    assert cfg.field_is_id("SUBJID")
    assert cfg.id_label_for("IC_SCRNNUM") == "SCRN"
    assert cfg.field_is_id("IC_SCRNNUM")

    for name in ("VISITDAT", "COLLDAT", "TBTXDT"):
        assert cfg.field_is_date(name)

    assert cfg.field_is_birthdate("IS_BIRTHDAT")
    assert cfg.cap_rule_for("AGE") is not None
    assert cfg.field_is_keep("CBC_HGB")


def test_default_scrub_config_leaves_clinical_passthrough_columns_unmatched(packaged_scrub_config):
    cfg = packaged_scrub_config

    for name in ("SEX", "WEIGHT"):
        assert not cfg.field_is_keep(name)
        assert not cfg.field_is_drop(name)
        assert not cfg.field_is_date(name)
        assert not cfg.field_is_id(name)
        assert cfg.cap_rule_for(name) is None


def test_default_scrub_config_binds_jurisdiction_identifier_columns(packaged_scrub_config):
    cfg = packaged_scrub_config

    for name in ("SSN", "MRN", "PHONE_NUM", "EMAIL"):
        assert cfg.field_is_drop(name)


def test_identifier_column_names_are_present_in_every_screening_row():
    generator = USStudyTabularGenerator(seed=42)
    expected_names = ["SSN", "MRN", "PHONE_NUM", "EMAIL"]
    forms = generator.generate_study(n_subjects=8)

    assert generator.identifier_column_names() == expected_names
    assert len(expected_names) == 4
    for row in forms[FORM_SCREENING]:
        assert set(expected_names).issubset(row)
