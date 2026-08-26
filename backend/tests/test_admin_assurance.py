"""D15 step 5: GET /api/admin/assurance -- the operator triage dashboard."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest


def _iso(delta_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=delta_seconds)).isoformat()


@pytest.mark.asyncio
async def test_admin_assurance_refuses_a_plain_reviewer():
    import server as srv

    with pytest.raises(Exception) as exc:
        await srv.admin_assurance(principal="mallory")
    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_admin_assurance_reports_every_category(monkeypatch):
    import server as srv
    from phi_core.db import get_db

    db = get_db()
    run_id = uuid.uuid4().hex
    task_id = uuid.uuid4().hex
    artifact_id = uuid.uuid4().hex
    sid = uuid.uuid4().hex

    await db.work_items.insert_one({
        "task_id": task_id, "task_type": "executor", "run_id": run_id,
        "state": "leased", "lease_owner": "worker-1", "lease_expires_at": _iso(-120),
    })
    await db.trace_events.insert_one({
        "run_id": run_id, "seq": 1, "session_id": sid, "outcome": "budget_exceeded",
        "status_text": "MAX_TOKENS_PER_TASK exceeded",
    })
    await db.gate_results.insert_one({
        "gate_id": uuid.uuid4().hex, "run_id": run_id, "task_id": task_id,
        "gate": "d11_decisions", "gate_version": "1", "status": "fail",
        "detail": "missing coverage", "created_at": _iso(),
    })
    await db.artifacts.insert_one({
        "artifact_id": artifact_id, "session_id": sid, "run_id": run_id,
        "state": "deletion_pending", "delete_attempts": 2, "delete_error": "permission denied",
    })
    await db.sessions.insert_one({
        "id": sid, "status": "erasure_pending", "erasure_error": "permission denied",
        "erasure_attempts": 1, "updated_at": _iso(),
    })
    await db.publication_pointers.insert_one({
        "session_id": sid, "run_id": run_id, "generation": 1, "certified_at": _iso(),
    })

    try:
        report = await srv.admin_assurance(principal="reviewer-1")
    finally:
        for collection, query in (
            (db.work_items, {"task_id": task_id}),
            (db.trace_events, {"run_id": run_id}),
            (db.gate_results, {"run_id": run_id}),
            (db.artifacts, {"artifact_id": artifact_id}),
            (db.sessions, {"id": sid}),
            (db.publication_pointers, {"session_id": sid}),
        ):
            await collection.delete_many(query)

    assert any(row["task_id"] == task_id for row in report["stuck_leases"])
    assert report["policy_denials"]["total"] >= 1
    assert "MAX_TOKENS_PER_TASK exceeded" in report["policy_denials"]["by_reason"]
    assert any(row["gate_id"] and row["run_id"] == run_id for row in report["gate_failures"])
    assert any(row["artifact_id"] == artifact_id for row in report["orphan_artifacts"])
    assert any(row["id"] == sid for row in report["erasure_failures"])
    assert any(row["session_id"] == sid for row in report["publication_outcomes"])
