"""The manifest-freeze gate (docs #49): the deterministic check that
decides whether a run's approved decisions may be frozen into a
:class:`~.records.VerifiedClassificationManifest` and, from there,
authorized for execution.

Freeze conditions (docs #49, verbatim): a manifest may only be frozen
``verified_for_execution`` when (1) Judge is complete, (2) Reviewer
Preview's verdict is ``PASS``, (3) Human Review is resolved (no decision
still routes to ``human_review``), and (4) the policy/decision-gate
sequence (``control/gates.py``'s D11 ``run_decision_gates``) reports
``ok``. Condition (2) is exempted from a literal ``PASS`` only when
condition (3) already holds: a Reviewer Preview verdict of
``CORRECTION_REQUIRED`` whose flagged items have since gone through
Human Review and come back resolved (``unresolved_items == 0``) was
settled by a human, not silently dropped. ``HUMAN_REVIEW_REQUIRED`` is
never exempted this way -- that status means at least one item is still
open, and a *different* item's resolution never retroactively clears it.

This module is the assembly/gate layer only: it does not itself persist
anything or decide run-lifecycle eligibility. Both of those stay
``SuperOrchestrator``'s exclusive authority (D9, ``control/
superorchestrator.py``) -- ``authorize_manifest_freeze`` already
implements the run-eligibility check (refuses a terminal/paused run) and
the ``MANIFEST_COLLECTION`` upsert; this module's ``ensure_frozen_manifest``
is the one caller a real execution path (``agents/orchestrator.py``'s
``execute_decisions``) needs, wrapping that pre-existing, previously
unwired (Wave R-b) authority with the four freeze conditions and the
idempotent "reuse a current manifest instead of minting a second one for
the same decision set" rule docs #49's own docstring on
``VerifiedClassificationManifest`` already commits to.
"""
from __future__ import annotations

from .artifacts import MANIFEST_COLLECTION
from .records import VerifiedClassificationManifest
from .store import ControlStore
from .superorchestrator import SuperOrchestrator


class ManifestFreezeRefused(RuntimeError):
    """Raised when the four docs #49 freeze conditions are not all
    satisfied. ``reasons`` names every condition that failed, not just
    the first -- a caller escalating to human review should be able to
    report the complete picture in one shot."""

    def __init__(self, reasons: list[str]) -> None:
        self.reasons = reasons
        super().__init__(f"manifest freeze refused: {', '.join(reasons) or 'unknown reason'}")


class ManifestInvalidated(RuntimeError):
    """Raised when the current manifest for this artifact_id has already
    been flipped to ``invalidated`` (R-b's lineage invalidation,
    ``ArtifactService.invalidate_descendants``). Execution from a stale
    manifest is refused outright -- never silently re-authorized, and
    never re-frozen under the same ``artifact_id`` (docs #30: an
    upstream change invalidates the existing manifest in place; it does
    not get reused once flipped)."""

    def __init__(self, manifest_id: str) -> None:
        self.manifest_id = manifest_id
        super().__init__(f"manifest {manifest_id!r} has been invalidated; refusing execution")


def manifest_artifact_id(run_id: str) -> str:
    """The synthetic, stable key ``VerifiedClassificationManifest``
    documents in :data:`~.artifacts.MANIFEST_COLLECTION` are keyed by for
    a run's decision set. Every existing manifest reader
    (``ArtifactService.open_for_download``, ``SuperOrchestrator
    .require_artifacts_current``) already queries that collection by
    ``artifact_id``; the pipeline's decision batch has no corresponding
    real, independently-versioned :class:`~.records.ArtifactRecord` of
    its own (dataset input files are plain uploaded-file dicts, not
    artifact-lineage records), so this is a deterministic, run-scoped
    substitute rather than a second, parallel keying scheme."""
    return f"decisions:{run_id}"


