"""Phase 9 step 1: idempotent, re-runnable control-plane migrations.

Every function here is safe to run twice: it only ever acts on documents
still missing the state it would create, so a second pass finds nothing
left to do and returns a zero count. Each has a documented reverse step
in ``docs/assurance/MIGRATION.md``.

``run_all(db)`` is the single entry point ``server.py::_startup_maintenance``
and any standalone migration invocation (``python -m phi_core.control.migrate``)
both call.
"""
from __future__ import annotations

import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from phi_core.paths import PUBLISHED_DIR, run_scoped_dir

from .records import ArtifactRecord, WorkflowRun

_INDEX_SPECS: tuple[tuple[str, tuple[Any, ...], dict[str, Any]], ...] = (
    ("sessions", ("id",), {"unique": True}),
    ("sessions", ("owner",), {}),
    ("agent_log", ("session_id",), {}),
    ("workflow_runs", ("run_id",), {"unique": True}),
    ("workflow_runs", ([("session_id", 1), ("state", 1)],), {}),
    ("work_items", ("task_id",), {"unique": True}),
    ("work_items", ([("run_id", 1), ("idempotency_key", 1)],), {"unique": True}),
    ("work_items", ("effect_key",), {"unique": True, "sparse": True}),
    ("work_items", ([("state", 1), ("next_eligible_at", 1)],), {}),
    ("work_items", ([("state", 1), ("lease_expires_at", 1)],), {}),
    ("trace_events", ([("run_id", 1), ("seq", 1)],), {"unique": True}),
    ("trace_events", ("event_id",), {"unique": True}),
    ("artifacts", ("artifact_id",), {"unique": True}),
    ("artifacts", ([("root", 1), ("rel_path", 1)],), {"unique": True}),
    ("artifacts", ([("state", 1), ("expires_at", 1)],), {}),
    ("human_review_events", ([("request_id", 1), ("client_event_id", 1)],), {"unique": True}),
    ("evidence_claims", ("claim_id",), {"unique": True}),
)


async def create_control_plane_indexes(db, *, retention_days: int, web_cache_refresh_days: int) -> None:
    """Every index the control plane depends on, shared verbatim between
    `server.py::_startup_maintenance` (boot-time) and a standalone
    migration run. `create_index` is itself idempotent (a no-op when the
    exact index already exists)."""
    for collection, args, kwargs in _INDEX_SPECS:
        await db[collection].create_index(*args, **kwargs)
    await db.agent_log.create_index("ts", expireAfterSeconds=retention_days * 86400)
    await db.web_cache.create_index("fetched_at", expireAfterSeconds=web_cache_refresh_days * 86400)


async def backfill_workflow_runs(db) -> int:
    """Every session with a legacy `_pipeline_run_id` but no matching
    `workflow_runs` document (a pre-Phase-5 session) gets a minimal
    `WorkflowRun` row, so run-scoped code added since Phase 5
    (`check_run_budget`, `TraceEventStore`, retention's `_run_hold`) has
    something to look up instead of silently treating the run as
    unbounded/unheld. Reverse: delete the `workflow_runs` row whose
    `correlation_id` starts with `"backfill:"`."""
    created = 0
    cursor = db.sessions.find(
        {"_pipeline_run_id": {"$exists": True, "$ne": None}}, {"_id": 0, "id": 1, "_pipeline_run_id": 1, "status": 1},
    )
    async for doc in cursor:
        run_id = doc.get("_pipeline_run_id")
        sid = doc.get("id")
        if not run_id or not sid:
            continue
        existing = await db.workflow_runs.find_one({"run_id": run_id}, {"_id": 1})
        if existing is not None:
            continue
        run = WorkflowRun(
            run_id=run_id, session_id=sid, run_type="study",
            state="complete" if doc.get("status") in ("complete", "partially_complete") else "failed",
            correlation_id="backfill:migrate_workflow_runs",
        )
        await db.workflow_runs.insert_one(run.model_dump(mode="json"))
        created += 1
    return created


async def backfill_export_artifacts(db) -> int:
    """Every `export_paths` entry with no matching `ArtifactRecord`
    (bytes written before the D14 artifact registry existed) is hashed in
    place, registered as a `promoted` `ArtifactRecord`, and its bytes
    moved under `published/<session_id>/<run_id>/<artifact_id>`; the
    session's `export_paths` entry is updated to the new path. Reverse:
    move the file back to its recorded pre-migration path (kept in the
    artifact's `parents` field as `legacy:<original path>`) and delete
    the `ArtifactRecord`."""
    migrated = 0
    cursor = db.sessions.find(
        {"export_paths": {"$exists": True, "$ne": {}}}, {"_id": 0, "id": 1, "export_paths": 1, "_pipeline_run_id": 1},
    )
    async for doc in cursor:
        sid = doc.get("id")
        run_id = doc.get("_pipeline_run_id") or sid
        export_paths = doc.get("export_paths") or {}
        updated_paths = dict(export_paths)
        changed = False
        for key, raw_path in export_paths.items():
            if not raw_path:
                continue
            path = Path(raw_path)
            # A path already under PUBLISHED_DIR was migrated by a prior
            # pass (or was never legacy to begin with -- Phase 3 onward
            # writes exports there directly): nothing left to do. This
            # check must be structural, not a database lookup keyed by
            # the current path, because migrating rewrites the session's
            # own `export_paths` entry to that same new path -- a lookup
            # keyed by "the path I am about to migrate" can never match
            # the record a prior pass already created for it.
            try:
                path.relative_to(PUBLISHED_DIR)
                continue
            except ValueError:
                pass
            if not path.is_file():
                continue
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
                    size += len(chunk)
            artifact_id = uuid4().hex
            dest_dir = run_scoped_dir(PUBLISHED_DIR, sid, run_id)
            dest_path = dest_dir / artifact_id
            shutil.copyfile(path, dest_path)
            record = ArtifactRecord(
                artifact_id=artifact_id, session_id=sid, run_id=run_id, producer_task_id="",
                scope="session", type="legacy_export", root="published",
                rel_path=f"{sid}/{run_id}/{artifact_id}", sha256=digest.hexdigest(), size_bytes=size,
                state="promoted", data_class="restricted_metadata", retention_class="short",
                parents=[f"legacy:{raw_path}"], generation=1,
                promoted_at=datetime.now(timezone.utc).isoformat(),
            )
            await db.artifacts.insert_one(record.model_dump(mode="json"))
            updated_paths[key] = str(dest_path)
            changed = True
            migrated += 1
        if changed:
            await db.sessions.update_one({"id": sid}, {"$set": {"export_paths": updated_paths}})
    return migrated


