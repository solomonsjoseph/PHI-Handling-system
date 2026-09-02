"""Phase 12 item 6: CleanupManager (control/cleanup_manager.py).

Proves every CleanupManifest field gets genuinely populated (not left at
its default the way destroy_sandbox alone leaves everything but
sandbox_destroyed), and that a failed destruction step produces a manifest
Manager.confirm_cleanup then refuses -- composing this module
with the pre-existing, already-tested confirm_cleanup invariant
(test_control_manager_lifecycle.py::
test_confirm_cleanup_refuses_an_unverified_manifest) end to end rather than
re-testing that invariant in isolation.
"""
from __future__ import annotations

import pytest
from phi_core.control.cleanup_manager import (
    CATEGORY_CREDENTIALS,
    CATEGORY_OPAQUE_MAP,
    CATEGORY_REVERSAL_KEY,
    CATEGORY_SANDBOX,
    CATEGORY_STAGED_ARTIFACTS,
    CLEANUP_MANIFEST_COLLECTION,
    CREDENTIAL_REVOCATIONS_COLLECTION,
    CleanupInputs,
    CleanupManager,
)
from phi_core.control.manager import Manager
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.records import SandboxRecord
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskService
from phi_core.control.workflow import WorkflowError

SESSION_ID = "a" * 32

_COMPLETE_OUTCOMES = (
    "ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok", "ok", "ok", "complete",
)


def _rig() -> tuple[CleanupManager, Manager, MemoryControlStore]:
    store = MemoryControlStore()
    tasks = TaskService(store, CapabilityPolicy(None))
    orch = Manager(store, tasks)
    return CleanupManager(store, orch), orch, store


async def _completed_run(orch: Manager, run_id: str):
    run = None
    for outcome in _COMPLETE_OUTCOMES:
        run = await orch.advance(run_id=run_id, outcome=outcome)
    return run


def _sandbox(run_id: str, tmp_path) -> SandboxRecord:
    workspace = tmp_path / "sandbox" / run_id
    workspace.mkdir(parents=True)
    (workspace / "temp.py").write_text("x = 1", encoding="utf-8")
    return SandboxRecord(
        run_id=run_id, workspace_path=str(workspace),
        max_cpu_seconds=30, max_memory_bytes=1024 * 1024 * 1024, max_wall_seconds=120,
    )

# ---- every manifest field gets populated, not just sandbox_destroyed ------


@pytest.mark.asyncio
async def test_cleanup_populates_every_manifest_field_on_full_success(tmp_path):
    manager, orch, store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    run = await _completed_run(orch, run.run_id)

    inputs = CleanupInputs(
        run_id=run.run_id,
        session_id=SESSION_ID,
        sandbox=_sandbox(run.run_id, tmp_path),
        opaque_map_present=True,
        reversal_key_present=True,
    )

    manifest = await manager.cleanup(inputs)

    assert manifest.run_id == run.run_id
    assert manifest.cleanup_started_at
    assert manifest.cleanup_completed_at
    assert manifest.sandbox_destroyed is True
    assert manifest.keys_destroyed is True
    assert manifest.credentials_revoked is True
    assert manifest.storage_sanitization_status == "complete"
    assert manifest.verification_status == "verified"
    assert manifest.failure_details == ""
    # The gap destroy_sandbox alone leaves: every one of these was `False`/
    # empty/"pending" before CleanupManager existed.
    assert CATEGORY_SANDBOX in manifest.destroyed_categories
    assert CATEGORY_OPAQUE_MAP in manifest.destroyed_categories
    assert CATEGORY_REVERSAL_KEY in manifest.destroyed_categories
    assert CATEGORY_CREDENTIALS in manifest.destroyed_categories
    revocation = await store.get_one(CREDENTIAL_REVOCATIONS_COLLECTION, {"run_id": run.run_id})
    assert revocation is not None  # a real, auditable record, not a bare True
    assert CATEGORY_STAGED_ARTIFACTS in manifest.destroyed_categories
    assert manifest.retained_safe_categories  # audit trail / manifest / run manifest
    # Workspace bytes are actually gone, not merely flagged.
    assert not (tmp_path / "sandbox" / run.run_id).exists()

    stored = await store.get_one(CLEANUP_MANIFEST_COLLECTION, {"run_id": run.run_id})
    assert stored is not None
    assert stored["verification_status"] == "verified"

    orch2 = Manager(store, TaskService(store, CapabilityPolicy(None)))
    updated = await orch2.confirm_cleanup(run_id=run.run_id, manifest=manifest)
    assert updated.state == "session_destroyed"


