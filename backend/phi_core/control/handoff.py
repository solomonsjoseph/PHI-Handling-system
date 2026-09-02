"""Phase 3: HandoffGateway (target-architecture reconciliation, local
reference doc docs/MASTER_ARCHITECTURE_V2.md, never committed).

``records.py``'s Phase 1 addendum flagged HandoffEnvelope/HandoffResult as
genuinely missing but not yet buildable: this codebase's Manager/
Manager sequences every agent today (ADR 0006), so there was no
direct agent-to-agent handoff to gate. This module builds that gate on its
own, ahead of any caller: a deny-by-default validator that will later let
Judge, Reviewer, Regulations Expert (``RegulationsExpert``), PHI Methods Expert
(``PHIMethodsExpert``), Schema, Lexicon, and Instrument exchange a bounded, typed
message directly instead of solely relaying through Manager.

Deterministic, no-LLM. Wired into ``phi_core/agents/`` by Wave R-c step 6:
``agents/manager.py``'s guardian query broker (``ask_schema``,
``ask_instrument``, ``ask_lexicon``) records each Judge query as a
governed handoff through ``handoff()`` on the fixed ``(Judge, Schema)``,
``(Judge, Instrument)``, ``(Judge, Lexicon)`` edges. Remaining edges
arrive with their agents in Phases 6 to 8.

Composes existing controls rather than reimplementing them:
``authorization.get_contract`` (contract lookup), ``secrets_scan.
contains_secret`` (credential-shape detection), ``gateway.
_contains_restricted_content`` (the same post-scrub residual-PHI check
``ProviderGateway.complete`` and ``source_projection`` already apply), and
``events.TraceEventStore`` (the sole authorized ``trace_events`` writer,
which itself sanitizes ``payload`` before persisting -- D66). This module
additionally keeps the trace payload it builds to sender/recipient/
allowed/reason metadata only, never the handoff's own payload content, so
a denied handoff's dataset-value or secret canary is never written to the
trace store in the first place -- belt and suspenders with D66's own
sanitize-before-append.

Role identities are the existing registered ``AgentManifest`` names
(``Judge``, ``RegulationsExpert``, ``PHIMethodsExpert``, ``Schema``, ``Lexicon``,
``Instrument``, ``Reviewer``) rather than new target-architecture strings:
"Regulations Expert" and "PHI Methods Expert" from the spec are this
codebase's ``RegulationsExpert`` and ``PHIMethodsExpert`` under their current names. Reusing
the names already registered in ``policy.MANIFESTS`` means every check
that needs "a registered contract" (checks 1, 2, 5, 9) works against real
data with no parallel, not-yet-real registry to maintain.

Checks 1-10 run in order and every one is a plain ``(bool, reason_code,
detail)`` return, never an exception, for a policy denial -- only a
programming error (a malformed store, an unknown edge with no schema
registered) raises. Checks 6 (minimum-necessary) and 10 (output schema)
both consult the same per-edge pydantic schema in ``EDGE_SCHEMAS``: 6
checks the payload's *keys* are a subset of the schema's declared fields
(deny-by-default on any extra key), 10 then runs full validation (types,
required fields) via that schema. This is one schema doing two jobs the
spec lists as separate checks, not two schemas doing one job: a second,
parallel allow-list of field names would drift from the schema the
moment either one changed.

Check 11 (correction/retry budget, spec section 48) is the one
deliberate exception to "never an exception": it raises
``policy.BudgetExceeded`` instead of returning a denial tuple, matching
every other D5 ceiling refusal already in this codebase (``gateway.py``,
``artifacts.py``, ``runs.py``, ``manager.py``), each of which
is always paired with a ``TraceEvent(outcome="budget_exceeded")``
recorded before re-raising, never expressed as a ``HandoffReasonCode``
string. ``handoff()`` reflects this: a budget refusal produces that
trace event and re-raises, with no ``HandoffResult`` constructed for it.
"""
from __future__ import annotations