def evaluate_freeze_conditions(
    *,
    judge_complete: bool,
    reviewer_preview_status: str | None,
    unresolved_items: int,
    policy_gate_ok: bool,
) -> list[str]:
    """Return the (possibly empty) list of reasons the four docs #49
    freeze conditions are not satisfied. See the module docstring for the
    Reviewer-Preview/Human-Review exemption rule."""
    reasons: list[str] = []
    if not judge_complete:
        reasons.append("judge_incomplete")
    if reviewer_preview_status == "HUMAN_REVIEW_REQUIRED":
        reasons.append("reviewer_preview_human_review_required")
    elif reviewer_preview_status not in ("PASS", None) and unresolved_items:
        reasons.append("reviewer_preview_not_pass")
    if unresolved_items:
        reasons.append("human_review_unresolved")
    if not policy_gate_ok:
        reasons.append("policy_gate_failed")
    return reasons


async def get_current_manifest(
    store: ControlStore, *, artifact_id: str,
) -> VerifiedClassificationManifest | None:
    """The read-side of the freeze/invalidate cycle: the manifest
    currently on file for ``artifact_id``, or ``None`` if this artifact
    has never had one frozen. A manifest whose ``status`` has since
    flipped to ``invalidated`` is still returned -- the caller (``ensure_
    frozen_manifest`` below) decides what that means for its own purpose,
    matching ``ArtifactService.open_for_download``'s own convention of
    distinguishing "never existed" from "existed and was invalidated"."""
    doc = await store.get_one(MANIFEST_COLLECTION, {"artifact_id": artifact_id})
    if doc is None:
        return None
    return VerifiedClassificationManifest.model_validate(
        {k: v for k, v in doc.items() if k != "artifact_id"}
    )


async def ensure_frozen_manifest(
    *,
    store: ControlStore,
    orchestrator: SuperOrchestrator,
    run_id: str,
    artifact_id: str,
    source_artifact_versions: dict[str, int],
    decision_refs: list[str],
    evidence_refs: list[str],
    preview_review_id: str,
    human_review_refs: list[str],
    judge_complete: bool,
    reviewer_preview_status: str | None,
    unresolved_items: int,
    policy_gate_ok: bool,
) -> VerifiedClassificationManifest:
    """The one call a real execution path needs: return a current,
    ``verified_for_execution`` manifest for ``artifact_id``, minting and
    authorizing a fresh one only when none exists yet.

    Idempotent by construction: a retried call against an ``artifact_id``
    that already has a ``verified_for_execution`` manifest returns that
    *same* manifest (same ``manifest_id``) unchanged -- never a second
    manifest for the same decision set, exactly as ``VerifiedClassification
    Manifest``'s own docstring commits to. A manifest that has since been
    flipped to ``invalidated`` (R-b's lineage invalidation) raises
    :class:`ManifestInvalidated` rather than silently minting a
    replacement under the same key: a materially different decision set
    belongs under a materially different ``artifact_id``, which is the
    caller's decision to make (e.g. a fresh Judge/Reviewer/Human-Review
    cycle producing a new decision batch), not this function's.

    Raises :class:`ManifestFreezeRefused` when no current manifest exists
    and the four docs #49 freeze conditions are not all satisfied.
    """
    existing = await get_current_manifest(store, artifact_id=artifact_id)
    if existing is not None:
        if existing.status == "invalidated":
            raise ManifestInvalidated(existing.manifest_id)
        return existing
    reasons = evaluate_freeze_conditions(
        judge_complete=judge_complete,
        reviewer_preview_status=reviewer_preview_status,
        unresolved_items=unresolved_items,
        policy_gate_ok=policy_gate_ok,
    )
    if reasons:
        raise ManifestFreezeRefused(reasons)
    manifest = VerifiedClassificationManifest(
        run_id=run_id,
        source_artifact_versions=source_artifact_versions,
        decision_refs=decision_refs,
        evidence_refs=evidence_refs,
        preview_review_id=preview_review_id,
        human_review_refs=human_review_refs,
        unresolved_items=unresolved_items,
        status="verified_for_execution",
    )
    return await orchestrator.authorize_manifest_freeze(
        run_id=run_id, artifact_id=artifact_id, manifest=manifest,
    )
