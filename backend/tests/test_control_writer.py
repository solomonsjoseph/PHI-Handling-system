"""Focused contracts for the task-scoped artifact facade (control/writer.py)."""
from __future__ import annotations

import pytest
from phi_core.control.artifacts import ArtifactService
from phi_core.control.store import MemoryControlStore
from phi_core.control.writer import ArtifactWriter


def _writer() -> tuple[ArtifactWriter, ArtifactService, MemoryControlStore]:
    store = MemoryControlStore()
    service = ArtifactService(store, session_id="a" * 32, run_id="b" * 32)
    return ArtifactWriter(service, producer_task_id="task-1"), service, store


@pytest.mark.asyncio
async def test_stage_then_finalize_promotes_bytes_to_the_real_root() -> None:
    writer, service, _ = _writer()

    artifact_id, tmp_path = await writer.stage("dataset_export", "report.csv", "restricted_metadata", "export")
    tmp_path.write_bytes(b"hello world")
    record = await writer.finalize(artifact_id)

    assert record.state == "staged"
    final_path = tmp_path.parent.parent / service.session_id / service.run_id / artifact_id
    assert final_path.read_bytes() == b"hello world"


@pytest.mark.asyncio
async def test_write_context_manager_stages_writes_and_finalizes_on_success() -> None:
    writer, service, _ = _writer()

    async with writer.write("narrative_export", "note.txt", "restricted_metadata", "export") as (artifact_id, tmp_path):
        tmp_path.write_text("clean text", encoding="utf-8")

    final_path = tmp_path.parent.parent / service.session_id / service.run_id / artifact_id
    assert final_path.read_text(encoding="utf-8") == "clean text"


@pytest.mark.asyncio
async def test_interrupted_producer_leaves_zero_files_under_the_real_root() -> None:
    """D14's core atomicity guarantee: a producer that raises after `stage`
    but before `finalize` must never leave a promotable partial file under
    the real (non-`.tmp`) artifact root -- only the untouched staging
    directory (`stage`/`finalize` never having been reached a second time)
    remains."""
    writer, service, store = _writer()

    artifact_id_a, tmp_path_a = await writer.stage(
        "dataset_export", "report.csv", "restricted_metadata", "export",
    )
    tmp_path_a.write_bytes(b"partial garbage mid-write")
    real_root = tmp_path_a.parent.parent  # `<root>/.tmp/<id>`.parent.parent == `<root>`
    real_dir = real_root / service.session_id / service.run_id

    artifact_id_b = ""
    with pytest.raises(RuntimeError):
        async with writer.write("dataset_export", "report.csv", "restricted_metadata", "export") as (aid, tmp2):
            artifact_id_b = aid
            tmp2.write_bytes(b"also partial")
            raise RuntimeError("producer crashed before finalize")

    # Neither artifact was ever finalized: the first because this test
    # never calls `finalize`, the second because `write`'s context manager
    # only finalizes on a clean exit. Neither may exist at the real path.
    assert not (real_dir / artifact_id_a).exists()
    assert not (real_dir / artifact_id_b).exists()
    # The tmp bytes are still exactly where the producer left them --
    # nothing here silently deleted or promoted them.
    assert tmp_path_a.read_bytes() == b"partial garbage mid-write"

    stored_a = await store.get_one("artifacts", {"artifact_id": artifact_id_a})
    assert stored_a["state"] == "provisional"
    stored_b = await store.get_one("artifacts", {"artifact_id": artifact_id_b})
    assert stored_b["state"] == "provisional"


@pytest.mark.asyncio
async def test_producer_id_is_attributed_on_every_staged_artifact() -> None:
    writer, _, store = _writer()

    artifact_id, tmp_path = await writer.stage("dataset_export", "report.csv", "restricted_metadata", "export")
    tmp_path.write_bytes(b"x")

    stored = await store.get_one("artifacts", {"artifact_id": artifact_id})
    assert stored["producer_task_id"] == "task-1"
