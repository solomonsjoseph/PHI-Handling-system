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


# ---- Phase 4 step 7: session-deletion coordination ------------------------


@pytest.mark.asyncio
async def test_stage_refuses_once_the_session_is_tombstoned() -> None:
    from phi_core.control.artifacts import tombstone_session

    service, store = _service()
    await tombstone_session(store, service.session_id)

    with pytest.raises(ArtifactError) as exc:
        await service.stage("dataset_export", "report.csv", "restricted_metadata", "short")
    assert exc.value.reason == "session_tombstoned"


@pytest.mark.asyncio
async def test_tombstone_session_is_idempotent() -> None:
    from phi_core.control.artifacts import is_session_tombstoned, tombstone_session

    store = MemoryControlStore()
    await tombstone_session(store, "a" * 32)
    await tombstone_session(store, "a" * 32)  # second call: no-op, not a duplicate-key error
    assert await is_session_tombstoned(store, "a" * 32) is True
    assert len(await store.find_many("session_tombstones", {"session_id": "a" * 32})) == 1


@pytest.mark.asyncio
async def test_is_session_tombstoned_false_for_an_untouched_session() -> None:
    from phi_core.control.artifacts import is_session_tombstoned

    store = MemoryControlStore()
    assert await is_session_tombstoned(store, "a" * 32) is False


@pytest.mark.asyncio
async def test_erase_session_records_deletes_only_the_named_sessions_records() -> None:
    store = MemoryControlStore()
    victim = ArtifactService(store, session_id="a" * 32, run_id="b" * 32)
    survivor = ArtifactService(store, session_id="c" * 32, run_id="d" * 32)
    v_id = await _stage_and_finalize(victim, b"erase me")
    s_id = await _stage_and_finalize(survivor, b"keep me")
    await victim.certify_publication(run_id=victim.run_id, artifact_ids=[v_id], gate_result_ids=[], fence=1)
    await survivor.certify_publication(run_id=survivor.run_id, artifact_ids=[s_id], gate_result_ids=[], fence=1)

    removed = await victim.erase_session_records("a" * 32)

    assert removed == 2  # one artifact record, one publication pointer
    assert await store.get_one("artifacts", {"artifact_id": v_id}) is None
    assert await store.get_one("artifacts", {"artifact_id": s_id}) is not None
    assert await store.find_many("publication_pointers", {"session_id": "a" * 32}) == []
    assert await store.find_many("publication_pointers", {"session_id": "c" * 32}) != []


def test_erase_session_artifacts_removes_on_disk_directories_across_every_root(tmp_path, monkeypatch) -> None:
    from phi_core.control import artifacts as artifacts_module

    session_id = "e" * 32
    monkeypatch.setattr(
        artifacts_module,
        "_ROOT_DIRS",
        {"staging": tmp_path / "staging", "published": tmp_path / "published"},
    )
    for root in artifacts_module._ROOT_DIRS.values():
        (root / session_id).mkdir(parents=True)
        (root / session_id / "leftover.bin").write_bytes(b"x")

    artifacts_module.erase_session_artifacts(session_id)

    for root in artifacts_module._ROOT_DIRS.values():
        assert not (root / session_id).exists()


def test_erase_session_artifacts_refuses_an_unsafe_session_id() -> None:
    from phi_core.control.artifacts import erase_session_artifacts

    with pytest.raises(ArtifactError) as exc:
        erase_session_artifacts("../../etc")
    assert exc.value.reason == "unsafe_session_id"
