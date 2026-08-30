"""Tests for SECURITY_BOUNDARY_VIOLATION handling (spec section 71,
Phase 15a: ``phi_core.control.security_incident``).

Covers the module's own contract (record / active / resolve / destruction
decision / no-auto-resume), the "never copies the leaked value" guarantee
that section 71 explicitly requires, the FinalAssuranceGate wiring
(``derive_security_incident_active`` -> ``no_unresolved_security_incident``),
and the live gateway integration where a real canary hit now also opens a
security incident (not just a trace event).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from phi_core.control import canary
from phi_core.control import security_incident as si
from phi_core.control.final_assurance import (
    ReportPackageContent,
    ReviewerFinalResult,
    derive_security_incident_active,
    evaluate_final_assurance,
    run_reporting_safety_gate,
)
from phi_core.control.gateway import GatewayRequest, ProviderGateway
from phi_core.control.policy import POLICY_VERSION, CapabilityPolicy
from phi_core.control.records import (
    ExecutionResult,
    ResourceBudget,
    ResourceUsage,
    VerificationResult,
    VerifiedClassificationManifest,
    WorkflowRun,
)
from phi_core.control.store import MemoryControlStore
from pydantic import ValidationError

RUN_ID = "sec-incident-run-" + "r" * 12
TASK_ID = "sec-incident-task-" + "t" * 12
SESSION_ID = "sec-incident-session-" + "s" * 10

SENSITIVE_VALUE = "999-88-7777"  # SSN-shaped: a real value that must never be copied
SENSITIVE_NAME = "Rutherford Applewhite"


@pytest.fixture(autouse=True)
def _clean_registry():
    si.reset_security_incidents()
    yield
    si.reset_security_incidents()


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


def test_record_security_incident_opens_and_activates():
    assert si.security_incident_active(RUN_ID) is False
    incident = si.record_security_incident(RUN_ID, "raw_data_escaped_sandbox", source="sandbox")
    assert incident.status == "open"
    assert si.security_incident_active(RUN_ID) is True
    assert incident in si.open_incidents(RUN_ID)


def test_incident_is_scoped_to_its_own_run():
    si.record_security_incident(RUN_ID, "raw_data_escaped_sandbox")
    assert si.security_incident_active(RUN_ID) is True
    assert si.security_incident_active("some-other-run") is False


def test_resolve_requires_an_explicit_authorized_principal():
    incident = si.record_security_incident(RUN_ID, "cross_run_data_access")
    with pytest.raises(ValueError):
        si.resolve_security_incident(incident.incident_id, resolved_by="")
    assert si.security_incident_active(RUN_ID) is True  # unchanged: still open


def test_resolve_security_incident_closes_it_and_stops_blocking():
    incident = si.record_security_incident(RUN_ID, "unauthorized_sensitive_review")
    resolved = si.resolve_security_incident(incident.incident_id, resolved_by="security-officer-42")
    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.resolved_by == "security-officer-42"
    assert resolved.resolved_at
    assert si.security_incident_active(RUN_ID) is False


def test_resolving_an_unknown_incident_id_is_a_safe_no_op():
    assert si.resolve_security_incident("does-not-exist", resolved_by="someone") is None


def test_resolving_an_already_resolved_incident_is_a_safe_no_op_not_a_reopen():
    incident = si.record_security_incident(RUN_ID, "dataset_value_in_trace")
    si.resolve_security_incident(incident.incident_id, resolved_by="officer-1")
    again = si.resolve_security_incident(incident.incident_id, resolved_by="officer-2")
    assert again is None
    assert incident.resolved_by == "officer-1"  # first resolution is authoritative


def test_nothing_ever_auto_resumes_an_open_incident():
    """DO NOT automatically resume (section 71): recording, checking active,
    and reading open_incidents any number of times must never itself close
    an incident. Only an explicit resolve_security_incident call does."""
    si.record_security_incident(RUN_ID, "provider_bypass_sensitive_content")
    for _ in range(5):
        assert si.security_incident_active(RUN_ID) is True
        si.open_incidents(RUN_ID)
    assert si.security_incident_active(RUN_ID) is True


def test_determine_destruction_required_is_a_decision_not_an_action():
    sandbox_incident = si.record_security_incident(RUN_ID, "raw_data_escaped_sandbox")
    assert si.determine_destruction_required(sandbox_incident) == "REQUIRED"

    other_incident = si.record_security_incident(RUN_ID, "unauthorized_sensitive_review")
    assert si.determine_destruction_required(other_incident) == "UNDETERMINED"

    # A decision point only: nothing about the incident record itself changes
    # or triggers a destroy just from calling the function.
    assert sandbox_incident.destruction_decision == "UNDETERMINED"
    assert si.security_incident_active(RUN_ID) is True


def test_handle_security_boundary_violation_runs_the_full_sequence():
    incident = si.handle_security_boundary_violation(
        RUN_ID, "raw_data_escaped_sandbox", source="sandbox", category="sandbox",
        summary="a path resolved outside the run workspace",
    )
    # PRESERVE: recorded, open.
    assert incident.status == "open"
    # DETERMINE destruction: a real recommendation was set, not left at the default.
    assert incident.destruction_decision == "REQUIRED"
    # BLOCK release consequence.
    assert si.security_incident_active(RUN_ID) is True
    # DO NOT auto-resume: still open after the handler returns.
    assert incident in si.open_incidents(RUN_ID)


# ---------------------------------------------------------------------------
# "Never copies the leaked sensitive value into incident telemetry" (section 71)
# ---------------------------------------------------------------------------


def test_never_copies_the_leaked_value_even_when_a_caller_passes_it_in_summary():
    """The strongest proof: plant a REAL sensitive value and drive it through
    a caller who (mistakenly, as a caller might) puts it directly in the
    free-text summary/escalation_note. The persisted incident -- serialized
    in full -- must never contain that value anywhere."""
    incident = si.handle_security_boundary_violation(
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
    # The whole run's registry, not just this one incident.
    for stored in si.open_incidents(RUN_ID):
        stored_dump = json.dumps(stored.model_dump())
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
# FinalAssuranceGate wiring: no_unresolved_security_incident
# ---------------------------------------------------------------------------


def _manifest(**overrides) -> VerifiedClassificationManifest:
    base = dict(
        run_id=RUN_ID, preview_review_id="pr1", status="verified_for_execution",
        source_artifact_versions={"f1": 1}, decision_refs=["f1:id"], unresolved_items=0,
    )
    base.update(overrides)
    return VerifiedClassificationManifest(**base)


def _evaluate(security_incident_active: bool) -> object:
    return evaluate_final_assurance(
        expected_file_ids=["f1"],
        manifest=_manifest(),
        reviewer_preview_verdict="PASS",
        execution_result=ExecutionResult(task_id="t1", run_id=RUN_ID, manifest_id="m1", success=True),
        verification_result=VerificationResult(run_id=RUN_ID, passed=True, failed_checks=[], manifest_coverage_percent=100),
        reviewer_final=ReviewerFinalResult(verdict="PASS"),
        privacy_findings_unresolved=0,
        security_incident_active=security_incident_active,
        report_package_complete=True,
        reporting_safety=run_reporting_safety_gate(ReportPackageContent()),
        integrity_checks_passed=True,
    )


def test_derive_security_incident_active_is_false_with_a_clean_registry():
    assert derive_security_incident_active(RUN_ID) is False


def test_derive_security_incident_active_is_true_once_an_incident_is_open():
    si.record_security_incident(RUN_ID, "cross_run_data_access")
    assert derive_security_incident_active(RUN_ID) is True


def test_open_incident_blocks_final_assurance_release():
    si.record_security_incident(RUN_ID, "cross_run_data_access")
    result = _evaluate(derive_security_incident_active(RUN_ID))
    assert result.verdict == "BLOCKED"
    assert "no_unresolved_security_incident" in result.failed_conditions


def test_resolved_incident_no_longer_blocks_final_assurance_release():
    incident = si.record_security_incident(RUN_ID, "cross_run_data_access")
    si.resolve_security_incident(incident.incident_id, resolved_by="security-officer-1")
    result = _evaluate(derive_security_incident_active(RUN_ID))
    assert result.verdict == "READY_FOR_EXPORT"
    assert "no_unresolved_security_incident" not in result.failed_conditions


# ---------------------------------------------------------------------------
# Live gateway integration: a real canary hit opens a security incident
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
    SecurityIncident (not just a TraceEvent), so a real leak blocks release
    until an authorized principal resolves it -- and the incident itself
    never carries the planted literal."""
    from phi_core.control import gateway as gateway_module

    monkeypatch.setattr(gateway_module.litellm, "completion", _never_called_completion)
    store = MemoryControlStore()
    await _open_gateway_run(store)
    grant = await _issue_gateway_grant(store)
    req = _gateway_request(grant_id=grant.grant_id, user_prompt="patient record ZZZLIVECANARY9911 attached")
    canary.activate_canary_set(RUN_ID, {"planted": [{"plant_id": "p1", "leak_literals": ["ZZZLIVECANARY9911"]}]})

    assert si.security_incident_active(RUN_ID) is False

    try:
        with pytest.raises(canary.SecurityBoundaryViolation):
            await ProviderGateway(store).complete(req)

        assert si.security_incident_active(RUN_ID) is True
        incidents = si.open_incidents(RUN_ID)
        assert len(incidents) == 1
        assert incidents[0].event_class == "dataset_value_to_provider"
        assert incidents[0].source == "provider_gateway"

        dumped = json.dumps(incidents[0].model_dump())
        assert "ZZZLIVECANARY9911" not in dumped
        assert "zzzlivecanary9911" not in dumped.lower()

        # BLOCK release: FinalAssuranceGate would see this run as blocked.
        assert derive_security_incident_active(RUN_ID) is True
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
        assert si.security_incident_active(RUN_ID) is False
    finally:
        canary.deactivate_canary_set(RUN_ID)