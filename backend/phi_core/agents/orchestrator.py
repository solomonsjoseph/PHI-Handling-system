"""Orchestrator: run the full agent pipeline for a session.

Pipeline:
  1. Specialists (Lexicon, Schema, Instrument) launch immediately. RegulationsExpert
     and PHIMethodsExpert do NOT launch here (section 33: no broad research at run
     start) -- they launch on demand, once, from the decide loop, right after
     Judge's first (triage) pass names which HIPAA categories the dataset contains.
  2. Judge <-> Sentinel loop (short-circuits on 0 blocking issues; capped at ITERATION_CAP=2)
  3. Human review gate if Sentinel still has blocking issues
  4. Executor applies decisions
  5. Publish Guard (deterministic residual PHI scan)
  6. Reviewer Final (docs #55): completeness/authorization/privacy/utility gate,
     the sole post-execution "second review" safety net (Auditor's LLM
     re-derivation role was retired; see Phase 17-B).

Scout, Ledger, and Herald (competitive-landscape research, benchmark ledger,
publication draft) are NOT part of this path. They are an opt-in, post-run
add-on triggered explicitly via ``POST /api/sessions/{sid}/post-run-report``
for an already-complete session -- see ``outward.run_post_run_report``. They
never run automatically and can never block, slow, or contaminate the PHI
handling path.

Cancellation: the orchestrator checks ``is_cancelled(sid)`` between
phases. When True the pipeline exits early with status='cancelled' and
no further LLM calls are made. This is why every await for a phase is
followed by a cancel check.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from motor.motor_asyncio import AsyncIOMotorDatabase

from phi_core.control.activation import ActivationFactory
from phi_core.control.context import AgentContext
from phi_core.control.handoff import JUDGE, METHODS_EXPERT, REGULATIONS_EXPERT, REVIEWER, ReviewerHandoff
from phi_core.control.policy import BudgetExceeded
from phi_core.control.records import (
    ExecutionResult,
    HandoffEnvelope,
    MethodFinding,
    RegulatoryFinding,
    StudyKnowledgePackage,
)
from phi_core.control.store import ControlStore

if TYPE_CHECKING:
    from phi_core.control.manager import Manager
    from phi_core.control.records import VerifiedClassificationManifest

from phi_core.control.deterministic_verifier import DeterministicVerifier

from ..anonymizer import scrub_for_prompt
from ..control.manager import ManagerSupervision
from ..paths import cleanup_session_unpacked
from ..security import scrub_decision, scrub_persisted_text
from .base import ITERATION_CAP, AgentMessage
from .codegen import CodeGenerationExhausted
from .experts import PHIMethodsExpert, RegulationsExpert
from .llm import LlmConfig
from .reasoning import (
    BLOCKING_ISSUE_FLOOR,
    Executor,
    Judge,
    annotate_pending_review,
    apply_age_dob_rule,
    apply_blocking_floor,
    apply_confidence_floor,
    apply_sentinel_escalations,
    apply_sentinel_hard_rules,
    apply_site_cardinality_rule,
    plain_human_review_reasons,
    validate_decisions,
)
from .reviewer import Reviewer
from .specialists import (
    Instrument,
    Lexicon,
    Schema,
    UncertainHeaderCeilingExceeded,
    assemble_study_knowledge_package,
)

PhaseCb = Callable[[str, dict[str, Any]], Awaitable[None]]


class PipelineCancelled(Exception):
    """Raised by the orchestrator when the operator requested cancel."""

class ResultAcceptanceError(Exception):
    """Raised when Manager.accept_result refuses a completed
    child's result (D9/D5: 'child success is not acceptance'). A caller
    must never treat an unaccepted result as authoritative."""


def _blocking_issues(sentinel_out: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the sentinel issues whose severity is 'blocking'.

    Advisory issues are recorded for the audit trail but do NOT trigger
    another Judge iteration. This closes the "Sentinel nitpicks endlessly"
    pathology and is Sir's Q1 requirement -- iterate 'where required',
    not always.
    """
    return [i for i in (sentinel_out.get("issues") or [])
            if str(i.get("severity", "")).lower() == "blocking"]


def _escalation_issues(sentinel_out: dict[str, Any]) -> list[dict[str, Any]]:
    """Issues Sentinel marked 'escalate' -- genuine ambiguity it cannot
    correct itself. Routes straight to human_review, skipping further Judge
    iterations for that column (unlike 'blocking', which sends it back)."""
    return [i for i in (sentinel_out.get("issues") or [])
            if str(i.get("severity", "")).lower() == "escalate"]


async def _check_cancel(db: AsyncIOMotorDatabase, sid: str, on_phase: PhaseCb) -> None:
    doc = await db.sessions.find_one({"id": sid}, {"cancel_requested": 1, "status": 1})
    if doc and doc.get("cancel_requested"):
        await on_phase("cancelled",
                       {"reason": "operator requested cancel via /api/sessions/{sid}/cancel"})
        raise PipelineCancelled()


async def _escalate_to_human_review(
    *, db: AsyncIOMotorDatabase, session_filter: dict[str, Any], reasons: list[str],
    reasons_plain: list[str], close_last_phase: Callable[[], Awaitable[None]],
    phase_timings: dict[str, Any], run_elapsed_s: float,
    approved_decisions: list[dict[str, Any]], sentinel_report: dict[str, Any] | None,
    manager: ManagerSupervision, store: "ControlStore | None", run_id: str, node: str,
    audit_version: str = "",
) -> dict[str, Any]:
    """The single path by which a run becomes 'awaiting_human_review' (D10).

    Manager keeps ``close_run`` (the deterministic report) but no longer
    owns the workflow-authority write: this function persists the session
    document itself, exactly matching every other decision-mutation call
    site's own pattern, then asks ``Manager.request_human_review``
    to open the durable, typed ``HumanReviewRequest`` and pause the run at
    ``node``. Every current caller of the deleted
    ``Manager.escalate_to_human_review`` calls this instead.

    Tolerates an unknown ``run_id`` (``store`` is ``None``, or no
    ``WorkflowRun`` exists yet for it): the session-document write above is
    the tested, load-bearing contract every caller of this function
    depends on; the durable request is additive until every entry path
    reliably opens a run through ``Manager.start_run`` first.

    ``audit_version`` (D13 step 4/7): historically non-empty only for the
    now-retired Auditor-verdict escalation (``node="human_review_audit"``
    with a content hash of Auditor's verdict, so ``confirm_auditor_confidence``
    could be checked against a since-superseded audit). Phase 17-B removed
    Auditor and its only producer of a non-empty value here; the parameter
    stays (a caller may still pass one) but no current call site does, so
    it is effectively always empty now.
    """
    # Mirrors what the deleted `Manager.escalate_to_human_review` set
    # internally, so `close_run`'s report still carries the same
    # `"escalation"` field content it always has.
    manager._escalation = {"reasons": reasons}
    await manager._log("manager.escalate", "info", {"reasons": reasons})
    await close_last_phase()
    report = await manager.close_run("awaiting_human_review")
    await db.sessions.update_one(session_filter, {"$set": {
        "status": "awaiting_human_review",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "phase_timings": phase_timings,
        "run_elapsed_s": round(run_elapsed_s, 3),
        "human_review_reasons": reasons,
        "human_review_reasons_plain": reasons_plain,
        "manager_report": report,
    }})
    if store is not None and run_id:
        from phi_core.control.manager import Manager
        from phi_core.control.policy import CapabilityPolicy
        from phi_core.control.tasks import TaskService
        from phi_core.control.workflow import WorkflowError

        # D13 step 7: the request's own decision_version is the run's
        # current one (D11's CAS-incremented authority), not a hardcoded
        # 0 -- the later delivery-gate match on (principal, file_id,
        # decision_version) has nothing authoritative to compare against
        # otherwise.
        run_doc = await store.get_one("workflow_runs", {"run_id": run_id})
        current_decision_version = int(run_doc.get("decision_version", 0)) if run_doc else 0
        try:
            await Manager(store, TaskService(store, CapabilityPolicy(None))).request_human_review(
                run_id=run_id, node=node, reason_codes=reasons, decision_version=current_decision_version,
                audit_version=audit_version,
            )
        except WorkflowError as exc:
            if not str(exc).startswith("unknown run_id:"):
                raise
    return {"status": "awaiting_human_review", "decisions": approved_decisions,
            "sentinel": sentinel_report, "phase_timings": phase_timings,
            "manager_report": report}


async def _dispatch_execute(state: _PipelineDriverState) -> "str | dict[str, Any]":
    """The ``execute`` node (step 6, first third of the retired
    ``execute_decisions`` monolith): manifest freeze plus Executor.

    Returns the bare string ``"ok"`` to hand off to
    ``_dispatch_verify_output`` (chained in-process by
    ``_dispatch_execute_tail`` below, not through ``advance()`` --
    ``verify_output``/``package`` are the FINAL, post-step-8/9-12
    architecture's node names; see progress ledger Ruling 12), or a
    terminal escalation dict on ``crashed`` (Executor raised) /
    ``blocked`` (the manifest-freeze gate refused).
    """
    decisions = state.approved_decisions
    await state.on_phase("executor", {"decision_count": len(decisions)})
    manifest = None
    if state.control_store is not None and state.effective_run_id:
        from phi_core.control.manager import Manager
        from phi_core.control.manifest import (
            ManifestFreezeRefused,
            ManifestInvalidated,
            ensure_frozen_manifest,
            manifest_artifact_id,
        )
        from phi_core.control.policy import CapabilityPolicy
        from phi_core.control.tasks import TaskService

        unresolved_items = sum(1 for d in decisions if d.get("action") == "human_review")
        reviewer_preview_status = (
            state.sentinel_report.get("preview_status") if isinstance(state.sentinel_report, dict) else None
        )
        try:
            manifest = await ensure_frozen_manifest(
                store=state.control_store,
                orchestrator=Manager(state.control_store, TaskService(state.control_store, CapabilityPolicy(None))),
                run_id=state.effective_run_id, artifact_id=manifest_artifact_id(state.effective_run_id),
                source_artifact_versions={f["file_id"]: 0 for f in state.files if f.get("file_id")},
                decision_refs=[f"{d.get('file_id', '')}:{d.get('column', '')}" for d in decisions],
                evidence_refs=[],
                preview_review_id=str((state.sentinel_report or {}).get("finding_id", "")),
                human_review_refs=[],
                judge_complete=True,
                reviewer_preview_status=reviewer_preview_status,
                unresolved_items=unresolved_items,
                policy_gate_ok=True,
            )
        except (ManifestFreezeRefused, ManifestInvalidated) as exc:
            # Only a current, verified manifest may authorize execution
            # (docs #49/#50): a refusal here is a policy outcome, not an
            # Executor crash, so it gets its own log tag, but the same
            # escalation route -- there is exactly one path a run takes
            # to 'awaiting_human_review' (D10), and inventing a second
            # one for this case would fork that invariant for no reason.
            await state.manager._log("manifest.freeze_refused", "info", {"detail": str(exc)})
            return await _escalate_to_human_review(
                db=state.db, session_filter=state.session_filter, reasons=["manifest_freeze_refused"],
                reasons_plain=plain_human_review_reasons(["manifest_freeze_refused"]),
                close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
                run_elapsed_s=time.perf_counter() - state.run_started,
                approved_decisions=decisions, sentinel_report=state.sentinel_report,
                manager=state.manager, store=state.control_store, run_id=state.effective_run_id,
                node="human_review_decisions")

    try:
        executor_ctx = await state.make_ctx("Executor")
        exec_out = await Executor(executor_ctx).run(
            files=state.files, decisions=decisions, omit_by_file=state.omit_by_file,
            manifest=manifest, store=state.control_store)
        await state.require_accepted(executor_ctx, exec_out, "Executor")
    except CodeGenerationExhausted as exc:
        # Rewrite plan Task 11: two full generate-check-execute-verify
        # rounds (agents/codegen.py's own bounded retry) failed to
        # produce working transformation code for some dataset file.
        # Distinct from the generic `executor_crashed` path below: this
        # is a specific, structured diagnosis (which checks failed and
        # why) a human reviewing generated code needs, not a bare
        # exception. Routes to its own node (DISCUSSIONS.md round 6's
        # `human_review_code`) rather than `human_review_decisions`,
        # since the resolution here is about code, not a classification
        # decision.
        await state.manager._log("executor.codegen_exhausted", "info",
                           {"diagnostics": exc.diagnostics[:5]})
        return await _escalate_to_human_review(
            db=state.db, session_filter=state.session_filter, reasons=["code_generation_exhausted"],
            reasons_plain=plain_human_review_reasons(["code_generation_exhausted"]),
            close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
            run_elapsed_s=time.perf_counter() - state.run_started,
            approved_decisions=decisions, sentinel_report=state.sentinel_report,
            manager=state.manager, store=state.control_store, run_id=state.effective_run_id,
            node="human_review_code")
    except Exception as exc:
        # Executor is deterministic and irreversible (writes exports to disk);
        # a crash here must never be papered over by an LLM's advice.
        # consult() fails open by design and is never a safety gate, so the
        # escalation itself is unconditional, fixed code -- see manager.py.
        # The class name alone has repeatedly proved undiagnosable: every
        # Executor crash looks like `exception:RuntimeError` and the cause
        # has to be reproduced by hand. Carry the last line of the message
        # too, through `scrub_for_prompt` first, because Executor is the
        # one agent that reads raw rows and a traceback from it can quote a
        # cell value.
        detail, _ = scrub_for_prompt(str(exc), detectors=("presidio", "rule"))
        detail = detail.splitlines()[-1][:400] if detail.strip() else ""
        await state.manager._log("executor.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}",
                            "detail": detail})
        return await _escalate_to_human_review(
            db=state.db, session_filter=state.session_filter, reasons=["executor_crashed"],
            reasons_plain=plain_human_review_reasons(["executor_crashed"]),
            close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
            run_elapsed_s=time.perf_counter() - state.run_started,
            approved_decisions=decisions, sentinel_report=state.sentinel_report,
            manager=state.manager, store=state.control_store, run_id=state.effective_run_id,
            node="human_review_decisions")

    # Reversal key: the mandatory second deliverable (PHI-handled output
    # plus the key to reverse it), distinct from the optional publishing
    # stack below. Persisted now, separate from `exports`, never bundled.
    if exec_out.get("reversal_key_blob"):
        await state.db.sessions.update_one(state.session_filter, {"$set": {
            "reversal_key_blob": exec_out["reversal_key_blob"],
            "reversal_key_created_at": datetime.now(timezone.utc).isoformat(),
        }})

    state.manifest = manifest
    state.executor_ctx = executor_ctx
    state.exec_out = exec_out
    return "ok"


