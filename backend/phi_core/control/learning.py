"""D16: controlled learning.

Inactive by default (``LEARNING_ENABLED=false``): every method on
:class:`LearningService` refuses outright unless the flag is set. Runtime
agents never import this module at all (proven statically by
``test_architecture_boundaries.py``) and hold no capability grant that can
write ``learning_proposals``/``learning_activations`` (proven by
``test_control_capability.py``) -- so even a proposal a running task
authors is inert data a human must evaluate, approve, and activate through
this service, never something the runtime itself can act on.

The workflow: ``propose`` (redacted-input-digest only, never raw content)
-> ``record_evaluation`` (offline, against versioned fixtures under
``backend/tests/fixtures/learning/``, and separately an adversarial pass)
-> ``approve`` (requires both evaluation kinds passing, and a
``lead_reviewer`` principal) -> a versioned ``LearningActivation`` starting
in ``shadow`` rollout -> ``promote_rollout`` through ``canary`` to ``full``
(a canary meeting its own criteria still requires the monitor's continued
pass; promotion to ``full`` refuses outright without it) ->
``record_monitor_result``, where a trip halts and reverts without human
action, restoring the prior good activation for the same target ->
``rollback``, the same restoration path but human-initiated.
"""
from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, get_args

from phi_core.paths import is_safe_scoped_id
from phi_core.security import reviewer_role, scrub_persisted_text

from .events import canonical_json
from .final_assurance import ReportingSafetyFinding, _scan_text_surface
from .records import (
    LearningActivation,
    LearningCase,
    LearningCaseSource,
    LearningEvaluation,
    LearningProposal,
)
from .store import ControlStore

_ROLLOUT_ORDER = ("shadow", "canary", "full")


