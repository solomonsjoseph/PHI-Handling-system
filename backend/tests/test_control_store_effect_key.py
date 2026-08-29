"""Regression coverage for the work_items.effect_key sparse unique index.

Found live during Wave 4b's smoke test: WorkItem.effect_key defaults to ""
(records.py), and migrate.py's index on it is unique+sparse -- but Mongo's
sparse semantics skip a document from the uniqueness check only when the
field is genuinely absent, not merely an empty string. Serializing every
WorkItem through model_dump() always includes effect_key="" for any task
that never sets a real one, so the very first WorkItem ever inserted into a
persistent Mongo collided with every subsequent one, raising a live
DuplicateKeyError on the second-ever pipeline task enqueue. This is a
real-Mongo-only regression: MemoryControlStore has no index enforcement and
cannot reproduce it, so this file needs a live mongod (see needs_mongo
below) rather than being covered by test_control_tasks.py's in-memory suite.
"""
from __future__ import annotations

import pytest

from phi_core.db import get_db


@pytest.fixture(autouse=True)
def _fresh_motor_client_per_test():
    """`get_db` is `@lru_cache`d process-wide, binding its
    `AsyncIOMotorClient` to whichever event loop was running at first
    construction. Each `@pytest.mark.asyncio` test gets its own loop, so a
    client cached by an earlier test would be bound to a closed loop here.
    Matches `test_control_migrate.py`'s identical fixture."""
    get_db.cache_clear()
    yield
    get_db.cache_clear()


@pytest.mark.asyncio
async def test_two_work_items_with_default_effect_key_do_not_collide() -> None:
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.store import MongoControlStore
    from phi_core.control.tasks import TaskService

    # The server's own boot-time create_control_plane_indexes already
    # created the work_items.effect_key sparse unique index this test
    # exercises; re-calling it here would only risk an IndexOptionsConflict
    # against the agent_log/web_cache TTL indexes if this test's retention
    # values ever differ from the server's own configured values.
    db = get_db()
    store = MongoControlStore(db)
    service = TaskService(store, CapabilityPolicy(None))
    run_id = "run-" + "e" * 28
    try:
        first = await service.enqueue(
            run_id=run_id, session_id="session-1", worker="Executor", task_type="executor",
        )
        second = await service.enqueue(
            run_id=run_id, session_id="session-1", worker="Executor", task_type="executor",
        )
        assert first.task_id != second.task_id
        assert first.effect_key == ""
        assert second.effect_key == ""
        # The stored document must genuinely omit the key, not merely carry
        # an empty string, or the sparse index provides no protection at all.
        stored_first = await store.get_one("work_items", {"task_id": first.task_id})
        assert stored_first is not None and "effect_key" not in stored_first
    finally:
        await db.work_items.delete_many({"run_id": run_id})
        await db.capability_grants.delete_many({"run_id": run_id})


@pytest.mark.asyncio
async def test_an_explicit_real_effect_key_still_enforces_uniqueness() -> None:
    """The sparse index must still do its job when a caller actually wants
    effect-based deduplication: a second insert with the SAME non-empty
    effect_key must still be rejected, proving the fix only widens what
    counts as "no effect key intended", not the index's real purpose."""
    from pymongo.errors import DuplicateKeyError

    from phi_core.control.records import CapabilityGrant, WorkItem
    from phi_core.control.store import MongoControlStore

    db = get_db()
    store = MongoControlStore(db)
    run_id = "run-" + "f" * 28
    shared_effect_key = "effect-" + "f" * 28
    try:
        grant = CapabilityGrant(
            grant_id="grant-1", run_id=run_id, task_id="task-1", agent="Executor",
            manifest_version="1", policy_version="1", data_class_ceiling="internal",
            provider="", model="", endpoint="",
        )
        await store.insert("capability_grants", grant)
        first = WorkItem(
            task_id="task-1", run_id=run_id, session_id="session-1", worker="Executor",
            worker_version="1", task_type="executor", max_attempts=1,
            idempotency_key=f"{run_id}:task-1", effect_key=shared_effect_key,
            grant_id="grant-1",
        )
        await store.insert("work_items", first)
        second = WorkItem(
            task_id="task-2", run_id=run_id, session_id="session-1", worker="Executor",
            worker_version="1", task_type="executor", max_attempts=1,
            idempotency_key=f"{run_id}:task-2", effect_key=shared_effect_key,
            grant_id="grant-1",
        )
        with pytest.raises(DuplicateKeyError):
            await store.insert("work_items", second)
    finally:
        await db.work_items.delete_many({"run_id": run_id})
        await db.capability_grants.delete_many({"run_id": run_id})
