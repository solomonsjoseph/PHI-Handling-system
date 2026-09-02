"""Phase 9: the manifest-freeze gate (``control/manifest.py``, docs #49).

Covers ``evaluate_freeze_conditions`` (the four docs #49 conditions and
the Reviewer-Preview/Human-Review exemption rule) and
``ensure_frozen_manifest``'s three real outcomes: freeze fresh, reuse a
current manifest unchanged (idempotent), and refuse a manifest that has
been invalidated.
"""
from __future__ import annotations

import pytest
from phi_core.control.manager import Manager
from phi_core.control.manifest import (
    ManifestFreezeRefused,
    ManifestInvalidated,
    ensure_frozen_manifest,
    evaluate_freeze_conditions,
    get_current_manifest,
    manifest_artifact_id,
)
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskService
from phi_core.control.testing import start_test_run


def _orch(store: MemoryControlStore) -> Manager:
    return Manager(store, TaskService(store, CapabilityPolicy(None)))


# ---- evaluate_freeze_conditions --------------------------------------------


def test_evaluate_freeze_conditions_all_clear_when_every_condition_holds() -> None:
    assert evaluate_freeze_conditions(
        judge_complete=True, reviewer_preview_status="PASS",
        unresolved_items=0, policy_gate_ok=True,
    ) == []


def test_evaluate_freeze_conditions_reports_every_failing_reason_at_once() -> None:
    reasons = evaluate_freeze_conditions(
        judge_complete=False, reviewer_preview_status="HUMAN_REVIEW_REQUIRED",
        unresolved_items=3, policy_gate_ok=False,
    )
    assert set(reasons) == {
        "judge_incomplete", "reviewer_preview_human_review_required",
        "human_review_unresolved", "policy_gate_failed",
    }


def test_reviewer_preview_correction_required_exempted_once_human_review_resolved() -> None:
    """A Reviewer Preview verdict of CORRECTION_REQUIRED, left over from an
    earlier iteration, does not block the freeze once every item it would
    have blocked has since gone through Human Review (unresolved_items ==
    0) -- settled by a human, not silently dropped."""
    assert evaluate_freeze_conditions(
        judge_complete=True, reviewer_preview_status="CORRECTION_REQUIRED",
        unresolved_items=0, policy_gate_ok=True,
    ) == []


def test_reviewer_preview_human_review_required_never_exempted() -> None:
    """Unlike CORRECTION_REQUIRED, HUMAN_REVIEW_REQUIRED always blocks the
    freeze even with unresolved_items == 0: it means a specific item is
    still open, and some other item's resolution never clears it."""
    reasons = evaluate_freeze_conditions(
        judge_complete=True, reviewer_preview_status="HUMAN_REVIEW_REQUIRED",
        unresolved_items=0, policy_gate_ok=True,
    )
    assert reasons == ["reviewer_preview_human_review_required"]


# ---- ensure_frozen_manifest -------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_frozen_manifest_mints_and_authorizes_a_fresh_manifest() -> None:
    store = MemoryControlStore()
    run = await start_test_run(store, "s" * 32)
    artifact_id = manifest_artifact_id(run.run_id)

    manifest = await ensure_frozen_manifest(
        store=store, orchestrator=_orch(store), run_id=run.run_id, artifact_id=artifact_id,
        source_artifact_versions={"f1": 0}, decision_refs=["f1:ssn"], evidence_refs=[],
        preview_review_id="", human_review_refs=[],
        judge_complete=True, reviewer_preview_status="PASS", unresolved_items=0, policy_gate_ok=True,
    )

    assert manifest.status == "verified_for_execution"
    assert manifest.run_id == run.run_id
    stored = await get_current_manifest(store, artifact_id=artifact_id)
    assert stored is not None and stored.manifest_id == manifest.manifest_id


@pytest.mark.asyncio
async def test_ensure_frozen_manifest_reuses_current_manifest_idempotently() -> None:
    """A second call against the same artifact_id must never mint a
    second manifest_id for the same decision set -- it returns the
    exact same manifest untouched, even when the freeze conditions
    passed the second time would (irrelevantly) differ."""
    store = MemoryControlStore()
    run = await start_test_run(store, "s" * 32)
    artifact_id = manifest_artifact_id(run.run_id)

    first = await ensure_frozen_manifest(
        store=store, orchestrator=_orch(store), run_id=run.run_id, artifact_id=artifact_id,
        source_artifact_versions={}, decision_refs=[], evidence_refs=[],
        preview_review_id="", human_review_refs=[],
        judge_complete=True, reviewer_preview_status="PASS", unresolved_items=0, policy_gate_ok=True,
    )
    second = await ensure_frozen_manifest(
        store=store, orchestrator=_orch(store), run_id=run.run_id, artifact_id=artifact_id,
        source_artifact_versions={}, decision_refs=[], evidence_refs=[],
        preview_review_id="", human_review_refs=[],
        judge_complete=False, reviewer_preview_status="HUMAN_REVIEW_REQUIRED",
        unresolved_items=5, policy_gate_ok=False,
    )

    assert second.manifest_id == first.manifest_id


@pytest.mark.asyncio
async def test_ensure_frozen_manifest_refuses_when_conditions_unmet() -> None:
    store = MemoryControlStore()
    run = await start_test_run(store, "s" * 32)

    with pytest.raises(ManifestFreezeRefused) as excinfo:
        await ensure_frozen_manifest(
            store=store, orchestrator=_orch(store), run_id=run.run_id,
            artifact_id=manifest_artifact_id(run.run_id),
            source_artifact_versions={}, decision_refs=[], evidence_refs=[],
            preview_review_id="", human_review_refs=[],
            judge_complete=True, reviewer_preview_status="HUMAN_REVIEW_REQUIRED",
            unresolved_items=1, policy_gate_ok=True,
        )
    assert "human_review_unresolved" in excinfo.value.reasons


@pytest.mark.asyncio
async def test_ensure_frozen_manifest_refuses_execution_from_an_invalidated_manifest() -> None:
    """The invariant: once a manifest has been flipped to invalidated (R-b's
    lineage invalidation), a caller can never authorize execution from it,
    and this function never silently mints a replacement under the same
    artifact_id."""
    store = MemoryControlStore()
    run = await start_test_run(store, "s" * 32)
    artifact_id = manifest_artifact_id(run.run_id)

    frozen = await ensure_frozen_manifest(
        store=store, orchestrator=_orch(store), run_id=run.run_id, artifact_id=artifact_id,
        source_artifact_versions={}, decision_refs=[], evidence_refs=[],
        preview_review_id="", human_review_refs=[],
        judge_complete=True, reviewer_preview_status="PASS", unresolved_items=0, policy_gate_ok=True,
    )
    invalidated = frozen.model_copy(update={"status": "invalidated"})
    document = invalidated.model_dump(mode="python")
    document["artifact_id"] = artifact_id
    await store.replace_one("verified_classification_manifests", {"artifact_id": artifact_id}, document)

    with pytest.raises(ManifestInvalidated) as excinfo:
        await ensure_frozen_manifest(
            store=store, orchestrator=_orch(store), run_id=run.run_id, artifact_id=artifact_id,
            source_artifact_versions={}, decision_refs=[], evidence_refs=[],
            preview_review_id="", human_review_refs=[],
            judge_complete=True, reviewer_preview_status="PASS", unresolved_items=0, policy_gate_ok=True,
        )
    assert excinfo.value.manifest_id == frozen.manifest_id
