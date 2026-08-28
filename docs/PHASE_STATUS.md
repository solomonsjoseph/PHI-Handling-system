# PHI handling system: infrastructure rewrite phase status

This file is the authoritative carried state for the infrastructure rewrite. The
execution plan, the master architecture prompt, and this file are the only state that
survives a context clear. At every phase gate, record exactly one of
`PASS`, `FAIL`, or `BLOCKED` per phase, with real pass, fail, and skip counts.

Durable spec: `docs/MASTER_ARCHITECTURE_V2.md` (gitignored). Execution plan lives in the
session-local plan file and is quoted into `docs/PHASE_STATUS.md` only where a later phase
must consume an interface or decision.

## Standing sections

### KNOWN_XFAIL

Every `xfail(strict=True)` in the suite is listed here with its nodeid, resolving phase,
and exact reason string. A phase may not close with an unrecorded xfail.

- nodeid: `tests/test_control_phaseR_contracts.py::test_judge_output_schema_matches_column_decision_contract`
  resolving phase: Phase 7
  reason: "Phase 7: Judge's output_schema is still the legacy 'judge_decisions' registration; ColumnDecision's typed contract uses 'column_decision'. Both sides of the duplicate-schema debt must move together."
  commit: `c7b16db`

### DELETED_TESTS

Every test removed by design is listed here with its nodeid, the production symbol whose
removal justifies it, and the commit that removed that symbol.

(empty)

### RESIDUAL_RISK

Disclosed architectural limitations that no phase can fix. Each entry points at the
threat model or runbook section that discloses it.

(empty)

## Step 0: durable spec, environment, and baseline

### Completed (prep only, no test runs)

- Master prompt written verbatim to `docs/MASTER_ARCHITECTURE_V2.md` (gitignored, 4690 lines).
- `docs/PHASE_STATUS.md` created and seeded with the baseline table and defect list.
- MongoDB running on `127.0.0.1:27017`, dedicated dbpath
  `/Users/sj1136/.local/share/phi-handling/mongo`, as a persistent hub process `phi-mongo`.
  Note: a separate brew `mongodb-community` service runs on port 55292 and was left untouched.
- Backend server running on `127.0.0.1:8001` (persistent hub process `phi-backend`),
  `DATA_DIR` set to the repo `data/` directory. `/api/health` returns `200` with
  `{"status":"ok","version":"2.0.0"}`.
- Stale environment note in `CLAUDE.md` corrected: `backend/.venv` is Python 3.11.16,
  `numpy==1.26.4`, and `import presidio_analyzer` succeeds.
- Root-suite test deps installed into `backend/.venv`: `faker==40.37.0`, `xlwt==1.3.0`.
  Verified the install did not disturb the `numpy==1.26.4` pin or the Presidio import.

### Completed: baseline test runs

Recorded with MongoDB up on `127.0.0.1:27017` and the backend server up on
`127.0.0.1:8001`, per Step 0 item 4.

**Backend serial** (`cd backend && .venv/bin/python -m pytest tests -q -p no:cacheprovider`):
**12 failed, 1021 passed, 4 skipped, 2 errors, 1039 collected, 75.71 s.** Full failure list:
`docs/baseline/backend-serial-step0-failures.txt`. This is the canonical Step 0 backend
result (see the `-n auto` note below for why).

**Backend `-n auto`, and why it is not the canonical baseline.** Ran twice with identical
inputs. Run 1: 12 failed, 1021 passed, 4 skipped, 2 errors, 57.17 s — matches serial exactly.
Run 2: 16 failed, 2 errors, 60.74 s — three additional failures in
`test_security_paths.py::test_intake_rejects_zip_with_evil_entry_path[...]` (`"rate limit
exceeded; try again later"`) and `test_agent_pipeline.py` failures shifted from
`"pipeline failed"` to `"pipeline capacity exhausted (2 concurrent runs); retry shortly"`.
Root cause: `-n auto` runs multiple workers against the one live backend server, and the
server's own rate limiter and pipeline-concurrency cap (2 concurrent runs) trip
nondeterministically depending on worker scheduling. This is exactly the order-sensitivity
the plan warns about. Per Step 0 item 5, **serial is the canonical baseline of record.**
Per-phase gates still use `-n auto` for speed per the plan's own gate steps; a phase-gate
failure that is one of these two symptoms (`rate limit exceeded`, `pipeline capacity
exhausted`) should be re-run serially before being treated as a regression.

**Nodeid sets** (`--collect-only`, filtered to lines containing `::` to exclude the
pytest summary line, which otherwise pollutes `comm -23` comparisons): backend **1039**
nodeids at `docs/baseline/backend-nodeids.txt`; root **997** nodeids at
`docs/baseline/root-nodeids.txt`. Both committed.

