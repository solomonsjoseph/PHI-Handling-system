"""Phase A: classification & method accuracy tests.

These lock in the promise 'all PHI variables and values are accurately
classified and the right method applied' by running the deterministic
hard-rule layer over the shipped labelled corpus at
`/app/backend/tests/corpora/hipaa_categories.json`.

Bar: overall F1 >= 0.95, method-appropriateness >= 0.98, zero unclassified.
"""
import json

from phi_core.validation import (
    CORPUS_PATH, run_validation,
)


def test_corpus_covers_every_hipaa_letter():
    corpus = json.loads(CORPUS_PATH.read_text())
    letters_in_corpus = {c["expected_hipaa_letter"] for c in corpus["columns"] if c["expected_hipaa_letter"]}
    assert letters_in_corpus == set("ABCDEFGHIJKLMNOPQR"), f"missing letters: {set('ABCDEFGHIJKLMNOPQR')-letters_in_corpus}"


def test_corpus_includes_non_phi_keeps_and_scrub_text():
    corpus = json.loads(CORPUS_PATH.read_text())
    keeps = [c for c in corpus["columns"] if c["expected_action"] == "keep"]
    scrubs = [c for c in corpus["columns"] if c["expected_action"] == "scrub_text"]
    assert len(keeps) >= 8, "need at least a handful of non-PHI keeps to test false-positive rate"
    assert len(scrubs) >= 3, "need free-text columns to test scrub_text action"


def test_overall_method_appropriateness_high_bar():
    rep = run_validation()
    assert rep.action_accuracy >= 0.98, f"method appropriateness dropped: {rep.action_accuracy}"


def test_overall_category_accuracy_high_bar():
    rep = run_validation()
    assert rep.category_accuracy >= 0.98, f"category accuracy dropped: {rep.category_accuracy}"


def test_zero_unclassified_columns():
    """No column in the shipped corpus should fall through to the LLM. The
    deterministic layer must cover every canonical name variant we ship."""
    rep = run_validation()
    unresolved = [p for p in rep.predictions if p["predicted_action"] == "unclassified"]
    assert not unresolved, "\n".join(f"unclassified: {p['column']}" for p in unresolved)


def test_per_category_f1_all_high():
    rep = run_validation()
    low = [c for c in rep.per_category if c["f1"] < 0.95]
    assert not low, "categories below 0.95 F1: " + ", ".join(f"{c['letter']}={c['f1']}" for c in low)


def test_names_action_is_drop_or_pseudonymize():
    rep = run_validation()
    for p in rep.predictions:
        if p["expected_letter"] == "A":
            assert p["predicted_action"] in {"drop", "pseudonymize"}, p


def test_dates_action_is_year_only():
    rep = run_validation()
    for p in rep.predictions:
        if p["expected_letter"] == "C" and p["expected_action"] == "year_only":
            assert p["predicted_action"] == "year_only", p


def test_ssn_email_phone_fax_all_dropped():
    rep = run_validation()
    for p in rep.predictions:
        if p["expected_letter"] in {"D", "E", "F", "G"}:
            assert p["predicted_action"] == "drop", p


def test_mrn_pseudonymized():
    rep = run_validation()
    for p in rep.predictions:
        if p["expected_letter"] == "H":
            assert p["predicted_action"] == "pseudonymize", p


def test_free_text_columns_get_scrub_text():
    rep = run_validation()
    for p in rep.predictions:
        if p["expected_action"] == "scrub_text":
            assert p["predicted_action"] == "scrub_text", p


def test_clinical_columns_are_kept():
    rep = run_validation()
    for p in rep.predictions:
        if p["expected_action"] == "keep":
            assert p["predicted_action"] == "keep", (
                f"clinical column {p['column']!r} incorrectly redacted to {p['predicted_action']}"
            )
