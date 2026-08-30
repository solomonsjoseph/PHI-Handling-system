"""Phase 16 evaluation 9/9: handoff minimum-context compliance.

For every ``HandoffGateway.ALLOWED_EDGES`` edge (9 total), builds the exact
payload the real production call site sends -- reusing Phase 11b's
agent-pair integration test infrastructure (``test_agent_pair_integration.
py``'s ``_seeded_store``/``_gateway`` helpers and every edge's real payload
shape, documented there per-edge with its production call site) as the
base, per the plan's guidance, rather than rebuilding harness scaffolding.

Two checks per edge, both against the REAL, unedited ``HandoffGateway``
(``phi_core.control.handoff``):

1. Structural: the real production payload's key set is a subset of that
   edge's declared minimal schema (``EDGE_SCHEMAS[edge].model_fields``) --
   no raw dataset value, no unrelated field.
2. Behavioral: the same payload, with one raw-value-shaped key injected,
   is genuinely REJECTED by the gateway's real minimum-necessary check
   (reason_code ``"not_minimum_necessary"``) -- proving check 6 is a live
   enforcement boundary for every edge, not merely a schema that happens
   to already match.

Compliance rate = fraction of the 9 edges where both checks pass.
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from phi_core.agents.orchestrator import _handoff_finding_payload
from phi_core.control.handoff import (
    ALLOWED_EDGES,
    EDGE_SCHEMAS,
    INSTRUMENT,
    JUDGE,
    LEXICON,
    METHODS_EXPERT,
    REGULATIONS_EXPERT,
    REVIEWER,
    SCHEMA,
    InstrumentQuestion,
    LexiconQuestion,
    ReviewerHandoff,
    RevisedArtifactHandoff,
    SchemaQuestion,
)
from phi_core.control.records import HandoffEnvelope, MethodFinding, RegulatoryFinding
from test_agent_pair_integration import RUN_ID, _gateway, _seeded_store


@pytest.fixture(autouse=True)
def _stub_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same posture as test_agent_pair_integration.py / test_control_phase3_
    # handoff_gateway.py / test_control_phase7_handoff_contracts.py:
    # HandoffGateway's own residual-PHI heuristic
    # (gateway._contains_restricted_content, via scrub_for_prompt's
    # "presidio" detector) calls phi_core.detectors.presidio_detect,
    # whose NER model has real false positives on ordinary short
    # test-only tokens (e.g. "f1", "visit_date"). This harness exercises
    # HandoffGateway's minimum-necessary/topology/trace machinery, not
    # the presidio install.
    import phi_core.detectors as detectors

    monkeypatch.setattr(detectors, "presidio_detect", lambda text: [])


def _real_payload_for_edge(edge: tuple[str, str]) -> dict[str, Any]:
    """The exact payload shape the real production call site for ``edge``
    sends, per test_agent_pair_integration.py's own per-test documentation
    of that call site."""
    if edge == (JUDGE, SCHEMA):
        return SchemaQuestion(column="visit_date", file_id="f1").model_dump()
    if edge == (JUDGE, LEXICON):
        return LexiconQuestion(column="dx_code", assumption="ICD-10 code",
                                reasoning="matches dictionary prefix").model_dump()
    if edge == (JUDGE, INSTRUMENT):
        return InstrumentQuestion(field_or_variable="q12_freetext", file_id="f2").model_dump()
    if edge == (JUDGE, REGULATIONS_EXPERT):
        return {"hipaa_category": "A", "question": "Is a partial ZIP code an identifier here?"}
    if edge == (REGULATIONS_EXPERT, JUDGE):
        finding = RegulatoryFinding(run_id=RUN_ID, hipaa_category="A",
                                     summary="Partial ZIP is a Safe Harbor identifier.")
        return _handoff_finding_payload(finding)
    if edge == (JUDGE, METHODS_EXPERT):
        return {"hipaa_category": "C", "question": "Best-practice method for a birth date column?"}
    if edge == (METHODS_EXPERT, JUDGE):
        finding = MethodFinding(run_id=RUN_ID, hipaa_category="C", summary="Use year-only generalization.")
        return _handoff_finding_payload(finding)
    if edge == (JUDGE, REVIEWER):
        return RevisedArtifactHandoff(decision_ids=["f1:ssn"],
                                       revision_summary="Corrected the SSN masking rule.").model_dump()
    if edge == (REVIEWER, JUDGE):
        return ReviewerHandoff(decision_ids=["f1:col1"], note="Round 1: unsafe KEEP still present.").model_dump()
    raise AssertionError(f"no real-payload builder registered for edge {edge}")  # pragma: no cover


# A raw-content-shaped key no edge schema ever declares -- the same class
# of leak check 7 (residual-PHI heuristic) and check 6 (minimum-necessary)
# exist to jointly stop; used here purely to probe check 6.
_RAW_VALUE_INJECTION_KEY = "internal_debug_context"


@pytest.mark.asyncio
async def test_every_allowed_edge_payload_is_minimum_necessary_and_the_boundary_is_enforced():
    assert len(ALLOWED_EDGES) == 9, "a new edge was added -- extend this harness's edge coverage"

    store = await _seeded_store()
    gateway = _gateway(store)

    compliant_edges: list[tuple[str, str]] = []
    results: list[dict[str, Any]] = []
    for edge in sorted(ALLOWED_EDGES):
        sender, recipient = edge
        schema_fields = set(EDGE_SCHEMAS[edge].model_fields)
        real_payload = _real_payload_for_edge(edge)

        structural_ok = set(real_payload) <= schema_fields

        real_result = await gateway.handoff(HandoffEnvelope(
            run_id=RUN_ID, sender=sender, recipient=recipient,
            data_class="restricted_metadata" if edge != (JUDGE, REGULATIONS_EXPERT) and edge != (JUDGE, METHODS_EXPERT)
            else "internal",
            payload=real_payload, handoff_id=uuid4().hex,
        ))
        behavioral_ok_real = real_result.allowed is True

        injected_payload = {**real_payload, _RAW_VALUE_INJECTION_KEY: "555-12-3456 raw-looking value"}
        injected_result = await gateway.handoff(HandoffEnvelope(
            run_id=RUN_ID, sender=sender, recipient=recipient,
            data_class="restricted_metadata" if edge != (JUDGE, REGULATIONS_EXPERT) and edge != (JUDGE, METHODS_EXPERT)
            else "internal",
            payload=injected_payload, handoff_id=uuid4().hex,
        ))
        behavioral_ok_injected = (
            injected_result.allowed is False and injected_result.reason_code == "not_minimum_necessary"
        )

        edge_compliant = structural_ok and behavioral_ok_real and behavioral_ok_injected
        if edge_compliant:
            compliant_edges.append(edge)
        results.append({
            "edge": f"{sender}->{recipient}", "structural_ok": structural_ok,
            "real_payload_allowed": behavioral_ok_real,
            "injection_rejected": behavioral_ok_injected,
            "injection_reason_code": injected_result.reason_code,
        })

    compliance_rate = round(len(compliant_edges) / len(ALLOWED_EDGES), 4)
    print(f"\n[Phase16][handoff_minimal_context] compliance rate: {compliance_rate} "
          f"over {len(ALLOWED_EDGES)} ALLOWED_EDGES")
    for r in results:
        print(f"[Phase16][handoff_minimal_context] {r['edge']}: structural={r['structural_ok']} "
              f"real_allowed={r['real_payload_allowed']} injection_rejected={r['injection_rejected']} "
              f"({r['injection_reason_code']})")

    non_compliant = [r["edge"] for r in results if not (
        r["structural_ok"] and r["real_payload_allowed"] and r["injection_rejected"]
    )]
    assert not non_compliant, f"edge(s) failed minimum-context compliance: {non_compliant}"
    assert compliance_rate == 1.0