from typing import Any, Mapping

from pydantic import ValidationError

from . import authorization, limits
from .events import TraceEventStore
from .gateway import _contains_restricted_content
from .policy import BudgetExceeded, CapabilityDenied
from .records import (
    ControlRecord,
    HandoffEnvelope,
    HandoffReasonCode,
    HandoffResult,
    MethodFinding,
    RegulatoryFinding,
    TraceEvent,
)
from .secrets_scan import contains_secret
from .store import ControlStore

# ---- role identities (existing registered AgentManifest names) ------------

JUDGE = "Judge"
REGULATIONS_EXPERT = "RegulationsExpert"
METHODS_EXPERT = "PHIMethodsExpert"
SCHEMA = "Schema"
LEXICON = "Lexicon"
INSTRUMENT = "Instrument"
REVIEWER = "Reviewer"

# ---- allowed topology (spec section 86) ------------------------------------
# Everything not listed here is blocked -- including the reverse direction
# of a one-way edge (Judge->Schema does not imply Schema->Judge).

ALLOWED_EDGES: frozenset[tuple[str, str]] = frozenset({
    (JUDGE, REGULATIONS_EXPERT), (REGULATIONS_EXPERT, JUDGE),
    (JUDGE, METHODS_EXPERT), (METHODS_EXPERT, JUDGE),
    (REVIEWER, JUDGE), (JUDGE, REVIEWER),
    (JUDGE, SCHEMA), (JUDGE, LEXICON), (JUDGE, INSTRUMENT),
})


# ---- minimal per-edge payload schemas (check 10, and check 6's key set) ---
# ``ControlRecord`` already forbids extra fields (``extra="forbid"``), which
# is what makes it usable for the minimum-necessary key check in ``_evaluate``
# below without a second, parallel allow-list.


class RegulatoryQuestion(ControlRecord):
    hipaa_category: str
    question: str


class MethodQuestion(ControlRecord):
    hipaa_category: str
    question: str


class ReviewerHandoff(ControlRecord):
    decision_ids: list[str] = []
    note: str = ""


class RevisedArtifactHandoff(ControlRecord):
    """Judge -> Reviewer through revised artifact (spec section 11): the
    answering half of ReviewerHandoff's decision_ids/note conversation --
    Judge hands the same decision_ids back once revised, with a summary
    of what changed."""

    decision_ids: list[str] = []
    revision_summary: str = ""


class SchemaQuestion(ControlRecord):
    column: str
    file_id: str = ""


class LexiconQuestion(ControlRecord):
    column: str
    assumption: str = ""
    reasoning: str = ""


class InstrumentQuestion(ControlRecord):
    field_or_variable: str
    file_id: str = ""


EDGE_SCHEMAS: Mapping[tuple[str, str], type[ControlRecord]] = {
    (JUDGE, REGULATIONS_EXPERT): RegulatoryQuestion,
    (REGULATIONS_EXPERT, JUDGE): RegulatoryFinding,
    (JUDGE, METHODS_EXPERT): MethodQuestion,
    (METHODS_EXPERT, JUDGE): MethodFinding,
    (REVIEWER, JUDGE): ReviewerHandoff,
    (JUDGE, REVIEWER): RevisedArtifactHandoff,
    (JUDGE, SCHEMA): SchemaQuestion,
    (JUDGE, LEXICON): LexiconQuestion,
    (JUDGE, INSTRUMENT): InstrumentQuestion,
}


