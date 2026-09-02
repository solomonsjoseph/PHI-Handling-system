"""Phase 3 (target-architecture reconciliation, local reference doc
docs/MASTER_ARCHITECTURE_V2.md, never committed): ``HandoffGateway``, the
standalone agent-to-agent handoff validation module. Not wired into
``phi_core/agents/`` yet -- see ``phi_core/control/handoff.py``'s module
docstring.

Covers the required-checks matrix from spec section 86: the four PASS
edges, the topology BLOCK, the capability/tool BLOCK, the cross-run BLOCK,
and the dataset-value-canary BLOCK (with the canary asserted absent from
the resulting TraceEvent).

Wave R-b additions (spec section 11 and section 48): the missing
``(Judge, Reviewer)`` topology edge and its ``RevisedArtifactHandoff``
schema; check 11 (the correction/retry budget); an AST-based single-
source-of-truth guard on ``ALLOWED_EDGES`` and its topology check, in
place of a "not mutable at runtime" test (true of any frozenset
regardless of this system, so it would prove nothing); and an exhaustive
42-pair topology matrix (every ordered pair of the 7 declared roles,
self-pairs excluded) that replaces hand-listed positive/negative cases.
"""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

import pytest
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
    HandoffGateway,
)
from phi_core.control.policy import BudgetExceeded
from phi_core.control.records import HandoffEnvelope, WorkflowRun
from phi_core.control.store import MemoryControlStore

RUN_ID = "run-" + "a" * 28
OTHER_RUN_ID = "run-" + "c" * 28
SESSION_ID = "session-" + "b" * 24

# The 7 role constants handoff.py declares (spec section 13's primary
# runtime agents, minus Manager/Executor -- neither participates in a
# direct agent-to-agent handoff edge). Built from the imported constants,
# not retyped as literal strings, so it cannot silently drift from
# handoff.py's own definitions.
ROLES: tuple[str, ...] = (JUDGE, REGULATIONS_EXPERT, METHODS_EXPERT, SCHEMA, LEXICON, INSTRUMENT, REVIEWER)


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
        (REVIEWER, JUDGE), (JUDGE, REVIEWER),
        (JUDGE, SCHEMA), (JUDGE, LEXICON), (JUDGE, INSTRUMENT),
    })


def test_schema_lexicon_edge_is_not_allowed():
    assert (SCHEMA, LEXICON) not in ALLOWED_EDGES
    assert (LEXICON, SCHEMA) not in ALLOWED_EDGES


# ---- single-source-of-truth guards on ALLOWED_EDGES itself -------------------
# Replaces a "ALLOWED_EDGES is not mutable at runtime" test: that would only
# prove CPython enforces frozenset semantics, a property of the language,
# not of this system, and would pass forever regardless of whether the
# real security property (one declaration, read verbatim, never derived
# from request data) holds.


def test_allowed_edges_constant_assigned_exactly_once():
    """Positive control: scans every backend module (not just handoff.py)
    for any statement that assigns the name ``ALLOWED_EDGES``, and
    requires there be exactly one -- in handoff.py. A second, drifted
    definition anywhere else in the tree would defeat the single-source-
    of-truth property this constant exists for, and a frozenset-immutability
    test cannot detect that at all."""
    backend_root = Path(__file__).resolve().parents[1]
    hits: list[Path] = []
    for path in backend_root.rglob("*.py"):
        rel_parts = path.relative_to(backend_root).parts
        if ".venv" in rel_parts or "node_modules" in rel_parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "ALLOWED_EDGES":
                    hits.append(path)
    assert len(hits) == 1, f"expected exactly one ALLOWED_EDGES assignment, found {hits}"
    assert hits[0].name == "handoff.py", f"ALLOWED_EDGES assigned outside handoff.py: {hits[0]}"


