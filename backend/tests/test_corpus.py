"""Corpus generator + verifier tests (Phase C1 + C2)."""
from __future__ import annotations

import csv
import io
import json
import zipfile


# ---- Planter tests ----------------------------------------------------


def test_planter_emits_valid_manifest_zip():
    """The corpus ZIP must be a valid manifest the intake endpoint would
    accept: datasets/, dictionary/, and (optionally) forms/ folders."""
    from phi_corpus.planters import plant
    art = plant(scenario_id="oncology_v1", jurisdiction="us",
                edge_case_tags=[], row_count=4, seed=1)
    z = zipfile.ZipFile(io.BytesIO(art.zip_bytes))
    names = z.namelist()
    assert any(n.startswith("datasets/") and n.endswith(".csv") for n in names)
    assert any(n.startswith("dictionary/") and n.endswith(".csv") for n in names)
    # Every dataset row is a valid CSV row (matches header column count)
    for n in names:
        if n.startswith("datasets/"):
            rows = list(csv.reader(z.read(n).decode("utf-8").splitlines()))
            assert len(rows) >= 2  # header + 1+ data
            header_len = len(rows[0])
            for r in rows[1:]:
                assert len(r) == header_len, f"row shape mismatch in {n}: {r}"


def test_planter_ground_truth_covers_every_cell():
    """Ground-truth must record every (file, row, column) cell so the
    verifier can score both PHI transforms and clinical preservation."""
    from phi_corpus.planters import plant
    art = plant(scenario_id="oncology_v1", edge_case_tags=[],
                row_count=5, seed=1, include_forms=False)
    z = zipfile.ZipFile(io.BytesIO(art.zip_bytes))
    total_cells = 0
    for n in z.namelist():
        if n.startswith("datasets/"):
            rows = list(csv.reader(z.read(n).decode("utf-8").splitlines()))
            total_cells += (len(rows) - 1) * len(rows[0])
    assert len(art.ground_truth["planted"]) == total_cells


# ---- Forms tests (Phase C1 forms/pdf component) -----------------------

def test_planter_emits_two_pdfs_when_forms_enabled():
    from phi_corpus.planters import plant
    art = plant(scenario_id="oncology_v1", edge_case_tags=[],
                row_count=2, seed=1, include_forms=True)
    names = zipfile.ZipFile(io.BytesIO(art.zip_bytes)).namelist()
    assert "forms/consent_digital.pdf" in names
    assert "forms/consent_scanned.pdf" in names


def test_planter_form_pdfs_add_phi_plants_to_ground_truth():
    from phi_corpus.planters import plant
    art = plant(scenario_id="diabetes_v1", edge_case_tags=[],
                row_count=1, seed=2, include_forms=True)
    edge_tags = {p["edge_case_tag"] for p in art.ground_truth["planted"]}
    assert "form_consent_digital" in edge_tags
    assert "form_consent_scanned" in edge_tags
    # Each PDF plants 6 PHI slots (A/C/D/F/B + a repeated D)
    digital_plants = [p for p in art.ground_truth["planted"]
                      if p["edge_case_tag"] == "form_consent_digital"]
    assert len(digital_plants) == 6


def test_digital_pdf_text_layer_is_extractable():
    """The digital PDF must be pypdf-extractable so the fast path handles
    it without invoking OCR."""
    from phi_corpus.planters import plant
    from pypdf import PdfReader
    art = plant(scenario_id="oncology_v1", edge_case_tags=[],
                row_count=1, seed=3, include_forms=True)
    z = zipfile.ZipFile(io.BytesIO(art.zip_bytes))
    pdf_bytes = z.read("forms/consent_digital.pdf")
    reader = PdfReader(io.BytesIO(pdf_bytes))
    text = reader.pages[0].extract_text()
    assert "Study Protocol" in text
    assert "Patient Full Name" in text


def test_planter_edge_case_tag_persists_into_ground_truth():
    from phi_corpus.planters import plant
    art = plant(scenario_id="oncology_v1",
                edge_case_tags=["age_over_89", "restricted_zip3",
                                "notes_carry_name", "clinical_hr_90s"],
                row_count=3, seed=99, include_forms=False)
    tags = {p["edge_case_tag"] for p in art.ground_truth["planted"] if p["edge_case_tag"]}
    assert tags == {"age_over_89", "restricted_zip3", "notes_carry_name", "clinical_hr_90s"}


def test_planter_age_over_89_edge_actually_plants_ages_over_89():
    from phi_corpus.planters import plant
    art = plant(scenario_id="oncology_v1", edge_case_tags=["age_over_89"],
                row_count=6, seed=7)
    age_cells = [p for p in art.ground_truth["planted"] if p["column"] == "age"]
    assert all(int(p["value"]) >= 90 for p in age_cells), age_cells


