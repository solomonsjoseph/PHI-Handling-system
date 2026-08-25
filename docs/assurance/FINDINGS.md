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
- Code anchors: `backend/server.py::session_handle,session_human_review,_startup_maintenance`, `backend/phi_core/control/tasks.py::TaskService`, `backend/phi_core/control/worker.py::Worker`
- Acceptance tests: `backend/tests/test_control_tasks.py`, `backend/tests/test_control_workflow.py`, `backend/tests/test_control_worker.py`, `backend/tests/test_human_review_resume_execution.py`
- Status: open
- Disposition: Phase 4 lands leased, fenced `WorkItem` records, a CAS-safe `TaskService`, and an N-worker claim loop; `session_handle`/`session_human_review` now enqueue through it instead of a bare per-request `asyncio.create_task`. A crashed run's lease expires and `reconcile_leases` returns it to `ready` for automatic re-execution, replacing the previous "wait 900 seconds, then require a manual resubmission" recovery path for that case; the 900-second sweep remains as the backstop for a `WorkItem` that exhausts every retry attempt.
- Residual risk: standalone Mongo limits atomicity to a document and embedded outbox; `control/worker.py`'s `OUTBOX_HANDLERS` registry is still empty (nothing yet produces an `OutboxEntry`), Scout is not yet a `TaskService`-lifecycle child task, and the boundary kill tests proving recovery survives a process death mid-transition are not written.

## F-ORCH-001

- Owner: control-plane program
- Code anchors: `backend/phi_core/agents/orchestrator.py::_escalate_to_human_review,run_pipeline,execute_decisions`, `backend/server.py::session_handle,session_human_review,session_cancel,session_delete,corpus_study_research,_run_warmup`, `backend/phi_core/control/superorchestrator.py::SuperOrchestrator,create_child_work,accept_result`, `backend/phi_core/control/activation.py::ActivationFactory.activate_child,complete_and_accept`, `backend/phi_core/agents/base.py::Agent.__init_subclass__`, `backend/phi_core/control/runs.py::check_run_budget,record_run_usage`
- Acceptance tests: `backend/tests/test_control_superorchestrator.py`, `backend/tests/test_certification_invalidation.py`, `backend/tests/test_manager.py`, `backend/tests/test_production_readiness.py`, `backend/tests/test_control_bounds.py`, `backend/tests/test_architecture_boundaries.py`, `backend/tests/test_operator.py`, `backend/tests/test_human_review_resume_execution.py`
- Status: resolved
- Disposition: Phase 5 is complete. `SuperOrchestrator` is the D9 exclusive-authority class (fenced node transitions, run-wide/parallel/fanout/depth/budget-widen-checked child delegation, review request/consume, acceptance); `session_handle`/`session_human_review`/`session_cancel`/`session_delete`/`corpus_study_research`/`settings_warmup` (`_run_warmup`, shared with `_warmup_scheduler_loop`) all open a durable `WorkflowRun` through it before doing provider or workflow work. `corpus_study_run` delegates to `session_handle` directly. `session_intake` and `corpus_study_generate` do no provider/workflow work (pure file/DB I/O) and are correctly out of this step's scope by its own qualifier. `Manager.escalate_to_human_review` is deleted (D10); every former caller, plus `run_pipeline`'s decide-loop escalation, calls a shared `orchestrator._escalate_to_human_review` helper that persists the session document and then calls `SuperOrchestrator.request_human_review` at the exact D9 node each site models. Ledger's Compare/Aggregate and Herald's Abstract/Sections are created via `create_child_work` under their parent's task, with `accept_result` as the acceptance authority for each child's material result -- converting them surfaced and fixed a real, pre-existing gap: no synchronous-pipeline agent activation ever completed its own `WorkItem`, so `create_child_work`'s live-task accounting (the first real caller of it) would have failed in any real run. `Agent.__init_subclass__` now wraps every leaf subclass's `run()` to complete (or fail) its task via a new `AgentContext.tasks`; Praxis's `method_for()` (the one calling convention that bypasses `run()`) completes explicitly at both its call sites. All 17 enqueue/gateway-time D5 bounds are enforced and tested (`test_control_bounds.py`), including the 5 run-level aggregates (`control/runs.py`'s accumulator) that had no enforcement point before this phase. All 5 delegation boundary tests from the plan's "Mandatory acceptance tests" spec pass against real code, each documenting its one narrow, intentional exception: `control/activation.py::ActivationFactory.activate` remains the interim `TaskService.enqueue` caller for the separate, materially larger "every individual agent activation becomes a durable child task" migration (~20 activations per run, not just Ledger/Herald's four) that this phase did not undertake.
- Residual risk: the `ActivationFactory.activate`-to-`create_child_work` migration for every non-Ledger/Herald agent activation remains open, tracked as its own future scope, not a Phase 5 gap. `_startup_maintenance`'s orphan-run reconciliation marks a stuck legacy `sessions` document `failed` directly rather than also transitioning its `WorkflowRun` through `SuperOrchestrator`; low-value/low-frequency (boot-time recovery of a crashed process) and not fixed here.

