# Assurance runbook

Operator procedures for the control-plane failure categories `GET
/api/admin/assurance` (Phase 7 step 5, role `lead_reviewer`) reports. Each
section gives the dashboard field, the direct Mongo query for deeper
investigation, and the remediation path.

## Stuck leases

**Dashboard field:** `stuck_leases` — `work_items` in state `leased` whose
`lease_expires_at` is already in the past.

```js
db.work_items.find({state: "leased", lease_expires_at: {$lt: new Date().toISOString()}})
```

`reconcile_forever` (`control/worker.py::reconcile_leases`, interval
`LEASE_RECONCILE_INTERVAL_S`) already returns an expired lease to `ready`
automatically. A lease that stays in this list across two consecutive
dashboard checks (longer than the reconcile interval) means the
reconciler loop itself has stalled or crashed — check the process log for
`Worker %s: claim loop iteration failed` / an unhandled exception in
`reconcile_forever`, and restart the backend process if the loop is not
running. Never hand-edit `work_items.state` directly: a manual `ready`
flip bypasses the fence, and a worker still legitimately holding the
lease could then race a second claimant.

## Policy denials

**Dashboard field:** `policy_denials` — a count and per-reason breakdown
of `trace_events` whose `outcome` is `budget_exceeded` or whose
`gateway_decision` is `denied`, drawn from the most recent 2000 rows.

```js
db.trace_events.find({outcome: {$in: ["budget_exceeded", "denied"]}}).sort({ts: -1}).limit(50)
```

A steady baseline of `budget_exceeded` denials is expected (D5 bounds are
deliberately tight). A sudden spike, or a `by_reason` key naming a
manifest/model that should never be requesting a given tool, indicates
either a runaway agent loop or an attempted capability escalation —
cross-reference `agent`/`task_id` on the matching rows and, if a genuine
escalation attempt, treat it as a security incident (see
`docs/THREAT_MODEL.md`), not routine noise.

## Gate failures

**Dashboard field:** `gate_failures` — `gate_results` with `status` in
`fail` or `blocked`, newest first.

```js
db.gate_results.find({status: {$in: ["fail", "blocked"]}}).sort({created_at: -1}).limit(50)
```

Each row names the exact gate (`d11_decisions`, `d12_evidence`, etc.) and
`run_id`/`task_id`. Trace the run's full `trace_events` history
(`db.trace_events.find({run_id: ...}).sort({seq: 1})`) to see what
decision or evidence state produced the failure before deciding whether
the run needs a manual `human_review` escalation or was already correctly
routed there.

## Orphan artifacts

**Dashboard field:** `orphan_artifacts` — `artifacts` in state
`deletion_pending`, or carrying `delete_attempts > 0` from a prior failed
sweep.

```js
db.artifacts.find({$or: [{state: "deletion_pending"}, {delete_attempts: {$gt: 0}}]})
```

`ArtifactService.reconcile` (run hourly from `_purge_settled_sessions_loop`
step 4) retries these automatically. A `delete_error` naming a permission
or disk-full condition needs operator action on the underlying volume,
not a code change; once fixed, the next hourly sweep clears the record on
its own confirmation that the bytes are gone. An artifact whose `hold`
field is set is deliberately excluded from this list's automatic
cleanup — check `db.artifacts.find({hold: {$ne: ""}})` separately when
auditing legal holds.

## Erasure failures

**Dashboard field:** `erasure_failures` — sessions with
`status: "erasure_pending"` (a `DELETE /api/sessions/{sid}` call, or the
retention loop's own terminal-state sweep, could not confirm every
filesystem deletion).

```js
db.sessions.find({status: "erasure_pending"}, {id: 1, erasure_error: 1, erasure_attempts: 1, updated_at: 1})
```

Retried automatically every hour (`_purge_settled_sessions_loop` step 3).
`erasure_attempts` climbing without bound alongside the same
`erasure_error` means the underlying filesystem issue needs direct
operator remediation (permissions, a mount that went read-only, disk
full) before the retry can ever succeed. The session document is a
correctness guarantee, not just a display artifact: while it says
`erasure_pending`, do not assume the right-to-erasure request has been
honored.

## Publication outcomes

**Dashboard field:** `publication_outcomes` — the most recent
`publication_pointers` rows, newest first.

```js
db.publication_pointers.find().sort({certified_at: -1}).limit(50)
```

Used to confirm a specific session's export was certified through the
real Publish Guard path (one `PublicationPointer` per generation, fenced
against overwrite by an older generation) rather than through some other
route. A session with a `complete` status but no matching
`publication_pointers` row is a discrepancy worth escalating: something
outside `ArtifactService.certify_publication` marked it complete.

## Review-retention expiry

Not a dashboard field (it is a routine, expected transition, not a
failure): `awaiting_human_review` sessions past `REVIEW_RETENTION_DAYS`
(defaults to `RETENTION_DAYS`) have their raw
`UPLOAD_DIR/<sid>` bytes erased and move to `expired_awaiting_review`.

```js
db.sessions.find({status: "expired_awaiting_review"}, {id: 1, updated_at: 1})
```

To keep a specific paused review's raw PHI past this window (an active
legal or regulatory hold), a `lead_reviewer` calls the hold API before
the window elapses:

```
POST /api/admin/hold   {"session_id": "<sid>", "reason": "<reason>"}
DELETE /api/admin/hold?session_id=<sid>&reason=<reason>
```

Both ends set/clear `hold` on the session's `WorkflowRun` and record a
`hold_set`/`hold_cleared` trace event carrying the principal and reason.
A session with no durable run (a pre-D9 legacy record) has nothing to
hold and the route 409s; fall back to a direct edit in that case only:

```js
db.workflow_runs.update_one({run_id: "<run_id>"}, {$set: {hold: "<reason>"}})
```

Every retention timer in this document — terminal-session purge,
review-retention expiry, and artifact reconciliation — checks this field
and skips the session/artifact entirely while it is non-empty. Clearing
it (via the API, or `$set: {hold: ""}` for the legacy-record fallback)
resumes normal retention.
