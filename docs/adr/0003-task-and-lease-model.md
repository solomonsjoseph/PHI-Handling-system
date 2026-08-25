# 0003: CAS-fenced task lifecycle and a durable worker loop

## Status

Accepted (partial: `TaskService` and `control/worker.py` exist and are tested; no production route enqueues through them yet — see Consequences)

## Context

`session_handle` and `session_human_review` (`server.py`) each launch pipeline work as a bare, per-request `asyncio.create_task(worker())` closure with no persisted lease, no fence, and no independent recovery path: a process restart mid-run silently orphans the task (recovered only by a 900-second wall-clock "orphaned by process restart" sweep, `server.py`'s `_startup_maintenance`), and two requests racing the same session have no CAS boundary preventing a double-accepted effect.

## Decision

`backend/phi_core/control/tasks.py::TaskService` owns every `work_items` state and lease transition: `enqueue`, `claim`, `heartbeat`, `complete`, `fail`, `cancel_subtree`, `reconcile_leases`. Every transition made by an already-claimed holder (`heartbeat`, `complete`, `fail`) is one `ControlStore.compare_and_set` call filtered on `lease_owner` and `fence`; `claim`'s filter is `state == "ready"` (the state flip is itself the uniqueness guard against two racing claimants); `complete`/`fail` bump `fence`. A caller whose `lease_owner`/`fence` no longer matches the stored document gets back `TaskOutcome(outcome="fenced")`, never an exception and never a silent no-op that looks like success. `cancel_subtree` walks every `parent_task_id` descendant of a task within a run and best-effort CAS-flips each non-terminal one to `cancelled`, bumping its fence so an in-flight stale holder's next `complete`/`fail` is rejected. `reconcile_leases` returns an expired `leased` task to `ready` (or fails it past `max_attempts`, bumping fence either way), which is the mechanism `docs/adr/0001-workflow-engine.md`'s durable resume needs in place of a wall-clock orphan sweep.

`control/worker.py::Worker` is the claim-and-lease loop: it polls `TaskService.claim` for `ready` work items whose `task_type` has a registered handler, heartbeats while the handler runs, and reports through `complete`/`fail`, discarding a `fenced` outcome silently (a fenced result means a different worker now owns the task). `drain_outbox` reads embedded `OutboxEntry` records and dispatches by `kind` to a handler registry, leaving an entry with no registered handler `pending` rather than dropping or erroring it. A reconciler wraps `TaskService.reconcile_leases` on a fixed `LEASE_RECONCILE_INTERVAL_S`. All three loops are started once from `_startup_maintenance` and each survives an exception in one iteration (log and continue) rather than dying.

## Consequences

- The CAS/fence discipline is real and independently tested (a concurrent `claim` race, a stale-fence `complete` rejection, and lease-expiry reconciliation each have a dedicated test), but no production code path has been switched onto it yet: `session_handle` and `session_human_review` still run their per-request `asyncio.create_task` closures unchanged, and the three new background loops start with an empty handler registry (`OUTBOX_HANDLERS` is `{}`), so `drain_outbox` currently has nothing to actually drain.
- The 900-second orphan sweep in `_startup_maintenance` is unchanged; replacing it with lease reconciliation (keeping a backstop sweep only for pre-migration sessions with no `WorkflowRun`) requires the routes to actually enqueue through `TaskService` first, which has not happened yet.
- This ADR documents the infrastructure decision now so the migration that wires `session_handle`/`session_human_review` onto `TaskService.enqueue` plus the `control/workflow.py` node table (deleting `_run_tail`) has a stable, already-verified foundation to build on, rather than a moving target.
