"""Corpus tier-ladder tests (workstream A hardening). Pure unit, no Mongo
and no LLM. ``backend/tests/test_corpus.py`` is untouched and must stay
green independently of these."""
from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import zipfile

import pytest


# ---- every ladder entry plants and satisfies intake v3 -------------------


def test_every_ladder_entry_plants_and_satisfies_intake_v3():
    from phi_corpus.planters import plant
    from phi_corpus.tiers import ladder_for

    for entry in ladder_for("all"):
        art = plant(entry.scenario_id, edge_case_tags=list(entry.edge_case_tags),
                     row_count=entry.row_count, seed=entry.seed, tier=entry.tier)
        z = zipfile.ZipFile(io.BytesIO(art.zip_bytes))
        names = z.namelist()
        assert any(n.startswith("datasets/") for n in names), entry.scenario_id
        assert any(n.startswith("dictionary/") for n in names), entry.scenario_id
        assert not any(n.startswith("forms/") for n in names), entry.scenario_id
        assert not any(n.endswith(".pdf") for n in names), entry.scenario_id
        # No two members share identical bytes (intake rejects cross-component
        # sha256 duplicates).
        seen: dict[str, str] = {}
        for n in names:
            digest = hashlib.sha256(z.read(n)).hexdigest()
            assert digest not in seen, (
                f"{entry.scenario_id}: {n!r} is byte-identical to {seen.get(digest)!r}"
            )
            seen[digest] = n


# ---- the violation catalogue is fully planted -----------------------------


def test_coverage_plants_every_required_violation():
    from phi_corpus.tiers import coverage, ladder_for, REQUIRED_VIOLATIONS

    cov = coverage(ladder_for("all"))
    missing = [k for k in REQUIRED_VIOLATIONS if not cov.get(k)]
    assert not missing, f"uncovered REQUIRED_VIOLATIONS keys: {missing}"


# ---- canary uniqueness ----------------------------------------------------


def test_canary_uniqueness_holds_for_every_entry():
    from phi_corpus.planters import plant
    from phi_corpus.tiers import ladder_for

    for entry in ladder_for("all"):
        art = plant(entry.scenario_id, edge_case_tags=list(entry.edge_case_tags),
                     row_count=entry.row_count, seed=entry.seed, tier=entry.tier)
        gt = art.ground_truth
        z = zipfile.ZipFile(io.BytesIO(art.zip_bytes))
        dict_blob = "\n".join(
            z.read(n).decode("utf-8", errors="replace").lower()
            for n in z.namelist() if n.startswith("dictionary/")
        )
        survives_blob = "\n".join(
            (c.get("value") or "").lower()
            for c in gt["planted"]
            if (c.get("expectation") or {}).get("survives_verbatim")
        )
        blob = dict_blob + "\n" + survives_blob
        for c in gt["planted"]:
            for lit in c.get("leak_literals") or []:
                if len(lit) < 4:
                    continue
                assert lit.lower() not in blob, (
                    f"{entry.scenario_id}: canary literal {lit!r} (plant {c.get('plant_id')}) "
                    f"collided with a verbatim-surviving cell or the dictionary"
                )


# ---- every planted cell carries plant_id / tier / scoreable expectation --


def test_every_planted_cell_is_scoreable():
    from phi_corpus.planters import plant
    from phi_corpus.tiers import ladder_for, TIERS

    for entry in ladder_for("all"):
        art = plant(entry.scenario_id, edge_case_tags=list(entry.edge_case_tags),
                     row_count=entry.row_count, seed=entry.seed, tier=entry.tier)
        for c in art.ground_truth["planted"]:
            assert c.get("plant_id"), (entry.scenario_id, c.get("column"))
            assert c.get("tier") in TIERS, (entry.scenario_id, c.get("column"))
            expectation = c.get("expectation")
            assert expectation is not None, (entry.scenario_id, c.get("column"))
            assert expectation.get("kind") in ("literal", "regex", "text_scrub", "human_review"), (
                entry.scenario_id, c.get("column"), expectation
            )


# ---- determinism -----------------------------------------------------------


def test_plant_is_deterministic_for_a_fixed_seed():
    from phi_corpus.planters import plant

    a1 = plant("l1_sdtm_oncology_v1", row_count=4, seed=7)
    a2 = plant("l1_sdtm_oncology_v1", row_count=4, seed=7)
    assert a1.zip_bytes == a2.zip_bytes


