"""Focused D12 contracts for the evidence-verification rule (control/evidence.py)."""
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
    return EvidenceClaim(run_id="run", task_id="task", subject="statute:us", statement="stmt", **overrides)


def test_is_tool_backed_requires_the_url_in_the_scoped_citation_set() -> None:
    assert is_tool_backed("https://example.gov/rule", {"https://example.gov/rule"}) is True
    assert is_tool_backed("https://example.gov/rule", {"https://other.gov/rule"}) is False
    assert is_tool_backed("", {"https://example.gov/rule"}) is False


def test_record_source_forces_every_dimension_unverified_when_not_tool_backed() -> None:
    source = record_source(
        claim_id="c1",
        url="https://model-invented.example/citation",
        tool_backed=False,
        dimensions={"claim_support": ("VERIFIED", "looks fine")},  # must be ignored
    )

    assert {v.dimension for v in source.verifications} == set(VERIFICATION_DIMENSIONS)
    assert all(v.state == "UNVERIFIED" for v in source.verifications)
    assert all(v.reason == "not tool-derived" for v in source.verifications)


def test_record_source_uses_supplied_dimension_states_when_tool_backed() -> None:
    dimensions = {dimension: ("VERIFIED", "checked") for dimension in VERIFICATION_DIMENSIONS}
    source = record_source(claim_id="c1", url="https://example.gov/rule", tool_backed=True, dimensions=dimensions)

    assert all(v.state == "VERIFIED" for v in source.verifications)


def test_record_source_defaults_unsupplied_dimensions_to_unverified_even_when_tool_backed() -> None:
    source = record_source(
        claim_id="c1",
        url="https://example.gov/rule",
        tool_backed=True,
        dimensions={"claim_support": ("VERIFIED", "checked")},
    )

    by_dimension = {v.dimension: v.state for v in source.verifications}
    assert by_dimension["claim_support"] == "VERIFIED"
    assert by_dimension["source_authority"] == "UNVERIFIED"
    assert by_dimension["source_authority"] != "VERIFIED"


def test_source_free_claim_stays_unverified() -> None:
    claim = evaluate_claim(_claim(), [])

    assert claim.state == "UNKNOWN"
    assert claim.source_ids == []


def test_model_authored_sources_are_not_tool_provenance() -> None:
    claim = _claim()
    updated, sources = evaluate_evidence(
        claim=claim,
        candidate_urls=["https://model-invented.example/citation"],
        cited_urls=set(),  # the gateway's tool call returned no citations at all
        dimensions={dimension: ("VERIFIED", "checked") for dimension in VERIFICATION_DIMENSIONS},
    )

    assert updated.state == "UNVERIFIED"
    assert sources[0].verifications[0].state == "UNVERIFIED"


def test_claim_reaches_verified_only_with_one_fully_tool_backed_source() -> None:
    claim = _claim()
    dimensions = {dimension: ("VERIFIED", "checked") for dimension in VERIFICATION_DIMENSIONS}

    updated, sources = evaluate_evidence(
        claim=claim,
        candidate_urls=["https://example.gov/rule", "https://model-invented.example/other"],
        cited_urls={"https://example.gov/rule"},
        dimensions=dimensions,
    )

    assert updated.state == "VERIFIED"
    assert len(sources) == 2
    assert updated.source_ids == [s.source_id for s in sources]


def test_a_tool_backed_source_missing_one_dimension_stays_unverified() -> None:
    claim = _claim()
    dimensions = {dimension: ("VERIFIED", "checked") for dimension in VERIFICATION_DIMENSIONS if dimension != "freshness"}

    updated, _ = evaluate_evidence(
        claim=claim,
        candidate_urls=["https://example.gov/rule"],
        cited_urls={"https://example.gov/rule"},
        dimensions=dimensions,
    )

    assert updated.state == "UNVERIFIED"


def test_contradicted_source_wins_over_a_verified_source() -> None:
    claim = _claim()
    verified_dims = {dimension: ("VERIFIED", "checked") for dimension in VERIFICATION_DIMENSIONS}
    contradicted_dims = dict(verified_dims)
    contradicted_dims["contradiction"] = ("CONTRADICTED", "conflicting statute found")

    verified_source = record_source(claim_id=claim.claim_id, url="https://example.gov/a", tool_backed=True, dimensions=verified_dims)
    contradicted_source = record_source(
        claim_id=claim.claim_id, url="https://example.gov/b", tool_backed=True, dimensions=contradicted_dims
    )

    updated = evaluate_claim(claim, [verified_source, contradicted_source])

    assert updated.state == "CONTRADICTED"
    assert updated.contradicted_by == [contradicted_source.source_id]


def test_confidence_cannot_substitute_for_evidence() -> None:
    """D12: model confidence is telemetry only. evaluate_claim/evaluate_evidence
    take no confidence input at all, so a caller cannot pass one in to flip
    the verdict -- the only way to reach VERIFIED is a tool-backed source
    with every dimension VERIFIED."""
    claim = _claim()

    updated, _ = evaluate_evidence(
        claim=claim,
        candidate_urls=["https://model-invented.example/high-confidence-guess"],
        cited_urls=set(),
        dimensions={dimension: ("VERIFIED", "checked") for dimension in VERIFICATION_DIMENSIONS},
    )

    assert updated.state == "UNVERIFIED"


def test_evaluate_claim_only_considers_sources_for_the_same_claim_id() -> None:
    claim = _claim()
    other_claim_source = record_source(
        claim_id="a-different-claim",
        url="https://example.gov/rule",
        tool_backed=True,
        dimensions={dimension: ("VERIFIED", "checked") for dimension in VERIFICATION_DIMENSIONS},
    )

    updated = evaluate_claim(claim, [other_claim_source])

    assert updated.state == "UNKNOWN"
    assert updated.source_ids == []
