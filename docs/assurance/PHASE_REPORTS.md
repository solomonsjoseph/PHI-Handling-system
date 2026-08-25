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
