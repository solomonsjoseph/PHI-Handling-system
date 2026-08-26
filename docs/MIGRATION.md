# Migrations

Every migration in `backend/phi_core/control/migrate.py` is idempotent
(safe to run twice; a second pass finds nothing left to do and returns a
zero count) and independent of the others except for ordering: indexes
are created first since the backfills below insert into the collections
those indexes protect against duplication.

Run all of them:

```
cd backend && python -m phi_core.control.migrate
```

`server.py::_startup_maintenance` runs `create_control_plane_indexes`
automatically on every boot. The four backfill/cleanup migrations below
are **not** run automatically; an operator runs them deliberately, once,
against a deployment that has legacy data from before the corresponding
control-plane component existed.

## `create_control_plane_indexes`

Creates every index the control plane depends on (`sessions.id` unique,
`workflow_runs.run_id` unique, `trace_events.(run_id, seq)` unique, the
`web_cache.fetched_at` and `agent_log.ts` TTL indexes, and so on).
`create_index` is itself idempotent — an index that already exists with
the same key and options is a no-op.

**Reverse:** `db.<collection>.drop_index(<name>)` per index. Dropping an
index never loses data; it only removes the query-plan/uniqueness
guarantee, which the next boot's `create_control_plane_indexes` call
recreates anyway.

## `backfill_workflow_runs`

Every `sessions` document with a legacy `_pipeline_run_id` but no
matching `workflow_runs` row (a pre-Phase-5 session, before
`SuperOrchestrator.start_run` existed) gets a minimal `WorkflowRun` row
stamped `correlation_id="backfill:migrate_workflow_runs"`, `state` set to
`complete` or `failed` from the session's own `status`. Without this row,
Phase 5+ code that expects a durable run (`check_run_budget`,
`TraceEventStore`'s fence check, retention's `_run_hold`) silently treats
the run as unbounded and unheld — this migration gives every legacy
session the same durable-run floor a session created after Phase 5 has
always had.

**Reverse:** call `backfill_workflow_runs_rollback(db)`. It deletes only
the migration-marked row when it remains pristine. A marked run with
recorded activity or run-scoped history is retained rather than deleted.

## `backfill_export_artifacts`

Every `export_paths` entry pointing at a file with no matching
`ArtifactRecord` (bytes written before the D14 artifact registry
existed, Phase 3) is hashed in place, registered as a `promoted`
`ArtifactRecord` of type `legacy_export`, and copied to
`published/<session_id>/<run_id>/<artifact_id>`. The session's
`export_paths` entry is rewritten to the new path so every download route
(`ArtifactService.open_for_download`) can serve it identically to a
post-Phase-3 export. Idempotency is structural, not a database lookup:
an entry whose current path already resolves under `PUBLISHED_DIR` is
skipped outright, since migrating rewrites the very path a naive
database-keyed idempotency check would have looked up.

**Reverse:** call `backfill_export_artifacts_rollback(db)`. It restores
each owning session's matching `export_paths` entry to the path in its
`legacy:` parent marker, deletes the copied published file when the
original retained bytes still match the recorded digest, and removes the
`ArtifactRecord`. If the legacy file no longer exists, it moves the
published copy back to that path before deleting the record.

## `migrate_agent_log_to_trace_events`

Every remaining legacy `agent_log` row (written before Phase 7 retired
that path) is converted to a `trace_events` row with a
synthetic sequence number scoped per `session_id` (legacy rows have no
`run_id`) and deleted from `agent_log` immediately after its conversion
succeeds. Consumption is the idempotency guarantee: a second pass finds
no `agent_log` rows left to convert.

**Reverse:** not reversible. `agent_log` rows are deleted, not archived,
once converted, and a converted `trace_events` row cannot be
distinguished from a real Phase 7+ trace event after the fact (both share
the same schema). If reversibility is a hard requirement for a specific
deployment, `mongodump --collection=agent_log` before running this
migration.

## `clear_orphaned_reversal_key_blobs`

A `reversal_key_blob` present on a session whose `status` is already
terminal (`complete`, `failed`, `cancelled`, `blocked`, `intake_failed`,
`partially_complete`, `expired_awaiting_review`, or `erasure_pending`)
and whose run carries no D14 hold is cleared. `session_reversal_key`
already deletes the blob the moment it is downloaded and refuses to
serve one for a non-owner; a blob surviving on an already-terminal,
unheld session is one nobody downloaded and the session's own retention
sweep will erase the whole document soon regardless — this migration
closes the gap between "no route can serve it any more" and "it is
actually gone." A held run's blob is left alone: a legal hold may
specifically need the reversal key preserved for a compelled
re-identification.

**Reverse:** not reversible. A reversal key is encrypted key material
with no separate backup by design (D14's "exact-once" semantics for a
reversal key: the operator sees it exactly once, at download time, or
never again). Clearing it here is the same one-way action
`session_reversal_key`'s own successful download already performs. Before
running the migration, back up the candidate `sessions` documents with
`mongodump --collection=sessions --query='{"reversal_key_blob":{"$exists":true,"$ne":null},"status":{"$in":["complete","failed","cancelled","blocked","intake_failed","partially_complete","expired_awaiting_review","erasure_pending"]}}'`.