async def _dispatch_verify_output(state: _PipelineDriverState) -> "str | dict[str, Any]":
    """The (pre-step-8) ``verify_output`` stage (step 6, second third of
    the retired ``execute_decisions`` monolith, chained in-process by
    ``_dispatch_execute_tail`` -- see ``_dispatch_execute``'s docstring
    for why this is not yet its own ``advance()``-routed node):
    DeterministicVerifier (Operator), Reviewer's coverage audit, the
    Manager advisory checkpoint, Reviewer Final, and Publish Guard.

    Returns ``"ok"`` on success (every check clear; ``_dispatch_package``
    still applies its own ``partially_complete`` rule for deferred
    columns), or a terminal/escalation dict on ``leak_detected``
    (Publish Guard found residual PHI -- genuinely terminal, not an
    escalation), ``corrections_needed`` (the Manager's advisory coverage
    checkpoint asked for human eyes), or ``failed`` (Reviewer Final FAIL,
    routed through the existing rewind mechanism).
    """
    decisions = state.approved_decisions
    exec_out = state.exec_out
    manifest = state.manifest
    executor_ctx = state.executor_ctx
    omit_by_file = state.omit_by_file

    # DeterministicVerifier (docs #54): deterministic self-verification of
    # what Executor wrote, one stage before Publish Guard, mirroring the
    # Judge/Sentinel split one stage later. exec_out["exports"] stays
    # Executor's own factual record of what it wrote and is never mutated
    # here; `exports` is the DeterministicVerifier-then-Reviewer-filtered
    # view every later step in this function uses. Not an `Agent` (see
    # that module's docstring), so this is a plain call, not a
    # make_ctx/require_accepted pair -- `executor_ctx.sandbox` is passed
    # directly so its raw-row work stays inside the same isolation
    # boundary Executor itself already opted into for this run (`None`
    # for every pre-existing unit test's make_ctx-built context, exactly
    # as before).
    await state.on_phase("operator", {"decision_count": len(decisions)})
    try:
        op_out = await DeterministicVerifier().run(
            files=state.files, decisions=decisions, exports=exec_out["exports"],
            omit_by_file=omit_by_file, sandbox=executor_ctx.sandbox)
    except Exception as exc:
        # Fail open into the existing failed-file machinery: a file the
        # verifier cannot verify is dropped from exports exactly like an
        # unreadable file already is, rather than trusting it or
        # inventing a new path.
        await state.manager._log("operator.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}"})
        op_out = {"failed_file_ids": list(exec_out["exports"].keys()), "verdicts": []}
    verification_result = None
    if manifest is not None and state.control_store is not None:
        # docs #54/Phase 9 item 5: migrate Operator's useful deterministic
        # verification into a governed VerificationResult rather than
        # letting it live only as an ephemeral dict Reviewer's coverage
        # audit happens to consume -- additive only; every existing
        # consumer of `op_out` below is unchanged. Kept as a local
        # variable (Phase 10) so Reviewer Final below can reuse the same
        # object instead of re-deriving it from `op_out` a second time.
        from phi_core.control.verification import build_verification_result, record_verification_result

        verification_result = build_verification_result(
            run_id=state.effective_run_id, task_id=f"execution:{manifest.manifest_id}", attempt_id="",
            manifest_id=manifest.manifest_id, manifest_version=str(manifest.schema_version),
            input_artifact_version=0, output_artifact_version=0, operator_result=op_out,
        )
        await record_verification_result(state.control_store, verification_result)
    # Operator's own `failed_file_ids` only covers a file it could not read
    # or that never made it into `exports` at all (see operator.py). A
    # shape-check or reverse-completeness failure surfaces as a per-column
    # 'fail' verdict on an otherwise-readable file, and must block that
    # file from `exports` exactly the same way -- fold both into one set.
    op_failed_ids = sorted(set(op_out["failed_file_ids"]) |
                           {v["file_id"] for v in op_out["verdicts"] if v.get("verdict") == "fail"})
    exports = {fid: p for fid, p in exec_out["exports"].items()
              if fid not in op_failed_ids}

    # Reviewer: confirms Operator's coverage of every decision against
    # the real written export, catching gaps Operator's own pass cannot see
    # (e.g. an omit_by_file column that leaked into the header). Its own
    # filtered exports become canonical for every remaining step below,
    # starting with Publish Guard.
    try:
        reviewer_ctx = await state.make_ctx("Reviewer")
        rv_out = await Reviewer(reviewer_ctx).run(
            decisions=decisions,
            operator_result={"failed_file_ids": op_failed_ids, "verdicts": op_out["verdicts"]},
            exports=exports,
            omit_by_file=omit_by_file,
            metadata_file_ids={f.get("file_id", "") for f in state.dict_files},
        )
        await state.require_accepted(reviewer_ctx, rv_out, "Reviewer")
    except Exception as exc:
        # Same fail-open shape as Operator above: an unverifiable file is
        # dropped from exports, never trusted.
        await state.manager._log("reviewer.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}"})
        rv_out = {"exports": {}, "findings": []}
    reviewer_blocked_ids = sorted(set(exports) - set(rv_out["exports"]))
    exports = rv_out["exports"]

    # Advisory checkpoint: a Manager consult here is never a safety gate
    # (Publish Guard below remains the deterministic boundary regardless of
    # its advice); it only lets a systemically bad run reach a human sooner
    # than Publish Guard's blunter "blocked" report would.
    coverage_advice = await state.manager.consult(
        agent_name="Reviewer", phase="reviewer",
        signal={"operator_failed_count": len(op_failed_ids),
                "reviewer_blocked_count": len(reviewer_blocked_ids),
                "decision_count": len(decisions)})
    if coverage_advice.action == "escalate_human_review":
        await state.db.sessions.update_one(state.session_filter, {"$set": {
            "reviewer_findings": rv_out["findings"], "operator_failures": op_failed_ids}})
        return await _escalate_to_human_review(
            db=state.db, session_filter=state.session_filter, reasons=["manager_advisory_coverage_escalation"],
            reasons_plain=plain_human_review_reasons(["manager_advisory_coverage_escalation"]),
            close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
            run_elapsed_s=time.perf_counter() - state.run_started,
            approved_decisions=decisions, sentinel_report=state.sentinel_report,
            manager=state.manager, store=state.control_store, run_id=state.effective_run_id, node="human_review_audit")

    # Reviewer Final (docs #55, Phase 10): the real completeness/
    # authorization/privacy/utility gate for this attempt, distinct from
    # `rv_out`'s coverage audit above (confirms DeterministicVerifier's
    # own coverage of every decision, not the decisions' authorization/
    # privacy/utility). Gated the same way the governed
    # `VerificationResult` write above is (`manifest`/`store` both
    # present) -- every pre-existing unit test's direct `execute_
    # decisions` call with neither supplied never computes one, exactly
    # like every other Phase 9/10 additive control-plane write in this
    # function. Reuses `reviewer_ctx` from the coverage-audit call above
    # rather than opening a second `make_ctx("Reviewer")` task/`WorkItem`
    # for the same logical role in the same attempt; `finalize` never
    # touches `ctx.tasks` itself (unlike `run`), so no completion
    # bookkeeping is skipped by sharing it. `human_decisions` is `[]`:
    # no live caller constructs a `HumanDecision` record yet (Phase R-a
    # pre-add, still unwired), matching `ensure_frozen_manifest`'s own
    # `human_review_refs=[]` above -- forward-compatible, not a gap this
    # function invents.
    reviewer_final: dict[str, Any] | None = None
    if manifest is not None and state.control_store is not None and verification_result is not None:
        try:
            execution_results = await state.control_store.find_many(
                "execution_results", {"task_id": f"execution:{manifest.manifest_id}"})
            execution_result = (
                ExecutionResult.model_validate(execution_results[-1]) if execution_results
                else ExecutionResult(task_id=f"execution:{manifest.manifest_id}", run_id=state.effective_run_id,
                                     manifest_id=manifest.manifest_id, success=True)
            )
            # Reviewer Final audits what is ACTUALLY being shipped, not
            # the raw pre-filter pass: a per-file DeterministicVerifier
            # failure that `op_failed_ids`/`reviewer_blocked_ids` already
            # excluded from `exports` is this run's existing, legitimate
            # `blocked` degradation path (Ruling 13) -- it must not ALSO
            # trip a second, competing FAIL/rewind escalation here for
            # the exact same already-handled fact. `final_verification_
            # result`/`final_safe_output_metadata` are scoped to the
            # surviving `exports` only; the `verification_result`
            # persisted above (docs #54/Phase 9's exact contract) stays
            # the full, unfiltered record, unchanged.
            surviving_verdicts = [v for v in op_out["verdicts"] if v.get("file_id") in exports]
            final_verification_result = build_verification_result(
                run_id=state.effective_run_id, task_id=f"execution:{manifest.manifest_id}:final", attempt_id="",
                manifest_id=manifest.manifest_id, manifest_version=str(manifest.schema_version),
                input_artifact_version=0, output_artifact_version=0,
                operator_result={
                    "status": "clean" if all(v.get("verdict") == "pass" for v in surviving_verdicts) else "issues",
                    "verdicts": surviving_verdicts, "failed_file_ids": [],
                },
            )
            final_safe_output_metadata = dict(op_out)
            final_safe_output_metadata["column_counts"] = {
                fid: counts for fid, counts in (op_out.get("column_counts") or {}).items() if fid in exports
            }
            final_safe_output_metadata["schema_valid"] = {
                fid: ok for fid, ok in (op_out.get("schema_valid") or {}).items() if fid in exports
            }
            reviewer_final = await Reviewer(reviewer_ctx).finalize(
                manifest=manifest, execution_result=execution_result,
                verification_result=final_verification_result, decisions=decisions,
                human_decisions=[], safe_output_metadata=final_safe_output_metadata,
            )
            await state.on_phase("reviewer_final", {"verdict": reviewer_final["verdict"]})
        except Exception as exc:
            # Same fail-open shape as Operator/Reviewer coverage-audit
            # above: Reviewer Final is an additive audit gate, never the
            # sole boundary (Publish Guard below remains that regardless
            # of what happens here) -- a crash here (e.g. a test double
            # standing in for a role that predates this method) must
            # never take down an otherwise-successful run.
            await state.manager._log("reviewer_final.crashed", "info",
                               {"error_kind": f"exception:{type(exc).__name__}"})
            reviewer_final = None

    if reviewer_final is not None and reviewer_final["verdict"] == "FAIL" and reviewer_final.get("signal"):
        # Root-cause classification + rewind routing (docs #56, Phase
        # 10): never implement "FINAL FAIL -> STOP FOREVER" -- classify
        # the failure, try to route the run back to the earliest
        # affected node via the EXISTING `Manager.rewind` (no
        # second rewind mechanism built here), then fall back to the
        # same human-review escalation mechanism every other "this run
        # cannot silently succeed" branch in this function already uses.
        # Deliberately OUTSIDE the try/except above: a failure inside
        # `_escalate_to_human_review` itself must propagate/return
        # normally, never be swallowed as if it were merely a
        # `finalize()` crash (which would wrongly let a real FAIL fall
        # through to Publish Guard as if nothing happened).
        #
        # Disclosed structural limitation: `_dispatch_execute_tail`
        # (step 6, formerly `execute_decisions`) is dispatched as ONE
        # opaque unit from `run_pipeline`'s own coarse D9 registry (see
        # its docstring) -- the durable `WorkflowRun.node` is still
        # `"execute"` for this call's entire duration, so a resolved
        # target of `"execute"` itself (EXECUTION_ERROR) or
        # `"human_review_audit"` (the default post-execution
        # UNRESOLVED_UNCERTAINTY target, later than `"execute"` in D9's
        # checkpoint order) can never be strictly earlier than the run's
        # current node -- `rewind()` correctly refuses both with
        # `WorkflowError`, caught below rather than propagated. This is
        # the one disclosed case rewind "genuinely cannot apply" today:
        # the actual re-execution loop that would let a later phase
        # resume a run from an arbitrary rewound checkpoint is explicitly
        # out of this phase's scope (section 56: "do not implement").
        from phi_core.control.manager import Manager as _ManagerForRewind
        from phi_core.control.policy import CapabilityPolicy
        from phi_core.control.rewind import RewindRouter, record_rewind_decision
        from phi_core.control.tasks import TaskService
        from phi_core.control.workflow import WorkflowError as _WorkflowError

        rewind_orchestrator = _ManagerForRewind(state.control_store, TaskService(state.control_store, CapabilityPolicy(None)))
        escalation_node = "human_review_audit"
        rewind_reasons = ["reviewer_final_fail"]
        try:
            decision, _rewound_run = await RewindRouter.route(
                super_orchestrator=rewind_orchestrator, run_id=state.effective_run_id,
                signal=reviewer_final["signal"],
            )
            await record_rewind_decision(state.control_store, run_id=state.effective_run_id, decision=decision)
            await state.on_phase("rewind", decision.to_dict())
            escalation_node = decision.to_node
            rewind_reasons = [f"reviewer_final_fail_rewind:{decision.category}"]
        except _WorkflowError as exc:
            await state.manager._log("reviewer_final.rewind_structurally_refused", "info", {"detail": str(exc)})
            rewind_reasons = ["reviewer_final_fail_rewind_unavailable"]
        await state.db.sessions.update_one(state.session_filter, {"$set": {
            "reviewer_final": reviewer_final, "reviewer_findings": rv_out["findings"],
            "operator_failures": op_failed_ids,
        }})
        return await _escalate_to_human_review(
            db=state.db, session_filter=state.session_filter, reasons=rewind_reasons,
            reasons_plain=plain_human_review_reasons(rewind_reasons),
            close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
            run_elapsed_s=time.perf_counter() - state.run_started,
            approved_decisions=decisions, sentinel_report=state.sentinel_report,
            manager=state.manager, store=state.control_store, run_id=state.effective_run_id, node=escalation_node)

    # Publish Guard: deterministic last-mile PHI scan on emitted exports.
    # GOAL invariant: exports are only 'ready to share publicly' after this
    # boundary check clears. Runs synchronously; downloads are 403 until clean.
    from ..publish_guard import scan_all_exports as _scan_all_exports
    if exec_out["exports"]:
        # If Operator's checks wiped every file out of a non-empty
        # Executor output, scan_all_exports naturally reports "blocked"
        # on the resulting empty dict (scanned == 0) -- exactly the
        # existing "can't certify clean" behavior, no special-casing
        # needed here.
        guard_report = _scan_all_exports(
            exports, decisions=decisions, jurisdiction=state.session.get("jurisdiction", "us")
        ).to_dict()
    else:
        # Executor itself produced nothing exportable this round (e.g.
        # every column of the only dataset is still deferred). This is a
        # legitimate empty-so-far state, not a leak.
        guard_report = {"status": "clean", "results": [], "scanned": 0, "blocked": 0}
    if state.control_store is not None and state.effective_run_id and guard_report.get("results"):
        from phi_core.control.artifacts import ArtifactService, register_guard_rejections
        await register_guard_rejections(
            ArtifactService(state.control_store, session_id=state.sid, run_id=state.effective_run_id),
            guard_report=guard_report,
        )
    await state.on_phase("publish_guard", {"status": guard_report["status"],
                                     "scanned": guard_report["scanned"],
                                     "blocked": guard_report["blocked"]})

    if guard_report["status"] != "clean":
        await state.close_last_phase()
        manager_report = await state.manager.close_run("blocked")
        await state.db.sessions.update_one(
            state.session_filter,
            {"$set": {
                "status": "blocked",
                "guard_report": guard_report,
                "export_paths": exports,
                "agent_decisions": decisions,
                "phase_timings": state.phase_timings,
                "run_elapsed_s": round(time.perf_counter() - state.run_started, 3),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "manager_report": manager_report,
                "reviewer_findings": rv_out["findings"],
                "operator_failures": op_failed_ids,
            }},
        )
        cleanup_session_unpacked(state.sid)
        return {"status": "blocked", "guard": guard_report,
                "decisions": decisions, "phase_timings": state.phase_timings}

    state.exports = exports
    state.guard_report = guard_report
    state.op_failed_ids = op_failed_ids
    state.reviewer_blocked_ids = reviewer_blocked_ids
    state.rv_out = rv_out
    state.reviewer_final = reviewer_final
    return "ok"


