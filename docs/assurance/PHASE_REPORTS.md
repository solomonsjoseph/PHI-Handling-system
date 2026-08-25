# Assurance phase reports

## Phase 0: freeze the baseline

### 1. Verified code-backed baseline relevant to this phase, with `path:line` anchors.

- Started from `6a11cfae42f10faf26c186e25cae73ef9735a718` on `feat/agent-design-docs`; `git status --porcelain` was empty. Created `feat/phi-assurance-control-plane` before any edit. `git ls-remote origin refs/heads/feat/agent-design-docs` returned the same SHA.
- Workflow, provider, human-review, artifact, cleanup, cancellation, and seven download anchors are recorded in `docs/assurance/ANCHORS.md`.
- Resolver evidence: the original dependency set could not resolve because LiteLLM 1.83.9 conflicts with the prior pins. Exact conflict and compatible direct pins are recorded under `F-DEP-001` in `docs/assurance/FINDINGS.md`.

### 2. Files and symbols changed.

- `backend/requirements.txt`: resolver-compatible direct pins for LiteLLM 1.83.9.
- `docs/assurance/FINDINGS.md`: baseline finding ledger, dependency finding, and four open product and legal policy findings.
- `docs/assurance/ANCHORS.md`: source-to-claim anchors and control-path inventory.
- `docs/assurance/RISK_REGISTER.md`: Phase 0-9 risks, dependencies, mitigations, and rollback mapping.
- `docs/assurance/PHASE_REPORTS.md`: this report.

### 3. Threat or failure mode addressed, naming the `F-*` finding.

- `F-DEP-001`: no CI-parity environment could be created from the original mutually inconsistent requirements. The repaired pins resolve and install without resolver overrides.
- `F-TEST-001`, `F-EGRESS-001`, `F-CAP-001`, `F-ART-001`, `F-EVID-001`, `F-DUR-001`, `F-ORCH-001`, `F-HITL-001`, `F-OBS-001`, `F-RET-001`, `F-LEARN-001`, and `F-POLICY-001` through `F-POLICY-004` are classified, open, and mapped to closing phases.

### 4. Data and authority boundaries before and after.

- Before and after: runtime data and authority boundaries are unchanged. This phase records the baseline, creates no provider, tool, workflow, artifact, or human-review bypass, and does not claim compliance.

### 5. Migration and rollback behaviour, cross-referenced to `docs/assurance/MIGRATION.md`.

- No schema or data migration occurred. Rollback is a single commit reverting Phase 0 requirements and assurance documents. The Phase 9 migration document will contain tested reverse steps; the plan-to-rollback mapping is in `docs/assurance/RISK_REGISTER.md`.

### 6. Tests added and the exact commands run.

- No tests added in this baseline phase.
- `/home/sj1136/bin/micromamba create -y -n phi311 -c conda-forge python=3.11`
- `/home/sj1136/bin/micromamba run -n phi311 pip install --dry-run -r backend/requirements.txt -r backend/requirements-dev.txt`
- `/home/sj1136/bin/micromamba run -n phi311 pip install -r backend/requirements.txt -r backend/requirements-dev.txt`
- `cd backend && /home/sj1136/bin/micromamba run -n phi311 pytest tests -q --collect-only`
- `cd backend && /home/sj1136/bin/micromamba run -n phi311 python -m pytest tests -q --collect-only`
- `cd backend && /home/sj1136/bin/micromamba run -n phi311 python -m pytest tests --ignore=tests/test_agent_pipeline.py -q -rs`
- `/home/sj1136/bin/micromamba run -n phi311 ruff check --isolated backend`
- `cd frontend && npm ci --ignore-scripts`
- `cd frontend && REACT_APP_BACKEND_URL="" INLINE_RUNTIME_CHUNK="false" npm run build`
- `/home/sj1136/bin/micromamba run -n phi311 python scripts/cleanup.py --all`

### 7. Exact results, including every failure and every skip with its reason.

