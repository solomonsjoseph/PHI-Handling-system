# Acceptance

Phase 9 step 3. Maps every phase gate from the master control-plane plan
to the code and test evidence that satisfies it, so a reader can confirm
"every requirement in the master prompt maps to code and tests" without
re-deriving nine phases of history.

## Methodology and a documented divergence

The master plan named exact test files and test names ahead of
implementation (`backend/tests/test_control_durable.py`,
`test_control_review.py`, and specific test names inside
`test_control_events.py`, `test_control_learning.py`, and
`test_control_capability.py`, among others). Execution consistently
organized tests under the repository's own existing file conventions
instead of creating the exact named files, documenting the substitution
in each phase's own `docs/assurance/PHASE_REPORTS.md` entry rather than
silently diverging. Two consequences, both intentional and both
recorded:

1. `test_control_durable.py` and `test_control_review.py` do not exist as
   named files. Their scenarios (kill/restart at every workflow node,
   lease racing and fencing, outbox-boundary recovery, idempotent review
   submission, supersede-not-clear) are covered under
   `test_control_tasks.py`, `test_control_workflow.py`,
   `test_control_worker.py`, `test_human_review_resume_execution.py`,
   `test_certification_invalidation.py`, `test_control_superorchestrator.py`,
   and `test_production_readiness.py` -- see each phase's own report for
   the exact test names used.
2. `test_control_events.py`, `test_control_learning.py`, and
   `test_control_capability.py` **do** exist (the last two written during
   Phase 8/9's own closing pass, `test_control_capability.py` for the
   first time -- see the residual-gap note below), but with test names
   this session chose to match its own acceptance criteria rather than
   the plan's literal names. The properties asserted are equivalent;
   the exact strings differ.

This is not treated as a gap requiring a name-for-name audit of every one
of the plan's ~100 named tests: the phase gates below are the actual
contract, and every one has direct, current, re-run test evidence.

## Genuine residual gaps found during Phase 8/9's closing pass

- **F-CAP-001** (Phase 2): `backend/tests/test_control_capability.py` was
  never created when Phase 2 closed, despite being named in the master
  plan. Discovered and partially filled in Phase 8: 4 of the plan's 5
  named tests now exist against real code
  (`test_nonresearch_agent_denied_web_search`,
  `test_agent_cannot_widen_its_own_grant`,
  `test_restricted_phi_is_never_sendable`,
  `test_agents_receive_no_database_handle`, plus
  `test_agent_cannot_widen_its_accepted_input_class_ceiling` as this
  session's closest faithful analog of the plan's "caller-declared
  classification is overridden by the derived value" test, which
  describes a derivation mechanism this codebase does not actually have).
  The 5th
  (`test_untrusted_text_cannot_change_grants_tools_evidence_gates_publication_or_workflow_state`)
  is not attempted: it needs a concrete enumeration of five distinct
  "untrusted text" injection points this pass did not have grounds to
  construct honestly.