async def _dispatch_package(state: _PipelineDriverState) -> dict[str, Any]:
    """The (pre-step-8) ``package`` stage (step 6, final third of the
    retired ``execute_decisions`` monolith): the terminal completion
    write. Always returns a dict -- this stage genuinely has no
    "hand off to the next stage" outcome; ``execute``/``verify_output``
    already own every dynamic escalation path.

    ``final_status`` (Ruling 13, user-confirmed): ``blocked`` when
    Operator or Reviewer could not verify a column that WAS attempted
    (``op_failed_ids``/``reviewer_blocked_ids`` -- a genuine verification
    failure, checked here rather than partially_complete since a
    "missing or unverified column decision" is never partially_complete
    per plan step 6); ``partially_complete`` when every attempted column
    verified clean but at least one column was deliberately deferred by
    a human (``omit_by_file`` -- never attempted, not a verification
    failure, so it does not compete with the `blocked` rule above);
    ``complete`` otherwise.
    """
    decisions = state.approved_decisions
    exports = state.exports
    guard_report = state.guard_report
    op_failed_ids = state.op_failed_ids
    reviewer_blocked_ids = state.reviewer_blocked_ids
    rv_out = state.rv_out
    reviewer_final = state.reviewer_final

    await _check_cancel(state.db, state.sid, state.on_phase)

    final_status = (
        "blocked" if (op_failed_ids or reviewer_blocked_ids)
        else "partially_complete" if state.omit_by_file
        else "complete"
    )

    await state.close_last_phase()
    manager_report = await state.manager.close_run(final_status)
    extra_completion_fields = {"advisory_issues": state.advisory_issues, "iteration_cap": state.iteration_cap}
    extra_result_fields = {"advisory_issues": state.advisory_issues, "iteration_cap": state.iteration_cap}
    if state.resume_from_node == "human_review_decisions":
        extra_completion_fields.update({
            "session_review": state.session_review_history,
            "pending_review": state.pending_review,
            "human_review_required": bool(state.pending_review),
        })
    result = {
        "status": final_status,
        "decisions": decisions,
        "exports": exports,
        "guard": guard_report,
        "phase_timings": state.phase_timings,
        "run_elapsed_s": round(time.perf_counter() - state.run_started, 3),
        "manager_report": manager_report,
        "operator_failures": op_failed_ids,
        "reviewer_findings": rv_out["findings"],
        "reviewer_final": reviewer_final,
    }
    result.update(extra_result_fields)
    completion_set = {
        "guard_report": guard_report,
        "export_paths": exports,
        "status": final_status,
        "phase_timings": state.phase_timings,
        "run_elapsed_s": result["run_elapsed_s"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "manager_report": manager_report,
        "operator_failures": op_failed_ids,
        "reviewer_findings": rv_out["findings"],
        "reviewer_final": reviewer_final,
    }
    completion_set.update(extra_completion_fields)
    await state.db.sessions.update_one(state.session_filter, {"$set": completion_set})
    if final_status == "complete":
        cleanup_session_unpacked(state.sid)
    return result


class _PipelineDriverState:
    """Mutable state shared across one ``run_pipeline`` invocation's
    registry-dispatched node handlers (Wave 4b, docs #87).

    Replaces the closures and local variables the pre-Wave-4b
    ``run_pipeline`` captured directly in its own function body: each
    ``_dispatch_*`` handler below reads and writes fields here exactly
    as that body once read and wrote its own locals. Construction has
    no side effects -- ``_prepare_pipeline_state`` (skipped entirely
    when a caller injects its own ``dispatch_registry``, the mechanism-
    only test seam) does the actual ``ActivationFactory``/
    ``ManagerSupervision`` setup work.
    """

    def __init__(
        self, *, session: dict[str, Any], db: AsyncIOMotorDatabase, llm_cfg: LlmConfig,
        emit: Callable[[AgentMessage], Awaitable[None]], on_phase: PhaseCb,
        run_id: str | None, control_store: "ControlStore | None", root_task_id: str | None,
        sid: str, effective_run_id: str, resume_from_node: str | None = None,
    ) -> None:
        self.session = session
        self.db = db
        self.llm_cfg = llm_cfg
        self.emit = emit
        self.on_phase = on_phase
        self.run_id = run_id
        self.control_store = control_store
        self.root_task_id = root_task_id
        self.sid = sid
        self.effective_run_id = effective_run_id
        self.session_filter: dict[str, Any] = {"id": sid}
        self.files: list[dict[str, Any]] = []
        self.dataset_files: list[dict[str, Any]] = []
        self.form_files: list[dict[str, Any]] = []
        self.dict_files: list[dict[str, Any]] = []
        self.phase_timings: dict[str, dict[str, float]] = {}
        self.run_started: float = time.perf_counter()
        self.close_last_phase: Callable[[], Awaitable[None]] = _noop_close_last_phase
        self.iteration_cap: int = ITERATION_CAP
        self.factory: "ActivationFactory | None" = None
        self.manager: "ManagerSupervision | None" = None
        self.make_ctx: Callable[[str], Awaitable[AgentContext]] | None = None
        self.make_child_ctx: Callable[[str, str], Awaitable[AgentContext]] | None = None
        self.complete_and_accept: Callable[[AgentContext, dict[str, Any]], Awaitable[bool]] | None = None
        self.require_accepted: Callable[[AgentContext, dict[str, Any], str], Awaitable[None]] | None = None
        self.lexicon_ctx: AgentContext | None = None
        self.schema_ctx: AgentContext | None = None
        self.instrument_ctx: AgentContext | None = None
        self.lexicon_agent: Any = None
        self.schema_agent: Any = None
        self.instrument_agent: Any = None
        self.lex_task: Any = None
        self.schema_task: Any = None
        self.inst_task: Any = None
        self.statute: dict[str, Any] = {}
        self.praxis_methods: dict[str, Any] = {}
        self.lexicon: dict[str, Any] = {}
        self.schema: dict[str, Any] = {}
        self.instrument: dict[str, Any] = {}
        self.study_knowledge_package: "StudyKnowledgePackage | None" = None
        self.schema_stats: dict[str, Any] = {}
        self.prompt_scrub_counts: dict[str, int] = {}
        self.decisions: list[dict[str, Any]] = []
        self.approved_decisions: list[dict[str, Any]] = []
        self.judge_call_failures: int = 0
        self.sentinel_call_failures: int = 0
        self.advisory_issues: list[dict[str, Any]] = []
        self.all_sentinel_overrides: list[dict[str, Any]] = []
        self.all_model_output_rejections: list[dict[str, Any]] = []
        self.manager_early_escalation: bool = False
        self.blocking_attempts: dict[tuple[str, str], int] = {}
        self.sentinel_report: dict[str, Any] = {}
        self.dictionary_by_column: dict[str, str] = {}
        self.reasons: list[str] = []
        # Step 6: set only when this run_pipeline() call is resuming a
        # parked node (server.py's human-review-resume path) rather than
        # advancing forward within one continuous call. Dispatch
        # handlers that behave differently on resume (currently
        # _dispatch_human_review_decisions) branch on this.
        self.resume_from_node: str | None = resume_from_node
        self.omit_by_file: dict[str, set[str]] = {}
        self.pending_review: list[dict[str, Any]] = []
        self.session_review_history: list[dict[str, Any]] = []
        # Step 6: cross-stage fields threaded through the execute-tail
        # split (_dispatch_execute -> _dispatch_verify_output ->
        # _dispatch_package), replacing what the retired monolithic
        # `execute_decisions` held as plain locals across the same span.
        self.manifest: "VerifiedClassificationManifest | None" = None
        self.executor_ctx: AgentContext | None = None
        self.exec_out: dict[str, Any] = {}
        self.exports: dict[str, str] = {}
        self.guard_report: dict[str, Any] = {}
        self.op_failed_ids: list[str] = []
        self.reviewer_blocked_ids: list[str] = []
        self.rv_out: dict[str, Any] = {}
        self.reviewer_final: dict[str, Any] | None = None


async def _noop_close_last_phase() -> None:
    return None


DispatchFn = Callable[[_PipelineDriverState], Awaitable["str | dict[str, Any]"]]


async def _prepare_pipeline_state(state: _PipelineDriverState) -> None:
    """Populate every shared field the production ``_dispatch_*`` handlers
    need: dataset file partitioning, phase-timing instrumentation, the
    durable ``ActivationFactory``, and the ``ManagerSupervision``
    (opened with its charter) -- exactly the setup the pre-Wave-4b
    ``run_pipeline`` did inline before its first phase. Skipped entirely
    when a caller supplies its own ``dispatch_registry``."""
    session = state.session
    state.session_filter = {"id": state.sid}
    if state.run_id is not None:
        state.session_filter["_pipeline_run_id"] = state.run_id
    files = session.get("files", [])
    state.files = files
    state.dataset_files = [f for f in files if f["kind"] == "dataset"]
    state.form_files = [f for f in files if f["kind"] == "narrative"]
    state.dict_files = [f for f in files if f["kind"] == "metadata"]

    state.run_started = time.perf_counter()
    original_on_phase = state.on_phase
    _last_phase: dict[str, str | None | float] = {"key": None, "t0": 0.0}

    async def timed_on_phase(phase: str, payload: dict[str, Any]) -> None:
        now = time.perf_counter()
        prev = _last_phase["key"]
        if prev and prev not in ("cancelled", "complete", "__end__") and prev != phase:
            row = state.phase_timings.setdefault(prev, {"start_s": _last_phase["t0"] - state.run_started})
            row["end_s"] = now - state.run_started
            row["duration_ms"] = (now - _last_phase["t0"]) * 1000
        state.phase_timings.setdefault(phase, {"start_s": now - state.run_started})
        _last_phase["key"] = phase
        _last_phase["t0"] = now
        payload = dict(payload or {})
        payload["_elapsed_s"] = round(now - state.run_started, 3)
        await state.manager.note_phase(phase, now - state.run_started)
        await original_on_phase(phase, payload)

    async def close_last_phase() -> None:
        prev = _last_phase["key"]
        if not prev:
            return
        now = time.perf_counter()
        row = state.phase_timings.setdefault(prev, {"start_s": _last_phase["t0"] - state.run_started})
        row.setdefault("end_s", now - state.run_started)
        row.setdefault("duration_ms", (now - _last_phase["t0"]) * 1000)

    state.on_phase = timed_on_phase
    state.close_last_phase = close_last_phase

    iteration_cap = int(session.get("iteration_cap") or ITERATION_CAP)
    state.iteration_cap = max(1, min(iteration_cap, ITERATION_CAP))

    state.factory = ActivationFactory(state.db, state.llm_cfg, store=state.control_store)

    async def make_ctx(agent: str) -> AgentContext:
        if state.root_task_id:
            return await state.factory.activate_child(
                session_id=state.sid, run_id=state.effective_run_id,
                parent_task_id=state.root_task_id, agent=agent, emit=state.emit,
                manager=state.manager, lease_owner=f"pipeline:{state.effective_run_id}",
            )
        return await state.factory.activate(
            session_id=state.sid, run_id=state.effective_run_id, agent=agent,
            emit=state.emit, manager=state.manager, lease_owner=f"pipeline:{state.effective_run_id}",
        )

    async def make_child_ctx(agent: str, parent_task_id: str) -> AgentContext:
        return await state.factory.activate_child(
            session_id=state.sid, run_id=state.effective_run_id, parent_task_id=parent_task_id,
            agent=agent, emit=state.emit, manager=state.manager,
            lease_owner=f"pipeline:{state.effective_run_id}",
        )

    async def complete_and_accept(ctx: AgentContext, result: dict[str, Any]) -> bool:
        return await state.factory.complete_and_accept(ctx, result)

    async def require_accepted(ctx: AgentContext, result: dict[str, Any], agent: str) -> None:
        if not await complete_and_accept(ctx, result):
            raise ResultAcceptanceError(f"{agent} result was not accepted")

    state.make_ctx = make_ctx
    state.make_child_ctx = make_child_ctx
    state.complete_and_accept = complete_and_accept
    state.require_accepted = require_accepted

    manager = ManagerSupervision(await make_ctx("Manager"), db=state.db)
    state.manager = manager
    manager_result = await manager.run(
        roster=["Lexicon", "Schema", "Instrument", "RegulationsExpert", "PHIMethodsExpert", "Judge",
                "Reviewer", "Executor"],
        phase_plan=["specialists", "statute", "praxis", "judge_iter", "sentinel_iter",
                    "executor", "publish_guard"],
    )
    await require_accepted(manager.ctx, manager_result, "Manager")


def _handoff_finding_payload(finding: "RegulatoryFinding | MethodFinding") -> dict[str, Any]:
    """Check 6's "minimum necessary" payload for a RegulatoryFinding/
    MethodFinding handoff to Judge -- deliberately narrower than
    ``finding.model_dump()``. ``evidence_refs`` (raw source URLs) and
    ``created_at`` (a precise timestamp) are both HIPAA Safe Harbor
    identifier shapes (categories N and C) that this codebase's own
    residual-PHI heuristic (``control.gateway._contains_restricted_
    content``, check 7, the same heuristic ``ProviderGateway.complete``
    applies at LLM egress) correctly refuses to let cross ANY agent
    boundary raw -- discovered empirically 2026-08-29 (Phase 5/6
    orchestrator follow-up item 1): every ``RegulatoryFinding``/
    ``MethodFinding`` handoff was denied ``residual_phi_detected`` until
    this trim. Nothing is lost: the full finding (evidence_refs/
    created_at included) is still durably persisted to
    ``regulatory_findings``/``method_findings`` immediately after a
    successful handoff (see both call sites below) -- only the
    GOVERNANCE ENVELOPE itself carries the minimum Judge needs to
    correlate a finding with its durable record (``finding_id``) and
    know what it concerns (``hipaa_category``, ``summary``)."""
    return {
        "finding_id": finding.finding_id,
        "run_id": finding.run_id,
        "hipaa_category": finding.hipaa_category,
        "summary": finding.summary,
    }


async def _run_regulations_expert(state: _PipelineDriverState, categories: list[str]) -> dict[str, Any]:
    """RegulationsExpert researches every identifier category for one
    jurisdiction in a single call (``rules_for``'s own contract) -- there
    is no per-category web search to fragment or deduplicate here. The
    caller already deduplicated ``categories`` (see
    ``_needed_hipaa_categories``); this function reuses the one reply to
    build one governed ``RegulatoryFinding`` per category (section 35:
    "Return may go directly to Judge through the governed handoff path").

    Routed through ``HandoffGateway.handoff`` on the ``(RegulationsExpert,
    Judge)`` edge -- 2026-08-29 (Phase 5/6 orchestrator follow-up item 1).
    An earlier version of this function persisted directly to the control
    store instead, to dodge a pre-existing exclusivity invariant
    (``test_control_phaseR_integration.py::
    test_handoff_call_sites_confined_to_manager_broker``) that then
    confined every ``.handoff(`` call site to ``agents/manager.py``; that
    invariant's allowlist is now widened to include this file (see its
    own docstring), because ``HandoffGateway.ALLOWED_EDGES``/
    ``EDGE_SCHEMAS`` already registered this edge and its
    ``RegulatoryFinding`` payload schema (Wave R-c Step 6 / Phase R-a) --
    no ``control/handoff.py`` change was needed. See
    ``_handoff_finding_payload`` above for why the payload is narrower
    than the full finding.

    The handoff's allowed/denied verdict never gates persistence --
    exactly ``manager.py``'s own ``_record_handoff`` precedent for its 3
    edges ("its allowed/denied verdict is written to the trace store but
    never gates [the underlying operation's] own return value"). A real
    HIPAA regulatory citation (e.g. "45 CFR 164.514") is legitimate,
    already-public regulatory text, not PHI, but its digit run can still
    trip the same over-cautious residual-PHI heuristic
    (``control.gateway._contains_restricted_content``) that flags a
    genuine 9-digit SSN; denying a governed handoff on a false positive
    must never silently drop a real, correctly-derived finding the rest
    of this codebase already depends on being durable. A denial is
    logged (never silent) and the finding is still persisted."""
    ctx = await state.make_ctx("RegulationsExpert")
    agent = RegulationsExpert(ctx)
    result = await agent.run(jurisdiction=state.session.get("jurisdiction", "us"))
    await state.require_accepted(ctx, result, "RegulationsExpert")
    for category in categories:
        finding = _regulatory_finding_for_category(result, category, ctx.run_id)
        if ctx.handoff is not None:
            handoff_result = await ctx.handoff.handoff(HandoffEnvelope(
                run_id=ctx.run_id, sender=REGULATIONS_EXPERT, recipient=JUDGE,
                data_class="restricted_metadata", payload=_handoff_finding_payload(finding),
            ))
            if not handoff_result.allowed:
                await agent._log("regulations_expert.handoff_denied", "info",
                                  {"category": category, "reason": handoff_result.reason_code,
                                   "detail": handoff_result.detail})
        await state.factory.store.insert("regulatory_findings", finding)
    return result


async def _run_phi_methods_expert_method(state: _PipelineDriverState, category: str) -> dict[str, Any]:
    # PHIMethodsExpert is called per-category via `method_for`, never `run` --
    # `Agent.__init_subclass__`'s completion wrap only ever sees `run`,
    # so this path completes/fails its own task explicitly, matching
    # what that wrap does for every other agent.
    ctx = await state.make_ctx("PHIMethodsExpert")
    agent = PHIMethodsExpert(ctx)
    try:
        result = await agent.method_for(category)
    except Exception as exc:
        await agent._log("phi_methods_expert.category_failed", "info",
                          {"category": category, "error": f"{type(exc).__name__}: {exc}"})
        if ctx.tasks is not None:
            await ctx.tasks.fail(f"agent_crashed:{type(exc).__name__}")
        raise
    result = result if isinstance(result, dict) else {}
    if ctx.tasks is not None:
        await ctx.tasks.complete(result)
    await state.require_accepted(ctx, result, "PHIMethodsExpert")
    # docs #38: research discovery alone never grants execution
    # permission, and nothing in this codebase calls
    # register_method/promote yet -- MethodFinding.recommended_method_id
    # stays the field's own default rather than minting one here.
    # Routed through HandoffGateway.handoff on the (PHIMethodsExpert,
    # Judge) edge -- see _run_regulations_expert's and
    # _handoff_finding_payload's docstrings above for the full
    # history/rationale (2026-08-29, Phase 5/6 orchestrator follow-up
    # item 1), including why a denial never gates persistence; the same
    # reasoning applies here verbatim.
    finding = _method_finding_for_category(result, category, ctx.run_id)
    if ctx.handoff is not None:
        handoff_result = await ctx.handoff.handoff(HandoffEnvelope(
            run_id=ctx.run_id, sender=METHODS_EXPERT, recipient=JUDGE,
            data_class="restricted_metadata", payload=_handoff_finding_payload(finding),
        ))
        if not handoff_result.allowed:
            await agent._log("phi_methods_expert.handoff_denied", "info",
                              {"category": category, "reason": handoff_result.reason_code,
                               "detail": handoff_result.detail})
    await state.factory.store.insert("method_findings", finding)
    return result


# ---- section 33/89: demand-driven RegulationsExpert/PHIMethodsExpert research
#
# The pre-Phase-6 pipeline launched both experts unconditionally at t=0
# (RegulationsExpert once, PHIMethodsExpert once per every one of 17 fixed
# HIPAA categories, regardless of what the dataset actually contains) --
# exactly the pattern section 33 forbids. Research is now requested by
# the dispatch handler (acting on Judge's behalf, since Judge's own
# `reasoning.py` is out of this file's scope to restructure) once,
# from `_dispatch_decide`, right after Judge's own first pass -- its
# triage -- names which HIPAA categories the dataset actually contains.

_KNOWN_HIPAA_CATEGORY_LETTERS: frozenset[str] = frozenset(chr(c) for c in range(ord("A"), ord("R") + 1))


def _needed_hipaa_categories(decisions: list[dict[str, Any]]) -> list[str]:
    """Deduplicated (a ``set`` comprehension), sorted list of the real
    HIPAA identifier-category letters Judge's own decisions name -- never
    every possible category, never zero regardless of what is in the
    data. Two columns tagged with the same category still produce exactly
    one entry, so ``_dispatch_demand_driven_research`` below makes exactly
    one ``PHIMethodsExpert.method_for`` call per distinct category and
    reuses its result for every column that needs it. Anything Judge
    proposed outside the real A-R letter vocabulary (``"NONE"``,
    ``"QUASI"``, ``None``, a malformed model reply) is silently excluded,
    never sent on to an expert call."""
    return sorted({
        category for d in decisions
        if (category := d.get("phi_category")) in _KNOWN_HIPAA_CATEGORY_LETTERS
    })


def _regulatory_finding_for_category(reply: dict[str, Any], category: str, run_id: str) -> RegulatoryFinding:
    """Section 35's typed output, sliced from RegulationsExpert's one
    jurisdiction-wide reply for the one category this finding is about.
    Never reads anything but the expert's own reply -- no decision text,
    column name, or dataset value ever reaches this construction (section
    36: a research finding is built from research, not from the data)."""
    rule = next(
        (hr for hr in (reply.get("handling_rules") or []) if hr.get("category") == category), None,
    )
    evidence_refs = sorted({s.get("url") for s in (reply.get("sources") or []) if s.get("url")})
    if rule:
        summary = f"{reply.get('regulation', '')}: {rule.get('rule', '')}".strip(": ")
    else:
        summary = reply.get("citation") or reply.get("regulation") or ""
    return RegulatoryFinding(run_id=run_id, hipaa_category=category, evidence_refs=evidence_refs, summary=summary)


def _method_finding_for_category(reply: dict[str, Any], category: str, run_id: str) -> MethodFinding:
    """Section 37's typed output for one ``PHIMethodsExpert.method_for``
    reply. Never reads anything but the expert's own reply, for the same
    reason as ``_regulatory_finding_for_category`` above."""
    methods = reply.get("methods") or []
    evidence_refs = sorted({
        s.get("url") for m in methods for s in (m.get("sources") or []) if s.get("url")
    })
    summary = "; ".join(m.get("name", "") for m in methods if m.get("name"))
    return MethodFinding(run_id=run_id, hipaa_category=category, evidence_refs=evidence_refs, summary=summary)


async def _dispatch_demand_driven_research(state: _PipelineDriverState, categories: list[str]) -> None:
    """Replaces the removed t=0 broad launch. Called exactly once, from
    ``_dispatch_decide``, right after Judge's triage pass names which
    categories the dataset contains -- never before, and never at all
    when `categories` is empty (nothing in the data needs regulatory or
    method grounding, so no research is requested). RegulationsExpert
    and PHIMethodsExpert still run concurrently with each other, exactly
    as the removed t=0 code did -- only the trigger condition and timing
    changed, not the underlying concurrency."""
    if not categories:
        return
    await state.on_phase("statute", {})
    await state.on_phase("praxis", {})
    regulations_expert_task = asyncio.create_task(_run_regulations_expert(state, categories))
    phi_methods_expert_gather_task = asyncio.gather(
        *[_run_phi_methods_expert_method(state, category) for category in categories],
        return_exceptions=True,
    )
    state.statute = await regulations_expert_task
    method_results = await phi_methods_expert_gather_task
    praxis_methods: dict[str, Any] = {}
    for category, res in zip(categories, method_results, strict=True):
        if isinstance(res, Exception):
            continue
        praxis_methods[category] = res
    state.praxis_methods = praxis_methods


async def _dispatch_research(state: _PipelineDriverState) -> str:
    """Step 6 fold: the pre-step-8 ``research`` node is now a no-op
    pass-through. The Lexicon/Schema/Instrument launch it used to own
    has moved into ``_dispatch_specialists`` below -- step 8 repurposes
    the ``research`` node name for the demand-driven RegulationsExpert/
    PHIMethodsExpert research step instead (see
    ``_dispatch_demand_driven_research``), so nothing about *that*
    later step belongs under this name any more. Kept only so the
    pre-step-8 ``charter -> research -> specialists`` table hop still
    dispatches to something; step 8 removes this node/handler entirely
    once the table itself changes."""
    return "ok"


_SPECIALIST_DEGRADED_RESULT: dict[str, dict[str, Any]] = {
    "Lexicon": {"columns": [], "notes": ""},
    "Schema": {"columns": []},
    "Instrument": {"fields": []},
}


async def _dispatch_specialists(state: _PipelineDriverState) -> "str | dict[str, Any]":
    """The ``specialists`` node: launches the Lexicon/Schema/Instrument
    tasks (folded in from the now-retired pre-step-8 ``research`` node,
    step 6), awaits them, then assembles their (possibly-degraded, see
    section 27 below) outputs into one ``StudyKnowledgePackage``
    (section 28) for ``_dispatch_decide`` to hand Judge, instead of
    Judge reading three separate specialist dicts.
    Section 27 (failure isolation): ``return_exceptions=True`` on the
    gather means one specialist crashing never discards its siblings'
    already-successful results -- the pre-existing bare
    ``asyncio.gather`` (no ``return_exceptions``) would propagate the
    first exception immediately, losing whatever the other two tasks
    already produced. ``UncertainHeaderCeilingExceeded`` is still
    checked first and short-circuits to the same 'blocked' response
    exactly as before -- it names a run-wide ceiling, not one
    specialist's own failure, so it is never treated as a "degrade and
    continue" case. Any OTHER exception is logged via ``state.on_phase``
    (never silent) and degrades just that one specialist to its own
    "did not run" empty shape (the same shape ``_dispatch_research``
    already substitutes when the specialist has no matching input
    files -- ``_SPECIALIST_DEGRADED_RESULT`` above), letting the other
    two specialists' real results flow through unchanged. Task
    lifecycle: ``Agent.__init_subclass__``'s completion
    wrap (``agents/base.py``) already calls ``ctx.tasks.fail(...)`` on
    an unhandled exception inside ``Lexicon.run``/``Schema.run``/
    ``Instrument.run`` before it ever reaches this function -- confirmed
    by reading that wrap -- so nothing here needs to fail the task a
    second time; ``return_exceptions=True`` only changes how the
    already-failed task's exception propagates to this caller, not
    what already happened to the task before it was raised."""
    await _prepare_pipeline_state(state)
    await state.on_phase("specialists", {"agents": ["Lexicon", "Schema", "Instrument"]})

    dataset_files, form_files, dict_files = state.dataset_files, state.form_files, state.dict_files
    state.lexicon_ctx = await state.make_ctx("Lexicon") if dict_files else None
    state.schema_ctx = await state.make_ctx("Schema") if dataset_files else None
    state.instrument_ctx = await state.make_ctx("Instrument") if form_files else None
    state.lexicon_agent = Lexicon(state.lexicon_ctx) if state.lexicon_ctx else None
    state.schema_agent = Schema(state.schema_ctx) if state.schema_ctx else None
    state.instrument_agent = Instrument(state.instrument_ctx) if state.instrument_ctx else None
    state.lex_task = (state.lexicon_agent.run(dict_files=dict_files)
                       if state.lexicon_agent else _empty({"columns": [], "notes": ""}))
    state.schema_task = (state.schema_agent.run(dataset_files=dataset_files)
                          if state.schema_agent else _empty({"columns": []}))
    state.inst_task = (state.instrument_agent.run(form_files=form_files)
                        if state.instrument_agent else _empty({"fields": []}))

    results = await asyncio.gather(
        state.lex_task, state.schema_task, state.inst_task, return_exceptions=True,
    )
    for result in results:
        if isinstance(result, UncertainHeaderCeilingExceeded):
            exc = result
            # v3 section 7: past the per-run uncertain-header ceiling, an
            # unresolved ambiguous-header population is itself evidence the
            # review process is not keeping up for this run -- block, same
            # "blocked" shape Publish Guard's own scan uses further down. No
            # TRANSITIONS edge models "specialists -> blocked": this
            # short-circuit bypasses advance() entirely, exactly as the
            # pre-Wave-4b pipeline already did.
            await state.close_last_phase()
            manager_report = await state.manager.close_run("blocked")
            await state.db.sessions.update_one(
                state.session_filter,
                {"$set": {
                    "status": "blocked",
                    "failure_class": exc.failure_class,
                    "phase_timings": state.phase_timings,
                    "run_elapsed_s": round(time.perf_counter() - state.run_started, 3),
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                    "manager_report": manager_report,
                }},
            )
            cleanup_session_unpacked(state.sid)
            return {"status": "blocked", "failure_class": exc.failure_class,
                    "detail": str(exc), "phase_timings": state.phase_timings}
    names = ("Lexicon", "Schema", "Instrument")
    agents = (state.lexicon_agent, state.schema_agent, state.instrument_agent)
    ctxs = (state.lexicon_ctx, state.schema_ctx, state.instrument_ctx)
    outputs: dict[str, dict[str, Any]] = {}
    crashed: set[str] = set()
    for name, agent, ctx, result in zip(names, agents, ctxs, results, strict=True):
        if isinstance(result, BaseException):
            crashed.add(name)
            await state.on_phase("specialist_crashed", {
                "specialist": name,
                "error": f"{type(result).__name__}: {scrub_persisted_text(str(result))}",
            })
            outputs[name] = dict(_SPECIALIST_DEGRADED_RESULT[name])
            continue
        outputs[name] = result
        if agent:
            await state.require_accepted(ctx, result, name)
    lexicon, schema, instrument = outputs["Lexicon"], outputs["Schema"], outputs["Instrument"]
    # Deterministic guardian query broker: the ManagerSupervision
    # holds the only reference to each specialist for targeted
    # ask_schema/ask_instrument/ask_lexicon lookups, attached only when
    # that specialist actually ran (and did not crash -- a crashed
    # specialist's own instance is left half-initialized; attaching it
    # would just move the failure to a later, harder-to-diagnose broker
    # query instead of the "did not run" degraded shape already
    # substituted for it above).
    if state.lexicon_agent and "Lexicon" not in crashed:
        state.manager.attach_lexicon(state.lexicon_agent)
    if state.schema_agent and "Schema" not in crashed:
        state.manager.attach_schema(state.schema_agent)
    if state.instrument_agent and "Instrument" not in crashed:
        state.manager.attach_instrument(state.instrument_agent)
    # Carried forward for the site/facility cardinality rule. Fakes/mocks
    # in tests never set `_stats`, so default to empty rather than assume
    # a real Schema instance ran (or a crashed one whose partial state
    # cannot be trusted).
    state.schema_stats = (
        getattr(state.schema_agent, "_stats", {})
        if (state.schema_agent and "Schema" not in crashed) else {}
    )
    state.prompt_scrub_counts = {
        "lexicon": (state.lexicon_agent.scrub_count
                    if (state.lexicon_agent and "Lexicon" not in crashed) else 0),
        "instrument": (state.instrument_agent.scrub_count
                       if (state.instrument_agent and "Instrument" not in crashed) else 0),
    }
    state.lexicon, state.schema, state.instrument = lexicon, schema, instrument
    state.study_knowledge_package = assemble_study_knowledge_package(
        run_id=state.effective_run_id,
        datasets=[f.get("file_id", "") for f in state.dataset_files],
        schema=schema, lexicon=lexicon, instrument=instrument,
    )
    return "ok"


async def _dispatch_decide(state: _PipelineDriverState) -> str:
    """The ``decide`` node: the Judge <-> Sentinel loop (short-circuits
    on 0 blocking issues; capped at ``state.iteration_cap``, floored at
    ``BLOCKING_ISSUE_FLOOR``)."""
    judge_call_failures = 0
    sentinel_call_failures = 0
    last_judge_message_id: str | None = None
    prior_feedback = ""
    approved_decisions: list[dict[str, Any]] = []
    advisory_issues: list[dict[str, Any]] = []
    all_sentinel_overrides: list[dict[str, Any]] = []
    all_model_output_rejections: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    manager_early_escalation = False
    # Anti-loop rule: (file_id, column) -> action that Sentinel rejected as
    # blocking in a prior iteration. If Judge's revision proposes the exact
    # same action again, it isn't a real revision -- escalate immediately
    # instead of burning another iteration on a repeated proposal.
    prior_blocking_actions: dict[tuple[str, str], dict[str, Any]] = {}
    # Blocking-issue floor: a dedicated per-column counter, independent of
    # iteration_cap, that forces human_review once Sentinel has raised
    # 'blocking' on a column BLOCKING_ISSUE_FLOOR times.
    blocking_attempts: dict[tuple[str, str], int] = {}
    max_iterations = max(state.iteration_cap, BLOCKING_ISSUE_FLOOR)
    # Mutable so a granted re-ask can buy the one iteration it needs. A
    # rejection observed on the last permitted pass is the common case, and
    # without this the retry is granted and then never runs.
    iteration_budget = max_iterations
    iteration = 0
    research_dispatched = False
    reask_used = False
    s: dict[str, Any] = {}
    while iteration < iteration_budget:
        iteration += 1
        await _check_cancel(state.db, state.sid, state.on_phase)
        await state.on_phase(f"judge_iter_{iteration}", {"iteration": iteration})
        judge_ctx = await state.make_ctx("Judge")
        judge = Judge(judge_ctx)
        j = await judge.run(schema=state.schema, instrument=state.instrument, lexicon=state.lexicon,
                            statute=state.statute, praxis=state.praxis_methods,
                            prior_feedback=prior_feedback,
                            study_knowledge_package=state.study_knowledge_package)
        await state.require_accepted(judge_ctx, j, "Judge")
        judge_call_failures += judge.call_failures
        last_judge_message_id = judge.last_message_id
        decisions = j.get("decisions", [])
        # 2.10: coerce any model-proposed action/subject/category outside the
        # executable vocabulary to the fail-closed default before the hard-rule
        # table or Sentinel ever see it.
        decisions, rejections = validate_decisions(decisions)
        all_model_output_rejections.extend(rejections)
        if not research_dispatched:
            # Section 33/89: Judge's first pass through this loop *is*
            # its triage (docs section 32) -- schema/instrument/lexicon
            # only, no regulatory/method grounding yet. Its own initial
            # decisions name exactly which HIPAA categories the dataset
            # contains; that is what "demand-driven" research is
            # triggered from, once, here -- never before Judge has
            # produced this first proposal, never again on a later
            # iteration (state.statute/state.praxis_methods, once
            # populated, are reused for the rest of this loop).
            research_dispatched = True
            await _dispatch_demand_driven_research(state, _needed_hipaa_categories(decisions))
        # Sentinel deterministic hard-rules: force known direct identifiers off
        # 'human_review' before invoking the LLM Sentinel.
        decisions, overrides = apply_sentinel_hard_rules(decisions)
        if overrides:
            all_sentinel_overrides.extend(overrides)
            await state.on_phase(f"sentinel_hard_rules_iter_{iteration}",
                                 {"iteration": iteration, "overrides": overrides})
        # Cross-column rule: a retained age column means DOB must be dropped.
        decisions, age_dob_overrides = apply_age_dob_rule(decisions)
        if age_dob_overrides:
            all_sentinel_overrides.extend(age_dob_overrides)
            await state.on_phase(f"age_dob_rule_iter_{iteration}",
                                 {"iteration": iteration, "overrides": age_dob_overrides})
        # Site/facility cardinality rule: deterministic, so it runs before
        # the anti-loop check and before Sentinel ever sees the column.
        decisions, cardinality_overrides = apply_site_cardinality_rule(decisions, state.schema_stats)
        if cardinality_overrides:
            all_sentinel_overrides.extend(cardinality_overrides)
            await state.on_phase(f"site_cardinality_iter_{iteration}",
                                 {"iteration": iteration, "overrides": cardinality_overrides})
        anti_loop_forced: list[dict[str, Any]] = []
        if prior_blocking_actions:
            forced_decisions = []
            for d in decisions:
                key = (d.get("file_id"), d.get("column"))
                prior = prior_blocking_actions.get(key)
                if prior and d.get("action") == prior.get("action"):
                    forced = dict(d)
                    forced.update(
                        action="human_review",
                        reason=(
                            f"Anti-loop: Judge repeated the previously-rejected "
                            f"action {prior.get('action')!r} without a substantive "
                            "revision; forced to human review rather than "
                            "resubmitting to Sentinel for the same rejection."
                        ),
                        suggested_action=prior.get("action"),
                        suggested_confidence=d.get("confidence"),
                        suggested_reason=(
                            f"Judge repeated the previously-rejected action "
                            f"{prior.get('action')!r} without change. Sentinel's objection: "
                            f"{prior.get('issue_text') or 'see prior iteration issues'}"
                        ),
                    )
                    anti_loop_forced.append({
                        "file_id": d.get("file_id"), "column": d.get("column"),
                        "repeated_action": d.get("action"),
                    })
                    forced_decisions.append(forced)
                else:
                    forced_decisions.append(d)
            decisions = forced_decisions
            if anti_loop_forced:
                await state.on_phase(f"anti_loop_iter_{iteration}",
                                     {"iteration": iteration, "forced": anti_loop_forced})
        # Confidence floor: below 0.80 always goes to human review.
        decisions, floor_overrides = apply_confidence_floor(decisions)
        if floor_overrides:
            all_sentinel_overrides.extend(floor_overrides)
            await state.on_phase(f"confidence_floor_iter_{iteration}",
                                 {"iteration": iteration, "overrides": floor_overrides})
        await _check_cancel(state.db, state.sid, state.on_phase)
        await state.on_phase(f"sentinel_iter_{iteration}", {"iteration": iteration, "decision_count": len(decisions)})
        sentinel_ctx = await state.make_ctx("Reviewer")
        sentinel = Reviewer(sentinel_ctx)
        s = await sentinel.preview(decisions=decisions, statute=state.statute, instrument=state.instrument,
                                   files=state.dataset_files, parent_id=last_judge_message_id)
        # `Agent.__init_subclass__` (agents/base.py) wraps only each
        # subclass's own `run`, so it completes the WorkItem for
        # `Reviewer(...).run(...)` (the finalize path further down) but NOT
        # for `preview`, which is a separate public method invoked directly
        # here. Without this explicit completion the task stays `leased`
        # with an empty `output_ref`, and `Manager.accept_result`'s
        # `result != task.output_ref` check then refuses acceptance --
        # surfacing as `ResultAcceptanceError: Reviewer result was not
        # accepted` and failing the whole run after Judge already
        # succeeded. Mirrors exactly what `_completing_run` does.
        if sentinel_ctx.tasks is not None:
            await sentinel_ctx.tasks.complete(s if isinstance(s, dict) else {})
        await state.require_accepted(sentinel_ctx, s, "Reviewer")
        sentinel_call_failures += sentinel.call_failures
        # Sentinel-originated escalation: genuine ambiguity Sentinel can't
        # correct itself. Applied immediately.
        escalations = _escalation_issues(s)
        if escalations:
            decisions, escalation_overrides = apply_sentinel_escalations(decisions, escalations)
            if escalation_overrides:
                all_sentinel_overrides.extend(escalation_overrides)
                await state.on_phase(f"sentinel_escalation_iter_{iteration}",
                                     {"iteration": iteration, "overrides": escalation_overrides})
        blocking = _blocking_issues(s)
        # docs #44/#48: send the structured correction directly to Judge
        # through HandoffGateway on the (Reviewer, Judge) edge, so the
        # attempt is durably recorded and counted against
        # limits.HANDOFF_ATTEMPT_BUDGET["judge_reviewer"] -- the same
        # gateway-tracked budget category `control/handoff.py`'s
        # `_EDGE_ATTEMPT_CATEGORY` already registers this edge under.
        # Exhausting it must never fabricate certainty (docs #48): it
        # breaks the loop immediately rather than trying Judge again,
        # and `_dispatch_gate_decisions`' own final `run_decision_gates`
        # pass re-applies the confidence/blocking floors as the
        # authoritative last word on whatever decisions this loop
        # converged on, so nothing blocking silently ships.
        budget_exhausted = False
        if blocking and sentinel_ctx.handoff is not None:
            correction_payload = ReviewerHandoff(
                decision_ids=[f"{b.get('file_id', '')}:{b.get('column', '')}" for b in blocking],
                note=_summarise_issues(blocking)[:2000],
            )
            try:
                handoff_result = await sentinel_ctx.handoff.handoff(HandoffEnvelope(
                    run_id=sentinel_ctx.run_id, sender=REVIEWER, recipient=JUDGE,
                    data_class="restricted_metadata", payload=correction_payload.model_dump(),
                ))
                if not handoff_result.allowed:
                    await sentinel._log("reviewer.correction_handoff_denied", "info",
                                        {"reason": handoff_result.reason_code, "detail": handoff_result.detail})
            except BudgetExceeded as exc:
                budget_exhausted = True
                await sentinel._log("reviewer.correction_budget_exhausted", "info",
                                    {"detail": str(exc), "iteration": iteration})
        blocking_by_column = {(b.get("file_id"), b.get("column")): b for b in blocking if b.get("column")}
        for key in blocking_by_column:
            blocking_attempts[key] = blocking_attempts.get(key, 0) + 1
        for d in decisions:
            key = (d.get("file_id"), d.get("column"))
            if key in blocking_by_column and d.get("action") != "human_review":
                prior_blocking_actions[key] = {
                    "action": d.get("action"),
                    "issue_text": blocking_by_column[key].get("problem") or "",
                }
        # Every advisory issue stays in the audit trail even after early
        # approval.
        advisory_issues.extend(
            i for i in (s.get("issues") or [])
            if str(i.get("severity", "")).lower() == "advisory"
        )
        # A column at the floor never gets a fourth Judge iteration.
        decisions, blocking_floor_overrides = apply_blocking_floor(decisions, blocking_attempts)
        if blocking_floor_overrides:
            all_sentinel_overrides.extend(blocking_floor_overrides)
            await state.on_phase(f"blocking_floor_iter_{iteration}",
                                 {"iteration": iteration, "overrides": blocking_floor_overrides})
        approved_decisions = decisions
        if budget_exhausted:
            # docs #48: the correction/retry budget is exhausted -- never
            # fabricate certainty by trying Judge again. `_dispatch_gate_
            # decisions`' final `run_decision_gates` pass forces every
            # still-blocking column to human_review from here.
            await state.on_phase(f"reviewer_correction_budget_exhausted_iter_{iteration}",
                                 {"iteration": iteration})
            break
        # A rejected decision is a reason to iterate in its own right.
        # `validate_decisions` fails closed on unusable model output by
        # routing the column to human_review at confidence 0.0, which
        # discards whatever Judge actually reasoned. When the only fault is
        # the action string -- Judge answering "human_review" although the
        # prompt tells it that is not one of its options -- a human ends up
        # resolving a column the model had an opinion about, over a
        # formatting slip. Sentinel does not raise a blocking issue for
        # this (the coerced decision is safe, just uninformative), so
        # without this the loop short-circuits and the column is never
        # re-asked. Re-asking is bounded by the operator's own rigor
        # selector, and the retry is ordinary Judge output that every
        # downstream gate still inspects.
        # Only an 'action' rejection is worth another Judge call. The other
        # fields (`phi_category`, `subject`) are coerced to a safe default
        # without disturbing the action Judge chose, so re-asking would
        # spend a model call to tidy a label.
        retry_rejections = [
            r for r in rejections if r.get("column") and r.get("field") == "action"
        ]
        # One extra Judge call per run, wherever in the loop the bad answer
        # turns up. Tying this to `iteration_cap` instead would mean a
        # rejection on the final permitted iteration -- which is exactly
        # when it was observed -- gets no retry at all, while a rejection
        # that keeps recurring could still consume the whole budget.
        reask = bool(retry_rejections) and not reask_used
        if reask:
            reask_used = True
            iteration_budget = max(iteration_budget, iteration + 1)
            await state.on_phase(f"judge_rejection_reask_iter_{iteration}",
                                 {"iteration": iteration,
                                  "columns": sorted({str(r.get("column")) for r in retry_rejections})})
        if not blocking and not reask:
            # Iterate only when required. No blocking issues means Sentinel
            # has nothing PHI-critical to complain about.
            await state.on_phase(f"sentinel_short_circuit_iter_{iteration}",
                                 {"iteration": iteration,
                                  "advisory_issues": len(advisory_issues)})
            s["verdict"] = "approved"
            break
        if not reask and blocking_by_column and iteration >= state.iteration_cap and all(
            blocking_attempts.get(key, 0) >= BLOCKING_ISSUE_FLOOR for key in blocking_by_column
        ):
            break
        prior_feedback = "\n".join(
            part for part in (_summarise_issues(blocking), _summarise_rejections(retry_rejections))
            if part
        )
        if iteration < iteration_budget:
            advice = await state.manager.consult(
                agent_name="Judge", phase=f"judge_sentinel_iter_{iteration}",
                signal={"iteration": iteration, "iteration_cap": state.iteration_cap,
                        "blocking_count": len(blocking),
                        "advisory_count": len(advisory_issues),
                        "decision_count": len(decisions),
                        "judge_call_failures": judge_call_failures,
                        "sentinel_call_failures": sentinel_call_failures})
            if advice.action == "escalate_human_review":
                manager_early_escalation = True
                break

    state.decisions = decisions
    state.approved_decisions = approved_decisions if approved_decisions else []
    state.judge_call_failures = judge_call_failures
    state.sentinel_call_failures = sentinel_call_failures
    state.advisory_issues = advisory_issues
    state.all_sentinel_overrides = all_sentinel_overrides
    state.all_model_output_rejections = all_model_output_rejections
    state.manager_early_escalation = manager_early_escalation
    state.blocking_attempts = blocking_attempts
    state.sentinel_report = s
    return "ok"


async def _dispatch_gate_decisions(state: _PipelineDriverState) -> str:
    """The ``gate_decisions`` node: the D11 gate sequence's final,
    authoritative pass over whatever the Judge/Sentinel loop converged
    on, then the human-review-required computation. Returns
    ``"proceed"`` or ``"human_review_needed"`` -- both modeled
    ``TRANSITIONS`` outcomes from this node. ``run_decision_gates``
    raising ``DecisionGateFailure`` (the unmodeled ``"coverage_failed"``
    edge) propagates as an exception exactly as the pre-Wave-4b pipeline
    already did -- a disclosed, forward-compatible gap; teaching
    ``run_decision_gates`` to report a clean outcome instead of raising
    would change a contract this wave does not own.
    """
    from phi_core.control.gates import DecisionGateFailure, run_decision_gates

    approved_decisions = [scrub_decision(d) for d in state.approved_decisions]
    dictionary_by_column = {c.get("name"): c.get("description", "")
                            for c in state.lexicon.get("columns", []) if c.get("name")}
    # A fresh real activation, not a captured ctx: a test double can (and
    # does) stand in for `Judge` without retaining `.ctx`, and this stamp
    # is bookkeeping only -- `run_decision_gates` never calls the gateway
    # this context carries.
    gate_outcome = await run_decision_gates(
        decisions=approved_decisions,
        files=state.dataset_files,
        statute=state.statute,
        instrument=state.instrument,
        schema_stats=state.schema_stats,
        jurisdiction=state.session.get("jurisdiction", "us"),
        blocking_attempts=state.blocking_attempts,
        sentinel_report=state.sentinel_report if isinstance(state.sentinel_report, dict) else None,
        stage="orchestrator.final_decision",
        ctx=await state.factory.activate(
            session_id=state.sid, run_id=state.effective_run_id, agent="Judge",
            emit=state.emit, manager=state.manager, lease_owner=f"pipeline:{state.effective_run_id}",
        ),
    )
    for gate_result in gate_outcome.gate_results:
        await state.factory.store.insert("gate_results", gate_result)
    if gate_outcome.overrides:
        state.all_sentinel_overrides.extend(gate_outcome.overrides)
    keep_demotions = gate_outcome.demotions
    if keep_demotions:
        await state.on_phase("keep_verification", {"demotions": keep_demotions})
    # Re-annotate with the real lexicon-derived dictionary: the gate
    # sequence's own internal pass has no dictionary context, so this
    # overwrite is purely additive.
    approved_decisions = annotate_pending_review(gate_outcome.decisions, dictionary_by_column)
    if not gate_outcome.ok:
        raise DecisionGateFailure(gate_outcome)
    s = state.sentinel_report
    if isinstance(s, dict):
        s = dict(s)
        if isinstance(s.get("issues"), list):
            s["issues"] = [scrub_persisted_text(x) if isinstance(x, str) else x for x in s["issues"]]
        for k in ("summary", "reason", "notes"):
            if isinstance(s.get(k), str):
                s[k] = scrub_persisted_text(s[k])
    # Human review is required when a decision routes to human_review, an
    # agent exhausted supervised retries, Sentinel still has unresolved
    # BLOCKING issues after the iteration cap, or the ManagerSupervision
    # advises early escalation because the Judge/Sentinel loop is not
    # converging.
    reasons: list[str] = []
    human_needed = any(d.get("action") == "human_review" for d in approved_decisions)
    if human_needed:
        reasons.append("decision_routed_human_review")
    if state.judge_call_failures or state.sentinel_call_failures or not state.decisions:
        human_needed = True
        if state.judge_call_failures:
            reasons.append("judge_call_failure")
        if state.sentinel_call_failures:
            reasons.append("sentinel_call_failure")
        if not state.decisions:
            reasons.append("empty_decisions")
    if _blocking_issues(s):
        human_needed = True
        reasons.append("sentinel_blocking_after_cap")
        await state.on_phase("human_review_required",
                             {"reason": "sentinel still has blocking issues after cap",
                              "blocking_count": len(_blocking_issues(s))})
    if state.manager_early_escalation:
        human_needed = True
        reasons.append("manager_advisory_early_escalation")

    await state.db.sessions.update_one(
        state.session_filter,
        {"$set": {
            "agent_decisions": approved_decisions,
            "agent_sentinel_last": s,
            "agent_statute": state.statute,
            "agent_specialists": {"schema": state.schema, "instrument": state.instrument, "lexicon": state.lexicon},
            "agent_praxis": state.praxis_methods,
            "sentinel_overrides": state.all_sentinel_overrides,
            "keep_demotions": keep_demotions,
            "prompt_scrub_counts": state.prompt_scrub_counts,
            "llm_failures": {
                "judge": state.judge_call_failures,
                "sentinel": state.sentinel_call_failures,
                "empty_decisions": not state.decisions,
            },
            "model_output_rejections": state.all_model_output_rejections,
            "human_review_required": human_needed,
        }},
    )
    state.approved_decisions = approved_decisions
    state.sentinel_report = s
    state.dictionary_by_column = dictionary_by_column
    state.reasons = reasons
    return "human_review_needed" if human_needed else "proceed"


async def _dispatch_human_review_decisions(state: _PipelineDriverState) -> "str | dict[str, Any]":
    """The ``human_review_decisions`` node, reached two ways (step 6):

    Forward, from ``gate_decisions``'s ``"human_review_needed"`` outcome
    within the SAME ``run_pipeline`` call: persist and pause (D10's
    single path to ``awaiting_human_review``) -- returns a final dict,
    since there is nothing yet to report back into ``advance()``.

    Resume, via a NEW ``run_pipeline(resume_from_node=
    "human_review_decisions")`` call once ``server.py``'s human-review
    endpoint has already written the human's resolutions onto the
    session document: delegates to ``_resume_human_review_decisions``,
    which can genuinely report ``"resolved"`` back into the loop.
    """
    if state.resume_from_node == "human_review_decisions":
        return await _resume_human_review_decisions(state)
    return await _escalate_to_human_review(
        db=state.db, session_filter=state.session_filter, reasons=state.reasons,
        reasons_plain=plain_human_review_reasons(state.reasons),
        close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
        run_elapsed_s=time.perf_counter() - state.run_started,
        approved_decisions=state.approved_decisions, sentinel_report=state.sentinel_report,
        manager=state.manager, store=state.control_store, run_id=state.effective_run_id,
        node="human_review_decisions")


async def _resume_human_review_decisions(state: _PipelineDriverState) -> "str | dict[str, Any]":
    """Ports the pre-step-6 ``server.py::_handle_pipeline_resume``'s own
    logic verbatim onto the shared driver state, so resuming re-enters
    ``run_pipeline`` instead of calling ``execute_decisions`` directly
    (step 6). docs #46: every human decision triggers mandatory
    re-review -- a deterministic-only Reviewer Preview pass (no LLM
    call) over the just-resolved decisions catches a resolution that
    reintroduces an obvious hard-rule violation before Executor ever
    runs. Populates the ``state`` fields ``_dispatch_execute`` needs
    (``approved_decisions``/``omit_by_file``/``dictionary_by_column``/
    ``pending_review``/``session_review_history``) rather than calling
    ``execute_decisions`` itself -- that is ``advance()``'s job once
    this returns ``"resolved"``.
    """
    await _prepare_pipeline_state(state)
    session = state.session
    decisions = session.get("agent_decisions") or []
    pending_review = session.get("pending_review") or []
    session_review_history = session.get("session_review") or []
    dictionary_by_column = {
        c.get("name"): c.get("description", "")
        for c in (session.get("agent_specialists") or {}).get("lexicon", {}).get("columns", [])
        if c.get("name")
    }
    resolved_decisions = [d for d in decisions if d.get("action") != "human_review"]
    scrubbed_decisions = [scrub_decision(d) for d in resolved_decisions]
    omit_by_file: dict[str, set[str]] = {}
    for entry in pending_review:
        omit_by_file.setdefault(entry["file_id"], set()).add(entry["column"])

    rereview_ctx = await state.make_ctx("Reviewer")
    rereview = await Reviewer(rereview_ctx).preview(
        decisions=scrubbed_decisions, files=state.files, deterministic_only=True,
    )
    if rereview.get("preview_status") == "HUMAN_REVIEW_REQUIRED":
        reasons = ["reviewer_preview_required_after_human_decision"]
        return await _escalate_to_human_review(
            db=state.db, session_filter=state.session_filter, reasons=reasons,
            reasons_plain=plain_human_review_reasons(reasons),
            close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
            run_elapsed_s=time.perf_counter() - state.run_started,
            approved_decisions=decisions, sentinel_report=rereview,
            manager=state.manager, store=state.control_store, run_id=state.effective_run_id,
            node="human_review_decisions")
    state.approved_decisions = scrubbed_decisions
    state.omit_by_file = omit_by_file
    state.dictionary_by_column = dictionary_by_column
    state.pending_review = pending_review
    state.session_review_history = session_review_history
    return "resolved"


async def _dispatch_execute_tail(state: _PipelineDriverState) -> dict[str, Any]:
    """Registered under the ``"execute"`` node (step 6): chains
    ``_dispatch_execute`` -> ``_dispatch_verify_output`` ->
    ``_dispatch_package`` in-process via their own bare-string ``"ok"``
    outcomes -- not through ``advance()``/``workflow_runs.node`` (see
    ``_dispatch_execute``'s docstring and progress ledger Ruling 12 for
    why ``verify_output``/``package`` are not yet their own durable
    checkpoints). Any stage returning a dict short-circuits the chain
    immediately -- a terminal result or a human-review escalation.

    This registry entry's granularity matches the SAME natural function
    boundary the retired ``execute_decisions`` monolith owned (Scout/
    Ledger/Herald are opt-in post-run, no longer part of this node --
    see ``outward.run_post_run_report``); step 6 only decomposes what
    was previously one 440-line function into three narrowly-scoped,
    independently unit-testable stages sharing one outcome contract.

    ``omit_by_file`` is always threaded through now (step 6): empty for
    a fresh run (``gate_decisions`` never reaches ``execute`` while any
    column is still ``human_review``), populated with whatever columns
    remain deferred for a resumed run
    (``_resume_human_review_decisions`` sets it from the session's own
    ``pending_review``). The resume-specific completion fields
    (``session_review``/``pending_review``/``human_review_required``)
    are merged in by ``_dispatch_package`` only on that same resumed
    path, matching exactly what the now-retired
    ``server.py::_handle_pipeline_resume`` used to pass.
    """
    step = await _dispatch_execute(state)
    if isinstance(step, dict):
        return step
    step = await _dispatch_verify_output(state)
    if isinstance(step, dict):
        return step
    return await _dispatch_package(state)


_DEFAULT_DISPATCH_REGISTRY: "Mapping[str, DispatchFn]" = {
    "research": _dispatch_research,
    "specialists": _dispatch_specialists,
    "decide": _dispatch_decide,
    "gate_decisions": _dispatch_gate_decisions,
    "human_review_decisions": _dispatch_human_review_decisions,
    "execute": _dispatch_execute_tail,
}


async def run_pipeline(
    session: dict[str, Any],
    db: AsyncIOMotorDatabase,
    llm_cfg: LlmConfig,
    emit: Callable[[AgentMessage], Awaitable[None]],
    on_phase: PhaseCb,
    run_id: str | None = None,
    control_store: "ControlStore | None" = None,
    root_task_id: str | None = None,
    *,
    dispatch_registry: "Mapping[str, DispatchFn] | None" = None,
    super_orchestrator: "Manager | None" = None,
    resume_from_node: str | None = None,
) -> dict[str, Any]:
    """Thin driver (Wave 4b, docs #87): asks
    ``Manager.advance()`` for sequencing on every iteration and
    dispatches exclusively through a registry -- never decides what runs
    next on its own, and never constructs an agent class directly (see
    ``tests/test_control_run_pipeline_driver.py``'s AST invariant).

    ``dispatch_registry``/``super_orchestrator`` are an injectable test
    seam: supplying either skips the production ``ActivationFactory``/
    ``ManagerSupervision`` setup entirely (see
    ``_prepare_pipeline_state``), so a test can drive the mechanism
    itself against stub node names/handlers with no production
    infrastructure required. Every existing positional/keyword caller is
    unaffected -- both new parameters are keyword-only and default to
    the real registry and a freshly constructed ``Manager``.

    ``resume_from_node`` (step 6): set by a caller re-entering a parked
    run (e.g. a human just answered a ``human_review_decisions``
    request). The run's own stored node is already ``resume_from_node``
    -- it was left there by the earlier escalation -- so seeding the
    loop with a synthetic ``outcome="ok"`` and calling ``advance()``
    would ask ``next_node`` to resolve a ``(resume_from_node, "ok")``
    pair that was never declared, since a parked node's real outcomes
    are things like ``resolved``/``approved``/``rejected``, never
    ``ok``. Instead this recovers the run (flips ``awaiting_human_review``
    back to ``running``, fails closed to ``RESUME_FAILSAFE_NODE`` on a
    stale checkpoint version) and dispatches ``resume_from_node``
    directly, feeding its real returned outcome into the same loop
    every other node already uses.
    """
    from phi_core.control.manager import Manager as _Manager
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.tasks import TaskService
    from phi_core.control.workflow import TERMINAL_NODES

    sid = session["id"]
    effective_run_id = run_id or session.get("_pipeline_run_id") or sid
    orchestrator = (
        super_orchestrator if super_orchestrator is not None
        else _Manager(control_store, TaskService(control_store, CapabilityPolicy(llm_cfg)))
    )
    registry: "Mapping[str, DispatchFn]" = (
        dispatch_registry if dispatch_registry is not None else _DEFAULT_DISPATCH_REGISTRY
    )

    state = _PipelineDriverState(
        session=session, db=db, llm_cfg=llm_cfg, emit=emit, on_phase=on_phase,
        run_id=run_id, control_store=control_store, root_task_id=root_task_id,
        sid=sid, effective_run_id=effective_run_id, resume_from_node=resume_from_node,
    )

    if resume_from_node is not None:
        # workflow_runs tracking is not yet reliably wired into every path
        # that can park a run "awaiting_human_review" (disclosed gap:
        # `_escalate_to_human_review` opens a `HumanReviewRequest` via
        # `request_human_review`, a separate mechanism from `advance()`,
        # and nothing yet guarantees every session that can reach that
        # status also has a `WorkflowRun` whose own `node` was advanced to
        # match). `recover(expected_node=...)` closes that gap
        # authoritatively: the caller already knows, from the session
        # document's own persisted status, which node this run is
        # genuinely parked at, so a checkpoint that disagrees is
        # resynchronized to it rather than trusted blindly.
        recovered = await orchestrator.recover(
            run_id=effective_run_id, cause="pipeline_resume", expected_node=resume_from_node
        )
        if recovered.node in TERMINAL_NODES:
            return {"status": recovered.node, "phase_timings": state.phase_timings}
        step = await registry[resume_from_node](state)
        if isinstance(step, dict):
            return step
        outcome = step
    else:
        outcome = "ok"  # charter (session admission) is satisfied by the
                        # caller (server.py's session_handle route) before
                        # run_pipeline is ever invoked -- this first
                        # outcome is what carries the run past the
                        # "charter" node's own transition.
    while True:
        run = await orchestrator.advance(run_id=effective_run_id, outcome=outcome)
        node = run.node
        # A membership check against TERMINAL_NODES, not workflow.is_terminal
        # (which additionally validates `node` against the full NODES
        # vocabulary and raises on anything outside it): every production
        # `node` value already came from a real advance() call, which
        # guarantees membership via its own next_node() validation, so this
        # check is redundant safety there -- but the injectable
        # dispatch_registry test seam intentionally uses node names outside
        # that closed vocabulary (see test_control_run_pipeline_driver.py),
        # and workflow.is_terminal would wrongly raise for those instead of
        # just answering "not terminal, keep going".
        if node in TERMINAL_NODES:
            return {"status": node, "phase_timings": state.phase_timings}
        step = await registry[node](state)
        if isinstance(step, dict):
            return step
        outcome = step


def _summarise_issues(issues: list[dict[str, Any]]) -> str:
    if not issues:
        return ""
    parts = []
    for i in issues[:10]:
        parts.append(f"- {i.get('column')} ({i.get('file_id', '?')[:6]}): {i.get('problem')} -> {i.get('suggested_action')}")
    return "\n".join(parts)


def _summarise_rejections(rejections: list[dict[str, Any]]) -> str:
    """Tell Judge which of its own fields came back unusable, and why.

    Only the column name, the field name, and the value Judge itself
    proposed are echoed, all three of which originated in Judge's own
    output or in the schema headers it was given, so nothing from a
    dataset row can reach the prompt through here. The proposed value is
    truncated because a malformed model reply can be arbitrarily long.
    """
    if not rejections:
        return ""
    parts = ["Your previous answer was rejected for these columns. Re-decide each one, "
             "choosing an action from the list above and giving your honest confidence. "
             "'human_review' is not one of your options: commit to the action you believe "
             "is correct and let a low confidence say how sure you are."]
    for r in rejections[:10]:
        proposed = str(r.get("proposed"))[:60]
        parts.append(f"- {r.get('column')}: field '{r.get('field')}' was {proposed!r}, which is not valid.")
    return "\n".join(parts)


async def _empty(v):
    return v