- Python 3.11.16 environment created. Dry-run and install resolved without overrides after the `F-DEP-001` pin repair. The installed direct pins are: `aiohttp==3.13.3`, `click==8.1.8`, `huggingface_hub==0.28.1`, `importlib_metadata==8.5.0`, `jsonschema==4.23.0`, `openai==2.24.0`, `pydantic==2.12.5`, `pydantic_core==2.41.5`, `tiktoken==0.12.0`, and `tokenizers==0.22.2`.
- Console-script collection failed: 215 tests collected and 33 `ModuleNotFoundError: phi_core` collection errors. `python -m pytest` collected 577 tests in 22.91 seconds. Root cause is the console script omitting the backend current-working directory from `sys.path`; `python -c "import phi_core"` succeeds from `backend`. Phase 1 will make the CI invocation import-safe alongside its pytest configuration.
- Module-invoked suite: 485 passed, 85 failed, 7 skipped, 70 warnings in 79.71 seconds. All 85 failures are `async def` tests marked `pytest.mark.asyncio` without an async pytest plugin. They are classified under `F-TEST-001`. The seven skips are: `ANTHROPIC_API_KEY not set` in `test_corpus_researcher.py:141` and `test_experts_web_search.py:70`; no awaiting-human-review session in `test_human_review_invariant.py:133`; missing `tesseract / pdf2image / poppler` in `test_instrument_guardian.py:190`; missing `tesseract / pdf2image` in `test_ocr_pdf.py:60,72`; and no decisions with reason/citation in `test_security_round2.py:222`.
- Ruff baseline: 305 errors. The plan-recorded categories and count are I001 121, BLE001 65, PLR0402 15, UP045 14, F401 14, UP035 10, UP037 9, S110 9, RUF012 7, RUF059 5, C408 5, PLW1508 4, F821 3, RUF100 2, RET501 2, PLR1711 2, PERF102 2, and one each of UP012, SIM103, SIM102, RUF022, PYI034, PLW1510, PLW0127, PIE810, FURB188, FURB167, F841, F601, F541, C405, C401, B008. Classified under `F-TEST-001`.
- Frontend: `npm ci --ignore-scripts` completed with upstream deprecation warnings. `npm run build` completed successfully. Output sizes: 101.54 kB JS and 5.11 kB CSS gzip. Node emitted `DEP0176` for `fs.F_OK`.
- Cleanup dry run found 48,112 ignored garbage paths, including test caches, `frontend/build`, and `frontend/node_modules`. No cleanup was applied because the installed frontend dependencies are required by the next phase; no ignored path is staged.

### 8. Remaining risk and deferred work, cross-referenced to `docs/assurance/RISK_REGISTER.md`.

- Phase 1 must install and configure an async test runner, prevent silent coroutine passes, fix lint and corpus determinism, make the pytest invocation import-safe, and add required deterministic and frontend tests. See `F-TEST-001` and the Phase 1 row in `docs/assurance/RISK_REGISTER.md`.
- Four product and legal choices remain open with fail-safe interim behavior. See `F-POLICY-001` through `F-POLICY-004` in `docs/assurance/FINDINGS.md`.
- Runtime architecture risks remain unchanged. See `F-EGRESS-001` through `F-LEARN-001` and their phase mappings in `docs/assurance/RISK_REGISTER.md`.

## Phase 1: repair the verification foundation

### 1. Verified code-backed baseline relevant to this phase, with `path:line` anchors.

- `backend/requirements-dev.txt:10-14` now pins `pytest-asyncio==1.4.0`, whose metadata accepts pytest 9.1.1. `backend/pytest.ini:1-4` makes collection strict and adds the backend import root. `backend/phi_core/detectors.py:27,135` rejects Presidio results below 0.3 after a reproduced 0.05-confidence `IN_PAN` false positive redacted "assessment".

### 2. Files and symbols changed.

- Verification configuration, Ruff configuration, CI workflow, corpus archive writer, detector threshold, tests, frontend review tests, and `docs/adr/0008-pytest-9-coroutine-enforcement.md`.

### 3. Threat or failure mode addressed, naming the `F-*` finding.

- `F-TEST-001`: inert async tests, non-reproducible test imports, lint drift, corpus ZIP timestamp nondeterminism, missing full-path and frontend coverage. The Presidio low-score false positive was corrected at its detection boundary without weakening rule detection.

### 4. Data and authority boundaries before and after.

- No provider, workflow, artifact, or reviewer authority changed. Strict test execution and the full-path test make existing boundaries observable. The frontend reconnect only replaces a failed EventSource and does not alter review authority.

### 5. Migration and rollback behaviour, cross-referenced to `docs/assurance/MIGRATION.md`.

- No migration. Revert this phase commit to restore the prior verification setup. The pytest 9 warning-class incompatibility and import-root decision are recorded in `docs/adr/0008-pytest-9-coroutine-enforcement.md`.

### 6. Tests added and the exact commands run.

- Added `backend/tests/test_full_path.py`, offline counterparts in the existing live-state test modules, and `frontend/src/pages/__tests__/SessionDetail.review.test.jsx` plus `SessionDetail.stream.test.jsx`.
- `pip index versions pytest-asyncio`; `ruff check backend`; the 20-iteration `plant(...).zip_bytes` equality command; focused offline and full-path pytest commands; `npm ci --ignore-scripts`; `npm run build`; `npm test -- --watchAll=false`; `pytest tests -q -rs`.

### 7. Exact results, including every failure and every skip with its reason.

- Ruff passes. The deterministic corpus check completed all 20 iterations without output. Focused offline review tests passed 30 with two documented live-state skips. The full-path test passed. Frontend build passed and its two suites, five tests passed. Backend suite passed: 573 passed, 8 skipped, 4 warnings in 93.42 seconds. Skips: no configured live server, no Anthropic API key (two tests), no matching live review session, unavailable OCR binaries (three tests), and no matching live decision state.

### 8. Remaining risk and deferred work, cross-referenced to `docs/assurance/RISK_REGISTER.md`.

- Credentialed provider routing stays an isolated CI capability. Phase 2 replaces direct provider paths and begins policy enforcement. See `F-EGRESS-001`, `F-CAP-001`, and the Phase 2 row in `docs/assurance/RISK_REGISTER.md`.

## Phase 2: core identity, then provider and tool policy

### 1. Verified code-backed baseline relevant to this phase, with `path:line` anchors.

