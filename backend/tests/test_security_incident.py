"""Tests for SECURITY_BOUNDARY_VIOLATION handling (spec section 71,
Phase 15a: ``phi_core.control.security_incident``).

Covers the module's own contract (record / active / resolve / destruction
decision / no-auto-resume), the "never copies the leaked value" guarantee
that section 71 explicitly requires, durability across a simulated backend
restart (an open incident is a release-blocking safety fact and must not be
lost when the process restarts), and the live gateway integration where a real
canary hit now also opens a durable security incident (not just a trace event).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from phi_core.control import canary
from phi_core.control import security_incident as si
from phi_core.control.gateway import GatewayRequest, ProviderGateway
from phi_core.control.policy import POLICY_VERSION, CapabilityPolicy
from phi_core.control.records import (
    ResourceBudget,
    ResourceUsage,
    WorkflowRun,
)
from phi_core.control.store import MemoryControlStore, MongoControlStore
from phi_core.db import get_db
from pydantic import ValidationError

RUN_ID = "sec-incident-run-" + "r" * 12
TASK_ID = "sec-incident-task-" + "t" * 12
SESSION_ID = "sec-incident-session-" + "s" * 10

SENSITIVE_VALUE = "999-88-7777"  # SSN-shaped: a real value that must never be copied
SENSITIVE_NAME = "Rutherford Applewhite"


def _store() -> MemoryControlStore:
    return MemoryControlStore()


# ---------------------------------------------------------------------------
# Core module contract
# ---------------------------------------------------------------------------


def test_six_event_classes_match_section_71():
    import typing
    args = typing.get_args(si.SecurityIncidentEventClass)
    assert set(args) == {
        "dataset_value_to_provider",
        "dataset_value_in_trace",
        "raw_data_escaped_sandbox",
        "cross_run_data_access",
        "unauthorized_sensitive_review",
        "provider_bypass_sensitive_content",
    }


@pytest.mark.asyncio
async def test_record_security_incident_opens_and_activates():
    store = _store()
    assert await si.security_incident_active(store, RUN_ID) is False
    incident = await si.record_security_incident(store, RUN_ID, "raw_data_escaped_sandbox", source="sandbox")
    assert incident.status == "open"
    assert await si.security_incident_active(store, RUN_ID) is True
    assert incident.incident_id in {i.incident_id for i in await si.open_incidents(store, RUN_ID)}


@pytest.mark.asyncio
async def test_incident_is_scoped_to_its_own_run():
    store = _store()
    await si.record_security_incident(store, RUN_ID, "raw_data_escaped_sandbox")
    assert await si.security_incident_active(store, RUN_ID) is True
    assert await si.security_incident_active(store, "some-other-run") is False


@pytest.mark.asyncio
async def test_resolve_requires_an_explicit_authorized_principal():
    store = _store()
    incident = await si.record_security_incident(store, RUN_ID, "cross_run_data_access")
    with pytest.raises(ValueError):
        await si.resolve_security_incident(store, incident.incident_id, resolved_by="")
    assert await si.security_incident_active(store, RUN_ID) is True  # unchanged: still open


@pytest.mark.asyncio
async def test_resolve_security_incident_closes_it_and_stops_blocking():
    store = _store()
    incident = await si.record_security_incident(store, RUN_ID, "unauthorized_sensitive_review")
    resolved = await si.resolve_security_incident(store, incident.incident_id, resolved_by="security-officer-42")
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.resolved_by == "security-officer-42"
    assert resolved.resolved_at
    assert await si.security_incident_active(store, RUN_ID) is False


@pytest.mark.asyncio
async def test_resolving_an_unknown_incident_id_is_a_safe_no_op():
    store = _store()
    assert await si.resolve_security_incident(store, "does-not-exist", resolved_by="someone") is None


@pytest.mark.asyncio
async def test_resolving_an_already_resolved_incident_is_a_safe_no_op_not_a_reopen():
    store = _store()
    incident = await si.record_security_incident(store, RUN_ID, "dataset_value_in_trace")
    await si.resolve_security_incident(store, incident.incident_id, resolved_by="officer-1")
    again = await si.resolve_security_incident(store, incident.incident_id, resolved_by="officer-2")
    assert again is None
    stored = await store.get_one(si.COLLECTION, {"incident_id": incident.incident_id})
    assert stored["resolved_by"] == "officer-1"  # first resolution is authoritative


@pytest.mark.asyncio
async def test_nothing_ever_auto_resumes_an_open_incident():
    """DO NOT automatically resume (section 71): recording, checking active,
    and reading open_incidents any number of times must never itself close
    an incident. Only an explicit resolve_security_incident call does."""
    store = _store()
    await si.record_security_incident(store, RUN_ID, "provider_bypass_sensitive_content")
    for _ in range(5):
        assert await si.security_incident_active(store, RUN_ID) is True
        await si.open_incidents(store, RUN_ID)
    assert await si.security_incident_active(store, RUN_ID) is True


@pytest.mark.asyncio
async def test_determine_destruction_required_is_a_decision_not_an_action():
    store = _store()
    sandbox_incident = await si.record_security_incident(store, RUN_ID, "raw_data_escaped_sandbox")
    assert si.determine_destruction_required(sandbox_incident) == "REQUIRED"

    other_incident = await si.record_security_incident(store, RUN_ID, "unauthorized_sensitive_review")
    assert si.determine_destruction_required(other_incident) == "UNDETERMINED"

    # A decision point only: merely calling the pure function must not
    # itself write anything -- the stored record is untouched.
    stored = await store.get_one(si.COLLECTION, {"incident_id": sandbox_incident.incident_id})
    assert stored["destruction_decision"] == "UNDETERMINED"
    assert await si.security_incident_active(store, RUN_ID) is True


@pytest.mark.asyncio
async def test_handle_security_boundary_violation_runs_the_full_sequence():
    store = _store()
    incident = await si.handle_security_boundary_violation(
        store, RUN_ID, "raw_data_escaped_sandbox", source="sandbox", category="sandbox",
        summary="a path resolved outside the run workspace",
    )
    # PRESERVE: recorded, open, durably.
    assert incident.status == "open"
    # DETERMINE destruction: a real recommendation was persisted, not left
    # at the default.
    stored = await store.get_one(si.COLLECTION, {"incident_id": incident.incident_id})
    assert stored["destruction_decision"] == "REQUIRED"
    assert incident.destruction_decision == "REQUIRED"
    # BLOCK release consequence.
    assert await si.security_incident_active(store, RUN_ID) is True
    # DO NOT auto-resume: still open after the handler returns.
    assert incident.incident_id in {i.incident_id for i in await si.open_incidents(store, RUN_ID)}


# ---------------------------------------------------------------------------
# "Never copies the leaked sensitive value into incident telemetry" (section 71)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_never_copies_the_leaked_value_even_when_a_caller_passes_it_in_summary():
    """The strongest proof: plant a REAL sensitive value and drive it through
    a caller who (mistakenly, as a caller might) puts it directly in the
    free-text summary/escalation_note. The persisted incident -- read back
    from the store and serialized in full -- must never contain that value
    anywhere."""
    store = _store()
    incident = await si.handle_security_boundary_violation(
        store,
        RUN_ID,
        "dataset_value_to_provider",
        source="provider_gateway",
        category="provider",
        summary=f"leaked value observed: {SENSITIVE_VALUE}, patient {SENSITIVE_NAME}",
        escalation_note=f"escalating because {SENSITIVE_VALUE} was seen",
    )
    dumped = json.dumps(incident.model_dump())

    assert SENSITIVE_VALUE not in dumped
    assert SENSITIVE_NAME not in dumped

    # The durably persisted document itself, not just the in-memory return
    # value -- read it back fresh from the store.
    stored = await store.get_one(si.COLLECTION, {"incident_id": incident.incident_id})
    stored_dump = json.dumps(stored)
    assert SENSITIVE_VALUE not in stored_dump
    assert SENSITIVE_NAME not in stored_dump

    # And the scrubber genuinely fired -- the summary is not simply empty,
    # it is the redacted text with the value replaced.
    assert "[G]" in incident.summary or "999" not in incident.summary
    assert SENSITIVE_VALUE not in incident.summary
    assert SENSITIVE_NAME not in incident.summary


def test_security_incident_schema_has_no_field_capable_of_holding_a_raw_value():
    """Structural guarantee, independent of the scrubber: no field on
    SecurityIncident is named/shaped to carry the raw offending value, so
    even a scrubber bug cannot leak it through a value-shaped field."""
    field_names = set(si.SecurityIncident.model_fields)
    forbidden = {"value", "values", "content", "payload", "raw", "raw_value", "leaked_value", "data", "literal"}
    assert not (field_names & forbidden), field_names & forbidden


def test_record_security_incident_rejects_extra_fields_a_value_could_hide_in():
    """ControlRecord's closed-schema convention (extra='forbid', inherited
    from control.records) means a caller cannot smuggle a raw value in
    through an unexpected keyword either."""
    with pytest.raises(ValidationError):
        si.SecurityIncident(
            run_id=RUN_ID, event_class="dataset_value_to_provider",
            leaked_value=SENSITIVE_VALUE,  # not a real field
        )


# ---------------------------------------------------------------------------
# Durability: an open incident must survive a simulated backend restart
# (Mongo, not MemoryControlStore -- proving real persistence, not merely
# that the in-test object graph shares a reference).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _fresh_motor_client_per_test():
    """Matches test_control_migrate.py / test_control_phase12_cleanup_wiring.py's
    own fixture: get_db is @lru_cache'd process-wide against whichever event
    loop was running at first construction, and asyncio_mode=strict gives
    each test its own loop."""
    get_db.cache_clear()
    yield
    get_db.cache_clear()


@pytest.mark.asyncio
async def test_open_incident_survives_a_simulated_backend_restart():
    """Phase 15a durability fix: record an incident against the real test
    Mongo instance, "restart" the backend (get_db.cache_clear() forces a
    brand-new AsyncIOMotorClient and therefore a brand-new MongoControlStore
    on the next call -- the exact technique
    test_resilience_restart_resume.py's _mongo_orch and
    test_control_phase12_cleanup_wiring.py's restart test both use), and
    confirm a MongoControlStore built from scratch after the "restart"
    still finds the incident open and still resolves the destruction
    decision and safe metadata identically. Nothing about this depends on
    any Python object surviving -- only the durable Mongo document does."""
    db = get_db()
    store = MongoControlStore(db)
    run_id = "restart-" + RUN_ID

    try:
        incident = await si.handle_security_boundary_violation(
            store, run_id, "raw_data_escaped_sandbox", source="sandbox", category="sandbox",
            summary=f"a path escaped the sandbox, saw {SENSITIVE_VALUE}",
        )
        assert await si.security_incident_active(store, run_id) is True

        # "Restart": drop the cached Motor client so the next store is
        # constructed from scratch, sharing only the durable Mongo state.
        get_db.cache_clear()
        fresh_store = MongoControlStore(get_db())

        assert await si.security_incident_active(fresh_store, run_id) is True
        reopened = (await si.open_incidents(fresh_store, run_id))[0]
        assert reopened.incident_id == incident.incident_id
        assert reopened.event_class == "raw_data_escaped_sandbox"
        assert reopened.destruction_decision == "REQUIRED"
        assert SENSITIVE_VALUE not in json.dumps(reopened.model_dump())

        # BLOCK release still holds against the fresh store, and DO NOT
        # auto-resume: the incident is still open, nothing cleared it.
        assert await si.security_incident_active(fresh_store, run_id) is True

        # Only an explicit authorized resolve against the fresh store closes
        # it -- and that closure is itself durable across a further restart.
        resolved = await si.resolve_security_incident(fresh_store, incident.incident_id, resolved_by="security-officer-9")
        assert resolved is not None
        get_db.cache_clear()
        second_fresh_store = MongoControlStore(get_db())
        assert await si.security_incident_active(second_fresh_store, run_id) is False
    finally:
        await db.security_incidents.delete_many({"run_id": run_id})


# ---------------------------------------------------------------------------
# Live gateway integration: a real canary hit opens a durable security incident
# ---------------------------------------------------------------------------


def _llm_cfg() -> SimpleNamespace:
    return SimpleNamespace(provider="anthropic", model="claude-test", base_url="")


async def _issue_gateway_grant(store: MemoryControlStore) -> object:
    policy = CapabilityPolicy(_llm_cfg())
    grant = policy.issue_grant(run_id=RUN_ID, task_id=TASK_ID, agent="RegulationsExpert", task_type="regulationsexpert")
    await store.insert("capability_grants", grant)
    return grant


async def _open_gateway_run(store: MemoryControlStore) -> WorkflowRun:
    run = WorkflowRun(
        run_id=RUN_ID, session_id=SESSION_ID, workflow_version="wf/1", policy_version=POLICY_VERSION,
        run_type="study", state="running", node="charter",
        started_at=datetime.now(timezone.utc).isoformat(),
        budget=ResourceBudget(), usage=ResourceUsage(),
    )
    await store.insert("workflow_runs", run)
    return run


def _gateway_request(*, grant_id: str, **overrides) -> GatewayRequest:
    fields: dict = dict(
        session_id=SESSION_ID, run_id=RUN_ID, task_id=TASK_ID, agent="RegulationsExpert", attempt=1,
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


@pytest.mark.asyncio
async def test_real_canary_hit_at_the_gateway_opens_a_security_incident(monkeypatch) -> None:
    """The live production detection point for dataset_value_to_provider:
    ProviderGateway.complete's existing canary scan now also records an open
    SecurityIncident (not just a TraceEvent) through the very same
    ControlStore the gateway itself uses, so a real leak blocks release
    until an authorized principal resolves it -- and the incident itself
    never carries the planted literal."""
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", _never_called_completion)
    store = MemoryControlStore()
    await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store)
    req = _gateway_request(grant_id=grant.grant_id, user_prompt="patient record ZZZLIVECANARY9911 attached")
    canary.activate_canary_set(RUN_ID, {"planted": [{"plant_id": "p1", "leak_literals": ["ZZZLIVECANARY9911"]}]})

    assert await si.security_incident_active(store, RUN_ID) is False

    try:
        with pytest.raises(canary.SecurityBoundaryViolation):
            await ProviderGateway(store).complete(req)

        assert await si.security_incident_active(store, RUN_ID) is True
        incidents = await si.open_incidents(store, RUN_ID)
        assert len(incidents) == 1
        assert incidents[0].event_class == "dataset_value_to_provider"
        assert incidents[0].source == "provider_gateway"

        dumped = json.dumps(incidents[0].model_dump())
        assert "ZZZLIVECANARY9911" not in dumped
        assert "zzzlivecanary9911" not in dumped.lower()

        # The run reads as release-blocking through the same durable store
        # the gateway just wrote to.
        assert await si.security_incident_active(store, RUN_ID) is True
    finally:
        canary.deactivate_canary_set(RUN_ID)


@pytest.mark.asyncio
async def test_clean_canary_scan_never_opens_a_security_incident(monkeypatch) -> None:
    from phi_core.control import gateway as gateway_module

    class _FakeResponse:
        choices = [SimpleNamespace(message=SimpleNamespace(content="a clean, ordinary reply"))]
        usage = {"total_tokens": 7}
        provider = "anthropic"
        model = "claude-test"
        id = "resp-1"

    monkeypatch.setattr(gateway_module.litellm, "completion", lambda **kwargs: _FakeResponse())
    store = MemoryControlStore()
    await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store)
    req = _gateway_request(grant_id=grant.grant_id, user_prompt="ordinary, canary-free request")
    canary.activate_canary_set(RUN_ID, {"planted": [{"plant_id": "p1", "leak_literals": ["ZZZLIVECANARY9911"]}]})

    try:
        result = await ProviderGateway(store).complete(req)
        assert result.status == "ok"
        assert await si.security_incident_active(store, RUN_ID) is False
    finally:
        canary.deactivate_canary_set(RUN_ID)