"""Focused D14 contracts for the artifact registry (control/artifacts.py)."""
from __future__ import annotations

import pytest
from phi_core.control.artifacts import ArtifactError, ArtifactService
from phi_core.control.store import MemoryControlStore
from phi_core.paths import UnsafePath


def _service() -> tuple[ArtifactService, MemoryControlStore]:
    store = MemoryControlStore()
    service = ArtifactService(store, session_id="a" * 32, run_id="b" * 32)
    return service, store


@pytest.mark.asyncio
async def test_stage_then_finalize_atomically_promotes_provisional_to_staged() -> None:
    service, store = _service()
    artifact_id, tmp_path = await service.stage("dataset_export", "report.csv", "restricted_metadata", "short")
    assert tmp_path.parent.name == ".tmp"
    tmp_path.write_bytes(b"hello world")

    record = await service.finalize(artifact_id)

    assert record.state == "staged"
    assert record.size_bytes == len(b"hello world")
    assert not tmp_path.exists()  # renamed away, not copied
    final_path = tmp_path.parent.parent / service.session_id / service.run_id / artifact_id
    assert final_path.read_bytes() == b"hello world"
    stored = await store.get_one("artifacts", {"artifact_id": artifact_id})
    assert stored["state"] == "staged"
    assert stored["sha256"] == record.sha256


@pytest.mark.asyncio
async def test_stage_never_stores_the_original_filename() -> None:
    service, _ = _service()
    artifact_id, _ = await service.stage(
        "dataset_export", "smith_john_2019-04-02.csv", "restricted_metadata", "short"
    )

    doc = await service._store.get_one("artifacts", {"artifact_id": artifact_id})
    assert "filename" not in doc
    assert "original_name" not in doc
    assert "smith" not in str(doc).lower()


@pytest.mark.asyncio
async def test_stage_rejects_a_traversal_filename() -> None:
    service, _ = _service()
    with pytest.raises(UnsafePath):
        await service.stage("dataset_export", "../../etc/passwd", "restricted_metadata", "short")


@pytest.mark.asyncio
async def test_finalize_leaves_no_promotable_partial_file_when_the_producer_never_finalizes() -> None:
    """A crash between `stage` and `finalize` (the producer wrote partial
    bytes to the tmp path and never called finalize) must leave nothing
    promotable: the record stays provisional and no file exists at the
    final run-scoped path."""
    service, store = _service()
    artifact_id, tmp_path = await service.stage("dataset_export", "report.csv", "restricted_metadata", "short")
    tmp_path.write_bytes(b"only half the b")  # simulated interrupted write; finalize is never called

    stored = await store.get_one("artifacts", {"artifact_id": artifact_id})
    assert stored["state"] == "provisional"
    final_dir = tmp_path.parent.parent / service.session_id / service.run_id
    assert not (final_dir / artifact_id).exists()


@pytest.mark.asyncio
async def test_finalize_leaves_no_promotable_partial_file_on_a_mid_hash_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    """A read failure while hashing the staged bytes (disk fault, permission
    error) must raise before the rename and before the record is touched."""
    service, store = _service()
    artifact_id, tmp_path = await service.stage("dataset_export", "report.csv", "restricted_metadata", "short")
    tmp_path.write_bytes(b"some bytes")

    import phi_core.control.artifacts as artifacts_module

    def _boom(path: object) -> tuple[str, int]:
        raise OSError("simulated disk fault reading staged bytes")

    monkeypatch.setattr(artifacts_module, "_hash_file", _boom)

    with pytest.raises(OSError):
        await service.finalize(artifact_id)

    stored = await store.get_one("artifacts", {"artifact_id": artifact_id})
    assert stored["state"] == "provisional"
    assert tmp_path.exists()  # never renamed away
    final_dir = tmp_path.parent.parent / service.session_id / service.run_id
    assert not (final_dir / artifact_id).exists()


@pytest.mark.asyncio
async def test_finalize_refuses_when_the_tmp_file_was_never_written() -> None:
    service, _ = _service()
    artifact_id, _ = await service.stage("dataset_export", "report.csv", "restricted_metadata", "short")

    with pytest.raises(ArtifactError) as exc:
        await service.finalize(artifact_id)
    assert exc.value.reason == "artifact_write_incomplete"


@pytest.mark.asyncio
async def test_finalize_refuses_a_second_call_on_an_already_staged_artifact() -> None:
    service, _ = _service()
    artifact_id, tmp_path = await service.stage("dataset_export", "report.csv", "restricted_metadata", "short")
    tmp_path.write_bytes(b"bytes")
    await service.finalize(artifact_id)

    with pytest.raises(ArtifactError) as exc:
        await service.finalize(artifact_id)
    assert exc.value.reason == "artifact_not_provisional"


