"""Phase 9 step 1 acceptance tests for control/migrate.py.

Uses the real test Mongo instance (matching test_admin_assurance.py's
convention): these migrations operate on raw motor collections, not
`ControlStore`, so a `MemoryControlStore` fixture would not exercise the
same code path.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from phi_core.control.migrate import (
    backfill_export_artifacts,
    backfill_workflow_runs,
    clear_orphaned_reversal_key_blobs,
    create_control_plane_indexes,
    migrate_agent_log_to_trace_events,
    run_all,
)
from phi_core.db import get_db


def _sid() -> str:
    return uuid.uuid4().hex


@pytest.fixture(autouse=True)
def _fresh_motor_client_per_test():
    """`get_db` is `@lru_cache`d process-wide, binding its
    `AsyncIOMotorClient` to whichever event loop was running at first
    construction. `asyncio_mode=strict` gives each `@pytest.mark.asyncio`
    test its own loop, so a client cached by an earlier test file/test
    crashes every real-Mongo call here with "Event loop is closed"
    unless it is rebuilt fresh against the loop this test actually
    runs on."""
    get_db.cache_clear()
    yield
    get_db.cache_clear()


@pytest.mark.asyncio
async def test_create_control_plane_indexes_is_idempotent():
    db = get_db()
    await create_control_plane_indexes(db, retention_days=30, web_cache_refresh_days=7)
    await create_control_plane_indexes(db, retention_days=30, web_cache_refresh_days=7)  # must not raise


@pytest.mark.asyncio
async def test_backfill_workflow_runs_creates_a_run_for_a_legacy_session_and_is_idempotent():
    db = get_db()
    sid = _sid()
    run_id = _sid()
    await db.sessions.insert_one({"id": sid, "status": "complete", "_pipeline_run_id": run_id})
    try:
        created_first = await backfill_workflow_runs(db)
        assert created_first >= 1
        run_doc = await db.workflow_runs.find_one({"run_id": run_id})
        assert run_doc is not None
        assert run_doc["session_id"] == sid
        assert run_doc["correlation_id"] == "backfill:migrate_workflow_runs"

        created_second = await backfill_workflow_runs(db)
        assert created_second == 0  # already backfilled; nothing left to do
    finally:
        await db.sessions.delete_one({"id": sid})
        await db.workflow_runs.delete_one({"run_id": run_id})


@pytest.mark.asyncio
async def test_backfill_export_artifacts_registers_and_moves_a_legacy_export(tmp_path):
    db = get_db()
    sid = _sid()
    run_id = _sid()
    legacy_export = tmp_path / "legacy_export.csv"
    legacy_export.write_text("handled data", encoding="utf-8")
    await db.sessions.insert_one({
        "id": sid, "_pipeline_run_id": run_id, "export_paths": {"dataset": str(legacy_export)},
    })
    try:
        migrated_first = await backfill_export_artifacts(db)
        assert migrated_first >= 1
        record = await db.artifacts.find_one({"session_id": sid, "type": "legacy_export"})
        assert record is not None
        assert record["state"] == "promoted"
        assert record["parents"] == [f"legacy:{legacy_export}"]
        updated_session = await db.sessions.find_one({"id": sid})
        assert updated_session["export_paths"]["dataset"] != str(legacy_export)
        assert Path(updated_session["export_paths"]["dataset"]).is_file()

        migrated_second = await backfill_export_artifacts(db)
        assert migrated_second == 0  # already registered; nothing left to do
    finally:
        await db.sessions.delete_one({"id": sid})
        await db.artifacts.delete_many({"session_id": sid})


@pytest.mark.asyncio
async def test_migrate_agent_log_to_trace_events_converts_and_consumes_every_row():
    db = get_db()
    sid = _sid()
    await db.agent_log.insert_many([
        {"id": uuid.uuid4().hex, "session_id": sid, "agent": "Judge", "phase": "judge.decide",
         "direction": "in", "payload": {"prompt_text": "x"}, "ts": datetime.now(timezone.utc)},
        {"id": uuid.uuid4().hex, "session_id": sid, "agent": "Judge", "phase": "judge.decide",
         "direction": "out", "payload": {"reply_text": "y"}, "ts": datetime.now(timezone.utc)},
    ])
    try:
        converted = await migrate_agent_log_to_trace_events(db)
        assert converted >= 2
        remaining = await db.agent_log.count_documents({"session_id": sid})
        assert remaining == 0  # consumed

        events = await db.trace_events.find({"session_id": sid}).sort("seq", 1).to_list(length=None)
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        assert events[0]["direction"] == "in"
        assert events[1]["direction"] == "out"

        converted_again = await migrate_agent_log_to_trace_events(db)
        assert converted_again == 0  # already consumed; nothing left to convert
    finally:
        await db.agent_log.delete_many({"session_id": sid})
        await db.trace_events.delete_many({"session_id": sid})


@pytest.mark.asyncio
async def test_clear_orphaned_reversal_key_blobs_only_touches_terminal_sessions():
    db = get_db()
    terminal_sid = _sid()
    live_sid = _sid()
    await db.sessions.insert_many([
        {"id": terminal_sid, "status": "complete", "reversal_key_blob": "encrypted-blob"},
        {"id": live_sid, "status": "reading", "reversal_key_blob": "encrypted-blob"},
    ])
    try:
        cleared = await clear_orphaned_reversal_key_blobs(db)
        assert cleared >= 1
        terminal_doc = await db.sessions.find_one({"id": terminal_sid})
        assert "reversal_key_blob" not in terminal_doc
        live_doc = await db.sessions.find_one({"id": live_sid})
        assert live_doc["reversal_key_blob"] == "encrypted-blob"
    finally:
        await db.sessions.delete_many({"id": {"$in": [terminal_sid, live_sid]}})



@pytest.mark.asyncio
async def test_clear_orphaned_reversal_key_blobs_respects_a_hold():
    """D14: a hold on the session's run suspends this retention action
    too, like every other one in the control plane."""
    db = get_db()
    sid = _sid()
    run_id = _sid()
    await db.sessions.insert_one({
        "id": sid, "status": "complete", "reversal_key_blob": "encrypted-blob", "_pipeline_run_id": run_id,
    })
    await db.workflow_runs.insert_one({"run_id": run_id, "session_id": sid, "hold": "litigation-hold-1"})
    try:
        cleared = await clear_orphaned_reversal_key_blobs(db)
        assert cleared == 0
        doc = await db.sessions.find_one({"id": sid})
        assert doc["reversal_key_blob"] == "encrypted-blob"
    finally:
        await db.sessions.delete_one({"id": sid})
        await db.workflow_runs.delete_one({"run_id": run_id})

@pytest.mark.asyncio
async def test_run_all_returns_a_count_per_migration_and_never_raises_on_an_empty_database():
    db = get_db()
    counts = await run_all(db, retention_days=30, web_cache_refresh_days=7)
    assert set(counts) == {
        "workflow_runs_backfilled", "export_artifacts_backfilled",
        "agent_log_rows_converted", "reversal_key_blobs_cleared",
    }
    assert all(isinstance(v, int) for v in counts.values())

