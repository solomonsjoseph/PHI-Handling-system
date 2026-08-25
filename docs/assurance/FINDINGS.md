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
- Code anchors: `backend/server.py::session_human_review,HumanReviewSubmit,session_dataset_file,_startup_maintenance`, `backend/phi_core/security.py::reviewer_principals,reviewer_role`, `backend/phi_core/agents/reasoning.py::auditor_escalation_reason,Auditor.run`, `backend/phi_core/agents/operator.py::_verify_record,_source_value_mismatch_problem`, `backend/phi_core/agents/orchestrator.py::execute_decisions,_escalate_to_human_review`, `backend/phi_core/control/superorchestrator.py::SuperOrchestrator.request_human_review,supersede_human_review,consume_review_event`, `backend/phi_core/control/worker.py::TASK_HANDLERS,OUTBOX_HANDLERS`, `frontend/src/pages/SessionDetail.jsx:640,1163-1246,1373`, `frontend/src/pages/Wizard.jsx:349-395,528-552`
- Acceptance tests: `backend/tests/test_certification_invalidation.py`, `backend/tests/test_production_readiness.py`, `backend/tests/test_manager_checkpoints.py`, `backend/tests/test_control_decisions.py`, `backend/tests/test_operator.py`, `backend/tests/test_control_superorchestrator.py`, `backend/tests/test_control_records_policy.py`, `frontend/src/pages/__tests__/SessionDetail.review.test.jsx`
- Status: resolved
- Disposition: All nine numbered steps of Phase 6's plan text are now addressed. `REVIEWER_PRINCIPALS`-backed authorization gates `session_human_review` (step 1). `auditor_escalation_reason` blocks a high-confidence `issues` verdict unconditionally and rejects an audit naming an unknown or hash-mismatched artifact (step 2, partial: evidence-sufficiency and deterministic-gate-result inputs to the same gate are not yet wired -- no established data path threads `EvidenceClaim`/`GateResult` objects into Auditor's context, tracked as residual risk below, not fixed here). `client_event_id` idempotency and durable request resolution (steps 3/4/5/9) land in `session_human_review` directly. `audit_version` is now required whenever `confirm_auditor_confidence` is true and must match the open `HumanReviewRequest`'s own value (409 on mismatch) -- minted by the orchestrator as a content hash of Auditor's verdict at the exact escalation call site and persisted onto the session document so the frontend's plain session poll can render it. `SuperOrchestrator.supersede_human_review` closes an open request without a review event, and `request_human_review` calls it automatically on every new escalation for a run that already has one open: at most one open `HumanReviewRequest` per run_id always holds, closing the gap where an Auditor rerun could otherwise leave two competing open requests. Comment-mode resolution never auto-applies regardless of confidence (step 6). `decision_version` is real (CAS-incremented from `workflow_runs`, not hardcoded 0) everywhere D13 needs it, and `dataset_file_downloads` entries are scoped to `(principal, file_id, decision_version)` -- a download recorded against a superseded decision_version no longer satisfies the actual-knowledge gate (step 7). `Judge.run` returns a typed `JudgeProposal` (pydantic), validated immediately after the LLM call. `_build_review_event` is the single constructor both the still-awaiting and resuming branches of `session_human_review` call, proven identical outside surface-specific fields by `test_build_review_event_is_the_same_typed_record_on_both_review_surfaces` (step 5's parity half). Operator source-vs-export value comparison is done for `cap_age_90`, `year_only`, `zip3_truncate`, and `pseudonymize`. `SessionDetail.jsx`/`Wizard.jsx` replaced the operator-typed, localStorage-persisted "reviewer" field -- which was never actually sent to the backend, so it could display a name that did not match the authenticated principal actually stamped on every decision -- with a read-only identity from `GET /api/auth/whoami`, plus a confidence-only confirmation control that posts `confirm_auditor_confidence` with the open request's `audit_version` (step 8).

  The plan's literal `HumanReviewService` class (D13's `submit`/`supersede` methods, an `OutboxEntry` of kind `review_resume` drained by `control/worker.py`'s already-built `drain_outbox`) was not built as specified. Deliberate, not a gap: the durability property that design existed for -- resuming the pipeline tail survives a process restart between the review submission and the resume actually running -- is already provided by a different, equally durable mechanism this codebase had already built for the fresh-run path. `session_human_review` ends by calling `SuperOrchestrator.start_run(..., root_task_type="pipeline_resume")`, which creates a durable `WorkItem` claimed by one of `_startup_maintenance`'s `Worker` instances (`TASK_HANDLERS["pipeline_resume"] = _handle_pipeline_resume`) -- the same claim-and-lease, heartbeat, and retry machinery `pipeline_run` (fresh sessions) already uses, not a bespoke path. Adding a parallel `OutboxEntry(kind="review_resume")` relay on top would duplicate that durability with no functional gain, and would mean two different async-resume mechanisms for the two entry paths instead of one. `supersede_human_review` (above) is the one piece of D13's `HumanReviewService` surface that had no equivalent anywhere else in the codebase, so it was built directly. The fence/`lease_owner` check D13's step 2 names has no target in this architecture: no `WorkItem` exists yet at review-submission time (the route validates, gates, and CAS-updates the session document -- the same fenced-update pattern (`review_filter`) every other decision-mutation call site in this codebase already uses -- before `start_run` ever creates one); the `WorkItem` that check would apply to belongs to the already-covered `pipeline_run`/`pipeline_resume` claim-and-lease loop, not to a step that runs before any `WorkItem` exists.
- Residual risk: `auditor_escalation_reason`'s evidence-sufficiency and deterministic-gate-result grounds remain unwired (step 2). `_build_review_event`'s parity test proves shared-field equality for one input; it does not sweep every field pair across many random session states. `HumanReviewRequest.superseded_by` is `"system"` for every automatic supersede -- there is no path for a human (e.g. `lead_reviewer`) to explicitly supersede a request outside the automatic new-escalation case, which D13 also names but this pass does not implement (no product need for it has surfaced yet).

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