def test_corpus_version_reproduces_across_separate_interpreters():
    """Guards against a repr-based hash silently embedding a memory
    address: two independent `python -c` runs must agree."""
    out1 = subprocess.run(
        [sys.executable, "-c", "from phi_corpus.tiers import corpus_version; print(corpus_version())"],
        cwd=__file__.rsplit("/tests/", 1)[0], capture_output=True, text=True, check=True,
    ).stdout.strip()
    out2 = subprocess.run(
        [sys.executable, "-c", "from phi_corpus.tiers import corpus_version; print(corpus_version())"],
        cwd=__file__.rsplit("/tests/", 1)[0], capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert out1 and out1 == out2


# ---- real-format assertions, one per system -------------------------------


def test_mbi_values_match_the_documented_character_class():
    import re
    from phi_corpus.planters import plant

    pattern = re.compile(
        r"^[1-9][A-HJ-NP-RT-Y][0-9A-HJ-NP-RT-Y][0-9][A-HJ-NP-RT-Y][0-9A-HJ-NP-RT-Y]"
        r"[0-9][A-HJ-NP-RT-Y][A-HJ-NP-RT-Y][0-9][0-9]$"
    )
    art = plant("l2_cms_claims_v1", row_count=10, seed=1)
    mbi_cells = [c for c in art.ground_truth["planted"] if c["column"] == "MBI"]
    assert mbi_cells
    assert all(pattern.match(c["value"]) for c in mbi_cells), mbi_cells


def test_npi_values_pass_luhn_over_80840_prefix():
    from phi_core.detectors import luhn
    from phi_corpus.planters import plant

    art = plant("l2_cms_claims_v1", row_count=10, seed=1)
    npi_cells = [c for c in art.ground_truth["planted"] if c["column"] == "NPI"]
    assert npi_cells
    assert all(luhn("80840" + c["value"]) for c in npi_cells), npi_cells

    hijack = plant("l3_keeper_hijack_v1", row_count=10, seed=1)
    ref_cells = [c for c in hijack.ground_truth["planted"] if c["column"] == "referrer_num"]
    assert ref_cells
    assert all(luhn("80840" + c["value"]) for c in ref_cells), ref_cells


def test_naaccr_dates_are_eight_digit_yyyymmdd():
    import re
    from phi_corpus.planters import plant

    art = plant("l2_naaccr_registry_v1", row_count=10, seed=1)
    for col in ("Date of Birth", "Date of Diagnosis"):
        cells = [c for c in art.ground_truth["planted"] if c["column"] == col]
        assert cells
        assert all(re.fullmatch(r"\d{8}", c["value"]) for c in cells), (col, cells)


def test_sdtm_dtc_values_match_iso_or_partial_shape():
    import re
    from phi_corpus.planters import plant

    pattern = re.compile(r"^(\d{4}(-\d{2}(-\d{2}(T\d{2}:\d{2})?)?)?|\d{4}---\d{2})?$")
    art = plant("l1_sdtm_oncology_v1", row_count=10, seed=1)
    for c in art.ground_truth["planted"]:
        if c["column"].endswith("DTC"):
            assert pattern.match(c["value"]), (c["column"], c["value"])


def test_pcornet_sex_and_race_are_permitted_values():
    from phi_corpus.planters import plant

    art = plant("l2_pcornet_raw_v1", row_count=10, seed=1)
    sex_cells = [c for c in art.ground_truth["planted"]
                 if c["file_name"] == "demographic.csv" and c["column"] == "SEX"]
    race_cells = [c for c in art.ground_truth["planted"]
                  if c["file_name"] == "demographic.csv" and c["column"] == "RACE"]
    assert sex_cells and race_cells
    assert all(c["value"] in {"A", "F", "M", "NI", "UN", "OT"} for c in sex_cells)
    assert all(c["value"] in {"01", "02", "03", "04", "05", "06", "07", "NI", "UN", "OT"}
               for c in race_cells)


def test_redcap_dictionary_has_exactly_the_18_documented_headers_in_order():
    from phi_corpus.planters import plant
    from phi_corpus.scenarios import REDCAP_DICTIONARY_HEADERS

    for scenario_id in ("l1_redcap_registry_v1", "l2_redcap_hostile_v1"):
        art = plant(scenario_id, row_count=4, seed=1)
        z = zipfile.ZipFile(io.BytesIO(art.zip_bytes))
        text = z.read("dictionary/columns.csv").decode("utf-8-sig")
        header_line = text.splitlines()[0]
        headers = next(__import__("csv").reader([header_line]))
        assert headers == list(REDCAP_DICTIONARY_HEADERS), scenario_id


def test_redcap_complete_and_checkbox_values_are_constrained():
    from phi_corpus.planters import plant

    for scenario_id in ("l1_redcap_registry_v1", "l2_redcap_hostile_v1"):
        art = plant(scenario_id, row_count=10, seed=1)
        for c in art.ground_truth["planted"]:
            if c["column"].endswith("_complete"):
                assert c["value"] in {"0", "1", "2"}, (scenario_id, c)
            if "___" in c["column"]:
                assert c["value"] in {"0", "1"}, (scenario_id, c)


# ---- cross-file linkage consistency ---------------------------------------


def test_sdtm_usubjid_set_is_shared_across_all_four_files():
    from phi_corpus.planters import plant

    art = plant("l1_sdtm_oncology_v1", row_count=6, seed=1)
    by_file: dict[str, set[str]] = {}
    for c in art.ground_truth["planted"]:
        if c["column"] == "USUBJID":
            by_file.setdefault(c["file_name"], set()).add(c["value"])
    assert set(by_file.keys()) == {"dm.csv", "ae.csv", "vs.csv", "lb.csv"}
    values = list(by_file.values())
    assert all(v == values[0] for v in values), by_file


def test_i2b2_patient_num_set_is_shared_across_both_files():
    from phi_corpus.planters import plant

    art = plant("l3_i2b2_crosswalk_v1", row_count=6, seed=1)
    by_file: dict[str, set[str]] = {}
    for c in art.ground_truth["planted"]:
        if c["column"] == "PATIENT_NUM":
            by_file.setdefault(c["file_name"], set()).add(c["value"])
    assert set(by_file.keys()) == {"patient_dimension.csv", "patient_mapping.csv"}
    values = list(by_file.values())
    assert all(v == values[0] for v in values), by_file


# ---- quasi-identifier tagging ---------------------------------------------


def test_quasi_identifier_cells_carry_human_review_and_are_excluded_from_deferral():
    from phi_corpus.planters import plant
    from phi_corpus.verify import verify

    art = plant("l3_quasi_identifier_v1", row_count=6, seed=1)
    qi_cells = [c for c in art.ground_truth["planted"] if c.get("edge_case_tag") == "quasi_identifier"]
    assert qi_cells
    assert all(c["expected_action"] == "human_review" for c in qi_cells), qi_cells

    ridageyr_cells = [c for c in art.ground_truth["planted"] if c["column"] == "RIDAGEYR"]
    assert ridageyr_cells
    assert all(c["expected_action"] == "keep" for c in ridageyr_cells)
    assert all(int(c["value"]) <= 80 for c in ridageyr_cells), ridageyr_cells

    # Build decisions that correctly defer every quasi_identifier column,
    # to confirm verify() routes them to excluded_count, not deferral.count.
    seen: set[tuple[str, str]] = set()
    decisions = []
    for c in art.ground_truth["planted"]:
        key = (c["file_name"], c["column"])
        if key in seen:
            continue
        seen.add(key)
        action = "human_review" if c.get("edge_case_tag") == "quasi_identifier" else c["expected_action"]
        decisions.append({"file_id": c["file_name"], "column": c["column"], "action": action})
    rep = verify(art.ground_truth, decisions)
    assert rep["deferral"]["count"] == 0
    assert rep["deferral"]["excluded_count"] == len({(c["file_name"], c["column"]) for c in qi_cells})


# ---- oracle contract -------------------------------------------------------


def test_expected_for_raises_when_cap_age_90_column_has_no_age_sem():
    from phi_corpus.planters import expected_for

    with pytest.raises(ValueError):
        expected_for("cap_age_90", "92", {})


def test_expected_for_raises_when_year_only_column_has_no_year_sem():
    from phi_corpus.planters import expected_for

    with pytest.raises(ValueError):
        expected_for("year_only", "2020-01-01", {})


def test_expected_for_raises_when_zip3_truncate_column_has_no_zip3_or_non_us_sem():
    from phi_corpus.planters import expected_for

    with pytest.raises(ValueError):
        expected_for("zip3_truncate", "94110", {})