- `backend/phi_core/agents/llm.py:20,113,180` was the only production `litellm.completion` boundary besides `chatgpt_auth.py:34`. `backend/phi_core/agents/base.py:140,145,150,220` logged a scrubbed prompt surrogate distinct from the raw prompt actually sent. `backend/phi_core/agents/base.py:115` defaulted `allow_web_search_escalation=True`. `backend/phi_core/agents/llm.py:134-153` had two silent plain-completion fallbacks for non-Anthropic search. Agents received a shared Motor database handle and `LlmConfig` via `**common` at `orchestrator.py:163,180,185-187,197-199,237-238,517,544,555,581,658,715,723` and a bare `Judge(session_id=..., llm=..., db=...)` in `server.py`.

### 2. Files and symbols changed.

- New package `backend/phi_core/control/` with `records.py`, `limits.py`, `store.py`, `policy.py`, `egress.py`, `opaque.py`, `gateway.py`, `context.py`, `runs.py`, `tasks.py`, `activation.py`, `testing.py`, and `__init__.py`.
- `backend/phi_core/crypto.py`: `egress_digest_key`, `encrypt_display_name`/`decrypt_display_name`.
- `backend/phi_core/agents/llm.py`, `base.py`, `orchestrator.py`, `manager.py`, `experts.py`, `specialists.py`, `reasoning.py`, `outward.py`, `__init__.py`: gateway migration, `AgentContext` construction, opaque filenames.
- `backend/phi_core/bundle.py`: opaque archive member names.
- `backend/phi_core/models.py`: `FileArtifact.original_name_encrypted` replaces `original_name`.
- `backend/phi_core/security.py`: `require_api_token`'s boot exemption now matches `resolve_principal`'s `PHI_ENV=dev`-only exemption (D2/4d).
- `backend/server.py`: opens a real `WorkflowRun` and `WorkItem` per activation via `RunStore`/`ActivationFactory`; opaque per-file identifiers; decrypted display name only for the owner-scoped download.
- `docs/adr/0002-provider-gateway.md`.
- Tests: `test_control_records_policy.py`, `test_control_gateway_egress.py`, and the eleven D8-named plus four additional legacy test files migrated from `db=`/`llm=` construction to `control.testing.make_ctx`.

### 3. Threat or failure mode addressed, naming the `F-*` finding.

- `F-EGRESS-001`: the exact sanitized payload is now what is digested and sent; the separately scrubbed log surrogate is deleted.
- `F-CAP-001`: every activation receives a manifest-derived, deny-by-default `CapabilityGrant` instead of a shared database handle and unconditional web-search escalation.

### 4. Data and authority boundaries before and after.

- Before: any agent could reach `self.db` and any LiteLLM provider through two call sites with no persisted identity. After: agents hold only `AgentContext` (no database handle), every provider call carries a persisted `run_id`/`task_id`/`grant_id` and is revalidated against the grant's pinned provider/model/endpoint triple, `restricted_phi` is refused unconditionally for every manifest, and non-Anthropic `web_search` is denied rather than silently downgraded. Upload filenames are Fernet-encrypted at rest and reach provider prompts, persisted status text, and bundle archive names only as opaque tokens.

### 5. Migration and rollback behaviour, cross-referenced to `docs/assurance/MIGRATION.md`.

- No schema migration; new collections are additive. Rollback is reverting this phase's commit; no Phase 1 data is destructively altered. `FileArtifact.original_name_encrypted` replaces `original_name` in code, not in any persisted document from a prior run (this deployment has never persisted a session), so no backfill is required at this phase. Phase 9's `control/migrate.py` will cover any deployment that already has legacy rows.

### 6. Tests added and the exact commands run.

- `backend/tests/test_control_records_policy.py`, `backend/tests/test_control_gateway_egress.py`.
- `ruff check backend`; `cd backend && pytest tests -q -rs`.

### 7. Exact results, including every failure and every skip with its reason.

- Ruff: clean. Backend suite: `583 passed, 8 skipped` in 81.16 seconds, no warnings. The eight skips are unchanged from Phase 1 (no live server, no Anthropic key for two tests, no matching live review session, unavailable OCR binaries for three tests, no matching live decision state).
- One genuine regression was found and fixed during verification: `orchestrator.py`'s per-category Praxis exception handler referenced a `praxis_agent` variable that no longer existed after each category call became its own `Praxis(await make_ctx("Praxis"))` instance; fixed by logging the failure from the per-category agent instance itself before re-raising.
- Two pre-existing tests (`test_default_bundle_contains_safe_to_share_only`, and the bundle extension fix covering `test_bundle_omits_unclean_and_unreported_exports`) asserted readable original filenames in bundle archive member names. Ruling: the plan's Phase 2 step 4c and D14 mandate opaque archive member names; the tests were updated to assert the opaque `file_id` name instead of reverting the behavior.

### 8. Remaining risk and deferred work, cross-referenced to `docs/assurance/RISK_REGISTER.md`.

