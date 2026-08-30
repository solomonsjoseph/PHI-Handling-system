"""SECURITY_BOUNDARY_VIOLATION incident handling (spec section 71).

A run-scoped registry and handler for security-boundary incidents. Section 71
names six event classes, each with a structural detection site in the control
plane:

  - ``dataset_value_to_provider``     -> ``gateway.ProviderGateway.complete``
    (the sole production LiteLLM / research-tool boundary; a leak canary hit
    there means a dataset value was about to leave for a provider).
  - ``dataset_value_in_trace``        -> ``trace_sanitizer.sanitize_payload`` /
    the trace write path (a dataset value persisted in a ``TraceEvent``).
  - ``raw_data_escaped_sandbox``      -> ``sandbox.validate_sandbox_path``
    (``SandboxPathViolation`` fires the moment a path leaves the run
    workspace).
  - ``cross_run_data_access``         -> ``handoff.py``'s run-identity check
    (``cross_run_reference``) and any cross-run store read.
  - ``unauthorized_sensitive_review`` -> ``authorization.authorize_capability``
    / the reviewer-preview and human-review authorization checks.
  - ``provider_bypass_sensitive_content`` -> the gateway's provider/endpoint
    mismatch and restricted-content refusal paths.

The action sequence mandated by section 71 is, in order: STOP the unsafe
processing, BLOCK further unsafe dispatch, ISOLATE the run, PRESERVE safe
incident metadata only, ESCALATE to authorized security review, BLOCK release,
DETERMINE whether destruction is required, and DO NOT automatically resume.

This module implements the whole sequence as data plus explicit functions:

  * STOP/BLOCK                                         -- the caller stops its own
    unsafe work (the gateway already reconciles the run budget and raises
    ``canary.SecurityBoundaryViolation``; nothing here swallows that).
  * PRESERVE safe metadata only                        -- :class:`SecurityIncident`
    carries only safe fields. It has no field through which a leaked value can
    enter, and every free-text field is scrubbed through
    ``security.scrub_persisted_text`` on write as defense in depth.
  * ISOLATE / ESCALATE / DETERMINE destruction         -- recorded on the incident.
  * BLOCK release                                      -- ``security_incident_active``
    feeds ``FinalAssuranceGate``'s ``no_unresolved_security_incident`` condition.
  * DO NOT automatically resume                        -- an incident stays open
    until ``resolve_security_incident`` is called with an explicit authorized
    principal; no code path clears it on its own.

The registry is process-local and append-only, matching the lifetime of a run:
both the boundary detection and the release gate run inside the one backend
process. Nothing here is serialized to disk. "Do not copy the leaked sensitive
value into incident telemetry" holds structurally, not just by convention.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import Field

from phi_core.security import scrub_persisted_text

from .records import ControlRecord

# --- section 71's six event classes ----------------------------------------


SecurityIncidentEventClass = Literal[
    "dataset_value_to_provider",
    "dataset_value_in_trace",
    "raw_data_escaped_sandbox",
    "cross_run_data_access",
    "unauthorized_sensitive_review",
    "provider_bypass_sensitive_content",
]

DestructionDecision = Literal["NOT_REQUIRED", "REQUIRED", "UNDETERMINED"]

IncidentStatus = Literal["open", "resolved"]


def _new_id() -> str:
    return uuid4().hex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------


class SecurityIncident(ControlRecord):
    """One security-boundary incident (section 71).

    Deliberately carries no field capable of holding the leaked sensitive
    value: there is no ``value``/``content``/``payload``/``raw`` slot, and the
    two free-text fields (``summary``, ``escalation_note``) are scrubbed on
    write. ``egress_digest`` is an opaque keyed HMAC of the outbound payload,
    usable for correlation but not reversible to its content.
    """

    incident_id: str = Field(default_factory=_new_id)
    run_id: str
    event_class: SecurityIncidentEventClass
    source: str = ""                 # structural detection site
    category: str = ""               # safe category for triage
    summary: str = ""                # safe, scrubbed description
    escalation_note: str = ""        # safe, scrubbed escalation reference
    egress_digest: str = ""          # opaque keyed digest, never the payload
    destruction_decision: DestructionDecision = "UNDETERMINED"
    status: IncidentStatus = "open"
    detected_at: str = Field(default_factory=_now)
    resolved_at: str = ""
    resolved_by: str = ""            # authorized principal, explicit action only


# --- run-scoped, append-only registry --------------------------------------
# Process-local, matches the lifetime of the one backend process that both
# detects a violation and later evaluates the release gate. Never persisted.
_INCIDENTS: dict[str, list[SecurityIncident]] = {}


def record_security_incident(
    run_id: str,
    event_class: SecurityIncidentEventClass,
    *,
    source: str = "",
    category: str = "",
    summary: str = "",
    escalation_note: str = "",
    egress_digest: str = "",
) -> SecurityIncident:
    """Record a boundary violation with safe metadata only. PRESERVE-safe:
    both free-text fields are scrubbed so a leaked value can never reach the
    stored record even if a caller mistakenly passes one in ``summary``."""
    incident = SecurityIncident(
        run_id=run_id,
        event_class=event_class,
        source=source,
        category=category,
        summary=scrub_persisted_text(summary),
        escalation_note=scrub_persisted_text(escalation_note),
        egress_digest=egress_digest,
    )
    _INCIDENTS.setdefault(run_id, []).append(incident)
    return incident


def open_incidents(run_id: str) -> tuple[SecurityIncident, ...]:
    """The still-open incidents for ``run_id`` (append-order preserved)."""
    return tuple(i for i in _INCIDENTS.get(run_id, []) if i.status == "open")


def security_incident_active(run_id: str) -> bool:
    """True when ``run_id`` has at least one unresolved incident. This is the
    producer ``FinalAssuranceGate``'s ``no_unresolved_security_incident``
    condition reads, so an open incident BLOCKs release."""
    return any(i.status == "open" for i in _INCIDENTS.get(run_id, []))


def determine_destruction_required(incident: SecurityIncident) -> DestructionDecision:
    """DETERMINE whether destruction is required; a decision point, never an
    automatic destroy. Raw data that crossed the sandbox boundary is the one
    class where destruction of the escaped copy is the safe default; every
    other class is left UNDETERMINED for authorized review to decide."""
    if incident.event_class == "raw_data_escaped_sandbox":
        return "REQUIRED"
    return "UNDETERMINED"


def resolve_security_incident(
    incident_id: str,
    *,
    resolved_by: str,
    destruction_decision: DestructionDecision = "NOT_REQUIRED",
) -> SecurityIncident | None:
    """Close one incident by explicit authorized action. Returns ``None`` (and
    changes nothing) when the id is unknown or already resolved, so there is
    no accidental resume. ``resolved_by`` is required and non-empty: the
    "DO NOT automatically resume" rule means a principal must act, nothing
    clears the open status on its own."""
    if not resolved_by:
        raise ValueError("resolved_by is required: incidents are only closed by explicit authorized action")
    for run in _INCIDENTS.values():
        for incident in run:
            if incident.incident_id == incident_id:
                if incident.status != "open":
                    return None
                incident.status = "resolved"
                incident.resolved_at = _now()
                incident.resolved_by = resolved_by
                incident.destruction_decision = destruction_decision
                return incident
    return None


def handle_security_boundary_violation(
    run_id: str,
    event_class: SecurityIncidentEventClass,
    *,
    source: str = "",
    category: str = "",
    summary: str = "",
    escalation_note: str = "",
    egress_digest: str = "",
) -> SecurityIncident:
    """The ordered section-71 action sequence as one call.

    STOP/BLOCK are the caller's own act (it must not continue the unsafe
    dispatch); this records the ISOLATE / PRESERVE / ESCALATE / DETERMINE
    steps and enforces the BLOCK-release and DO-NOT-auto-resume consequences:

      * PRESERVE safe metadata only (scrubbed, no value field).
      * DETERMINE destruction (recommendation only, no destroy).
      * ESCALATE to authorized review (safe reference only).
      * With an open incident, ``security_incident_active(run_id)`` is True and
        FinalAssuranceGate blocks release; only ``resolve_security_incident``
        (explicit authorized action) clears it.
    """
    incident = record_security_incident(
        run_id,
        event_class,
        source=source,
        category=category,
        summary=summary,
        escalation_note=escalation_note,
        egress_digest=egress_digest,
    )
    incident.destruction_decision = determine_destruction_required(incident)
    return incident


def reset_security_incidents() -> None:
    """Test-only: clear every run's incident registry in-process."""
    _INCIDENTS.clear()