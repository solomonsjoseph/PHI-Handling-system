"""Phase 15b category 9: cleanup (docs section 98).

Positive-detection adversarial tests: sandbox workspace/temporary
executables, an opaque-map key, a reversal key, a run's credentials,
staged/cached artifacts, and a published export past its retention
window must each be genuinely destroyed/refused by CleanupManager
(docs #76-77) and the export-retention gate -- never silently reported
as clean when a real erasure step actually failed.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from phi_core.control.cleanup_manager import CATEGORY_CREDENTIALS, CleanupInputs, CleanupManager
from phi_core.control.manager import Manager
from phi_core.control.policy import CapabilityPolicy
from phi_core.control.sandbox import create_sandbox, destroy_sandbox
from phi_core.control.store import MemoryControlStore
from phi_core.control.tasks import TaskService

_COMPLETE_OUTCOMES = (
    "ok", "ok", "ok", "ok", "proceed", "ok", "ok", "ok", "clean", "ok", "ok", "ok", "complete",
)
_FAIL_CLOSED_TEST_NAME = "__never_matches__"


@pytest.fixture(autouse=True)
def _allow_unenforced_sandbox_memory(request, monkeypatch):
    if request.node.name != _FAIL_CLOSED_TEST_NAME:
        monkeypatch.setenv("PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY", "1")


def _rig() -> tuple[CleanupManager, Manager, MemoryControlStore]:
    store = MemoryControlStore()
    orch = Manager(store, TaskService(store, CapabilityPolicy(None)))
    return CleanupManager(store, orch), orch, store


async def _completed_run(orch: Manager, run_id: str):
    run = await orch.start_run(session_id=run_id, principal="dev", run_id=run_id)
    for outcome in _COMPLETE_OUTCOMES:
        run = await orch.advance(run_id=run_id, outcome=outcome)
    return run


# ---------------------------------------------------------------------------
# 1. sandbox (and temporary code, folded into the same on-disk workspace)
#    survives -- a real sandbox, with a real planted executable-shaped
#    temp file inside it, must genuinely no longer exist on disk after
#    a verified cleanup pass, proven against the real filesystem, not a
#    mocked boolean.
# ---------------------------------------------------------------------------


def test_sandbox_and_temporary_executable_do_not_survive_a_real_cleanup_pass():
    run_id = uuid4().hex
    record = create_sandbox(run_id)
    planted_script = Path(record.workspace_path) / "leftover_transform.py"
    planted_script.write_text("#!/usr/bin/env python\nprint('temporary worker code')\n", encoding="utf-8")
    assert planted_script.exists()

    updated, _cleanup_manifest = destroy_sandbox(record)

    assert updated.state == "destroyed"
    assert not planted_script.exists()
    assert not Path(record.workspace_path).exists()


@pytest.mark.asyncio
async def test_cleanup_manager_genuinely_destroys_a_real_sandbox_workspace(tmp_path):
    manager, orch, _store = _rig()
    run = await _completed_run(orch, uuid4().hex)
    real_sandbox = create_sandbox(run.run_id)
    marker = Path(real_sandbox.workspace_path) / "cached_intermediate.tmp"
    marker.write_text("should not survive cleanup", encoding="utf-8")

    manifest = await manager.cleanup(CleanupInputs(run_id=run.run_id, sandbox=real_sandbox))

    assert manifest.sandbox_destroyed is True
    assert not Path(real_sandbox.workspace_path).exists()
    assert not marker.exists()


# ---------------------------------------------------------------------------
# 2. key survives -- a genuine partial failure (opaque map erased, but
#    the reversal key erasure step itself fails) must be recorded
#    honestly: keys_destroyed False, the surviving category named, and
#    verification never reports success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_reversal_key_that_fails_to_erase_is_never_reported_as_destroyed():
    manager, orch, _store = _rig()
    run = await _completed_run(orch, uuid4().hex)

    async def _opaque_map_erases_fine() -> bool:
        return True

    async def _reversal_key_erasure_genuinely_fails() -> bool:
        return False  # the real erasure step ran and reported failure

    manifest = await manager.cleanup(CleanupInputs(
        run_id=run.run_id, opaque_map_present=True, erase_opaque_map=_opaque_map_erases_fine,
        reversal_key_present=True, erase_reversal_key=_reversal_key_erasure_genuinely_fails,
    ))

    assert manifest.keys_destroyed is False
    assert "reversal_key_ciphertext" not in manifest.destroyed_categories
    assert "opaque_map_vault" in manifest.destroyed_categories  # the genuinely-successful half is honest too
    assert manifest.verification_status == "failed"
    assert "reversal_key_ciphertext" in manifest.failure_details


# ---------------------------------------------------------------------------
# 3. credential survives -- an external revocation action (e.g.
#    invalidating a signed session cookie/token) that raises must still
#    be recorded as a genuine failure, even though the internal
#    revocation-record write always succeeds.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_raising_external_credential_revocation_step_is_recorded_not_swallowed():
    manager, orch, store = _rig()
    run = await _completed_run(orch, uuid4().hex)

    async def _external_revocation_backend_is_down() -> bool:
        raise RuntimeError("session token revocation service unreachable (503)")

    manifest = await manager.cleanup(CleanupInputs(
        run_id=run.run_id, revoke_credentials=_external_revocation_backend_is_down,
    ))

    assert CATEGORY_CREDENTIALS not in manifest.destroyed_categories
    assert manifest.credentials_revoked is False
    assert "session token revocation service unreachable" in manifest.failure_details
    assert manifest.verification_status == "failed"
    # The internal audit record was still written -- this category's
    # failure is specifically the external step, not silently invisible.
    revocations = await store.find_many("run_credential_revocations", {"run_id": run.run_id})
    assert len(revocations) == 1


# ---------------------------------------------------------------------------
# 4. cache survives -- staged/cached artifact erasure reporting a genuine
#    per-item failure list must block verification, never silently
#    marked complete.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_staged_cache_artifacts_that_fail_to_erase_block_verification():
    manager, orch, _store = _rig()
    run = await _completed_run(orch, uuid4().hex)
    surviving_cache_files = ["cache/research_snapshot_a.json", "cache/research_snapshot_b.json"]

    async def _cache_erasure_leaves_files_behind() -> "tuple[bool, list[str]]":
        return False, surviving_cache_files

    manifest = await manager.cleanup(CleanupInputs(
        run_id=run.run_id, erase_staged_artifacts=_cache_erasure_leaves_files_behind,
    ))

    assert "staged_intake_and_cached_artifacts" not in manifest.destroyed_categories
    assert manifest.verification_status == "failed"
    for name in surviving_cache_files:
        assert name in manifest.failure_details


# ---------------------------------------------------------------------------
# 5. ZIP survives expiry -- a published, clean, export-ready session
#    whose EXPORT_RETENTION_WINDOW has genuinely elapsed must be refused
#    (410) at every real download route, not merely "flagged" -- driven
#    through the actual production session_export function.
# ---------------------------------------------------------------------------


class _StubDB:
    def __init__(self, doc: dict):
        self.doc = doc
        self.sessions = self

    async def find_one(self, *_args, **_kwargs):
        return self.doc

    async def update_one(self, *_args, **_kwargs):
        return None


@pytest.mark.asyncio
async def test_an_expired_export_zip_is_refused_not_served(monkeypatch):
    import server as srv

    sid = uuid4().hex
    # EXPORT_RETENTION_WINDOW_DAYS defaults to 14; go well past it regardless
    # of any local override.
    long_expired_updated_at = datetime.now(timezone.utc) - timedelta(days=3650)
    session_doc = {
        "id": sid,
        "status": "complete",
        "updated_at": long_expired_updated_at.isoformat(),
        "guard_report": {"status": "clean", "results": [{"file_id": "dataset", "status": "clean"}]},
        "export_paths": {"dataset": f"/staging/{sid}/whatever/export.csv"},
    }
    db = _StubDB(session_doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)

    with pytest.raises(Exception) as excinfo:
        await srv.session_export(sid, "dataset")

    assert getattr(excinfo.value, "status_code", None) == 410