- `Manager.escalate_to_human_review` still writes directly to a database handle rather than routing through a workflow authority; Phase 4-5 replace this with `SuperOrchestrator.request_human_review`. See `F-ORCH-001`.
- Typed evidence, canonical decision gates, and artifact staging are not yet in place; Statute/Praxis still trust `reply["sources"]`. See `F-EVID-001`, `F-ART-001`, and the Phase 3 row in `docs/assurance/RISK_REGISTER.md`.

## Phase 3: typed contracts and artifact staging

### 1. Verified code-backed baseline relevant to this phase, with `path:line` anchors.

- `orchestrator.py`'s decide loop and `server.py`'s human-review re-gating each called the bare deterministic functions (`verify_keep_decisions`, `annotate_pending_review`) directly with no proof that Executor would receive exactly one decision per real column. Statute (`experts.py`), Praxis (`experts.py`), Scout (`outward.py`), and `CorpusResearcher` (`phi_corpus/researcher.py`) trusted `reply["sources"]` with no tool-call-citation correlation. `reasoning.py`'s `Executor.run` wrote every export directly under `EXPORT_DIR` keyed by original filename, with no `ArtifactRecord` created before the first byte. `session_export`/`session_bundle`/`session_reversal_key` (`server.py`) served a raw filesystem path with a `force` parameter that could override a blocked Publish Guard verdict.

### 2. Files and symbols changed.

- New: `backend/phi_core/control/{gates,evidence,artifacts,adapters,writer}.py`.
- `backend/phi_core/paths.py`: `STAGING_DIR`, `EVIDENCE_DIR`, `REVERSAL_DIR`, `PUBLISHED_DIR`, `CACHE_DIR`, `run_scoped_dir`, `artifact_id_from_export_alias`.
- `backend/phi_core/agents/orchestrator.py`: final decision-mutation event routed through `run_decision_gates`; `gate_results` persisted.
- `backend/phi_core/agents/reasoning.py`: `Executor.run` rewritten onto `ArtifactWriter.stage`/`finalize`, including the atomic narrative-export path and the suffix-alias hard link.
- `backend/phi_core/agents/experts.py`, `outward.py`, `phi_corpus/researcher.py`: `EvidenceClaim`/`EvidenceSource` verification via `control/evidence.py`, replacing direct `reply["sources"]` trust.
- `backend/phi_core/control/testing.py`, `activation.py`: `ArtifactWriter` wired into every production and test `AgentContext`.
- `backend/server.py`: human-review re-gating routed through `run_decision_gates`; `session_export`/`session_bundle`/`session_reversal_key` rewritten onto `ArtifactService.open_for_download`; `force`/`guard_overrides` deleted.
- `backend/phi_core/publish_guard.py`: `GuardResult.artifact_id`/`sha256`; `scan_names` (Presidio PERSON, HIPAA category A) wired into CSV/TSV/XLSX (per-cell) and TXT/MD (whole-text).
- `backend/phi_core/agents/reasoning.py`: `validate_decisions` no longer nulls `suggested_action`/`suggested_confidence`/`suggested_reason` for a non-`human_review` decision, so the fixed D11 gate sequence is idempotent when re-run over an already-settled decision list without erasing a deterministic override's provenance (see finding below).
- `docs/adr/0004-artifact-registry.md`, `docs/adr/0005-evidence-model.md`.
- Tests: `test_control_{decisions,evidence,artifacts,adapters,writer,evidence_agents}.py`, `test_download_artifact_binding.py`, plus fixture repairs across `test_manager.py`, `test_manager_checkpoints.py`, `test_operator.py`, `test_human_review_invariant.py`, `test_publish_guard.py`, `test_security_findings.py`, `test_certification_invalidation.py`, `test_hardening_gates.py`, `test_narrative_export.py`, `test_realworld_file_shapes.py`, `test_full_path.py`.

### 3. Threat or failure mode addressed, naming the `F-*` finding.

- `F-EVID-001`: Statute/Praxis/Scout/`CorpusResearcher` no longer trust an unverified model-reported source; a claim reaches `VERIFIED` only through `control/evidence.py`'s tool-backed, five-dimension rule.
- `F-ART-001`: every material Executor output is a hash-tracked `ArtifactRecord` staged before its first byte; a mid-write crash leaves no promotable partial file.

### 4. Data and authority boundaries before and after.

- Before: Executor wrote directly to a shared export directory keyed by original filename; a download route trusted that raw path, and `force=true` could serve a file Publish Guard had blocked. After: every export is staged under a run-scoped artifact root, registered before the first byte, and served only through `ArtifactService.open_for_download`, which refuses on state, generation, or on-disk hash mismatch. `force`/`guard_overrides` are structurally absent from the codebase (`inspect.signature` has no such parameter on any download/publish route). A duplicate, missing, or invented decision can no longer reach Executor: `assert_exact_coverage` proves exact per-column coverage immediately before execute, in both the orchestrator decide loop and the server.py human-review re-gating path, and raises `DecisionGateFailure` (propagating to the existing generic exception handler) on any violation.

### 5. Migration and rollback behaviour, cross-referenced to `docs/assurance/MIGRATION.md`.