**Root suite** (`cd <repo root> && PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m
pytest tests -q -p no:cacheprovider`): **85 failed, 909 passed, 3 skipped, 997 collected,
58.63 s.** Failures cluster entirely in `phi_engine` (out of scope, never edited): 27 in
`test_xls_isolation.py`, 25 in `test_intake_naming.py`, 15 in `test_intake_manifest_v4.py`,
6 in `test_stress_standalone.py`, 6 in `test_atomic_fs.py`, 5 in `test_intake_preflight.py`,
1 in `test_dataset_first_phase2.py`. Many carry `failure_code=READER_UNAVAILABLE` or
reference `/proc/self/fd`, a Linux-only path, suggesting a macOS-specific gap in an XLS
reader dependency or fd-accounting technique, not something introduced by installing
`faker`/`xlwt`. This is a pre-existing `phi_engine` platform gap; per ground rules
`phi_engine`/root `tests/` are never edited, so this is recorded as the baseline floor,
not fixed. Full failure list: `docs/baseline/root-suite-step0-failures.txt`.

Step 0 is complete. Phase R may begin.

## Verified baseline: phases 0 to 3

Evidence gathered on `feat/phi-infrastructure-v2` at `3bf66e8`.

Measured: `pytest tests/test_control_phase{1,2,3}*.py tests/test_control_records_policy.py
tests/test_control_gateway_egress.py -q` gives **123 passed, 4 failed**. Full backend
collection: **1028 tests**.

| Phase | Status | What is actually true |
|---|---|---|
| 0 | PARTIAL | Instruction inventory complete, no nested `CLAUDE.md` or `AGENTS.md`. `INSTRUCTION_BASELINE_STATUS = ALIGNED` at `docs/PRE_IMPLEMENTATION_AUDIT.md:33` is scoped to `main@dcec23a`, which predates all four rewrite commits. `PRECHECK`/`PHASE_0_STATUS` absent. Branch-migration inventory of `feat/agent-design-docs` absent. Baseline test record is pre-rewrite CI data. |
| 1 | PARTIAL | 12 of 25 contracts typed and field-matching. 4 partial, 9 absent. State transitions tested FAIL. No duplicate competing schemas FAIL: `ColumnDecision` (`records.py:576`, unwired) versus the live `JudgeDecision` (`reasoning.py:49`). Only 2 of 8 new records wired. |
| 2 | PARTIAL | Wired and real: ProviderGateway as sole LLM egress, secret blocking, and the sanitize-then-hash-chain trace stack. Zero call sites: SandboxManager (2A), HeaderSafetyGate and SourceProjectionGateway (2E), MethodRegistry. Absent entirely: artifact lineage invalidation (section 30). |
| 3 | PARTIAL | `HandoffGateway` deny-by-default and correct, 22 tests, 11 of 12 checks, 6 of 7 topology edges. Zero call sites. Both exit criteria unmet. |

## Defects found during review