def test_topology_check_never_derives_from_envelope():
    """AST-assert the ``in``/``not in`` topology check inside ``_evaluate``
    compares against the bare module constant ``ALLOWED_EDGES`` -- never an
    attribute, call, or expression built from ``envelope`` -- so no
    attacker-controlled handoff field can widen or narrow which edges are
    permitted."""
    handoff_path = Path(__file__).resolve().parents[1] / "phi_core" / "control" / "handoff.py"
    tree = ast.parse(handoff_path.read_text(encoding="utf-8"), filename=str(handoff_path))
    evaluate_fn = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_evaluate"
    )

    topology_compares = [
        node for node in ast.walk(evaluate_fn)
        if isinstance(node, ast.Compare)
        and any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops)
        and any(isinstance(c, ast.Name) and c.id == "ALLOWED_EDGES" for c in node.comparators)
    ]
    assert topology_compares, "expected an `in`/`not in` comparison against ALLOWED_EDGES in _evaluate"
    for compare in topology_compares:
        for comparator in compare.comparators:
            if isinstance(comparator, ast.Name) and comparator.id == "ALLOWED_EDGES":
                continue
            pytest.fail(f"unexpected comparator alongside ALLOWED_EDGES: {ast.dump(comparator)}")

    # ALLOWED_EDGES must never be combined (union, etc.) with anything else
    # anywhere in the function -- that would let a per-call expression
    # (built from envelope data) widen or narrow the effective allow-list.
    for node in ast.walk(evaluate_fn):
        if isinstance(node, ast.BinOp):
            names_in_binop = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if "ALLOWED_EDGES" in names_in_binop:
                pytest.fail(f"ALLOWED_EDGES combined via a binary operation: {ast.dump(node)}")


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
async def test_judge_to_reviewer_passes():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=JUDGE, recipient=REVIEWER, data_class="internal",
        payload={"decision_ids": ["dec-1"], "revision_summary": "Corrected the SSN column's masking rule."},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True
    assert result.reason_code == ""


def test_judge_to_reviewer_edge_uses_revised_artifact_schema():
    assert (JUDGE, REVIEWER) in ALLOWED_EDGES
    assert (JUDGE, REVIEWER) in EDGE_SCHEMAS
    assert EDGE_SCHEMAS[(JUDGE, REVIEWER)].__name__ == "RevisedArtifactHandoff"


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


# ---- Executor / "raw worker" are not declared roles --------------------------
# Spec section 11's "generally unnecessary" list names both, but they are
# NOT interchangeable in this codebase: "Executor" has a real registered
# AgentManifest (policy.MANIFESTS["Executor"]) with no topology edge, so it
# is topology_blocked (see the two tests directly above). The literal
# string "raw worker" has no manifest at all, so it fails one check
# earlier, at recipient registration -- distinct behavior, distinct test.


def test_executor_and_raw_worker_are_not_declared_roles():
    assert "Executor" not in ROLES
    assert "raw worker" not in ROLES


@pytest.mark.asyncio
async def test_raw_worker_recipient_unregistered():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REVIEWER, recipient="raw worker", data_class="internal", payload={},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is False
    assert result.reason_code == "recipient_unregistered"


# ---- exhaustive topology matrix (42 = 7*6 ordered pairs, self-pairs excluded) -
# Subsumes every hand-listed positive/negative topology case above: for
# every ordered pair of the 7 declared roles, handoff() must permit the
# pair if and only if it is a member of ALLOWED_EDGES. Stays correct
# automatically as later phases add roles or edges -- it is a consistency
# check between the topology table and the gateway's actual runtime
# decision, not a hardcoded expectation of which edges the spec requires
# (test_allowed_edges_matches_spec_section_86 above owns that).

_EDGE_FIXTURE: dict[tuple[str, str], tuple[str, dict]] = {
    (JUDGE, REGULATIONS_EXPERT): ("internal", {"hipaa_category": "A", "question": "q"}),
    (REGULATIONS_EXPERT, JUDGE): ("internal", {"run_id": RUN_ID, "hipaa_category": "A", "summary": "s"}),
    (JUDGE, METHODS_EXPERT): ("internal", {"hipaa_category": "A", "question": "q"}),
    (METHODS_EXPERT, JUDGE): ("internal", {"run_id": RUN_ID, "hipaa_category": "A", "summary": "s"}),
    (REVIEWER, JUDGE): ("internal", {"decision_ids": ["d1"], "note": "n"}),
    (JUDGE, REVIEWER): ("internal", {"decision_ids": ["d1"], "revision_summary": "revised"}),
    (JUDGE, SCHEMA): ("restricted_metadata", {"column": "c", "file_id": "f"}),
    (JUDGE, LEXICON): ("restricted_metadata", {"column": "c", "assumption": "a", "reasoning": "r"}),
    (JUDGE, INSTRUMENT): ("restricted_metadata", {"field_or_variable": "v", "file_id": "f"}),
}