- No schema migration; `ArtifactRecord`/`EvidenceClaim`/`EvidenceSource`/`GateResult` collections are additive. Rollback is reverting this phase's commit. `control/adapters.py::legacy_decision_adapter`/`legacy_files_adapter` remain the migration seam for any call site not yet routed onto `run_decision_gates` directly, tracked as `F-ADAPT-001`; Phase 5 removes them once every caller has migrated.

### 6. Tests added and the exact commands run.

- New modules listed above, plus fixture repairs. `ruff check backend`; `cd backend && pytest tests -q -rs`; `pytest tests/test_full_path.py -q` repeated 5 times against a fresh `DATA_DIR` to rule out Presidio-driven flakiness in `scan_names`.

### 7. Exact results, including every failure and every skip with its reason.

- Ruff: clean. Backend suite: `662 passed, 8 skipped` in 110 seconds, no warnings beyond the pre-existing `on_event` deprecation notice. Skips unchanged from Phase 1/2 (no live server, no Anthropic key for two tests, no matching live review session, unavailable OCR binaries for three tests, no matching live decision state).
- Genuine regressions found and fixed during full-suite verification (the two Phase 3 sub-dispatches had verified only curated test subsets, not the full suite, before this integration pass):
  - `validate_decisions` unconditionally nulled `suggested_action` for any non-`human_review` decision. `run_decision_gates` re-runs the fixed D11 sequence, including `validate_decisions`, over an already-settled decision list; this silently erased the site-cardinality/hard-rule override provenance a first pass had legitimately set. Fixed by leaving those fields untouched when the action is not `human_review`, restoring idempotency (`test_cardinality_rule.py::test_pipeline_fires_before_sentinel_and_after_age_dob`).
  - Five test fixtures across `test_manager.py`, `test_manager_checkpoints.py`, and `test_operator.py` constructed a `session["files"]` entry with no `columns` key, which production code always populates before `run_pipeline` (`server.py:1979-1991`). Against the new coverage gate this made every dataset file schema-unreadable, changing which decisions synthesized and which phase the run reached. Fixed by adding the `columns` list each fixture's own `FakeJudge` decisions already implied.
  - `test_human_review_invariant.py::test_human_review_captures_session_review_offline` used a `SimpleNamespace`-based fake db with no collection-subscript support; `server.py`'s re-gating now persists `gate_results` via `MongoControlStore(db)`, which needs `db["gate_results"]`. Fixed by extending the fake db with a minimal collection double, matching real `AsyncIOMotorDatabase` semantics, and adding a matching `files` entry for the new coverage proof.
  - `test_operator.py::test_run_pipeline_reviewer_only_finding_excludes_file_and_ends_partially_complete` deliberately fed Judge two decisions for the same `(file_id, column)` to exercise Reviewer's own downstream coverage-mismatch recount. `assert_exact_coverage` now refuses that duplicate before Executor ever runs, making the original scenario unreachable through `orchestrator.run_pipeline`. Renamed to `test_run_pipeline_duplicate_judge_decision_fails_closed_before_executor` and rewritten to assert the new invariant (`DecisionGateFailure`, Executor never invoked); Reviewer's own coverage-mismatch detection remains directly covered, unaffected, by `test_reviewer.py::test_coverage_mismatch_when_zero_fail_verdicts_but_column_count_differs`.

### 8. Remaining risk and deferred work, cross-referenced to `docs/assurance/RISK_REGISTER.md`.

