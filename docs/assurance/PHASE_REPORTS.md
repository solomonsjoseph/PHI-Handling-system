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
