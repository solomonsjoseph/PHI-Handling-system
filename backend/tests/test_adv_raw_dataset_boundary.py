"""Phase 15b category 1: raw dataset boundary (docs section 98).

Positive-detection adversarial tests: a raw row-shaped value, a raw-row
sandbox return payload, a canary literal in a provider request, in a
handoff-carried payload, in a research query, in a trace payload field,
and in a learning-pipeline candidate must each be caught/refused before
it ever leaves this process's trust boundary -- never a vacuous
absence-only check.

Extends (never rebuilds) the existing Wave R-d leak-canary harness
(``phi_core.control.canary`` / ``tests/test_control_phaseR_canary.py``),
the Phase 2 sandbox/raw-data-boundary suite
(``tests/test_control_phase2_sandbox_and_raw_data_boundary.py``,
``tests/test_control_phase2_source_projection.py``), the Phase 3 handoff
gateway's own residual-PHI check (``tests/test_control_phase3_handoff_gateway.py``),
the D66 trace sanitizer (``control/trace_sanitizer.py``), and the Phase
12 learning candidate pipeline's own planted-value backstop pattern
(``tests/test_control_learning_case_pipeline.py``).
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from phi_core.control import canary
from phi_core.control.context import StoreTraceWriter
from phi_core.control.events import TraceEventStore
from phi_core.control.gateway import GatewayRequest, ProviderGateway, ToolGateway, ToolResult
from phi_core.control.handoff import HandoffGateway
from phi_core.control.learning import LEARNING_CANDIDATES_COLLECTION, LEARNING_CASES_COLLECTION, LearningCaseService
from phi_core.control.policy import POLICY_VERSION, CapabilityPolicy
from phi_core.control.records import HandoffEnvelope, ResourceBudget, ResourceUsage, TraceEvent, WorkflowRun
from phi_core.control.sandbox import SandboxError, create_sandbox, destroy_sandbox, run_isolated
from phi_core.control.source_projection import source_projection
from phi_core.control.store import MemoryControlStore

_FAIL_CLOSED_TEST_NAME = "__never_matches__"


@pytest.fixture(autouse=True)
def _allow_unenforced_sandbox_memory(request, monkeypatch):
    if request.node.name != _FAIL_CLOSED_TEST_NAME:
        monkeypatch.setenv("PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY", "1")


def _run_id() -> str:
    return uuid4().hex


# ---------------------------------------------------------------------------
# 1. agent attempts row access (denied)
# ---------------------------------------------------------------------------


def test_agent_row_content_never_survives_source_projection_for_a_planted_row_value():
    """A genuinely row-shaped raw value (not merely a header, extending
    test_control_phase2_source_projection.py's header-only coverage) --
    a free-text 'comment' surface an agent would read verbatim -- must
    never reach an agent's prompt with the planted identifiers intact."""
    planted_name = "Jennifer Alvarez-Whitmore"
    planted_ssn = "987-65-4321"
    raw_row_text = f"Patient {planted_name} (SSN {planted_ssn}) reported no adverse events."

    result = source_projection(content_type="comment", raw_text=raw_row_text, run_id="advraw-run-1")

    assert planted_name not in result.projected_text
    assert planted_ssn not in result.projected_text


def test_agent_row_content_with_credential_shape_is_blocked_outright():
    """A row value that also happens to carry a credential shape must be
    blocked entirely (projected_text == ''), not merely redacted in place --
    mirrors test_source_projection_blocks_credential_shape_and_projects_nothing
    but with genuinely row-shaped (not header) content."""
    raw_row_text = "Free-text note: use API key sk-ant-" + "b" * 40 + " to resync the record."

    result = source_projection(content_type="form", raw_text=raw_row_text, run_id="advraw-run-2")

    assert result.projected_text == ""
    assert result.blocked is True


# ---------------------------------------------------------------------------
# 2. tool attempts row return (denied)
# ---------------------------------------------------------------------------


def _return_raw_multi_row_payload():
    return [
        {"patient_name": "Marcus Whitfield", "mrn": "MRN9988776"},
        {"patient_name": "Sofia Delgado", "mrn": "MRN1122334"},
    ]


def test_sandboxed_worker_returning_raw_row_dicts_is_rejected_before_reaching_caller():
    """A raw-data worker dispatched through the sandbox must never be able
    to smuggle row content back out as its return value: run_isolated's
    conforming-payload check (per declared return_kind) rejects a raw
    list[dict] of PHI-shaped row values outright, and the planted values
    never surface in the raised exception's own text."""
    record = create_sandbox(_run_id())
    try:
        with pytest.raises(SandboxError) as excinfo:
            run_isolated(record, _return_raw_multi_row_payload, return_kind="json")
        message = str(excinfo.value)
        assert "Marcus Whitfield" not in message
        assert "Sofia Delgado" not in message
        assert "MRN9988776" not in message
    finally:
        destroy_sandbox(record)


# ---------------------------------------------------------------------------
# Shared canary-gateway rig (mirrors test_control_phaseR_canary.py)
# ---------------------------------------------------------------------------


def _llm_cfg() -> SimpleNamespace:
    return SimpleNamespace(provider="anthropic", model="claude-test", base_url="")


async def _issue_gateway_grant(store: MemoryControlStore, run_id: str, task_id: str):
    policy = CapabilityPolicy(_llm_cfg())
    grant = policy.issue_grant(run_id=run_id, task_id=task_id, agent="RegulationsExpert", task_type="regulationsexpert")
    await store.insert("capability_grants", grant)
    return grant


async def _open_gateway_run(store: MemoryControlStore, run_id: str, session_id: str) -> WorkflowRun:
    run = WorkflowRun(
        run_id=run_id, session_id=session_id, workflow_version="wf/1", policy_version=POLICY_VERSION,
        run_type="study", state="running", node="charter",
        started_at=datetime.now(timezone.utc).isoformat(),
        budget=ResourceBudget(), usage=ResourceUsage(),
    )
    await store.insert("workflow_runs", run)
    return run


def _gateway_request(*, run_id: str, task_id: str, session_id: str, grant_id: str, **overrides) -> GatewayRequest:
    fields: dict = dict(
        session_id=session_id, run_id=run_id, task_id=task_id, agent="RegulationsExpert", attempt=1,
        purpose="research", input_class="internal", grant_id=grant_id,
        provider="anthropic", model="claude-test", endpoint="",
        system_prompt="system", user_prompt="user", coaching_note=None, tool_results=(),
        allowed_tools={}, response_schema="research_evidence",
        timeout_s=30.0, max_tokens=100, max_cost_usd=0.01, policy_version=POLICY_VERSION,
    )
    fields.update(overrides)
    return GatewayRequest(**fields)


def _never_called_completion(**kwargs):
    raise AssertionError("litellm.completion must never be called once a canary hit is detected")


# ---------------------------------------------------------------------------
# 3. provider request contains canary -- extends coverage to the
#    tool_results channel (prior handoff/tool output embedded verbatim
#    into the next call), not just system_prompt/user_prompt.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canary_embedded_in_tool_results_channel_blocks_the_send(monkeypatch):
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", _never_called_completion)
    run_id, task_id, session_id = _run_id(), _run_id(), _run_id()
    store = MemoryControlStore()
    await _open_gateway_run(store, run_id, session_id)
    grant = await _issue_gateway_grant(store, run_id, task_id)
    literal = "ZZZTOOLRESULTCANARY4471"
    tool_result = ToolResult(
        tool="web_search", tool_request_id=f"{task_id}:web_search",
        content=f"prior lookup returned record for {literal}", status="ok",
    )
    req = _gateway_request(
        run_id=run_id, task_id=task_id, session_id=session_id, grant_id=grant.grant_id,
        user_prompt="continue the analysis using the attached prior result",
        tool_results=(tool_result,),
    )
    canary.activate_canary_set(run_id, {"planted": [{"plant_id": "p1", "leak_literals": [literal]}]})

    try:
        with pytest.raises(canary.SecurityBoundaryViolation):
            await ProviderGateway(store).complete(req)
    finally:
        canary.deactivate_canary_set(run_id)


# ---------------------------------------------------------------------------
# 4. handoff contains canary -- a canary literal carried through a real
#    HandoffGateway-validated payload, riding along in an ID-shaped field
#    (``decision_ids``) HandoffGateway's own residual-content heuristic
#    does not fire on (proven not to trip it, unlike
#    test_dataset_value_canary_blocked_and_never_traced's SSN-shaped
#    free-text case), then embedded -- as production code does -- into
#    the next agent's GatewayRequest, where the dedicated leak-canary
#    gate still catches it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canary_carried_through_a_handoff_payload_is_caught_at_the_next_gateway_call(monkeypatch):
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", _never_called_completion)
    run_id, task_id, session_id = _run_id(), _run_id(), _run_id()
    store = MemoryControlStore()
    await _open_gateway_run(store, run_id, session_id)
    grant = await _issue_gateway_grant(store, run_id, task_id)
    literal = "ZZZHANDOFFCANARY8823"

    # A real Reviewer -> Judge handoff (an allowed edge) whose decision_ids
    # carry the canary literal. decision_ids are ID-shaped, not free-text
    # prose, so HandoffGateway's residual-content heuristic (tuned for
    # PHI-shaped prose) does not fire here -- this handoff is genuinely
    # accepted, exactly as a legitimate one carrying real decision ids
    # would be.
    gateway = HandoffGateway(store, session_id=session_id)
    envelope = HandoffEnvelope(
        run_id=run_id, sender="Reviewer", recipient="Judge", data_class="internal",
        payload={"decision_ids": [literal], "note": "see attached decision for re-classification"},
    )
    result = await gateway.handoff(envelope)
    assert result.allowed is True, result.detail

    req = _gateway_request(
        run_id=run_id, task_id=task_id, session_id=session_id, grant_id=grant.grant_id,
        user_prompt=f"Judge re-evaluating decision {literal} per Reviewer handoff",
    )
    canary.activate_canary_set(run_id, {"planted": [{"plant_id": "p1", "leak_literals": [literal]}]})

    try:
        with pytest.raises(canary.SecurityBoundaryViolation):
            await ProviderGateway(store).complete(req)
    finally:
        canary.deactivate_canary_set(run_id)


# ---------------------------------------------------------------------------
# 5. research query contains canary -- extends the existing ToolGateway
#    coverage with the budget-reservation give-back guarantee: a
#    canary-blocked search must never leave the run permanently charged
#    for a call that never reached the provider.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_canary_blocked_research_query_gives_back_its_reserved_budget(monkeypatch):
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", _never_called_completion)
    run_id, task_id, session_id = _run_id(), _run_id(), _run_id()
    store = MemoryControlStore()
    await _open_gateway_run(store, run_id, session_id)
    grant = await _issue_gateway_grant(store, run_id, task_id)
    literal = "ZZZQUERYBUDGETCANARY31"
    req = _gateway_request(
        run_id=run_id, task_id=task_id, session_id=session_id, grant_id=grant.grant_id,
        allowed_tools={"web_search": 2}, max_tokens=100, max_cost_usd=0.05,
    )
    tool_gateway = ToolGateway(ProviderGateway(store))
    canary.activate_canary_set(run_id, {"planted": [{"plant_id": "p1", "leak_literals": [literal]}]})

    try:
        with pytest.raises(canary.SecurityBoundaryViolation):
            await tool_gateway.search(req=req, query=f"lookup {literal}")
    finally:
        canary.deactivate_canary_set(run_id)

    stored_run = await store.get_one("workflow_runs", {"run_id": run_id})
    used = stored_run["usage"]
    assert used["tokens"] == 0
    assert used["cost_usd"] == 0.0
    assert used["tool_calls"] == 0


# ---------------------------------------------------------------------------
# 6. trace contains canary -- a PHI-shaped literal placed directly into a
#    TraceEvent.payload dict field (not status_text/retry_category, which
#    test_control_phaseR_trace.py already covers) must be scrubbed by
#    sanitize_payload before it is chained into the hash or persisted.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_event_payload_field_with_a_planted_identifier_is_scrubbed_before_persisting():
    run_id, session_id = _run_id(), _run_id()
    store = MemoryControlStore()
    await store.insert("workflow_runs", WorkflowRun(run_id=run_id, session_id=session_id))
    planted_ssn = "555-11-2222"
    event = TraceEvent(
        run_id=run_id, seq=0, session_id=session_id, input_class="internal", output_class="internal",
        payload={"notes": f"resolved discrepancy for record SSN {planted_ssn}"},
    )

    stored = await TraceEventStore(store, run_id=run_id, session_id=session_id).append(event)

    assert planted_ssn not in stored.payload["notes"]
    raw = await store.get_one("trace_events", {"run_id": run_id, "seq": stored.seq})
    assert planted_ssn not in str(raw)


@pytest.mark.asyncio
async def test_trace_event_payload_raw_prompt_key_is_redacted_by_default_even_when_clean():
    """D66's fail-closed default: a payload key documented as raw LLM I/O
    (``prompt_text``) is replaced with CONTENT_REDACTED unless the
    corresponding TRACE_RAW_* flag is explicitly set -- proven here via
    the real StoreTraceWriter.emit() facade agents actually call, not a
    bare sanitize_payload() unit call."""
    run_id, session_id, task_id = _run_id(), _run_id(), _run_id()
    store = MemoryControlStore()
    await store.insert("workflow_runs", WorkflowRun(run_id=run_id, session_id=session_id))
    planted = "diagnosis notes for Marcus Whitfield, MRN 4455667"
    writer = StoreTraceWriter(store, run_id=run_id, session_id=session_id)

    await writer.emit(task_id=task_id, agent="RegulationsExpert", input_class="internal",
                       output_class="internal", payload={"prompt_text": planted})

    events = await store.find_many("trace_events", {"run_id": run_id})
    assert len(events) == 1
    assert events[0]["payload"]["prompt_text"] == "CONTENT_REDACTED"
    assert planted not in str(events[0])


# ---------------------------------------------------------------------------
# 7. learning contains canary -- extends test_control_learning_case_pipeline
#    .py's own planted-SSN/name/VIN backstop pattern to two distinct new
#    identifier shapes: a phone number (redacted by the sanitize stage,
#    like the existing SSN case) and an NPI number (a genuine backstop
#    case: NOT one of scrub_persisted_text's own regexes, so only the
#    dedicated PHI/PII scan stage catches it -- the same "backstop, not a
#    rubber stamp" property the existing VIN test proves, on a different
#    identifier shape).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_learning_candidate_with_a_planted_phone_number_is_redacted_by_sanitize():
    store = MemoryControlStore()
    service = LearningCaseService(store)
    planted_phone = "555-867-5309"

    case = await service.create_candidate(
        run_id="a" * 32, source="judge_mistake",
        raw_content=f"Judge mis-generalized a contact record; caller phone was {planted_phone}.",
    )

    assert planted_phone not in case.abstract
    stored = await store.get_one(LEARNING_CASES_COLLECTION, {"case_id": case.case_id})
    assert planted_phone not in repr(stored)


@pytest.mark.asyncio
async def test_learning_candidate_with_a_planted_mbi_number_fails_the_genuine_phi_backstop():
    """A Medicare Beneficiary Identifier is a real HIPAA-relevant
    identifier shape (jurisdictions.py's ``MBI`` GuardPattern) that
    scrub_persisted_text's own regex set does not recognize and that
    contains no long consecutive digit run either (so the reconstruction
    heuristic does not independently catch it) -- exactly the same
    "backstop, not a rubber stamp" property
    test_the_phi_scan_stage_is_a_genuine_backstop_not_a_rubber_stamp
    proves for a VIN, proven here for a different identifier shape and a
    genuinely new candidate source. The durable safe store never receives
    the rejected candidate at all (asserted here); the separate, genuine
    gap where the *raised exception's* ``case.abstract`` still carries
    the raw literal in memory is tracked as a regression, not asserted
    away here -- see test_regression_phase15b.py::
    test_learning_case_error_exposes_raw_backstop_only_identifier_via_case_abstract."""
    store = MemoryControlStore()
    service = LearningCaseService(store)
    planted_mbi = "1EG4TE5MK73"

    with pytest.raises(Exception) as excinfo:
        await service.create_candidate(
            run_id="a" * 32, source="reviewer_correction",
            raw_content=f"Judge dropped a provider identifier {planted_mbi} without redaction.",
        )

    assert excinfo.value.reason == "phi_pii_scan_failed"
    stored_cases = await store.find_many(LEARNING_CASES_COLLECTION, {})
    assert stored_cases == []
    assert await store.get_one(LEARNING_CANDIDATES_COLLECTION, {"case_id": excinfo.value.case.case_id}) is None