## F-ADAPT-001

- Owner: control-plane program
- Code anchors: former `backend/phi_core/control/adapters.py`; direct `run_decision_gates` callers in `backend/phi_core/agents/orchestrator.py` and `backend/server.py`
- Acceptance tests: `backend/tests/test_manager.py`, `backend/tests/test_human_review_resume_execution.py`, `backend/tests/test_certification_invalidation.py`
- Status: resolved
- Disposition: Phase 5 step 9 deleted the adapter module and its dedicated shim tests. The two live callers now pass their existing typed decision lists and hydrated session-file projections directly to `run_decision_gates`; repository search confirms no adapter import or symbol remains.
- Residual risk: none.

## F-HITL-001

- Owner: control-plane program
- Code anchors: `backend/server.py::session_human_review,HumanReviewSubmit`, `backend/phi_core/security.py::reviewer_principals,reviewer_role`, `backend/phi_core/agents/reasoning.py::auditor_escalation_reason,Auditor.run`, `backend/phi_core/agents/operator.py::_verify_record,_source_value_mismatch_problem`, `frontend/src/pages/SessionDetail.jsx:635-643,781-808,1128-1131`, `frontend/src/pages/Wizard.jsx:349-393,533-556`
- Acceptance tests: `backend/tests/test_certification_invalidation.py`, `backend/tests/test_production_readiness.py`, `backend/tests/test_manager_checkpoints.py`, `backend/tests/test_control_decisions.py`, `backend/tests/test_operator.py`, planned `backend/tests/test_control_review.py`, frontend review tests
- Status: open
- Disposition: `REVIEWER_PRINCIPALS`-backed authorization now gates `session_human_review` (D13 step 1) and boots insecure without it outside dev. Comment-mode resolution never auto-applies regardless of confidence (D13 step 6): every model interpretation of free text now requires a separate, explicit reviewer confirmation. `auditor_escalation_reason` now blocks a high-confidence `issues` verdict unconditionally and rejects an audit naming an unknown or hash-mismatched artifact (plan step 2, partial: evidence-sufficiency and deterministic-gate-result inputs to the same gate are not yet wired -- no established data path threads `EvidenceClaim`/`GateResult` objects into Auditor's context). `Judge.run` now returns a typed `JudgeProposal` (pydantic), validated immediately after the LLM call rather than trusting a bare dict. `client_event_id` idempotency and durable request resolution (D13 steps 3/4/5/9, partial) land in `session_human_review` directly rather than through a `HumanReviewService`, which does not exist yet. Plan step 6 (Operator source-vs-export value comparison) is done for `cap_age_90`, `year_only`, `zip3_truncate`, and `pseudonymize`. Plan step 5 (one typed audit record used by both review surfaces) is done: `_build_review_event` is the single constructor both the still-awaiting and resuming branches of `session_human_review` call, and `test_build_review_event_is_the_same_typed_record_on_both_review_surfaces` proves every field outside the surface-specific ones (`run_id`, `result`, `submitted_at`, `event_id`) matches for equivalent input. `audit_version`-required-when-`confirm_auditor_confidence` (D13 step 7) is not implemented. `decision_version` ownership (plan step 7) and frontend `whoami()` wiring (plan step 8) are not started.

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
