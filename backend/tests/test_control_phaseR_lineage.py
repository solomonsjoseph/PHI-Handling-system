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
from phi_core.control.artifacts import ArtifactError, ArtifactService
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