- `decision_version` is stamped `0` on every `GateResult`: nothing yet opens a `WorkflowRun` for a session, so `_next_decision_version`'s CAS increment has no durable counter to key off. Phase 4/5's `SuperOrchestrator`/`RunStore.open_run` wiring closes this gap.
- Publish Guard and `bundle.py` still read a raw, suffix-bearing filesystem path (Executor's hard-link alias) rather than the artifact registry directly; tracked in `docs/adr/0004-artifact-registry.md`.
- `Manager.escalate_to_human_review` still writes directly to a database handle rather than routing through a workflow authority. See `F-ORCH-001` and the Phase 4 row in `docs/assurance/RISK_REGISTER.md`.

## Phase 4 (in progress): durable task service, workflow node table, worker loop

Steps 1-7 of the plan's ten-step Phase 4 are landed and verified, plus step 2's production wiring (`session_handle`/`session_human_review` now enqueue through `TaskService` and are executed by an N-worker claim loop instead of a bare per-request `asyncio.create_task`). Step 5's remainder (Scout as a durable `TaskService` child task, blocked on `AgentContext` not yet carrying a lease fence), step 9 (`HumanReviewService`/`TraceEventStore` fencing, blocked on those Phase 6/7 components not existing yet), and step 10 (boundary kill tests, blocked on `OUTBOX_HANDLERS` having no registered handler to kill a process against) remain open; see item 8 below.

### 1. Verified code-backed baseline relevant to this phase, with `path:line` anchors.

- `server.py`'s `session_handle` (`~1882-2053`) and `session_human_review`'s `_run_tail` (`~2470-2738`) each launch a bare `asyncio.create_task(worker())` closure with no persisted lease or fence; a process restart mid-run is recovered only by the 900-second `_startup_maintenance` orphan sweep. `control/tasks.py` had no `TaskService`; `control/workflow.py` did not exist.

### 2. Files and symbols changed.

- New: `backend/phi_core/control/tasks.py::TaskService` (`enqueue`, `claim`, `heartbeat`, `complete`, `fail`, `cancel_subtree`, `reconcile_leases`), `backend/phi_core/control/workflow.py` (D9 node table, `TRANSITIONS`, `next_node`, `Checkpoint`, `resume_node`), `backend/phi_core/control/worker.py` (`Worker`, `drain_outbox`, lease reconciler).
- `backend/server.py`: `_startup_maintenance` starts the three new background loops; no route changed.
- `backend/phi_core/publish_guard.py`: `_is_opaque_generated_token` exemption (see finding below); unrelated to the durability work but found and fixed during this checkpoint's full-suite verification.
- `docs/adr/0001-workflow-engine.md`, `docs/adr/0003-task-and-lease-model.md`.
- Tests: `test_control_tasks.py`, `test_control_workflow.py`, `test_control_worker.py`.

### 3. Threat or failure mode addressed, naming the `F-*` finding.

- `F-DUR-001` (partial): the CAS/fence primitives a durable, restart-safe task lifecycle needs now exist and are independently tested (concurrent-claim race, stale-fence rejection, lease-expiry reconciliation). No production route uses them yet, so the finding is not closed.

### 4. Data and authority boundaries before and after.

- Unchanged in production: `session_handle`/`session_human_review` still run pipeline work exactly as before. The new `TaskService`/`Worker`/`workflow.py` are additive infrastructure with no caller in this phase's traffic path.

### 5. Migration and rollback behaviour, cross-referenced to `docs/assurance/MIGRATION.md`.

- No schema migration; `work_items` CAS fields and the three new background loops are additive and inert (the loops start with an empty `OUTBOX_HANDLERS` registry and nothing enqueues a `work_items` row of a type any handler recognizes). Rollback is reverting this commit.

### 6. Tests added and the exact commands run.

- `test_control_tasks.py`, `test_control_workflow.py`, `test_control_worker.py`, plus a `test_publish_guard.py` regression test for the flake fix below. `ruff check backend`; `cd backend && pytest tests -q -rs`; `pytest tests/test_full_path.py -q` repeated 25 times against a fresh `DATA_DIR` to confirm the flake is gone.

### 7. Exact results, including every failure and every skip with its reason.

- Ruff: clean. Backend suite: `717 passed, 8 skipped` in 117 seconds. Skips unchanged from prior phases.
- A genuine, reproducible flake was found during full-suite verification and is unrelated to this phase's own durability work: `test_full_path.py::test_planted_corpus_full_path_uses_real_safety_components` failed roughly one run in three. Root cause: `PseudonymRegistry.get`/`.digest` (`reasoning.py`) emit short, one-way hex tokens (`P` + 8 hex chars, or 16 bare hex chars) derived from a random per-study salt; Presidio's PERSON NER occasionally misclassified one of these as a name, and since the token's exact content varies run to run, the false positive was non-deterministic. Fixed by exempting a cell whose entire stripped content matches either token shape from `publish_guard.py`'s per-cell name scan (`_scan_csv_names`, `_scan_xlsx`) before it reaches Presidio: the shape is generated entirely by this codebase's own one-way cryptographic output and can never contain a real name, so skipping it is not a detection weakening. Verified with 25 consecutive clean runs after the fix (0 failures, versus roughly 5 failures in the prior 15-run sample) and two dedicated regression tests (`test_scan_names_exempts_pseudonymize_and_hash_token_shapes`, `test_is_opaque_generated_token_matches_registry_shapes_only`).

### 8. Remaining risk and deferred work, cross-referenced to `docs/assurance/RISK_REGISTER.md`.

- Since the checkpoint above, steps 6 and 7 also landed: `session_handle` refuses with `409 error="reintake_required"` when any `FileArtifact.stored_path` is missing or no longer re-hashes to its recorded `sha256` (`_validate_rerun_inputs`); `_fail_session_correlated` only calls `cleanup_session_unpacked` when its own run-filtered `update_one` actually matched a document; `session_delete` tombstones the session before deleting anything, so `ArtifactService.stage` refuses for that session from that point on, then erases every registered `artifacts`/`publication_pointers` record and on-disk artifact directory. `control/worker.py::drain_outbox` now moves an entry past `MAX_ATTEMPTS_PER_TASK` to an `outbox_dead_letters` record instead of retrying forever in place (step 10, partial). `ArtifactService.certify_publication`'s `fence` parameter (step 9's third fencing target) was already built in Phase 3; step 9's other two targets, `HumanReviewService.submit` and `TraceEventStore.append`, do not exist until Phases 6 and 7 respectively and cannot be fenced before then.
- Since that update, step 4's core deliverable also landed: `phi_core.agents.orchestrator.execute_decisions` extracts the shared Executor-through-Herald tail (previously duplicated as `run_pipeline`'s own body and `server.py`'s ~270-line `_run_tail` closure) into one function. `run_pipeline` calls it from the `gate_decisions` "proceed" outcome; `session_human_review`'s resume worker calls the identical function from the `human_review_decisions` "resolved" outcome, passing `omit_by_file` for any column still deferred. `_run_tail` is deleted. The extraction inherited every defensive behavior the fresh path already had that resume previously lacked: `try/except` wrapping around Executor/Operator/Reviewer/Auditor, `_check_cancel` calls, Manager `consult()` checkpoints, and `manager_report`/full completion-field parity. Two real design corrections were required and verified, not just implemented: (1) `final_status` cannot be computed from `decisions` containing `human_review` entries, since the resume caller pre-filters those out before calling the shared function; it is computed from `bool(omit_by_file)` instead, which is empty on the fresh path and non-empty exactly when a resume left a column deferred. (2) the bespoke `confirm_auditor_confidence` override `_run_tail` used to honor has no fresh-path equivalent and is deleted per the plan's explicit instruction (`HumanReviewSubmit.confirm_auditor_confidence` stays on the wire schema, inert, pending Phase 6's D13 step 5 replacement). A new end-to-end test (`test_human_review_resume_execution.py`) drives the resume worker's background task to completion against a full Motor-shaped fake db and proves both a fully-resolved resume (`status="complete"`, Ledger/Herald output present) and a partially-resolved resume (`status="partially_complete"`, `omit_by_file` correctly excludes only the deferred column): the first tests in the suite to actually execute this path rather than only its synchronous decision-resolution half.
- Since that update, part of step 5 also landed: `execute_decisions`'s three exit paths that leave Scout's background `asyncio.create_task` running (Publish Guard blocked, the operator cancel/`PipelineCancelled` branch, and the coverage-advice escalation branch) now cancel and await it through a shared `_cancel_and_await` helper before returning, rather than firing `scout_task.cancel()` without ever observing the cancellation land. The coverage-advice escalation branch previously did not cancel Scout at all, matching the "Scout leak" the plan names at the pre-refactor `orchestrator.py:545`. A new regression test (`test_manager.py::test_coverage_escalation_fences_scouts_background_task`) captures Scout's real `asyncio.Task` via a `create_task` interception and asserts it is `.done()` and `.cancelled()` after `run_pipeline` returns. Full `TaskService`-lifecycle management of Scout as a durable child `WorkItem` (the rest of step 5's "Scout becomes a durable child task") still requires `AgentContext` to carry the claimed lease `fence`, which it does not yet.
- `cancel_subtree` (part of step 7's "await or fence children") was not yet meaningfully callable from `session_delete` as of the prior checkpoint below: no production route enqueued pipeline work through `TaskService`, so no `WorkItem` tree existed to cancel. The tombstone-then-erase ordering already implemented is what actually prevents resurrection today. See the following entry: `session_handle`/`session_human_review` now enqueue through `TaskService`, so a `WorkItem` tree exists for `cancel_subtree` to walk, though `session_delete` itself does not yet call it (recursive cancellation on delete remains a step-7 gap, tracked in `docs/assurance/RISK_REGISTER.md`).
- Since that update, the remainder of step 2 and step 8 landed: `session_handle` and `session_human_review` now call `TaskService.enqueue` and return `{"status": "started"|"resuming"}` rather than launching a bare `asyncio.create_task`; `_startup_maintenance` starts `_MAX_CONCURRENT_PIPELINES` `Worker` instances (registered for `pipeline_run`/`pipeline_resume`) in place of the single always-inert loop from the earlier checkpoint. `_admit_pipeline_run`'s in-process concurrency cap and immediate-429 contract are unchanged, checked in the route before enqueue; `_release_pipeline_run` moved to the handlers' own `finally` block so the slot frees when the pipeline genuinely finishes, not when the HTTP response returns. `control/policy.py::MANIFESTS` gained a `"Pipeline"` role for the enqueued unit itself. A crashed run now recovers automatically once its lease expires (`reconcile_leases` + the next `Worker` poll re-run the handler), rather than only through the 900-second orphan sweep; that sweep is kept, unmodified, as the backstop for a `WorkItem` that exhausts every retry attempt (`F-DUR-001`'s residual risk) and for any pre-migration session with no `WorkflowRun`. `docs/adr/0003-task-and-lease-model.md` records this decision and its consequences. Phase 4 steps 5 (Scout `TaskService`-lifecycle), 9 (`HumanReviewService`/`TraceEventStore` fencing), and 10 (boundary kill tests) remain open, each blocked on a component from a later phase; see `F-DUR-001`, `F-ORCH-001`, and the Phase 4 row in `docs/assurance/RISK_REGISTER.md`.

## Phase 5 (in progress): Super Orchestrator and bounded delegation

Phase 5 steps 1 and 3 are landed and verified; step 2 has its first production entry-path slice (`session_handle`), step 6 is complete, and steps 4, 5, 7-9 remain open. `ArtifactService.certify_publication`/`PublicationPointer` (step 3) was already production-called from `server.py:1197` and tested in `test_control_artifacts.py`; `control/policy.py::TEAMS` (step 6) already had the exact five required groups, and this phase added ADR 0007 plus an exact-partition test. This entry checkpoints the verified work before the remaining route and delegation migrations.

### 1. Verified code-backed baseline relevant to this phase, with `path:line` anchors.

- `control/workflow.py`'s D9 node table and `TRANSITIONS` map (built in Phase 4) were fully tested but had no production caller: `workflow_runs.node` was stamped `"charter"` at creation (`control/runs.py::RunStore.open_run`, called from `server.py:2323`) and never advanced anywhere. `Manager.escalate_to_human_review` (`manager.py:322-336`) still writes human-review state directly; `orchestrator.py` calls it from four sites (`:173`, `:272`, `:380`, `:913`). `control/superorchestrator.py` did not exist.

### 2. Files and symbols changed.

- New: `backend/phi_core/control/superorchestrator.py::SuperOrchestrator` (`start_run`, `cancel_run`, `advance`, `create_child_work`, `request_human_review`, `consume_review_event`, `accept_result`, `recover`, `authorize_publication`, `terminal_outcome`).
- `backend/phi_core/control/tasks.py`: added a read-only `TaskService.policy` property so `create_child_work` can re-validate a parent's grant without a second, separately constructed `CapabilityPolicy`.
- `docs/adr/0006-super-orchestrator.md`.
- Tests: `test_control_superorchestrator.py` (26 tests, one or more per method plus refusal paths).

### 3. Threat or failure mode addressed, naming the `F-*` finding.

- `F-ORCH-001` (partial): the exclusive-authority primitive D9 requires now exists and is independently tested (CAS-fenced node transitions that fail closed on an unmodelled outcome, budget/depth/fanout-checked child delegation, acceptance that a child cannot self-grant). No production route uses it yet, so the finding is not closed.

### 4. Data and authority boundaries before and after.

- Unchanged in production: every entry route still opens/advances workflow state exactly as before (`RunStore.open_run` directly; `Manager.escalate_to_human_review` directly; no route calls `TaskService.enqueue` for a child task). `SuperOrchestrator` is additive infrastructure with no caller in this phase's traffic path, matching Phase 4's own first checkpoint.

### 5. Migration and rollback behaviour, cross-referenced to `docs/assurance/MIGRATION.md`.

- No schema migration. `TaskService.policy` is a pure read-only property addition. Rollback is reverting this commit.

### 6. Tests added and the exact commands run.

- `test_control_superorchestrator.py`. `ruff check backend`; `cd backend && DATA_DIR=<fresh tmp dir> pytest tests -q -rs`.

### 7. Exact results, including every failure and every skip with its reason.

- Ruff: clean. Backend suite: `760 passed, 8 skipped` in 142 seconds (734 from the Phase 4 checkpoint plus 26 new). Skips unchanged from prior phases.

### 8. Remaining risk and deferred work, cross-referenced to `docs/assurance/RISK_REGISTER.md`.

- Step 2 is partial. `session_handle` retains its existing atomic session claim, passes that claimed `run_id` to `SuperOrchestrator.start_run`, and no longer calls `RunStore.open_run` or `TaskService.enqueue` directly; the orchestrator creates the matching `WorkflowRun` and durable `pipeline_run` root with `input_ref={"run_type": "study"}`. `session_human_review` reuses the existing run id (or creates a durable record under the legacy run token) and submits the `pipeline_resume` root through the same authority. `session_cancel` mirrors its session cancellation signal into `SuperOrchestrator.cancel_run`, and `session_delete` tombstones first, then uses it to fence root and descendant work before erasing artifacts. `session_intake`, corpus paths, warmup, and startup recovery remain direct.
- Step 4 is complete: `Manager.escalate_to_human_review` is deleted (D10). All four `orchestrator.py` callers, plus `run_pipeline`'s decide-loop escalation, now call a shared `orchestrator._escalate_to_human_review` helper: it persists the session document itself (Manager keeps `close_run`'s report; its `_escalation` bookkeeping is now set by the caller), then calls `SuperOrchestrator.request_human_review` at the exact D9 node the deleted method's call site modelled -- `"human_review_decisions"` for the executor-crash and decide-loop paths, `"human_review_audit"` for the reviewer- and auditor-stage paths. Tolerates a missing `store`/unknown `run_id` (test doubles and any not-yet-migrated caller): the session-document write is the tested, load-bearing contract; the durable request is additive. `test_manager.py`'s three escalation tests were rewritten to assert against the session-document write and return shape directly, since `Manager` no longer owns that call.
- Step 5 (Ledger/Herald as durable `create_child_work` children) is not started; it needs a `MANIFESTS` change (a non-empty `allowed_child_task_types`) that does not exist yet -- confirmed every current manifest entry has `allowed_child_task_types=frozenset()`, so any `create_child_work` call in production would currently refuse.
- Step 6 is complete: `TEAMS` has ADR 0007 and `test_control_bounds.py`'s exact-partition contract.
- Step 9 is complete: `control/adapters.py` and its dedicated shim tests are deleted. Both live `run_decision_gates` callers now receive the typed decision list and hydrated session-file projection they already hold, with no conversion layer; `F-ADAPT-001` is resolved. Steps 7 (the remaining D5 resource-ceiling tests) and 8 (`test_architecture_boundaries.py`) remain open. See `F-ORCH-001`, `F-DUR-001`, and the Phase 5 row in `docs/assurance/RISK_REGISTER.md`.