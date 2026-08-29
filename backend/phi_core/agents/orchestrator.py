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
  6. Auditor + Scout in parallel (Scout doesn't depend on Auditor)
  7. Ledger (Compare + Aggregate) sub-agent split
  8. Herald (Abstract + Sections) sub-agent split

Cancellation: the orchestrator checks ``is_cancelled(sid)`` between
phases. When True the pipeline exits early with status='cancelled' and
no further LLM calls are made. This is why every await for a phase is
followed by a cancel check.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from motor.motor_asyncio import AsyncIOMotorDatabase

from phi_core.control.activation import ActivationFactory
from phi_core.control.context import AgentContext
from phi_core.control.handoff import JUDGE, METHODS_EXPERT, REGULATIONS_EXPERT
from phi_core.control.records import HandoffEnvelope, MethodFinding, RegulatoryFinding
from phi_core.control.store import ControlStore

if TYPE_CHECKING:
    from phi_core.control.superorchestrator import SuperOrchestrator

from ..paths import cleanup_session_unpacked
from ..security import scrub_decision, scrub_persisted_text
from .base import ITERATION_CAP, AgentMessage
from .experts import PHIMethodsExpert, RegulationsExpert
from .llm import LlmConfig
from .manager import ExecutionHealthSupervisor
from .operator import Operator
from .outward import Herald, Ledger, Scout
from .reasoning import (
    BLOCKING_ISSUE_FLOOR,
    Auditor,
    Executor,
    Judge,
    Sentinel,
    annotate_pending_review,
    apply_age_dob_rule,
    apply_blocking_floor,
    apply_confidence_floor,
    apply_sentinel_escalations,
    apply_sentinel_hard_rules,
    apply_site_cardinality_rule,
    auditor_escalation_reason,
    materialize_auditor_disagreements,
    plain_human_review_reasons,
    validate_decisions,
)
from .reviewer import Reviewer
from .specialists import Instrument, Lexicon, Schema, UncertainHeaderCeilingExceeded

PhaseCb = Callable[[str, dict[str, Any]], Awaitable[None]]


class PipelineCancelled(Exception):
    """Raised by the orchestrator when the operator requested cancel."""

class ResultAcceptanceError(Exception):
    """Raised when SuperOrchestrator.accept_result refuses a completed
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


async def _cancel_and_await(task: "asyncio.Task[Any]") -> None:
    """Cancel ``task`` and wait for the cancellation to actually land.

    ``Task.cancel()`` alone only requests cancellation; the task keeps
    running until it next reaches an await point, and a caller that
    never awaits it again never observes whether that landed cleanly or
    raised something else. Every exit path this helper is used on is
    about to return or re-raise, so this is the last chance to fence
    Scout's still-running background task before the caller moves on.
    """
    if task.done():
        return
    task.cancel()
    try:
        await task
    except (asyncio.CancelledError, Exception):
        # The cancellation itself, or any exception the task raised on
        # its way out, is expected here and not this function's problem
        # to surface -- the caller has already decided the run is done.
        pass


async def _escalate_to_human_review(
    *, db: AsyncIOMotorDatabase, session_filter: dict[str, Any], reasons: list[str],
    reasons_plain: list[str], close_last_phase: Callable[[], Awaitable[None]],
    phase_timings: dict[str, Any], run_elapsed_s: float,
    approved_decisions: list[dict[str, Any]], sentinel_report: dict[str, Any] | None,
    manager: ExecutionHealthSupervisor, store: "ControlStore | None", run_id: str, node: str,
    audit_version: str = "",
) -> dict[str, Any]:
    """The single path by which a run becomes 'awaiting_human_review' (D10).

    Manager keeps ``close_run`` (the deterministic report) but no longer
    owns the workflow-authority write: this function persists the session
    document itself, exactly matching every other decision-mutation call
    site's own pattern, then asks ``SuperOrchestrator.request_human_review``
    to open the durable, typed ``HumanReviewRequest`` and pause the run at
    ``node``. Every current caller of the deleted
    ``Manager.escalate_to_human_review`` calls this instead.

    Tolerates an unknown ``run_id`` (``store`` is ``None``, or no
    ``WorkflowRun`` exists yet for it): the session-document write above is
    the tested, load-bearing contract every caller of this function
    depends on; the durable request is additive until every entry path
    reliably opens a run through ``SuperOrchestrator.start_run`` first.

    ``audit_version`` (D13 step 4/7): non-empty only for an Auditor-verdict
    escalation (``node="human_review_audit"`` with a real audit content
    hash); the reviewer's later ``confirm_auditor_confidence`` submission
    must echo it back, so a confirmation against a since-superseded audit
    verdict is rejected rather than silently accepted.
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
        from phi_core.control.policy import CapabilityPolicy
        from phi_core.control.superorchestrator import SuperOrchestrator
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
            await SuperOrchestrator(store, TaskService(store, CapabilityPolicy(None))).request_human_review(
                run_id=run_id, node=node, reason_codes=reasons, decision_version=current_decision_version,
                audit_version=audit_version,
            )
        except WorkflowError as exc:
            if not str(exc).startswith("unknown run_id:"):
                raise
    return {"status": "awaiting_human_review", "decisions": approved_decisions,
            "sentinel": sentinel_report, "phase_timings": phase_timings,
            "manager_report": report}


async def execute_decisions(
    *,
    db: AsyncIOMotorDatabase,
    sid: str,
    session: dict[str, Any],
    session_filter: dict[str, Any],
    files: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
    statute: dict[str, Any],
    praxis_methods: dict[str, Any],
    dictionary_by_column: dict[str, str],
    make_ctx: Callable[[str], Awaitable[AgentContext]],
    make_child_ctx: Callable[[str, str], Awaitable[AgentContext]],
    complete_and_accept: Callable[[AgentContext, dict[str, Any]], Awaitable[bool]],
    manager: ExecutionHealthSupervisor,
    on_phase: PhaseCb,
    close_last_phase: Callable[[], Awaitable[None]],
    phase_timings: dict[str, dict[str, float]],
    run_started: float,
    omit_by_file: dict[str, set[str]] | None = None,
    sentinel_report: dict[str, Any] | None = None,
    extra_completion_fields: dict[str, Any] | None = None,
    extra_result_fields: dict[str, Any] | None = None,
    run_id: str = "",
    store: "ControlStore | None" = None,
) -> dict[str, Any]:
    """The D9 ``execute`` node onward -- Executor, Operator, Reviewer,
    Publish Guard, Auditor/Scout, Ledger, Herald, and the terminal
    completion write.

    Reached identically whether ``gate_decisions`` transitioned here
    directly (``proceed``: a fresh run with no ``human_review`` decisions
    left) or via ``human_review_decisions`` (``resolved``: a resume,
    possibly with some columns still deferred through ``omit_by_file``).
    """
    async def require_accepted(ctx: AgentContext, result: dict[str, Any], agent: str) -> None:
        if not await complete_and_accept(ctx, result):
            raise ResultAcceptanceError(f"{agent} result was not accepted")

    await on_phase("executor", {"decision_count": len(decisions)})
    try:
        executor_ctx = await make_ctx("Executor")
        exec_out = await Executor(executor_ctx).run(
            files=files, decisions=decisions, omit_by_file=omit_by_file)
        await require_accepted(executor_ctx, exec_out, "Executor")
    except Exception as exc:
        # Executor is deterministic and irreversible (writes exports to disk);
        # a crash here must never be papered over by an LLM's advice.
        # consult() fails open by design and is never a safety gate, so the
        # escalation itself is unconditional, fixed code -- see manager.py.
        await manager._log("executor.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}"})
        return await _escalate_to_human_review(
            db=db, session_filter=session_filter, reasons=["executor_crashed"],
            reasons_plain=plain_human_review_reasons(["executor_crashed"]),
            close_last_phase=close_last_phase, phase_timings=phase_timings,
            run_elapsed_s=time.perf_counter() - run_started,
            approved_decisions=decisions, sentinel_report=sentinel_report,
            manager=manager, store=store, run_id=run_id, node="human_review_decisions")

    # Reversal key: the mandatory second deliverable (PHI-handled output
    # plus the key to reverse it), distinct from the optional publishing
    # stack below. Persisted now, separate from `exports`, never bundled.
    if exec_out.get("reversal_key_blob"):
        await db.sessions.update_one(session_filter, {"$set": {
            "reversal_key_blob": exec_out["reversal_key_blob"],
            "reversal_key_created_at": datetime.now(timezone.utc).isoformat(),
        }})

    # Scout has no dependency on Operator, Reviewer, Publish Guard, or
    # Auditor, so it starts here and runs in the background across all of
    scout_ctx = await make_ctx("Scout")
    scout_agent = Scout(scout_ctx)
    scout_task = asyncio.create_task(scout_agent.run())

    # Operator: deterministic self-verification of what Executor wrote,
    # one stage before Publish Guard, mirroring the Judge/Sentinel split one
    # stage later. exec_out["exports"] stays Executor's own factual record
    # of what it wrote and is never mutated here; `exports` is the
    # Operator-then-Reviewer-filtered view every later step in this
    # function uses.
    await on_phase("operator", {"decision_count": len(decisions)})
    try:
        operator_ctx = await make_ctx("Operator")
        op_out = await Operator(operator_ctx).run(
            files=files, decisions=decisions, exports=exec_out["exports"], omit_by_file=omit_by_file)
        await require_accepted(operator_ctx, op_out, "Operator")
    except Exception as exc:
        # Fail open into the existing failed-file machinery: a file Operator
        # cannot verify is dropped from exports exactly like an unreadable
        # file already is, rather than trusting it or inventing a new path.
        await manager._log("operator.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}"})
        op_out = {"failed_file_ids": list(exec_out["exports"].keys()), "verdicts": []}
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
        reviewer_ctx = await make_ctx("Reviewer")
        rv_out = await Reviewer(reviewer_ctx).run(
            decisions=decisions,
            operator_result={"failed_file_ids": op_failed_ids, "verdicts": op_out["verdicts"]},
            exports=exports,
            omit_by_file=omit_by_file,
        )
        await require_accepted(reviewer_ctx, rv_out, "Reviewer")
    except Exception as exc:
        # Same fail-open shape as Operator above: an unverifiable file is
        # dropped from exports, never trusted.
        await manager._log("reviewer.crashed", "info",
                           {"error_kind": f"exception:{type(exc).__name__}"})
        rv_out = {"exports": {}, "findings": []}
    reviewer_blocked_ids = sorted(set(exports) - set(rv_out["exports"]))
    exports = rv_out["exports"]
    # A run with any column still deferred via `omit_by_file` (a resume
    # that left some columns pending) can never be "complete" -- a fresh
    # run's `omit_by_file` is always empty, since `gate_decisions` never
    # reaches this function while a `human_review` decision is present,
    # so this reduces to the original `op_failed_ids or reviewer_blocked_ids`
    # check there. `decisions` itself is deliberately not consulted here:
    # a resume caller pre-filters `decisions` to exclude every still-
    # pending column before calling this function (its `omit_by_file` is
    # the authoritative record of what remains pending).
    final_status = ("partially_complete" if (op_failed_ids or reviewer_blocked_ids or omit_by_file)
                    else "complete")

    # Advisory checkpoint: a Manager consult here is never a safety gate
    # (Publish Guard below remains the deterministic boundary regardless of
    # its advice); it only lets a systemically bad run reach a human sooner
    # than Publish Guard's blunter "blocked" report would.
    coverage_advice = await manager.consult(
        agent_name="Reviewer", phase="reviewer",
        signal={"operator_failed_count": len(op_failed_ids),
                "reviewer_blocked_count": len(reviewer_blocked_ids),
                "decision_count": len(decisions)})
    if coverage_advice.action == "escalate_human_review":
        await db.sessions.update_one(session_filter, {"$set": {
            "reviewer_findings": rv_out["findings"], "operator_failures": op_failed_ids}})
        # Recursive cancellation (D9/Phase 4 step 5): this return path
        # previously left Scout's background task running unobserved --
        # the "Scout leak" the plan names explicitly. Fence it here,
        # before returning, same as every other exit path below.
        await _cancel_and_await(scout_task)
        return await _escalate_to_human_review(
            db=db, session_filter=session_filter, reasons=["manager_advisory_coverage_escalation"],
            reasons_plain=plain_human_review_reasons(["manager_advisory_coverage_escalation"]),
            close_last_phase=close_last_phase, phase_timings=phase_timings,
            run_elapsed_s=time.perf_counter() - run_started,
            approved_decisions=decisions, sentinel_report=sentinel_report,
            manager=manager, store=store, run_id=run_id, node="human_review_audit")

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
            exports, decisions=decisions, jurisdiction=session.get("jurisdiction", "us")
        ).to_dict()
    else:
        # Executor itself produced nothing exportable this round (e.g.
        # every column of the only dataset is still deferred). This is a
        # legitimate empty-so-far state, not a leak.
        guard_report = {"status": "clean", "results": [], "scanned": 0, "blocked": 0}
    if store is not None and run_id and guard_report.get("results"):
        from phi_core.control.artifacts import ArtifactService, register_guard_rejections
        await register_guard_rejections(
            ArtifactService(store, session_id=sid, run_id=run_id), guard_report=guard_report,
        )
    await on_phase("publish_guard", {"status": guard_report["status"],
                                     "scanned": guard_report["scanned"],
                                     "blocked": guard_report["blocked"]})

    if guard_report["status"] != "clean":
        await close_last_phase()
        manager_report = await manager.close_run("blocked")
        await db.sessions.update_one(
            session_filter,
            {"$set": {
                "status": "blocked",
                "guard_report": guard_report,
                "export_paths": exports,
                "agent_decisions": decisions,
                "phase_timings": phase_timings,
                "run_elapsed_s": round(time.perf_counter() - run_started, 3),
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "manager_report": manager_report,
                "reviewer_findings": rv_out["findings"],
                "operator_failures": op_failed_ids,
            }},
        )
        await _cancel_and_await(scout_task)
        cleanup_session_unpacked(sid)
        return {"status": "blocked", "guard": guard_report,
                "decisions": decisions, "phase_timings": phase_timings}

    try:
        await _check_cancel(db, sid, on_phase)
    except PipelineCancelled:
        await _cancel_and_await(scout_task)
        raise

    # Auditor (Scout already started earlier, in parallel with
    # Operator/Reviewer/Publish Guard). Ledger + Herald still need
    # Auditor's metrics + Scout's landscape so they wait on both here.
    await on_phase("auditor_scout", {})
    from phi_core.control.artifacts import _hash_file
    artifact_refs: list[tuple[str, str]] = []
    for file_id, path in exports.items():
        try:
            sha256, _size = _hash_file(path)
        except OSError:
            continue
        artifact_refs.append((file_id, sha256))
    auditor_agent = Auditor(await make_ctx("Auditor"))
    audit, scout, benchmark = await asyncio.gather(
        auditor_agent.run(
            decisions=decisions, exports=exports, files=files, artifact_refs=artifact_refs,
            statute=statute, praxis_methods=praxis_methods,
            audit_controls=(
                await asyncio.gather(
                    store.find_many("evidence_claims", {"run_id": run_id}),
                    store.find_many("gate_results", {"run_id": run_id}),
                )
                if store is not None and run_id else ([], [])
            ),
        ),
        scout_task,
        _empty(None),   # placeholder for future synthetic benchmark run
        return_exceptions=True,
    )
    # Auditor/Scout are presentational (Publish Guard already gated the
    # export above); an unhandled exception here must not crash a run that
    # already succeeded. Log it and fall back to a report that visibly says
    # "not verified" rather than claiming a clean audit it never performed.
    audit_crashed = isinstance(audit, Exception)
    if audit_crashed:
        await auditor_agent._log("auditor.crashed", "info",
                                  {"error": f"{type(audit).__name__}: {audit}"})
        audit = {"verdict": "issues", "issues": [{"file": "", "problem": "Auditor crashed; not verified"}],
                 "metrics": {}, "confidence": 0.0,
                 "summary": "Auditor raised an exception; audit not performed."}
    if isinstance(scout, Exception):
        await scout_agent._log("scout.crashed", "info", {"error": f"{type(scout).__name__}: {scout}"})
        scout = {}
    if isinstance(benchmark, Exception):
        benchmark = None

    audit_advice = await manager.consult(
        agent_name="Auditor", phase="auditor_scout",
        signal={"audit_verdict": str(audit.get("verdict")), "audit_crashed": audit_crashed})
    # The confidence-floor escalation is a deterministic gate, not an LLM
    # advisory: this is the design doc's "second human review", distinct
    # from Sentinel's pre-execution round -- it must fire on the numbers
    # every time, never fail open the way `consult()` legitimately does.
    auditor_reason = auditor_escalation_reason(audit, artifact_refs=dict(artifact_refs))
    if audit_advice.action == "escalate_human_review" or auditor_reason:
        reasons = [r for r in ("manager_advisory_audit_escalation" if audit_advice.action == "escalate_human_review" else None,
                               auditor_reason) if r]
        # Turn any per-column Auditor disagreement into a resolvable
        # human_review decision, so this second review has an actual
        # lever for the reviewer to pull rather than only a status flag
        # that can be blindly resubmitted against a non-deterministic model.
        decisions = materialize_auditor_disagreements(decisions, audit, dictionary_by_column)
        # D13 step 4/7: a content hash of the actual verdict, not an
        # incrementing counter -- any change to Auditor's issues/metrics
        # for this run mints a new value, so a reviewer's later
        # `confirm_auditor_confidence` submission can be checked against
        # exactly the verdict they saw, not merely "some verdict existed".
        # Persisted onto the session document (not just the durable
        # `HumanReviewRequest`) so the frontend's plain `GET
        # /api/sessions/{sid}` poll can render the confirm control without
        # a second endpoint.
        audit_version = hashlib.sha256(
            json.dumps(audit, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        await db.sessions.update_one(session_filter, {"$set": {
            "guard_report": guard_report, "export_paths": exports,
            "reviewer_findings": rv_out["findings"], "operator_failures": op_failed_ids,
            "audit": audit, "agent_decisions": decisions, "audit_version": audit_version,
        }})
        return await _escalate_to_human_review(
            db=db, session_filter=session_filter, reasons=reasons,
            reasons_plain=plain_human_review_reasons(reasons),
            close_last_phase=close_last_phase, phase_timings=phase_timings,
            run_elapsed_s=time.perf_counter() - run_started,
            approved_decisions=decisions, sentinel_report=sentinel_report,
            manager=manager, store=store, run_id=run_id, node="human_review_audit",
            audit_version=audit_version)

    await _check_cancel(db, sid, on_phase)

    # Ledger (split into Compare + Aggregate under the hood). D5 plan step
    # 5: Compare/Aggregate are durable child work under Ledger's own task,
    # not a bare root enqueue, with SuperOrchestrator.accept_result as the
    # sole authority accepting each subagent's material result.
    await on_phase("ledger", {})
    ledger_ctx = await make_ctx("Ledger")
    ledger = await Ledger(
        ledger_ctx,
        await make_child_ctx("Ledger.Compare", ledger_ctx.task_id),
        await make_child_ctx("Ledger.Aggregate", ledger_ctx.task_id),
        complete_and_accept=complete_and_accept,
    ).run(decisions=decisions, audit=audit, scout=scout, benchmark_result=benchmark)
    if ledger_ctx.tasks is not None:
        await ledger_ctx.tasks.complete(ledger)
    await require_accepted(ledger_ctx, ledger, "Ledger")

    await _check_cancel(db, sid, on_phase)

    # Herald (split into Abstract + Sections under the hood so no LLM
    # call exceeds the 90 s hard timeout). Same durable-child-work
    # treatment as Ledger above.
    await on_phase("herald", {})
    herald_ctx = await make_ctx("Herald")
    herald = await Herald(
        herald_ctx,
        await make_child_ctx("Herald.Abstract", herald_ctx.task_id),
        await make_child_ctx("Herald.Sections", herald_ctx.task_id),
        complete_and_accept=complete_and_accept,
    ).run(ledger=ledger, audit=audit, target_venue=session.get("target_venue") or "JAMIA Open")
    if herald_ctx.tasks is not None:
        await herald_ctx.tasks.complete(herald)
    await require_accepted(herald_ctx, herald, "Herald")

    await close_last_phase()
    manager_report = await manager.close_run(final_status)
    result = {
        "status": final_status,
        "decisions": decisions,
        "audit": audit,
        "scout": scout,
        "ledger": ledger,
        "herald": herald,
        "exports": exports,
        "guard": guard_report,
        "phase_timings": phase_timings,
        "run_elapsed_s": round(time.perf_counter() - run_started, 3),
        "manager_report": manager_report,
        "operator_failures": op_failed_ids,
        "reviewer_findings": rv_out["findings"],
    }
    result.update(extra_result_fields or {})
    completion_set = {
        "agent_audit": audit,
        "agent_ledger": ledger,
        "agent_herald": herald,
        "agent_scout": scout,
        "guard_report": guard_report,
        "export_paths": exports,
        "status": final_status,
        "phase_timings": phase_timings,
        "run_elapsed_s": result["run_elapsed_s"],
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "manager_report": manager_report,
        "operator_failures": op_failed_ids,
        "reviewer_findings": rv_out["findings"],
    }
    completion_set.update(extra_completion_fields or {})
    await db.sessions.update_one(session_filter, {"$set": completion_set})
    if final_status == "complete":
        cleanup_session_unpacked(sid)
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
    ``ExecutionHealthSupervisor`` setup work.
    """

    def __init__(
        self, *, session: dict[str, Any], db: AsyncIOMotorDatabase, llm_cfg: LlmConfig,
        emit: Callable[[AgentMessage], Awaitable[None]], on_phase: PhaseCb,
        run_id: str | None, control_store: "ControlStore | None", root_task_id: str | None,
        sid: str, effective_run_id: str,
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
        self.manager: "ExecutionHealthSupervisor | None" = None
        self.make_ctx: Callable[[str], Awaitable[AgentContext]] | None = None
        self.make_child_ctx: Callable[[str, str], Awaitable[AgentContext]] | None = None
        self.complete_and_accept: Callable[[AgentContext, dict[str, Any]], Awaitable[bool]] | None = None
        self.require_accepted: Callable[[AgentContext, dict[str, Any], str], Awaitable[None]] | None = None
        self.hipaa_cats: list[str] = []
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


async def _noop_close_last_phase() -> None:
    return None


DispatchFn = Callable[[_PipelineDriverState], Awaitable["str | dict[str, Any]"]]


async def _prepare_pipeline_state(state: _PipelineDriverState) -> None:
    """Populate every shared field the production ``_dispatch_*`` handlers
    need: dataset file partitioning, phase-timing instrumentation, the
    durable ``ActivationFactory``, and the ``ExecutionHealthSupervisor``
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

    manager = ExecutionHealthSupervisor(await make_ctx("Manager"), db=state.db)
    state.manager = manager
    manager_result = await manager.run(
        roster=["Lexicon", "Schema", "Instrument", "RegulationsExpert", "PHIMethodsExpert", "Judge",
                "Sentinel", "Executor", "Auditor", "Scout", "Ledger", "Herald"],
        phase_plan=["specialists", "statute", "praxis", "judge_iter", "sentinel_iter",
                    "executor", "publish_guard", "auditor_scout", "ledger", "herald"],
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
    """The ``research`` node: launches only the Lexicon/Schema/Instrument
    specialist tasks (stashed on ``state`` for ``_dispatch_specialists``
    to await). RegulationsExpert and PHIMethodsExpert no longer launch
    here -- section 33 forbids an unconditional broad research call at
    run start. See ``_dispatch_demand_driven_research`` for where and
    when they now launch instead."""
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
    return "ok"


_SPECIALIST_DEGRADED_RESULT: dict[str, dict[str, Any]] = {
    "Lexicon": {"columns": [], "notes": ""},
    "Schema": {"columns": []},
    "Instrument": {"fields": []},
}


async def _dispatch_specialists(state: _PipelineDriverState) -> "str | dict[str, Any]":
    """The ``specialists`` node: await the Lexicon/Schema/Instrument
    tasks ``_dispatch_research`` already launched.

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
    # Deterministic guardian query broker: the ExecutionHealthSupervisor
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
    research_dispatched = False
    s: dict[str, Any] = {}
    for iteration in range(1, max_iterations + 1):
        await _check_cancel(state.db, state.sid, state.on_phase)
        await state.on_phase(f"judge_iter_{iteration}", {"iteration": iteration})
        judge_ctx = await state.make_ctx("Judge")
        judge = Judge(judge_ctx)
        j = await judge.run(schema=state.schema, instrument=state.instrument, lexicon=state.lexicon,
                            statute=state.statute, praxis=state.praxis_methods,
                            prior_feedback=prior_feedback)
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
        sentinel_ctx = await state.make_ctx("Sentinel")
        sentinel = Sentinel(sentinel_ctx)
        s = await sentinel.run(decisions=decisions, statute=state.statute, instrument=state.instrument,
                               parent_id=last_judge_message_id)
        await state.require_accepted(sentinel_ctx, s, "Sentinel")
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
        if not blocking:
            # Iterate only when required. No blocking issues means Sentinel
            # has nothing PHI-critical to complain about.
            await state.on_phase(f"sentinel_short_circuit_iter_{iteration}",
                                 {"iteration": iteration,
                                  "advisory_issues": len(advisory_issues)})
            s["verdict"] = "approved"
            break
        if blocking_by_column and iteration >= state.iteration_cap and all(
            blocking_attempts.get(key, 0) >= BLOCKING_ISSUE_FLOOR for key in blocking_by_column
        ):
            break
        prior_feedback = _summarise_issues(blocking)
        if iteration < max_iterations:
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
    # BLOCKING issues after the iteration cap, or the ExecutionHealthSupervisor
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


async def _dispatch_human_review_decisions(state: _PipelineDriverState) -> dict[str, Any]:
    """The ``human_review_decisions`` node reached via
    ``gate_decisions``'s ``"human_review_needed"`` outcome: persist and
    pause. Resuming happens through a separate entry path
    (``server.py``'s human-review-resume route calling
    ``execute_decisions`` directly, not by continuing this same
    ``run_pipeline`` invocation), so this handler always returns a
    final dict -- there is no ``"resolved"`` outcome for this node to
    report back into ``advance()`` from inside this call."""
    return await _escalate_to_human_review(
        db=state.db, session_filter=state.session_filter, reasons=state.reasons,
        reasons_plain=plain_human_review_reasons(state.reasons),
        close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
        run_elapsed_s=time.perf_counter() - state.run_started,
        approved_decisions=state.approved_decisions, sentinel_report=state.sentinel_report,
        manager=state.manager, store=state.control_store, run_id=state.effective_run_id,
        node="human_review_decisions")


async def _dispatch_execute(state: _PipelineDriverState) -> dict[str, Any]:
    """The ``execute`` node onward: delegates to ``execute_decisions``,
    which already covers -- by its own docstring -- "Executor, Operator,
    Reviewer, Publish Guard, Auditor/Scout, Ledger, Herald, and the
    terminal completion write" as one unit. This registry entry's
    granularity matches that existing, natural function boundary rather
    than re-decomposing it onto the state machine's finer per-node
    vocabulary (verify_operator/verify_reviewer/publish_guard/audit/
    human_review_audit/report_ledger/report_herald/publish) -- none of
    that internal sequencing is itself a governed agent handoff Wave 4b
    is asked to arbitrate; it is deterministic verification chaining
    ``execute_decisions`` already owns unchanged.
    """
    return await execute_decisions(
        db=state.db, sid=state.sid, session=state.session, session_filter=state.session_filter,
        files=state.files, decisions=state.approved_decisions,
        statute=state.statute, praxis_methods=state.praxis_methods,
        dictionary_by_column=state.dictionary_by_column,
        make_ctx=state.make_ctx, make_child_ctx=state.make_child_ctx,
        complete_and_accept=state.complete_and_accept,
        manager=state.manager, on_phase=state.on_phase,
        close_last_phase=state.close_last_phase, phase_timings=state.phase_timings,
        run_started=state.run_started, sentinel_report=state.sentinel_report,
        extra_completion_fields={"advisory_issues": state.advisory_issues, "iteration_cap": state.iteration_cap},
        extra_result_fields={"advisory_issues": state.advisory_issues, "iteration_cap": state.iteration_cap},
        run_id=state.effective_run_id, store=state.control_store,
    )


_DEFAULT_DISPATCH_REGISTRY: "Mapping[str, DispatchFn]" = {
    "research": _dispatch_research,
    "specialists": _dispatch_specialists,
    "decide": _dispatch_decide,
    "gate_decisions": _dispatch_gate_decisions,
    "human_review_decisions": _dispatch_human_review_decisions,
    "execute": _dispatch_execute,
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
    super_orchestrator: "SuperOrchestrator | None" = None,
) -> dict[str, Any]:
    """Thin driver (Wave 4b, docs #87): asks
    ``SuperOrchestrator.advance()`` for sequencing on every iteration and
    dispatches exclusively through a registry -- never decides what runs
    next on its own, and never constructs an agent class directly (see
    ``tests/test_control_run_pipeline_driver.py``'s AST invariant).

    ``dispatch_registry``/``super_orchestrator`` are an injectable test
    seam: supplying either skips the production ``ActivationFactory``/
    ``ExecutionHealthSupervisor`` setup entirely (see
    ``_prepare_pipeline_state``), so a test can drive the mechanism
    itself against stub node names/handlers with no production
    infrastructure required. Every existing positional/keyword caller is
    unaffected -- both new parameters are keyword-only and default to
    the real registry and a freshly constructed ``SuperOrchestrator``.
    """
    from phi_core.control.policy import CapabilityPolicy
    from phi_core.control.superorchestrator import SuperOrchestrator as _SuperOrchestrator
    from phi_core.control.tasks import TaskService
    from phi_core.control.workflow import TERMINAL_NODES

    sid = session["id"]
    effective_run_id = run_id or session.get("_pipeline_run_id") or sid
    orchestrator = (
        super_orchestrator if super_orchestrator is not None
        else _SuperOrchestrator(control_store, TaskService(control_store, CapabilityPolicy(llm_cfg)))
    )
    registry: "Mapping[str, DispatchFn]" = (
        dispatch_registry if dispatch_registry is not None else _DEFAULT_DISPATCH_REGISTRY
    )

    state = _PipelineDriverState(
        session=session, db=db, llm_cfg=llm_cfg, emit=emit, on_phase=on_phase,
        run_id=run_id, control_store=control_store, root_task_id=root_task_id,
        sid=sid, effective_run_id=effective_run_id,
    )

    outcome = "ok"  # charter (session admission) is satisfied by the caller
                    # (server.py's session_handle route) before run_pipeline
                    # is ever invoked -- this first outcome is what carries
                    # the run past the "charter" node's own transition.
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


async def _empty(v):
    return v