@pytest.mark.asyncio
async def test_begin_cleanup_is_called_so_the_run_enters_destroying_state():
    manager, orch, store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    run = await _completed_run(orch, run.run_id)

    await manager.cleanup(CleanupInputs(run_id=run.run_id))

    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["state"] == "destroying"


# ---- a failed step blocks SESSION_DESTROYED, does not silently proceed ----


@pytest.mark.asyncio
async def test_a_failed_destruction_step_is_never_verified_and_blocks_session_destroyed():
    manager, orch, store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    run = await _completed_run(orch, run.run_id)

    async def _fails() -> bool:
        return False

    inputs = CleanupInputs(
        run_id=run.run_id,
        opaque_map_present=True,
        erase_opaque_map=_fails,
    )

    manifest = await manager.cleanup(inputs)

    assert manifest.verification_status == "failed"
    assert manifest.storage_sanitization_status == "failed"
    assert manifest.keys_destroyed is False
    assert "opaque_map_vault" in manifest.failure_details

    orch2 = Manager(store, TaskService(store, CapabilityPolicy(None)))
    with pytest.raises(WorkflowError):
        await orch2.confirm_cleanup(run_id=run.run_id, manifest=manifest)
    stored = await store.get_one("workflow_runs", {"run_id": run.run_id})
    assert stored["state"] == "destroying"  # never advanced to session_destroyed


@pytest.mark.asyncio
async def test_a_raising_erasure_step_is_caught_and_recorded_not_propagated():
    manager, orch, _store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    run = await _completed_run(orch, run.run_id)

    async def _boom() -> bool:
        raise RuntimeError("simulated erasure backend outage")

    manifest = await manager.cleanup(CleanupInputs(
        run_id=run.run_id, reversal_key_present=True, erase_reversal_key=_boom,
    ))

    assert manifest.verification_status == "failed"
    assert "simulated erasure backend outage" in manifest.failure_details


@pytest.mark.asyncio
async def test_sandbox_destroy_failure_is_recorded_and_blocks_verification(tmp_path, monkeypatch):
    manager, orch, _store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    run = await _completed_run(orch, run.run_id)
    sandbox = _sandbox(run.run_id, tmp_path)

    import shutil

    def _boom(path):
        raise OSError("simulated permission denied")

    monkeypatch.setattr(shutil, "rmtree", _boom)

    manifest = await manager.cleanup(CleanupInputs(run_id=run.run_id, sandbox=sandbox))

    assert manifest.sandbox_destroyed is False
    assert manifest.verification_status == "failed"
    assert "sandbox_workspace" in manifest.failure_details


# ---- retained categories are honest about what is actually kept ----------


@pytest.mark.asyncio
async def test_export_within_retention_is_recorded_as_retained_not_destroyed():
    manager, orch, _store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    run = await _completed_run(orch, run.run_id)

    manifest = await manager.cleanup(CleanupInputs(run_id=run.run_id, export_within_retention=True))

    assert "published_clean_export_within_retention" in manifest.retained_safe_categories
    assert manifest.verification_status == "verified"


# ---- idempotency / retry: a second cleanup pass records a second row -----


@pytest.mark.asyncio
async def test_latest_manifest_returns_none_before_any_cleanup_ran():
    manager, orch, _store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    run = await _completed_run(orch, run.run_id)

    assert await manager.latest_manifest(run.run_id) is None


@pytest.mark.asyncio
async def test_latest_manifest_picks_the_most_recent_of_multiple_attempts():
    manager, orch, _store = _rig()
    run = await orch.start_run(session_id=SESSION_ID, principal="operator-1")
    run = await _completed_run(orch, run.run_id)

    first = await manager.cleanup(CleanupInputs(run_id=run.run_id))
    second = await manager.cleanup(CleanupInputs(run_id=run.run_id))

    latest = await manager.latest_manifest(run.run_id)
    assert latest is not None
    assert latest.cleanup_completed_at == second.cleanup_completed_at
    assert latest.cleanup_completed_at >= first.cleanup_completed_at
