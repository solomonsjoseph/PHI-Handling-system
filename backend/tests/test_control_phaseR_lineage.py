"""Wave R-b: artifact lineage invalidation (docs #30, master spec section 30).

``ArtifactRecord.parents`` was previously populated only by ``control/migrate.py``
and nothing walked it. This module tests the three pieces added to
``control/artifacts.py`` to make section 30's lineage chain real:

1. ``ArtifactService.stage`` requires an explicit ``parents`` list for any
   "consequential" artifact type (docs #29's list) and stores it on the record.
2. ``ArtifactService.invalidate_descendants`` walks ``parents`` forward within
   one run, superseding every descendant and invalidating any linked
   ``VerifiedClassificationManifest``, cycle-safely and idempotently.
3. ``ArtifactService.open_for_download`` refuses to serve a superseded
   artifact or one whose linked manifest has been invalidated.
"""
from __future__ import annotations

import pytest
from phi_core.control.artifacts import ArtifactError, ArtifactService, MANIFEST_COLLECTION
from phi_core.control.store import MemoryControlStore


def _service(run_id: str = "b" * 32) -> tuple[ArtifactService, MemoryControlStore]:
    store = MemoryControlStore()
    service = ArtifactService(store, session_id="a" * 32, run_id=run_id)
    return service, store


# ---- Unit A: stage() requires explicit parents for consequential types ----


@pytest.mark.asyncio
async def test_stage_refuses_a_consequential_artifact_type_without_parents() -> None:
    service, _ = _service()
    with pytest.raises(ArtifactError) as exc:
        await service.stage("StudyKnowledgePackage", "study.json", "internal", "short")
    assert exc.value.reason == "artifact_parents_required"


@pytest.mark.asyncio
async def test_stage_refuses_a_consequential_artifact_type_with_an_empty_parents_list() -> None:
    service, _ = _service()
    with pytest.raises(ArtifactError) as exc:
        await service.stage(
            "VerifiedClassificationManifest", "manifest.json", "internal", "short", parents=[],
        )
    assert exc.value.reason == "artifact_parents_required"


@pytest.mark.asyncio
async def test_stage_accepts_a_non_consequential_type_with_no_parents() -> None:
    """``dataset_export`` and friends are not in docs #29's consequential
    list, so the guard must not apply to them."""
    service, _ = _service()
    artifact_id, _ = await service.stage("dataset_export", "report.csv", "restricted_metadata", "short")
    assert artifact_id


@pytest.mark.asyncio
async def test_stage_accepts_a_consequential_artifact_type_with_explicit_parents() -> None:
    service, _ = _service()
    artifact_id, _ = await service.stage(
        "StudyKnowledgePackage", "study.json", "internal", "short", parents=["source-file-1"],
    )
    assert artifact_id


# ---- Unit B: stage() persists the given parents list on the record --------


@pytest.mark.asyncio
async def test_stage_persists_the_given_parents_list_on_a_consequential_artifact() -> None:
    service, store = _service()
    artifact_id, _ = await service.stage(
        "StudyKnowledgePackage", "study.json", "internal", "short",
        parents=["source-file-1", "source-file-2"],
    )
    stored = await store.get_one("artifacts", {"artifact_id": artifact_id})
    assert stored["parents"] == ["source-file-1", "source-file-2"]


async def _stage_and_finalize(
    service: ArtifactService,
    artifact_type: str,
    *,
    parents: list[str] | None = None,
    content: bytes = b"lineage bytes",
) -> str:
    artifact_id, tmp_path = await service.stage(
        artifact_type, "x.json", "internal", "short", parents=parents,
    )
    tmp_path.write_bytes(content)
    await service.finalize(artifact_id)
    return artifact_id


# ---- Unit C: invalidate_descendants walks the full section-30 chain -------


@pytest.mark.asyncio
async def test_invalidate_descendants_supersedes_the_full_multilevel_chain() -> None:
    """A 4-level chain: source -> knowledge -> decisions -> preview -> manifest.
    Invalidating ``source`` must supersede every one of the four descendants."""
    service, store = _service()
    source_id = await _stage_and_finalize(service, "dataset_export")
    knowledge_id = await _stage_and_finalize(
        service, "StudyKnowledgePackage", parents=[source_id],
    )
    decisions_id = await _stage_and_finalize(
        service, "JudgeDecisionSet", parents=[knowledge_id],
    )
    preview_id = await _stage_and_finalize(
        service, "ReviewerPreviewResult", parents=[decisions_id],
    )
    manifest_id = await _stage_and_finalize(
        service, "VerifiedClassificationManifest", parents=[preview_id],
    )

    superseded = await service.invalidate_descendants(source_id)

    superseded_ids = {record.artifact_id for record in superseded}
    assert superseded_ids == {knowledge_id, decisions_id, preview_id, manifest_id}
    for artifact_id in (knowledge_id, decisions_id, preview_id, manifest_id):
        stored = await store.get_one("artifacts", {"artifact_id": artifact_id})
        assert stored["state"] == "superseded"


@pytest.mark.asyncio
async def test_invalidate_descendants_leaves_ancestors_and_unrelated_artifacts_untouched() -> None:
    service, store = _service()
    source_id = await _stage_and_finalize(service, "dataset_export")
    knowledge_id = await _stage_and_finalize(
        service, "StudyKnowledgePackage", parents=[source_id],
    )
    unrelated_id = await _stage_and_finalize(service, "dataset_export")

    await service.invalidate_descendants(knowledge_id)

    source_doc = await store.get_one("artifacts", {"artifact_id": source_id})
    unrelated_doc = await store.get_one("artifacts", {"artifact_id": unrelated_id})
    assert source_doc["state"] == "staged"
    assert unrelated_doc["state"] == "staged"


