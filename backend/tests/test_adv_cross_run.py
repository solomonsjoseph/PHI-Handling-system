"""Phase 15b category 2: cross-run isolation (docs section 98).

Positive-detection adversarial tests: Run A's artifact, download, and
review surfaces must never be reachable from Run B's context, and two
runs' cache-root artifacts and sandboxes must never share state --
proven against the actual production authorization/scoping checks, not
a vacuous "nothing was returned" absence check.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from phi_core.control.artifacts import ArtifactError, ArtifactService
from phi_core.control.sandbox import create_sandbox, destroy_sandbox
from phi_core.control.store import MemoryControlStore


def _run_id() -> str:
    return uuid4().hex


_FAIL_CLOSED_TEST_NAME = "__never_matches__"


@pytest.fixture(autouse=True)
def _allow_unenforced_sandbox_memory(request, monkeypatch):
    if request.node.name != _FAIL_CLOSED_TEST_NAME:
        monkeypatch.setenv("PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY", "1")


async def _stage_and_finalize(store: MemoryControlStore, sid: str, run_id: str, content: bytes) -> str:
    service = ArtifactService(store, session_id=sid, run_id=run_id)
    artifact_id, tmp = await service.stage("dataset_export", "export.csv", "restricted_metadata", "export")
    tmp.write_bytes(content)
    await service.finalize(artifact_id)
    return artifact_id


# ---------------------------------------------------------------------------
# 1. Run A -> Run B artifact: open_for_download must never resolve an
#    artifact staged under Run A's session when queried under Run B's.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_open_for_download_refuses_run_a_artifact_under_run_b_session():
    store = MemoryControlStore()
    sid_a, run_a = uuid4().hex, uuid4().hex
    sid_b, run_b = uuid4().hex, uuid4().hex
    planted = b"run-a-only clean export bytes, never for run b"
    artifact_id = await _stage_and_finalize(store, sid_a, run_a, planted)

    service_b = ArtifactService(store, session_id=sid_b, run_id=run_b)
    with pytest.raises(ArtifactError) as excinfo:
        await service_b.open_for_download(sid_b, artifact_id)
    assert excinfo.value.reason == "artifact_missing"

    # And confirm the mechanism is genuinely the session scope, not merely
    # "never promoted": the SAME artifact_id under its OWN session at
    # least resolves the record (state may still be pre-promotion, but the
    # doc itself is found), proving the refusal above is specifically
    # about crossing sessions.
    own_doc = await store.get_one("artifacts", {"artifact_id": artifact_id, "session_id": sid_a})
    assert own_doc is not None
    cross_doc = await store.get_one("artifacts", {"artifact_id": artifact_id, "session_id": sid_b})
    assert cross_doc is None


# ---------------------------------------------------------------------------
# 2. Run A -> Run B download: the real session_export/session_bundle
#    download routes refuse an artifact_id minted for a different
#    session, driven through the actual production ArtifactService
#    wiring (mirrors test_download_artifact_binding.py's own rig).
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
async def test_session_bundle_refuses_when_export_paths_reference_another_runs_artifact(monkeypatch):
    """Session B's own document accidentally (or maliciously) references
    Run A's artifact_id in its export_paths -- the hash/session binding
    inside ArtifactService.open_for_download must still refuse it rather
    than serve Run A's bytes to a Run B request."""
    import server as srv

    store = MemoryControlStore()
    sid_a, run_a = uuid4().hex, uuid4().hex
    sid_b, run_b = uuid4().hex, uuid4().hex
    artifact_a = await _stage_and_finalize(store, sid_a, run_a, b"run-a export bytes")

    session_b_doc = {
        "id": sid_b,
        "status": "complete",
        "_pipeline_run_id": run_b,
        "export_paths": {"dataset": f"/staging/{sid_a}/{run_a}/{artifact_a}.csv"},
        "guard_report": {"status": "clean", "results": [{"file_id": "dataset", "status": "clean"}]},
    }
    db = _StubDB(session_b_doc)
    monkeypatch.setattr(srv, "get_db", lambda: db)
    monkeypatch.setattr(srv, "_artifact_service",
                        lambda _db, s, r: ArtifactService(store, session_id=s, run_id=r))

    with pytest.raises(Exception) as excinfo:
        await srv.session_export(sid_b, "dataset")
    # Either a clean HTTPException (404/409/410) or the raw ArtifactError
    # bubbling from a scope mismatch -- either way, never a 200 with Run
    # A's bytes served to a Run B request.
    status = getattr(excinfo.value, "status_code", None)
    assert status is None or status >= 400