def _references_other_run(value: Any, run_id: str) -> bool:
    """Check 4: a handoff whose payload names a ``run_id`` other than its
    own is a cross-run leak, regardless of how deep in the payload that key
    sits (an evidence/finding record nested under a list, for instance)."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key == "run_id" and child != run_id:
                return True
            if _references_other_run(child, run_id):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_references_other_run(child, run_id) for child in value)
    return False


# ---- check 11: correction/retry budget (spec section 48) -------------------
# section 48 names six budgeted categories; only the four below correspond
# to an agent-to-agent handoff edge -- "provider_retry" and "tool_retry"
# gate LLM/tool retries elsewhere in the system (ProviderGateway, tool
# dispatch), not a HandoffGateway edge, so they have no entry here. An
# edge with no entry is unbounded by this check (none exist among today's
# ALLOWED_EDGES; every edge above maps to one of the four categories).
_EDGE_ATTEMPT_CATEGORY: Mapping[tuple[str, str], str] = {
    (JUDGE, REVIEWER): "judge_reviewer",
    (REVIEWER, JUDGE): "judge_reviewer",
    (JUDGE, REGULATIONS_EXPERT): "judge_regulations",
    (REGULATIONS_EXPERT, JUDGE): "judge_regulations",
    (JUDGE, METHODS_EXPERT): "judge_methods",
    (METHODS_EXPERT, JUDGE): "judge_methods",
    (JUDGE, SCHEMA): "specialist_clarification",
    (JUDGE, LEXICON): "specialist_clarification",
    (JUDGE, INSTRUMENT): "specialist_clarification",
}


class HandoffGateway:
    """Validates one agent-to-agent handoff and records the attempt.

    Not called from ``phi_core/agents/`` yet -- Manager still sequences
    every agent in the running pipeline. This is the standalone gate a
    later phase wires callers through.
    """

    def __init__(self, store: ControlStore, *, session_id: str) -> None:
        self._store = store
        self._session_id = session_id

    async def handoff(self, envelope: HandoffEnvelope) -> HandoffResult:
        trace = TraceEventStore(self._store, run_id=envelope.run_id, session_id=self._session_id)
        try:
            allowed, reason_code, detail = self._evaluate(envelope)
        except BudgetExceeded as exc:
            # Check 11's refusal: recorded the same way every other D5
            # ceiling refusal in this codebase is (outcome="budget_exceeded",
            # never "allowed"/"denied"), then re-raised -- there is no
            # HandoffResult for a budget refusal, matching the exception
            # (not tuple-return) contract check 11 uses.
            await trace.append(TraceEvent(
                run_id=envelope.run_id,
                seq=0,
                session_id=self._session_id,
                agent=envelope.sender,
                phase="handoff",
                direction=f"{envelope.sender}->{envelope.recipient}",
                input_class=envelope.data_class,
                output_class=envelope.data_class,
                outcome="budget_exceeded",
                status_text=str(exc),
                payload={
                    "handoff_id": envelope.handoff_id,
                    "sender": envelope.sender,
                    "recipient": envelope.recipient,
                    "allowed": False,
                    "reason": "attempt_budget_exceeded",
                },
            ))
            raise
        event = await trace.append(TraceEvent(
            run_id=envelope.run_id,
            seq=0,
            session_id=self._session_id,
            agent=envelope.sender,
            phase="handoff",
            direction=f"{envelope.sender}->{envelope.recipient}",
            input_class=envelope.data_class,
            output_class=envelope.data_class,
            # Deliberately not "blocked": that string is one of
            # events.TERMINAL_RUN_OUTCOMES, which forces the fenced
            # terminal-event path (a task_id + work_item lookup this
            # handoff attempt has neither). "denied" names the same
            # outcome without colliding with the run-lifecycle vocabulary.
            outcome="allowed" if allowed else "denied",
            gateway_decision=reason_code,
            # Metadata only -- the handoff's own payload never enters the
            # trace, allowed or not, so a blocked dataset-value/secret
            # canary cannot reach the trace store through this path.
            payload={
                "handoff_id": envelope.handoff_id,
                "sender": envelope.sender,
                "recipient": envelope.recipient,
                "allowed": allowed,
                "reason": reason_code,
            },
        ))
        return HandoffResult(
            handoff_id=envelope.handoff_id,
            run_id=envelope.run_id,
            sender=envelope.sender,
            recipient=envelope.recipient,
            allowed=allowed,
            reason_code=reason_code,
            detail=detail,
            trace_event_id=event.event_id,
        )

    def _evaluate(self, envelope: HandoffEnvelope) -> tuple[bool, HandoffReasonCode, str]:
        # 1. sender authorization
        try:
            sender_contract = authorization.get_contract(envelope.sender)
        except CapabilityDenied:
            return False, "sender_unregistered", f"no contract for sender {envelope.sender!r}"

        # 2. recipient authorization
        try:
            recipient_contract = authorization.get_contract(envelope.recipient)
        except CapabilityDenied:
            return False, "recipient_unregistered", f"no contract for recipient {envelope.recipient!r}"

        # 3. topology validation
        edge = (envelope.sender, envelope.recipient)
        if edge not in ALLOWED_EDGES:
            return False, "topology_blocked", f"{envelope.sender} -> {envelope.recipient} is not an allowed edge"

        # 4. run identity (cross-run leak prevention)
        if _references_other_run(envelope.payload, envelope.run_id):
            return False, "cross_run_reference", "payload references a run_id other than the handoff's own"

        # 5. data classification check (reuses AgentManifest.accepted_input_classes,
        # the same allow-list authorize_capability's check_data_class consults)
        if envelope.data_class not in recipient_contract.accepted_input_classes:
            return False, "data_class_forbidden", f"{envelope.recipient} does not accept {envelope.data_class!r}"

        schema_cls = EDGE_SCHEMAS[edge]

        # 6. minimum-necessary check (deny any payload key the edge schema
        # does not declare, before spending a full validation pass on it)
        extra_keys = set(envelope.payload) - set(schema_cls.model_fields)
        if extra_keys:
            return False, "not_minimum_necessary", f"unexpected payload keys: {sorted(extra_keys)}"

        # 7. dataset-value leak prevention (residual-PHI heuristic, same
        # check ProviderGateway.complete and source_projection apply)
        if _contains_restricted_content(envelope.payload):
            return False, "residual_phi_detected", "payload failed the residual-PHI heuristic"

        # 8. secret check
        if contains_secret(envelope.payload):
            return False, "secret_detected", "payload matched a credential/secret pattern"

        # 9. capability validation: the sender's own contract must grant
        # any tool the handoff asks the recipient to be handed.
        if envelope.requested_tool:
            granted = sender_contract.allowed_tools.get(envelope.requested_tool, 0)
            if granted <= 0:
                return False, "tool_not_granted", (
                    f"{envelope.sender} is not granted tool {envelope.requested_tool!r}"
                )

        # 10. output schema requirement (full validation: types + required fields)
        try:
            schema_cls.model_validate(envelope.payload)
        except ValidationError as exc:
            return False, "payload_schema_invalid", str(exc)

        # 11. correction/retry budget (spec section 48): unlike checks 1-10,
        # a budget refusal is not a (bool, reason_code, detail) denial --
        # it raises ``BudgetExceeded``, the same D5 ceiling-check pattern
        # every other budget refusal in this codebase already uses
        # (gateway.py, artifacts.py, runs.py, manager.py), so
        # ``handoff()`` can record it the same way: a TraceEvent with
        # outcome="budget_exceeded", then re-raise. attempt_number and
        # correction_number both count toward the ceiling -- a correction
        # round is still another round trip on the same edge.
        category = _EDGE_ATTEMPT_CATEGORY.get(edge)
        if category is not None:
            budget = limits.HANDOFF_ATTEMPT_BUDGET[category]
            rounds = envelope.attempt_number + envelope.correction_number
            if rounds > budget:
                raise BudgetExceeded(
                    f"{envelope.sender} -> {envelope.recipient} exceeded the "
                    f"{category!r} retry budget ({rounds} > {budget})"
                )

        return True, "", ""