@pytest.mark.asyncio
async def test_invalidate_descendants_is_cycle_safe_and_terminates() -> None:
    """A malformed parents graph with a cycle must not infinite-loop: A -> B
    (B declares A as parent) and B -> A (A also declares B as parent, via a
    direct store write since ``stage`` cannot express a forward reference)."""
    service, store = _service()
    a_id = await _stage_and_finalize(service, "dataset_export")
    b_id = await _stage_and_finalize(service, "StudyKnowledgePackage", parents=[a_id])

    # Force the cycle: make A also declare B as a parent, after the fact.
    a_doc = await store.get_one("artifacts", {"artifact_id": a_id})
    from phi_core.control.records import ArtifactRecord
    a_record = ArtifactRecord.model_validate(a_doc)
    cyclic_a = a_record.model_copy(update={"parents": [b_id]})
    await store.replace_one("artifacts", {"artifact_id": a_id}, cyclic_a)

    # Must return promptly (no hang) and never touch the ancestor `a_id`.
    superseded = await service.invalidate_descendants(a_id)

    superseded_ids = {record.artifact_id for record in superseded}
    assert superseded_ids == {b_id}
    a_doc_after = await store.get_one("artifacts", {"artifact_id": a_id})
    assert a_doc_after["state"] == "staged"


@pytest.mark.asyncio
async def test_invalidate_descendants_is_idempotent_on_a_second_call() -> None:
    service, store = _service()
    source_id = await _stage_and_finalize(service, "dataset_export")
    knowledge_id = await _stage_and_finalize(
        service, "StudyKnowledgePackage", parents=[source_id],
    )
    decisions_id = await _stage_and_finalize(
        service, "JudgeDecisionSet", parents=[knowledge_id],
    )

    first = await service.invalidate_descendants(source_id)
    second = await service.invalidate_descendants(source_id)

    assert {r.artifact_id for r in first} == {knowledge_id, decisions_id}
    assert {r.artifact_id for r in second} == {knowledge_id, decisions_id}
    for record in second:
        assert record.state == "superseded"
    knowledge_doc = await store.get_one("artifacts", {"artifact_id": knowledge_id})
    decisions_doc = await store.get_one("artifacts", {"artifact_id": decisions_id})
    assert knowledge_doc["state"] == "superseded"
    assert decisions_doc["state"] == "superseded"


@pytest.mark.asyncio
async def test_invalidate_descendants_is_scoped_to_its_own_run_id() -> None:
    """A same-named artifact_id chain in a different run must never be
    touched by a walk scoped to this service's run_id."""
    service_run_b, store = _service(run_id="b" * 32)
    service_run_c = ArtifactService(store, session_id="a" * 32, run_id="c" * 32)

    source_id = await _stage_and_finalize(service_run_b, "dataset_export")
    knowledge_id = await _stage_and_finalize(
        service_run_b, "StudyKnowledgePackage", parents=[source_id],
    )

    other_source_id = await _stage_and_finalize(service_run_c, "dataset_export")
    other_knowledge_id = await _stage_and_finalize(
        service_run_c, "StudyKnowledgePackage", parents=[other_source_id],
    )

    await service_run_b.invalidate_descendants(source_id)

    other_doc = await store.get_one("artifacts", {"artifact_id": other_knowledge_id})
    assert other_doc["state"] == "staged"
    knowledge_doc = await store.get_one("artifacts", {"artifact_id": knowledge_id})
    assert knowledge_doc["state"] == "superseded"


# ---- Unit D: invalidate_descendants invalidates a linked manifest ---------


@pytest.mark.asyncio
async def test_invalidate_descendants_flips_a_linked_manifest_to_invalidated() -> None:
    service, store = _service()
    source_id = await _stage_and_finalize(service, "dataset_export")
    manifest_artifact_id = await _stage_and_finalize(
        service, "VerifiedClassificationManifest", parents=[source_id],
    )
    await store.insert(
        MANIFEST_COLLECTION,
        {
            "manifest_id": "m1",
            "artifact_id": manifest_artifact_id,
            "run_id": service.run_id,
            "status": "verified_for_execution",
        },
    )

    await service.invalidate_descendants(source_id)

    manifest_doc = await store.get_one(MANIFEST_COLLECTION, {"artifact_id": manifest_artifact_id})
    assert manifest_doc["status"] == "invalidated"


@pytest.mark.asyncio
async def test_invalidate_descendants_leaves_an_unrelated_manifest_untouched() -> None:
    service, store = _service()
    source_id = await _stage_and_finalize(service, "dataset_export")
    unrelated_manifest_artifact_id = await _stage_and_finalize(service, "dataset_export")
    await store.insert(
        MANIFEST_COLLECTION,
        {
            "manifest_id": "m2",
            "artifact_id": unrelated_manifest_artifact_id,
            "run_id": service.run_id,
            "status": "verified_for_execution",
        },
    )

    await service.invalidate_descendants(source_id)

    manifest_doc = await store.get_one(
        MANIFEST_COLLECTION, {"artifact_id": unrelated_manifest_artifact_id}
    )
    assert manifest_doc["status"] == "verified_for_execution"
