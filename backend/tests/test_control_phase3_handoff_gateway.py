"""Phase 3 (target-architecture reconciliation, local reference doc
docs/MASTER_ARCHITECTURE_V2.md, never committed): ``HandoffGateway``, the
standalone agent-to-agent handoff validation module. Not wired into
``phi_core/agents/`` yet -- see ``phi_core/control/handoff.py``'s module
docstring.

Covers the required-checks matrix from spec section 86: the four PASS
edges, the topology BLOCK, the capability/tool BLOCK, the cross-run BLOCK,
and the dataset-value-canary BLOCK (with the canary asserted absent from
the resulting TraceEvent).
"""
from __future__ import annotations

import pytest
from phi_core.control.handoff import (
    ALLOWED_EDGES,
    INSTRUMENT,
    JUDGE,
    LEXICON,
    METHODS_EXPERT,
    REGULATIONS_EXPERT,
    REVIEWER,
    SCHEMA,
    HandoffGateway,
)
from phi_core.control.records import HandoffEnvelope, WorkflowRun
from phi_core.control.store import MemoryControlStore

RUN_ID = "run-" + "a" * 28
OTHER_RUN_ID = "run-" + "c" * 28
SESSION_ID = "session-" + "b" * 24


@pytest.fixture(autouse=True)
def _stub_presidio(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same posture as test_control_phase2_source_projection.py: presidio's
    # spaCy/thinc/numpy chain can be ABI-broken in a given local interpreter
    # independent of this repo's code, so these tests exercise the rule
    # detector plus the secret scan, not that install.
    import phi_core.detectors as detectors

    monkeypatch.setattr(detectors, "presidio_detect", lambda text: [])


async def _seeded_store() -> MemoryControlStore:
    store = MemoryControlStore()
    await store.insert("workflow_runs", WorkflowRun(run_id=RUN_ID, session_id=SESSION_ID))
    return store


def _gateway(store: MemoryControlStore) -> HandoffGateway:
    return HandoffGateway(store, session_id=SESSION_ID)


# ---- topology table itself -------------------------------------------------


def test_allowed_edges_matches_spec_section_86():
    assert ALLOWED_EDGES == frozenset({
        (JUDGE, REGULATIONS_EXPERT), (REGULATIONS_EXPERT, JUDGE),
        (JUDGE, METHODS_EXPERT), (METHODS_EXPERT, JUDGE),
        (REVIEWER, JUDGE),
        (JUDGE, SCHEMA), (JUDGE, LEXICON), (JUDGE, INSTRUMENT),
    })


def test_schema_lexicon_edge_is_not_allowed():
    assert (SCHEMA, LEXICON) not in ALLOWED_EDGES
    assert (LEXICON, SCHEMA) not in ALLOWED_EDGES


# ---- PASS matrix ------------------------------------------------------------


@pytest.mark.asyncio
async def test_judge_to_regulations_expert_passes():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=REGULATIONS_EXPERT, data_class="internal",
        payload={"hipaa_category": "A", "question": "Is a partial ZIP code an identifier here?"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True
    assert result.reason_code == ""


@pytest.mark.asyncio
async def test_judge_to_methods_expert_passes():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=METHODS_EXPERT, data_class="internal",
        payload={"hipaa_category": "E", "question": "Best-practice method for a birth date column?"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_reviewer_to_judge_passes():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REVIEWER, recipient=JUDGE, data_class="internal",
        payload={"decision_ids": ["dec-1", "dec-2"], "note": "Two decisions lack an omit_by_file match."},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_judge_to_schema_passes_when_allowed():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=SCHEMA, data_class="restricted_metadata",
        payload={"column": "visit_date", "file_id": "f1"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_judge_to_lexicon_passes():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=LEXICON, data_class="restricted_metadata",
        payload={"column": "dx_code", "assumption": "ICD-10 code", "reasoning": "matches dictionary prefix"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_judge_to_instrument_passes():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=INSTRUMENT, data_class="restricted_metadata",
        payload={"field_or_variable": "q12_freetext", "file_id": "f2"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True


# ---- topology BLOCK ---------------------------------------------------------


@pytest.mark.asyncio
async def test_regulations_expert_to_executor_blocked_by_topology():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REGULATIONS_EXPERT, recipient="Executor", data_class="internal",
        payload={},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "topology_blocked"


@pytest.mark.asyncio
async def test_schema_to_lexicon_blocked_by_topology():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=SCHEMA, recipient=LEXICON, data_class="restricted_metadata", payload={},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "topology_blocked"


@pytest.mark.asyncio
async def test_reviewer_to_raw_worker_blocked_by_topology():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REVIEWER, recipient="Executor", data_class="internal", payload={},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "topology_blocked"


# ---- capability/tool BLOCK ---------------------------------------------------
# Schema requesting a row-read tool through a handoff must be blocked: Judge
# (the only permitted sender on this edge) has no granted tools at all
# (MANIFESTS["Judge"].allowed_tools == {}), so a handoff that tries to carry
# a raw-row-read tool request to Schema is refused at the sender's own
# capability check, before it ever reaches Schema.


@pytest.mark.asyncio
async def test_judge_to_schema_with_raw_row_tool_blocked_by_capability():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=SCHEMA, data_class="restricted_metadata",
        requested_tool="raw_row_read",
        payload={"column": "visit_date", "file_id": "f1"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "tool_not_granted"


# ---- run identity BLOCK ------------------------------------------------------


@pytest.mark.asyncio
async def test_wrong_run_finding_blocked_by_run_identity():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REGULATIONS_EXPERT, recipient=JUDGE, data_class="internal",
        payload={"run_id": OTHER_RUN_ID, "hipaa_category": "A", "summary": "wrong-run artifact"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "cross_run_reference"


# ---- dataset-value canary BLOCK ----------------------------------------------


@pytest.mark.asyncio
async def test_dataset_value_canary_blocked_and_never_traced():
    store = await _seeded_store()
    gateway = _gateway(store)
    canary = "123-45-6789"
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=SCHEMA, data_class="restricted_metadata",
        payload={"column": f"patient SSN {canary}", "file_id": "f1"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "residual_phi_detected"

    events = await store.find_many("trace_events", {"run_id": RUN_ID})
    assert events, "handoff attempt must still be traced"
    for event in events:
        assert canary not in str(event)


@pytest.mark.asyncio
async def test_secret_in_payload_blocked_and_never_traced():
    store = await _seeded_store()
    gateway = _gateway(store)
    secret = "sk-ant-" + "a" * 30
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REVIEWER, recipient=JUDGE, data_class="internal",
        payload={"decision_ids": ["dec-1"], "note": f"leaked key {secret}"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "secret_detected"

    events = await store.find_many("trace_events", {"run_id": RUN_ID})
    for event in events:
        assert secret not in str(event)


# ---- unregistered agents -----------------------------------------------------


@pytest.mark.asyncio
async def test_unregistered_sender_blocked():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender="Ghost", recipient=JUDGE, data_class="internal", payload={},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "sender_unregistered"


@pytest.mark.asyncio
async def test_unregistered_recipient_blocked():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient="Ghost", data_class="internal", payload={},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "recipient_unregistered"


# ---- minimum-necessary / output schema BLOCK ---------------------------------


@pytest.mark.asyncio
async def test_unexpected_payload_key_blocked_as_not_minimum_necessary():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=SCHEMA, data_class="restricted_metadata",
        payload={"column": "visit_date", "file_id": "f1", "unexpected_extra_field": "x"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "not_minimum_necessary"


@pytest.mark.asyncio
async def test_missing_required_payload_field_blocked_by_output_schema():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=SCHEMA, data_class="restricted_metadata",
        payload={"file_id": "f1"},  # missing required "column"
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "payload_schema_invalid"


# ---- every attempt is traced, allowed or blocked -----------------------------


@pytest.mark.asyncio
async def test_allowed_and_blocked_handoffs_both_produce_a_trace_event():
    store = await _seeded_store()
    gateway = _gateway(store)

    allowed_envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=REGULATIONS_EXPERT, data_class="internal",
        payload={"hipaa_category": "A", "question": "?"},
    )
    blocked_envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REGULATIONS_EXPERT, recipient="Executor", data_class="internal", payload={},
    )

    allowed_result = await gateway.handoff(allowed_envelope)
    blocked_result = await gateway.handoff(blocked_envelope)

    events = await store.find_many("trace_events", {"run_id": RUN_ID})
    assert len(events) == 2

    by_handoff_id = {e["payload"]["handoff_id"]: e for e in events}
    allowed_event = by_handoff_id[allowed_envelope.handoff_id]
    blocked_event = by_handoff_id[blocked_envelope.handoff_id]

    assert allowed_event["payload"]["sender"] == JUDGE
    assert allowed_event["payload"]["recipient"] == REGULATIONS_EXPERT
    assert allowed_event["payload"]["allowed"] is True
    assert allowed_event["payload"]["reason"] == ""

    assert blocked_event["payload"]["sender"] == REGULATIONS_EXPERT
    assert blocked_event["payload"]["recipient"] == "Executor"
    assert blocked_event["payload"]["allowed"] is False
    assert blocked_event["payload"]["reason"] == "topology_blocked"

    assert allowed_result.trace_event_id == allowed_event["event_id"]
    assert blocked_result.trace_event_id == blocked_event["event_id"]