def learning_enabled() -> bool:
    return os.environ.get("LEARNING_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def redact_digest(payload: Any) -> str:
    """Sha256 of a canonical JSON encoding of ``payload``. A
    ``LearningProposal`` records only this digest -- never the raw
    evidence, prompt text, or dataset content that motivated it."""
    document = payload if isinstance(payload, dict) else {"value": payload}
    return hashlib.sha256(canonical_json(document).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LearningError(RuntimeError):
    """Raised with a fixed, testable ``reason`` on any refusal."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason}: {detail}" if detail else reason)


class LearningService:
    def __init__(self, store: ControlStore) -> None:
        self._store = store

    def _require_enabled(self) -> None:
        if not learning_enabled():
            raise LearningError("learning_disabled")

    async def propose(
        self, *, kind: str, target: str, baseline_version: str, proposed_version: str,
        redacted_input: Any, rationale: str = "", created_by_task_id: str = "",
    ) -> LearningProposal:
        self._require_enabled()
        proposal = LearningProposal(
            kind=kind, target=target, baseline_version=baseline_version, proposed_version=proposed_version,
            redacted_input_digest=redact_digest(redacted_input), rationale=rationale,
            created_by_task_id=created_by_task_id,
        )
        await self._store.insert("learning_proposals", proposal)
        return proposal

    async def record_evaluation(
        self, *, proposal_id: str, fixture_set: str, metrics: dict[str, float], passed: bool,
        adversarial: bool = False,
    ) -> LearningEvaluation:
        self._require_enabled()
        proposal_doc = await self._store.get_one("learning_proposals", {"proposal_id": proposal_id})
        if proposal_doc is None:
            raise LearningError("proposal_missing", proposal_id)
        evaluation = LearningEvaluation(
            proposal_id=proposal_id, fixture_set=fixture_set, metrics=dict(metrics), passed=passed,
            adversarial=adversarial,
        )
        await self._store.insert("learning_evaluations", evaluation)
        proposal = LearningProposal.model_validate(proposal_doc)
        if passed and proposal.state == "proposed":
            await self._store.compare_and_set(
                "learning_proposals", {"proposal_id": proposal_id}, {"state": "proposed"},
                proposal.model_copy(update={"state": "evaluated"}),
            )
        return evaluation

    async def approve(self, *, proposal_id: str, principal: str) -> LearningActivation:
        """Requires a recorded, passing offline evaluation *and* a
        separate passing adversarial evaluation, plus an authorized
        ``lead_reviewer`` principal (D16's gate: "activation requires a
        recorded evaluation and an authorized human approval")."""
        self._require_enabled()
        if reviewer_role(principal) != "lead_reviewer":
            raise LearningError("not_lead_reviewer", principal)
        proposal_doc = await self._store.get_one("learning_proposals", {"proposal_id": proposal_id})
        if proposal_doc is None:
            raise LearningError("proposal_missing", proposal_id)
        proposal = LearningProposal.model_validate(proposal_doc)
        if proposal.state != "evaluated":
            raise LearningError("proposal_not_evaluated", f"state={proposal.state!r}")
        evaluations = [
            LearningEvaluation.model_validate(d)
            for d in await self._store.find_many("learning_evaluations", {"proposal_id": proposal_id})
        ]
        if not any(e.passed and not e.adversarial for e in evaluations):
            raise LearningError("missing_offline_evaluation", proposal_id)
        if not any(e.passed and e.adversarial for e in evaluations):
            raise LearningError("missing_adversarial_evaluation", proposal_id)
        updated_proposal = proposal.model_copy(update={"state": "approved"})
        if not await self._store.compare_and_set(
            "learning_proposals", {"proposal_id": proposal_id}, {"state": "evaluated"}, updated_proposal,
        ):
            raise LearningError("proposal_state_race", proposal_id)
        activation = LearningActivation(
            proposal_id=proposal_id, version=proposal.proposed_version, approved_by=principal, rollout="shadow",
        )
        await self._store.insert("learning_activations", activation)
        return activation

    async def promote_rollout(self, *, activation_id: str, rollout: str) -> LearningActivation:
        """Rollout and monitoring are two separate gates (D16): a canary
        meeting its own criteria still requires the monitor's continued
        pass before promotion to ``full`` -- never bypassed by the canary
        criteria alone. A tripped activation can never be promoted."""
        self._require_enabled()
        if rollout not in _ROLLOUT_ORDER:
            raise LearningError("unknown_rollout", rollout)
        doc = await self._store.get_one("learning_activations", {"activation_id": activation_id})
        if doc is None:
            raise LearningError("activation_missing", activation_id)
        activation = LearningActivation.model_validate(doc)
        if activation.monitor_status == "tripped":
            raise LearningError("monitor_tripped", activation_id)
        if _ROLLOUT_ORDER.index(rollout) < _ROLLOUT_ORDER.index(activation.rollout):
            raise LearningError("cannot_demote_rollout", f"{activation.rollout} -> {rollout}")
        if rollout == "full" and activation.monitor_status != "passing":
            raise LearningError("monitor_not_passing", activation_id)
        updated = activation.model_copy(update={
            "rollout": rollout,
            "activated_at": activation.activated_at or _now(),
        })
        if not await self._store.compare_and_set(
            "learning_activations", {"activation_id": activation_id}, {"rollout": activation.rollout}, updated,
        ):
            raise LearningError("activation_state_race", activation_id)
        return updated

    async def record_monitor_result(self, *, activation_id: str, passing: bool, reason: str = "") -> LearningActivation:
        """A monitor trip halts and reverts without human action (D16)."""
        self._require_enabled()
        doc = await self._store.get_one("learning_activations", {"activation_id": activation_id})
        if doc is None:
            raise LearningError("activation_missing", activation_id)
        activation = LearningActivation.model_validate(doc)
        if passing:
            updated = activation.model_copy(update={"monitor_status": "passing"})
            if not await self._store.compare_and_set(
                "learning_activations", {"activation_id": activation_id},
                {"monitor_status": activation.monitor_status}, updated,
            ):
                raise LearningError("activation_state_race", activation_id)
            return updated
        return await self._trip_and_restore(activation, reason=reason or "monitor_tripped", by="monitor")

    async def rollback(self, *, activation_id: str, principal: str, reason: str) -> LearningActivation:
        """Human-initiated equivalent of a monitor trip: same restoration
        path, gated to ``lead_reviewer``."""
        self._require_enabled()
        if reviewer_role(principal) != "lead_reviewer":
            raise LearningError("not_lead_reviewer", principal)
        doc = await self._store.get_one("learning_activations", {"activation_id": activation_id})
        if doc is None:
            raise LearningError("activation_missing", activation_id)
        activation = LearningActivation.model_validate(doc)
        return await self._trip_and_restore(activation, reason=reason, by=principal)

    async def _trip_and_restore(self, activation: LearningActivation, *, reason: str, by: str) -> LearningActivation:
        tripped = activation.model_copy(update={
            "monitor_status": "tripped", "rolled_back_at": _now(), "rollback_reason": f"{by}: {reason}",
        })
        if not await self._store.compare_and_set(
            "learning_activations", {"activation_id": activation.activation_id},
            {"monitor_status": activation.monitor_status}, tripped,
        ):
            raise LearningError("activation_state_race", activation.activation_id)
        proposal_doc = await self._store.get_one("learning_proposals", {"proposal_id": activation.proposal_id})
        target = (proposal_doc or {}).get("target", "")
        prior = await self._find_prior_good_activation(target, exclude=activation.activation_id)
        if prior is not None:
            restored = prior.model_copy(update={"rollout": "full", "monitor_status": "passing"})
            await self._store.compare_and_set(
                "learning_activations", {"activation_id": prior.activation_id},
                {"activation_id": prior.activation_id}, restored,
            )
        return tripped

    async def _find_prior_good_activation(self, target: str, *, exclude: str) -> LearningActivation | None:
        candidates: list[LearningActivation] = []
        for doc in await self._store.find_many("learning_activations", {}):
            if doc.get("activation_id") == exclude or doc.get("rolled_back_at"):
                continue
            proposal_doc = await self._store.get_one("learning_proposals", {"proposal_id": doc.get("proposal_id")})
            if proposal_doc and proposal_doc.get("target") == target:
                candidates.append(LearningActivation.model_validate(doc))
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.approved_at)


# --- Phase 12 item 4: the docs #73 learning candidate pipeline ------------
#
# Feeds LearningService above (LearningProposal/LearningEvaluation/
# LearningActivation) -- not a replacement for it, see records.LearningCase's
# own docstring. This is the missing front half docs #73 describes:
# candidate -> abstract -> sanitize -> PHI/PII scan -> study reconstruction
# check -> policy validation -> (unsafe -> DELETE) -> safe learning store.
# Section 74's "no autonomous self-modification" is LearningService's own
# job above (propose -> record_evaluation -> approve -> promote_rollout, all
# human-gated, never auto-deploying); this pipeline only ever produces a
# sanitized LearningCase a human can later choose to turn into a
# LearningProposal by hand -- nothing here calls LearningService.propose
# automatically.
#
# Reuses this codebase's existing PHI/PII primitives rather than rebuilding
# any of them: `phi_core.security.scrub_persisted_text` (the same scrubber
# `trace_sanitizer.sanitize_status_text` already reuses) for sanitize, and
# `final_assurance._scan_text_surface` (itself a thin wrapper around
# `publish_guard._scan_text`/`publish_guard.scan_names`) for the PHI/PII
# scan stage.

LEARNING_CANDIDATES_COLLECTION = "learning_case_candidates"
LEARNING_CASES_COLLECTION = "learning_cases"

# No numeric value is given anywhere in the spec text for how long an
# abstract may run; 1000 chars is a chosen default (same disclosed-default
# convention Wave R-a used for MAX_UNCERTAIN_HEADERS_PER_RUN/
# MAX_SANDBOX_OUTPUT_BYTES) -- long enough for a real category-level
# summary, short enough that a caller cannot smuggle a near-verbatim
# document through as one long "abstract".
MAX_ABSTRACT_CHARS = 1000

_VALID_LEARNING_CASE_SOURCES = frozenset(get_args(LearningCaseSource))

_LONG_DIGIT_RUN = re.compile(r"\d{6,}")
_LONG_QUOTED_EXCERPT = re.compile(r"\"[^\"]{40,}\"|'[^']{40,}'")


class LearningCaseError(RuntimeError):
    """Raised with a fixed, testable ``reason`` on any refusal, matching
    ``LearningError``'s convention. ``case`` (when set) is the rejected,
    already-deleted-from-staging candidate, so a caller can inspect which
    pipeline stage flags are ``False`` without a second store round trip."""

    def __init__(self, reason: str, detail: str = "", case: LearningCase | None = None) -> None:
        self.reason = reason
        self.case = case
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _abstract(raw_content: str) -> str:
    """Whitespace-normalize and length-cap. Deterministic, not an LLM
    summarization step -- this pipeline runs with no model in the loop
    (section 74's own "no autonomous self-modification" posture extends
    naturally to "no autonomous candidate authoring" too, since an LLM
    call here would itself be exactly the kind of runtime behavior that
    directly touches the learning store this module gates)."""
    return " ".join((raw_content or "").split())[:MAX_ABSTRACT_CHARS]


def _sanitize(text: str) -> str:
    return scrub_persisted_text(text)


def _phi_pii_scan(text: str, jurisdiction: str) -> list[str]:
    if not text:
        return []
    findings: list[ReportingSafetyFinding] = []
    _scan_text_surface("learning_case_abstract", text, jurisdiction, findings)
    return [f"{f.pattern_id}:{f.hipaa_category}" for f in findings]


def _reconstruction_check(text: str) -> list[str]:
    """A heuristic, deterministic reconstructability check distinct from
    the PHI/PII scan above: a run of 6+ consecutive digits (an account
    number, an MRN, a numerically-written date) or a long quoted excerpt
    (a verbatim cell/column value) can reconstruct study content even
    when it does not match a named PHI pattern or a person's name."""
    reasons: list[str] = []
    if _LONG_DIGIT_RUN.search(text):
        reasons.append("long_digit_run")
    if _LONG_QUOTED_EXCERPT.search(text):
        reasons.append("long_quoted_excerpt")
    return reasons


def _policy_check(*, run_id: str, source: str, abstract: str, validated: bool) -> list[str]:
    reasons: list[str] = []
    if not validated:
        reasons.append("source_not_validated")
    if source not in _VALID_LEARNING_CASE_SOURCES:
        reasons.append("source_not_allowlisted")
    if not is_safe_scoped_id(run_id):
        reasons.append("run_id_not_scoped")
    if not abstract.strip():
        reasons.append("empty_abstract")
    return reasons


class LearningCaseService:
    """The docs #73 candidate pipeline. A separate service from
    ``LearningService`` even though both live in this module: they operate
    on different records (``LearningCase`` vs
    ``LearningProposal``/``LearningEvaluation``/``LearningActivation``) and
    different concerns (candidate creation and safety filtering, versus
    approval and rollout of an already-safe learning artifact). Unlike
    ``LearningService``, this pipeline is not gated by ``LEARNING_ENABLED``
    -- producing a sanitized audit-trail candidate is safe and useful even
    while the self-modification machinery downstream stays off; the flag
    only gates whether a human may ever turn a candidate into a live
    ``LearningProposal``.
    """

    def __init__(self, store: ControlStore) -> None:
        self._store = store

    async def create_candidate(
        self, *, run_id: str, source: LearningCaseSource, raw_content: str,
        validated: bool = True, jurisdiction: str = "us",
    ) -> LearningCase:
        """Run the full docs #73 pipeline over ``raw_content`` (never
        itself persisted -- only ever held in a local variable of this
        call). ``validated=True`` is the caller's own attestation that
        ``raw_content`` genuinely comes from a validated signal already
        looked up elsewhere (a real ``ReviewFinding``, ``HumanDecision``,
        ``ExecutionResult``, ``VerificationResult``, or rewind
        classification) -- section 73's "only from validated ..." opening
        clause; this function has no way to independently re-verify that
        claim, so it is a required, explicit keyword rather than a
        default-true toggle a caller could forget to consider.

        Raises :class:`LearningCaseError` with a fixed ``reason`` and the
        rejected ``case`` on any pipeline failure. The staged candidate
        row is always deleted before the error is raised (docs #73's
        "unsafe -> DELETE"); nothing unsafe is ever written to
        ``LEARNING_CASES_COLLECTION``, and the raw/sanitized abstract text
        itself is never written to the staging collection at all -- only
        the run-scoped bookkeeping fields (``case_id``/``run_id``/
        ``source``/timestamps) are staged before the abstract exists, so
        even a rejected candidate's staged row never carries any of the
        text this function scanned.
        """
        if source not in _VALID_LEARNING_CASE_SOURCES:
            raise LearningCaseError("invalid_source", str(source))

        case = LearningCase(run_id=run_id, source=source)
        await self._store.insert(LEARNING_CANDIDATES_COLLECTION, case)

        abstract = _sanitize(_abstract(raw_content))
        case = case.model_copy(update={"abstract": abstract, "sanitized": True})

        phi_reasons = _phi_pii_scan(abstract, jurisdiction)
        case = case.model_copy(update={"phi_pii_scan_passed": not phi_reasons})
        if phi_reasons:
            await self._reject(case)
            # The PHI/PII scan just caught an identifier scrub_persisted_text
            # left intact (the sanitize stage's regex set is not exhaustive).
            # Withhold that sanitize-missed raw text instead of letting it
            # travel back out on the rejection object's case.abstract.
            case = case.model_copy(update={"abstract": ""})
            raise LearningCaseError("phi_pii_scan_failed", ",".join(phi_reasons), case=case)

        reconstruction_reasons = _reconstruction_check(abstract)
        case = case.model_copy(update={"reconstruction_check_passed": not reconstruction_reasons})
        if reconstruction_reasons:
            await self._reject(case)
            raise LearningCaseError("reconstruction_check_failed", ",".join(reconstruction_reasons), case=case)

        policy_reasons = _policy_check(run_id=run_id, source=source, abstract=abstract, validated=validated)
        case = case.model_copy(update={"policy_validation_passed": not policy_reasons})
        if policy_reasons:
            await self._reject(case)
            raise LearningCaseError("policy_validation_failed", ",".join(policy_reasons), case=case)

        case = case.model_copy(update={"detail": "passed the full docs #73 candidate pipeline"})
        await self._store.delete_one(LEARNING_CANDIDATES_COLLECTION, {"case_id": case.case_id})
        await self._store.insert(LEARNING_CASES_COLLECTION, case)
        return case

    async def _reject(self, case: LearningCase) -> None:
        await self._store.delete_one(LEARNING_CANDIDATES_COLLECTION, {"case_id": case.case_id})

    async def get_case(self, case_id: str) -> LearningCase | None:
        doc = await self._store.get_one(LEARNING_CASES_COLLECTION, {"case_id": case_id})
        return LearningCase.model_validate(doc) if doc else None