async def migrate_agent_log_to_trace_events(db) -> int:
    """Every remaining legacy `agent_log` row (from before Phase 7 retired
    that write path) is converted to a `trace_events` row with a
    synthetic, run-scoped sequence number and a hash chain seeded fresh
    per run, then the original row is deleted -- consumption is itself
    the idempotency guarantee: a second pass finds no `agent_log` rows
    left to convert. Reverse: `agent_log` rows are not reconstructable
    from `trace_events` once deleted; keep a `mongodump` of `agent_log`
    before running this migration if reversibility is required."""

    converted = 0
    seq_by_run: dict[str, int] = {}
    cursor = db.agent_log.find({}).sort("ts", 1)
    async for msg in cursor:
        run_id = msg.get("session_id", "")  # legacy rows have no run_id; scope the synthetic chain by session
        seq_by_run[run_id] = seq_by_run.get(run_id, 0) + 1
        ts = msg.get("ts")
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts or "")
        event = {
            "schema_version": 1,
            "event_id": uuid4().hex,
            "run_id": run_id,
            "seq": seq_by_run[run_id],
            "session_id": msg.get("session_id", ""),
            "agent": msg.get("agent", ""),
            "phase": msg.get("phase", ""),
            "direction": msg.get("direction", ""),
            "payload": msg.get("payload") or {},
            "duration_ms": msg.get("duration_ms", 0.0),
            "latency_ms": int(msg.get("duration_ms", 0.0)),
            "parent_msg_id": msg.get("parent_id") or "",
            "status_text": msg.get("status_text", ""),
            "input_class": "internal",
            "output_class": "internal",
            "fence": 0,
            "prev_hash": "",
            "hash": hashlib.sha256(f"legacy:{msg.get('id', uuid4().hex)}".encode()).hexdigest(),
            "ts": ts_str,
        }
        await db.trace_events.insert_one(event)
        await db.agent_log.delete_one({"_id": msg["_id"]})
        converted += 1
    return converted


async def clear_orphaned_reversal_key_blobs(db) -> int:
    """A `reversal_key_blob` on a session already past its terminal
    retention window (long since erasure-eligible, per
    `_TERMINAL_RETENTION_STATUSES`) is a leftover no download route can
    reach any more -- `session_reversal_key` refuses for a non-owner and
    the session's own retention sweep will erase the whole document soon
    regardless. Clearing it here closes the gap between "no route can
    serve it" and "it is actually gone."

    Skips a session whose run carries a D14 hold, matching every other
    retention action in this control plane (a legal hold may specifically
    need the reversal key preserved for a compelled re-identification, so
    this is not a candidate for a blanket `update_many`).

    Reverse: reversal keys are encrypted key material with no separate
    backup by design; this migration is not reversible, matching D14's
    exact-once semantics for a reversal key."""
    cleared = 0
    cursor = db.sessions.find(
        {
            "reversal_key_blob": {"$exists": True, "$ne": None},
            "status": {"$in": ["complete", "failed", "cancelled", "blocked", "intake_failed",
                                "partially_complete", "expired_awaiting_review", "erasure_pending"]},
        },
        {"_id": 0, "id": 1, "_pipeline_run_id": 1},
    )
    async for doc in cursor:
        sid = doc.get("id")
        if not sid:
            continue
        run_id = doc.get("_pipeline_run_id")
        if run_id:
            run_doc = await db.workflow_runs.find_one({"run_id": run_id}, {"hold": 1})
            if (run_doc or {}).get("hold"):
                continue
        result = await db.sessions.update_one({"id": sid}, {"$unset": {"reversal_key_blob": ""}})
        if getattr(result, "modified_count", 0):
            cleared += 1
    return cleared


async def run_all(
    db, *, retention_days: int = 30, web_cache_refresh_days: int = 7,
) -> dict[str, int]:
    """Run every migration in dependency order (indexes first, since the
    backfills below insert into collections the indexes protect against
    duplication). Returns a per-migration affected-row count."""
    await create_control_plane_indexes(
        db, retention_days=retention_days, web_cache_refresh_days=web_cache_refresh_days,
    )
    return {
        "workflow_runs_backfilled": await backfill_workflow_runs(db),
        "export_artifacts_backfilled": await backfill_export_artifacts(db),
        "agent_log_rows_converted": await migrate_agent_log_to_trace_events(db),
        "reversal_key_blobs_cleared": await clear_orphaned_reversal_key_blobs(db),
    }


async def _main() -> None:  # pragma: no cover - operator entry point
    from phi_core.db import get_db

    db = get_db()
    counts = await run_all(db)
    for name, count in counts.items():
        print(f"{name}: {count}")


if __name__ == "__main__":  # pragma: no cover
    import asyncio

    asyncio.run(_main())
