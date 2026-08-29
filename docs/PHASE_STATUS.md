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

### Phase 0 reconfirmation at current HEAD (Wave R-b, R-Docs)

`PRECHECK = PASS`
`INSTRUCTION_BASELINE_STATUS = ALIGNED`, scoped to `feat/phi-infrastructure-v2`
at `eaaa1f4d04a87f0fa95da4a64b30f5b9138dc58c` (`git rev-parse HEAD`, the tip after
Wave R-a landed). This supersedes the row 108 note above: the `main@dcec23a` scope
that note flags as stale is now reconfirmed current, for the reasons below.

Re-audited, not assumed from the stale record:

- `main` at `e2f8d4b` (current tip) differs from the `dcec23a` commit the original
  audit scoped itself to by exactly 4 commits, all from the audit's own PR (`#16`):
  the audit report's own creation plus one clarifying sentence added to `CLAUDE.md`.
  No executable code changed between `dcec23a` and `main`'s current tip.
- `feat/phi-infrastructure-v2` forks directly from `main`'s current tip
  (`git merge-base feat/phi-infrastructure-v2 main` == `main`'s tip == `e2f8d4b`;
  `main` is a confirmed ancestor of `HEAD`), so the audit's Section 2 findings
  (instruction hierarchy) transitively cover everything on this branch except the
  27 rewrite commits added on top.
- A fresh glob for `**/CLAUDE.md`, `**/AGENTS.md`, `**/.cursorrules`, and
  `**/.github/copilot-instructions.md` across the current tree found exactly one
  file: the root `CLAUDE.md`. No nested instruction file exists anywhere under
  `backend/`, `frontend/`, or `phi_engine/` today, matching the original audit's
  Section 2 finding exactly.
- `git diff --stat main..HEAD` touches 40 files; of those, the only one under the
  "instruction file" category is `CLAUDE.md` (56 insertions, 0 deletions). The diff
  is a pure addition (`git diff main..HEAD -- CLAUDE.md`): a new "Migration status"
  subsection describing the in-flight Phase R rewrite and an environment note. It
  introduces no new instruction file, contradicts nothing already in the doc, and
  every claim in it was independently checked against the current tree (agents/
  vs control/ wiring state, the Presidio/numpy environment note, the D8 hang
  description) and found accurate as of this commit.
- `.claude/settings.json` is unchanged (still pins only `{"model": "sonnet"}`, no
  instruction content). `.github/workflows/ci.yml` is unchanged in any way relevant
  to instruction hierarchy.

`PHASE_0_STATUS`: the three items row 108 flagged absent are addressed this wave:
`PRECHECK`/`INSTRUCTION_BASELINE_STATUS` now recorded above at current HEAD; the
branch-migration inventory of `feat/agent-design-docs` is now
`docs/BRANCH_MIGRATION_INVENTORY.md`; the baseline test record is the Step 0 record
above (backend serial, 12 failed/1021 passed/4 skipped/2 errors), gathered on this
branch, not pre-rewrite CI data. Phase 0 is now materially complete, not merely
`PARTIAL`; row 108 above is left unedited as the historical record of what Wave R-a
found, per this file's own convention of never rewriting completed work in place.

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

### Wave R-b (5 parallel): sandbox, lineage, trace, handoff, docs — COMPLETE

37 commits since Wave R-a landed. 18 files touched, all cleanly disjoint across the 5
subagents' owns lists (verified via `git diff --stat`, no collisions).

**R-Sandbox** (13 commits, `control/sandbox.py` +
`test_control_phase2_sandbox_and_raw_data_boundary.py`): fixed D1 (env allowlist replaces
the broken substring denylist), D2 (drain the result queue before joining, fixes the
128 KiB+ payload deadlock), D3 child half (scrub + cap forwarded exception text), D7
(subsumed by the D1 allowlist rewrite), D9 (explicit chmod 0700 on the run_id intermediate
directory), plus the fail-closed memory-limit switch (`PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY`,
never `PHI_ENV`) and a declared return contract on `run_isolated`. All 4 baseline macOS
sandbox failures now pass. Final run: `pytest
tests/test_control_phase2_sandbox_and_raw_data_boundary.py -q -p no:cacheprovider -p
no:xdist` -> **19 passed**.
**Correction to the plan's stated rlimit behavior** (grounded in direct empirical
reproduction on this machine, Darwin 25.6.0): `setrlimit(RLIMIT_AS, ...)` does not
unconditionally raise `ValueError` on Darwin as the plan stated; it raises only when the
target ceiling is below the process's already-mapped virtual address space, which a modern
Python 3.11 process's own shared-library mappings exceed at the configured 1 GiB default.
The enforceability probe (`_probe_memory_limit_enforceable` in `sandbox.py`) tests the real
configured ceiling (`limits.MAX_SANDBOX_MEMORY_BYTES`), not an arbitrary large sentinel, and
touches only the soft limit (the hard limit is a one-way ratchet without elevated privilege).