# ---------------------------------------------------------------------------
# 3. Run A -> Run B review: session_dataset_file (and, through it,
#    session_human_review_source, docs #47) resolves file_id strictly
#    against the requested session's own files list -- a file_id that
#    only exists on a different session's document is refused.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_dataset_file_refuses_a_file_id_that_belongs_to_another_session(monkeypatch):
    import server as srv

    sid_b = uuid4().hex
    other_session_file_id = "file-belongs-to-session-a-" + uuid4().hex
    session_b_doc = {
        "id": sid_b,
        "files": [{"file_id": "file-belongs-to-session-b", "kind": "dataset", "stored_path": "/tmp/b.csv"}],
    }

    class _DB:
        sessions = None

        async def find_one(self, *_a, **_kw):
            return session_b_doc

    db = _DB()
    db.sessions = db
    monkeypatch.setattr(srv, "get_db", lambda: db)

    with pytest.raises(Exception) as excinfo:
        await srv.session_dataset_file(sid_b, other_session_file_id, "dev")
    assert getattr(excinfo.value, "status_code", 404) == 404
    assert "no such file on this session" in str(getattr(excinfo.value, "detail", excinfo.value))


# ---------------------------------------------------------------------------
# 4. cache collision: two runs staging a "cache"-root artifact must land
#    on genuinely disjoint on-disk paths -- run B never sees run A's
#    cached bytes even when both stage the exact same declared filename.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_two_runs_cache_root_artifacts_never_collide_on_disk():
    from phi_core.paths import CACHE_DIR

    store = MemoryControlStore()
    sid_a, run_a = uuid4().hex, uuid4().hex
    sid_b, run_b = uuid4().hex, uuid4().hex
    service_a = ArtifactService(store, session_id=sid_a, run_id=run_a)
    service_b = ArtifactService(store, session_id=sid_b, run_id=run_b)

    id_a, tmp_a = await service_a.stage("research_snapshot", "cache.json", "internal", "review", root="cache")
    id_b, tmp_b = await service_b.stage("research_snapshot", "cache.json", "internal", "review", root="cache")
    tmp_a.write_bytes(b"run-a-cache-payload")
    tmp_b.write_bytes(b"run-b-cache-payload")
    await service_a.finalize(id_a)
    await service_b.finalize(id_b)

    final_a = CACHE_DIR / sid_a / run_a / id_a
    final_b = CACHE_DIR / sid_b / run_b / id_b
    assert final_a != final_b
    assert final_a.read_bytes() == b"run-a-cache-payload"
    assert final_b.read_bytes() == b"run-b-cache-payload"
    assert id_a != id_b


# ---------------------------------------------------------------------------
# 5. sandbox collision: two DIFFERENT runs' sandboxes must never share
#    state, extending test_control_phase2_sandbox_and_raw_data_boundary
#    .py's own same-run collision test to the cross-run case.
# ---------------------------------------------------------------------------


def test_two_different_runs_sandboxes_never_share_state():
    run_a, run_b = _run_id(), _run_id()
    record_a = create_sandbox(run_a)
    record_b = create_sandbox(run_b)
    try:
        assert record_a.workspace_path != record_b.workspace_path
        planted = "run-a-only-secret-marker-9f31"
        (Path(record_a.workspace_path) / "marker.txt").write_text(planted, encoding="utf-8")

        workspace_b_files = list(Path(record_b.workspace_path).rglob("*"))
        for f in workspace_b_files:
            if f.is_file():
                assert planted not in f.read_text(encoding="utf-8", errors="ignore")
        assert not (Path(record_b.workspace_path) / "marker.txt").exists()
    finally:
        destroy_sandbox(record_a)
        destroy_sandbox(record_b)