async def _stage_and_finalize(service: ArtifactService, content: bytes = b"published bytes") -> str:
    artifact_id, tmp_path = await service.stage("dataset_export", "report.csv", "restricted_metadata", "short")
    tmp_path.write_bytes(content)
    await service.finalize(artifact_id)
    return artifact_id


@pytest.mark.asyncio
async def test_certify_publication_promotes_artifacts_and_open_for_download_serves_them() -> None:
    service, _ = _service()
    artifact_id = await _stage_and_finalize(service)

    pointer = await service.certify_publication(run_id=service.run_id, artifact_ids=[artifact_id], gate_result_ids=[], fence=1)

    assert pointer.generation == 1
    path = await service.open_for_download(service.session_id, artifact_id)
    assert path.read_bytes() == b"published bytes"


@pytest.mark.asyncio
async def test_open_for_download_refuses_when_not_promoted() -> None:
    service, _ = _service()
    artifact_id = await _stage_and_finalize(service)  # staged, never certified

    with pytest.raises(ArtifactError) as exc:
        await service.open_for_download(service.session_id, artifact_id)
    assert exc.value.reason == "artifact_not_promoted"


@pytest.mark.asyncio
async def test_open_for_download_refuses_on_generation_mismatch_after_a_newer_publication() -> None:
    service, store = _service()
    first_id = await _stage_and_finalize(service, b"generation one")
    await service.certify_publication(run_id=service.run_id, artifact_ids=[first_id], gate_result_ids=[], fence=1)

    second_id = await _stage_and_finalize(service, b"generation two")
    await service.certify_publication(run_id=service.run_id, artifact_ids=[second_id], gate_result_ids=[], fence=2)

    # first_id is still `promoted` from generation 1, but generation 2 is now current.
    with pytest.raises(ArtifactError) as exc:
        await service.open_for_download(service.session_id, first_id)
    assert exc.value.reason == "generation_mismatch"

    path = await service.open_for_download(service.session_id, second_id)
    assert path.read_bytes() == b"generation two"


@pytest.mark.asyncio
async def test_open_for_download_refuses_on_a_hash_mismatch() -> None:
    service, _ = _service()
    artifact_id = await _stage_and_finalize(service)
    await service.certify_publication(run_id=service.run_id, artifact_ids=[artifact_id], gate_result_ids=[], fence=1)
    path = await service.open_for_download(service.session_id, artifact_id)

    # Simulate on-disk tampering after promotion.
    path.write_bytes(b"tampered bytes")

    with pytest.raises(ArtifactError) as exc:
        await service.open_for_download(service.session_id, artifact_id)
    assert exc.value.reason == "artifact_hash_mismatch"


@pytest.mark.asyncio
async def test_certify_publication_refuses_a_stale_or_equal_fence() -> None:
    service, _ = _service()
    artifact_id = await _stage_and_finalize(service)
    await service.certify_publication(run_id=service.run_id, artifact_ids=[artifact_id], gate_result_ids=[], fence=5)

    other_id = await _stage_and_finalize(service, b"other")
    with pytest.raises(ArtifactError) as exc:
        await service.certify_publication(run_id=service.run_id, artifact_ids=[other_id], gate_result_ids=[], fence=5)
    assert exc.value.reason == "stale_fence"
    with pytest.raises(ArtifactError) as exc:
        await service.certify_publication(run_id=service.run_id, artifact_ids=[other_id], gate_result_ids=[], fence=1)
    assert exc.value.reason == "stale_fence"


@pytest.mark.asyncio
async def test_certify_publication_increments_generation_monotonically() -> None:
    service, _ = _service()
    first_id = await _stage_and_finalize(service, b"one")
    second_id = await _stage_and_finalize(service, b"two")

    first_pointer = await service.certify_publication(run_id=service.run_id, artifact_ids=[first_id], gate_result_ids=[], fence=1)
    second_pointer = await service.certify_publication(run_id=service.run_id, artifact_ids=[second_id], gate_result_ids=[], fence=2)

    assert first_pointer.generation == 1
    assert second_pointer.generation == 2


@pytest.mark.asyncio
async def test_open_for_download_refuses_for_an_unknown_artifact() -> None:
    service, _ = _service()
    with pytest.raises(ArtifactError) as exc:
        await service.open_for_download(service.session_id, "no-such-artifact")
    assert exc.value.reason == "artifact_missing"
