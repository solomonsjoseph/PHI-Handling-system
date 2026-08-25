# Assurance findings

## F-TEST-001

- Owner: control-plane program
- Code anchors: `backend/requirements-dev.txt:10-13`, `backend/tests/test_corpus_tiers.py:160`, `backend/phi_corpus/planters.py:507-514`, `frontend/src`
- Acceptance tests: Phase 1 gate, `backend/tests/test_full_path.py`, frontend review and stream tests
- Status: open
- Disposition: Phase 1 repairs test collection, deterministic corpus archives, lint configuration, CI coverage, and frontend coverage.
- Residual risk: untested production-provider paths remain credentialed-test work until CI runs them.

## F-EGRESS-001

- Owner: control-plane program
- Code anchors: `backend/phi_core/agents/base.py:140-150,220`, `backend/phi_core/agents/llm.py:113,180`
- Acceptance tests: `backend/tests/test_control_gateway_egress.py`
- Status: open
- Disposition: Phase 2 introduces the policy gateway and keyed digest of the exact outbound payload.
- Residual risk: network-layer enforcement is unavailable in the current deployment.

## F-CAP-001

- Owner: control-plane program
- Code anchors: `backend/phi_core/agents/orchestrator.py:163,180-199`, `backend/phi_core/agents/base.py:115`
- Acceptance tests: `backend/tests/test_control_capability.py`
- Status: open
- Disposition: Phase 2 gives each activation a scoped grant and denies unlisted tools and data classes.
- Residual risk: manifests need continued review as agent roles change.

## F-ART-001

- Owner: control-plane program
- Code anchors: `backend/phi_core/paths.py:74-77`, `backend/phi_core/agents/reasoning.py:1101,1137`, `backend/server.py:881-926`
- Acceptance tests: `backend/tests/test_control_artifacts.py`
- Status: open
- Disposition: Phases 3 and 7 add run-scoped registry-owned artifacts, hash binding, reconciliation, and deletion records.
- Residual risk: legacy exports remain until migration completes.

## F-EVID-001

- Owner: control-plane program
- Code anchors: `backend/phi_core/agents/experts.py`, `backend/phi_core/agents/reasoning.py:1172-1216`
- Acceptance tests: `backend/tests/test_control_evidence.py`, `backend/tests/test_control_decisions.py`
- Status: open
- Disposition: Phases 3 and 6 introduce typed evidence verification and make confidence telemetry only.
- Residual risk: deterministic fallbacks need maintenance when authorities change.

## F-DUR-001

- Owner: control-plane program
- Code anchors: `backend/server.py:1736-1891,2247-2508,340-353,1746-1783`, `backend/phi_core/agents/orchestrator.py:545`
- Acceptance tests: `backend/tests/test_control_durable.py`
- Status: open
- Disposition: Phase 4 replaces detached work with leased, fenced work records and outbox recovery.
- Residual risk: standalone Mongo limits atomicity to a document and embedded outbox.

## F-ORCH-001

- Owner: control-plane program
- Code anchors: `backend/phi_core/agents/orchestrator.py:96-766`, `backend/server.py:1814-1890,2246-2508`, `backend/phi_core/agents/manager.py:311-336`
- Acceptance tests: `backend/tests/test_architecture_boundaries.py`, `backend/tests/test_control_bounds.py`
- Status: open
- Disposition: Phases 4 and 5 install the versioned workflow table and Super Orchestrator as the sole workflow writer.
- Residual risk: every future entry route must retain the command boundary.

## F-HITL-001

- Owner: control-plane program
- Code anchors: `backend/server.py:1929-2023,2374-2421`, `frontend/src/pages/SessionDetail.jsx:635-643,781-808,1128-1131`, `frontend/src/pages/Wizard.jsx:349-393,533-556`
- Acceptance tests: `backend/tests/test_control_review.py`, frontend review tests
- Status: open
- Disposition: Phase 6 persists typed idempotent review events before any provider work and binds them to identity, audit, decision, and delivery versions.
- Residual risk: reviewer policy remains operator-controlled.

## F-OBS-001

