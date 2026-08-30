"""Phase 12 item 6/7: proves session_delete's CleanupManager wiring
produces a genuinely verified CleanupManifest end to end and advances the
durable WorkflowRun to session_destroyed (not just the isolated
CleanupManager unit coverage in test_control_cleanup_manager.py), and
that the export/download lifecycle survives a simulated backend restart.

Uses the real test Mongo instance (matching test_control_migrate.py's own
convention): session_delete/session_export/session_bundle construct a
real MongoControlStore(get_db())/ArtifactService internally, so a
MemoryControlStore fixture would not exercise the same call path.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.superorchestrator import SuperOrchestrator
from phi_core.control.tasks import TaskService
from phi_core.db import get_db

_COMPLETE_OUTCOMES = (
    "ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok", "ok", "ok", "complete",
)


@pytest.fixture(autouse=True)
def _fresh_motor_client_per_test():
    """Matches test_control_migrate.py's fixture: get_db is @lru_cache'd
    process-wide against whichever event loop was running at first
    construction, and asyncio_mode=strict gives each test its own loop."""
    get_db.cache_clear()
    yield
    get_db.cache_clear()


def _sid() -> str:
    return uuid.uuid4().hex


async def _completed_workflow_run(db, run_id: str, session_id: str) -> None:
    from phi_core.control.store import MongoControlStore
    control_store = MongoControlStore(db)
    orch = SuperOrchestrator(control_store, TaskService(control_store, CapabilityPolicy(None)))
    await orch.start_run(session_id=session_id, principal="operator-1", run_id=run_id)
    for outcome in _COMPLETE_OUTCOMES:
        await orch.advance(run_id=run_id, outcome=outcome)


@pytest.mark.asyncio
async def test_session_delete_produces_a_verified_manifest_and_reaches_session_destroyed():
    db = get_db()
    sid = _sid()
    run_id = _sid()

    await _completed_workflow_run(db, run_id, sid)
    await db.sessions.insert_one({
        "id": sid, "owner": "alice", "status": "complete",
        "guard_report": {"status": "clean"}, "export_paths": {},
        "_pipeline_run_id": run_id,
    })

    try:
        import server as srv

        resp = await srv.session_delete(sid, principal="alice")
        assert resp == {"deleted": True}

        manifest_doc = await db.cleanup_manifests.find_one({"run_id": run_id}, {"_id": 0})
        assert manifest_doc is not None
        assert manifest_doc["verification_status"] == "verified"
        assert manifest_doc["sandbox_destroyed"] is True  # nothing to destroy is not a failure
        assert manifest_doc["credentials_revoked"] is True
        assert manifest_doc["keys_destroyed"] is True

        revocation = await db.run_credential_revocations.find_one({"run_id": run_id}, {"_id": 0})
        assert revocation is not None

        run_doc = await db.workflow_runs.find_one({"run_id": run_id}, {"_id": 0})
        assert run_doc is not None
        assert run_doc["state"] == "session_destroyed"

        # The session document itself is genuinely gone (session_delete's
        # own, unchanged contract).
        assert await db.sessions.find_one({"id": sid}) is None
    finally:
        await db.workflow_runs.delete_many({"run_id": run_id})
        await db.work_items.delete_many({"run_id": run_id})
        await db.cleanup_manifests.delete_many({"run_id": run_id})
        await db.run_credential_revocations.delete_many({"run_id": run_id})
        await db.sessions.delete_many({"id": sid})


@pytest.mark.asyncio
async def test_session_delete_still_succeeds_when_the_run_has_no_durable_workflow_run():
    """A pre-Phase-5 session (legacy _pipeline_run_id with no durable
    WorkflowRun) must still delete successfully -- CleanupManager's
    begin_cleanup call raises WorkflowError for an unknown run_id, and
    _run_cleanup_manager_best_effort must swallow that, not surface it."""
    db = get_db()
    sid = _sid()
    run_id = _sid()  # deliberately never given a WorkflowRun

    await db.sessions.insert_one({
        "id": sid, "owner": "alice", "status": "complete",
        "guard_report": {"status": "clean"}, "export_paths": {},
        "_pipeline_run_id": run_id,
    })

    try:
        import server as srv

        resp = await srv.session_delete(sid, principal="alice")
        assert resp == {"deleted": True}
        assert await db.sessions.find_one({"id": sid}) is None
        # No manifest, since there was never a durable run to clean up
        # against -- this must not raise, only skip the audit trail.
        assert await db.cleanup_manifests.find_one({"run_id": run_id}) is None
    finally:
        await db.sessions.delete_many({"id": sid})
        await db.cleanup_manifests.delete_many({"run_id": run_id})


@pytest.mark.asyncio
async def test_export_and_bundle_lifecycle_survives_a_backend_restart():
    """Phase 12 item 7: a session reaches export-ready, the process
    "restarts" (get_db.cache_clear() forces a brand-new AsyncIOMotorClient
    and therefore brand-new MongoControlStore/ArtifactService instances
    inside session_export/session_bundle on the next call -- the same
    simulated-restart technique test_control_superorchestrator_lifecycle.py's
    _fresh_orchestrator uses, applied here to the download routes), and
    both the export and bundle endpoints still serve correctly against
    the persisted Mongo state -- no in-process cache or object identity
    the first call happened to construct is required for the second.
    export_expires_at must also compute identically before and after,
    proving it derives purely from the persisted updated_at, not from
    anything held in memory.
    """
    from phi_core.control.artifacts import ArtifactService
    from phi_core.control.store import MongoControlStore

    db = get_db()
    sid = _sid()
    run_id = _sid()
    now = datetime.now(timezone.utc).isoformat()

    service = ArtifactService(MongoControlStore(db), session_id=sid, run_id=run_id)
    artifact_id, tmp_path = await service.stage("dataset_export", "a__export.csv", "restricted_metadata", "export")
    tmp_path.write_bytes(b"clean export bytes")
    await service.finalize(artifact_id)

    await db.sessions.insert_one({
        "id": sid, "owner": "alice", "status": "complete", "_pipeline_run_id": run_id,
        "export_paths": {"a": f"/staging/{sid}/{run_id}/{artifact_id}.csv"},
        "guard_report": {"status": "clean", "results": [{"file_id": "a", "status": "clean"}]},
        "updated_at": now,
    })

    try:
        import server as srv

        first_export = await srv.session_export(sid, "a", principal="alice")
        assert getattr(first_export, "status_code", 200) == 200
        assert first_export.path.read_bytes() == b"clean export bytes"

        first_bundle = await srv.session_bundle(sid, publication=False, attestation_pdf=False, principal="alice")
        assert first_bundle.status_code == 200

        session_before = await srv.session_get(sid, principal="alice")
        expires_before = session_before["export_expires_at"]
        assert expires_before is not None

        # "Restart": drop the cached Motor client so the next call
        # constructs a genuinely fresh AsyncIOMotorClient/MongoControlStore/
        # ArtifactService chain from scratch, sharing only the durable
        # Mongo state -- exactly the resume_plan tests' own contract.
        get_db.cache_clear()

        second_export = await srv.session_export(sid, "a", principal="alice")
        assert getattr(second_export, "status_code", 200) == 200
        assert second_export.path.read_bytes() == b"clean export bytes"

        second_bundle = await srv.session_bundle(sid, publication=False, attestation_pdf=False, principal="alice")
        assert second_bundle.status_code == 200

        session_after = await srv.session_get(sid, principal="alice")
        assert session_after["export_expires_at"] == expires_before
    finally:
        await db.sessions.delete_many({"id": sid})
        await service.erase_session_records(sid)
