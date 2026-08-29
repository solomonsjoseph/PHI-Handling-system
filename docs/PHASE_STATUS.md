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

### Wave R-c (solo): integration — COMPLETE

22 commits across 3 subagent dispatches (the first crashed at the infra level during
research, zero commits, clean retry; the second completed Steps 1-3 and honestly reported
running out of budget; the third completed Steps 4-8). All 8 steps landed in the mandated
order, verified: Step 2 (opaque map encryption) precedes Step 3 (header gate wiring) with no
intermediate commit where the gate is wired and the map still cleartext.

**Step 1:** `AgentContext` (control/context.py) gained `handoff` (always attached),
`sandbox` (attached only when `ActivationFactory.activate(..., needs_sandbox=True)`),
`opaque` and `methods` facades (extension beyond the brief's literal two fields, disclosed:
Steps 3 and 5 structurally require store-backed access to OpaqueMap/MethodRegistry).
`ActivationFactory._claim_and_build` is the single wiring point.

**Step 2 (fixes D5):** `OpaqueMap.to_opaque`/`from_opaque` encrypt/decrypt the stored
canonical value via new `crypto.encrypt_opaque_value`/`decrypt_opaque_value` (thin wrappers
over `encrypt_api_key`/`decrypt_api_key`'s Fernet primitive). Token-generation logic
untouched. `SuperOrchestrator.erase_opaque_map(run_id=...)` added as the erasure capability;
its caller was a gap Wave R-c flagged and the orchestrator closed directly (see below).

**Step 3:** `classify_header` gained a real `uncertain` disposition (ambiguous embedded
digit run, 3-9 chars). `Schema.run` routes every header through `classify_header`;
sensitive/uncertain headers are opaque-projected via `ctx.opaque`, never reaching the
agent-facing output under their literal text. Uncertain headers raise a non-blocking review
item via a trace event (`schema.header_uncertain_review`) — deliberately not
`HumanReviewRequest`, which pauses the run and allows only one open request per run_id, the
wrong tool for a non-blocking per-header flag. Exceeding
`limits.MAX_UNCERTAIN_HEADERS_PER_RUN` raises `UncertainHeaderCeilingExceeded`
(`failure_class="HEADER_SENSITIVE_CONTENT"`), blocking the run. Lexicon's dictionary-row
text and Instrument's Tier-2 form text now route through `source_projection()` before any
provider call.

**Step 4:** Executor's four raw-row-work call sites
(`apply_column_actions_to_dataset`, `_redact_metadata_file`, `_read_dataset_headers`,
`read_narrative`) route through `run_isolated` via new `_sandboxed_*` wrapper functions,
activating only when `ctx.sandbox` is attached. The four functions' own signatures and
direct callability are unchanged (every `make_ctx`-built unit test has `ctx.sandbox=None`
and calls them in-process, a documented permanent compatibility path). `PseudonymRegistry`
state crosses the `multiprocessing.spawn` boundary as plain `(salt, map-dict)` args in, a
workspace-relative JSON artifact filename out.

**Step 5:** `Praxis.method_for`'s existing evidence-verification fallback gate gained a
narrower condition: once D12 verification passes, a category whose
`ctx.methods.get_approved_methods(hipaa_category=...)` returns empty also falls back to the
deterministic method (research still runs and is cached; only the *trusted output* is gated
behind formal approval, per spec section 38).

**Step 6:** Manager's guardian query broker (`ask_schema`/`ask_instrument`/`ask_lexicon`)
records each query as a governed `(Judge, Schema)`/`(Judge, Instrument)`/`(Judge, Lexicon)`
handoff via `ctx.handoff.handoff(...)`; the verdict is recorded to trace but never gates the
broker's already-working return value.

**Step 7:** `ProviderGateway._validate_request`'s two direct `policy.check_provider`/
`policy.check_data_class` calls replaced by one `authorization.authorize_capability(...)`
call. Pure rename, identical composed calls, no security effect.

**Step 8:** `tests/test_control_phaseR_integration.py` has all 5 invariants (10 tests: 5 AST
exclusivity scans each with a positive control, 5 behavioral). Final run: `pytest
tests/test_control_phaseR_integration.py -q -p no:cacheprovider -p no:xdist` -> **20 passed**.

**Two gaps Wave R-c flagged rather than working around, both closed by the orchestrator
directly (small, targeted, no conflict with any subagent's ownership):**
- `erase_opaque_map` had no caller. `server.py`'s `session_delete` route and both erasure
  sites in `_purge_settled_sessions_loop` now call it via a new
  `_erase_opaque_map_best_effort(db, run_id)` helper, tolerating the legacy
  no-durable-WorkflowRun case identically to the existing `cancel_run` handling. A test-stub
  gap this surfaced (`FakeSuperOrchestrator` missing the method; a `_StubDB` missing
  `workflow_runs` item-access) was fixed in `test_production_readiness.py`.
- `PHI_SANDBOX_ALLOW_UNENFORCED_MEMORY` needed a suite-wide decision before Step 4 could
  land safely. `conftest.py` gained a suite-default autouse fixture setting it to `"1"` for
  every test except `test_create_sandbox_fails_closed_when_memory_limit_is_unenforceable_
  without_override`, mirroring R-Sandbox's own local exclusion pattern at the suite level.

**Stale docstrings corrected** (both in Wave-R-b-owned files, closed to R-c subagents,
fixed by the orchestrator directly after confirming the wiring they described was now true):
`control/handoff.py`'s "not wired into `phi_core/agents/` yet" (Step 6 made it wired) and
`control/sandbox.py`'s equivalent claim (Step 4 made it wired).

**One genuine test regression investigated and correctly resolved as a strengthening, not a
bug:** `test_instrument_guardian.py::test_scanned_form_routes_through_shared_read_pdf_and_
scrubs_before_prompt` asserted the pre-R-c behavior where a bare name in scanned-form text
reached the LLM prompt (only rule-detectable PHI was redacted first). Step 3's
`source_projection()` routing applies the same post-scrub residual-content check every
outbound provider payload gets (full presidio+rule, regardless of content type), so a bare
name is now correctly caught and the call blocked entirely. Fixed the test's content to
preserve its original intent (rule-detectable content redacted, call proceeds) and added
`test_scanned_form_with_residual_phi_after_rule_scrub_is_blocked_not_sent` to lock in and
document the new, stricter, correct behavior explicitly.

**Wave-landing check** (server restarted first): `cd backend && .venv/bin/python -m pytest
tests -q -p no:cacheprovider -n auto --deselect tests/test_agent_pipeline.py --deselect
tests/test_ocr_pdf.py --deselect tests/test_corpus_researcher.py` -> **3 failed, 1397
passed, 3 skipped, 1 xfailed, 62.51s**. All 3 remaining failures are the pre-existing
`test_human_review_invariant.py` failures (Phase 8 scope). Investigated and fixed one
environment-state false failure along the way:
`test_security_llm_and_auth.py::test_get_settings_never_returns_api_key_plaintext` returned
409 due to a stale `settings` document (`_id: "llm"`) in the shared long-running test Mongo
instance whose `api_key` predated the current encryption key state (`KeyRotated`, the
intended production behavior for a genuinely rotated key) — cleared the stale document, not
a code fix.

**Nodeid regression check:** 1421 nodeids collected vs 1039 at Step 0 baseline. `comm -23`
against the baseline nodeid set is **empty** — no silent test disappearance.

### Wave R-d (solo): leak-canary harness — COMPLETE

5 commits. New `control/canary.py` (`CanarySet`, process-local, never persisted, populated
from `CorpusArtifact.ground_truth`). `phi_corpus/verify.py` gained `scan_run_surfaces_for_
leaks()` covering the 8 non-export section-72 surfaces (trace_events including status_text,
workflow_runs.opaque_map, agent logs, HandoffEnvelope payloads, learning store, research
queries, errors, ZIP metadata), reusing the existing planted-literal set and leak-hit record
shape from `scan_exports_for_leaks`. `gateway.py`'s outbound-payload path scans against the
active `CanarySet` immediately after the existing scrub/restricted-content checks and before
the provider call: a hit raises `SECURITY_BOUNDARY_VIOLATION` and records
`{canary_scan: "violation", canary_id, hit_count}` on the `TraceEvent`, never the matched
value; a clean scan records `{canary_scan: "clean"}` alongside the existing `egress_digest`.
`ToolGateway.search`'s `query` argument gets the identical treatment.

**Genuine architectural finding, correctly resolved:** `HandoffEnvelope.payload` (a
required Part-1 surface) is never persisted anywhere by the existing, unmodified
`handoff.py`. Resolved by labelling hits on handoff-phase `trace_events` rows as
`surface=handoff_envelope_payload` (the only place such a leak could actually manifest),
plus a dedicated test driving the real `HandoffGateway.handoff` proving the payload
genuinely never persists, rather than building new persistence just to satisfy a test.

Own tests: `tests/test_control_phaseR_canary.py tests/test_phi_corpus_verify_run_surfaces.py`
-> **21 passed**. Re-verified zero regressions across 260 pre-existing tests spanning
gateway/egress, corpus, handoff, and architecture-boundary suites.

**Wave-landing check:** **3 failed, 1418 passed, 3 skipped, 1 xfailed, 63.26s** — same 3
pre-existing `test_human_review_invariant.py` failures, zero regressions. Nodeid check:
1442 collected, `comm -23` against baseline empty.

## Phase R gate — PHASE_R_STATUS = PASS

Full per-phase gate procedure run against `9e633a1` (the commit immediately before Wave
R-a's first commit) as the phase base.

**1-2. Acceptance R, checked against real evidence:**
- Every Phase 1, 2, 3 exit criterion in master-prompt sections 84, 85, 86 is met, including
  "independently tested and integrated": all 25 section-84 contracts typed and field-exact
  (Wave R-a); lifecycle single-sourced; state transitions and versioning/invalidation
  tested; `SandboxManager`, `HeaderSafetyGate`/`SourceProjectionGateway`, `MethodRegistry`,
  `HandoffGateway` all have live call sites with behavioral proof, not just exclusivity
  scans (Wave R-c step 8); artifact lineage invalidation built and tested (Wave R-b
  R-Lineage).
- D1 through D9: D1/D2/D3/D7/D9 fixed with dedicated tests (Wave R-b R-Sandbox); D3 parent
  half and D4 fixed (Wave R-b R-Trace); D5 fixed, opaque map encrypted at rest with a wired
  erasure caller (Wave R-c step 2 plus the orchestrator's `server.py` wiring); D6 disclosed
  as open until Phase 8 (`docs/THREAT_MODEL_BACKEND.md`); D8 fixed (Wave R-b R-Docs).
- The 4 macOS sandbox failures pass (Wave R-b R-Sandbox; confirmed absent from every
  wave-landing check since).
- The canary harness runs and is clean (Wave R-d; 21 passed, zero unexpected hits).

**2a. Test-first ordering check:** every one of the 6 new test files added during Phase R
(`test_control_phaseR_canary.py`, `test_control_phaseR_contracts.py`,
`test_control_phaseR_integration.py`, `test_control_phaseR_lineage.py`,
`test_control_phaseR_trace.py`, `test_phi_corpus_verify_run_surfaces.py`) was created by a
`test(...)`-prefixed commit as its first commit, verified via `git diff --diff-filter=A`.
The full ordered commit log (`git log --reverse`, 70 commits) shows a consistent
test-then-feat/fix pairing throughout; every subagent's report additionally supplied
verbatim RED blocks confirming this per unit of work. No implementation commit precedes its
own test commit.

**3. Full backend suite** (`pytest tests -q -p no:cacheprovider -n auto`, server restarted
first): **8 failed, 1427 passed, 4 skipped, 1 xfailed, 2 errors, 65.02s**. The 8 failed + 2
errors are an **exact match** to Step 0's baseline failure set, minus the 4 sandbox failures
Wave R-b fixed: 5 failed + 2 errors in `test_agent_pipeline.py` (deep pipeline-execution
integration, not attributed to any D1-D9 defect or listed in Phase R's acceptance criteria;
depends on Phase 4's Manager/SuperOrchestrator build-out) and 3 failed in
`test_human_review_invariant.py` (explicitly Phase 8's scope). **Zero new failures beyond
baseline.** The 4th skip (up from 3 in every wave-landing check) is `test_corpus_researcher.py`
skipping for a missing `ANTHROPIC_API_KEY` — that file is deselected in the wave-landing
subset but included in the full gate run; not a new condition.

**4. Lint:** `ruff check .` found 19 errors. 17 were cosmetic (import sorting, unused
imports), auto-fixed. One real finding: `phi_core/models.py`'s `Session` class had a dead
duplicate `status` field (`status: SessionStatus = "created"`) left over from Wave R-c's
lifecycle single-sourcing, which replaced `SessionStatus` with `RunState` but added the new
field instead of editing the old one in place; `from __future__ import annotations` deferred
evaluation meant the undefined-name reference never raised at runtime, but ruff's static AST
check caught it. Removed the dead line; `Session.status: RunState` is now the sole
definition. One `B905 zip() without strict=` in `specialists.py`'s header-projection code:
confirmed the 1:1 length invariant genuinely holds and added `strict=True` to enforce it.
`ruff check .` now reports **All checks passed!**

**5. Root suite** (`PYTHONDONTWRITEBYTECODE=1 backend/.venv/bin/python -m pytest -q -p
no:cacheprovider tests`, from repo root): **85 failed, 909 passed, 3 skipped, 63.75s** —
exact match to the Step 0 baseline (85/909/3), all failures confined to `phi_engine`
(out of scope, never edited), a pre-existing macOS platform gap.

**6. Architectural invariant check:** `test_control_phaseR_integration.py` -> **20 passed**.
Grep for `"not wired into phi_core/agents/ yet"` across `control/` -> **zero hits**.

**6a. Canary scan:** the canary harness's own test suite (21 passed) proves detection on
every surface Phase R introduced and zero false hits on a clean run. No live acceptance
corpus run exists yet to scan in production conditions (that is Phase 20's job); this gate
step is satisfied by the harness's own verified correctness.

**7. Regression rule:** zero silent nodeid disappearance (`comm -23` against the Step 0
baseline is empty across every wave-landing check and this final gate run); no skip
inflation (all 4 skips are either the same 3 already present at every wave landing, plus the
1 additional skip explained above as an artifact of including a previously-deselected file,
not a new condition, and every skip has an inline `pytest.skip`/`skipif` reason string); one
`xfail(strict=True)` exists, recorded in `KNOWN_XFAIL` above with its resolving phase
(Phase 7) and exact reason string.

**PHASE_R_STATUS = PASS.**

**Checkpoint: COMPACT.** Phase R's artifacts (control-plane wiring, the canary harness, the
corrected lifecycle/records contracts) are consumed directly by Phase 4 and beyond;
continuing in this same session.

Next: Phase 4 (Manager / Super Orchestrator), waves 4a then 4b.