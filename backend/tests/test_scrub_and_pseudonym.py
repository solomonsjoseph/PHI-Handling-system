"""Regression tests for GOAL invariants:

1. LLM never reads dataset row values (enforced by pipeline, not tested here).
2. scrub_text preserves clinical content while redacting PHI substrings.
3. PseudonymRegistry: same real value -> same token across files/columns
   in the same study (exact-match cross-file linkage).
4. PseudonymRegistry: different sessions -> different tokens (no cross-study leak).
"""
from phi_core.agents.reasoning import PseudonymRegistry, _apply_action, _scrub_text_cell


def test_pseudonym_exact_match_cross_file():
    reg = PseudonymRegistry(salt="s-1")
    t_a1 = _apply_action("P001", "pseudonymize", "patient_id", reg)
    t_a2 = _apply_action("P001", "pseudonymize", "patient_id", reg)
    t_a3 = _apply_action("P001", "pseudonymize", "linked_patient_id", reg)
    t_b = _apply_action("P002", "pseudonymize", "patient_id", reg)
    assert t_a1 == t_a2 == t_a3, "Same value must yield same pseudonym in same study"
    assert t_a1 != t_b, "Different values must yield different pseudonyms"
    assert t_a1.startswith("P"), "Token format is P<hex8>"


def test_pseudonym_no_cross_study_linkage():
    reg1 = PseudonymRegistry(salt="s-1")
    reg2 = PseudonymRegistry(salt="s-2")
    assert (
        _apply_action("P001", "pseudonymize", "patient_id", reg1)
        != _apply_action("P001", "pseudonymize", "patient_id", reg2)
    )


def test_scrub_text_preserves_clinical_content():
    text = "Patient John Doe, phone (415) 555-1234, seen at UCSF for headache. Prescribed 500mg acetaminophen."
    out = _scrub_text_cell(text)
    assert "John Doe" not in out
    assert "555-1234" not in out
    assert "headache" in out
    assert "acetaminophen" in out
    assert "UCSF" in out


def test_scrub_text_categorizes_ssn_and_email():
    text = "SSN 111-22-3333, contact james.smith@example.edu."
    out = _scrub_text_cell(text)
    assert "111-22-3333" not in out
    assert "james.smith@example.edu" not in out


def test_cap_age_90_safe_harbor():
    # HIPAA Safe Harbor 45 CFR 164.514(b)(2)(i)(C): ages > 89 must be aggregated
    assert _apply_action("95", "cap_age_90", "age") == "90+"
    assert _apply_action("50", "cap_age_90", "age") == "50"
    assert _apply_action("89", "cap_age_90", "age") == "89"  # <=89 kept
    assert _apply_action("90", "cap_age_90", "age") == "90+"  # >89 aggregated
    assert _apply_action("91", "cap_age_90", "age") == "90+"


def test_year_only_truncates_dob():
    assert _apply_action("1975-03-15", "year_only", "dob") == "1975"
    assert _apply_action("03/15/1975", "year_only", "dob") == "1975"


def test_zip3_truncate_and_restricted():
    assert _apply_action("94103", "zip3_truncate", "zip") == "941"
    # 036 is a restricted ZIP3 in Safe Harbor
    assert _apply_action("03601", "zip3_truncate", "zip") == "000"
