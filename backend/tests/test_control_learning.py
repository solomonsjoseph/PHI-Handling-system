"""D16 acceptance tests for controlled learning (control/learning.py)."""
from __future__ import annotations

import pytest
from phi_core.control.learning import LearningError, LearningService, learning_enabled, redact_digest
from phi_core.control.store import MemoryControlStore


def _service() -> tuple[LearningService, MemoryControlStore]:
    store = MemoryControlStore()
    return LearningService(store), store


# ---- disabled-by-default ----------------------------------------------------


def test_learning_enabled_is_false_by_default(monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    assert learning_enabled() is False


@pytest.mark.asyncio
async def test_every_method_refuses_outright_when_learning_is_disabled(monkeypatch):
    monkeypatch.delenv("LEARNING_ENABLED", raising=False)
    service, _ = _service()
    with pytest.raises(LearningError) as exc:
        await service.propose(
            kind="threshold_update", target="cap_age_90", baseline_version="v1", proposed_version="v2",
            redacted_input={"note": "irrelevant"},
        )
    assert exc.value.reason == "learning_disabled"


def test_redact_digest_never_stores_the_raw_payload():
    digest = redact_digest({"prompt_text": "contains something sensitive"})
    assert "sensitive" not in digest
    assert len(digest) == 64  # sha256 hex


# ---- propose -> evaluate -> approve -----------------------------------------


@pytest.mark.asyncio
async def test_approve_requires_both_an_offline_and_an_adversarial_passing_evaluation(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    monkeypatch.setenv("REVIEWER_PRINCIPALS", "lead-1:lead_reviewer")
    service, store = _service()
    proposal = await service.propose(
        kind="threshold_update", target="cap_age_90", baseline_version="v1", proposed_version="v2",
        redacted_input={"digest_of": "evidence"},
    )

    with pytest.raises(LearningError) as exc:
        await service.approve(proposal_id=proposal.proposal_id, principal="lead-1")
    assert exc.value.reason == "proposal_not_evaluated"

    await service.record_evaluation(
        proposal_id=proposal.proposal_id, fixture_set="fixtures/learning/threshold_v1.json",
        metrics={"leak_rate": 0.0}, passed=True, adversarial=False,
    )
    with pytest.raises(LearningError) as exc:
        await service.approve(proposal_id=proposal.proposal_id, principal="lead-1")
    assert exc.value.reason == "missing_adversarial_evaluation"

    await service.record_evaluation(
        proposal_id=proposal.proposal_id, fixture_set="fixtures/learning/threshold_v1_adversarial.json",
        metrics={"leak_rate": 0.0}, passed=True, adversarial=True,
    )
    activation = await service.approve(proposal_id=proposal.proposal_id, principal="lead-1")

    assert activation.rollout == "shadow"
    assert activation.approved_by == "lead-1"
    stored_proposal = await store.get_one("learning_proposals", {"proposal_id": proposal.proposal_id})
    assert stored_proposal["state"] == "approved"


@pytest.mark.asyncio
async def test_approve_refuses_a_non_lead_reviewer(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    monkeypatch.setenv("REVIEWER_PRINCIPALS", "reviewer-1:reviewer")
    service, _ = _service()
    proposal = await service.propose(
        kind="threshold_update", target="cap_age_90", baseline_version="v1", proposed_version="v2",
        redacted_input={"x": 1},
    )
    await service.record_evaluation(proposal_id=proposal.proposal_id, fixture_set="f", metrics={}, passed=True)
    await service.record_evaluation(
        proposal_id=proposal.proposal_id, fixture_set="f", metrics={}, passed=True, adversarial=True,
    )

    with pytest.raises(LearningError) as exc:
        await service.approve(proposal_id=proposal.proposal_id, principal="reviewer-1")
    assert exc.value.reason == "not_lead_reviewer"


# ---- rollout and monitoring are separate gates ------------------------------


async def _approved_activation(service, *, target="cap_age_90"):
    proposal = await service.propose(
        kind="threshold_update", target=target, baseline_version="v1", proposed_version="v2",
        redacted_input={"x": 1},
    )
    await service.record_evaluation(proposal_id=proposal.proposal_id, fixture_set="f", metrics={}, passed=True)
    await service.record_evaluation(
        proposal_id=proposal.proposal_id, fixture_set="f", metrics={}, passed=True, adversarial=True,
    )
    return await service.approve(proposal_id=proposal.proposal_id, principal="lead-1")


@pytest.mark.asyncio
async def test_canary_meeting_its_own_criteria_still_needs_the_monitor_to_reach_full(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    monkeypatch.setenv("REVIEWER_PRINCIPALS", "lead-1:lead_reviewer")
    service, _ = _service()
    activation = await _approved_activation(service)

    canary = await service.promote_rollout(activation_id=activation.activation_id, rollout="canary")
    assert canary.rollout == "canary"

    # The canary itself has "passed" (an external canary-criteria check,
    # outside this service's scope) but the monitor has not reported
    # passing yet -- promotion to full must still refuse.
    with pytest.raises(LearningError) as exc:
        await service.promote_rollout(activation_id=activation.activation_id, rollout="full")
    assert exc.value.reason == "monitor_not_passing"

    await service.record_monitor_result(activation_id=activation.activation_id, passing=True)
    full = await service.promote_rollout(activation_id=activation.activation_id, rollout="full")
    assert full.rollout == "full"


@pytest.mark.asyncio
async def test_a_monitor_trip_halts_and_reverts_without_human_action(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    monkeypatch.setenv("REVIEWER_PRINCIPALS", "lead-1:lead_reviewer")
    service, store = _service()
    first = await _approved_activation(service, target="cap_age_90")
    await service.record_monitor_result(activation_id=first.activation_id, passing=True)
    await service.promote_rollout(activation_id=first.activation_id, rollout="canary")
    await service.promote_rollout(activation_id=first.activation_id, rollout="full")

    second = await _approved_activation(service, target="cap_age_90")
    await service.record_monitor_result(activation_id=second.activation_id, passing=True)
    await service.promote_rollout(activation_id=second.activation_id, rollout="canary")
    await service.promote_rollout(activation_id=second.activation_id, rollout="full")

    tripped = await service.record_monitor_result(
        activation_id=second.activation_id, passing=False, reason="leak rate spike",
    )

    assert tripped.monitor_status == "tripped"
    assert tripped.rolled_back_at
    assert "monitor: leak rate spike" in tripped.rollback_reason
    restored = await store.get_one("learning_activations", {"activation_id": first.activation_id})
    assert restored["rollout"] == "full"
    assert restored["monitor_status"] == "passing"


@pytest.mark.asyncio
async def test_explicit_rollback_by_a_lead_reviewer_restores_the_prior_version(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    monkeypatch.setenv("REVIEWER_PRINCIPALS", "lead-1:lead_reviewer")
    service, store = _service()
    first = await _approved_activation(service, target="cap_age_90")
    await service.record_monitor_result(activation_id=first.activation_id, passing=True)
    await service.promote_rollout(activation_id=first.activation_id, rollout="full")

    second = await _approved_activation(service, target="cap_age_90")
    await service.record_monitor_result(activation_id=second.activation_id, passing=True)
    await service.promote_rollout(activation_id=second.activation_id, rollout="full")

    rolled_back = await service.rollback(
        activation_id=second.activation_id, principal="lead-1", reason="regression found in review",
    )

    assert rolled_back.monitor_status == "tripped"
    restored = await store.get_one("learning_activations", {"activation_id": first.activation_id})
    assert restored["rollout"] == "full"


@pytest.mark.asyncio
async def test_promote_rollout_refuses_to_demote_or_promote_a_tripped_activation(monkeypatch):
    monkeypatch.setenv("LEARNING_ENABLED", "true")
    monkeypatch.setenv("REVIEWER_PRINCIPALS", "lead-1:lead_reviewer")
    service, _ = _service()
    activation = await _approved_activation(service)
    await service.promote_rollout(activation_id=activation.activation_id, rollout="canary")

    with pytest.raises(LearningError) as exc:
        await service.promote_rollout(activation_id=activation.activation_id, rollout="shadow")
    assert exc.value.reason == "cannot_demote_rollout"

    await service.record_monitor_result(activation_id=activation.activation_id, passing=False, reason="bad metric")
    with pytest.raises(LearningError) as exc:
        await service.promote_rollout(activation_id=activation.activation_id, rollout="full")
    assert exc.value.reason == "monitor_tripped"
