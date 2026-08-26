"""POST/DELETE /api/admin/hold -- setting and clearing a D14
legal/administrative hold on a session's run."""
from __future__ import annotations

import uuid

import pytest
from phi_core.db import get_db


@pytest.fixture(autouse=True)
def _fresh_motor_client_per_test():
    """See test_control_migrate.py's identical fixture: `get_db` is
    `@lru_cache`d process-wide and binds its client to whichever event
    loop was running at first construction, which crashes real-Mongo
    calls here once an earlier test file's loop has closed."""
    get_db.cache_clear()
    yield
    get_db.cache_clear()


@pytest.mark.asyncio
async def test_admin_set_hold_refuses_a_plain_reviewer():
    import server as srv

    with pytest.raises(Exception) as exc:
        await srv.admin_set_hold(srv.AdminHoldBody(session_id="x", reason="litigation"), principal="mallory")
    assert getattr(exc.value, "status_code", None) == 403


@pytest.mark.asyncio
async def test_admin_set_hold_requires_a_reason():
    import server as srv

    with pytest.raises(Exception) as exc:
        await srv.admin_set_hold(srv.AdminHoldBody(session_id="x", reason="  "), principal="reviewer-1")
    assert getattr(exc.value, "status_code", None) == 400


@pytest.mark.asyncio
async def test_admin_set_hold_404s_for_an_unknown_session():
    import server as srv

    with pytest.raises(Exception) as exc:
        await srv.admin_set_hold(
            srv.AdminHoldBody(session_id=uuid.uuid4().hex, reason="litigation"), principal="reviewer-1",
        )
    assert getattr(exc.value, "status_code", None) == 404


@pytest.mark.asyncio
async def test_admin_set_hold_409s_when_the_session_has_no_durable_run():
    import server as srv

    db = get_db()
    sid = uuid.uuid4().hex
    await db.sessions.insert_one({"id": sid, "status": "complete"})
    try:
        with pytest.raises(Exception) as exc:
            await srv.admin_set_hold(srv.AdminHoldBody(session_id=sid, reason="litigation"), principal="reviewer-1")
        assert getattr(exc.value, "status_code", None) == 409
    finally:
        await db.sessions.delete_one({"id": sid})


@pytest.mark.asyncio
async def test_admin_set_and_clear_hold_round_trip_and_record_trace_events():
    import server as srv

    db = get_db()
    sid = uuid.uuid4().hex
    run_id = uuid.uuid4().hex
    await db.sessions.insert_one({"id": sid, "status": "complete", "_pipeline_run_id": run_id})
    await db.workflow_runs.insert_one({
        "run_id": run_id, "session_id": sid, "hold": "", "event_seq": 0,
        "updated_at": "2026-01-01T00:00:00+00:00",
    })
    try:
        set_result = await srv.admin_set_hold(
            srv.AdminHoldBody(session_id=sid, reason="litigation-hold-1"), principal="reviewer-1",
        )
        assert set_result == {"run_id": run_id, "hold": "litigation-hold-1"}
        run_doc = await db.workflow_runs.find_one({"run_id": run_id})
        assert run_doc["hold"] == "litigation-hold-1"

        clear_result = await srv.admin_clear_hold(session_id=sid, reason="resolved", principal="reviewer-1")
        assert clear_result == {"run_id": run_id, "hold": ""}
        run_doc_after = await db.workflow_runs.find_one({"run_id": run_id})
        assert run_doc_after["hold"] == ""

        events = await db.trace_events.find({"run_id": run_id}).sort("seq", 1).to_list(length=None)
        assert [e["outcome"] for e in events] == ["hold_set", "hold_cleared"]
        assert "principal=reviewer-1" in events[0]["status_text"]
        assert "reason=litigation-hold-1" in events[0]["status_text"]
    finally:
        await db.sessions.delete_one({"id": sid})
        await db.workflow_runs.delete_one({"run_id": run_id})
        await db.trace_events.delete_many({"run_id": run_id})