_ROLE_PAIRS = list(itertools.permutations(ROLES, 2))


@pytest.mark.parametrize("sender,recipient", _ROLE_PAIRS, ids=[f"{s}-to-{r}" for s, r in _ROLE_PAIRS])
@pytest.mark.asyncio
async def test_topology_matrix_permits_exactly_allowed_edges(sender, recipient):
    store = await _seeded_store()
    gateway = _gateway(store)
    edge = (sender, recipient)
    if edge in _EDGE_FIXTURE:
        data_class, payload = _EDGE_FIXTURE[edge]
    else:
        data_class, payload = "internal", {}
    envelope = HandoffEnvelope(run_id=RUN_ID, sender=sender, recipient=recipient, data_class=data_class, payload=payload)
    result = await gateway.handoff(envelope)
    if edge in ALLOWED_EDGES:
        assert result.allowed is True, f"{edge} should be permitted: {result.reason_code} {result.detail}"
        assert result.reason_code == ""
    else:
        assert result.allowed is False, f"{edge} should be denied"
        assert result.reason_code == "topology_blocked", f"{edge}: expected topology_blocked, got {result.reason_code!r}"


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


# ---- correction/retry budget (check 11, spec section 48) ---------------------
# Unlike checks 1-10, a budget refusal is not expressed as a (bool,
# reason_code, detail) denial: it raises policy.BudgetExceeded, the same
# D5 ceiling-check pattern every other budget/ceiling refusal in this
# codebase already uses (gateway.py, artifacts.py, runs.py,
# manager.py) -- always paired with a TraceEvent(outcome=
# "budget_exceeded") recorded before re-raising. HandoffReasonCode's
# Literal correctly has no budget-shaped value: no budget refusal
# anywhere in this codebase is ever expressed through that channel.


@pytest.mark.asyncio
async def test_attempt_budget_exceeded_raises_budget_exceeded():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REVIEWER, recipient=JUDGE, data_class="internal",
        payload={"decision_ids": ["dec-1"], "note": "n"},
        attempt_number=2, correction_number=2,  # rounds=4 > HANDOFF_ATTEMPT_BUDGET["judge_reviewer"]=3
    )
    with pytest.raises(BudgetExceeded):
        await gateway.handoff(envelope)


@pytest.mark.asyncio
async def test_attempt_budget_at_ceiling_still_allowed():
    # Positive control: attempt_number + correction_number == the budget,
    # not yet exceeding it, must still be permitted -- proves the check is
    # a strict ">" ceiling, and that both fields (not attempt_number alone)
    # participate in the sum.
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REVIEWER, recipient=JUDGE, data_class="internal",
        payload={"decision_ids": ["dec-1"], "note": "n"},
        attempt_number=2, correction_number=1,  # rounds=3 == budget
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True


@pytest.mark.asyncio
async def test_attempt_budget_exceeded_is_traced_before_raising():
    store = await _seeded_store()
    gateway = _gateway(store)
    envelope = HandoffEnvelope(
        run_id=RUN_ID, sender=REVIEWER, recipient=JUDGE, data_class="internal",
        payload={"decision_ids": ["dec-1"], "note": "n"},
        attempt_number=4, correction_number=0,  # rounds=4 > budget
    )
    with pytest.raises(BudgetExceeded):
        await gateway.handoff(envelope)

    events = await store.find_many("trace_events", {"run_id": RUN_ID})
    assert len(events) == 1, "the budget refusal itself must still produce exactly one trace event"
    assert events[0]["outcome"] == "budget_exceeded"
    assert events[0]["payload"]["sender"] == REVIEWER
    assert events[0]["payload"]["recipient"] == JUDGE
    assert events[0]["payload"]["allowed"] is False


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
