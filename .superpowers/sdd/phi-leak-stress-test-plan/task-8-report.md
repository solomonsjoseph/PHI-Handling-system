# Task 8 report: certification invalidation

## Scope

Closed the completed-session rerun window without adding public fields or digest schemas.

- `run_pipeline` atomically changes the session to `classifying` and removes `guard_report` plus `export_paths` before its first agent await.
- Export download requires `status == "complete"` before it reads paths, Guard rows, or `force`. Processing requests return 403 and create no override record.
- Bundle download requires `status == "complete"` before it accepts a clean aggregate Guard result.
- Completed clean exports and completed blocked exports using the existing audited `force=true` override retain their focused regression coverage.

## RED

Command, from `backend/` with a writable data directory:

```sh
DATA_DIR=/tmp/phi-task8-data python -m pytest tests/test_certification_invalidation.py -q
```

Before the implementation, 4 tests failed:

- Processing session served a stale clean export with HTTP 200.
- Processing session served a blocked export through `force=true` with HTTP 200.
- Processing session built a bundle from a stale clean aggregate report.
- `run_pipeline` left the completed status, Guard report, and export paths available before agent work.

## GREEN

Command, from `backend/` with a writable data directory:

```sh
DATA_DIR=/tmp/phi-task8-data python -m pytest tests/test_certification_invalidation.py tests/test_security_findings.py -q
```

Result: `22 passed` in 7.00 seconds. This includes a completed clean bundle regression that verifies one bundle build and a processing bundle regression that verifies no build occurs. Two pre-existing FastAPI `on_event` deprecation warnings were emitted.

## Files

- `backend/phi_core/agents/orchestrator.py`
- `backend/server.py`
- `backend/tests/test_certification_invalidation.py`
- `.superpowers/sdd/phi-leak-stress-test-plan/task-8-report.md`

## Commit

`fix: invalidate stale export certification on rerun` (single atomic commit).

## Concerns

The fix intentionally closes the application-level interval only. It does not add a generation or digest binding between emitted bytes and the Guard report, as excluded by the task brief.
