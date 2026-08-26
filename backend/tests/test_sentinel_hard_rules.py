"""Sentinel hard-rule tests: verify known direct identifiers are forced off
'human_review' into safe HIPAA-compliant actions.
"""
from phi_core.agents.reasoning import apply_sentinel_hard_rules


def _decide(column: str, action: str = "human_review", file_id: str = "f1"):
    return {"column": column, "action": action, "file_id": file_id, "confidence": 0.5, "reason": "LLM said"}


def test_dob_forced_from_human_review_to_year_only():
    out, overrides = apply_sentinel_hard_rules([_decide("dob")])
    assert out[0]["action"] == "year_only"
    assert overrides[0]["from"] == "human_review"
    assert overrides[0]["to"] == "year_only"
    assert "164.514(b)(2)(i)(C)" in overrides[0]["citation"]


def test_ssn_forced_to_drop():
    out, _ = apply_sentinel_hard_rules([_decide("SSN"), _decide("social_security_number")])
    assert all(d["action"] == "drop" for d in out)


def test_mrn_forced_to_pseudonymize():
    out, _ = apply_sentinel_hard_rules([_decide("mrn"), _decide("medical_record_number")])
    assert all(d["action"] == "pseudonymize" for d in out)


def test_phone_email_fax_dropped():
    cols = ["phone", "phone_number", "email", "email_address", "fax", "fax_number"]
    out, _ = apply_sentinel_hard_rules([_decide(c) for c in cols])
    assert all(d["action"] == "drop" for d in out)


def test_zip_forced_to_zip3_truncate():
    out, _ = apply_sentinel_hard_rules([_decide("zip"), _decide("postal_code")])
    assert all(d["action"] == "zip3_truncate" for d in out)


def test_age_forced_to_cap_age_90_only_if_human_review():
    # human_review -> cap_age_90
    out1, _ = apply_sentinel_hard_rules([_decide("age", "human_review")])
    assert out1[0]["action"] == "cap_age_90"
    # keep is in allow-list; Judge's action choice preserved. phi_category is
    # still corrected to the rule's letter (the test fixture omits it), so an
    # override IS recorded, but it's a category-only correction, not an
    # action change.
    out2, ov2 = apply_sentinel_hard_rules([_decide("age", "keep")])
    assert out2[0]["action"] == "keep"
    assert out2[0]["phi_category"] == "C"
    assert len(ov2) == 1
    assert ov2[0]["from"] == ov2[0]["to"] == "keep"
    assert ov2[0]["category_corrected"] == "C"


def test_dob_year_only_choice_respected():
    # Judge already picked year_only which is in the allow-list -> action
    # unchanged, but phi_category is still corrected to 'C' since the test
    # fixture omits it.
    out, ov = apply_sentinel_hard_rules([_decide("date_of_birth", "year_only")])
    assert out[0]["action"] == "year_only"
    assert out[0]["phi_category"] == "C"
    assert len(ov) == 1
    assert ov[0]["from"] == ov[0]["to"] == "year_only"


def test_unknown_column_untouched():
    d = _decide("bmi", "keep")
    out, ov = apply_sentinel_hard_rules([d])
    assert out[0]["action"] == "keep"
    assert ov == []


def test_case_insensitive_and_variants():
    variants = ["DOB", "Date_Of_Birth", "birthdate", "birth_date"]
    for v in variants:
        out, _ = apply_sentinel_hard_rules([_decide(v)])
        assert out[0]["action"] == "year_only", f"Failed for variant: {v}"


def test_reason_and_citation_updated():
    out, _ = apply_sentinel_hard_rules([_decide("ssn")])
    assert "Sentinel hard-rule" in out[0]["reason"]
    assert "45 CFR" in out[0]["citation"]
    assert out[0]["confidence"] >= 0.95
