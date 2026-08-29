"""Phase 7: consumer-driven contract tests for HandoffGateway edges.

R-a tested that each control record has the right field list -- a schema
test, not a contract test. Nothing before this file tested that a
producer's own real output actually satisfies what its consumer declares
it needs. Judge is the first agent with multiple typed upstream inputs
and a typed downstream output, so for every edge in ``ALLOWED_EDGES``
this constructs the producer's real output record, passes it through
``HandoffGateway.handoff()``, and asserts the consumer accepts it with
zero ``ValidationError`` and zero payload key the schema itself does not
declare. Parametrized over ``EDGE_SCHEMAS`` (via ``ALLOWED_EDGES``, whose
own single-source-of-truth is separately guarded in
``test_control_phase3_handoff_gateway.py``) so a new edge cannot ship
without a contract test here.
"""
from __future__ import annotations

import pytest
from phi_core.control.handoff import (
    ALLOWED_EDGES,
    EDGE_SCHEMAS,
    INSTRUMENT,
    JUDGE,
    LEXICON,
    METHODS_EXPERT,
    REGULATIONS_EXPERT,
    SCHEMA,
    HandoffGateway,
)
from phi_core.control.records import HandoffEnvelope, MethodFinding, RegulatoryFinding, WorkflowRun
from phi_core.control.store import MemoryControlStore

RUN_ID = "run-" + "f" * 28
SESSION_ID = "session-" + "g" * 24

# Realistic per-edge producer kwargs for the seven edges with no live
# producer call site yet (Phase 3 status: "zero call sites") -- the
# schema class itself is the producer contract for these, so an instance
# is constructed and dumped, never hand-typed as a bare payload dict.
# Values reused verbatim from test_control_phase3_handoff_gateway.py's
# own PASS-matrix fixtures, which are already proven not to trip the
# residual-PHI/secret heuristics (checks 7-8).
_REALISTIC_KWARGS: dict[tuple[str, str], dict[str, object]] = {
    (JUDGE, REGULATIONS_EXPERT): {
        "hipaa_category": "A", "question": "Is a partial ZIP code an identifier here?",
    },
    (JUDGE, METHODS_EXPERT): {
        "hipaa_category": "E", "question": "Best-practice method for a birth date column?",
    },
    ("Reviewer", JUDGE): {
        "decision_ids": ["dec-1", "dec-2"], "note": "Two decisions lack an omit_by_file match.",
    },
    (JUDGE, "Reviewer"): {
        "decision_ids": ["dec-1"], "revision_summary": "Corrected the SSN column's masking rule.",
    },
    (JUDGE, SCHEMA): {"column": "visit_date", "file_id": "f1"},
    (JUDGE, LEXICON): {
        "column": "dx_code", "assumption": "ICD-10 code", "reasoning": "matches dictionary prefix",
    },
    (JUDGE, INSTRUMENT): {"field_or_variable": "q12_freetext", "file_id": "f2"},
}


def _producer_payload(edge: tuple[str, str]) -> dict[str, object]:
    """The producer's real output record, dumped to the payload shape it
    actually sends across the edge.

    ``(RegulationsExpert, Judge)`` and ``(PHIMethodsExpert, Judge)`` are
    the two edges the live pipeline already routes through
    ``HandoffGateway`` (orchestrator.py's ``_run_regulations_expert``/
    ``_run_phi_methods_expert``): the real producer record is a
    ``RegulatoryFinding``/``MethodFinding``, and the real payload is
    ``orchestrator._handoff_finding_payload``'s own minimum-necessary
    narrowing of it (evidence_refs/created_at are HIPAA Safe Harbor
    identifier shapes the residual-PHI heuristic refuses at the gateway
    boundary, per that function's docstring) -- reusing that live
    function here is what makes this a producer-driven contract test,
    not a hand-typed stand-in.
    """
    if edge == (REGULATIONS_EXPERT, JUDGE):
        from phi_core.agents.orchestrator import _handoff_finding_payload

        finding = RegulatoryFinding(
            run_id=RUN_ID, hipaa_category="A",
            evidence_refs=["https://www.hhs.gov/hipaa/for-professionals/privacy/index.html"],
            summary="Names are a Safe Harbor direct identifier.",
        )
        return _handoff_finding_payload(finding)
    if edge == (METHODS_EXPERT, JUDGE):
        from phi_core.agents.orchestrator import _handoff_finding_payload

        finding = MethodFinding(
            run_id=RUN_ID, hipaa_category="C", recommended_method_id="year_only",
            evidence_refs=["https://www.hhs.gov/hipaa/for-professionals/privacy/index.html"],
            summary="year_only preserves research utility for a date field.",
        )
        return _handoff_finding_payload(finding)
    schema_cls = EDGE_SCHEMAS[edge]
    return schema_cls(**_REALISTIC_KWARGS[edge]).model_dump()


async def _seeded_store() -> MemoryControlStore:
    store = MemoryControlStore()
    await store.insert("workflow_runs", WorkflowRun(run_id=RUN_ID, session_id=SESSION_ID))
    return store


@pytest.fixture(autouse=True)
def _stub_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same posture as test_control_phase3_handoff_gateway.py: presidio's
    # spaCy/thinc/numpy chain can be ABI-broken in a given local
    # interpreter independent of this repo's code, so this exercises the
    # rule detector plus the secret scan, not that install.
    import phi_core.detectors as detectors

    monkeypatch.setattr(detectors, "presidio_detect", lambda text: [])


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", sorted(ALLOWED_EDGES), ids=lambda e: f"{e[0]}->{e[1]}")
async def test_consumer_accepts_producers_real_output_record(edge: tuple[str, str]) -> None:
    schema_cls = EDGE_SCHEMAS[edge]
    payload = _producer_payload(edge)

    # The consumer's own declared contract never raises constructing what
    # the producer actually sent.
    schema_cls.model_validate(payload)

    # The consumer never needs a key the producer did not populate --
    # every payload key is a real, declared schema field (the inverse,
    # a schema field the payload omits, is already proven by the
    # model_validate call above: a missing required field raises there).
    assert set(payload) <= set(schema_cls.model_fields)

    store = await _seeded_store()
    gateway = HandoffGateway(store, session_id=SESSION_ID)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=edge[0], recipient=edge[1],
        data_class="restricted_metadata" if edge[1] in (SCHEMA, LEXICON, INSTRUMENT) else "internal",
        payload=payload,
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True, (result.reason_code, result.detail)
    assert result.reason_code == ""