- Owner: control-plane program
- Code anchors: `backend/server.py:243-244,308-338,721-766,2511-2548`, `backend/phi_core/agents/base.py:_log`
- Acceptance tests: `backend/tests/test_control_events.py`
- Status: open
- Disposition: Phase 7 replaces session-mixed logs and competing queues with run-scoped trace events and fan-out.
- Residual risk: legacy rows require scrub-or-purge migration.

## F-RET-001

- Owner: control-plane program
- Code anchors: `backend/server.py:1667-1706,549-565`
- Acceptance tests: `backend/tests/test_control_artifacts.py`
- Status: open
- Disposition: Phase 7 makes erasure retryable and covers paused reviews, terminal states, traces, artifacts, and caches.
- Residual risk: policy duration for paused review remains open in F-POLICY-002.

## F-LEARN-001

- Owner: control-plane program
- Code anchors: `backend/phi_core/agents/manager.py`, `backend/phi_core/agents/cache.py:13`
- Acceptance tests: `backend/tests/test_control_learning.py`
- Status: open
- Disposition: Phase 8 adds review-gated proposals, evaluations, activation, monitoring, rollback, and cache versioning.
- Residual risk: learning remains disabled until an authorized activation.

## F-DEP-001

- Owner: control-plane program
- Code anchors: `backend/requirements.txt:9,23,49,51,57,74,93-94,126,128`
- Acceptance tests: `micromamba run -n phi311 pip install --dry-run -r backend/requirements.txt -r backend/requirements-dev.txt`
- Status: resolved
- Disposition: LiteLLM 1.83.9 pins `aiohttp==3.13.3`, `click==8.1.8`, `importlib-metadata==8.5.0`, `jsonschema==4.23.0`, `openai==2.24.0`, `pydantic==2.12.5`, `pydantic-core==2.41.5`, `tiktoken==0.12.0`, and `tokenizers==0.22.2`. `huggingface_hub==1.24.0` required click >= 8.4.2, so it is pinned to 0.28.1, whose metadata has no click dependency.
- Residual risk: `spacy==3.8.0` is yanked upstream for model compatibility. The existing project pin is retained and must be covered by Phase 1 tests.

## F-POLICY-001

- Owner: operator
- Code anchors: `backend/server.py:_refuse_to_boot_insecure`, planned `backend/phi_core/control/review.py`
- Acceptance tests: `backend/tests/test_control_review.py`
- Status: open
- Disposition: Interim reviewer roles come from `REVIEWER_PRINCIPALS=name:role,...`; `reviewer` resolves columns and `lead_reviewer` may supersede reviews, activate learning, and access assurance administration. Outside development, an unset value fails boot.
- Residual risk: operator has not supplied a product authorization model.

## F-POLICY-002

- Owner: operator
- Code anchors: `backend/server.py:1667-1706`
- Acceptance tests: `backend/tests/test_control_artifacts.py::test_awaiting_review_cannot_retain_raw_phi_beyond_policy_without_a_hold`
- Status: open
- Disposition: Interim `REVIEW_RETENTION_DAYS` defaults to configured `RETENTION_DAYS`, currently 30 by default. Phase 7 retention changes do not ship without an operator decision or explicit acceptance of this derived default.
- Residual risk: paused-review retention lacks an operator decision.

## F-POLICY-003

- Owner: operator
- Code anchors: planned `backend/phi_core/control/artifacts.py`, planned `POST /api/admin/hold`
- Acceptance tests: `backend/tests/test_control_artifacts.py::test_hold_suspends_every_retention_timer`
- Status: open
- Disposition: Interim hold authority is limited to `lead_reviewer`; set and clear events include principal and reason in a trace event.
- Residual risk: operator has not defined an alternative legal-hold process.

## F-POLICY-004

- Owner: operator
- Code anchors: `backend/phi_core/agents/base.py`, planned `backend/phi_core/control/policy.py`
- Acceptance tests: `backend/tests/test_control_capability.py::test_restricted_phi_is_never_sendable`
- Status: open
- Disposition: Interim provider egress ceiling is `restricted_metadata`; `restricted_phi` is denied for every manifest. Any widening requires an operator decision and manifest change.
- Residual risk: approved provider terms are not yet recorded.
