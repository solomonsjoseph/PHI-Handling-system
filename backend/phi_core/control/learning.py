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
from datetime import datetime, timezone
from typing import Any

from phi_core.security import reviewer_role

from .events import canonical_json
from .records import LearningActivation, LearningEvaluation, LearningProposal
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