**R-Lineage** (12 commits, `control/artifacts.py` + new
`test_control_phaseR_lineage.py`): built artifact lineage invalidation (section 30).
`stage()` requires an explicit `parents` list for the 13 section-29 consequential artifact
types. `invalidate_descendants(artifact_id)` walks `parents` forward (cycle-safe,
idempotent, run-scoped), flips descendants to `superseded`, flips any linked
`VerifiedClassificationManifest` to `invalidated`. A read guard in `open_for_download`
refuses a superseded artifact or an invalidated linked manifest. Final run: `pytest
tests/test_control_phaseR_lineage.py tests/test_control_artifacts.py -q -p no:cacheprovider
-p no:xdist` -> **44 passed**.
**Convention future work must follow:** `VerifiedClassificationManifest` has no field
linking it to an `ArtifactRecord`; no code anywhere yet creates one. R-Lineage introduced a
dedicated collection `MANIFEST_COLLECTION = "verified_classification_manifests"`, documents
keyed by `{artifact_id, status, ...}`. Any future code that creates a
`VerifiedClassificationManifest` (likely Phase 9/10) must write into this collection with
that shape for `invalidate_descendants`/`open_for_download` to see it.

**R-Trace** (2 commits, `control/events.py` + `control/trace_sanitizer.py` + new
`test_control_phaseR_trace.py`): fixed D3 store half and D4. `TraceEventStore.append` now
scrubs `status_text` and `retry_category` through the existing `scrub_persisted_text` before
hashing, matching how `payload` was already treated. A planted SSN in either field no longer
survives into the persisted/hashed event. Final run: `pytest tests/test_control_phaseR_trace.py
tests/test_control_events.py -q -p no:cacheprovider -p no:xdist` -> passed (see wave-landing
total below for exact count).

**R-Handoff** (3 commits, `control/handoff.py` +
`test_control_phase3_handoff_gateway.py`): added the missing `(Judge, Reviewer)` topology
edge with a `RevisedArtifactHandoff` schema; implemented check 11 (attempt/correction budget
enforcement); replaced the confused "ALLOWED_EDGES is not mutable" test with three real
tests (single-assignment AST scan, no-request-data-read AST assert, exhaustive 42-pair
matrix); tested `Executor`/raw-worker absence from the role registry.
**Design decision on check 11:** budget denial does not fit any of the 10 existing
`HandoffReasonCode` Literal values (a closed enum in `records.py`, correctly left untouched).
Rather than edit the closed file, check 11 follows the codebase's established D5-ceiling
pattern: raise `policy.BudgetExceeded`, record a `TraceEvent(outcome="budget_exceeded")`,
re-raise. No `HandoffResult` is constructed for a budget refusal; this is a documented
exception carved out of checks 1-10's tuple-return contract.

**R-Docs** (10 commits, `docs/*`, `CLAUDE.md`, `backend/.env.example`, `backend/tests/conftest.py`,
`backend/phi_core/db.py`, plus the explicitly-assigned `test_control_gateway_egress.py`
widening): Phase 0 reconfirmed at current HEAD (`PRECHECK = PASS`,
`INSTRUCTION_BASELINE_STATUS = ALIGNED`, scoped to `eaaa1f4`). New
`docs/BRANCH_MIGRATION_INVENTORY.md`: `feat/agent-design-docs` is an older superseded
snapshot, not an unmerged feature branch; every structural component already exists on
`main` equal-or-better; its deletion of `phi_engine`/`harness` contradicts this rewrite's
out-of-scope rule, so that move is flagged DELETE. New `docs/THREAT_MODEL_BACKEND.md` with
all six required disclosures (macOS memory-limit gap, accurate socket-monkeypatch bypass
list, same-uid filesystem reality, D9 caveat, D6 cookie gap, crypto dev-key orphaning),
each independently re-verified against the live code, not just paraphrased. D8 fixed:
`conftest.py` docstring corrected, `_mongo_up()`/`needs_mongo` skip guard added and applied
to the three Mongo-dependent test files, `serverSelectionTimeoutMS=2000` added to
`phi_core/db.py`. `backend/.env.example` documents the three `TRACE_RAW_*` flags. Final
acceptance run: **19 passed**.

**Wave-landing check** (orchestrator, backend server restarted first to pick up the runtime
changes to sandbox/artifacts/events/handoff/trace_sanitizer/db):
`cd backend && .venv/bin/python -m pytest tests -q -p no:cacheprovider -n auto --deselect
tests/test_agent_pipeline.py --deselect tests/test_ocr_pdf.py --deselect
tests/test_corpus_researcher.py` -> **3 failed, 1376 passed, 3 skipped, 1 xfailed, 63.48s**.
All 3 remaining failures are the pre-existing `test_human_review_invariant.py`
`client_event_id` schema mismatches (out of Phase R scope, deferred to Phase 8). The 4
sandbox failures present at the R-a wave-landing check are now fixed. **Zero regressions.**

**Nodeid regression check** (full corrected comparison, `PHI_TEST_BASE_URL` set to match the
baseline capture conditions): 1400 nodeids collected now vs 1039 at Step 0 baseline. `comm
-23` against the baseline nodeid set is **empty** — no silent test disappearance.

### RESIDUAL_RISK (Phase R-b additions)

See `docs/THREAT_MODEL_BACKEND.md` for full detail. Summary pointers:
- macOS cannot enforce `RLIMIT_AS`; fails closed via `PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY`.
- `socket.socket` monkeypatch stops accidental egress only, not deliberate bypass
  (`_socket`, `importlib.reload`, `subprocess`/`os.system`/`os.execve`, `ctypes`,
  pre-patch unpickling references).
- Same-uid filesystem: the sandboxed worker can read `backend/.env`, `~/.aws/credentials`,
  `~/.ssh/`, service-account tokens.
- D6 (cookie expiry / HMAC domain separation) remains open until Phase 8.
- `crypto.py` dev-key auto-generation orphans existing ciphertext; see
  `docs/RUNBOOK.md`'s new "Encryption-key rotation" section.

Next: Wave R-c (solo): integration.