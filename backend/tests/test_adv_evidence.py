"""Phase 15b category 5: evidence integrity (docs section 98).

Positive-detection adversarial tests against the D12 evidence-
verification rule (control/evidence.py's EvidenceRegistry-era
enforcement, EvidenceClaim/EvidenceSource/EvidenceVerificationResult):
a fake citation, a stale source, a conflicting source, an unsupported
interpretation, and missing evidence must each keep a claim short of
``VERIFIED`` -- never silently promoted by confidence, recency, or
source count.
"""
from __future__ import annotations

from phi_core.control.evidence import (
    VERIFICATION_DIMENSIONS,
    evaluate_claim,
    evaluate_evidence,
    is_tool_backed,
    record_source,
)
from phi_core.control.records import EvidenceClaim


def _claim(**overrides: object) -> EvidenceClaim:
    fields = dict(run_id="run", task_id="task", subject="statute:us", statement="stmt")
    fields.update(overrides)
    return EvidenceClaim(**fields)


def _all_verified() -> dict:
    return {dimension: ("VERIFIED", "checked") for dimension in VERIFICATION_DIMENSIONS}


# ---------------------------------------------------------------------------
# 1. fake citation -- a near-identical but not byte-exact URL (trailing
#    slash, scheme swap) must never be treated as tool-derived provenance;
#    is_tool_backed is an exact match, never a fuzzy one.
# ---------------------------------------------------------------------------


def test_a_near_miss_url_is_never_treated_as_tool_backed():
    cited = {"https://www.ecfr.gov/current/title-45/part-164"}

    assert is_tool_backed("https://www.ecfr.gov/current/title-45/part-164/", cited) is False  # trailing slash
    assert is_tool_backed("http://www.ecfr.gov/current/title-45/part-164", cited) is False  # scheme swap
    assert is_tool_backed("https://www.ECFR.gov/current/title-45/part-164", cited) is False  # case swap


def test_fake_citation_claim_stays_unverified_even_with_forged_supporting_dimensions():
    """A model asserts a citation URL it never actually retrieved, then
    (as if compromised) also supplies fully-VERIFIED dimension states for
    it -- record_source's not-tool-backed override must still force every
    dimension UNVERIFIED regardless of what the caller supplied, so the
    claim can never reach VERIFIED through this path."""
    claim = _claim()
    forged_dimensions = _all_verified()

    updated, sources = evaluate_evidence(
        claim=claim, candidate_urls=["https://forged.example.gov/rule"],
        cited_urls=set(), dimensions=forged_dimensions,
    )

    assert updated.state == "UNVERIFIED"
    assert all(v.state == "UNVERIFIED" for v in sources[0].verifications)


# ---------------------------------------------------------------------------
# 2. stale source -- a tool-backed source whose freshness dimension fails
#    (a superseded/withdrawn regulation) must keep the claim short of
#    VERIFIED even when every other dimension genuinely passes.
# ---------------------------------------------------------------------------


def test_a_stale_source_with_every_other_dimension_verified_stays_unverified():
    claim = _claim()
    dimensions = _all_verified()
    dimensions["freshness"] = ("REJECTED", "source was superseded by a 2019 final rule; content is stale")

    updated, sources = evaluate_evidence(
        claim=claim, candidate_urls=["https://www.ecfr.gov/current/title-45/part-164"],
        cited_urls={"https://www.ecfr.gov/current/title-45/part-164"}, dimensions=dimensions,
    )

    assert updated.state == "UNVERIFIED"
    by_dimension = {v.dimension: v.state for v in sources[0].verifications}
    assert by_dimension["freshness"] == "REJECTED"
    assert by_dimension["retrieval_authenticity"] == "VERIFIED"  # every other check genuinely passed


# ---------------------------------------------------------------------------
# 3. conflicting source -- a source flagged CONTRADICTED for one claim
#    must never bleed into a different claim's own verified state, even
#    when both claims' sources are evaluated together in one pass (proves
#    the CONTRADICTED-wins rule is genuinely claim-scoped, not global).
# ---------------------------------------------------------------------------


def test_contradiction_on_one_claim_never_contaminates_a_different_claims_verified_state():
    claim_a = _claim(claim_id="claim-a", statement="Safe Harbor permits year-only dates.")
    claim_b = _claim(claim_id="claim-b", statement="Safe Harbor permits full ZIP codes.")

    source_a = record_source(
        claim_id="claim-a", url="https://www.hhs.gov/hipaa/safe-harbor-a", tool_backed=True,
        dimensions=_all_verified(),
    )
    source_b_supporting = record_source(
        claim_id="claim-b", url="https://www.hhs.gov/hipaa/safe-harbor-b-support", tool_backed=True,
        dimensions=_all_verified(),
    )
    contradiction_dims = dict(_all_verified())
    contradiction_dims["contradiction"] = ("CONTRADICTED", "a later source states full ZIP codes are restricted")
    source_b_contradicting = record_source(
        claim_id="claim-b", url="https://www.hhs.gov/hipaa/safe-harbor-b-conflict", tool_backed=True,
        dimensions=contradiction_dims,
    )

    all_sources = [source_a, source_b_supporting, source_b_contradicting]
    updated_a = evaluate_claim(claim_a, all_sources)
    updated_b = evaluate_claim(claim_b, all_sources)

    assert updated_a.state == "VERIFIED"
    assert updated_a.contradicted_by == []
    assert updated_b.state == "CONTRADICTED"
    assert updated_b.contradicted_by == [source_b_contradicting.source_id]


# ---------------------------------------------------------------------------
# 4. unsupported interpretation -- a genuinely tool-backed, fresh,
#    authoritative source whose text simply does not say what the claim
#    asserts (claim_support fails) must keep the claim UNVERIFIED even
#    though retrieval/authority/freshness all genuinely pass.
# ---------------------------------------------------------------------------


def test_an_unsupported_interpretation_stays_unverified_despite_a_genuine_authoritative_source():
    claim = _claim(statement="This source establishes a blanket exemption for research data.")
    dimensions = _all_verified()
    dimensions["claim_support"] = (
        "UNVERIFIED", "the cited source discusses de-identification standards generally; it never "
        "states or implies a blanket research exemption -- the interpretation overreaches the text",
    )

    updated, sources = evaluate_evidence(
        claim=claim, candidate_urls=["https://www.hhs.gov/hipaa/deidentification-guidance"],
        cited_urls={"https://www.hhs.gov/hipaa/deidentification-guidance"}, dimensions=dimensions,
    )

    assert updated.state == "UNVERIFIED"
    by_dimension = {v.dimension: v.state for v in sources[0].verifications}
    assert by_dimension["claim_support"] == "UNVERIFIED"
    assert by_dimension["source_authority"] == "VERIFIED"


# ---------------------------------------------------------------------------
# 5. missing evidence -- a claim with zero candidate sources at all (the
#    end-to-end evaluate_evidence path, not merely evaluate_claim called
#    with an empty list by hand) must reach UNKNOWN, never a silent
#    VERIFIED default and never conflated with UNVERIFIED (a distinct
#    state: evidence was sought and found insufficient, versus evidence
#    was never even produced).
# ---------------------------------------------------------------------------


def test_a_claim_with_zero_candidate_sources_reaches_unknown_not_verified():
    claim = _claim()

    updated, sources = evaluate_evidence(claim=claim, candidate_urls=[], cited_urls=set())

    assert sources == []
    assert updated.state == "UNKNOWN"
    assert updated.state != "VERIFIED"
    assert updated.source_ids == []