def test_planter_restricted_zip3_edge_uses_a_denylist_prefix():
    """Every planted ZIP under the restricted_zip3 edge case must start
    with one of the 17 HIPAA-restricted 3-digit prefixes."""
    from phi_corpus.planters import plant
    from phi_core.jurisdictions import get_pack
    art = plant(scenario_id="oncology_v1", edge_case_tags=["restricted_zip3"],
                row_count=5, seed=11)
    zip_cells = [p for p in art.ground_truth["planted"] if p["column"] == "zip"]
    prefixes = get_pack("us").restricted_zip3_prefixes
    assert all(p["value"][:3] in prefixes for p in zip_cells), zip_cells


def test_planter_clinical_hr_90s_edge_plants_only_90_to_99():
    """Guard false-positive test: HR must land in 90-99 exactly."""
    from phi_corpus.planters import plant
    art = plant(scenario_id="oncology_v1", edge_case_tags=["clinical_hr_90s"],
                row_count=5, seed=13)
    hr_cells = [p for p in art.ground_truth["planted"] if p["column"] == "heart_rate_bpm"]
    for c in hr_cells:
        assert 90 <= int(c["value"]) <= 99, c


# ---- Verifier tests ---------------------------------------------------


def test_verifier_scores_perfect_pipeline_as_100_percent():
    """If the pipeline made the exactly-correct decision for every column
    the verifier reports precision=recall=F1=1.0."""
    from phi_corpus.planters import plant
    from phi_corpus.verify import verify

    art = plant(scenario_id="oncology_v1", edge_case_tags=[],
                row_count=3, seed=42)
    # Build a perfect decision list from the ground truth (one per column).
    seen: set[tuple[str, str]] = set()
    decisions = []
    for p in art.ground_truth["planted"]:
        k = (p["file_name"], p["column"])
        if k in seen:
            continue
        seen.add(k)
        decisions.append({
            "file_id": p["file_name"], "column": p["column"],
            "action": p["expected_action"],
            "hipaa_category": p["hipaa_category"],
        })
    rep = verify(art.ground_truth, decisions)
    assert rep["correctness"]["overall_f1"] == 1.0
    assert rep["correctness"]["overall_precision"] == 1.0
    assert rep["correctness"]["overall_recall"] == 1.0
    assert rep["deferral"]["count"] == 0


def test_verifier_flags_false_negative_when_pipeline_leaks_phi():
    """If Judge decides ``keep`` on a column that carries PHI, the
    verifier reports a false-negative."""
    from phi_corpus.planters import plant
    from phi_corpus.verify import verify

    art = plant(scenario_id="oncology_v1", edge_case_tags=[],
                row_count=3, seed=42)
    decisions = []
    seen: set[tuple[str, str]] = set()
    for p in art.ground_truth["planted"]:
        k = (p["file_name"], p["column"])
        if k in seen:
            continue
        seen.add(k)
        action = p["expected_action"]
        # Sabotage: keep the phone column that MUST be dropped.
        if p["column"] == "phone":
            action = "keep"
        decisions.append({
            "file_id": p["file_name"], "column": p["column"], "action": action,
            "hipaa_category": p["hipaa_category"],
        })
    rep = verify(art.ground_truth, decisions)
    assert rep["correctness"]["false_negatives"], "leak not detected"
    fn = rep["correctness"]["false_negatives"][0]
    assert fn["column"] == "phone"
    assert fn["expected_action"] == "drop"
    assert fn["actual_action"] == "keep"


def test_verifier_flags_false_positive_when_pipeline_over_blocks_clinical():
    """If Judge decides ``drop`` on a clinical column, the verifier
    reports a false-positive (over-blocking)."""
    from phi_corpus.planters import plant
    from phi_corpus.verify import verify

    art = plant(scenario_id="oncology_v1", edge_case_tags=["clinical_hr_90s"],
                row_count=3, seed=42)
    decisions = []
    seen: set[tuple[str, str]] = set()
    for p in art.ground_truth["planted"]:
        k = (p["file_name"], p["column"])
        if k in seen:
            continue
        seen.add(k)
        action = p["expected_action"]
        # Sabotage: over-block heart_rate_bpm
        if p["column"] == "heart_rate_bpm":
            action = "drop"
        decisions.append({
            "file_id": p["file_name"], "column": p["column"], "action": action,
            "hipaa_category": p["hipaa_category"],
        })
    rep = verify(art.ground_truth, decisions)
    assert rep["correctness"]["false_positives"], "over-block not detected"
    fp = rep["correctness"]["false_positives"][0]
    assert fp["column"] == "heart_rate_bpm"
    assert fp["actual_action"] == "drop"


def test_verifier_scores_deferral_rate_separately():
    """Q2(iii): deferrals to human_review are counted separately, not as
    correctness misses. Enables the 'reduce deferral over time' plot."""
    from phi_corpus.planters import plant
    from phi_corpus.verify import verify

    art = plant(scenario_id="oncology_v1", edge_case_tags=[],
                row_count=3, seed=42)
    decisions = []
    seen: set[tuple[str, str]] = set()
    for p in art.ground_truth["planted"]:
        k = (p["file_name"], p["column"])
        if k in seen:
            continue
        seen.add(k)
        action = p["expected_action"]
        if p["column"] in ("dob", "age"):
            action = "human_review"
        decisions.append({
            "file_id": p["file_name"], "column": p["column"], "action": action,
            "hipaa_category": p["hipaa_category"],
        })
    rep = verify(art.ground_truth, decisions)
    assert rep["deferral"]["count"] == 2
    assert rep["deferral"]["rate"] > 0
    # Correctness should be perfect on the remaining planted columns.
    assert rep["correctness"]["overall_recall"] == 1.0