- `ArtifactRecord.state` values `rejected` and `superseded` have no
  production producer (see F-RET-001 disposition and
  `docs/assurance/THREAT_MODEL.md`'s artifact-boundary section):
  `ArtifactService.reconcile`'s handling of them is real and tested
  directly against hand-built records, not yet exercised by a live
  Operator/Reviewer/Publish-Guard rejection path.
- D5's `outbox`/`checkpoint.payload_refs` bounds
  (`MAX_OUTBOX_ENTRIES_PER_DOC`, `MAX_CHECKPOINT_PAYLOAD_REFS`) have no
  enforcement code: nothing in the current codebase ever appends an
  `OutboxEntry` to either embedded array or populates
  `checkpoint.payload_refs` with real content (`OUTBOX_HANDLERS` is
  registered but no caller ever enqueues a kind it handles). Building a
  cap for an array nothing writes to would be untestable dead code; this
  is recorded rather than faked.

## Phase gates

| Phase | Gate (from the master plan) | Status | Evidence |
| --- | --- | --- | --- |
| 0 | Baseline frozen; branch created before any code change. | Met | `docs/assurance/PHASE_REPORTS.md` Phase 0; `git log` shows every phase as commits on `feat/phi-assurance-control-plane`. |
| 1 | Verification foundation repaired: async test collection, deterministic corpus fixtures, lint configuration, CI coverage. | Met | Phase 1 report: 570 -> full suite passing, flake fixed and verified over 20 runs. |
| 2 | Every provider call is gateway-mediated; a capability grant cannot be widened; `restricted_phi` never reaches a provider. | Met | `test_control_gateway_egress.py`, `test_control_capability.py` (Phase 8/9 closing pass), `test_architecture_boundaries.py::test_only_the_gateway_imports_litellm`-equivalent checks. |
| 3 | Every material artifact is staged, hash-verified, and promoted before being served; evidence claims require tool-backed provenance. | Met | `test_control_artifacts.py`, `test_control_evidence.py`, `test_control_evidence_agents.py`, `test_control_decisions.py`. |
| 4 | A crash or race at any point in the durable-execution lifecycle recovers to a consistent state, never a duplicate or lost effect. | Met | `test_control_tasks.py`, `test_control_workflow.py`, `test_control_worker.py`, `test_human_review_resume_execution.py` (kill/restart, lease fencing, outbox-boundary, duplicate-delivery idempotency scenarios, under repo-convention file names per the methodology note above). |
| 5 | `SuperOrchestrator` is the exclusive authority for workflow transitions, child delegation, and D5 resource bounds; nothing else calls `TaskService.enqueue` or writes `publication_pointers`. | Met | `test_control_superorchestrator.py`, `test_control_bounds.py`, `test_architecture_boundaries.py` (`test_manager_holds_no_workflow_authority`, `test_only_the_artifact_service_writes_the_publication_pointer`, `test_no_module_outside_the_super_orchestrator_calls_task_service_enqueue`, `test_every_entry_path_submits_a_command`, `test_concurrent_child_creation_cannot_exceed_parent_ancestor_or_run_budgets`). One documented, narrow exception: `ActivationFactory.activate` (F-ORCH-001). |
| 6 | Human review is authenticated, idempotent, fenced, and never treats model confidence as authority; evidence and gate parity hold across a fresh run and a resumed one. | Met | `test_certification_invalidation.py`, `test_manager_checkpoints.py`, `test_control_decisions.py`, `test_judge_typed_proposal.py`, `test_production_readiness.py`, `test_control_superorchestrator.py`'s supersede tests, frontend `SessionDetail.review.test.jsx`/`SessionDetail.stream.test.jsx`. Residual: F-HITL-001 (evidence-sufficiency/deterministic-gate-result grounds not yet wired into Auditor's escalation reasoning). |
| 7 | Every run's control flow, decisions, evidence, and artifact lineage are reconstructable from sanitized events without reconstructing restricted content; multiple SSE subscribers each receive the complete sequence; every artifact is registry-owned, held, or collected; an erasure failure cannot disappear after record deletion. | Met | `test_control_events.py` (seq/hash-chain/fence, seal/purge, archive registration, broker fan-out/overflow/`__end__` teardown), `test_control_artifacts.py` (reconcile), `test_production_readiness.py` (retention/erasure-pending/review-expiry/hold), `test_admin_assurance.py`, `test_security_audit_iter18.py` (bounded rate-bucket/chatgpt-login maps). Residual: `rejected`/`superseded` artifact states and outbox/checkpoint bounds have no live producer, noted above. |
| 8 | Activation requires a recorded evaluation and an authorized human approval; rollback restores the prior version; runtime tasks cannot write active policy stores. | Met | `test_control_learning.py`, `test_architecture_boundaries.py` (`test_no_agents_module_imports_control_learning`, `test_no_agents_module_writes_learning_or_capability_collections`), `test_control_research_cache.py`, `test_manager.py::test_manager_coaching_state_is_never_seeded_from_a_durable_store`. `LEARNING_ENABLED` defaults `false`. |
| 9 | Documentation and source agree; migration and rollback are tested; no P0/P1 finding remains open; all safety and acceptance tests pass. | Met, with residuals noted | `test_control_migrate.py`; `docs/assurance/MIGRATION.md` documents every migration's reverse step; `docs/AGENT_ARCHITECTURE.md`, `docs/assurance/ANCHORS.md`, `memory/ARCHITECTURE.md`, `README.md` updated; `docs/API.md` regenerated via `backend/scripts/export_openapi.py`; full backend + frontend suite run recorded in the closing `docs/assurance/PHASE_REPORTS.md` entry. No open P0/P1 finding in `docs/assurance/FINDINGS.md` as of this pass; the residual gaps above are P2/P3 (missing test coverage for an already-safe default state, not an open vulnerability). |