| ID | Location | Defect |
|---|---|---|
| D1 | `sandbox.py:154-155` | `os.environ.clear()` runs before `_stripped_env()` reads `os.environ`, so it returns `{}`. The credential denylist is dead code. The child also loses `PATH`/`HOME`/`TMPDIR`. |
| D2 | `sandbox.py:189-196` | `proc.join()` precedes `queue.get()`. CPython deadlock: any result larger than the ~64 KiB pipe hangs until the 120 s wall clock, then raises a spurious `SandboxTimeout`. |
| D3 | `sandbox.py:162,198` -> `superorchestrator.py:341-342` -> `events.py:118-135` | The child forwards raw exception text, which embeds the offending cell. `append()` sanitizes `payload` only, so the raw value is SHA-256 chained and streamed over SSE. |
| D4 | `events.py:118-135`, `records.py:525` | `TraceEvent.status_text` is never sanitized and already carries column names. |
| D5 | `opaque.py:34`, `records.py:215`, `superorchestrator.py:555-565` | `OpaqueMap.to_opaque` stores the raw canonical value in cleartext, unencrypted. |
| D6 | `crypto.py:135-152` | `pseudonym_salt` and the signature half of `sign_principal_cookie` are byte-identical for the same input (no domain separator). Cookies carry no issued-at, nonce, or key version. |
| D7 | `_DENYLIST_ENV_FRAGMENTS`, `sandbox.py:53` | Matches `API_KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `CREDENTIAL`, `MONGO_URL`. Misses `APP_ENCRYPTION_KEY`, `ATTESTATION_SIGNING_KEY`, `AWS_ACCESS_KEY_ID`, `DATABASE_URL`, `SSH_AUTH_SOCK`. |
| D8 | `conftest.py:4-5` | Claims tests guard on `MONGO_URL`. False: zero Mongo skip guards exist. |
| D9 | `sandbox.py:82-83` | `mkdir(parents=True, mode=0o700)` applies the mode to the leaf only; the `run_id` intermediate is umask-derived. |

## Phase R: remediation and integration of Phases 1 to 3

### Wave R-a (solo): contracts and shared-file pre-adds — COMPLETE

13 commits (`3de3d3b`..`5b7e24c`), 9 files touched (exactly the owns list). Final targeted
run: `pytest tests/test_control_records_policy.py tests/test_control_workflow.py
tests/test_control_phaseR_contracts.py tests/test_control_evidence.py
tests/test_control_evidence_agents.py -q -p no:cacheprovider -p no:xdist` ->
**325 passed, 1 xfailed**.

**Delivered:** all 9 absent section-84 contracts (FailureClass, EvidenceRecord,
ReviewFinding, HumanReviewPacket, HumanDecision, ExecutionTask, ExecutionResult,
VerificationResult, LearningCase, RunManifest); 4 partial contracts completed (RunState,
AgentContract/AgentManifest, TraceEvent, HumanDecision); lifecycle single-sourced
(`workflow.RUN_LIFECYCLE_STATES` is now the sole source; `models.SessionStatus` replaced by
derived `session_status_display()`); all 7 pre-adds (`SandboxRecord.memory_limit_enforced`,
`SandboxRecord.max_output_bytes`, `HandoffEnvelope.attempt_number`,
`HandoffEnvelope.correction_number`, `limits.MAX_SANDBOX_OUTPUT_BYTES`,
`limits.HANDOFF_ATTEMPT_BUDGET`, `limits.MAX_UNCERTAIN_HEADERS_PER_RUN`); state-transition
tests (34 legal + 226 illegal pairs, both directions); versioning/invalidation tests for all
8 Phase 1 records; `agents/reviewer.py` now constructs typed `ReviewFinding` records.

**Deviations from the brief, resolved in favor of the durable spec:**
- `FailureClass` has **26** members (section 105 text), not the 28 the brief stated.
  Verified twice (manual enumeration + `awk` count over the section).
- `limits.HANDOFF_ATTEMPT_BUDGET`: section 48 names 6 budgeted edge categories with no
  numeric values anywhere in the text. Defaulted every category to
  `limits.MAX_ATTEMPTS_PER_TASK` (3), recorded as a chosen default, not spec-derived.
- `limits.MAX_UNCERTAIN_HEADERS_PER_RUN = 50` and `limits.MAX_SANDBOX_OUTPUT_BYTES =
  104857600` (100 MiB): no numeric value given anywhere in the spec; chosen defaults,
  documented in-line.
- `EvidenceRecord` (section 39, 17-field list) is the registry's public read-model
  contract; `EvidenceSource`/`EvidenceClaim` remain the internal storage/verification split
  in `control/evidence.py`, unchanged.
- `AgentManifest`/`AgentContract`: section 12's 14 names mostly have no literal match in the
  existing 18 `AgentManifest` fields (only `allowed_tools` matched). Added the 13
  non-overlapping names as new fields with safe defaults rather than renaming/merging, to
  avoid breaking `policy.py`'s `MANIFESTS` construction (not touched this wave).
- `TraceEvent`'s "9 missing section 65 fields": section 65 lists 37 names; most already have
  a differently-named equivalent on `TraceEvent` (kept, not renamed, per the ground rule
  against renaming to OpenTelemetry `gen_ai.*` spellings). The 9 genuinely new fields added:
  `agent_role`, `attempt_id`, `event_type`, `input_artifact_refs`, `output_artifact_refs`,
  `tool_call_id`, `decision`, `policy_checks`, `human_review_ref`.

**Wave-landing check** (run by the orchestrator after R-a landed, backend server restarted
first to pick up the model changes):
`cd backend && .venv/bin/python -m pytest tests -q -p no:cacheprovider -n auto --deselect
tests/test_agent_pipeline.py --deselect tests/test_ocr_pdf.py --deselect
tests/test_corpus_researcher.py` -> **7 failed, 1295 passed, 3 skipped, 1 xfailed, 59.97s**.
All 7 failures are pre-existing baseline failures (4 sandbox D1/D2/D9, 3
`test_human_review_invariant.py` client_event_id schema mismatches) — **zero regressions**.
`server.py` imports cleanly with `DATA_DIR` set; the earlier bare `import server` failure in
R-a's own transcript was the pre-existing missing-`DATA_DIR` issue, not a regression.

Next: Wave R-b (5 parallel subagents: R-Sandbox, R-Lineage, R-Trace, R-Handoff, R-Docs).