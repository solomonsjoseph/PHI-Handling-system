# Task 9 report

## Red

`cd backend && python -m pytest tests/test_certification_invalidation.py -q`

Before the implementation, 3 launch tests failed:

- Accepted reruns left `status="complete"` and retained prior certification before the worker ran.
- Two concurrent `POST /api/sessions/{sid}/handle` requests both returned `started`.
- `run_pipeline(..., run_id="old-claim")` was unsupported, so a worker could not be tied to its claim.

The later human-review claim regression also failed before its fix: a stale unresolved review returned `still_awaiting` instead of rejecting its write after a newer tail claim. The terminal-event regression failed because `_emit` did not accept or check a run claim, so stale `complete` and `__end__` events could close a newer stream.

## Green

`cd backend && python -m pytest tests/test_certification_invalidation.py -q`

Result: `12 passed` in 6.76 seconds. Pytest reported 2 existing FastAPI `on_event` deprecation warnings.

Coverage includes immediate normal export, forced export, and bundle rejection after a claimed rerun; concurrent handle requests; stale standard-pipeline completion; atomic human-review tail claim; stale unresolved human-review rejection; stale terminal-event rejection after a newer claim; completed clean download; completed blocked audited force download; and completed clean bundle download.

## Files

- `backend/server.py`
- `backend/phi_core/agents/orchestrator.py`
- `backend/tests/test_certification_invalidation.py`
- `.superpowers/sdd/phi-leak-stress-test-plan/task-9-report.md`

## Commit

Final atomic commit: `fix: atomically claim pipeline launches`.

## Concerns

No broad suite was run, per task constraint.