def test_verifier_reports_scenario_and_jurisdiction():
    """The report echoes the scenario / jurisdiction / edge_case_tags so
    downstream aggregation (Phase C4) can slice by these dimensions."""
    from phi_corpus.planters import plant
    from phi_corpus.verify import verify

    art = plant(scenario_id="diabetes_v1", jurisdiction="us",
                edge_case_tags=["notes_carry_name"], row_count=2, seed=1)
    rep = verify(art.ground_truth, decisions=[])
    assert rep["scenario_id"] == "diabetes_v1"
    assert rep["jurisdiction"] == "us"
    assert "notes_carry_name" in rep["edge_case_tags"]


# ---- Narrative / form-plant scoring (Phase C form fix) ---------------


def test_verifier_credits_form_plant_when_planted_value_absent_from_export(tmp_path):
    """Form/narrative plants (row == 0) are scored by inspecting the
    pipeline's redacted export text: if the raw planted value substring
    is absent, credit the plant as TP."""
    from phi_corpus.verify import verify

    ground_truth = {
        "scenario_id": "custom_v1",
        "jurisdiction": "us",
        "planted": [
            {"file_name": "consent.pdf", "row": 0, "column": "Phone",
             "value": "415-555-1234", "hipaa_category": "D",
             "expected_action": "scrub_text", "edge_case_tag": "form_test"},
            {"file_name": "consent.pdf", "row": 0, "column": "Full Name",
             "value": "James Smith", "hipaa_category": "A",
             "expected_action": "scrub_text", "edge_case_tag": "form_test"},
        ],
    }
    # Redacted export that scrubbed both plants.
    export = tmp_path / "abc__consent.redacted.txt"
    export.write_text(
        "Patient [A] enrolled. Contact [D] between 9am and 5pm.",
        encoding="utf-8",
    )
    export_paths = {"abc": str(export)}
    name_map = {"consent.pdf": "abc"}

    rep = verify(ground_truth, decisions=[], file_name_map=name_map,
                 export_paths=export_paths)
    assert rep["summary"]["fn"] == 0, "form plants must be credited as caught"
    assert rep["summary"]["tp"] == 2
    assert rep["correctness"]["overall_recall"] == 1.0


def test_verifier_flags_form_plant_when_raw_phi_survives_in_export(tmp_path):
    """If the pipeline's redacted export STILL contains the raw planted
    PHI substring, the verifier flags it as a false-negative leak."""
    from phi_corpus.verify import verify

    ground_truth = {
        "scenario_id": "custom_v1", "jurisdiction": "us",
        "planted": [
            {"file_name": "consent.pdf", "row": 0, "column": "Phone",
             "value": "415-555-1234", "hipaa_category": "D",
             "expected_action": "scrub_text", "edge_case_tag": "form_test"},
        ],
    }
    export = tmp_path / "abc__consent.redacted.txt"
    # Leak: raw phone survived.
    export.write_text(
        "Contact the participant at 415-555-1234 during clinic hours.",
        encoding="utf-8",
    )
    rep = verify(
        ground_truth, decisions=[],
        file_name_map={"consent.pdf": "abc"},
        export_paths={"abc": str(export)},
    )
    assert rep["summary"]["fn"] == 1
    fn = rep["correctness"]["false_negatives"][0]
    assert fn["file"] == "consent.pdf"
    assert fn["column"] == "Phone"
    assert fn["actual_action"] == "leaked_in_export"


def test_verifier_scores_max_adversarial_ground_truth_shape():
    """Sanity: the max-adversarial scenario ground truth covers HIPAA A-R."""
    from phi_corpus.planters import plant

    art = plant(scenario_id="hipaa_max_adversarial_v1", jurisdiction="us",
                edge_case_tags=[], row_count=3, seed=1)
    cats = {p["hipaa_category"] for p in art.ground_truth["planted"]}
    for letter in "ABCDEFGHIJKLMNOPQR":
        assert letter in cats, f"HIPAA {letter} missing from max-adversarial corpus"




def test_cli_summary_only_lists_scenarios_and_edge_cases(tmp_path):
    from phi_corpus.generate import _cli
    import io
    import contextlib

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _cli(["--scenario", "oncology_v1", "--out",
                   str(tmp_path / "x.zip"), "--summary-only"])
    assert rc == 0
    data = json.loads(buf.getvalue())
    assert data["scenarios"]
    assert any(s["id"] == "oncology_v1" for s in data["scenarios"])
    assert data["edge_cases"]
    assert any(e["tag"] == "age_over_89" for e in data["edge_cases"])
